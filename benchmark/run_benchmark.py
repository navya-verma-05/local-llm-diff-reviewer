"""Run diffwise against the benchmark diff suite and generate benchmark/RESULTS.md."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BENCHMARK_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = BENCHMARK_DIR / "manifest.json"
DEFAULT_OUTPUT = BENCHMARK_DIR / "RESULTS.md"


@dataclass
class CaseResult:
    """Outcome for a single benchmark diff evaluation."""

    case_id: str
    category: str
    target_file: str
    expected_severity: str
    caught: bool
    matched_severity: str
    false_positives: int
    total_findings: int
    fallback_used: bool
    elapsed_seconds: float
    finding_titles: list[str]
    error: str = ""


def _init_temp_repo(tmp_dir: Path, case: dict[str, Any]) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Benchmark"], cwd=tmp_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "bench@example.com"], cwd=tmp_dir, check=True
    )
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=tmp_dir, check=True)

    base_rel = case.get("base_dir")
    if base_rel:
        src_base = BENCHMARK_DIR / base_rel
        for item in src_base.rglob("*"):
            if item.is_file():
                rel = item.relative_to(src_base)
                dest = tmp_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
        subprocess.run(["git", "add", "-A"], cwd=tmp_dir, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "Base commit"], cwd=tmp_dir, check=True
        )

    diff_path = BENCHMARK_DIR / case["diff_file"]
    subprocess.run(
        ["git", "apply", "--index", str(diff_path)],
        cwd=tmp_dir,
        check=True,
    )


def _finding_matches_case(finding: dict[str, Any], case: dict[str, Any]) -> bool:
    file_path = str(finding.get("file", "")).replace("\\", "/").lstrip("./")
    if file_path != case["target_file"]:
        return False

    line = finding.get("line")
    line_min, line_max = case["line_range"]
    if isinstance(line, int) and not (line_min - 2 <= line <= line_max + 2):
        return False

    haystack = " ".join(
        [
            str(finding.get("title", "")),
            str(finding.get("explanation", "")),
            str(finding.get("suggested_fix", "")),
        ]
    ).lower()

    return all(
        any(kw.lower() in haystack for kw in group)
        for group in case["keyword_groups"]
    )


def evaluate_case(
    case: dict[str, Any],
    *,
    model: str,
    backend: str,
) -> CaseResult:
    """Stage one benchmark diff in a temp repo, run diffwise, and score the output."""
    with tempfile.TemporaryDirectory(prefix=f"dw_bench_{case['id']}_") as tmp_name:
        tmp_dir = Path(tmp_name)
        _init_temp_repo(tmp_dir, case)

        cmd = [
            sys.executable,
            "-m",
            "diffwise.cli",
            "--repo",
            str(tmp_dir),
            "--model",
            model,
            "--backend",
            backend,
            "--format",
            "json",
            "--fail-on",
            "none",
        ]
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        elapsed = time.perf_counter() - t0

        if proc.returncode != 0:
            err_msg = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
            return CaseResult(
                case_id=case["id"],
                category=case["category"],
                target_file=case["target_file"],
                expected_severity=case["expected_severity"],
                caught=False,
                matched_severity="-",
                false_positives=0,
                total_findings=0,
                fallback_used=False,
                elapsed_seconds=elapsed,
                finding_titles=[],
                error=err_msg.splitlines()[-1][:120],
            )

        report = json.loads(proc.stdout)
        findings: list[dict[str, Any]] = report.get("findings", [])
        fallback_notes: list[dict[str, Any]] = report.get("fallback_notes", [])

        matched: list[dict[str, Any]] = []
        non_matching: list[dict[str, Any]] = []
        for f in findings:
            if _finding_matches_case(f, case):
                matched.append(f)
            else:
                non_matching.append(f)

        caught = len(matched) > 0
        matched_sev = str(matched[0].get("severity", "-")) if matched else "-"
        titles = [f"[{f.get('severity')}] L{f.get('line')}: {f.get('title')}" for f in findings]

        return CaseResult(
            case_id=case["id"],
            category=case["category"],
            target_file=case["target_file"],
            expected_severity=case["expected_severity"],
            caught=caught,
            matched_severity=matched_sev,
            false_positives=len(non_matching),
            total_findings=len(findings),
            fallback_used=len(fallback_notes) > 0,
            elapsed_seconds=elapsed,
            finding_titles=titles,
        )


def render_results_markdown(
    results: list[CaseResult],
    *,
    model: str,
    backend: str,
) -> str:
    """Render benchmark metrics and per-case results as Markdown."""
    total = len(results)
    caught_count = sum(1 for r in results if r.caught)
    missed_count = total - caught_count
    total_fp = sum(r.false_positives for r in results)
    total_time = sum(r.elapsed_seconds for r in results)
    catch_rate = (caught_count / total * 100.0) if total else 0.0
    exact_sev_count = sum(
        1 for r in results if r.caught and r.matched_severity == r.expected_severity
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# diffwise Benchmark Results",
        "",
        f"- **Date:** {timestamp}",
        f"- **Backend:** `{backend}`",
        f"- **Model:** `{model}`",
        f"- **Cases:** {total}",
        f"- **Caught:** {caught_count}/{total} ({catch_rate:.1f}%)",
        f"- **Exact Severity Match:** {exact_sev_count}/{total}",
        f"- **Missed:** {missed_count}/{total}",
        f"- **False Positives (non-seeded findings):** {total_fp}",
        f"- **Total Runtime:** {total_time:.1f}s (avg {total_time / total:.1f}s/diff)",
        "",
        "## Per-Case Results",
        "",
        (
            "| ID | Seeded Issue | File | Expected | Result | Reported Severity | "
            "False Positives | Latency | Findings |"
        ),
        "| --- | --- | --- | --- | --- | --- | ---: | ---: | --- |",
    ]

    for r in results:
        if r.error:
            status = f"Error (`{r.error}`)"
        elif r.caught:
            status = "Caught"
        elif r.fallback_used:
            status = "Missed (JSON fallback)"
        else:
            status = "Missed"

        summary_titles = "; ".join(r.finding_titles) if r.finding_titles else "None"
        summary_titles = summary_titles.replace("|", "\\|")
        lines.append(
            f"| `{r.case_id}` | {r.category} | `{r.target_file}` | {r.expected_severity} | "
            f"{status} | {r.matched_severity} | {r.false_positives} | "
            f"{r.elapsed_seconds:.1f}s | {summary_titles} |"
        )

    lines.extend(
        [
            "",
            "## Scoring Methodology",
            "",
            (
                "- Each benchmark case applies a real unified `.diff` to an isolated temporary "
                "Git repository and runs `diffwise --format json` end-to-end."
            ),
            (
                "- **Caught:** At least one structured JSON finding targets the seeded file within "
                "+/- 2 lines of the seeded defect (or file-level) and matches the case's semantic "
                "keyword groups."
            ),
            (
                "- **False Positives:** Any structured finding emitted on the diff that does not "
                "match the seeded issue."
            ),
            (
                "- Plain-text JSON parse fallbacks are scored conservatively as **Missed "
                "(JSON fallback)** rather than guessed as caught."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run diffwise benchmark suite.")
    parser.add_argument("--model", default="qwen2.5-coder:7b", help="Model name to evaluate.")
    parser.add_argument("--backend", default="ollama", choices=["ollama", "llamacpp"])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    cases: list[dict[str, Any]] = json.loads(args.manifest.read_text(encoding="utf-8"))
    results: list[CaseResult] = []

    print(f"Running {len(cases)} benchmark diffs against {args.backend}/{args.model}...")
    for idx, case in enumerate(cases, start=1):
        res = evaluate_case(case, model=args.model, backend=args.backend)
        results.append(res)
        label = "CAUGHT" if res.caught else ("ERROR" if res.error else "MISSED")
        print(
            f"[{idx:02d}/{len(cases)}] {res.case_id:<24} {label:<6} "
            f"fp={res.false_positives} time={res.elapsed_seconds:.1f}s"
        )

    md = render_results_markdown(results, model=args.model, backend=args.backend)
    args.output.write_text(md, encoding="utf-8", newline="\n")
    caught = sum(1 for r in results if r.caught)
    fp = sum(r.false_positives for r in results)
    print(
        f"\nSaved results to {args.output}: "
        f"{caught}/{len(results)} caught, {fp} false positives."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
