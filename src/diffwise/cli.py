"""Typer CLI entrypoint for diffwise."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from diffwise import __version__
from diffwise.config import ConfigError, load_config
from diffwise.git_diff import GitDiffError, chunk_diffs, get_staged_diff
from diffwise.report import print_diff_summary, print_empty_diff_message

app = typer.Typer(
    name="diffwise",
    help="Local, private, zero-cost AI code review for staged Git diffs.",
    no_args_is_help=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"diffwise {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Path to the git repository (defaults to current directory)."),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the diffwise version and exit.",
        ),
    ] = False,
) -> None:
    """Review staged Git changes using a local LLM."""
    if ctx.invoked_subcommand is not None:
        return

    console = Console()
    start_dir = repo or Path.cwd()

    try:
        cfg = load_config(start_dir=start_dir)
    except ConfigError as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc

    try:
        collection = get_staged_diff(
            repo_path=start_dir,
            max_file_size_bytes=cfg.max_file_size_bytes,
            ignore_globs=cfg.ignore_globs,
        )
    except GitDiffError as exc:
        console.print(f"[bold red]Git error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc

    if collection.is_empty:
        print_empty_diff_message(console, target=collection.target)
        raise typer.Exit(code=0)

    chunks = chunk_diffs(collection.files, max_chunk_chars=cfg.max_chunk_chars)
    print_diff_summary(console, collection, chunk_count=len(chunks))
    if not collection.has_reviewable_files:
        console.print("[yellow]No reviewable code hunks remain after filtering.[/yellow]")
        raise typer.Exit(code=0)


if __name__ == "__main__":
    app()

