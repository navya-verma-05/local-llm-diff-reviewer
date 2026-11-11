"""Rich terminal, Markdown, and JSON report rendering for diffwise."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from diffwise.config import BackendName, SeverityThreshold
from diffwise.git_diff import DiffCollection, FileDiff
from diffwise.prompts import SEVERITY_ORDER, FallbackNote, Finding, Severity

OutputFormat = Literal["terminal", "markdown", "json"]

_SEVERITY_STYLES: dict[Severity, tuple[str, str]] = {
    "security": ("SECURITY", "bold white on red"),
    "bug": ("BUG", "bold black on bright_red"),
    "style": ("STYLE", "bold black on yellow"),
    "suggestion": ("SUGGESTION", "bold white on blue"),
}

_FENCE_STRIP_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\s*\n?(.*?)\n?```\s*$", re.DOTALL)


@dataclass(slots=True)
class ReviewReport:
    """Complete review result ready for rendering and exit-code evaluation."""

    collection: DiffCollection
    backend: BackendName
    model: str
    chunk_count: int
    findings: list[Finding] = field(default_factory=list)
    fallback_notes: list[FallbackNote] = field(default_factory=list)
    fail_on: SeverityThreshold = "none"

    @property
    def blocked(self) -> bool:
        """Return True when findings meet or exceed the configured `fail_on` threshold."""
        return should_fail(self.findings, self.fail_on)

    def severity_counts(self) -> dict[Severity, int]:
        """Return finding counts keyed by each severity level."""
        counts: dict[Severity, int] = {
            "security": 0,
            "bug": 0,
            "style": 0,
            "suggestion": 0,
        }
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts


def should_fail(findings: Sequence[Finding], fail_on: SeverityThreshold) -> bool:
    """Determine whether findings should trigger a non-zero exit code."""
    if fail_on == "none":
        return False
    if fail_on == "security":
        return any(f.severity == "security" for f in findings)
    return any(f.severity in ("bug", "security") for f in findings)


def normalize_findings(
    findings: Sequence[Finding],
    collection: DiffCollection,
) -> list[Finding]:
    """Normalize finding paths to match `collection.files` and sort by severity and line."""
    known_paths = {f.path for f in collection.files}
    normalized: list[Finding] = []
    for finding in findings:
        path = finding.file.strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if path not in known_paths and (path.startswith("a/") or path.startswith("b/")):
            candidate = path[2:]
            if candidate in known_paths:
                path = candidate
        normalized.append(
            Finding(
                file=path,
                line=finding.line,
                severity=finding.severity,
                title=finding.title,
                explanation=finding.explanation,
                suggested_fix=finding.suggested_fix,
            )
        )

    normalized.sort(
        key=lambda f: (
            SEVERITY_ORDER[f.severity],
            f.line if f.line is not None else 10**9,
            f.title.lower(),
        )
    )
    return normalized


def _extract_snippet(
    file_diff: FileDiff | None,
    line: int | None,
    context_lines: int = 2,
) -> tuple[str, int, set[int]] | None:
    if file_diff is None or line is None or line <= 0:
        return None

    numbered_lines: dict[int, str] = {}
    for hunk in file_diff.hunks:
        for diff_line in hunk.lines:
            if diff_line.new_lineno is not None:
                numbered_lines[diff_line.new_lineno] = diff_line.content

    if not numbered_lines or line not in numbered_lines:
        return None

    window = [
        (ln, numbered_lines[ln])
        for ln in sorted(numbered_lines)
        if line - context_lines <= ln <= line + context_lines
    ]
    if not window:
        return None

    code = "\n".join(text for _, text in window)
    start_line = window[0][0]
    return code, start_line, {line}


def _strip_code_fence(text: str) -> str:
    match = _FENCE_STRIP_RE.match(text.strip())
    if match:
        return match.group(1).strip()
    return text.strip()


def _looks_like_code(text: str) -> bool:
    stripped = text.strip()
    if "\n" in stripped:
        return True
    code_tokens = (
        "def ",
        "class ",
        "return ",
        "SELECT ",
        "import ",
        "const ",
        "let ",
        "func ",
        "=",
    )
    return stripped.startswith("```") or any(tok in stripped for tok in code_tokens)


def _group_findings_by_file(
    report: ReviewReport,
) -> list[tuple[str, FileDiff | None, list[Finding]]]:
    file_map = {f.path: f for f in report.collection.files}
    grouped: dict[str, list[Finding]] = defaultdict(list)
    for finding in report.findings:
        grouped[finding.file].append(finding)

    ordered_paths: list[str] = [f.path for f in report.collection.files if f.path in grouped]
    for extra_path in grouped:
        if extra_path not in file_map:
            ordered_paths.append(extra_path)

    return [(path, file_map.get(path), grouped[path]) for path in ordered_paths]


def render_terminal_report(report: ReviewReport, console: Console) -> None:
    """Render a full Rich terminal report with header panel, findings, snippets, and summary."""
    target_desc = (
        "staged changes"
        if report.collection.target == "staged"
        else f"commit {report.collection.target}"
    )
    skipped_count = len(report.collection.skipped)
    header_lines = [
        f"[bold]Target:[/bold] {target_desc}",
        f"[bold]Backend:[/bold] {report.backend}  [bold]Model:[/bold] {report.model}",
        (
            f"[bold]Scope:[/bold] {len(report.collection.files)} file(s) reviewed, "
            f"{report.chunk_count} chunk(s), {skipped_count} skipped"
        ),
    ]
    console.print(
        Panel(
            "\n".join(header_lines),
            title="[bold cyan]diffwise Code Review[/bold cyan]",
            border_style="cyan",
        )
    )

    if not report.findings and not report.fallback_notes:
        console.print(
            Panel(
                "[bold green]No issues found.[/bold green] The reviewed changes look clean.",
                border_style="green",
            )
        )
    else:
        for file_path, file_diff, file_findings in _group_findings_by_file(report):
            language = file_diff.language if file_diff is not None else "text"
            rule_title = f"[bold]{file_path}[/bold] ({language}, {len(file_findings)} finding(s))"
            console.print(Rule(rule_title))
            for finding in file_findings:
                label, badge_style = _SEVERITY_STYLES[finding.severity]
                header = Text()
                header.append(f" {label} ", style=badge_style)
                header.append(f"  {finding.title}", style="bold")
                if finding.line is not None:
                    header.append(f"  (line {finding.line})", style="dim")

                body_parts: list[RenderableType] = [
                    header,
                    Text(finding.explanation),
                ]

                snippet_info = _extract_snippet(file_diff, finding.line)
                if snippet_info is not None:
                    snippet_code, start_line, highlight_lines = snippet_info
                    body_parts.append(
                        Syntax(
                            snippet_code,
                            lexer=language,
                            line_numbers=True,
                            start_line=start_line,
                            highlight_lines=highlight_lines,
                            theme="monokai",
                            word_wrap=True,
                        )
                    )

                if finding.suggested_fix:
                    clean_fix = _strip_code_fence(finding.suggested_fix)
                    body_parts.append(Text("Suggested fix:", style="bold green"))
                    if _looks_like_code(finding.suggested_fix):
                        body_parts.append(
                            Syntax(
                                clean_fix,
                                lexer=language,
                                line_numbers=False,
                                theme="monokai",
                                word_wrap=True,
                            )
                        )
                    else:
                        body_parts.append(Text(clean_fix, style="green"))

                border = "red" if finding.severity in ("security", "bug") else "yellow"
                console.print(Panel(Group(*body_parts), border_style=border))

        for note in report.fallback_notes:
            console.print(
                Panel(
                    Text(note.text),
                    title=f"[yellow]Plain-Text Fallback: {note.file}[/yellow]",
                    border_style="yellow",
                )
            )

    summary = Table(title="Review Summary", show_header=True, header_style="bold")
    summary.add_column("Severity", style="bold")
    summary.add_column("Count", justify="right")

    counts = report.severity_counts()
    summary.add_row("[red]security[/red]", str(counts["security"]))
    summary.add_row("[bright_red]bug[/bright_red]", str(counts["bug"]))
    summary.add_row("[yellow]style[/yellow]", str(counts["style"]))
    summary.add_row("[blue]suggestion[/blue]", str(counts["suggestion"]))
    summary.add_row("[bold]total[/bold]", f"[bold]{len(report.findings)}[/bold]")

    console.print(summary)

    if report.blocked:
        console.print(
            f"[bold red]Commit blocked:[/bold red] findings matched `--fail-on {report.fail_on}`."
        )
    else:
        console.print("[bold green]Review complete.[/bold green]")


def format_markdown_report(report: ReviewReport) -> str:
    """Format the review report as GitHub-flavored Markdown."""
    counts = report.severity_counts()
    target_desc = (
        "staged changes"
        if report.collection.target == "staged"
        else f"commit `{report.collection.target}`"
    )
    lines: list[str] = [
        "# diffwise Code Review",
        "",
        f"- **Target:** {target_desc}",
        f"- **Backend:** `{report.backend}`",
        f"- **Model:** `{report.model}`",
        f"- **Files reviewed:** {len(report.collection.files)} "
        f"({len(report.collection.skipped)} skipped, {report.chunk_count} chunk(s))",
        f"- **Status:** {'BLOCKED' if report.blocked else 'PASSED'} (`--fail-on {report.fail_on}`)",
        "",
        "## Summary",
        "",
        "| Severity | Count |",
        "| --- | ---: |",
        f"| security | {counts['security']} |",
        f"| bug | {counts['bug']} |",
        f"| style | {counts['style']} |",
        f"| suggestion | {counts['suggestion']} |",
        f"| **total** | **{len(report.findings)}** |",
        "",
    ]

    if not report.findings and not report.fallback_notes:
        lines.append("No issues found.")
        return "\n".join(lines) + "\n"

    for file_path, file_diff, file_findings in _group_findings_by_file(report):
        language = file_diff.language if file_diff is not None else "text"
        lines.append(f"## `{file_path}`")
        lines.append("")
        for finding in file_findings:
            loc = f" (line {finding.line})" if finding.line is not None else ""
            lines.append(f"### [{finding.severity.upper()}] {finding.title}{loc}")
            lines.append("")
            lines.append(finding.explanation)
            lines.append("")
            snippet_info = _extract_snippet(file_diff, finding.line)
            if snippet_info is not None:
                snippet_code, start_line, _ = snippet_info
                lines.append(f"Code (starting at line {start_line}):")
                lines.append(f"```{language}")
                lines.append(snippet_code)
                lines.append("```")
                lines.append("")
            if finding.suggested_fix:
                clean_fix = _strip_code_fence(finding.suggested_fix)
                lines.append("Suggested fix:")
                if _looks_like_code(finding.suggested_fix):
                    lines.append(f"```{language}")
                    lines.append(clean_fix)
                    lines.append("```")
                else:
                    lines.append(clean_fix)
                lines.append("")

    if report.fallback_notes:
        lines.append("## Plain-Text Fallback Notes")
        lines.append("")
        for note in report.fallback_notes:
            lines.append(f"### `{note.file}`")
            lines.append("")
            lines.append(note.text)
            lines.append("")

    return "\n".join(lines)


def format_json_report(report: ReviewReport) -> str:
    """Format the review report as structured JSON."""
    counts = report.severity_counts()
    payload: dict[str, Any] = {
        "target": report.collection.target,
        "backend": report.backend,
        "model": report.model,
        "files_reviewed": len(report.collection.files),
        "files_skipped": len(report.collection.skipped),
        "chunks_analyzed": report.chunk_count,
        "summary": {
            "security": counts["security"],
            "bug": counts["bug"],
            "style": counts["style"],
            "suggestion": counts["suggestion"],
            "total": len(report.findings),
            "blocked": report.blocked,
            "fail_on": report.fail_on,
        },
        "findings": [
            {
                "file": f.file,
                "line": f.line,
                "severity": f.severity,
                "title": f.title,
                "explanation": f.explanation,
                "suggested_fix": f.suggested_fix,
            }
            for f in report.findings
        ],
        "fallback_notes": [
            {"file": n.file, "text": n.text} for n in report.fallback_notes
        ],
        "skipped_files": [
            {"path": s.path, "reason": s.reason, "detail": s.detail}
            for s in report.collection.skipped
        ],
    }
    return json.dumps(payload, indent=2) + "\n"


def emit_report(
    report: ReviewReport,
    *,
    output_format: OutputFormat,
    output_path: Path | None,
    stdout_console: Console,
    stderr_console: Console,
) -> None:
    """Render and write or print the review report in the requested format."""
    if output_format == "json":
        rendered = format_json_report(report)
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8", newline="\n")
            stderr_console.print(f"[green]Report written to {output_path}[/green]")
        else:
            stdout_console.file.write(rendered)
            stdout_console.file.flush()
        return

    if output_format == "markdown":
        rendered = format_markdown_report(report)
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8", newline="\n")
            stderr_console.print(f"[green]Report written to {output_path}[/green]")
        else:
            stdout_console.file.write(rendered)
            stdout_console.file.flush()
        return

    if output_path is not None:
        capture_console = Console(record=True, no_color=True, width=100)
        render_terminal_report(report, capture_console)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(capture_console.export_text(), encoding="utf-8", newline="\n")
        stderr_console.print(f"[green]Report written to {output_path}[/green]")
    else:
        render_terminal_report(report, stdout_console)


def print_empty_diff_message(console: Console, target: str = "staged") -> None:
    """Print a friendly message when no staged or target changes exist."""
    if target == "staged":
        message = (
            "[bold green]Nothing to review.[/bold green]\n"
            "Your staging area is empty. Stage changes with [cyan]git add <files>[/cyan] "
            "and run [cyan]diffwise[/cyan] again."
        )
    else:
        message = f"[bold green]Nothing to review.[/bold green]\nNo changes found for '{target}'."
    console.print(Panel(message, title="diffwise", border_style="green"))
