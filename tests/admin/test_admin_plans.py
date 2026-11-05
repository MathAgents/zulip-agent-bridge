"""Plans in `state.db`: IDs, binding to the bot, TTL, staleness and restarts (ADMIN.md §2.4)."""

import dataclasses
import hashlib
import sqlite3
from pathlib import Path

import pytest

from zab.admin.actions import Layout, Live, plan_hash
from zab.admin.approvals import Approvals, Received, Sender
from zab.admin.plans import (
    APPLYING,
    APPROVED,
    DONE,
    EXPIRED,
    FAILED,
    INTERRUPTED,
    PENDING,
    PLANNING,
    REFUSED,
    STALE,
    PlanError,
    PlanStore,
    Requirement,
    fingerprint,
)

from .conftest import CONFIG, FLEET, HARVARD, HASH, OTHER, direct, make, posted

TTL = CONFIG.plan_ttl_minutes * 60
IDS = {"plan_message": 7, "approval_message": 8}


def reopen(store: PlanStore, tmp_path, floor: int = 0) -> PlanStore:
    store.close()
    return PlanStore(tmp_path / "state.db", ttl_minutes=10, floor=floor, clock=store.clock)


def begin(store: PlanStore, command: str = "!stop alice"):
    return store.begin(workshop="harvard-2026", bot=901, requester=103, command=command)


def test_plan_ids_never_repeat_on_the_server(store, tmp_path):
    """Unique per server, across workshops and restarts, and even after a row is deleted
    (AUTOINCREMENT, not the reuse of the largest rowid); `floor` lifts a rebuilt state.db."""
    assert [begin(store).id for _ in range(3)] == [1, 2, 3]
    other = store.begin(workshop="zab-test", bot=902, requester=105, command="!stop erin")
    assert other.name == "P-4"
    with store.db:
        store.db.execute("DELETE FROM plans WHERE id = 4")
    store = reopen(store, tmp_path)
    assert begin(store).id == 5
    with store.db:
        store.db.execute("DELETE FROM plans WHERE id = 5")  # P-3 is now the newest row
    store = reopen(store, tmp_path, floor=3)  # a floor below the sequence changes nothing
    assert begin(store).id == 6
    store = reopen(store, tmp_path, floor=1200)
    assert begin(store).id == 1201
    store.close()


def test_a_rebuilt_state_db_never_reissues_an_id(store, tmp_path):
    """state.db is not backed up (§1.7): rebuilt empty, it looks like a new server's, so the
    floor, the highest ID in the audit log or in runs/P-<n>/, is required at every start."""
    assert [begin(store).id for _ in range(3)] == [1, 2, 3]
    store.close()
    (tmp_path / "state.db").unlink()  # lost
    rebuilt = PlanStore(tmp_path / "state.db", ttl_minutes=10, floor=3)
    assert begin(rebuilt).id == 4
    rebuilt.close()
    with pytest.raises(TypeError, match="floor"):
        PlanStore(tmp_path / "other.db", ttl_minutes=10)  # type: ignore[call-arg]


def test_one_process_owns_the_store(store, tmp_path):
    """A second opener (a second bot, a script calling recover()) fails; nothing changes."""
    plan = begin(store)
    with pytest.raises(PlanError, match="open in another process"):
        PlanStore(tmp_path / "state.db", ttl_minutes=10, floor=0)
    assert store.get(plan.id) == plan
    store.close()
    store.close()  # closing twice is harmless
    again = PlanStore(tmp_path / "state.db", ttl_minutes=10, floor=0)
    assert again.get(plan.id) == plan
    again.close()


@pytest.mark.parametrize("broken", ["a directory", "not a database"])
def test_a_store_that_fails_to_open_releases_its_lock(tmp_path, broken):
    """The connection fails (state.db is a directory), or the setup after it does (state.db
    is not SQLite): the error is raised, and the lock is free for the next opener."""
    path = tmp_path / "state.db"
    if broken == "a directory":
        path.mkdir()
    else:
        path.write_bytes(b"not a database, not even a header" * 4)
    with pytest.raises(sqlite3.DatabaseError):
        PlanStore(path, ttl_minutes=10, floor=0)
    path.rmdir() if path.is_dir() else path.unlink()
    store = PlanStore(path, ttl_minutes=10, floor=0)  # not "open in another process"
    assert begin(store).id == 1
    store.close()


def test_state_survives_a_restart(store, tmp_path):
    action = direct("teardown", ("alice", "bob"), ("alice", "bob"))
    plan = posted(store, action)
    approvals = Approvals(store, CONFIG)
    first = Received(10**6, 901, 42, Sender(104, False, True))
    assert approvals.approve(first, plan.id, HASH[:6], ("teardown", "2")).code == "recorded"
    store = reopen(store, tmp_path)
    assert store.get(plan.id) == plan
    assert plan.requirement == Requirement("destructive", 2, ("teardown", "2"))
    assert plan.targets == ("alice", "bob")
    assert (plan.state, plan.hash, plan.plan_message, plan.approval_message) == (
        PENDING,
        HASH,
        100,
        101,
    )
    approvals = Approvals(store, CONFIG)
    assert approvals.approved_by(plan.id) == (104,)
    second = Received(10**6 + 1, 901, 42, Sender(101, False, True))
    assert approvals.approve(second, plan.id, HASH[:6], ("teardown", "2")).code == "approved"
    store.close()


def test_post_binds_the_plan_to_its_bot_and_its_command(store):
    """A plan is the bot's that took the request (§2.5): an action of another workshop, another
    bot or another command, a malformed hash, or messages out of order are refused."""
    action = make("!stop alice")
    needs = Requirement("change")
    ids = {"plan_message": 7, "approval_message": 8}
    more = (*action.inputs, "/var/lib/zab-admin/tmp/P-2")  # a path that ends in /P-<m>
    zero = (*action.inputs, "/var/lib/zab-admin/runs/P-01/out.txt")  # P-1 is never P-01
    bad = [
        (dataclasses.replace(action, inputs=more), needs, HASH, ids, "another plan's files"),
        (dataclasses.replace(action, inputs=zero), needs, HASH, ids, "another plan's files"),
        (make("!stop erin", OTHER), needs, HASH, ids, "another bot"),
        (dataclasses.replace(action, bot=902), needs, HASH, ids, "another bot"),
        (dataclasses.replace(action, workshop="zab-test"), needs, HASH, ids, "another bot"),
        (make("!stop bob"), needs, HASH, ids, "not P-1's command"),
        (make("!start alice"), needs, HASH, ids, "not P-1's command"),
        (action, needs, HASH.upper(), ids, "full SHA-256"),
        (action, needs, HASH[:-1], ids, "full SHA-256"),
        (action, Requirement("change", 0), HASH, ids, "at least one approver"),
        (action, needs, HASH, {"plan_message": 0, "approval_message": 8}, "messages' IDs"),
        (action, needs, HASH, {"plan_message": 8, "approval_message": 8}, "follows"),
        (action, needs, HASH, {"plan_message": 9, "approval_message": 8}, "follows"),
    ]
    plan = begin(store)
    for act, req, digest, messages, error in bad:
        with pytest.raises(PlanError, match=error):
            store._post(plan.id, act, req, digest, **messages)
        assert store.get(plan.id) == plan
    assert store._post(plan.id, action, needs, HASH, **ids).state == PENDING
    with pytest.raises(PlanError, match="is pending, not planning"):
        store._post(plan.id, action, needs, HASH, **ids)
    with pytest.raises(PlanError, match="no plan P-99"):
        store._post(99, action, needs, HASH, **ids)


def test_a_plan_applies_only_its_own_upload(store, tmp_path):
    """`!roster import`: P-n's upload is runs/P-n/roster-upload.csv, an input of its hash, so
    approving P-1 never applies P-2's upload (review of #333)."""
    layout = Layout(runs=str(tmp_path / "runs"), retired=str(tmp_path / "retired-slugs.txt"))
    message, needs = "!roster import [r.csv](/user_uploads/2/a/r.csv)", Requirement("change")
    upload = [f"{layout.runs}/P-{n}/roster-upload.csv" for n in (0, 1, 2)]
    one, two = begin(store, "!roster import"), begin(store, "!roster import")
    mine, theirs = (make(message, layout=layout, upload=upload[n]) for n in (1, 2))
    for plan, action in ((one, theirs), (two, mine)):
        with pytest.raises(PlanError, match="another plan's files"):
            store._post(plan.id, action, needs, HASH, **IDS)
    bare = tuple(p for p in mine.inputs if p != upload[1])
    elsewhere = (f"{tmp_path}/uploads/roster-upload.csv", f"{tmp_path}/x/P-1/roster-upload.csv")
    for inputs in (bare, *((*bare, path) for path in elsewhere)):  # not runs/P-1/'s own
        with pytest.raises(PlanError, match=r"upload is runs/P-1/roster-upload\.csv"):
            store._post(one.id, dataclasses.replace(mine, inputs=inputs), needs, HASH, **IDS)
    assert store._post(one.id, mine, needs, HASH, **IDS).state == PENDING
    assert store._post(two.id, theirs, needs, HASH, **IDS).state == PENDING
    live = Live()
    Path(upload[1]).parent.mkdir(parents=True)
    Path(upload[1]).write_text("slug,email\nalice,a@example.org\n")
    before = fingerprint(mine, release="v0.3.0", live=live)
    Path(upload[1]).write_text("slug,email\nalice,b@example.org\n")
    assert fingerprint(mine, release="v0.3.0", live=live) != before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"workshop": "harvard-2026", "bot": 0, "requester": 103, "command": "!stop all"},
        {"workshop": "harvard-2026", "bot": 901, "requester": True, "command": "!stop all"},
        {"workshop": "harvard-2026", "bot": 901, "requester": 103, "command": "stop all"},
    ],
)
def test_a_plan_names_its_bot_requester_and_command(store, kwargs):
    with pytest.raises(PlanError, match="its bot, its requester and its command"):
        store.begin(**kwargs)


def test_the_ttl_runs_from_the_post(store, clock):
    plan = begin(store)
    clock.now += 300  # planning takes time; the TTL starts at the post
    plan = store._post(plan.id, make("!stop alice"), Requirement("change"), HASH, **IDS)
    assert plan.expires == clock.now + TTL
    clock.now += TTL - 1
    assert store.expire_due() == []
    assert [p.name for p in store.pending(901)] == ["P-1"]
    clock.now += 1
    assert store.pending(901) == []
    assert [(p.name, p.state) for p in store.expire_due()] == [("P-1", EXPIRED)]
    assert store.expire_due() == []


def test_the_listing_is_the_bots_own(store):
    mine, theirs = (
        posted(store, make("!stop alice")),
        posted(store, make("!stop erin", OTHER), fleet=OTHER),
    )
    begin(store)  # planning: not listed
    assert store.pending(901) == [mine]
    assert store.pending(902) == [theirs]


def approved(store, plan):
    return store.record(plan.id, lambda db: True)


def test_staleness_at_apply(store):
    """The hash recomputed at apply must be the approved one; otherwise STALE (§2.4 step 5)."""
    one, two = posted(store, make("!stop alice")), posted(store, make("!stop bob"))
    with pytest.raises(PlanError, match="is pending, not approved"):
        store.begin_apply(one.id, HASH)
    near = HASH[:-1] + "6"  # all but the last digit match: the full hash is checked
    stale = store.begin_apply(approved(store, one).id, near)
    assert (stale.state, stale.note) == (STALE, f"hash now {near[:12]}")
    with pytest.raises(PlanError, match="is stale"):
        store.begin_apply(one.id, HASH)
    assert store.begin_apply(approved(store, two).id, HASH).state == APPLYING
    assert store.finish(two.id, True).state == DONE
    with pytest.raises(PlanError, match="is done, not applying"):
        store.finish(two.id, False)
    three = approved(store, posted(store, make("!stop carol")))
    store.begin_apply(three.id, HASH)
    assert store.finish(three.id, False).state == FAILED


def test_an_approved_plan_must_begin_within_a_ttl(store, clock):
    """An old approval never applies: the apply begins within one TTL of the last approval
    (review of #339), whatever the clock does in between."""
    one = approved(store, posted(store, make("!stop alice")))
    assert one.approved == clock.now
    clock.now += TTL - 1
    assert store.begin_apply(one.id, HASH).state == APPLYING
    for jump in (TTL, 24 * 3600):
        late = approved(store, posted(store, make("!stop bob")))
        clock.now += jump
        expired = store.begin_apply(late.id, HASH)
        assert (expired.state, expired.note) == (EXPIRED, "not applied within the TTL")
    # approved without its time (not through record): never applied
    bare = store.move(posted(store, make("!stop carol")).id, (PENDING,), APPROVED)
    assert bare.approved is None
    assert store.begin_apply(bare.id, HASH).state == EXPIRED


def test_fingerprint_is_the_plan_hash_of_the_inputs_now(tmp_path):
    """A roster edited between plan and apply changes the fingerprint: the plan goes STALE."""
    roster = tmp_path / "roster.csv"
    workshop = dataclasses.replace(HARVARD, roster=str(roster))
    action = make("!stop alice", dataclasses.replace(FLEET, workshop=workshop))
    live, release = Live(vms={"alice": "i-0a"}), "v0.3.0+a1b2c3d"
    files = dict.fromkeys(action.inputs, "")  # neither the roster nor spares.json exists yet
    before = fingerprint(action, release=release, live=live)
    assert before == plan_hash(action, release=release, digests=files, live=live)
    roster.write_text("slug\nalice\n")
    files[str(roster)] = hashlib.sha256(b"slug\nalice\n").hexdigest()
    after = fingerprint(action, release=release, live=live)
    assert after == plan_hash(action, release=release, digests=files, live=live)
    assert after != before
    assert fingerprint(action, release="v0.3.1+b2c3d4e", live=live) != after
    assert fingerprint(action, release=release, live=Live(vms={"alice": "i-0b"})) != after


def test_a_restart_interrupts_what_was_running(store, tmp_path):
    """Nothing resumes on its own (§2.4 step 6); a pending plan waits for its TTL (§2.1)."""
    planning = begin(store)
    pending = posted(store, make("!stop alice"))
    app = approved(store, posted(store, make("!stop bob")))
    applying = store.begin_apply(approved(store, posted(store, make("!stop carol"))).id, HASH)
    done = store.begin_apply(approved(store, posted(store, make("!stop dave"))).id, HASH)
    store.finish(done.id, True)
    store = reopen(store, tmp_path)
    interrupted = store.recover()
    assert [p.id for p in interrupted] == [planning.id, app.id, applying.id]
    assert {(p.state, p.note) for p in interrupted} == {(INTERRUPTED, "the bot restarted")}
    assert store.get(pending.id).state == PENDING
    assert store.get(done.id).state == DONE
    assert store.recover() == []
    store.close()


def test_state_changes_are_compare_and_set(store):
    plan = begin(store)
    with pytest.raises(PlanError, match="is planning, not pending"):
        store.move(plan.id, (PENDING,), APPROVED)
    assert store.get(plan.id) == plan
    refused = store.refuse(plan.id, "a dry run failed")
    assert (refused.state, refused.note) == (REFUSED, "a dry run failed")
    with pytest.raises(PlanError, match="is refused, not planning"):
        store.refuse(plan.id, "again")


def test_a_concurrent_change_is_caught(store, monkeypatch):
    """If the row changed between the read and the write, nothing is written."""
    one, two = begin(store), begin(store)
    real = store.get

    def stale_read(plan_id):
        found = real(plan_id)
        with store.db:
            store.db.execute("UPDATE plans SET state = 'refused' WHERE id = ?", (plan_id,))
        return found

    monkeypatch.setattr(store, "get", stale_read)
    with pytest.raises(PlanError, match="changed meanwhile"):
        store.move(one.id, (PLANNING,), PENDING)
    with pytest.raises(PlanError, match="changed meanwhile"):
        store._post(two.id, make("!stop alice"), Requirement("change"), HASH, **IDS)
    monkeypatch.undo()
    assert {store.get(one.id).state, store.get(two.id).state} == {REFUSED}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ttl_minutes": 0, "floor": 0},
        {"ttl_minutes": 10, "floor": -1},
        {"ttl_minutes": 10, "floor": True},
    ],
)
def test_the_store_needs_a_ttl_and_a_floor(tmp_path, kwargs):
    with pytest.raises(PlanError, match="TTL"):
        PlanStore(tmp_path / "state.db", **kwargs)


def test_the_database_is_sqlite(store, tmp_path):
    begin(store)
    store.close()
    with sqlite3.connect(tmp_path / "state.db") as db:
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'plans'").fetchone() == (1,)
