"""The console's pages (docs/ADMIN.md §4.2, §4.3; `zab.admin.pages`), rendered from the
collector's golden snapshot: escaped, with no inline script or style, problems first."""

import copy
import json
import re
from pathlib import Path

import pytest

from zab.admin import pages

GOLDEN = Path(__file__).parent / "golden" / "collector-snapshot.json"
CSS = Path(pages.__file__).parent / "static" / "console.css"
NOW = 1_800_000_000.0 + 120  # two minutes after the snapshot
EVIL = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'


@pytest.fixture
def snapshot():
    return json.loads(GOLDEN.read_text())


def harvard(snapshot):
    return snapshot["workshops"]["harvard-2026"]


def records(n=3, workshop="harvard-2026"):
    return [
        {"seq": i, "ts": f"2027-01-15T07:{i % 60:02d}:00Z", "event": "apply_end",
         "workshop": workshop, "plan": f"P-{i}", "verb": "stop", "exit": 0, "outcome": "done",
         "slugs": ["bob"], "people": [{"id": 103, "name": "Olga Organizer", "as": "requester"}]}
        for i in range(1, n + 1)
    ]  # fmt: skip


def every_page(snapshot):
    ws = harvard(snapshot)
    yield pages.index(snapshot, ["harvard-2026", "zab-test"], NOW, user="Drew")
    yield pages.workshop("harvard-2026", ws, records(), NOW, user="Drew")
    for row in ws["vms"]:
        yield pages.vm("harvard-2026", ws, row["slug"], NOW, user="Drew")
    yield pages.actions("harvard-2026", records(), NOW, user="Drew")
    yield pages.login("That link was used already.")
    yield pages.message("Not found", "No such page.")


def test_no_inline_script_or_style_anywhere(snapshot):
    """§4.3: the policy allows `/static/console.css` and `/static/login.js` only."""
    for html in every_page(snapshot):
        assert html.startswith("<!doctype html>")
        assert '<link rel="stylesheet" href="/static/console.css">' in html
        assert not re.search(r"\sstyle\s*=", html, re.I)
        assert not re.search(r"<style", html, re.I)
        assert not re.search(r"\son[a-z]+\s*=", html, re.I)  # no event handler attribute
        for tag in re.findall(r"<script\b[^>]*>", html, re.I):
            assert tag == '<script src="/static/login.js">'
        assert "javascript:" not in html.lower()


def test_the_fleet_pages_refresh_and_the_sign_in_page_does_not(snapshot):
    *fleet, login, message = every_page(snapshot)
    assert all('<meta http-equiv="refresh" content="60">' in html for html in fleet)
    assert "http-equiv" not in login and "http-equiv" not in message
    assert '<script src="/static/login.js"></script>' in login
    assert '<form id="login" method="post" action="/login">' in login
    assert '<input type="hidden" name="token" id="token" value="">' in login
    assert "That link was used already." in login


def test_every_value_is_escaped(snapshot):
    ws = harvard(snapshot)
    alice = ws["vms"][0]
    alice["status"] |= {"doctor_failures": [EVIL], "last_error": EVIL, "zab_version": EVIL}
    alice["status"]["idle_stop"]["last_verdict"] = EVIL
    ws["plans"][0]["requester"] = EVIL
    ws["site"] = 'https://zulip.example.com/"><script>'
    shown = [
        pages.workshop("harvard-2026", ws, records(), NOW, user=EVIL),
        pages.vm("harvard-2026", ws, "alice", NOW, user=EVIL),
        pages.actions(
            "harvard-2026",
            [
                {
                    **records(1)[0],
                    "reason": EVIL,
                    "people": [{"id": 1, "name": EVIL, "as": "sender"}],
                }
            ],
            NOW,
            user=EVIL,
        ),
        pages.message(EVIL, EVIL),
    ]
    for html in shown:
        assert "<script>" not in html and "<img" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;&quot;&gt;&lt;img" in html


def test_problems_first(snapshot):
    ws = harvard(snapshot)
    by_slug = {row["slug"]: row for row in ws["vms"]}
    by_slug["zed"]["status"] = {"strained": ["mem"]}
    by_slug["zab-spare-1"]["status"] = {"bridge": "unreachable"}  # running, can't be reached
    by_slug["alice"]["status"]["doctor_failures"] = ["claude: not signed in"]
    order = [row["slug"] for row in sorted(ws["vms"], key=pages.rank)]
    assert order == ["zab-spare-1", "alice", "zed", "bob", "carol"]
    html = pages.workshop("harvard-2026", ws, [], NOW, user="Drew")
    shown = re.findall(r'href="/w/harvard-2026/vm/([^"]+)"', html)
    assert shown == order
    assert '<a class="problem" href="/w/harvard-2026/vm/zab-spare-1">' in html
    assert '<a class="strain" href="/w/harvard-2026/vm/zed">' in html


def test_bars_in_the_colours_of_vm():
    """#204's thresholds, on the whole percent: green below 70, yellow below 80, orange below
    90, red from 90; any use above zero fills a cell."""
    for fraction, cls in ((0, "w0 green"), (0.001, "w1 green"), (0.694, "w7 green"),
                          (0.695, "w7 yellow"), (0.8, "w8 orange"), (0.9, "w9 red"),
                          (1.7, "w10 red")):  # fmt: skip
        assert f'class="fill {cls}"' in pages.bar(fraction, "x"), fraction
    cpu, mem, disk = pages.resources(harvard(json.loads(GOLDEN.read_text()))["vms"][0])
    assert "0.5 of 8" in cpu and "w1 green" in cpu
    assert "8.0 of 32 GB" in mem and "w3 green" in mem
    assert '"num">40 of 97 GB (42%)</span>' in disk and "w4 green" in disk  # binary, #552
    assert pages.resources({"status": {"load": {"load1": 1, "cpus": 0}}}) == ("?", "?", "?")
    ws = harvard(json.loads(GOLDEN.read_text()))  # the fleet's row and the VM's page (#552)
    assert disk in pages.workshop("harvard-2026", ws, [], NOW, user="Drew")
    assert disk in pages.vm("harvard-2026", ws, "alice", NOW, user="Drew")


def disk_of(**status) -> str:
    return pages.resources({"state": "running", "status": status})[2]


def test_the_disk_is_its_use_of_its_size_in_binary_gb():
    """#552: `152 of 194 GB (78%)`, in binary GB as `df -h` gives the sizes (rounded up), with
    `df`'s own percentage and its bar. A read without the sizes (one made before #552), or with
    sizes that aren't `df`'s, shows the percentage alone, as before. The fleet's 200 GiB root:
    203056560 KiB, 193.6 GiB."""
    disk = {"size_bytes": 203_056_560 * 1024, "used_bytes": 151 * 2**30 + 1,
            "available_bytes": 32 * 2**30}  # fmt: skip
    shown = disk_of(disk_percent=78, disk=disk)
    assert shown.endswith('<span class="num">152 of 194 GB (78%)</span>') and "w8 yellow" in shown
    small = {"size_bytes": 83 * 2**30 // 10, "used_bytes": 2**30 - 1, "available_bytes": 7 * 2**30}
    assert disk_of(disk_percent=12, disk=small).endswith('"num">1.0 of 8.3 GB (12%)</span>')
    alone = disk_of(disk_percent=78)
    assert alone.endswith('<span class="num">78%</span>') and "w8 yellow" in alone
    for odd in (None, "194 GB", [1, 2, 3], {}, {**disk, "size_bytes": 0},
                {**disk, "used_bytes": disk["size_bytes"] + 1}, {**disk, "available_bytes": -1},
                {**disk, "used_bytes": 1.5e11}, {**disk, "size_bytes": True}):  # fmt: skip
        assert disk_of(disk_percent=78, disk=odd) == alone, odd
    assert disk_of(disk=disk) == "?"  # no percentage: no bar, as before
    page = pages.vm("w", {"vms": [{"slug": "alice", "state": "running",
                                   "status": {"disk_percent": 78, "disk": disk}}]},
                    "alice", NOW, user="Olga")  # fmt: skip
    assert "<td>Disk</td><td>" in page and "152 of 194 GB (78%)" in page


def test_the_fleet_totals(snapshot):
    ws = harvard(snapshot)
    ws["vms"][1]["status"] = {"codex_login": "ok", "claude_login": "ok", "zab_version": "0.2.5",
                              "idle_stop": {"enabled": False}}  # fmt: skip
    found = dict(pages.totals(ws["vms"]))
    assert found["VMs"] == "1 no VM, 2 running, 1 stopped"
    assert found["Spares"] == "1 running"  # #715: apart, as the cost line counts them
    assert found["Signed in"] == "1 Codex only, 1 both, 2 unknown"
    assert found["zab versions"] == "1 0.2.5, 1 0.2.6"
    assert found["Doctor failures"] == "1"  # alice; carol has no VM, whatever her last read
    assert found["Idle stop on"] == "1" and found["Waker opted in"] == "1"


def test_a_vms_page_and_its_commands(snapshot):
    ws = harvard(snapshot)
    html = pages.vm("harvard-2026", ws, "alice", NOW, user="Olga")
    assert "<code>!status alice</code>" in html and "<code>!stop alice</code>" in html
    assert '<a href="https://zulip.example.com/#narrow/channel/42">' in html
    assert "i-a · m7a.2xlarge" in html and "Termination protection</td><td>on" in html
    assert "busy: 1 session" in html and "7 min ago by !status" in html
    bob = pages.vm("harvard-2026", ws, "bob", NOW, user="Olga")
    assert "<code>!start bob</code>" in bob and "!stop bob" not in bob
    assert "<code>!launch carol</code>" in pages.vm("harvard-2026", ws, "carol", NOW, user="O")
    assert pages.vm("harvard-2026", ws, "nobody", NOW, user="Olga") is None
    zed = pages.vm("harvard-2026", ws, "zed", NOW, user="Olga")  # unlisted (Codex on #495)
    assert "<code>!" not in zed and "No command can name this VM" in zed
    spare = pages.vm("harvard-2026", ws, "zab-spare-1", NOW, user="Olga")
    assert "<code>!start zab-spare-1</code>" in spare and "!status zab-spare-1" not in spare
    del ws["site"]
    assert "the workshop's organizers channel" in pages.vm("harvard-2026", ws, "alice", NOW,
                                                           user="Olga")  # fmt: skip


def test_the_sentinel_of_each_vm(snapshot):
    """#588: status.py's `sentinel`, in the fleet's table and on the VM's page; a value it
    doesn't write (the VM's own text, or none from a read before #588) is `?`."""
    ws = harvard(snapshot)
    status = ws["vms"][0]["status"]
    assert status["sentinel"] == "ok"
    row = re.search(r"<tr><td>Sentinel</td><td>([^<]*)</td></tr>",
                    pages.vm("harvard-2026", ws, "alice", NOW, user="O"))  # fmt: skip
    assert row and row[1] == "ok"
    assert "<th>Doctor</th><th>Sentinel</th><th>Last activity</th>" in pages.workshop(
        "harvard-2026", ws, records(), NOW, user="O"
    )
    down = " (DM !restart is unavailable, and the bridge answers DM !vm itself)"
    for value, shown in (
        ("stale, no mark", "stale, no mark" + down),
        ("down", "down" + down),
        ("unknown", "unknown"),  # the read couldn't ask systemd: nothing known to be down
        ("warn", "warn"),
        ("unknown, stale", "unknown, stale" + down),
        (EVIL, "?"),
        ("ok, down", "?"),
        (None, "?"),
    ):
        status["sentinel"] = value
        html = pages.vm("harvard-2026", ws, "alice", NOW, user="O")
        assert f"<tr><td>Sentinel</td><td>{shown}</td></tr>" in html, value
    del status["sentinel"]
    assert pages.sentinel(status) == "?"


def test_the_admin_side_knows_every_state_the_sentinel_has():
    """#588's review: the console's copy of the sentinel's states (`zab.admin.pages` doesn't
    import `zab.tools.sentinel`) is the sentinel's, and status.py's `warn`; each, and a join of
    two, shows as it is, and those in which no sentinel answers say so."""
    from zab.commands import doctor
    from zab.tools import sentinel

    assert pages.SENTINEL_STATES == (*sentinel.UNHEALTHY, "warn")
    assert pages.SENTINEL_DOWN_STATES == sentinel.DOWN
    for state in pages.SENTINEL_STATES:
        for value in (state, f"{state}, stale", f"down, {state}"):
            assert pages.sentinel({"sentinel": value}) == value
    assert [s for s in pages.SENTINEL_STATES if pages.sentinel_down(s)] == list(sentinel.DOWN)
    assert pages.SENTINEL_DOWN == doctor.SENTINEL_DOWN  # the doctor's words


def test_the_index_and_the_actions(snapshot):
    html = pages.index(snapshot, ["harvard-2026"], NOW, user="Olga")
    assert '<a href="/w/harvard-2026/">harvard-2026</a>' in html and "zab-test" not in html
    assert "<td>2 of 3 · spares 1 of 1</td>" in html  # running, of the VMs (carol has none)
    every = records(60) + records(5, workshop="zab-test")
    shown = pages.actions("harvard-2026", every, NOW, user="Olga")
    plans = re.findall(r"<td>(P-\d+)</td>", shown)
    assert plans == [f"P-{n}" for n in range(60, 10, -1)]  # the last 50, newest first
    page = pages.workshop("harvard-2026", harvard(snapshot), every, NOW, user="Olga")
    assert re.findall(r"<td>(P-\d+)</td>", page)[:11] == ["P-7"] + [
        f"P-{n}" for n in range(60, 50, -1)
    ]  # the open plan, then the last 10 actions


def test_cost_is_shown_as_estimates(snapshot):
    ws = harvard(snapshot)
    html = pages.workshop("harvard-2026", ws, [], NOW, user="Drew")
    said = "$9 this month, $57 projected (estimates) · 2 of 3 running · spares: 1 of 1 running"
    assert said in html  # the spares apart (#715), as !status's cost line has them
    ws["cost"]["unpriced"] = ["x9.huge"]
    shown = pages.workshop("harvard-2026", ws, [], NOW, user="Drew")
    assert "the dollars leave out every VM that ran as, or is now, x9.huge (no price)" in shown
    # #499: a region without prices, as the collector writes it: no dollars, the region named
    ws["cost"] |= {"month": None, "projected": None, "unpriced": [], "unpriced_region": "eu-west-1"}
    shown = pages.workshop("harvard-2026", ws, [], NOW, user="Drew")
    assert "? this month, ? projected (estimates) · 2 of 3 running · spares: 1 of 1" in shown
    assert "running hours today · no prices for eu-west-1</p>" in shown


def test_ages():
    assert pages.age(NOW - 30, NOW) == "just now"
    assert pages.age("2027-01-15T07:55:00Z", NOW) == "7 min ago"
    assert pages.age(NOW - 5 * 3600, NOW) == "5 h ago"
    assert pages.age(NOW - 3 * 86400, NOW) == "3 days ago"
    assert pages.age(None, NOW) == pages.age("nonsense", NOW) == "?"


def test_every_class_the_pages_use_is_in_the_stylesheet(snapshot):
    css = CSS.read_text()
    used = set()
    for html in every_page(copy.deepcopy(snapshot)):
        for value in re.findall(r'class="([^"]*)"', html):
            used.update(value.split())
    used.update(f"w{n}" for n in range(11))
    missing = sorted(c for c in used if not re.search(rf"\.{c}\b", css))
    assert missing == []


@pytest.mark.parametrize("tag", ["Jane Doe", "../actions", "Zed", "a" * 70])
def test_a_slug_no_route_takes_is_shown_unlinked(snapshot, tag):
    """The review of #495: an unlisted VM's slug is its raw EC2 tag; a link to it would 404, or
    name another page."""
    ws = harvard(snapshot)
    ws["vms"][3]["slug"] = tag  # zed, unlisted
    html = pages.workshop("harvard-2026", ws, [], NOW, user="Drew")
    escaped = pages._e(tag)
    assert f"/vm/{escaped}" not in html and f'<span class="">{escaped}</span>' in html
    assert "no command can name it" in html
    assert '<a class="" href="/w/harvard-2026/vm/zab-spare-1">' in html  # a spare, linked


def test_a_read_before_the_boot_ranks_nothing(snapshot):
    """The review of #495: a read that found the VM unreachable, or strained, before its
    current boot doesn't rank it as unreachable or strained, or count it so in the totals;
    what a boot keeps (doctor failures, sign-ins, version) still counts (round 2)."""
    ws = harvard(snapshot)
    alice = ws["vms"][0]  # running since 07:00
    alice["status"] |= {"bridge": "unreachable", "strained": ["mem"], "doctor_failures": [],
                        "at": 1_800_000_000.0 - 3 * 86400}  # fmt: skip
    assert pages.rank(alice)[0] == 3
    found = dict(pages.totals(ws["vms"]))
    assert found["Unreachable"] == "0" and found["Under strain"] == "0"
    assert "before its last start: its reachability and strain" in pages.vm(
        "harvard-2026", ws, "alice", NOW, user="D"
    )
    alice["status"]["doctor_failures"] = ["claude: not signed in"]  # a boot keeps it
    assert pages.rank(alice)[0] == 1 and dict(pages.totals(ws["vms"]))["Doctor failures"] == "1"
    alice["status"]["doctor_failures"] = []
    alice["status"]["at"] = 1_800_000_000.0 - 60  # since the boot: it counts
    assert pages.rank(alice)[0] == 0
    alice["status"]["tried"] = {"at": 1_800_000_000.0 - 3 * 86400, "bridge": "unreachable"}
    alice["status"]["bridge"] = "active"
    assert pages.rank(alice)[0] == 2  # an old try counts no more than an old read


MALFORMED = [
    {"vms": "x"}, {"vms": [1, "x", None]}, {"cost": "x"}, {"ec2": "x"}, {"plans": "x"},
    {"plans": [{"targets": "abc"}]}, {"site": 3, "channels": "x"},
    {"vms": [{"slug": "alice", "state": "running", "status": [1], "waker": "x", "cost": "x"}]},
    {"vms": [{"slug": "alice", "state": "running", "status": {"tried": "x", "idle_stop": 3,
              "load": "x", "memory": [], "doctor_failures": "x"}}]},
    {"vms": [{"state": "running"}]}, "not a section", None,
]  # fmt: skip


@pytest.mark.parametrize("section", MALFORMED)
def test_a_malformed_section_degrades(section):
    """The review of #495: whatever the snapshot holds, the pages render."""
    records = [1, "x", {"workshop": "harvard-2026", "people": "x", "slugs": "abc", "seq": 1}]
    for html in (
        pages.workshop("harvard-2026", section, records, NOW, user="Drew"),
        pages.actions("harvard-2026", records, NOW, user="Drew"),
        pages.index({"workshops": {"harvard-2026": section}}, ["harvard-2026"], NOW, user="D"),
        pages.vm("harvard-2026", section, "alice", NOW, user="Drew") or "",
    ):
        assert isinstance(html, str)


@pytest.mark.parametrize("site", ["javascript:alert(1)//", "http://zulip.example.com",
                                  "https://zulip.example.com/x", "https://a b"])  # fmt: skip
def test_only_an_https_site_is_linked(snapshot, site):
    """The review of #495: the collector writes only https sites; the page checks again."""
    ws = harvard(snapshot) | {"site": site}
    html = pages.vm("harvard-2026", ws, "alice", NOW, user="Drew")
    assert "#narrow/channel" not in html and "javascript:" not in html.lower()
    assert "the workshop's organizers channel" in html


def test_an_old_ec2_read_is_a_warning(snapshot):
    """The review of #495: the collector reads EC2 every minute; an old read, or none, is said
    at the top of the page, and a missing or old snapshot on the index."""
    ws = harvard(snapshot)
    fresh = pages.workshop("harvard-2026", ws, [], NOW, user="Drew")
    assert 'class="warn"' not in fresh
    later = NOW + pages.STALE + 60
    old = pages.workshop("harvard-2026", ws, [], later, user="Drew")
    assert '<p class="warn">EC2 was last read 8 min ago: the states below may be' in old
    ws["ec2"] = {"at": None, "error": "UnauthorizedOperation"}
    never = pages.workshop("harvard-2026", ws, [], NOW, user="Drew")
    assert "EC2 hasn&#x27;t been read yet (the last read failed: UnauthorizedOperation)" in never
    assert 'class="warn"' not in pages.index(snapshot, ["harvard-2026"], NOW, user="D")
    assert "There is no snapshot yet" in pages.index({}, ["harvard-2026"], NOW, user="D")
    assert "The snapshot is from" in pages.index(snapshot, ["harvard-2026"], later, user="D")
