"""The console's view of the audit log (docs/ADMIN.md §2.8, §4.1): `console/audit-view.jsonl`,
and each workshop's open plans, which the collector follows from where it left off.

A record's projection holds its event, time, plan, verb, slugs (its `targets`: a plan's
`plan`, `apply_start` and `apply_end` records name its targets, and its other records none;
nor do a `request` and a `read`, whose slugs stay in their arguments), people (the
requester, the approvers, the sender of an approval or a refusal; an approver the runner
wrote as a bare user ID is named after the records that named it) and outcome (`outcome` or
`code`, `exit`, a short `reason`), and nothing else: no argument list (`args`, `argv`), no
hash, no output. The last `KEEP` of each workshop's, and of the server's own (no workshop), are
kept. The collector scrubs them again (§2.7); the log's records are scrubbed already.

**Open plans** are read from the same records: a `plan` record opens one, with its `request`'s
requester; its approvals are counted; the approval that completes them (`code` "approved")
makes it approved, from that record's time; `apply_start` makes it applying; `apply_end`,
`reject`, `expired`, `stale`, `interrupted`, and a `refused` record of the plan itself (one with
no sender: an approval's refusal leaves the plan pending) close it, as `unchanged` would (#708:
no `plan` record comes before one, since such a plan is never offered). A pending plan past its
TTL is no longer shown, whether or not the bot has written its `expired` yet; nor is an approved
one a TTL after its approval, the window `PlanStore.begin_apply` gives its apply to start, after
the runner's recheck (Codex on #490).
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

KEEP = 200
#: How long an applying plan is shown without its end: longer than any apply's timeout (§2.4).
APPLYING_MAX = 6 * 3600.0
CLOSING = frozenset({"apply_end", "reject", "expired", "stale", "interrupted", "unchanged"})
_PLAN = re.compile(r"P-[1-9][0-9]{0,17}", re.ASCII)
_TEXT = {"verb": 40, "outcome": 40, "code": 40, "reason": 200}


def _person(value: Any, role: str) -> dict[str, Any] | None:
    """`{"id", "name"}` as the bot writes a requester or a sender, or a bare user ID, as the
    runner writes `apply_start`'s approvers (Codex on #490): its name is then ""."""
    if type(value) is int:
        return {"id": value, "name": "", "as": role}
    if not isinstance(value, dict) or type(value.get("id")) is not int:
        return None
    name = value.get("name")
    return {"id": value["id"], "name": name[:60] if isinstance(name, str) else "", "as": role}


def _slugs(record: dict[str, Any]) -> list[str]:
    targets = record.get("targets")
    if isinstance(targets, list):
        return [t[:40] for t in targets[:50] if isinstance(t, str)]
    one = record.get("slug") or record.get("participant")
    return [one[:40]] if isinstance(one, str) else []


def project(record: Any) -> dict[str, Any] | None:
    """The record as the console may show it (module doc), or None for no record."""
    if not isinstance(record, dict):
        return None
    seq, ts, event = record.get("seq"), record.get("ts"), record.get("event")
    if type(seq) is not int or not isinstance(ts, str) or not isinstance(event, str):
        return None
    workshop = record.get("workshop")
    out: dict[str, Any] = {
        "seq": seq,
        "ts": ts[:20],
        "event": event[:20],
        "workshop": workshop[:64] if isinstance(workshop, str) else "",
    }
    plan = record.get("plan")
    if isinstance(plan, str) and _PLAN.fullmatch(plan):
        out["plan"] = plan
    for key, limit in _TEXT.items():
        if isinstance(record.get(key), str):
            out[key] = record[key][:limit]
    if type(record.get("exit")) is int:
        out["exit"] = record["exit"]
    people = [_person(record.get(role), role) for role in ("requester", "sender")]
    approvers = record.get("approvers")
    if isinstance(approvers, list):
        people += [_person(a, "approver") for a in approvers]
    out["people"] = [p for p in people if p is not None][:10]
    out["slugs"] = _slugs(record)
    return out


def _epoch(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class View:
    """The audit log, followed: the kept projections and the open plans (module doc)."""

    def __init__(self, ttl: float, on_record: Callable[[dict[str, Any]], None] | None = None):
        """`on_record` sees each record whole, before its projection: what a reader needs
        beyond the kept projections (the collector's `IDLE_VERBS` changes, #494)."""
        self.ttl, self.on_record = ttl, on_record
        self.inode: int | None = None
        self._reset()

    def _reset(self) -> None:
        self.offset = 0
        self.kept: dict[str, deque[dict[str, Any]]] = {}
        self.names: dict[int, str] = {}  # each person's name, as the records last gave it
        self.requesters: dict[str, str] = {}
        self.open: dict[str, dict[str, Any]] = {}

    def follow(self, path: Path) -> bool:
        """Read what was appended since the last call; True when anything changed. A log that
        is a new file, or shorter than what was read, is read again from its start."""
        try:
            with path.open("rb") as f:
                st = os.fstat(f.fileno())
                changed = False
                if st.st_ino != self.inode or st.st_size < self.offset:
                    self._reset()
                    self.inode, changed = st.st_ino, True
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return False
        end = data.rfind(b"\n") + 1  # a partial last line waits for its end
        for line in data[:end].splitlines():
            try:
                self.observe(json.loads(line))
            except ValueError:
                continue
            changed = True
        self.offset += end
        return changed

    def observe(self, record: Any) -> None:
        if self.on_record is not None and isinstance(record, dict):
            self.on_record(record)
        shown = project(record)
        if shown is None:
            return
        for person in shown["people"]:  # a bare ID is named after an earlier record
            if person["name"]:
                self.names[person["id"]] = person["name"]
            else:
                person["name"] = self.names.get(person["id"], "")
        kept = self.kept.setdefault(shown["workshop"], deque(maxlen=KEEP))
        kept.append(shown)
        plan, event = shown.get("plan"), shown["event"]
        if plan is None:
            return
        if event == "request":
            requester = next((p["name"] for p in shown["people"] if p["as"] == "requester"), "")
            self.requesters[plan] = requester
        elif event == "plan":
            approval = record.get("approval") if isinstance(record.get("approval"), dict) else {}
            needed = approval.get("approvers")
            self.open[plan] = {
                "plan": plan,
                "workshop": shown["workshop"],
                "verb": shown.get("verb", ""),
                "state": "pending",
                "since": shown["ts"],
                "requester": self.requesters.pop(plan, ""),
                "targets": shown["slugs"],
                "approvals": [],
                "needed": needed if type(needed) is int else 1,
            }
        elif plan in self.open:
            entry = self.open[plan]
            if event == "approval":
                who = next((p["id"] for p in shown["people"] if p["as"] == "sender"), None)
                if who is not None and who not in entry["approvals"]:
                    entry["approvals"].append(who)
                if record.get("code") == "approved":  # complete: the apply may begin
                    entry["state"], entry["since"] = "approved", shown["ts"]
            elif event == "apply_start":
                entry["state"], entry["since"] = "applying", shown["ts"]
            elif event in CLOSING or (event == "refused" and "sender" not in record):
                del self.open[plan]

    def records(self) -> list[dict[str, Any]]:
        """Every kept projection, in the log's order."""
        return sorted((r for kept in self.kept.values() for r in kept), key=lambda r: r["seq"])

    def plans(self, workshop: str, now: float) -> list[dict[str, Any]]:
        """The workshop's open plans at `now`, each with its expiry while pending or approved
        and not yet applying."""
        out = []
        for entry in self.open.values():
            since = _epoch(entry["since"])
            if entry["workshop"] != workshop or since is None:
                continue
            pending = entry["state"] in ("pending", "approved")
            if now - since > (self.ttl if pending else APPLYING_MAX):
                continue
            expires = _iso(since + self.ttl) if pending else None
            shown = {k: v for k, v in entry.items() if k != "approvals"}
            out.append(shown | {"approvals": len(entry["approvals"]), "expires_at": expires})
        return sorted(out, key=lambda p: int(p["plan"][2:]))
