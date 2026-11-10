"""Phase 2 CLI behavior tests (empty staging area, version, repo validation)."""

from __future__ import annotations

from pathlib import Path

from git import Repo
from typer.testing import CliRunner

from diffwise import __version__
from diffwise.cli import app

runner = CliRunner()


def test_cli_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"diffwise {__version__}" in result.stdout


def test_cli_empty_staging_area_exits_zero(tmp_path: Path) -> None:
    repo = Repo.init(tmp_path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Tester")
        cw.set_value("user", "email", "tester@example.com")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("init")

    result = runner.invoke(app, ["--repo", str(tmp_path)])
    assert result.exit_code == 0
    assert "Nothing to review" in result.stdout
    assert "staging area is empty" in result.stdout


def test_cli_non_git_directory_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--repo", str(tmp_path)])
    assert result.exit_code == 2
    assert "Not a git repository" in result.stdout


def test_cli_staged_diff_summary(tmp_path: Path) -> None:
    Repo.init(tmp_path)
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    repo = Repo(tmp_path)
    repo.index.add(["app.py", "uv.lock"])

    result = runner.invoke(app, ["--repo", str(tmp_path)])
    assert result.exit_code == 0
    assert "app.py" in result.stdout
    assert "uv.lock" in result.stdout
    assert "skipped" in result.stdout

