"""The console's pages (docs/ADMIN.md §4.2), as HTML, from the collector's snapshot and audit view.

Pure functions of what the console reads: no file, no request. Every value from the snapshot is
escaped (`_e`); a page has no inline script and no `style` attribute (§4.3's policy allows only
`/static/console.css` and, on the sign-in page, `/static/login.js`), so a bar's length and
colour are classes. The fleet pages refresh every 60 s, without JavaScript.

- **The workshops** (`index`): a summary row for each workshop the user may see.
- **A workshop** (`workshop`): its fleet totals, cost, open plans, one row per VM, problems
  first (unreachable, doctor failures, strain), and its last actions.
- **A VM** (`vm`): all the collector has of it, and the commands to request in Zulip: Zulip has
  no URL that fills in the compose box (V8), so they are text to copy, with a link to the
  organizers channel.
- **Actions** (`actions`): the workshop's last 50 audit-view records.
- **Sign-in** (`login`), and a page for a message (`message`).

What they read is the collector's, so each value is type-checked where it is used: a section
that isn't what the collector writes shows as unknown, never as an error (the review of #495).
A VM whose slug isn't one the console's routes take (an unlisted VM's raw EC2 tag) is listed
unlinked. A status read older than the VM's current boot says nothing of what a boot changes:
the VM's reachability and strain. Such a read neither ranks the VM as unreachable or strained
nor counts it so in the totals; the values a boot keeps (sign-ins, zab version, doctor failures,
idle stop) are still shown and counted, with the read's age (the reviews of #495). An EC2 read
older than `STALE` is a warning at the top of the workshop's page.
"""

from __future__ import annotations

import html
import math
import re
import time
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from zab.admin.costs import dollars, spares_said

ACTIONS = 50  # records on the actions page
RECENT = 10  # on the workshop's page
#: `!vm`'s colours and thresholds (`zab.render.bars`, #204): the whole percent's.
COLOURS = ((70, "green"), (80, "yellow"), (90, "orange"), (101, "red"))
SIGNED_IN = {"ok": "yes", "no": "no", "FAIL": "FAIL"}
MEMORY = ("used_bytes", "total_bytes")
DISK = ("size_bytes", "used_bytes", "available_bytes")  # status.py's `disk` (#552)
#: A slug the console's routes take (`console.ROUTES`): any other is shown unlinked.
SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", re.ASCII)
#: A Zulip site the collector writes (`collector._SITE`), checked again before it is linked.
SITE = re.compile(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?", re.ASCII)
#: An EC2 read older than this is a warning: the collector reads EC2 every minute.
STALE = 300.0
PROTECTED = {True: "on", False: "off"}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _epoch(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def age(value: Any, now: float) -> str:
    """`just now`, `5 min ago`, `3 h ago`, `2 days ago`; `?` when unknown."""
    at = _epoch(value)
    if at is None:
        return "?"
    seconds = max(0.0, now - at)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 48 * 3600:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def _utc(value: Any) -> str:
    at = _epoch(value)
    return "?" if at is None else time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(at))


def page(title: str, body: str, *, user: str | None = None, refresh: bool = True) -> str:
    """A whole page: the stylesheet, the refresh, the header with the sign-out form."""
    meta = '<meta http-equiv="refresh" content="60">\n' if refresh else ""
    who = ""
    if user is not None:
        who = (
            f'<form class="who" method="post" action="/logout">{_e(user)} '
            '<button type="submit">Sign out</button></form>'
        )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{meta}<title>{_e(title)} · zab console</title>\n"
        '<link rel="stylesheet" href="/static/console.css">\n</head>\n<body>\n'
        f'<header><a class="home" href="/">zab console</a>{who}</header>\n<main>\n'
        f"<h1>{_e(title)}</h1>\n{body}</main>\n</body>\n</html>\n"
    )


def _table(head: Iterable[str], rows: Iterable[Iterable[str]], cls: str = "") -> str:
    """A table of cells already escaped."""
    top = "".join(f"<th>{_e(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>\n" for row in rows)
    return f'<table class="{cls}"><thead><tr>{top}</tr></thead>\n<tbody>\n{body}</tbody></table>\n'


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _since_boot(vm: dict[str, Any], at: Any) -> bool:
    """Whether a read made `at` is of the VM's current boot (its launch time); True when
    either time is unknown."""
    boot, when = _epoch(vm.get("launched_at")), _epoch(at)
    return boot is None or when is None or when >= boot


def _status(vm: dict[str, Any]) -> dict[str, Any]:
    """The row's last status read; none for a row without a VM, whose read is of none it has."""
    status = vm.get("status")
    return status if isinstance(status, dict) and vm.get("state") != "no VM" else {}


def _failures(vm: dict[str, Any]) -> list[str]:
    listed = _status(vm).get("doctor_failures")
    return [f for f in listed if isinstance(f, str) and f] if isinstance(listed, list) else []


def _unreachable(vm: dict[str, Any]) -> bool:
    """Running, and the last read or try since its boot found it unreachable."""
    status = _status(vm)
    tried = _d(status.get("tried"))
    read = status.get("bridge") == "unreachable" and _since_boot(vm, status.get("at"))
    retried = tried.get("bridge") == "unreachable" and _since_boot(vm, tried.get("at"))
    return vm.get("state") == "running" and (read or retried)


def _strained(vm: dict[str, Any]) -> list[str]:
    """What the last read since its boot found under strain."""
    if not _since_boot(vm, _status(vm).get("at")):
        return []
    listed = _status(vm).get("strained")
    return [s for s in listed if isinstance(s, str)] if isinstance(listed, list) else []


def rank(vm: dict[str, Any]) -> tuple[int, str]:
    """Problems first (§4.2): unreachable, doctor failures, strain; then the rest, then the
    rows without a VM, whatever their last read said; by slug within each."""
    if vm.get("state") == "no VM":
        first = 4
    elif _unreachable(vm):
        first = 0
    elif _failures(vm):
        first = 1
    elif _strained(vm):
        first = 2
    else:
        first = 3
    return first, str(vm.get("slug"))


def _signed(vm: dict[str, Any]) -> str:
    status = _status(vm)
    codex, claude = (status.get(k) == "ok" for k in ("codex_login", "claude_login"))
    if status.get("codex_login") is None and status.get("claude_login") is None:
        return "unknown"
    return {(True, True): "both", (True, False): "Codex only", (False, True): "Claude only"}.get(
        (codex, claude), "neither"
    )


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def bar(fraction: float, label: str) -> str:
    """A ten-step bar in `!vm`'s colour for the whole percent, and its label."""
    percent = math.floor(max(0.0, min(1.0, fraction)) * 100 + 0.5)
    colour = next(name for top, name in COLOURS if percent < top)
    width = max(1 if fraction > 0 else 0, (percent + 5) // 10)
    return (
        f'<span class="bar"><span class="fill w{width} {colour}"></span></span> '
        f'<span class="num">{_e(label)}</span>'
    )


def disk_gb(n: int) -> str:
    """Bytes as binary "GB" (2**30 bytes), rounded up as `df -h` rounds them: to a tenth below
    10 GB, whole from there; `0` for none. The disk's unit (#552) is memory's and `!vm`'s (#204;
    SPEC §13.5: "GB means binary gigabytes"), and `df -h`'s; `!vm` rounds
    its size to the nearest GB, so the two can differ by one (the fleet's 200 GiB root, 193.6
    GiB, reads 194 in both). status.py has the same (`disk_gb`; a test keeps the two equal)."""
    if n <= 0:
        return "0"
    tenths = -(-n * 10 // 2**30)
    return f"{tenths // 10}.{tenths % 10}" if tenths < 100 else str(-(-n // 2**30))


def disk_space(value: Any) -> tuple[int, int, int] | None:
    """The root file system's size, use and available space in bytes, from a status read's
    `disk` (status.py's, #552), when they are whole numbers as `df` gives them (a positive size,
    the others within it); None otherwise, as for a read made before #552, which has none."""
    if not isinstance(value, dict):
        return None
    found = [value.get(k) for k in DISK]
    if not all(type(n) is int for n in found):
        return None
    size, used, available = found
    if size > 0 and 0 <= used <= size and 0 <= available <= size:
        return size, used, available
    return None


def resources(vm: dict[str, Any]) -> tuple[str, str, str]:
    """CPU, memory and disk as bars; memory in "GB" as `!vm` says it (binary, #204); the disk
    as `152 of 194 GB (78%)`, in binary GB as memory (`disk_gb`), with `df`'s percentage and
    its bar, or the percentage alone from a read without the sizes (one made before #552)."""
    status = _status(vm)
    load, memory, disk = status.get("load"), status.get("memory"), status.get("disk_percent")
    cpu = mem = dsk = "?"
    if isinstance(load, dict) and _number(load.get("load1")) and _number(load.get("cpus")):
        if load["cpus"] > 0:
            cpu = bar(load["load1"] / load["cpus"], f"{load['load1']:.1f} of {load['cpus']:g}")
    if isinstance(memory, dict) and all(_number(memory.get(k)) for k in MEMORY):
        used, total = memory["used_bytes"] / 2**30, memory["total_bytes"] / 2**30
        if total > 0:
            mem = bar(used / total, f"{used:.1f} of {math.ceil(total)} GB")
    if _number(disk):
        space = disk_space(status.get("disk"))
        label = f"{disk:g}%"
        if space is not None:
            label = f"{disk_gb(space[1])} of {disk_gb(space[0])} GB ({label})"
        dsk = bar(disk / 100, label)
    return cpu, mem, dsk


#: status.py's `sentinel` (#588): `ok`, or the states of the bots' sentinels that warn, of
#: `zab.tools.sentinel.UNHEALTHY` (a copy: the admin server doesn't import that module, and a
#: test keeps the two equal), or `warn` for another; anything else is the VM's own text. In
#: those of `SENTINEL_DOWN_STATES` (`sentinel.DOWN`) no sentinel answers; in `unknown` and
#: `warn` the read couldn't tell.
SENTINEL_DOWN_STATES = ("not installed", "down", "no mark", "stale")
SENTINEL_STATES = (*SENTINEL_DOWN_STATES, "unknown", "warn")
_STATE = "|".join(SENTINEL_STATES)
_SENTINEL = re.compile(rf"ok|(?:{_STATE})(?:, (?:{_STATE}))*+")  # possessive: the VM's text (#833)
#: ... said after a state of `SENTINEL_DOWN_STATES` (as `zab doctor` does).
SENTINEL_DOWN = "DM !restart is unavailable, and the bridge answers DM !vm itself"


def sentinel(status: dict[str, Any]) -> str:
    """The VM's sentinel as its last status read gave it (#588): `ok`, `down`, `stale, no
    mark`...; `?` for none (a read from before #588, or a zab before it) or any other text."""
    value = status.get("sentinel")
    return value if isinstance(value, str) and _SENTINEL.fullmatch(value) else "?"


def sentinel_down(shown: str) -> str:
    """`SENTINEL_DOWN` when one of the states `sentinel` gave says that no sentinel answers;
    "" otherwise."""
    return SENTINEL_DOWN if set(shown.split(", ")) & set(SENTINEL_DOWN_STATES) else ""


def _idle(vm: dict[str, Any]) -> str:
    idle = _status(vm).get("idle_stop")
    if not isinstance(idle, dict) or not isinstance(idle.get("enabled"), bool):
        return "?"
    if not idle["enabled"]:
        return "off"
    minutes = idle.get("minutes")
    return f"{minutes} min" if type(minutes) is int else "on"


def _waker(vm: dict[str, Any], now: float) -> str:
    waker = vm.get("waker")
    if not isinstance(waker, dict):
        return "?"
    if not waker.get("opted_in"):
        return "off"
    last = waker.get("last_wake")
    return f"on, woke {age(last, now)}" if last else "on"


def _uptime(vm: dict[str, Any], now: float) -> str:
    state = str(vm.get("state", "?"))
    if state != "running":
        return _e(state)
    since = _epoch(vm.get("launched_at"))
    if since is None:
        return "running"
    hours = max(0.0, now - since) / 3600
    up = (
        f"{hours * 60:.0f} min"
        if hours < 1
        else f"{hours:.0f} h"
        if hours < 48
        else (f"{hours / 24:.0f} days")
    )
    return f"running {up}"


def _cut(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _money(cost: Any, key: str) -> str:
    value = cost.get(key) if isinstance(cost, dict) else None
    return dollars(value) if _number(value) else "?"


def _spare(vm: dict[str, Any]) -> bool:
    return vm.get("kind") == "spare"


def running_of(vms: list[dict[str, Any]]) -> str:
    """`2 of 3`, the VMs running of those launched, and ` · spares 1 of 1` apart (#715), as
    `!status`'s cost line counts them (`collector.spares_said`)."""
    fleet = [vm for vm in vms if not _spare(vm) and vm.get("state") != "no VM"]
    spares = [vm for vm in vms if _spare(vm)]
    up = sum(1 for vm in fleet if vm.get("state") == "running")
    said = f"{up} of {len(fleet)}"
    if spares:
        said += f" · spares {sum(1 for vm in spares if vm.get('state') == 'running')} of "
        said += str(len(spares))
    return said


def totals(vms: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The fleet totals of §4.2, each as (what, how many), already escaped. A row without a VM
    counts only among the states: its last read is of no VM it has now. The spares' states are
    a line of their own (#715), as `!status`'s cost line has them."""
    states = Counter(str(vm.get("state")) for vm in vms if not _spare(vm))
    spared = Counter(str(vm.get("state")) for vm in vms if _spare(vm))
    vms = [vm for vm in vms if vm.get("state") != "no VM"]
    signed = Counter(_signed(vm) for vm in vms)
    versions = Counter(
        str(_status(vm).get("zab_version")) for vm in vms if _status(vm).get("zab_version")
    )
    idle = sum(1 for vm in vms if _idle(vm) not in ("off", "?"))
    woken = sum(1 for vm in vms if _d(vm.get("waker")).get("opted_in") is True)

    def counts(counter: Counter[str]) -> str:
        return ", ".join(f"{n} {_e(k)}" for k, n in sorted(counter.items())) or "none"

    return [
        ("VMs", counts(states)),
        *([("Spares", counts(spared))] if spared else []),
        ("Signed in", counts(signed)),
        ("zab versions", counts(versions)),
        ("Under strain", str(sum(1 for vm in vms if _strained(vm)))),
        ("Unreachable", str(sum(1 for vm in vms if _unreachable(vm)))),
        ("Doctor failures", str(sum(1 for vm in vms if _failures(vm)))),
        ("Idle stop on", str(idle)),
        ("Waker opted in", str(woken)),
    ]


def index(snapshot: dict[str, Any], names: list[str], now: float, *, user: str) -> str:
    """The workshops the user may see (`names`), a summary row each."""
    rows = []
    workshops = snapshot.get("workshops") if isinstance(snapshot.get("workshops"), dict) else {}
    for name in names:
        ws = _d(workshops.get(name))
        vms = [vm for vm in _l(ws.get("vms")) if isinstance(vm, dict)]
        cost = _d(ws.get("cost"))
        trouble = sum(1 for vm in vms if rank(vm)[0] < 3)
        rows.append([
            f'<a href="/w/{_e(name)}/">{_e(name)}</a>', _e(ws.get("region", "?")),
            running_of(vms), str(trouble), _money(cost, "month"),
            _money(cost, "projected"), _e(age(_d(ws.get("ec2")).get("at"), now)),
        ])  # fmt: skip
    head = ["Workshop", "Region", "Running", "Problems", "This month", "Projected", "EC2 read"]
    body = _table(head, rows) if rows else "<p>No workshop.</p>\n"
    note = '<p class="note">Costs are estimates (ADMIN §11.2).</p>\n'
    at = _epoch(snapshot.get("at"))
    warn = ""
    if at is None or now - at > STALE:  # the collector writes one every minute
        said = "There is no snapshot yet" if at is None else f"The snapshot is from {age(at, now)}"
        warn = f'<p class="warn">{_e(said)}: the collector may be down.</p>\n'
    for name in names:  # the waker down (#717)
        if isinstance(since := _d(workshops.get(name)).get("waker_down"), str):
            warn += f'<p class="warn">{_e(name)}: waker down since {_e(_utc(since))}.</p>\n'
    return page("Workshops", warn + body + note + _written(snapshot, now), user=user)


def _written(snapshot: dict[str, Any], now: float) -> str:
    return (
        f'<p class="note">Snapshot {_e(age(snapshot.get("at"), now))}; the waker\'s data '
        f"{_e(age(snapshot.get('waker_at'), now))}.</p>\n"
    )


def _zulip(ws: dict[str, Any]) -> str:
    site, channels = ws.get("site"), _l(ws.get("channels"))
    if not (isinstance(site, str) and SITE.fullmatch(site) and channels):
        return ""
    if type(channels[0]) is not int:
        return ""
    url = f"{site}/#narrow/channel/{channels[0]}"
    return f'<a href="{_e(url)}">the organizers channel</a>'


def workshop(name: str, ws: dict[str, Any], records: list[dict[str, Any]], now: float, *,
             user: str) -> str:  # fmt: skip
    """A workshop's page (module doc)."""
    ws = _d(ws)
    vms = sorted((vm for vm in _l(ws.get("vms")) if isinstance(vm, dict)), key=rank)
    records = [r for r in _l(records) if isinstance(r, dict)]
    cost = _d(ws.get("cost"))
    ec2 = _d(ws.get("ec2"))
    parts = [_stale(ec2, now), _down(ws), _table(["", ""], totals(vms), "totals")]
    hours = cost.get("hours_today") if _number(cost.get("hours_today")) else "?"
    line = (
        f"{_money(cost, 'month')} this month, {_money(cost, 'projected')} projected "
        f"(estimates) · {_e(cost.get('running', '?'))} of {_e(cost.get('vms', '?'))} running"
        f"{_e(spares_said(cost))} · {_e(hours)} running hours today"
    )
    unpriced = cost.get("unpriced")
    unpriced = [t for t in unpriced if isinstance(t, str)] if isinstance(unpriced, list) else []
    if unpriced:  # a VM with any such run leaves all its dollars out (the review of #490)
        kinds = ", ".join(_e(t) for t in unpriced)
        line += f" · the dollars leave out every VM that ran as, or is now, {kinds} (no price)"
    if cost.get("unpriced_region"):  # its region has no prices: no dollars at all (#499)
        line += f" · no prices for {_e(cost['unpriced_region'])}"
    parts.append(f"<h2>Cost</h2>\n<p>{line}</p>\n")
    plans = [p for p in _l(ws.get("plans")) if isinstance(p, dict)]
    if plans:
        parts.append("<h2>Open plans</h2>\n" + _table(
            ["Plan", "Verb", "Targets", "State", "Approvals", "Requested by", "Expires"],
            ([_e(p.get("plan")), _e(p.get("verb")), _e(" ".join(map(str, _l(p.get("targets"))))),
              _e(p.get("state")), f"{_e(p.get('approvals'))} of {_e(p.get('needed'))}",
              _e(p.get("requester")), _e(_utc(p.get("expires_at")) if p.get("expires_at")
                                         else "")] for p in plans),
        ))  # fmt: skip
    error = f" (last read failed: {_e(ec2['error'])})" if ec2.get("error") else ""
    parts.append(f"<h2>VMs</h2>\n<p class=\"note\">EC2 read {_e(age(ec2.get('at'), now))}{error}."
                 f" Status values as of each row's last read.</p>\n")  # fmt: skip
    head = ["VM", "State", "zab", "Signed in", "Doctor", "Sentinel", "Last activity", "CPU",
            "Memory", "Disk", "Idle stop", "Waker", "Cost (month)", "Read"]  # fmt: skip
    rows = []
    for vm in vms:
        slug = _e(vm.get("slug"))
        failures = _failures(vm)
        status = _status(vm)
        cls = {0: "problem", 1: "problem", 2: "strain"}.get(rank(vm)[0], "")
        shown = (
            f'<a class="{cls}" href="/w/{_e(name)}/vm/{slug}">{slug}</a>'
            if isinstance(vm.get("slug"), str) and SLUG.fullmatch(vm["slug"])
            else f'<span class="{cls}">{slug}</span>'  # no route takes it (the review of #495)
        )
        rows.append([
            shown + (f' <span class="kind">{_e(vm.get("kind"))}</span>'
                     if vm.get("kind") != "participant" else ""),
            ("unreachable" if _unreachable(vm) else _uptime(vm, now)),
            _e(status.get("zab_version") or "?"), _e(_signed(vm)),
            _e(_cut(failures[0])) + (f" (+{len(failures) - 1})" if len(failures) > 1 else "")
            if failures else "", _e(sentinel(status)),
            _e(age(status.get("last_activity"), now) if status else "?"),
            *resources(vm), _e(_idle(vm)), _e(_waker(vm, now)),
            _money(vm.get("cost"), "month"), _e(age(status.get("at"), now) if status else "never"),
        ])  # fmt: skip
    parts.append(_table(head, rows, "fleet") if rows else "<p>No VM and no roster row.</p>\n")
    if any(vm.get("kind") == "unlisted" for vm in vms):
        parts.append('<p class="note">An unlisted VM\'s participant tag is no row of the roster '
                     "and no spare: no command can name it. A deployer reconciles it with the "
                     "roster, or ends it by hand.</p>\n")  # fmt: skip
    recent = [r for r in records if r.get("workshop") == name][-RECENT:]
    more = f'<p><a href="/w/{_e(name)}/actions">The last {ACTIONS} actions</a></p>\n'
    parts.append(f"<h2>Recent actions</h2>\n{_records(list(reversed(recent)))}{more}")
    return page(name, "".join(parts), user=user)


def _down(ws: dict[str, Any]) -> str:
    """The waker down (#717; `zab.admin.wakerdown`); "" otherwise."""
    since = ws.get("waker_down")
    if not isinstance(since, str):
        return ""
    return (
        f'<p class="warn">waker down since {_e(_utc(since))}: no stopped VM wakes for its '
        "participant's messages. Start one with !start meanwhile (ORGANIZERS §4.1).</p>\n"
    )


def _stale(ec2: dict[str, Any], now: float) -> str:
    """A warning when EC2 hasn't been read lately (the review of #495); "" otherwise."""
    at = _epoch(ec2.get("at"))
    if at is not None and now - at <= STALE:
        return ""
    said = "EC2 hasn't been read yet" if at is None else f"EC2 was last read {age(at, now)}"
    why = f" (the last read failed: {ec2['error']})" if isinstance(ec2.get("error"), str) else ""
    return f'<p class="warn">{_e(said + why)}: the states below may be out of date.</p>\n'


def _records(records: list[dict[str, Any]]) -> str:
    if not records:
        return "<p>None.</p>\n"
    rows = []
    for r in records:
        people = ", ".join(
            f"{_e(p.get('name'))} ({_e(p.get('as'))})" for p in _l(r.get("people"))
            if isinstance(p, dict)
        )  # fmt: skip
        outcome = " ".join(_e(r[k]) for k in ("outcome", "code") if isinstance(r.get(k), str)) + (
            f" exit {_e(r['exit'])}" if "exit" in r else ""
        )
        reason = f'<br><span class="note">{_e(r["reason"])}</span>' if r.get("reason") else ""
        rows.append([
            _e(_utc(r.get("ts"))), _e(r.get("event")), _e(r.get("plan", "")),
            _e(r.get("verb", "")), _e(" ".join(map(str, _l(r.get("slugs"))))), people,
            outcome + reason,
        ])  # fmt: skip
    head = ["Time", "Event", "Plan", "Verb", "VMs", "People", "Outcome"]
    return _table(head, rows, "actions")


def actions(name: str, records: list[dict[str, Any]], now: float, *, user: str) -> str:
    """The workshop's last `ACTIONS` records, newest first."""
    mine = [r for r in _l(records) if isinstance(r, dict) and r.get("workshop") == name]
    mine = mine[-ACTIONS:]
    back = f'<p><a href="/w/{_e(name)}/">Back to {_e(name)}</a></p>\n'
    return page(f"{name}: actions", back + _records(list(reversed(mine))), user=user)


def _commands(vm: dict[str, Any]) -> list[str]:
    """What to request in Zulip for this VM: text to copy (V8)."""
    slug, state = str(vm.get("slug")), vm.get("state")
    if vm.get("kind") == "spare":
        return [f"!start {slug}", f"!stop {slug}"]
    if state == "no VM":
        return [f"!launch {slug}"]
    out = [f"!status {slug}"]
    out.append(f"!stop {slug}" if state == "running" else f"!start {slug}")
    return out


def vm(name: str, ws: dict[str, Any], slug: str, now: float, *, user: str) -> str | None:
    """One VM's page; None when the workshop has no such row or VM."""
    ws = _d(ws)
    rows = [v for v in _l(ws.get("vms")) if isinstance(v, dict)]
    found = next((v for v in rows if v.get("slug") == slug), None)
    if found is None:
        return None
    status = _status(found)
    cpu, mem, disk = resources(found)
    idle = status.get("idle_stop") if isinstance(status.get("idle_stop"), dict) else {}
    tried = status.get("tried") if isinstance(status.get("tried"), dict) else {}
    cost = _d(found.get("cost"))
    turns = status.get("running_turns")
    codex, claude = (SIGNED_IN.get(status.get(k), "?") for k in ("codex_login", "claude_login"))
    verdict = f" · last verdict: {_e(idle['last_verdict'])}" if idle.get("last_verdict") else ""
    activity = status.get("last_activity")
    read = f"{_e(age(status.get('at'), now))} by {_e(status.get('source', '?'))}"
    if status and not _since_boot(found, status.get("at")):
        read += " (before its last start: its reachability and strain are not the VM's now)"
    watch = sentinel(status)
    down = sentinel_down(watch)
    rows = [
        ("State", _uptime(found, now) + (" (unreachable)" if _unreachable(found) else "")),
        ("Instance", f"{_e(found.get('instance', '-'))} · {_e(found.get('type', '-'))}"),
        ("Termination protection", _e(PROTECTED.get(found.get("protected"), "?"))),
        ("zab", _e(status.get("zab_version") or "?")),
        ("Bridge", _e(status.get("bridge") or "?")),
        ("Sentinel", _e(watch) + f" ({down})" * bool(down)),
        ("Signed in", f"Codex {_e(codex)}, Claude {_e(claude)}"),
        ("Doctor", "<br>".join(_e(f) for f in _failures(found)) or "no failure"),
        ("Last activity", f"{_e(_utc(activity))} ({_e(age(activity, now))})"),
        ("Running turns", _e(len(turns)) if isinstance(turns, list) else "?"),
        ("CPU (load of CPUs)", cpu),
        ("Memory", mem),
        ("Disk", disk),
        ("Idle stop", _e(_idle(found)) + verdict),
        ("Waker", _e(_waker(found, now))),
        ("Last error", _e(status.get("last_error") or "")),
        (
            "Cost",
            f"{_money(cost, 'month')} this month, {_money(cost, 'projected')} projected; "
            f"{_e(cost.get('hours_today', '?'))} running hours today (estimates)",
        ),
        ("Status read", read if status else "never"),
    ]
    if tried:
        rows.append(("Last try", f"{_e(age(tried.get('at'), now))}: {_e(tried.get('bridge'))}"))
    zulip = _zulip(ws)
    where = f" in {zulip}" if zulip else " in the workshop's organizers channel"
    helper = "".join(f"<li><code>{_e(c)}</code></li>" for c in _commands(found))
    request = (
        f"<p>Copy a command and send it{where}; the bot posts a plan to approve. Nothing here "
        f'changes the fleet.</p>\n<ul class="commands">{helper}</ul>\n'
    )
    if found.get("kind") == "unlisted":  # no command can name it (Codex on #495)
        request = (
            "<p>No command can name this VM: its participant tag is no row of the workshop's "
            "roster and no spare of its <code>spares.json</code>. A deployer reconciles it "
            "with the roster, or ends it by hand (ORGANIZERS §7).</p>\n"
        )
    body = (
        f'<p><a href="/w/{_e(name)}/">Back to {_e(name)}</a></p>\n'
        + _table(["", ""], rows, "detail")
        + f"<h2>Request in Zulip</h2>\n{request}"
    )
    return page(f"{name}: {slug}", body, user=user)


def login(message: str | None = None) -> str:
    """The sign-in page: `login.js` posts the link's fragment (§4.4)."""
    note = f'<p class="note">{_e(message)}</p>\n' if message else ""
    body = (
        f"{note}<p>Send <code>!console</code> to your workshop's admin bot in Zulip, in your "
        "organizers channel or by direct message, and open the link it sends you by DM within "
        "5 minutes. Each link signs in once.</p>\n"
        '<form id="login" method="post" action="/login">'
        '<input type="hidden" name="token" id="token" value="">'
        "<noscript><p>Signing in needs JavaScript on this page only.</p></noscript>"
        "</form>\n"
        '<script src="/static/login.js"></script>\n'
    )
    return page("Sign in", body, refresh=False)


def message(title: str, text: str, *, user: str | None = None) -> str:
    return page(title, f"<p>{_e(text)}</p>\n", user=user, refresh=False)
