"""Tests for diffwise git diff extraction, parsing, filtering, and chunking."""

from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo

from diffwise.config import ConfigError, load_config
from diffwise.git_diff import (
    DiffCollection,
    GitDiffError,
    chunk_diffs,
    detect_language,
    get_commit_diff,
    get_staged_diff,
    parse_unified_diff,
)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Repo:
    """Create a fresh temporary Git repository with deterministic settings."""
    repo = Repo.init(tmp_path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test Developer")
        cw.set_value("user", "email", "dev@example.com")
        cw.set_value("core", "autocrlf", "false")
    return repo


def _write_and_stage(repo: Repo, rel_path: str, content: str | bytes) -> Path:
    assert repo.working_tree_dir is not None
    full_path = Path(repo.working_tree_dir) / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        full_path.write_bytes(content)
    else:
        full_path.write_text(content, encoding="utf-8", newline="\n")
    repo.index.add([rel_path])
    return full_path


def test_empty_staging_area(git_repo: Repo) -> None:
    _write_and_stage(git_repo, "README.md", "# Demo\n")
    git_repo.index.commit("Initial commit")

    assert git_repo.working_tree_dir is not None
    collection: DiffCollection = get_staged_diff(Path(git_repo.working_tree_dir))
    assert collection.is_empty is True
    assert collection.files == []
    assert collection.skipped == []
    assert collection.has_reviewable_files is False


def test_staged_new_file_on_unborn_head(git_repo: Repo) -> None:
    _write_and_stage(
        git_repo,
        "src/hello.py",
        "def greet(name: str) -> str:\n    return f'Hello, {name}'\n",
    )

    assert git_repo.working_tree_dir is not None
    collection = get_staged_diff(Path(git_repo.working_tree_dir))
    assert collection.is_empty is False
    assert len(collection.files) == 1

    fdiff = collection.files[0]
    assert fdiff.path == "src/hello.py"
    assert fdiff.status == "added"
    assert fdiff.language == "python"
    assert fdiff.added_lines == [
        (1, "def greet(name: str) -> str:"),
        (2, "    return f'Hello, {name}'"),
    ]
    assert fdiff.removed_lines == []


def test_staged_modified_file_line_numbers(git_repo: Repo) -> None:
    original = "\n".join(f"line_{i} = {i}" for i in range(1, 25)) + "\n"
    _write_and_stage(git_repo, "calc.py", original)
    git_repo.index.commit("Add calc.py")

    modified_lines = [f"line_{i} = {i}" for i in range(1, 25)]
    modified_lines[2] = "line_3 = 300"  # line 3
    modified_lines.insert(3, "extra = 999")  # inserted at line 4
    modified_lines[20] = "line_20 = 2000"  # shifted line 21 in new file
    _write_and_stage(git_repo, "calc.py", "\n".join(modified_lines) + "\n")

    assert git_repo.working_tree_dir is not None
    collection = get_staged_diff(Path(git_repo.working_tree_dir))
    assert len(collection.files) == 1
    fdiff = collection.files[0]
    assert fdiff.status == "modified"
    assert len(fdiff.hunks) == 2
    assert (3, "line_3 = 300") in fdiff.added_lines
    assert (4, "extra = 999") in fdiff.added_lines
    assert (21, "line_20 = 2000") in fdiff.added_lines
    assert (3, "line_3 = 3") in fdiff.removed_lines
    assert (20, "line_20 = 20") in fdiff.removed_lines


def test_staged_deleted_file(git_repo: Repo) -> None:
    _write_and_stage(git_repo, "obsolete.py", "a = 1\nb = 2\n")
    git_repo.index.commit("Add obsolete file")

    assert git_repo.working_tree_dir is not None
    repo_root = Path(git_repo.working_tree_dir)
    (repo_root / "obsolete.py").unlink()
    git_repo.index.remove(["obsolete.py"])

    collection = get_staged_diff(repo_root)
    assert len(collection.files) == 1
    fdiff = collection.files[0]
    assert fdiff.path == "obsolete.py"
    assert fdiff.status == "deleted"
    assert fdiff.removed_lines == [(1, "a = 1"), (2, "b = 2")]
    assert fdiff.added_lines == []


def test_staged_renamed_file_pure_and_modified(git_repo: Repo) -> None:
    _write_and_stage(git_repo, "old_pure.py", "def keep():\n    return 1\n")
    _write_and_stage(
        git_repo,
        "old_edited.py",
        "\n".join(f"x_{i} = {i}" for i in range(1, 15)) + "\n",
    )
    git_repo.index.commit("Initial files")

    assert git_repo.working_tree_dir is not None
    repo_root = Path(git_repo.working_tree_dir)
    git_repo.index.move(["old_pure.py", "new_pure.py"])

    # Rename + small modification so git detects rename with edits
    (repo_root / "old_edited.py").unlink()
    git_repo.index.remove(["old_edited.py"])
    edited = [f"x_{i} = {i}" for i in range(1, 15)]
    edited[0] = "x_1 = 999"
    _write_and_stage(git_repo, "new_edited.py", "\n".join(edited) + "\n")

    collection = get_staged_diff(repo_root)
    by_path = {f.path: f for f in collection.files}
    assert "new_pure.py" in by_path
    assert by_path["new_pure.py"].status == "renamed"
    assert by_path["new_pure.py"].old_path == "old_pure.py"
    assert by_path["new_pure.py"].hunks == []

    assert "new_edited.py" in by_path
    assert by_path["new_edited.py"].status == "renamed"
    assert by_path["new_edited.py"].old_path == "old_edited.py"
    assert by_path["new_edited.py"].added_lines == [(1, "x_1 = 999")]

    # Pure rename should not produce review chunks
    chunks = chunk_diffs(collection.files)
    assert [c.file_path for c in chunks] == ["new_edited.py"]


def test_skip_binary_lockfiles_minified_and_oversized(git_repo: Repo) -> None:
    _write_and_stage(git_repo, "assets/logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff")
    _write_and_stage(git_repo, "package-lock.json", '{"lockfileVersion": 3}\n')
    _write_and_stage(git_repo, "poetry.lock", "[[package]]\nname = 'typer'\n")
    _write_and_stage(git_repo, "uv.lock", "version = 1\n")
    _write_and_stage(git_repo, "dist/app.min.js", "console.log(1);\n")
    _write_and_stage(git_repo, "static/bundle.js", "var a=1;" * 200 + "\n")
    _write_and_stage(git_repo, "big.py", "\n".join(f"v_{i} = {i}" for i in range(100)) + "\n")
    _write_and_stage(git_repo, "small.py", "ok = True\n")

    assert git_repo.working_tree_dir is not None
    collection = get_staged_diff(
        Path(git_repo.working_tree_dir),
        max_file_size_bytes=400,
    )

    assert [f.path for f in collection.files] == ["small.py"]
    reasons = {s.path: s.reason for s in collection.skipped}
    assert reasons == {
        "assets/logo.png": "binary",
        "package-lock.json": "lockfile",
        "poetry.lock": "lockfile",
        "uv.lock": "lockfile",
        "dist/app.min.js": "minified",
        "static/bundle.js": "minified",
        "big.py": "oversized",
    }


def test_ignore_globs(git_repo: Repo) -> None:
    _write_and_stage(git_repo, "src/generated/schema.py", "A = 1\n")
    _write_and_stage(git_repo, "tests/snapshots/test.snap", "snap\n")
    _write_and_stage(git_repo, "src/app.py", "B = 2\n")

    assert git_repo.working_tree_dir is not None
    collection = get_staged_diff(
        Path(git_repo.working_tree_dir),
        ignore_globs=["src/generated/**", "*.snap"],
    )
    assert [f.path for f in collection.files] == ["src/app.py"]
    assert {s.path: s.reason for s in collection.skipped} == {
        "src/generated/schema.py": "ignored",
        "tests/snapshots/test.snap": "ignored",
    }


def test_chunk_splits_hunks_and_giant_single_hunk(git_repo: Repo) -> None:
    # 120-line newly added file creates one giant hunk
    lines = [f"def fn_{i}(x: int) -> int:\n    return x + {i}" for i in range(60)]
    _write_and_stage(git_repo, "huge.py", "\n".join(lines) + "\n")

    assert git_repo.working_tree_dir is not None
    collection = get_staged_diff(Path(git_repo.working_tree_dir))
    assert len(collection.files) == 1

    chunks = chunk_diffs(collection.files, max_chunk_chars=700)
    assert len(chunks) > 2
    assert all(c.file_path == "huge.py" for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(1, len(chunks) + 1))
    assert all(c.total_chunks == len(chunks) for c in chunks)

    # Every added line from 1..120 must survive chunk splitting in exact order
    reconstructed: list[tuple[int, str]] = []
    for chunk in chunks:
        for hunk in chunk.hunks:
            reconstructed.extend(hunk.added_lines)
    assert [ln for ln, _ in reconstructed] == list(range(1, 121))


def test_parser_resilient_to_diff_markers_in_content() -> None:
    raw_diff = (
        "diff --git a/query.sql b/query.sql\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/query.sql\n"
        "@@ -0,0 +1,4 @@\n"
        "+-- comment with dashes\n"
        "+--- triple dash header lookalike\n"
        "+SELECT 1;\n"
        "+diff --git a/fake b/fake\n"
        "\\ No newline at end of file\n"
    )
    parsed = parse_unified_diff(raw_diff)
    assert len(parsed) == 1
    assert parsed[0].path == "query.sql"
    assert parsed[0].language == "sql"
    assert parsed[0].added_lines == [
        (1, "-- comment with dashes"),
        (2, "--- triple dash header lookalike"),
        (3, "SELECT 1;"),
        (4, "diff --git a/fake b/fake"),
    ]


def test_commit_diff_and_ref_validation(git_repo: Repo) -> None:
    _write_and_stage(git_repo, "main.py", "x = 1\n")
    git_repo.index.commit("Root commit")
    _write_and_stage(git_repo, "main.py", "x = 2\n")
    git_repo.index.commit("Second commit")

    assert git_repo.working_tree_dir is not None
    repo_root = Path(git_repo.working_tree_dir)

    second = get_commit_diff("HEAD", repo_root)
    assert len(second.files) == 1
    assert second.files[0].removed_lines == [(1, "x = 1")]
    assert second.files[0].added_lines == [(1, "x = 2")]

    root = get_commit_diff("HEAD~1", repo_root)
    assert len(root.files) == 1
    assert root.files[0].added_lines == [(1, "x = 1")]

    with pytest.raises(GitDiffError):
        get_commit_diff("--output=/tmp/bad", repo_root)

    with pytest.raises(GitDiffError):
        get_commit_diff("nonexistent-branch", repo_root)


def test_language_detection() -> None:
    assert detect_language("app.py") == "python"
    assert detect_language("web/index.tsx") == "tsx"
    assert detect_language("cmd/server.go") == "go"
    assert detect_language("Dockerfile") == "dockerfile"
    assert detect_language("Makefile") == "makefile"
    assert detect_language("notes.unknownext") == "text"


def test_load_config_from_toml(tmp_path: Path) -> None:
    cfg_file = tmp_path / ".diffwise.toml"
    cfg_file.write_text(
        'model = "qwen2.5-coder:3b"\n'
        'backend = "llamacpp"\n'
        "max_file_size_bytes = 50000\n"
        "max_chunk_chars = 8000\n"
        'fail_on = "security"\n'
        'ignore_globs = ["*.snap", "dist/**"]\n'
        'rules = ["Check SQL queries for parameterization."]\n',
        encoding="utf-8",
    )
    cfg = load_config(start_dir=tmp_path)
    assert cfg.model == "qwen2.5-coder:3b"
    assert cfg.backend == "llamacpp"
    assert cfg.max_file_size_bytes == 50000
    assert cfg.max_chunk_chars == 8000
    assert cfg.fail_on == "security"
    assert cfg.ignore_globs == ["*.snap", "dist/**"]
    assert cfg.rules == ["Check SQL queries for parameterization."]

    cfg_file.write_text("max_file_size_bytes = -10\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(start_dir=tmp_path)


def test_quoted_paths_copy_binary_patch_and_commit_ranges(git_repo: Repo) -> None:
    raw = (
        'diff --git "a/dir name/my file.py" "b/dir name/my file.py"\n'
        '--- "a/dir name/my file.py"\n'
        '+++ "b/dir name/my file.py"\n'
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/bin.dat b/bin.dat\n"
        "GIT binary patch\n"
        "literal 2\n"
        "Jc${@?\n"
        "diff --git a/src.py b/dst.py\n"
        "similarity index 100%\n"
        "copy from src.py\n"
        "copy to dst.py\n"
    )
    parsed = parse_unified_diff(raw)
    assert len(parsed) == 3
    assert parsed[0].path == "dir name/my file.py"
    assert parsed[0].added_lines == [(1, "new")]
    assert "File: dir name/my file.py" in parsed[0].to_patch()
    assert parsed[1].is_binary is True
    assert parsed[2].status == "copied"
    assert parsed[2].old_path == "src.py"
    assert parsed[2].path == "dst.py"

    # Test commit ranges a..b and invalid range
    _write_and_stage(git_repo, "m.py", "v = 1\n")
    c1 = git_repo.index.commit("c1")
    _write_and_stage(git_repo, "m.py", "v = 2\n")
    c2 = git_repo.index.commit("c2")

    assert git_repo.working_tree_dir is not None
    repo_root = Path(git_repo.working_tree_dir)
    range_diff = get_commit_diff(f"{c1.hexsha}..{c2.hexsha}", repo_root)
    assert len(range_diff.files) == 1
    assert range_diff.files[0].added_lines == [(1, "v = 2")]

    with pytest.raises(GitDiffError):
        get_commit_diff("a..b..c", repo_root)


def test_unquote_git_path_flushes_utf8_bytes_before_escape_and_validates_urls(
    tmp_path: Path,
) -> None:
    raw = (
        'diff --git "a/caf\\303\\251\\z.py" "b/caf\\303\\251\\z.py"\n'
        "--- \"a/caf\\303\\251\\z.py\"\n"
        "+++ \"b/caf\\303\\251\\z.py\"\n"
        "@@ -0,0 +1 @@\n"
        "+ok = True\n"
    )
    parsed = parse_unified_diff(raw)
    assert len(parsed) == 1
    assert parsed[0].path == "caféz.py"

    cfg_file = tmp_path / ".diffwise.toml"
    cfg_file.write_text('ollama_url = "ftp://localhost:11434"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="http:// or https://"):
        load_config(start_dir=tmp_path)

    cfg_file.write_text('llamacpp_url = "not-a-url"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="http:// or https://"):
        load_config(start_dir=tmp_path)


