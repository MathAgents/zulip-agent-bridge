"""A10's part with a plan applying when a deploy starts (docs/ADMIN.md §2.4 step 6, §7; #989).

After the bot scenarios, on A4's VMs (it needs only `launched`): `!protect on zab-test-a`, a
no-op since A4 protected it, on `Workshop admin · e2e`, approved, and its `protect.py` stopped
(SIGSTOP, as root) as soon as it starts: the plan applies, and goes on applying, for as long as
the test holds it. Then deploy.sh, as a deployer runs it, with `--plan-wait 0`, deploys the next
rc of the commit under test: once the release is built, its server half finds the plan applying
and refuses, naming it with its bot; the server still runs what it ran, and the refused
release's directory is gone. The script is then continued: the plan ends with its own result,
never `⏹` (interrupted).

A plan pending when the deploy starts isn't part of it: the rig's plans expire after 2 minutes
until A9 (`plan_ttl_minutes`), less than a deploy takes up to its check, and the deploy rig's
tests name one (`tests/workshop/test_deploy_plans.py`; review of #1034, B3).

**The apply's deadline.** `!protect`'s apply has 15 minutes (ADMIN §2.4 step 5): everything up
to the deploy's check, its fetch, the venv's build and the self-checks, must finish within them.
Past them, the runner kills the stopped script, the plan ends `⏱`, and a deploy that only then
reaches its check finds nothing applying and goes through: a failure here that names a deploy
which went through, or a `⏱` result, may mean that the deploy took longer than that.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

from e2e.admin.conftest import DONE, requires
from e2e.admin.harness import upgrade
from e2e.admin.harness.chat import apply_s
from e2e.admin.harness.release import Releases, next_version
from e2e.admin.harness.rig import E2E
from e2e.admin.test_scenarios_2_bot import A, talk

#: As root on the server: the first process of `zab-admin` running `$1` without `--dry-run`,
#: stopped (SIGSTOP) as soon as it shows, within `$2` seconds; its PID on stdout. Started after
#: the plan's dry run ended, so the one it finds is the apply's.
HOLD = r"""set -eu
want=$1 until=$((SECONDS + $2))
while [ "$SECONDS" -lt "$until" ]; do
  for pid in $(pgrep -u zab-admin -f -- "$want" || true); do
    tr '\0' '\n' <"/proc/$pid/cmdline" 2>/dev/null | grep -qx -- --dry-run && continue
    kill -STOP "$pid" && { echo "$pid"; exit 0; }
  done
  sleep 0.05
done
echo "no apply of $want began within $2 s" >&2
exit 1
"""
SCRIPT = "workshop/aws/protect.py"
RELEASES = "/opt/zab-admin/releases"


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A10")
@requires("launched")
async def test_a10_a_deploy_refuses_while_a_plan_applies(
    scenario, admin_rig, rigged, server, tester, tmp_path, nonce
):
    t = talk(tester, rigged, E2E.name, f"e2e-A10-plans-{nonce}")
    rel = Releases(admin_rig)
    running = server.running()
    assert running, "the server runs no release"
    version = next_version(rel.base_version(rigged.commit), running)
    private = rel.release(rigged.commit, version)
    script = rel.extract(rigged.commit, "workshop/adminserver/deploy.sh", tmp_path / "deploy.sh")
    reviewed = upgrade.reviewed_sha256(script, admin_rig.repo, admin_rig.etc, private, running,
                                       tmp_path)  # fmt: skip
    log = Path(os.environ["ZAB_ADMIN_E2E_STATE"]).parent / "a10-plans-deploy.log"

    held = await t.request(f"!protect on {A}")
    watch = asyncio.create_task(
        asyncio.to_thread(server.script, HOLD, SCRIPT, "180", sudo=True, timeout=240, check=False)
    )
    sent = await t.say(held.approve)
    answer = await t.reply(sent, rf"^(✅ {held.id} |❌|busy)")
    assert answer["content"].startswith(f"✅ {held.id} "), answer["content"][:300]
    stopped = await watch
    pid = stopped.stdout.strip()
    assert stopped.ok and pid.isdigit(), f"{held.id}'s apply never showed: {stopped.stderr[-300:]}"
    try:  # within the apply's 15 minutes (module doc)
        done = await asyncio.to_thread(
            rel.deploy, script, version, "--plan-wait", "0", "--expect-diff-sha256", reviewed,
            out=tmp_path, log=log,
        )  # fmt: skip
        if done.ok:  # the later parts' release, before the failure
            DONE["release"] = version
        assert not done.ok, f"{version} deployed while {held.id} applied (past its deadline?)"
        assert "nothing was deployed" in done.why(), done.why()
        bot = rf"on {re.escape(E2E.name)}'s bot \(!protect\) for \d+ min"
        said = done.result.stderr
        assert re.search(rf"  {held.id}, applying {bot}: the deploy refuses until it", said), said
        assert server.running() == running, "the server changed release"
        left = server.run(["test", "-e", f"{RELEASES}/{version}"], check=False)
        assert not left.ok, f"{RELEASES}/{version} is left after the refusal"
    finally:
        server.run(["sudo", "kill", "-CONT", pid], check=False)
    ended = rf"^((✅|❌|⏱|⏹) \*\*{held.id}\*\* · |(❌|⏹) {held.id}[ :])"
    result = await t.reply(sent, ended, within=apply_s(held))
    assert result["content"].startswith(f"✅ **{held.id}**"), result["content"][:300]
    scenario.notes.append(
        f"deploy.sh {version} --plan-wait 0, while {held.id} applied (its protect.py stopped): "
        f"refused after the build, naming {held.id} with its bot, its release directory gone; "
        f"{held.id} then ended ✅, not interrupted"
    )
