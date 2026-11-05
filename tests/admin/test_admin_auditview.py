"""The console's view of the audit log (docs/ADMIN.md §2.8, `zab.admin.auditview`): the
projection without argument lists, the log followed from where it left off, the open plans."""

import json
import os

from zab.admin.auditview import KEEP, View, project

APPLY_END = {
    "seq": 7, "ts": "2026-10-20T12:03:00Z", "event": "apply_end", "workshop": "harvard",
    "plan": "P-42", "verb": "rollout", "args": ["v0.2.6", "alice", "idle-stop=60"],
    "argv": [["/opt/zab-admin/current/.venv/bin/python", "push_config.py", "--only", "alice"]],
    "hash": "3f" * 32, "exit": 0, "outcome": "done", "targets": ["alice", "bob"],
    "output": "runs/P-42/apply.txt", "output_sha256": "ab" * 32, "duration_s": 184,
    "requester": {"id": 102, "name": "Alice Organizer"},
    "approvers": [{"id": 101, "name": "Drew Sutherland"}], "prev": "00" * 32,
}  # fmt: skip


def test_the_projection_keeps_no_argument_list():
    shown = project(APPLY_END)
    assert shown == {
        "seq": 7, "ts": "2026-10-20T12:03:00Z", "event": "apply_end", "workshop": "harvard",
        "plan": "P-42", "verb": "rollout", "exit": 0, "outcome": "done",
        "slugs": ["alice", "bob"],
        "people": [{"id": 102, "name": "Alice Organizer", "as": "requester"},
                   {"id": 101, "name": "Drew Sutherland", "as": "approver"}],
    }  # fmt: skip
    text = json.dumps(shown)
    for gone in ("v0.2.6", "push_config", "idle-stop", "3f3f", "runs/P-42", "abab", "184"):
        assert gone not in text


def test_a_projection_is_bounded_and_typed():
    record = {"seq": 3, "ts": "2026-10-20T12:00:00Z", "event": "refused", "workshop": "x" * 99,
              "plan": "P-1; rm", "reason": "r" * 999, "sender": {"id": "7", "name": "n"},
              "slug": "alice"}  # fmt: skip
    shown = project(record)
    assert shown is not None
    assert len(shown["workshop"]) == 64 and len(shown["reason"]) == 200
    assert "plan" not in shown and shown["people"] == [] and shown["slugs"] == ["alice"]
    for bad in (None, [], {"seq": "1", "ts": "x", "event": "plan"}, {"seq": 1, "event": "x"}):
        assert project(bad) is None


def lines(*records: dict) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in records)


def record(seq: int, event: str, **fields) -> dict:
    return {"seq": seq, "ts": "2026-10-20T12:00:00Z", "event": event, "workshop": "harvard",
            **fields}  # fmt: skip


def test_the_log_is_followed_from_where_it_left_off(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_bytes(lines(record(1, "read"), record(2, "read")) + b'{"seq": 3, "ts')
    view = View(600)
    assert view.follow(path) and [r["seq"] for r in view.records()] == [1, 2]
    assert not view.follow(path)  # the partial line waits for its end
    with path.open("ab") as f:
        f.write(b'": "2026-10-20T12:00:01Z", "event": "read"}\nnot json\n')
    assert view.follow(path) and [r["seq"] for r in view.records()] == [1, 2, 3]
    assert not view.follow(path)
    # a log restored from a backup, a new file: read again from its start
    os.replace(_new(tmp_path, lines(record(1, "read"))), path)
    assert view.follow(path) and [r["seq"] for r in view.records()] == [1]
    assert not View(600).follow(tmp_path / "missing.jsonl")


def _new(tmp_path, data: bytes):
    other = tmp_path / "new.jsonl"
    other.write_bytes(data)
    return other


def test_each_workshop_keeps_its_last_records(tmp_path):
    path = tmp_path / "audit.jsonl"
    many = [record(n, "read") for n in range(1, KEEP + 11)]
    path.write_bytes(lines(*many, {**record(KEEP + 11, "upgrade"), "workshop": ""}))
    view = View(600)
    view.follow(path)
    seqs = [r["seq"] for r in view.records()]
    assert seqs[0] == 11 and len(seqs) == KEEP + 1 and seqs[-1] == KEEP + 11


def ts(minute: int) -> str:
    return f"2026-10-20T12:{minute:02d}:00Z"


def test_open_plans(tmp_path):
    now = 1792497600.0 + 5 * 60  # 2026-10-20T12:05:00Z
    path = tmp_path / "audit.jsonl"
    requester = {"id": 102, "name": "Alice Organizer"}
    approval = {"class": "destructive", "approvers": 2, "phrase": "teardown 1"}
    path.write_bytes(lines(
        record(1, "request", plan="P-41", requester=requester, verb="teardown"),
        {**record(2, "plan", plan="P-41", verb="teardown", targets=["bob"], approval=approval),
         "ts": ts(1)},
        record(3, "approval", plan="P-41", sender={"id": 101, "name": "Drew"}, code="recorded"),
        record(4, "approval", plan="P-41", sender={"id": 101, "name": "Drew"}, code="again"),
        record(5, "refused", plan="P-41", sender={"id": 9, "name": "x"}, code="not an approver"),
        record(6, "request", plan="P-42", requester=requester, verb="stop"),
        {**record(7, "plan", plan="P-42", verb="stop", targets=["alice"]), "ts": ts(2)},
        {**record(8, "apply_start", plan="P-42", verb="stop"), "ts": ts(3)},
        record(9, "request", plan="P-43", requester=requester, verb="stop"),
        record(10, "refused", plan="P-43", reason="the dry run failed"),
        {**record(11, "plan", plan="P-44", verb="stop", targets=["alice"]), "ts": ts(0)},
        record(12, "reject", plan="P-44", sender={"id": 101, "name": "Drew"}),
        {**record(13, "plan", plan="P-45", verb="stop", workshop="mit"), "workshop": "mit"},
    ))  # fmt: skip
    view = View(600)
    view.follow(path)
    plans = view.plans("harvard", now)
    assert [p["plan"] for p in plans] == ["P-41", "P-42"]
    assert plans[0] == {
        "plan": "P-41", "workshop": "harvard", "verb": "teardown", "state": "pending",
        "since": ts(1), "requester": "Alice Organizer", "targets": ["bob"], "approvals": 1,
        "needed": 2, "expires_at": ts(11),
    }  # fmt: skip
    assert plans[1]["state"] == "applying" and plans[1]["expires_at"] is None
    assert [p["plan"] for p in view.plans("mit", now)] == ["P-45"]
    # past its TTL a pending plan is no longer shown; an applying one still is
    assert [p["plan"] for p in view.plans("harvard", now + 600)] == ["P-42"]
    with path.open("ab") as f:
        f.write(lines(record(14, "apply_end", plan="P-42", exit=0, outcome="done")))
    view.follow(path)
    assert view.plans("harvard", now) == [view.plans("harvard", now)[0]]
    assert [p["plan"] for p in view.plans("harvard", now)] == ["P-41"]


def test_the_runners_approvers_are_people(tmp_path):
    """Codex on #490: `apply_start` names its approvers by user ID (`runner.py`, from
    `Approvals.approved_by`), as the runner writes it; each is named after an earlier record."""
    path = tmp_path / "audit.jsonl"
    path.write_bytes(lines(
        record(1, "approval", plan="P-9", sender={"id": 101, "name": "Drew Sutherland"},
               code="approved"),
        record(2, "apply_start", plan="P-9", hash="ab" * 32, verb="stop", args=["bob"],
               approvers=[101, 104], server="v0.2.6+abc", targets=["bob"]),
    ))  # fmt: skip
    view = View(600)
    view.follow(path)
    start = view.records()[-1]
    assert start["people"] == [
        {"id": 101, "name": "Drew Sutherland", "as": "approver"},
        {"id": 104, "name": "", "as": "approver"},
    ]
    assert project({**record(3, "x"), "approvers": [True, "7", 5]})["people"] == [
        {"id": 5, "name": "", "as": "approver"}
    ]


def test_an_approved_plan_stays_until_its_apply_may_no_longer_begin(tmp_path):
    """Codex on #490: the approval that completes a plan gives its apply a TTL to begin
    (`PlanStore.begin_apply`), after the runner's recheck; the plan stays shown, approved."""
    noon = 1792497600.0  # 2026-10-20T12:00:00Z
    path = tmp_path / "audit.jsonl"
    path.write_bytes(lines(
        {**record(1, "plan", plan="P-50", verb="rollout", targets=["alice"]), "ts": ts(0)},
        {**record(2, "approval", plan="P-50", sender={"id": 101, "name": "Drew"},
                  code="approved"), "ts": ts(9)},
    ))  # fmt: skip
    view = View(600)
    view.follow(path)
    [plan] = view.plans("harvard", noon + 11 * 60)  # past the plan's own TTL
    assert (plan["state"], plan["since"], plan["expires_at"]) == ("approved", ts(9), ts(19))
    assert plan["approvals"] == 1
    assert view.plans("harvard", noon + 19 * 60 + 1) == []
    with path.open("ab") as f:
        f.write(lines({**record(3, "apply_start", plan="P-50", verb="rollout"), "ts": ts(12)}))
    view.follow(path)
    [plan] = view.plans("harvard", noon + 30 * 60)
    assert (plan["state"], plan["expires_at"]) == ("applying", None)
