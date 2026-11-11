"""Git diff extraction, unified diff parsing, filtering, and context chunking."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo

from diffwise.config import DEFAULT_MAX_CHUNK_CHARS, DEFAULT_MAX_FILE_SIZE_BYTES

EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

FileStatus = Literal["modified", "added", "deleted", "renamed", "copied", "typechange"]
LineKind = Literal["context", "add", "remove"]
SkipReason = Literal["binary", "lockfile", "minified", "ignored", "oversized"]

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_DIFF_GIT_QUOTED_RE = re.compile(
    r'^diff --git ("(?:[^"\\]|\\.)+"|[^ ]+) ("(?:[^"\\]|\\.)+"|[^ ]+)$'
)
_DIFF_GIT_SAME_RE = re.compile(r"^diff --git a/(.+) b/\1$")

_LOCKFILE_NAMES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "bun.lock",
        "poetry.lock",
        "pipfile.lock",
        "uv.lock",
        "pdm.lock",
        "cargo.lock",
        "gemfile.lock",
        "composer.lock",
        "go.sum",
        "flake.lock",
        "mix.lock",
        "pubspec.lock",
        "podfile.lock",
        "packages.resolved",
        "gradle.lockfile",
        "shrinkwrap.yaml",
        "npm-shrinkwrap.json",
    }
)

_MINIFIED_SUFFIXES: tuple[str, ...] = (
    ".min.js",
    ".min.css",
    ".min.mjs",
    ".min.cjs",
    ".min.html",
    ".min.svg",
    ".bundle.js",
    ".bundle.css",
    ".chunk.js",
    ".js.map",
    ".css.map",
)

_EXACT_FILENAMES: dict[str, str] = {
    "dockerfile": "dockerfile",
    "containerfile": "dockerfile",
    "makefile": "makefile",
    "gnumakefile": "makefile",
    "jenkinsfile": "groovy",
    "vagrantfile": "ruby",
    "cmakelists.txt": "cmake",
    ".bashrc": "bash",
    ".zshrc": "bash",
    ".bash_profile": "bash",
    ".gitignore": "text",
    ".gitattributes": "text",
}

_EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".scala": "scala",
    ".sql": "sql",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".ps1": "powershell",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".jsonc": "json",
    ".xml": "xml",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".md": "markdown",
    ".mdx": "markdown",
    ".tf": "terraform",
    ".hcl": "hcl",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".proto": "protobuf",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".r": "r",
    ".lua": "lua",
    ".dart": "dart",
    ".ex": "elixir",
    ".exs": "elixir",
    ".vue": "vue",
    ".svelte": "svelte",
}


class GitDiffError(RuntimeError):
    """Raised when diff extraction fails due to repository state or invalid refs."""


@dataclass(slots=True)
class DiffLine:
    """Single line inside a unified diff hunk."""

    kind: LineKind
    content: str
    old_lineno: int | None
    new_lineno: int | None

    def format_patch_line(self, include_line_numbers: bool = False) -> str:
        """Render this line in standard unified diff form or with line number tags."""
        prefix = {"context": " ", "add": "+", "remove": "-"}[self.kind]
        if not include_line_numbers:
            return f"{prefix}{self.content}"
        if self.new_lineno is not None:
            tag = f"L{self.new_lineno:<4d}"
        elif self.old_lineno is not None:
            tag = f"OLD{self.old_lineno:<3d}"
        else:
            tag = "     "
        return f"[{tag}] {prefix}{self.content}"


@dataclass(slots=True)
class DiffHunk:
    """Contiguous hunk of changed and surrounding context lines."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def added_lines(self) -> list[tuple[int, str]]:
        """Return `(new_lineno, content)` pairs for all added lines in this hunk."""
        return [
            (line.new_lineno, line.content)
            for line in self.lines
            if line.kind == "add" and line.new_lineno is not None
        ]

    @property
    def removed_lines(self) -> list[tuple[int, str]]:
        """Return `(old_lineno, content)` pairs for all removed lines in this hunk."""
        return [
            (line.old_lineno, line.content)
            for line in self.lines
            if line.kind == "remove" and line.old_lineno is not None
        ]

    def to_patch(self, include_line_numbers: bool = False) -> str:
        """Serialize this hunk back to unified diff text."""
        body = [line.format_patch_line(include_line_numbers) for line in self.lines]
        return "\n".join([self.header, *body])


@dataclass(slots=True)
class FileDiff:
    """Parsed unified diff representation for a single file."""

    path: str
    language: str
    status: FileStatus = "modified"
    old_path: str | None = None
    hunks: list[DiffHunk] = field(default_factory=list)
    is_binary: bool = False
    raw_size_bytes: int = 0

    @property
    def added_lines(self) -> list[tuple[int, str]]:
        """Return all `(new_lineno, content)` additions across every hunk."""
        result: list[tuple[int, str]] = []
        for hunk in self.hunks:
            result.extend(hunk.added_lines)
        return result

    @property
    def removed_lines(self) -> list[tuple[int, str]]:
        """Return all `(old_lineno, content)` deletions across every hunk."""
        result: list[tuple[int, str]] = []
        for hunk in self.hunks:
            result.extend(hunk.removed_lines)
        return result

    @property
    def is_empty(self) -> bool:
        """Return True if this file diff contains no hunks."""
        return len(self.hunks) == 0

    def to_patch(self, include_line_numbers: bool = False) -> str:
        """Render the file header and all hunks as unified diff text."""
        header_lines = _format_file_header(self.path, self.status, self.old_path)
        hunk_texts = [hunk.to_patch(include_line_numbers) for hunk in self.hunks]
        return "\n".join([*header_lines, *hunk_texts])


@dataclass(slots=True)
class SkippedFile:
    """Metadata describing why a file was excluded from LLM review."""

    path: str
    reason: SkipReason
    detail: str = ""


@dataclass(slots=True)
class DiffChunk:
    """Context-bounded chunk belonging to a single file for LLM prompt submission."""

    file_path: str
    language: str
    status: FileStatus
    old_path: str | None
    hunks: list[DiffHunk]
    chunk_index: int = 1
    total_chunks: int = 1

    def to_patch(self, include_line_numbers: bool = True) -> str:
        """Render this chunk's file header and hunks for model consumption."""
        header_lines = _format_file_header(self.file_path, self.status, self.old_path)
        hunk_texts = [hunk.to_patch(include_line_numbers) for hunk in self.hunks]
        return "\n".join([*header_lines, *hunk_texts])


@dataclass(slots=True)
class DiffCollection:
    """Result of collecting and filtering diffs from a git repository."""

    files: list[FileDiff] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    target: str = "staged"

    @property
    def is_empty(self) -> bool:
        """Return True when nothing was changed or staged at all."""
        return len(self.files) == 0 and len(self.skipped) == 0

    @property
    def has_reviewable_files(self) -> bool:
        """Return True when at least one non-skipped file has hunks to review."""
        return any(not f.is_empty for f in self.files)


def _format_file_header(path: str, status: FileStatus, old_path: str | None) -> list[str]:
    lines = [f"File: {path} ({status})"]
    if status == "renamed" and old_path:
        lines.append(f"Renamed from: {old_path}")
    return lines


def detect_language(path: str) -> str:
    """Infer a Pygments-compatible language identifier from a file path."""
    posix_name = PurePosixPath(path.replace("\\", "/")).name
    lower_name = posix_name.lower()
    if lower_name in _EXACT_FILENAMES:
        return _EXACT_FILENAMES[lower_name]
    suffix = PurePosixPath(lower_name).suffix
    if suffix in _EXTENSION_LANGUAGES:
        return _EXTENSION_LANGUAGES[suffix]
    return "text"


def is_lockfile(path: str) -> bool:
    """Return True if the file path refers to a known dependency lockfile."""
    name = PurePosixPath(path.replace("\\", "/")).name.lower()
    return name in _LOCKFILE_NAMES or name.endswith(".lock")


def is_minified_path(path: str) -> bool:
    """Return True if the file path uses a minified asset or source-map suffix."""
    lower = path.replace("\\", "/").lower()
    return lower.endswith(_MINIFIED_SUFFIXES)


def is_minified_content(file_diff: FileDiff) -> bool:
    """Heuristically detect minified code from extreme single-line lengths."""
    candidate_lines = [content for _, content in file_diff.added_lines if content]
    if not candidate_lines:
        candidate_lines = [content for _, content in file_diff.removed_lines if content]
    if not candidate_lines:
        return False

    lengths = [len(line) for line in candidate_lines]
    max_len = max(lengths)
    avg_len = sum(lengths) / len(lengths)
    return max_len >= 1000 and avg_len >= 400


def matches_ignore_globs(path: str, ignore_globs: Sequence[str]) -> str | None:
    """Return the first matching glob pattern for `path`, or None if unignored."""
    norm_path = path.replace("\\", "/").lstrip("./")
    basename = PurePosixPath(norm_path).name
    for pattern in ignore_globs:
        clean_pattern = pattern.strip().replace("\\", "/").lstrip("./")
        if not clean_pattern:
            continue
        if fnmatch.fnmatch(norm_path, clean_pattern):
            return pattern
        if clean_pattern.endswith("/**") and norm_path.startswith(clean_pattern[:-2]):
            return pattern
        if "/" not in clean_pattern and fnmatch.fnmatch(basename, clean_pattern):
            return pattern
    return None


def _unquote_git_path(token: str) -> str:
    """Decode C-style quoted git paths when present."""
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        inner = token[1:-1]
        # Decode standard git backslash escapes (\", \\, \t, \n, octal \NNN)
        result: list[str] = []
        i = 0
        raw_bytes = bytearray()
        while i < len(inner):
            ch = inner[i]
            if ch != "\\":
                if raw_bytes:
                    result.append(raw_bytes.decode("utf-8", errors="replace"))
                    raw_bytes.clear()
                result.append(ch)
                i += 1
                continue
            i += 1
            if i >= len(inner):
                break
            esc = inner[i]
            if esc in ("\\", '"'):
                if raw_bytes:
                    result.append(raw_bytes.decode("utf-8", errors="replace"))
                    raw_bytes.clear()
                result.append(esc)
                i += 1
            elif esc == "t":
                raw_bytes.append(ord("\t"))
                i += 1
            elif esc == "n":
                raw_bytes.append(ord("\n"))
                i += 1
            elif esc == "r":
                raw_bytes.append(ord("\r"))
                i += 1
            elif esc in "01234567" and i + 2 < len(inner) and all(
                c in "01234567" for c in inner[i : i + 3]
            ):
                raw_bytes.append(int(inner[i : i + 3], 8))
                i += 3
            else:
                if raw_bytes:
                    result.append(raw_bytes.decode("utf-8", errors="replace"))
                    raw_bytes.clear()
                result.append(esc)
                i += 1
        if raw_bytes:
            result.append(raw_bytes.decode("utf-8", errors="replace"))
        return "".join(result)
    return token


def _strip_ab_prefix(path: str, prefix: str) -> str:
    if path.startswith(prefix):
        return path[len(prefix) :]
    return path


def _parse_diff_git_header(line: str) -> tuple[str | None, str | None]:
    same_match = _DIFF_GIT_SAME_RE.match(line)
    if same_match:
        rel = same_match.group(1)
        return rel, rel
    quoted_match = _DIFF_GIT_QUOTED_RE.match(line)
    if quoted_match:
        left = _strip_ab_prefix(_unquote_git_path(quoted_match.group(1)), "a/")
        right = _strip_ab_prefix(_unquote_git_path(quoted_match.group(2)), "b/")
        return left, right
    return None, None


def _parse_header_path(raw_value: str, expected_prefix: str) -> str | None:
    stripped = raw_value.strip()
    if stripped == "/dev/null":
        return None
    unquoted = _unquote_git_path(stripped)
    return _strip_ab_prefix(unquoted, expected_prefix)


def parse_unified_diff(raw_diff: str) -> list[FileDiff]:
    """Parse raw `git diff` output into structured `FileDiff` objects."""
    if not raw_diff or not raw_diff.strip():
        return []

    normalized = raw_diff.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    files: list[FileDiff] = []

    # Active file builder state
    in_file = False
    section_lines: list[str] = []
    status: FileStatus = "modified"
    a_header_path: str | None = None
    b_header_path: str | None = None
    old_path: str | None = None
    new_path: str | None = None
    is_binary = False
    hunks: list[DiffHunk] = []

    # Active hunk state
    active_hunk: DiffHunk | None = None
    old_ln = 0
    new_ln = 0
    remaining_old = 0
    remaining_new = 0

    def finalize_hunk() -> None:
        nonlocal active_hunk
        if active_hunk is not None:
            hunks.append(active_hunk)
            active_hunk = None

    def finalize_file() -> None:
        nonlocal in_file
        if not in_file:
            return
        finalize_hunk()
        if status == "deleted":
            canonical_path = old_path or a_header_path or new_path or b_header_path or ""
        else:
            canonical_path = new_path or b_header_path or old_path or a_header_path or ""
        canonical_path = canonical_path.replace("\\", "/")
        clean_old = old_path.replace("\\", "/") if old_path else None
        raw_bytes = len("\n".join(section_lines).encode("utf-8"))
        files.append(
            FileDiff(
                path=canonical_path,
                language=detect_language(canonical_path),
                status=status,
                old_path=clean_old,
                hunks=list(hunks),
                is_binary=is_binary,
                raw_size_bytes=raw_bytes,
            )
        )
        in_file = False

    for line in lines:
        # Consume lines inside an active hunk strictly by expected line counts
        if active_hunk is not None and (remaining_old > 0 or remaining_new > 0):
            section_lines.append(line)
            if line.startswith("\\"):
                # "\ No newline at end of file" does not affect line numbers
                continue
            if line.startswith("+"):
                active_hunk.lines.append(DiffLine("add", line[1:], None, new_ln))
                new_ln += 1
                remaining_new -= 1
            elif line.startswith("-"):
                active_hunk.lines.append(DiffLine("remove", line[1:], old_ln, None))
                old_ln += 1
                remaining_old -= 1
            elif line.startswith(" ") or line == "":
                content = line[1:] if line.startswith(" ") else ""
                active_hunk.lines.append(DiffLine("context", content, old_ln, new_ln))
                old_ln += 1
                new_ln += 1
                remaining_old -= 1
                remaining_new -= 1
            else:
                # Unexpected line terminates the hunk early and falls through to header parsing
                remaining_old = 0
                remaining_new = 0
                finalize_hunk()
                section_lines.pop()
            if active_hunk is not None and remaining_old == 0 and remaining_new == 0:
                finalize_hunk()
            if active_hunk is not None or (
                remaining_old == 0
                and remaining_new == 0
                and (line.startswith(("+", "-", " ", "\\")) or line == "")
            ):
                continue

        if line.startswith("diff --git "):
            finalize_file()
            in_file = True
            section_lines = [line]
            status = "modified"
            a_header_path, b_header_path = _parse_diff_git_header(line)
            old_path = a_header_path
            new_path = b_header_path
            is_binary = False
            hunks = []
            continue

        if not in_file:
            continue

        section_lines.append(line)

        if line.startswith("new file mode "):
            status = "added"
        elif line.startswith("deleted file mode "):
            status = "deleted"
        elif line.startswith("rename from "):
            status = "renamed"
            old_path = _unquote_git_path(line[len("rename from ") :])
        elif line.startswith("rename to "):
            status = "renamed"
            new_path = _unquote_git_path(line[len("rename to ") :])
        elif line.startswith("copy from "):
            status = "copied"
            old_path = _unquote_git_path(line[len("copy from ") :])
        elif line.startswith("copy to "):
            status = "copied"
            new_path = _unquote_git_path(line[len("copy to ") :])
        elif (line.startswith("Binary files ") and line.endswith(" differ")) or (
            line == "GIT binary patch"
        ):
            is_binary = True
        elif line.startswith("--- "):
            parsed_old = _parse_header_path(line[4:], "a/")
            if parsed_old is None:
                status = "added"
                old_path = None
            else:
                old_path = parsed_old
        elif line.startswith("+++ "):
            parsed_new = _parse_header_path(line[4:], "b/")
            if parsed_new is None:
                status = "deleted"
                new_path = None
            else:
                new_path = parsed_new
        else:
            hunk_match = _HUNK_HEADER_RE.match(line)
            if hunk_match:
                finalize_hunk()
                old_start = int(hunk_match.group(1))
                old_count = int(hunk_match.group(2)) if hunk_match.group(2) is not None else 1
                new_start = int(hunk_match.group(3))
                new_count = int(hunk_match.group(4)) if hunk_match.group(4) is not None else 1
                active_hunk = DiffHunk(
                    old_start=old_start,
                    old_count=old_count,
                    new_start=new_start,
                    new_count=new_count,
                    header=line,
                )
                old_ln = old_start
                new_ln = new_start
                remaining_old = old_count
                remaining_new = new_count
                if remaining_old == 0 and remaining_new == 0:
                    finalize_hunk()

    finalize_file()
    return files


def filter_file_diffs(
    parsed_files: Sequence[FileDiff],
    *,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    ignore_globs: Sequence[str] = (),
) -> tuple[list[FileDiff], list[SkippedFile]]:
    """Filter parsed diffs, skipping ignored, binary, lockfile, minified, and oversized files."""
    kept: list[FileDiff] = []
    skipped: list[SkippedFile] = []

    for file_diff in parsed_files:
        matched_glob = matches_ignore_globs(file_diff.path, ignore_globs)
        if matched_glob is not None:
            skipped.append(
                SkippedFile(
                    path=file_diff.path,
                    reason="ignored",
                    detail=f"Matched ignore glob '{matched_glob}'",
                )
            )
            continue
        if file_diff.is_binary:
            skipped.append(SkippedFile(path=file_diff.path, reason="binary", detail="Binary file"))
            continue
        if is_lockfile(file_diff.path):
            skipped.append(SkippedFile(path=file_diff.path, reason="lockfile", detail="Lockfile"))
            continue
        if is_minified_path(file_diff.path) or is_minified_content(file_diff):
            skipped.append(
                SkippedFile(path=file_diff.path, reason="minified", detail="Minified file")
            )
            continue
        if file_diff.raw_size_bytes > max_file_size_bytes:
            skipped.append(
                SkippedFile(
                    path=file_diff.path,
                    reason="oversized",
                    detail=f"{file_diff.raw_size_bytes} bytes > {max_file_size_bytes} limit",
                )
            )
            continue
        kept.append(file_diff)

    return kept, skipped


def _build_sub_hunk(lines: list[DiffLine], original_header: str) -> DiffHunk:
    """Create a synthetic DiffHunk for a slice of lines, preserving accurate line numbers."""
    old_lines = [line.old_lineno for line in lines if line.old_lineno is not None]
    new_lines = [line.new_lineno for line in lines if line.new_lineno is not None]
    old_start = old_lines[0] if old_lines else 0
    old_count = len(old_lines)
    new_start = new_lines[0] if new_lines else 0
    new_count = len(new_lines)

    # Preserve any trailing function context from the original @@ header
    func_suffix = ""
    match = _HUNK_HEADER_RE.match(original_header)
    if match and match.group(5):
        func_suffix = match.group(5)

    header = f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{func_suffix}"
    return DiffHunk(
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        header=header,
        lines=list(lines),
    )


def _split_large_hunk(hunk: DiffHunk, max_body_chars: int) -> list[DiffHunk]:
    """Split a single hunk whose serialized size exceeds `max_body_chars` into sub-hunks."""
    if max_body_chars <= 0 or not hunk.lines:
        return [hunk]

    sub_hunks: list[DiffHunk] = []
    current_lines: list[DiffLine] = []
    current_chars = len(hunk.header) + 1

    for line in hunk.lines:
        rendered_len = len(line.format_patch_line(include_line_numbers=True)) + 1
        if current_lines and current_chars + rendered_len > max_body_chars:
            sub_hunks.append(_build_sub_hunk(current_lines, hunk.header))
            current_lines = []
            current_chars = len(hunk.header) + 1
        current_lines.append(line)
        current_chars += rendered_len

    if current_lines:
        sub_hunks.append(_build_sub_hunk(current_lines, hunk.header))

    return sub_hunks


def chunk_file_diff(
    file_diff: FileDiff,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> list[DiffChunk]:
    """Split a single `FileDiff` into context-sized `DiffChunk` objects by hunk group."""
    if not file_diff.hunks:
        return []

    header_lines = _format_file_header(file_diff.path, file_diff.status, file_diff.old_path)
    header_overhead = len("\n".join(header_lines)) + 2
    budget = max(200, max_chunk_chars - header_overhead)

    # First expand any single hunk that exceeds the chunk budget on its own
    expanded_hunks: list[DiffHunk] = []
    for hunk in file_diff.hunks:
        hunk_len = len(hunk.to_patch(include_line_numbers=True))
        if hunk_len > budget:
            expanded_hunks.extend(_split_large_hunk(hunk, budget))
        else:
            expanded_hunks.append(hunk)

    grouped: list[list[DiffHunk]] = []
    current_group: list[DiffHunk] = []
    current_chars = 0

    for hunk in expanded_hunks:
        hunk_chars = len(hunk.to_patch(include_line_numbers=True)) + 1
        if current_group and current_chars + hunk_chars > budget:
            grouped.append(current_group)
            current_group = [hunk]
            current_chars = hunk_chars
        else:
            current_group.append(hunk)
            current_chars += hunk_chars

    if current_group:
        grouped.append(current_group)

    total = len(grouped)
    return [
        DiffChunk(
            file_path=file_diff.path,
            language=file_diff.language,
            status=file_diff.status,
            old_path=file_diff.old_path,
            hunks=group,
            chunk_index=idx,
            total_chunks=total,
        )
        for idx, group in enumerate(grouped, start=1)
    ]


def chunk_diffs(
    files: Sequence[FileDiff],
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> list[DiffChunk]:
    """Split all `FileDiff` objects into per-file, hunk-grouped `DiffChunk`s."""
    chunks: list[DiffChunk] = []
    for file_diff in files:
        chunks.extend(chunk_file_diff(file_diff, max_chunk_chars=max_chunk_chars))
    return chunks


def _open_repo(repo_path: Path | None) -> Repo:
    start = repo_path or Path.cwd()
    try:
        return Repo(start, search_parent_directories=True)
    except (InvalidGitRepositoryError, NoSuchPathError) as exc:
        raise GitDiffError(f"Not a git repository: {start}") from exc


def _validate_single_ref(repo: Repo, ref: str) -> str:
    clean = ref.strip()
    if not clean or clean.startswith("-") or any(ord(c) < 32 for c in clean):
        raise GitDiffError(f"Invalid git reference: {ref!r}")
    try:
        commit = repo.commit(clean)
    except Exception as exc:
        raise GitDiffError(f"Unknown git reference: {ref!r}") from exc
    return str(commit.hexsha)


def _run_git_diff(repo: Repo, diff_args: list[str]) -> str:
    cmd = [
        "git",
        "-c",
        "core.quotePath=false",
        "diff",
        "--unified=3",
        "--find-renames",
        "--no-color",
        "--no-ext-diff",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        *diff_args,
    ]
    try:
        output = repo.git.execute(cmd, with_stdout=True)
    except GitCommandError as exc:
        raise GitDiffError(f"git diff failed: {exc.stderr or exc}") from exc
    return output if isinstance(output, str) else output.decode("utf-8", errors="replace")


def get_staged_diff(
    repo_path: Path | None = None,
    *,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    ignore_globs: Sequence[str] = (),
) -> DiffCollection:
    """Extract, parse, and filter the staged git diff from the repository."""
    repo = _open_repo(repo_path)
    raw = _run_git_diff(repo, ["--cached"])
    parsed = parse_unified_diff(raw)
    kept, skipped = filter_file_diffs(
        parsed,
        max_file_size_bytes=max_file_size_bytes,
        ignore_globs=ignore_globs,
    )
    return DiffCollection(files=kept, skipped=skipped, target="staged")


def get_commit_diff(
    commit_ref: str,
    repo_path: Path | None = None,
    *,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    ignore_globs: Sequence[str] = (),
) -> DiffCollection:
    """Extract, parse, and filter the diff for a commit or commit range (e.g. `HEAD`, `a..b`)."""
    repo = _open_repo(repo_path)
    clean_ref = commit_ref.strip()
    if not clean_ref or clean_ref.startswith("-") or any(ord(c) < 32 for c in clean_ref):
        raise GitDiffError(f"Invalid git reference: {commit_ref!r}")

    if "..." in clean_ref:
        parts = clean_ref.split("...")
        if len(parts) != 2:
            raise GitDiffError(f"Invalid git range: {commit_ref!r}")
        left = _validate_single_ref(repo, parts[0] or "HEAD")
        right = _validate_single_ref(repo, parts[1] or "HEAD")
        diff_args = [f"{left}...{right}"]
    elif ".." in clean_ref:
        parts = clean_ref.split("..")
        if len(parts) != 2:
            raise GitDiffError(f"Invalid git range: {commit_ref!r}")
        left = _validate_single_ref(repo, parts[0] or "HEAD")
        right = _validate_single_ref(repo, parts[1] or "HEAD")
        diff_args = [left, right]
    else:
        commit_sha = _validate_single_ref(repo, clean_ref)
        commit_obj = repo.commit(commit_sha)
        parent_sha = commit_obj.parents[0].hexsha if commit_obj.parents else EMPTY_TREE_SHA
        diff_args = [parent_sha, commit_sha]

    raw = _run_git_diff(repo, diff_args)
    parsed = parse_unified_diff(raw)
    kept, skipped = filter_file_diffs(
        parsed,
        max_file_size_bytes=max_file_size_bytes,
        ignore_globs=ignore_globs,
    )
    return DiffCollection(files=kept, skipped=skipped, target=clean_ref)

