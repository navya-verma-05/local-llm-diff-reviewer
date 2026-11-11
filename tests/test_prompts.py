"""Tests for prompt building, JSON finding parsing, retry, and plain-text fallback."""

from __future__ import annotations

import pytest

from diffwise.backends.base import LLMBackend
from diffwise.git_diff import DiffChunk, DiffHunk, DiffLine
from diffwise.prompts import (
    FindingParseError,
    build_system_prompt,
    parse_findings_json,
    review_chunk,
)


class _StubBackend(LLMBackend):
    def __init__(self, responses: list[str]) -> None:
        self.model = "stub-model"
        self.base_url = "http://localhost:11434"
        self.timeout = 30.0
        self._responses = list(responses)
        self.prompts: list[str] = []

    def health_check(self) -> None:
        return None

    def review(self, prompt: str, *, system_prompt: str | None = None) -> str:
        self.prompts.append(prompt)
        return self._responses.pop(0)


def _sample_chunk() -> DiffChunk:
    hunk = DiffHunk(
        old_start=1,
        old_count=1,
        new_start=1,
        new_count=2,
        header="@@ -1,1 +1,2 @@",
        lines=[
            DiffLine("context", "import sqlite3", 1, 1),
            DiffLine("add", 'cur.execute(f"SELECT * FROM u WHERE id={uid}")', None, 2),
        ],
    )
    return DiffChunk(
        file_path="db.py",
        language="python",
        status="modified",
        old_path="db.py",
        hunks=[hunk],
    )


def test_build_system_prompt_includes_schema_and_rules() -> None:
    prompt = build_system_prompt(rules=["Never log PII."])
    assert "severity" in prompt
    assert "suggested_fix" in prompt
    assert "Never log PII." in prompt


def test_parse_findings_json_handles_fences_and_nested_backticks() -> None:
    raw = (
        "Here is my review:\n"
        "```json\n"
        "[\n"
        "  {\n"
        '    "file": "db.py",\n'
        '    "line": "L2",\n'
        '    "severity": "vulnerability",\n'
        '    "title": "SQL injection",\n'
        '    "explanation": "String interpolation in SQL query.",\n'
        '    "suggested_fix": "```python\\n'
        'cur.execute(\\"SELECT * FROM u WHERE id=?\\", (uid,))\\n```"\n'
        "  }\n"
        "]\n"
        "```\n"
        "Hope that helps!"
    )
    findings = parse_findings_json(raw, default_file="fallback.py")
    assert len(findings) == 1
    assert findings[0].file == "db.py"
    assert findings[0].line == 2
    assert findings[0].severity == "security"
    assert findings[0].title == "SQL injection"
    assert "cur.execute" in findings[0].suggested_fix


def test_parse_findings_json_accepts_wrapper_and_empty_array() -> None:
    assert parse_findings_json("[]") == []

    wrapped = (
        '{"findings": [{"severity": "bug", "title": "Off by one", '
        '"explanation": "Loop ends early"}]}'
    )
    findings = parse_findings_json(wrapped, default_file="calc.py")
    assert len(findings) == 1
    assert findings[0].file == "calc.py"
    assert findings[0].severity == "bug"

    with pytest.raises(FindingParseError):
        parse_findings_json("Looks good to me!")


def test_review_chunk_retries_once_and_recovers() -> None:
    valid_json = (
        '[{"file": "db.py", "line": 2, "severity": "security", '
        '"title": "SQLi", "explanation": "Interpolated SQL", "suggested_fix": "Use params"}]'
    )
    backend = _StubBackend(["I noticed a SQL injection on line 2.", valid_json])
    result = review_chunk(backend, _sample_chunk())

    assert result.used_retry is True
    assert result.fallback_note is None
    assert len(result.findings) == 1
    assert result.findings[0].severity == "security"
    assert len(backend.prompts) == 2


def test_review_chunk_falls_back_to_plain_text_after_retry_failure() -> None:
    backend = _StubBackend(
        [
            "Not JSON response 1",
            "Line 2 executes an unparameterized SQL query.",
        ]
    )
    result = review_chunk(backend, _sample_chunk())

    assert result.used_retry is True
    assert result.findings == []
    assert result.fallback_note is not None
    assert result.fallback_note.file == "db.py"
    assert "unparameterized SQL" in result.fallback_note.text
    assert len(backend.prompts) == 2


def test_parse_findings_json_validation_and_review_chunks_callback() -> None:
    from diffwise.prompts import review_chunks

    single = '{"title": "Null check", "severity": "warning", "explanation": "May be None"}'
    parsed = parse_findings_json(single, default_file="svc.py")
    assert len(parsed) == 1
    assert parsed[0].severity == "bug"

    with pytest.raises(FindingParseError):
        parse_findings_json('[{"severity": "bug", "title": ""}]')
    with pytest.raises(FindingParseError):
        parse_findings_json('[{"severity": "bogus", "title": "t", "explanation": "e"}]')
    with pytest.raises(FindingParseError):
        parse_findings_json("   ")

    seen_progress: list[tuple[int, int, str]] = []
    backend = _StubBackend(["[]"])
    findings, notes = review_chunks(
        backend,
        [_sample_chunk()],
        on_chunk_start=lambda idx, total, ch: seen_progress.append((idx, total, ch.file_path)),
    )
    assert findings == []
    assert notes == []
    assert seen_progress == [(1, 1, "db.py")]

