"""A10's check of a release's venv (`VENV_CHECK`), offline on a tree laid out as deploy.sh's
build leaves it (docs/ADMIN.md §1.6; issue #356; Codex round 3 on #363)."""

from __future__ import annotations

import json
import subprocess
import sys

from e2e.admin.test_scenarios_1_server import VENV_CHECK

HASH = "--hash=sha256:" + "0" * 64


def release(root, *, extra=(), installer="uv"):
    rel = root / "releases" / "v0.2.4rc1"
    site = rel / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (rel / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\nuv = 0.11.24\n")
    (site / "zab-release.pth").write_text(f"{rel / 'src'}\n")
    (rel / ".requirements.txt").write_text(
        f"boto3==1.35.0 \\\n    {HASH}\nhttpx==0.28.1 ; python_version >= '3.11' \\\n    {HASH}\n"
        "    # via zulip-agent-bridge\n"
    )
    for name in ("boto3-1.35.0", "httpx-0.28.1", "typing_extensions-4.12.2", *extra):
        info = site / f"{name}.dist-info"
        info.mkdir()
        (info / "INSTALLER").write_text(installer + "\n")
    (rel / ".requirements.txt").write_text(
        (rel / ".requirements.txt").read_text() + f"typing-extensions==4.12.2 \\\n    {HASH}\n"
    )
    (root / "current").symlink_to(rel)
    done = subprocess.run(  # noqa: S603 - an argument list
        [sys.executable, "-I", "-c", VENV_CHECK, "v0.2.4rc1", str(root)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(done.stdout)


def test_a_release_built_as_deploy_sh_builds_it_passes(tmp_path):
    out = release(tmp_path)
    assert out["current"] and out["pth"] and out["by_uv"]
    assert out["requirements"] == 3 and out["unhashed"] == []
    assert out["installed_not_locked"] == [] and out["not_uv"] == [] and out["built"] == []
    assert not out["cache_left"]


def test_what_a_built_or_unlocked_install_leaves_shows(tmp_path):
    out = release(tmp_path, extra=("zulip_agent_bridge-0.2.4rc1", "requests-2.32.3"))
    assert out["installed_not_locked"] == ["requests", "zulip-agent-bridge"]
    assert out["built"] == ["zulip_agent_bridge-0.2.4rc1.dist-info"]
