"""A10, the deploy gate (docs/ADMIN.md §1.6, §7; issue #356): deploy.sh from the development machine
against the test server, as a deployer runs it, with the rig's test-only signing key.

The four refusals are deploy.sh's own (its laptop half refuses them before anything reaches the
server, and the server half repeats the checks), each named by its reason; after each, the
server still runs what it ran. The good rc then deploys as a last hand deploy does when
`release_repo` is the public repository (#958, BOOTSTRAP §13.4): reviewed in the rig's own
repository, which plays the private one, since the running release; exported to the rig's
public repository (`Releases.export`); and deployed from that public commit, from a clone of
the public repository holding the running release's private tag alone, with
`--expect-diff-sha256` of the private review, which holds the deploy to the release paths
reviewed. `!version` shows the public commit, the audit log has its `upgrade` record with that
commit and the reviewed diff's SHA-256, and its venv holds exactly the hashed wheels of
`uv.lock`'s admin group, made by uv, with zab itself on a `.pth` and nothing built. A10b's
in-band upgrades then start from that public release.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import pytest

from e2e.admin.conftest import DONE
from e2e.admin.harness import upgrade
from e2e.admin.harness.chat import Talk
from e2e.admin.harness.release import Releases, next_version, older_version
from e2e.admin.harness.rig import ADMINS_CHANNEL, GLOBAL_BOT

AUDIT = "/var/lib/zab-admin/audit.jsonl"
#: As root on the server: the release's venv, as §1.6 says it is built. Prints JSON. `$2` is
#: the root for the offline test (default /opt/zab-admin).
VENV_CHECK = r"""
import json, pathlib, re, sys
opt = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "/opt/zab-admin")
rel = opt / "releases" / sys.argv[1]
out = {"current": str((opt / "current").resolve()) == str(rel)}
text = (rel / ".requirements.txt").read_text().replace("\\\n", " ")
lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
reqs = [ln.split()[0] for ln in lines]
out["requirements"] = len(reqs)
out["unhashed"] = [ln.split()[0] for ln in lines if "--hash=sha256:" not in ln]
norm = lambda n: re.sub(r"[-_.]+", "-", n).lower()
wanted = {norm(r.split("==")[0]) for r in reqs}
site = next((rel / ".venv" / "lib").glob("python3*/site-packages"))
infos = list(site.glob("*.dist-info"))
# `httpx-0.28.1.dist-info` is httpx: the suffix goes before the version (Codex round 3)
names = {norm(i.name.removesuffix(".dist-info").rsplit("-", 1)[0]) for i in infos}
out["installed_not_locked"] = sorted(names - wanted)
out["not_uv"] = sorted(i.name for i in infos if (i / "INSTALLER").read_text().strip() != "uv")
out["pth"] = (site / "zab-release.pth").read_text().strip() == str(rel / "src")
built = ("zulip-agent-bridge", "hatchling")
out["built"] = sorted(i.name for i in infos if norm(i.name).startswith(built))
out["by_uv"] = "uv = " in (rel / ".venv" / "pyvenv.cfg").read_text()
out["cache_left"] = (rel / ".uv-cache").exists()
out["root_owned"] = rel.stat().st_uid == 0 and (rel / ".venv").stat().st_uid == 0
print(json.dumps(out))
"""


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A10")
async def test_deploy_gate(scenario, admin_rig, rigged, server, tester, tmp_path, nonce):
    rel = Releases(admin_rig)
    script = rel.extract(rigged.commit, "workshop/adminserver/deploy.sh", tmp_path / "deploy.sh")
    log = Path(os.environ["ZAB_ADMIN_E2E_STATE"]).parent / "a10-deploy.log"  # with the report
    running = server.running()
    assert running == rigged.release, f"the server runs {running}, the setup deployed another"
    good = next_version(rel.base_version(rigged.commit), running)

    def refused(version: str, why: str, case: str) -> None:
        done = rel.deploy(script, version, out=tmp_path, log=log)
        assert not done.ok, f"{case}: deploy.sh accepted {version}"
        assert re.search(why, done.why()), f"{case}: refused for another reason: {done.why()}"
        assert server.running() == running, f"{case}: the server changed release"
        scenario.notes.append(f"{case}: refused ({done.why().removeprefix('deploy.sh: ')})")

    rel.release(rigged.commit, good, signer=None)
    refused(good, r"not signed with an SSH key", "unsigned tag")
    rel.release(rigged.commit, good, signer="unknown")
    refused(good, r"not signed by a key in", "a tag signed by an unknown key")
    private = rel.release(rigged.commit, good)  # signed: the good one, reviewed, then exported
    renamed = next_version(rel.base_version(rigged.commit), good)
    rel.alias(renamed, rel.git("rev-parse", f"refs/tags/{good}"))
    try:
        refused(renamed, r"holds a tag object named otherwise", f"refs/tags/{renamed} → {good}")
    finally:
        rel.git("update-ref", "-d", f"refs/tags/{renamed}")
    older = older_version(running)
    rel.release(rigged.commit, older)
    refused(older, rf"{re.escape(older)} is older than {re.escape(running)}", "older, no flag")

    # #958: reviewed in the private repository, since the running release, with its deploy.sh;
    # deployed from the public commit, whose diff from the private base must be the review's
    reviewed = upgrade.reviewed_sha256(script, admin_rig.repo, admin_rig.etc, private, running,
                                       tmp_path)  # fmt: skip
    commit = rel.export(good, private)
    clone = rel.deploy_clone(running, tmp_path / "deploy-clone")
    started = time.monotonic()
    done = rel.deploy(script, good, "--expect-diff-sha256", reviewed, out=tmp_path, log=log,
                      repo=clone)  # fmt: skip
    assert done.ok, f"the signed rc {good}'s public commit was refused: {done.why()}"
    scenario.timing("deploy of the good rc", time.monotonic() - started)
    assert server.running() == good
    DONE["release"] = good  # what the server runs from now on
    assert done.diff_sha256 == reviewed, "the public commit's diff isn't the private review's"
    assert "It is the diff you reviewed (--expect-diff-sha256)" in done.result.stderr
    scenario.notes.append(
        f"{good} deployed from its public commit {commit[:12]}, reviewed as {private[:12]} in the "
        "private repository: the same diff SHA-256 (#958)"
    )

    ids = rigged.ids()
    admins = Talk(tester, ids.bots[GLOBAL_BOT][0], ids.channels[ADMINS_CHANNEL], f"e2e-A10-{nonce}")
    shown = rf"^zab {re.escape(good)}\+{commit[:12]} · release `rc`"
    reply = None
    for _ in range(9):  # the bots restart with the deploy: a message before their queue is lost
        try:
            reply = await admins.ask("!version", shown, within=10)
            break
        except TimeoutError:
            await asyncio.sleep(5)
    if reply is None:
        active = server.bot("is-active").stdout.strip() or "absent"
        pytest.fail(f"!version never showed {good}+{commit[:12]} (zab-admin-bot.service: {active})")

    record = None
    for _ in range(12):
        lines = server.read(AUDIT).splitlines()
        found = [json.loads(ln) for ln in lines if '"upgrade"' in ln]
        found = [r for r in found if r.get("deploy", {}).get("version") == good]
        if found:
            record = found[-1]
            break
        await asyncio.sleep(5)
    assert record is not None, f"no upgrade record of {good} in the audit log"
    assert record["deploy"].get("diff_sha256") == done.diff_sha256, "another diff's SHA-256"
    assert record["deploy"].get("commit") == commit

    venv = json.loads(server.run(["sudo", "python3", "-I", "-c", VENV_CHECK, good]).stdout)
    assert venv["current"] and venv["pth"] and venv["by_uv"] and venv["root_owned"], venv
    assert venv["requirements"] > 0 and not venv["unhashed"], venv
    assert not (venv["installed_not_locked"] or venv["not_uv"] or venv["built"]), venv
    assert not venv["cache_left"], venv
    scenario.notes.append(
        f"{good} deployed: {venv['requirements']} hashed wheels, nothing built; the venv is "
        "root's after the build (§1.6), so `zab-build` is deploy.sh's code path, not observed"
    )
