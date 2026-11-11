"""Typer CLI entrypoint for diffwise."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from diffwise import __version__
from diffwise.backends import BackendError, create_backend
from diffwise.config import ConfigError, load_config, write_starter_config
from diffwise.git_diff import (
    DiffChunk,
    DiffCollection,
    GitDiffError,
    chunk_diffs,
    get_commit_diff,
    get_staged_diff,
)
from diffwise.prompts import review_chunks
from diffwise.report import (
    OutputFormat,
    ReviewReport,
    emit_report,
    normalize_findings,
    print_empty_diff_message,
)

app = typer.Typer(
    name="diffwise",
    help="Local, private, zero-cost AI code review for staged Git diffs.",
    no_args_is_help=False,
)


class BackendOption(str, Enum):
    """Supported local LLM backend choices."""

    ollama = "ollama"
    llamacpp = "llamacpp"


class FormatOption(str, Enum):
    """Supported output report formats."""

    terminal = "terminal"
    markdown = "markdown"
    json = "json"


class FailOnOption(str, Enum):
    """Severity threshold choices for blocking commits."""

    bug = "bug"
    security = "security"
    none = "none"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"diffwise {__version__}")
        raise typer.Exit()


@app.command("init")
def init_command(
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Directory where .diffwise.toml will be created."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing .diffwise.toml file."),
    ] = False,
) -> None:
    """Create a starter `.diffwise.toml` configuration file."""
    console = Console()
    try:
        dest = write_starter_config(target_dir=path, force=force)
    except ConfigError as exc:
        console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[bold green]Created[/bold green] starter config at [cyan]{escape(str(dest))}[/cyan]"
    )


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Local LLM model name (overrides config)."),
    ] = None,
    backend: Annotated[
        BackendOption | None,
        typer.Option("--backend", "-b", help="LLM backend to use (overrides config)."),
    ] = None,
    output_format: Annotated[
        FormatOption,
        typer.Option("--format", "-f", help="Report output format."),
    ] = FormatOption.terminal,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the report to FILE instead of stdout."),
    ] = None,
    fail_on: Annotated[
        FailOnOption | None,
        typer.Option(
            "--fail-on",
            help="Exit non-zero if findings match threshold (bug includes security).",
        ),
    ] = None,
    staged: Annotated[
        bool,
        typer.Option("--staged", help="Review staged git changes (default)."),
    ] = False,
    commit: Annotated[
        str | None,
        typer.Option("--commit", "-c", help="Review a commit or range REF (e.g. HEAD, a..b)."),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Path to the git repository (defaults to current directory)."),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Path to a .diffwise.toml configuration file."),
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
    """Review staged Git changes (or a commit REF) using a local LLM."""
    if ctx.invoked_subcommand is not None:
        return

    stdout_console = Console()
    stderr_console = Console(stderr=True)

    if staged and commit is not None:
        stderr_console.print(
            "[bold red]Usage error:[/bold red] Cannot combine `--staged` and `--commit`."
        )
        raise typer.Exit(code=2)

    start_dir = repo or Path.cwd()

    try:
        cfg = load_config(start_dir=start_dir, config_path=config_path)
    except ConfigError as exc:
        stderr_console.print(f"[bold red]Configuration error:[/bold red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    # CLI flags override .diffwise.toml values
    if model is not None:
        cleaned_model = model.strip()
        if not cleaned_model:
            stderr_console.print(
                "[bold red]Usage error:[/bold red] `--model` must be a non-empty model name."
            )
            raise typer.Exit(code=2)
        cfg.model = cleaned_model
    if backend is not None:
        cfg.backend = backend.value
    if fail_on is not None:
        cfg.fail_on = fail_on.value

    try:
        collection: DiffCollection
        if commit is not None:
            collection = get_commit_diff(
                commit,
                repo_path=start_dir,
                max_file_size_bytes=cfg.max_file_size_bytes,
                ignore_globs=cfg.ignore_globs,
            )
        else:
            collection = get_staged_diff(
                repo_path=start_dir,
                max_file_size_bytes=cfg.max_file_size_bytes,
                ignore_globs=cfg.ignore_globs,
            )
    except GitDiffError as exc:
        stderr_console.print(f"[bold red]Git error:[/bold red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    fmt: OutputFormat = output_format.value

    if collection.is_empty:
        if fmt == "json":
            empty_report = ReviewReport(
                collection=collection,
                backend=cfg.backend,
                model=cfg.model,
                chunk_count=0,
                fail_on=cfg.fail_on,
            )
            emit_report(
                empty_report,
                output_format=fmt,
                output_path=output,
                stdout_console=stdout_console,
                stderr_console=stderr_console,
            )
        else:
            print_empty_diff_message(stdout_console, target=collection.target)
        raise typer.Exit(code=0)

    chunks = chunk_diffs(collection.files, max_chunk_chars=cfg.max_chunk_chars)
    if not chunks:
        empty_filtered = ReviewReport(
            collection=collection,
            backend=cfg.backend,
            model=cfg.model,
            chunk_count=0,
            fail_on=cfg.fail_on,
        )
        emit_report(
            empty_filtered,
            output_format=fmt,
            output_path=output,
            stdout_console=stdout_console,
            stderr_console=stderr_console,
        )
        raise typer.Exit(code=0)

    llm = create_backend(cfg)
    try:
        with stderr_console.status(
            f"[cyan]Checking {escape(cfg.backend)} backend ({escape(cfg.model)})...[/cyan]",
            spinner="dots",
        ) as status:
            llm.health_check()

            def _update_spinner(index: int, total: int, chunk: DiffChunk) -> None:
                status.update(
                    f"[cyan]Reviewing {escape(chunk.file_path)} ({index}/{total}) "
                    f"with {escape(cfg.model)}...[/cyan]"
                )

            raw_findings, fallback_notes = review_chunks(
                llm,
                chunks,
                rules=cfg.rules,
                on_chunk_start=_update_spinner,
            )
    except BackendError as exc:
        stderr_console.print(f"[bold red]Backend error:[/bold red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    findings = normalize_findings(raw_findings, collection)
    report = ReviewReport(
        collection=collection,
        backend=cfg.backend,
        model=cfg.model,
        chunk_count=len(chunks),
        findings=findings,
        fallback_notes=fallback_notes,
        fail_on=cfg.fail_on,
    )
    emit_report(
        report,
        output_format=fmt,
        output_path=output,
        stdout_console=stdout_console,
        stderr_console=stderr_console,
    )

    if report.blocked:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
