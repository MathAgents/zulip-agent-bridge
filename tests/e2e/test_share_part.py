"""Scenario 9's `zab share --list` (#775), offline: the fixed script finds the topic's worktree
by the shared file and runs the installed zab there, against a fake zab and a fake VM that runs
the command here, so that its code is exercised before a live run."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from e2e import test_scenarios_features as features

SHA = "0123456789abcdef0123456789abcdef01234567"
LISTING = (
    f"20261103-120000-0a1b2c3d · 2026-11-03T12:00:00+00:00 · shared-1.md · "
    f"https://files.example.org/zab-test/{SHA}/shared-1.md"
)


class Vm:
    """Runs a command here, without the `sudo -u agent` of `as_agent`."""

    def run(self, argv, check=True, timeout=120):
        prefix = features.as_agent()
        assert argv[: len(prefix)] == prefix, argv
        command = argv[len(prefix) :]  # the scenario's fixed script, and the test's paths
        res = subprocess.run(command, capture_output=True, text=True, timeout=30)  # noqa: S603
        return SimpleNamespace(ok=res.returncode == 0, stdout=res.stdout, stderr=res.stderr)


@pytest.fixture
def checkout(tmp_path, monkeypatch) -> Path:
    """A checkout with two topic worktrees, and a fake zab that records where it ran and with
    what, then prints `LISTING` (or fails, given `$FAIL` in its directory)."""
    repo = tmp_path / "zab-test"
    for tree in ("e2e-9-other-1a2b3c", "e2e-9-share-4d5e6f"):
        (repo / ".zab" / "worktrees" / tree).mkdir(parents=True)
    zab = tmp_path / "zab"
    zab.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$PWD" "$*" > "{tmp_path}/ran"\n'
        f'if [ -e "{tmp_path}/FAIL" ]; then echo "zab share: no" >&2; exit 2; fi\n'
        f"echo '{LISTING}'\n"
    )
    zab.chmod(0o755)
    monkeypatch.setattr(features, "ZAB", str(zab))
    monkeypatch.setattr(features, "work", lambda rig: str(repo))
    return repo


def ran(checkout: Path) -> list[str]:
    return (checkout.parent / "ran").read_text().splitlines()


def test_the_listing_runs_in_the_worktree_that_holds_the_file(checkout):
    tree = checkout / ".zab" / "worktrees" / "e2e-9-share-4d5e6f"
    (tree / "shared-1.md").write_text("SHARED-1\n")
    assert features.shares_listed(Vm(), SimpleNamespace(), "shared-1.md") == {SHA: "shared-1.md"}
    assert ran(checkout) == [str(tree), "share --list"]


def test_the_listing_runs_in_the_checkout_that_holds_the_file(checkout):
    """A topic in shared mode works in the checkout itself."""
    (checkout / "shared-1.md").write_text("SHARED-1\n")
    assert features.shares_listed(Vm(), SimpleNamespace(), "shared-1.md") == {SHA: "shared-1.md"}
    assert ran(checkout) == [str(checkout), "share --list"]


def test_no_file_or_a_failed_listing_fails_the_scenario(checkout):
    with pytest.raises(AssertionError, match="neither the checkout nor a worktree"):
        features.shares_listed(Vm(), SimpleNamespace(), "shared-1.md")
    (checkout / "shared-1.md").write_text("SHARED-1\n")
    (checkout.parent / "FAIL").write_text("")
    with pytest.raises(AssertionError, match="zab share --list failed: zab share: no"):
        features.shares_listed(Vm(), SimpleNamespace(), "shared-1.md")
