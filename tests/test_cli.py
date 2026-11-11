"""End-to-end CLI tests covering flags, config overrides, init, formats, and exit codes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from git import Repo
from typer.testing import CliRunner

from diffwise import __version__
from diffwise.backends.base import BackendError, LLMBackend
from diffwise.cli import app
from diffwise.config import DiffwiseConfig, load_config

runner = CliRunner()


class _FakeCliBackend(LLMBackend):
    def __init__(
        self,
        response_text: str = "[]",
        *,
        fail_health: bool = False,
        config_capture: list[DiffwiseConfig] | None = None,
        cfg: DiffwiseConfig | None = None,
    ) -> None:
        self.model = cfg.model if cfg else "fake"
        self.base_url = "http://localhost:11434"
        self.timeout = 10.0
        self._response_text = response_text
        self._fail_health = fail_health
        if config_capture is not None and cfg is not None:
            config_capture.append(cfg)

    def health_check(self) -> None:
        if self._fail_health:
            raise BackendError("Server is offline")

    def review(self, prompt: str, *, system_prompt: str | None = None) -> str:
        return self._response_text


def _init_repo(path: Path) -> Repo:
    repo = Repo.init(path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Tester")
        cw.set_value("user", "email", "tester@example.com")
        cw.set_value("core", "autocrlf", "false")
    return repo


def test_cli_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"diffwise {__version__}" in result.stdout


def test_cli_empty_staging_area_exits_zero(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("init")

    result = runner.invoke(app, ["--repo", str(tmp_path)])
    assert result.exit_code == 0
    assert "Nothing to review" in result.stdout


def test_cli_non_git_directory_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--repo", str(tmp_path)])
    assert result.exit_code == 2
    assert "Not a git repository" in result.output


def test_cli_mutually_exclusive_staged_and_commit(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--repo", str(tmp_path), "--staged", "--commit", "HEAD"])
    assert result.exit_code == 2
    assert "Cannot combine" in result.output


def test_cli_init_writes_valid_config(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--path", str(tmp_path)])
    assert result.exit_code == 0
    cfg_file = tmp_path / ".diffwise.toml"
    assert cfg_file.is_file()

    cfg = load_config(start_dir=tmp_path)
    assert cfg.model == "qwen2.5-coder:7b"
    assert cfg.backend == "ollama"
    assert cfg.fail_on == "bug"
    assert len(cfg.rules) >= 1

    # Refuses overwrite without --force
    second = runner.invoke(app, ["init", "--path", str(tmp_path)])
    assert second.exit_code == 1

    forced = runner.invoke(app, ["init", "--path", str(tmp_path), "--force"])
    assert forced.exit_code == 0


def test_cli_flags_override_config_and_fail_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    (tmp_path / ".diffwise.toml").write_text(
        'model = "config-model"\nbackend = "ollama"\nfail_on = "none"\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("x = 1 / 0\n", encoding="utf-8")
    repo.index.add(["app.py"])

    seen_configs: list[DiffwiseConfig] = []
    finding_json = json.dumps(
        [
            {
                "file": "app.py",
                "line": 1,
                "severity": "bug",
                "title": "ZeroDivisionError",
                "explanation": "Dividing literal 1 by 0 always raises ZeroDivisionError.",
                "suggested_fix": "x = 0",
            }
        ]
    )

    def fake_factory(cfg: DiffwiseConfig) -> LLMBackend:
        return _FakeCliBackend(finding_json, config_capture=seen_configs, cfg=cfg)

    monkeypatch.setattr("diffwise.cli.create_backend", fake_factory)

    out_file = tmp_path / "review.json"
    result = runner.invoke(
        app,
        [
            "--repo",
            str(tmp_path),
            "--model",
            "override-model",
            "--backend",
            "llamacpp",
            "--format",
            "json",
            "--output",
            str(out_file),
            "--fail-on",
            "bug",
        ],
    )
    assert result.exit_code == 1
    assert len(seen_configs) == 1
    assert seen_configs[0].model == "override-model"
    assert seen_configs[0].backend == "llamacpp"
    assert seen_configs[0].fail_on == "bug"

    report_data = json.loads(out_file.read_text(encoding="utf-8"))
    assert report_data["model"] == "override-model"
    assert report_data["backend"] == "llamacpp"
    assert report_data["summary"]["blocked"] is True
    assert report_data["summary"]["bug"] == 1


def test_cli_json_stdout_clean_and_commit_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    (tmp_path / "main.py").write_text("def f():\n    return 42\n", encoding="utf-8")
    repo.index.add(["main.py"])
    repo.index.commit("Add main.py")

    def fake_factory(cfg: DiffwiseConfig) -> LLMBackend:
        return _FakeCliBackend("[]", cfg=cfg)

    monkeypatch.setattr("diffwise.cli.create_backend", fake_factory)

    result = runner.invoke(
        app,
        ["--repo", str(tmp_path), "--commit", "HEAD", "--format", "json"],
    )
    assert result.exit_code == 0
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["target"] == "HEAD"
    assert payload["files_reviewed"] == 1
    assert payload["summary"]["total"] == 0


def test_cli_backend_error_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
    repo.index.add(["a.py"])

    def failing_factory(cfg: DiffwiseConfig) -> LLMBackend:
        return _FakeCliBackend(fail_health=True, cfg=cfg)

    monkeypatch.setattr("diffwise.cli.create_backend", failing_factory)

    result = runner.invoke(app, ["--repo", str(tmp_path)])
    assert result.exit_code == 2
    assert "Server is offline" in result.output


def test_pre_commit_hooks_yaml_present() -> None:
    hook_file = Path(__file__).resolve().parent.parent / ".pre-commit-hooks.yaml"
    content = hook_file.read_text(encoding="utf-8")
    assert "id: diffwise" in content
    assert "entry: diffwise" in content
    assert "pass_filenames: false" in content

