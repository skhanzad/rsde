import shutil
import subprocess

import pytest

from helpers import write_files
from rsde.repository.config import ConfigError, WorkspaceError, find_workspace_root, load_config, open_workspace
from rsde.repository.files import (
    FileHasher,
    compile_pattern,
    diff_snapshots,
    list_workspace_files,
    matching_files,
    take_snapshot,
)


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("src/a.py", "src/a.py", True),
        ("src/a.py", "src/a.pyc", False),
        ("src", "src/x/y.py", True),
        ("src/", "src/x.py", True),
        ("src/", "srcx/a.py", False),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/sub/a.py", False),
        ("src/*", "src/sub/a.py", True),
        ("src/**/*.py", "src/a.py", True),
        ("src/**/*.py", "src/a/b/c.py", True),
        ("**/test_*.py", "tests/unit/test_a.py", True),
        ("tests/test_?.py", "tests/test_a.py", True),
        ("tests/test_[ab].py", "tests/test_c.py", False),
        ("tests/test_[!ab].py", "tests/test_c.py", True),
        ("a.b", "aXb", False),
    ],
)
def test_ownership_patterns(pattern, path, expected):
    assert bool(compile_pattern(pattern).match(path)) is expected


def test_matching_files_is_sorted_and_deduplicated():
    files = ["b.py", "a.py", "lib/c.py"]
    assert matching_files(files, ["*.py", "a.py"]) == ["a.py", "b.py"]
    assert matching_files(files, []) == []


def test_listing_skips_state_caches_and_ignored_paths(tmp_path):
    write_files(
        tmp_path,
        {
            "a.py": "",
            ".rsde/state.json": "",
            "pkg/__pycache__/x.pyc": "",
            "node_modules/m/index.js": "",
            "docs/notes.md": "",
            "b.pyc": "",
        },
    )
    assert list_workspace_files(tmp_path, ignore=["docs/**"]) == ["a.py"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_listing_respects_gitignore_inside_git_repositories(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    write_files(tmp_path, {".gitignore": "build/\n", "build/out.txt": "", "src/a.py": ""})
    assert list_workspace_files(tmp_path) == [".gitignore", "src/a.py"]


def test_snapshots_detect_created_modified_and_deleted_files(tmp_path):
    write_files(tmp_path, {"a.txt": "one", "b.txt": "two"})
    hasher = FileHasher(tmp_path)
    before = take_snapshot(tmp_path, hasher)
    (tmp_path / "a.txt").write_text("one, changed")
    (tmp_path / "b.txt").unlink()
    (tmp_path / "c.txt").write_text("three")
    changes = diff_snapshots(before, take_snapshot(tmp_path, hasher))
    assert (changes.created, changes.modified, changes.deleted) == (("c.txt",), ("a.txt",), ("b.txt",))
    assert changes.paths == ("a.txt", "b.txt", "c.txt") and len(changes) == 3


def test_digest_depends_on_names_and_contents(tmp_path):
    write_files(tmp_path, {"a.txt": "x", "b.txt": "x"})
    hasher = FileHasher(tmp_path)
    assert hasher.digest(["a.txt"]) != hasher.digest(["b.txt"])
    first = hasher.digest(["a.txt", "b.txt"])
    assert first == hasher.digest(["b.txt", "a.txt"])
    (tmp_path / "a.txt").write_text("changed")
    assert hasher.digest(["a.txt", "b.txt"]) != first


def test_config_defaults(tmp_path):
    config = load_config(tmp_path)
    assert (config.root_spec, config.agent, config.max_attempts, config.ignore) == ("master.md", "none", 3, ())


def test_config_file_is_loaded(tmp_path):
    write_files(
        tmp_path,
        {
            "rsde.toml": """
            [project]
            root = "spec/root.md"
            ignore = ["vendor/**"]
            [execute]
            agent = "claude"
            max_attempts = 5
            verify_timeout = 30
            strict_scope = true
            [agents.claude]
            command = ["claude", "-p", "{prompt}"]
            """
        },
    )
    config = load_config(tmp_path)
    assert config.root_spec == "spec/root.md" and config.ignore == ("vendor/**",)
    assert (config.agent, config.max_attempts, config.verify_timeout, config.strict_scope) == ("claude", 5, 30.0, True)
    assert config.agents["claude"]["command"][0] == "claude"


@pytest.mark.parametrize(
    "content,message",
    [
        ("[execute]\nmax_atempts = 2\n", "did you mean `max_attempts`"),
        ("[projcet]\nroot = 'x'\n", "did you mean `project`"),
        ("[execute]\nmax_attempts = '3'\n", "must be an integer"),
        ("[execute]\nmax_attempts = 0\n", "at least 1"),
        ("[project\n", "invalid TOML"),
        ("agents = 3\n", "[agents] must contain tables"),
    ],
)
def test_config_errors_are_explicit(tmp_path, content, message):
    (tmp_path / "rsde.toml").write_text(content)
    with pytest.raises(ConfigError, match=message.replace("[", r"\[").replace("]", r"\]")):
        load_config(tmp_path)


def test_workspace_root_is_found_from_nested_directories(tmp_path):
    write_files(tmp_path, {"master.md": "# M", "a/b/c.txt": ""})
    assert find_workspace_root(tmp_path / "a" / "b") == tmp_path.resolve()
    assert find_workspace_root(tmp_path / "a" / "b" / "c.txt") == tmp_path.resolve()


def test_missing_workspace_explains_how_to_create_one(tmp_path):
    with pytest.raises(WorkspaceError, match="rsde init"):
        open_workspace(tmp_path)


def test_hash_cache_never_trusts_a_recently_modified_file(tmp_path):
    import os

    path = tmp_path / "a.txt"
    path.write_text("aaaa")
    hasher = FileHasher(tmp_path)
    first = hasher.hash_file("a.txt")
    stat = path.stat()
    path.write_text("bbbb")  # same size …
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # … and the same timestamp tick
    assert hasher.hash_file("a.txt") != first


def test_state_directory_and_bad_globs_in_config(tmp_path):
    from rsde.repository.config import Workspace, load_config

    (tmp_path / "rsde.toml").write_text('[project]\nstate_dir = "rsde-state"\nignore = ["vendor/**"]\n')
    ws = Workspace(tmp_path, load_config(tmp_path))
    assert ws.ignore == ("vendor/**", "rsde-state/")
    write_files(tmp_path, {"rsde-state/state.json": "{}", "a.py": ""})
    assert list_workspace_files(tmp_path, ws.ignore) == ["a.py", "rsde.toml"]
    (tmp_path / "rsde.toml").write_text('[project]\nignore = ["src/[z-a]"]\n')
    with pytest.raises(ConfigError, match="invalid glob"):
        load_config(tmp_path)
