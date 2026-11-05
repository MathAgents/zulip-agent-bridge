"""A19, the export re-pack's sandbox on the rig's server (docs/ADMIN.md §1.2, §7; #374, #356).

Right after the setup's deploy, before any bot scenario, and without Q9: no VM, no repository,
no Zulip. Crafted archives go straight to the checker's socket through the real client, as
`zab-admin` and as two users it must refuse; a held connection shows its instance's user and
view; the journal shows one instance per connection; the reviewer's probe of #384 runs under
PID 1 in a copy of the installed units. The steps and their checks are `harness/sandbox.py`'s;
nothing is left on the server (the archives and the re-packs have no name, the copy is removed).
"""

from __future__ import annotations

import pytest

from e2e.admin.conftest import DONE
from e2e.admin.harness import settle
from e2e.admin.harness.sandbox import (
    MACHINE,
    PROBED,
    Sandbox,
    cases,
    corpus,
    inspected,
    journaled,
    judged,
    probe,
)


@pytest.fixture
def box(rigged, server) -> Sandbox:
    """The server, checked to run the release under test (A10's, once it deployed one)."""
    sandbox = Sandbox(server)
    sandbox.machine = sandbox.ready(DONE.get("release", rigged.release))
    return sandbox


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A19")
async def test_the_checker_repacks_refuses_and_answers_only_its_peers(scenario, box, tmp_path):
    since = box.now() - 2
    sent, problems = [], []
    for case in cases():
        got = box.send(case)
        sent.append((case, got))
        found = judged(case, got, tmp_path)
        problems += found
        said = got["problem"] or f"ok: {got['note']}"
        scenario.notes.append(f"{case.name} (as {case.user}): {'FAILED: ' * bool(found)}{said}")
    try:  # journald may show an instance's end a moment after the client has its answer
        await settle.until(
            lambda: box.journal(since), lambda units: not journaled(sent, units),
            what="one instance per connection in the journal", within=60, every=3,
        )  # fmt: skip
    except AssertionError:
        pass  # said below, by what is missing
    units = box.journal(since)
    problems += journaled(sent, units)
    scenario.notes.append(
        f"journal: {len(units)} instance(s) of zab-archive-check@ since the start"
    )
    left = box.left_in_tmp(since)
    problems += [f"left in the server's tmp: {left}"] * bool(left)
    assert not problems, "\n".join(problems)


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A19")
def test_the_instance_runs_as_its_own_dynamic_user_without_the_keys(scenario, box):
    facts = box.inspect()
    problems = inspected(facts)
    show = facts.get("show", {})
    if not facts.get("started"):  # how it ended instead (226/NAMESPACE: its sandbox's setup)
        scenario.notes.append(f"held connection: {problems[0]}")
    else:
        scenario.notes.append(
            f"held connection: {show.get('Id')} as {show.get('User')} (uid {show.get('UID')}, "
            f"DynamicUser={show.get('DynamicUser')}); as it sees them, {facts.get('inside')} "
            f"entries; on the server, {facts.get('outside')}"
        )
    assert not problems, "\n".join(problems)


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A19")
def test_the_kernel_refuses_io_uring_sockets_and_writes(scenario, box):
    assert box.machine == MACHINE, f"the row's probe runs on arm64, not {box.machine!r}"
    t = corpus()
    since = box.now() - 2
    got, left = box.probe(probe(), t.tar_of(t.file_("work/a.md", b"alpha")))
    scenario.notes.append(f"the probe under PID 1 on {box.machine}: {got['problem']}")
    assert not left, f"the copy of the units is still there: {left}"
    if got["problem"] != f"{PROBED}: deleted":
        ended = {u: m[-3:] for u, m in box.journal(since, unit=box.copy).items()}
        pytest.fail(
            "the probe's answer (AF_UNIX, AF_INET, fork, /tmp, /var/tmp, /dev/shm, io_uring, "
            f"/run/dbus): {got['problem'] or got['note']}; its instance in the journal: {ended}"
        )
