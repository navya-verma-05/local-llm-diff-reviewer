"""Prompt construction and LLM finding response parsing (completed in Phase 3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Severity = Literal["bug", "security", "style", "suggestion"]


@dataclass(slots=True)
class Finding:
    """Structured code review finding returned by the LLM."""

    file: str
    line: int | None
    severity: Severity
    title: str
    explanation: str
    suggested_fix: str = ""

