"""Prompt engineering, robust JSON parsing, retry logic, and fallback handling."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from diffwise.backends.base import LLMBackend
from diffwise.git_diff import DiffChunk

Severity = Literal["bug", "security", "style", "suggestion"]

SEVERITY_ORDER: dict[Severity, int] = {
    "security": 0,
    "bug": 1,
    "style": 2,
    "suggestion": 3,
}

_SEVERITY_ALIASES: dict[str, Severity] = {
    "security": "security",
    "sec": "security",
    "vuln": "security",
    "vulnerability": "security",
    "bug": "bug",
    "error": "bug",
    "critical": "bug",
    "high": "bug",
    "defect": "bug",
    "style": "style",
    "lint": "style",
    "formatting": "style",
    "format": "style",
    "suggestion": "suggestion",
    "suggest": "suggestion",
    "improvement": "suggestion",
    "refactor": "suggestion",
    "nit": "suggestion",
    "info": "suggestion",
    "warning": "bug",
}

_OPEN_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?")
_CLOSE_FENCE_RE = re.compile(r"\n?\s*```\s*$")


class FindingParseError(ValueError):
    """Raised when an LLM response cannot be parsed into structured findings."""


@dataclass(slots=True)
class Finding:
    """Structured code review finding returned by the LLM."""

    file: str
    line: int | None
    severity: Severity
    title: str
    explanation: str
    suggested_fix: str = ""


@dataclass(slots=True)
class FallbackNote:
    """Plain-text review output used when JSON parsing fails after retry."""

    file: str
    text: str


@dataclass(slots=True)
class ChunkReviewResult:
    """Review outcome for a single diff chunk."""

    findings: list[Finding] = field(default_factory=list)
    fallback_note: FallbackNote | None = None
    used_retry: bool = False


def build_system_prompt(rules: Sequence[str] = ()) -> str:
    """Build the senior code reviewer system prompt enforcing the JSON schema."""
    base = (
        "You are a senior staff software engineer conducting a focused pre-commit code review.\n"
        "Review ONLY the provided unified diff chunk. Diff lines include `[L123]` prefixes\n"
        "showing the 1-based line number in the new file (and `[OLD123]` for deleted lines).\n\n"
        "Return ONLY a valid JSON array of finding objects (no prose outside JSON, no markdown\n"
        "fences). Each object in the array MUST have exactly these keys:\n"
        '  - "file": string (file path from the diff)\n'
        '  - "line": integer or null (1-based line from `[L...]`, or null if deleted/file-level)\n'
        '  - "severity": one of "bug", "security", "style", "suggestion"\n'
        '  - "title": short concise summary of the issue\n'
        '  - "explanation": clear technical explanation of the defect or risk\n'
        '  - "suggested_fix": concrete replacement code or actionable fix\n\n'
        "Review standards:\n"
        "- Prioritize real bugs, logic/off-by-one errors, security vulnerabilities, injection\n"
        "  risks, hardcoded secrets, unhandled exceptions, race conditions, and resource leaks.\n"
        "- Do not invent issues or report issues on unchanged code unless broken by the diff.\n"
        "- If there are no issues worth reporting, return [] strictly."
    )
    clean_rules = [r.strip() for r in rules if r.strip()]
    if clean_rules:
        bullet_list = "\n".join(f"- {rule}" for rule in clean_rules)
        base = f"{base}\n\nProject-specific review rules:\n{bullet_list}"
    return base


def build_chunk_prompt(chunk: DiffChunk) -> str:
    """Format a `DiffChunk` into the user prompt for LLM inspection."""
    part_label = (
        f" (chunk {chunk.chunk_index} of {chunk.total_chunks})"
        if chunk.total_chunks > 1
        else ""
    )
    return (
        f"Review this {chunk.language} diff for `{chunk.file_path}`{part_label}.\n"
        "Return ONLY a JSON array matching the schema, or [] if no issues are found.\n\n"
        f"{chunk.to_patch(include_line_numbers=True)}"
    )


def _build_retry_prompt(chunk: DiffChunk, invalid_response: str) -> str:
    snippet = invalid_response.strip()[:1200]
    return (
        "Your previous response could not be parsed as valid JSON.\n"
        "Respond ONLY with a valid JSON array of objects matching:\n"
        '[{"file": str, "line": int|null, "severity": "bug"|"security"|"style"|"suggestion", '
        '"title": str, "explanation": str, "suggested_fix": str}]\n'
        "If there are no issues, respond with []. Do not include markdown fences or prose.\n\n"
        f"Diff being reviewed:\n{chunk.to_patch(include_line_numbers=True)}\n\n"
        f"Previous invalid response (for correction):\n{snippet}"
    )


def _strip_outer_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _OPEN_FENCE_RE.sub("", stripped, count=1)
        if stripped.rstrip().endswith("```"):
            stripped = _CLOSE_FENCE_RE.sub("", stripped, count=1)
    return stripped.strip()


def _decode_json_candidate(raw_text: str) -> Any:
    """Try strict JSON load, outer fence stripping, and `raw_decode` scanning."""
    cleaned = raw_text.strip()
    if not cleaned:
        raise FindingParseError("Empty response from LLM.")

    candidates = [cleaned]
    unfenced = _strip_outer_code_fences(cleaned)
    if unfenced and unfenced != cleaned:
        candidates.append(unfenced)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for candidate in candidates:
        for idx, ch in enumerate(candidate):
            if ch not in ("[", "{"):
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate, idx)
                return parsed
            except json.JSONDecodeError:
                continue

    raise FindingParseError("Response did not contain valid JSON.")


def _normalize_severity(raw_value: Any) -> Severity:
    if not isinstance(raw_value, str):
        raise FindingParseError(f"Invalid finding severity: {raw_value!r}")
    normalized = raw_value.strip().lower()
    if normalized in _SEVERITY_ALIASES:
        return _SEVERITY_ALIASES[normalized]
    raise FindingParseError(f"Unknown finding severity: {raw_value!r}")


def _normalize_line(raw_value: Any) -> int | None:
    if raw_value is None or isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int):
        return raw_value if raw_value > 0 else None
    if isinstance(raw_value, str):
        digits = raw_value.strip().lstrip("L")
        if digits.isdigit():
            parsed = int(digits)
            return parsed if parsed > 0 else None
    return None


def _coerce_finding(item: Any, default_file: str) -> Finding:
    if not isinstance(item, dict):
        raise FindingParseError(f"Finding entry must be a JSON object, got {type(item).__name__}.")

    title_raw = item.get("title")
    explanation_raw = item.get("explanation")
    if not isinstance(title_raw, str) or not title_raw.strip():
        raise FindingParseError("Finding object is missing a non-empty 'title'.")
    if not isinstance(explanation_raw, str) or not explanation_raw.strip():
        explanation_raw = title_raw

    severity = _normalize_severity(item.get("severity", "suggestion"))
    file_raw = item.get("file")
    file_path = file_raw.strip() if isinstance(file_raw, str) and file_raw.strip() else default_file
    line = _normalize_line(item.get("line"))
    fix_raw = item.get("suggested_fix", "")
    suggested_fix = fix_raw.strip() if isinstance(fix_raw, str) else ""

    return Finding(
        file=file_path,
        line=line,
        severity=severity,
        title=title_raw.strip(),
        explanation=explanation_raw.strip(),
        suggested_fix=suggested_fix,
    )


def parse_findings_json(raw_text: str, *, default_file: str = "") -> list[Finding]:
    """Parse raw LLM text into validated `Finding` objects."""
    parsed = _decode_json_candidate(raw_text)

    if isinstance(parsed, dict):
        for wrapper_key in ("findings", "issues", "reviews"):
            if isinstance(parsed.get(wrapper_key), list):
                parsed = parsed[wrapper_key]
                break
        else:
            if "title" in parsed and ("severity" in parsed or "explanation" in parsed):
                parsed = [parsed]
            else:
                raise FindingParseError("JSON object did not contain a findings list.")

    if not isinstance(parsed, list):
        raise FindingParseError(f"Expected JSON array of findings, got {type(parsed).__name__}.")

    return [_coerce_finding(entry, default_file=default_file) for entry in parsed]


def review_chunk(
    backend: LLMBackend,
    chunk: DiffChunk,
    *,
    rules: Sequence[str] = (),
) -> ChunkReviewResult:
    """Review a single diff chunk, retrying once on invalid JSON and falling back to text."""
    system_prompt = build_system_prompt(rules=rules)
    user_prompt = build_chunk_prompt(chunk)

    raw_response = backend.review(user_prompt, system_prompt=system_prompt)
    try:
        findings = parse_findings_json(raw_response, default_file=chunk.file_path)
        return ChunkReviewResult(findings=findings)
    except FindingParseError:
        pass

    retry_prompt = _build_retry_prompt(chunk, raw_response)
    retry_response = backend.review(retry_prompt, system_prompt=system_prompt)
    try:
        findings = parse_findings_json(retry_response, default_file=chunk.file_path)
        return ChunkReviewResult(findings=findings, used_retry=True)
    except FindingParseError:
        fallback_text = retry_response.strip() or raw_response.strip()
        if not fallback_text:
            fallback_text = "(Model returned an empty unparseable response.)"
        return ChunkReviewResult(
            findings=[],
            fallback_note=FallbackNote(file=chunk.file_path, text=fallback_text),
            used_retry=True,
        )


def review_chunks(
    backend: LLMBackend,
    chunks: Sequence[DiffChunk],
    *,
    rules: Sequence[str] = (),
    on_chunk_start: Callable[[int, int, DiffChunk], None] | None = None,
) -> tuple[list[Finding], list[FallbackNote]]:
    """Review all diff chunks sequentially and aggregate findings and fallback notes."""
    all_findings: list[Finding] = []
    fallback_notes: list[FallbackNote] = []
    total = len(chunks)

    for index, chunk in enumerate(chunks, start=1):
        if on_chunk_start is not None:
            on_chunk_start(index, total, chunk)
        result = review_chunk(backend, chunk, rules=rules)
        all_findings.extend(result.findings)
        if result.fallback_note is not None:
            fallback_notes.append(result.fallback_note)

    return all_findings, fallback_notes
