"""Rich terminal, Markdown, and JSON report formatting (completed in Phase 4)."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from diffwise.git_diff import DiffCollection


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


def print_diff_summary(console: Console, collection: DiffCollection, chunk_count: int) -> None:
    """Print a concise summary of parsed and skipped files for Phase 2 inspection."""
    console.print(
        f"[bold cyan]diffwise[/bold cyan] parsed {len(collection.files)} file(s) "
        f"into {chunk_count} chunk(s) (skipped {len(collection.skipped)} file(s))."
    )
    for file_diff in collection.files:
        adds = len(file_diff.added_lines)
        dels = len(file_diff.removed_lines)
        console.print(
            f"  [green]{file_diff.status:8s}[/green] {file_diff.path} "
            f"[{file_diff.language}] (+{adds} / -{dels})"
        )
    for skipped in collection.skipped:
        console.print(
            f"  [yellow]skipped [/yellow] {skipped.path} ({skipped.reason}: {skipped.detail})"
        )

