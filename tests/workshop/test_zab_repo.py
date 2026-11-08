"""Where a release tag installs from (#593, review of #600): one default, the public repository,
in `aws_common.ZAB_REPO_URL`, which `ZAB_REPO` overrides in push_config.py, build_ami.py and
build.sh; and an AMI build without a wheel names its release."""

from __future__ import annotations

import pathlib
import sys

import aws_common
import build_ami as ba
import push_config as pc
import pytest

from .scope import with_build

PUBLIC = "https://github.com/MathAgents/zulip-agent-bridge"
FORK = "https://git.example.org/fork/zab"


@pytest.fixture
def dry(monkeypatch, tmp_path):
    """`build_ami.py --dry-run …`: no boto3, no network; (exit status, what it printed)."""
    monkeypatch.setitem(sys.modules, "boto3", None)
    monkeypatch.setattr(ba, "egress_ip", lambda *a: pytest.fail("no network in a dry run"))
    monkeypatch.setattr(ba.m2_debs, "origin_repo", lambda *a: "o/r")  # whatever the checkout's
    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests test@example\n")

    def run(*args: str, capsys) -> tuple[int, str]:
        argv = ["--dry-run", "--account-id", "123456789012", "--ssh-public-key", str(key)]
        rc = with_build(ba.main, [*argv, *args])
        out = capsys.readouterr()
        return rc, out.out + out.err

    return run


def test_one_default_everywhere(monkeypatch):
    monkeypatch.delenv("ZAB_REPO", raising=False)
    assert aws_common.ZAB_REPO_URL == pc.REPO_URL == ba.ZAB_REPO_URL == PUBLIC
    build = (pathlib.Path(ba.__file__).parent / "build.sh").read_text()
    assert f'\nZAB_REPO="${{ZAB_REPO:-{PUBLIC}}}"\n' in build


def test_the_ami_build_passes_zab_repo_to_build_sh(dry, monkeypatch, capsys):
    monkeypatch.delenv("ZAB_REPO", raising=False)
    rc, out = dry("--zab-tag", "v9.9.9", capsys=capsys)
    assert rc == 0 and f"ZAB_REPO={PUBLIC}" in out and "ZAB_TAG=v9.9.9" in out
    monkeypatch.setenv("ZAB_REPO", FORK)
    rc, out = dry("--zab-tag", "v9.9.9", capsys=capsys)
    assert rc == 0 and f"ZAB_REPO={FORK}" in out
    rc, out = dry("--zab-tag", "v9.9.9", "--zab-repo", PUBLIC, capsys=capsys)
    assert rc == 0 and f"ZAB_REPO={PUBLIC}" in out  # the option wins over the environment


def test_without_a_wheel_the_build_names_its_release(dry, capsys, tmp_path):
    """The public repository carries the releases' tags only, never the old default v0.0.2."""
    rc, out = dry(capsys=capsys)
    assert rc == 2 and "--zab-tag is required without --zab-wheel" in out
    wheel = tmp_path / "zulip_agent_bridge-0.2.9-py3-none-any.whl"
    wheel.write_bytes(b"PK")
    rc, out = dry("--zab-wheel", str(wheel), capsys=capsys)
    assert rc == 0 and "ZAB_TAG=v0.2.9" in out  # a wheel's own version names it
