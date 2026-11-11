"""Configuration loading and validation for diffwise."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

BackendName = Literal["ollama", "llamacpp"]
SeverityThreshold = Literal["bug", "security", "none"]

DEFAULT_CONFIG_FILENAME = ".diffwise.toml"
DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_BACKEND: BackendName = "ollama"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_LLAMACPP_URL = "http://localhost:8080"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_FILE_SIZE_BYTES = 100_000
DEFAULT_MAX_CHUNK_CHARS = 12_000
DEFAULT_FAIL_ON: SeverityThreshold = "none"


class ConfigError(ValueError):
    """Raised when a diffwise configuration file cannot be parsed or is invalid."""


@dataclass(slots=True)
class DiffwiseConfig:
    """Runtime configuration for diffwise."""

    model: str = DEFAULT_MODEL
    backend: BackendName = DEFAULT_BACKEND
    ollama_url: str = DEFAULT_OLLAMA_URL
    llamacpp_url: str = DEFAULT_LLAMACPP_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS
    fail_on: SeverityThreshold = DEFAULT_FAIL_ON
    ignore_globs: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)


def _validate_backend(value: Any) -> BackendName:
    if value == "ollama":
        return "ollama"
    if value == "llamacpp":
        return "llamacpp"
    raise ConfigError(f"Invalid backend '{value}'. Expected 'ollama' or 'llamacpp'.")


def _validate_fail_on(value: Any) -> SeverityThreshold:
    if value == "bug":
        return "bug"
    if value == "security":
        return "security"
    if value == "none":
        return "none"
    raise ConfigError(f"Invalid fail_on '{value}'. Expected 'bug', 'security', or 'none'.")


def _validate_positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"Configuration '{name}' must be a positive integer, got {value!r}.")
    return value


def _validate_positive_float(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"Configuration '{name}' must be a positive number, got {value!r}.")
    return float(value)


def _validate_str_list(name: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"Configuration '{name}' must be a list of strings.")
    return list(value)


def _validate_non_empty_str(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Configuration '{name}' must be a non-empty string.")
    return value.strip()


def find_config_file(start_dir: Path | None = None) -> Path | None:
    """Locate `.diffwise.toml` starting at `start_dir` and walking up parent directories."""
    current = (start_dir or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / DEFAULT_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_config(
    start_dir: Path | None = None,
    config_path: Path | None = None,
) -> DiffwiseConfig:
    """Load configuration from `.diffwise.toml` or return defaults if absent."""
    target = config_path if config_path is not None else find_config_file(start_dir)
    if target is None:
        return DiffwiseConfig()

    if not target.is_file():
        raise ConfigError(f"Configuration file not found: {target}")

    try:
        raw_bytes = target.read_bytes()
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Failed to parse {target}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Invalid TOML structure in {target}.")

    # Allow settings either at top level or under [diffwise]
    maybe_section = data.get("diffwise")
    section: dict[str, Any] = maybe_section if isinstance(maybe_section, dict) else data

    cfg = DiffwiseConfig()
    if "model" in section:
        cfg.model = _validate_non_empty_str("model", section["model"])
    if "backend" in section:
        cfg.backend = _validate_backend(section["backend"])
    if "ollama_url" in section:
        cfg.ollama_url = _validate_non_empty_str("ollama_url", section["ollama_url"])
    if "llamacpp_url" in section:
        cfg.llamacpp_url = _validate_non_empty_str("llamacpp_url", section["llamacpp_url"])
    if "timeout_seconds" in section:
        cfg.timeout_seconds = _validate_positive_float(
            "timeout_seconds", section["timeout_seconds"]
        )
    if "max_file_size_bytes" in section:
        cfg.max_file_size_bytes = _validate_positive_int(
            "max_file_size_bytes", section["max_file_size_bytes"]
        )
    if "max_chunk_chars" in section:
        cfg.max_chunk_chars = _validate_positive_int("max_chunk_chars", section["max_chunk_chars"])
    if "fail_on" in section:
        cfg.fail_on = _validate_fail_on(section["fail_on"])
    if "ignore_globs" in section:
        cfg.ignore_globs = _validate_str_list("ignore_globs", section["ignore_globs"])
    if "rules" in section:
        cfg.rules = _validate_str_list("rules", section["rules"])

    return cfg


def render_starter_config() -> str:
    """Return a documented starter `.diffwise.toml` configuration file."""
    return (
        "# diffwise configuration\n"
        "# CLI flags always override settings in this file.\n\n"
        f'model = "{DEFAULT_MODEL}"\n'
        f'backend = "{DEFAULT_BACKEND}"\n\n'
        f'ollama_url = "{DEFAULT_OLLAMA_URL}"\n'
        f'llamacpp_url = "{DEFAULT_LLAMACPP_URL}"\n'
        f"timeout_seconds = {DEFAULT_TIMEOUT_SECONDS}\n\n"
        f"max_file_size_bytes = {DEFAULT_MAX_FILE_SIZE_BYTES}\n"
        f"max_chunk_chars = {DEFAULT_MAX_CHUNK_CHARS}\n\n"
        '# Block commits when findings reach threshold: "bug", "security", or "none"\n'
        'fail_on = "bug"\n\n'
        "# Glob patterns to skip during review\n"
        "ignore_globs = [\n"
        '  "dist/**",\n'
        '  "build/**",\n'
        '  "*.snap",\n'
        '  "*.generated.*",\n'
        "]\n\n"
        "# Custom review rules appended to the system prompt\n"
        "rules = [\n"
        '  "Flag hardcoded credentials, API keys, or secrets.",\n'
        '  "Ensure SQL queries use parameterized bindings instead of string interpolation.",\n'
        '  "Check error handling around external I/O, network requests, and subprocess calls.",\n'
        "]\n"
    )


def write_starter_config(target_dir: Path | None = None, *, force: bool = False) -> Path:
    """Write a starter `.diffwise.toml` in `target_dir` and return its path."""
    base_dir = (target_dir or Path.cwd()).resolve()
    dest = base_dir / DEFAULT_CONFIG_FILENAME
    if dest.exists() and not force:
        raise ConfigError(
            f"{dest} already exists. Pass --force to overwrite the existing configuration."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_starter_config(), encoding="utf-8", newline="\n")
    return dest


