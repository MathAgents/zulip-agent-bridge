"""A10b, `!upgrade-server` in-band (docs/ADMIN.md §1.6, §7; issue #569): A10's cases, through
the global admin bot and the server's root helpers, with the rig's test-only signing key.

It runs after A10 (this file sorts after `test_scenarios_1_server.py`), on the release A10 deployed
from its public commit. The test release repository (`harness/upgrade.py`,
`ZAB_ADMIN_E2E_RELEASE_REPO`) plays the public one, `release_repo` (#958): it gets each case's tag
from the rig's public repository, an exported commit on its `main`, which descends from the running
release's public one (`Releases.export`), and the server fetches it there with its own deploy key,
made on it and added read-only (BOOTSTRAP §13.4). The running release's tag there names the public
commit `deploys.jsonl` records, as v0.2.19's will after its last hand deploy. The refusals are the
plan helper's, each named by its reason, and after each the server still runs what it ran: an
unsigned tag; a tag signed by an unknown key; a ref that points at another tag's object; an older
release; the word `downgrade`, which the bot refuses with the way back (`deploy.sh --downgrade`),
since the in-band upgrade only goes forward; a `sha256=` that isn't the deployer's review's. The
good rc is then planned with the SHA-256 the deployer's own review prints (the running release's
`deploy.sh --review`, since it), the same in the public repository as in the private one, approved
by one deployer and applied: the server runs it, `!version` shows it once the bots have restarted
into it, the deploy record and its copy in the audit log name the plan and the SHA-256, and its
fleet wheel is placed for `!rollout`, the same bytes as the laptop's `--wheel` builds (hatchling's
builds reproduce: DECISIONS, 2026-10-17), with the SHA-256 the result shows. At the end, once no
apply runs, the server's `admin.toml` is the rig's again, as A16 leaves it, and a good tag the
server took but doesn't run is dropped.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from e2e.admin.conftest import DONE
from e2e.admin.harness import upgrade
from e2e.admin.harness.chat import Refused, whole
from e2e.admin.harness.release import Releases, next_version, older_version
from e2e.admin.test_scenarios_2_bot import ready, restart, talk

DEPLOYS = "/opt/zab-admin/deploys.jsonl"
AUDIT = "/var/lib/zab-admin/audit.jsonl"
#: The plan helper's copy of the tags it took, which a failed run may leave holding `good`.
TAKEN = "/var/lib/zab-admin-upgrade/repo.git"
#: The apply helper's unit, its own 1500 s and more: the bots never restart while it runs.
APPLY_UNIT, APPLY_UNIT_S = "zab-admin-upgrade.service", 1600.0
WHEELS = "/var/lib/zab-admin/releases/wheels"
#: A plan's dry run: the fetch and the review, within `upgrade_server.py`'s 1260 s, and its post
PLAN_S = 1400.0


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A10b")
async def test_upgrade_server_in_band(scenario, admin_rig, rigged, server, tester, tmp_path, nonce):
    repo, token = upgrade.release_repo(), upgrade.release_token()
    if not (repo and token):
        pytest.skip(upgrade.NO_REPO)
    rel = Releases(admin_rig)
    rel.ensure_repo()
    running = server.running()
    assert running and rel.has_public_tag(running), (
        f"the rig's public repository has no tag of the running {running}: A10 deploys it from"
        " its public commit (#958), and A10b runs after A10"
    )
    good = next_version(rel.base_version(rigged.commit), running)
    admins = talk(tester, rigged, "global", f"e2e-A10b-{nonce}")
    rig_toml = (admin_rig.etc / "admin.toml").read_text(encoding="utf-8")
    upgrade.deploy_key(server, repo, token)
    upgrade.push(rel.public, repo, token, f"refs/tags/{running}")  # the review's base
    server.install_etc("admin.toml", upgrade.with_release_repo(rig_toml, repo))
    try:
        await restart(server, admins)  # the bots read release_repo at their start
        await _cases(scenario, rel, admin_rig, rigged, server, tester, admins, (repo, token),
                     running, good, tmp_path)  # fmt: skip
    finally:
        # review of #902, N4: an apply still running (an approval that timed out) ends first; a
        # bot restarted before its switch would run on in the old release
        for _ in range(int(APPLY_UNIT_S / 10)):
            state = server.run(["systemctl", "is-active", APPLY_UNIT], check=False).stdout.strip()
            if state not in ("active", "activating", "deactivating"):
                break
            await asyncio.sleep(10)
        # review of #902, N3: a run that failed after the plan took `good` leaves the server's
        # copy of it, which the next run's tag of that name, at another commit, would meet as a
        # moved tag; the server's own release keeps its own
        if server.running() != good:
            server.run(["sudo", "git", "-C", TAKEN, "tag", "-d", good], check=False)
        server.install_etc("admin.toml", rig_toml)
        server.bot("restart")
        await ready(admins)


async def _cases(scenario, rel, admin_rig, rigged, server, tester, admins, where, running, good,
                 tmp_path) -> None:  # fmt: skip
    repo, token = where  # the test release repository, and its own token

    async def refused(version: str, why: str, case: str, sha256: str = "0" * 64, more: str = ""):
        with pytest.raises(Refused) as caught:
            await admins.request(f"!upgrade-server {version} sha256={sha256}{more}", within=PLAN_S)
        said = await whole(tester, caught.value.message)
        assert re.search(why, said), f"{case}: refused for another reason: {said[:600]}"
        assert server.running() == running, f"{case}: the server changed release"
        scenario.notes.append(f"{case}: refused ({why})")

    def pushed(version: str, signer: str | None = "signing") -> tuple[str, str]:
        """The private test release, signed, and its public commit, tagged with `signer`'s key
        and pushed to the test release repository (#958). Both commits."""
        private = rel.release(rigged.commit, version)
        public = rel.export(version, private, signer)
        upgrade.push(rel.public, repo, token, f"refs/tags/{version}")
        return private, public

    pushed(good, None)
    await refused(good, r"not signed with an SSH key", "unsigned tag")
    pushed(good, "unknown")
    await refused(good, r"not signed by a key in", "a tag signed by an unknown key")
    private, commit = pushed(good)  # signed: the good one, applied last
    renamed = next_version(rel.base_version(rigged.commit), good)
    rel.public_git(
        "update-ref", f"refs/tags/{renamed}", rel.public_git("rev-parse", f"refs/tags/{good}")
    )
    try:
        upgrade.push(rel.public, repo, token, f"refs/tags/{renamed}")
        await refused(
            renamed, r"holds a tag object named otherwise", f"refs/tags/{renamed} → {good}"
        )
    finally:
        rel.public_git("update-ref", "-d", f"refs/tags/{renamed}")
        upgrade.push(rel.public, repo, token, f":refs/tags/{renamed}")
    older = older_version(running)  # refused by its name, before any fetch: no tag needed
    forward = re.escape("only goes forward: go back with")
    await refused(
        older, rf"{re.escape(older)} is older than {re.escape(running)}.*{forward}", "older"
    )
    # no in-band downgrades (Drew, #569): the bot refuses the word itself, with the way back
    back = re.escape("`!upgrade-server` only goes forward: go back with `deploy.sh --downgrade`")
    await refused(good, back, "the word downgrade", more=" downgrade")

    # the deployer's own review: the running release's deploy.sh, since it (BOOTSTRAP §13.4),
    # of the public commit in the public repository, and the same of the private one (#958)
    script = tmp_path / "deploy.sh"
    script.write_text(rel.public_git("show", f"{running}:workshop/adminserver/deploy.sh") + "\n")
    sha = upgrade.reviewed_sha256(script, rel.public, admin_rig.etc, commit, running, tmp_path)
    same = upgrade.reviewed_sha256(script, admin_rig.repo, admin_rig.etc, private, running,
                                   tmp_path)  # fmt: skip
    assert same == sha, "the private review's SHA-256 isn't the public one's"
    await refused(good, r"not the sha256= you gave", "a SHA-256 not the review's", "1" * 64)

    plan = await admins.request(f"!upgrade-server {good} sha256={sha}", within=PLAN_S)
    assert plan.phrase == "", f"{plan.id} asks for a phrase: one deployer's approval, no more"
    assert f"SHA-256 {sha}; release tree" in plan.text, plan.text[:600]
    assert f"release {good}: tag " in plan.text and f"base, running {running}: tag " in plan.text
    _, text = await admins.approve(plan)
    assert text.startswith(f"✅ **{plan.id}**"), text[:600]
    shown_sha = re.search(rf"{re.escape(good)} is installed and current, with its fleet wheel "
                          r"\(SHA-256 ([0-9a-f]{64})\)", text)  # fmt: skip
    assert shown_sha, text[:600]
    assert server.running() == good
    DONE["release"] = good  # what the server runs from now on

    shown = rf"^zab {re.escape(good)}\+{commit[:12]} · release `rc`"
    for _ in range(18):  # the bots restart into the new release after the result
        try:
            await admins.ask("!version", shown, within=10)
            break
        except TimeoutError:
            continue
    else:
        active = server.bot("is-active").stdout.strip() or "absent"
        pytest.fail(f"!version never showed {good}+{commit[:12]} (zab-admin-bot.service: {active})")

    last = json.loads(server.read(DEPLOYS).splitlines()[-1])
    assert (last["event"], last["version"], last["commit"]) == ("upgrade", good, commit), last
    assert (last.get("plan"), last.get("diff_sha256"), last.get("base")) == (plan.id, sha, running)
    record = None  # review of #902, B4: the bot copies the deploy record into the audit log
    for _ in range(12):
        lines = server.read(AUDIT).splitlines()
        found = [json.loads(ln) for ln in lines if '"upgrade"' in ln]
        found = [r for r in found if r.get("deploy", {}).get("version") == good]
        if found:
            record = found[-1]["deploy"]
            break
        await asyncio.sleep(5)
    assert record is not None, f"no upgrade record of {good} in the audit log"
    assert (record.get("plan"), record.get("diff_sha256")) == (plan.id, sha), record
    assert record.get("commit") == commit, record
    wheel = f"zulip_agent_bridge-{good[1:]}-py3-none-any.whl"
    assert server.run(["sudo", "test", "-f", f"{WHEELS}/{wheel}"], check=False).ok, f"no {wheel}"
    # the server's build is the laptop's, byte for byte: `--wheel` builds it here and finds the
    # server's the same (`place_wheel`'s `cmp`), or refuses it as "there with other content"
    script = tmp_path / "deploy-good.sh"
    script.write_text(rel.public_git("show", f"{commit}:workshop/adminserver/deploy.sh") + "\n")
    placed = rel.deploy(script, good, "--wheel", out=tmp_path, repo=rel.public)
    assert placed.ok, f"the laptop's {wheel} is not the server's: {placed.why()}"
    same = f"{wheel} is already there: SHA-256 {shown_sha[1]}."  # the result's, too
    assert same in placed.result.stderr, placed.result.stderr[-600:]
    scenario.notes.append(
        f"{good} upgraded in-band by {plan.id}: the bots restarted into it, and its wheel is "
        "placed, the same bytes as the laptop's build"
    )
