"""Tests for report formatting, severity filtering, and Rich/Markdown/JSON output."""

from __future__ import annotations

import json
from io import StringIO

from rich.console import Console

from diffwise.git_diff import DiffCollection, DiffHunk, DiffLine, FileDiff, SkippedFile
from diffwise.prompts import FallbackNote, Finding
from diffwise.report import (
    ReviewReport,
    format_json_report,
    format_markdown_report,
    normalize_findings,
    render_terminal_report,
    should_fail,
)


def _sample_report() -> ReviewReport:
    fdiff = FileDiff(
        path="src/auth.py",
        language="python",
        status="modified",
        hunks=[
            DiffHunk(
                old_start=10,
                old_count=2,
                new_start=10,
                new_count=3,
                header="@@ -10,2 +10,3 @@ def login():",
                lines=[
                    DiffLine("context", "def login(user):", 10, 10),
                    DiffLine("add", '    token = "hardcoded_secret_123"', None, 11),
                    DiffLine("context", "    return token", 11, 12),
                ],
            )
        ],
    )
    collection = DiffCollection(
        files=[fdiff],
        skipped=[SkippedFile(path="uv.lock", reason="lockfile", detail="Lockfile")],
        target="staged",
    )
    raw_findings = [
        Finding(
            file="b/src/auth.py",
            line=11,
            severity="security",
            title="Hardcoded secret token",
            explanation="Credentials must not be committed in source code.",
            suggested_fix='token = os.environ["AUTH_TOKEN"]',
        ),
        Finding(
            file="./src/auth.py",
            line=12,
            severity="style",
            title="Missing return type annotation",
            explanation="Add `-> str` return annotation.",
            suggested_fix="",
        ),
    ]
    findings = normalize_findings(raw_findings, collection)
    return ReviewReport(
        collection=collection,
        backend="ollama",
        model="qwen2.5-coder:7b",
        chunk_count=1,
        findings=findings,
        fallback_notes=[FallbackNote(file="src/auth.py", text="Extra note")],
        fail_on="bug",
    )


def test_should_fail_thresholds() -> None:
    sec = [Finding("a.py", 1, "security", "t", "e")]
    bug = [Finding("a.py", 1, "bug", "t", "e")]
    style = [Finding("a.py", 1, "style", "t", "e")]

    assert should_fail(sec, "none") is False
    assert should_fail(sec, "security") is True
    assert should_fail(bug, "security") is False
    assert should_fail(bug, "bug") is True
    assert should_fail(sec, "bug") is True  # bug threshold includes security
    assert should_fail(style, "bug") is False


def test_render_terminal_report_and_clean_state() -> None:
    report = _sample_report()
    buf = StringIO()
    console = Console(file=buf, no_color=True, width=100)
    render_terminal_report(report, console)
    out = buf.getvalue()

    assert "diffwise Code Review" in out
    assert "src/auth.py" in out
    assert "SECURITY" in out
    assert "Hardcoded secret token" in out
    assert "hardcoded_secret_123" in out
    assert "Review Summary" in out
    assert "Commit blocked" in out

    clean_report = ReviewReport(
        collection=DiffCollection(files=[FileDiff(path="ok.py", language="python")]),
        backend="ollama",
        model="qwen2.5-coder:7b",
        chunk_count=1,
        findings=[],
        fail_on="bug",
    )
    clean_buf = StringIO()
    render_terminal_report(clean_report, Console(file=clean_buf, no_color=True, width=100))
    assert "No issues found" in clean_buf.getvalue()


def test_markdown_and_json_reports() -> None:
    report = _sample_report()

    md = format_markdown_report(report)
    assert "# diffwise Code Review" in md
    assert "| security | 1 |" in md
    assert "### [SECURITY] Hardcoded secret token (line 11)" in md
    assert "```python" in md

    parsed = json.loads(format_json_report(report))
    assert parsed["model"] == "qwen2.5-coder:7b"
    assert parsed["summary"]["security"] == 1
    assert parsed["summary"]["style"] == 1
    assert parsed["summary"]["total"] == 2
    assert parsed["summary"]["blocked"] is True
    assert parsed["findings"][0]["file"] == "src/auth.py"
    assert parsed["skipped_files"][0]["path"] == "uv.lock"

