"""scripts/data-branch.sh against a throwaway origin, cloned shallow the way CI clones."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "data-branch.sh"
BRANCH = "farewatch-data"


def git(*args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def script(*args, cwd):
    return subprocess.run([str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def origin(tmp_path):
    """A bare repository whose main branch holds one code file."""
    origin = tmp_path / "origin.git"
    git("init", "--quiet", "--bare", "--initial-branch=main", str(origin), cwd=tmp_path)
    seed = tmp_path / "seed"
    git("clone", "--quiet", str(origin), str(seed), cwd=tmp_path)
    (seed / "code.txt").write_text("code\n")
    git("add", ".", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "--quiet", "-m", "code", cwd=seed)
    git("push", "--quiet", "origin", "HEAD:main", cwd=seed)
    return origin


def ci_clone(origin, tmp_path, name):
    target = tmp_path / name
    git("clone", "--quiet", "--depth", "1", f"file://{origin}", str(target), cwd=tmp_path)
    return target


def data_branch_log(origin):
    return git("log", "--format=%s", BRANCH, cwd=origin).splitlines()


def test_first_scan_creates_a_branch_that_holds_only_data(origin, tmp_path):
    clone = ci_clone(origin, tmp_path, "run1")

    assert script("checkout", "data", cwd=clone).returncode == 0
    assert [path.name for path in (clone / "data").iterdir() if path.name != ".git"] == []

    (clone / "data" / "history.jsonl").write_text('{"scan": 1}\n')
    assert script("save", "data", cwd=clone).returncode == 0

    assert git("ls-tree", "--name-only", BRANCH, cwd=origin) == "history.jsonl"
    assert len(data_branch_log(origin)) == 1
    assert git("ls-tree", "--name-only", "main", cwd=origin) == "code.txt"
    assert git("status", "--porcelain", "--untracked-files=no", cwd=clone) == ""


def test_later_scans_continue_from_what_was_saved(origin, tmp_path):
    first = ci_clone(origin, tmp_path, "run1")
    script("checkout", "data", cwd=first)
    (first / "data" / "history.jsonl").write_text('{"scan": 1}\n')
    script("save", "data", cwd=first)

    second = ci_clone(origin, tmp_path, "run2")
    assert script("checkout", "data", cwd=second).returncode == 0
    history = second / "data" / "history.jsonl"
    assert history.read_text() == '{"scan": 1}\n'

    history.write_text(history.read_text() + '{"scan": 2}\n')
    assert script("save", "data", cwd=second).returncode == 0

    assert git("show", f"{BRANCH}:history.jsonl", cwd=origin) == '{"scan": 1}\n{"scan": 2}'
    assert len(data_branch_log(origin)) == 2


def test_saving_without_changes_adds_no_commit(origin, tmp_path):
    first = ci_clone(origin, tmp_path, "run1")
    script("checkout", "data", cwd=first)
    (first / "data" / "state.json").write_text("{}\n")
    script("save", "data", cwd=first)

    second = ci_clone(origin, tmp_path, "run2")
    script("checkout", "data", cwd=second)
    result = script("save", "data", cwd=second)

    assert result.returncode == 0
    assert "nothing new to save" in result.stdout
    assert len(data_branch_log(origin)) == 1


def test_an_unreachable_origin_fails_instead_of_starting_an_empty_history(origin, tmp_path):
    clone = ci_clone(origin, tmp_path, "run1")
    git("remote", "set-url", "origin", f"file://{tmp_path}/gone.git", cwd=clone)

    result = script("checkout", "data", cwd=clone)

    assert result.returncode not in (0, 2)
    assert "cannot list branches on origin" in result.stderr
    assert not (clone / "data").exists()


def test_usage_errors(origin, tmp_path):
    clone = ci_clone(origin, tmp_path, "run1")

    assert script(cwd=clone).returncode != 0
    assert script("checkout", cwd=clone).returncode != 0
    unknown = script("publish", "data", cwd=clone)
    assert unknown.returncode == 2 and "usage:" in unknown.stderr
