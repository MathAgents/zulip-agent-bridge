"""`!status`'s answer (#439): the plans' table and a few readable lines, never the JSON.

The reads are `status.py`'s own `fleet_json`, as `--json` prints it, over rows such as it
gathers (`test_admin_plan_fit.vm_rows`), or from EC2's state alone for a stopped VM."""

import json
import re

import pytest

from zab.admin import planning
from zab.admin.planning import ATTACH, Planning, status_reply
from zab.admin.scrub import Scrubber

from .botkit import HARVARD, OLGA, incoming, make_site
from .test_admin_plan_fit import JWT, SPLIT, STATUS, vm_rows

TABLE = "VM     state   zab    idle stop  running turns"


def read(*rows) -> str:
    return json.dumps(STATUS.fleet_json(list(rows)), indent=2) + "\n"


def reply(*rows, limit=10000):
    return status_reply(read(*rows), Scrubber(), limit=limit)


def alice():
    (vm,) = vm_rows(["alice"], 1)  # a Claude turn running since 2027-01-15 08:00 UTC
    vm.bridge, vm.claude_login, vm.zab_version = "active", "no", "0.2.4"
    verdict = "2026-10-18T14:00:00Z stay up: zab active 3 min ago"
    vm.idle_stop = {"enabled": True, "active": True, "minutes": 60, "last_verdict": verdict}
    return vm


def down(slug, bridge, last_error=""):
    """A row as status.py writes it from EC2's state (`gather_or_down`), or SSH's failure."""
    return STATUS.VmStatus(slug=slug, host="198.51.100.9", bridge=bridge, last_error=last_error)


def test_one_running_vm_gets_its_table_row_and_a_few_lines():
    answer = reply(alice())
    assert answer.full is None
    assert answer.text == "\n".join([
        "```text",
        TABLE,
        "alice  active  0.2.4  60 min     1 claude",
        "```",
        "- Codex signed in, Claude not signed in",
        "- last activity 2027-01-15 07:50 UTC",
        "- running: claude since 2027-01-15 08:00 UTC",
        "- load 0.1 of 8 CPUs, memory 2.6 of 32 GB, disk 23%",  # binary GB, as `!vm` says them
        "- idle stop's last verdict: `2026-10-18T14:00:00Z stay up: zab active 3 min ago`",
    ])  # fmt: skip
    assert '"schema"' not in answer.text and "{" not in answer.text


def test_a_stopped_vm_is_its_row_alone_and_an_unreachable_one_says_why():
    assert reply(down("carol", "stopped")).text == "\n".join(
        ["```text", "VM     state    zab  idle stop  running turns",
         "carol  stopped  ?    ?          ?", "```"]
    )  # fmt: skip
    text = reply(down("erin", "unreachable", "ssh: connect to host 198.51.100.9 port 22: x")).text
    assert text.endswith("\n```\n- last error: `ssh: connect to host 198.51.100.9 port 22: x`")


def test_one_vm_with_doctor_failures_lists_them_and_its_last_error_once():
    vm = alice()
    vm.bridge = "active, doctor FAIL"
    vm.doctor_failures = [f"zulip:claude: {n} 401 Unauthorized" for n in range(5)]
    vm.last_error = STATUS.first_line(vm.doctor_failures[0], 20)  # status.py's 80, shorter
    lines = reply(vm).text.split("\n")
    assert lines[-4:] == [
        "- doctor: `zulip:claude: 0 401 Unauthorized`",
        "- doctor: `zulip:claude: 1 401 Unauthorized`",
        "- doctor: `zulip:claude: 2 401 Unauthorized`",
        "- and 2 more doctor failures",
    ]
    vm.last_error = "status: database is locked"  # not the doctor's: shown too
    assert reply(vm).text.endswith("\n- last error: `status: database is locked`")


def test_one_vms_turns_are_listed_three_at_most():
    vm = alice()
    vm.running_turns = vm.running_turns * 4 + [{"bot": "codex", "backend": "codex"}]
    expected = "- running: " + ", ".join(["claude since 2027-01-15 08:00 UTC"] * 3) + ", and 2 more"
    assert expected in reply(vm).text.split("\n")
    assert "1 codex, 4 claude" not in reply(vm).text and "4 claude, 1 codex" in reply(vm).text


def test_several_vms_get_a_line_each_with_something_to_look_at_and_the_summary():
    a, b, d = alice(), *vm_rows(["bob", "dave"], 0)
    b.bridge, b.last_error = "active, doctor FAIL", "zulip:claude: 401 Unauthorized"
    b.doctor_failures = ["zulip:claude: 401 Unauthorized", "codex:connection: daemon down"]
    d.load, d.mem, d.disk = "9.7/8 !", "29/31G !", "94% !"  # under strain
    rows = [a, b, down("carol", "stopped"), d, down("erin", "unreachable", "ssh: timed out")]
    answer = reply(*rows)
    assert answer.full is None
    assert answer.text == "\n".join([
        "```text",
        "VM     state                    zab    idle stop  running turns",
        "alice  active                   0.2.4  60 min     1 claude",
        "bob    active, doctor FAIL      0.2.3  15 min     0",
        "carol  stopped                  ?      ?          ?",
        "dave   active, sign-in pending  0.2.3  15 min     0",
        "erin   unreachable              ?      ?          ?",
        "```",
        "- **bob**: doctor: `zulip:claude: 401 Unauthorized` (and 1 more)",
        "- **dave**: under strain: load, memory, disk",
        "- **erin**: last error: `ssh: timed out`",
        "",  # ends the list
        "signed in: 2 both, 1 Codex only, 0 Claude only, 0 neither; 1 unreachable, 1 stopped, "
        "1 doctor FAIL",
    ])  # fmt: skip
    assert answer.text.split("\n")[-1] == STATUS.summary_line(rows)  # status.py's own words


def test_the_sentinel_is_a_line_of_one_vm_and_flags_one_of_several():
    """#588: status.py's `sentinel`; `?` (a zab before #588) says nothing, and of several VMs
    only one whose sentinel isn't `ok` gets a line for it."""
    vm = alice()
    lines = reply(vm).text.split("\n")
    assert not any("sentinel" in line for line in lines)  # `?`
    vm.sentinel = "ok"
    assert reply(vm).text.split("\n")[4:6] == [
        "- Codex signed in, Claude not signed in",
        "- sentinel ok",
    ]
    vm.sentinel = "stale, no mark"
    down = "DM !restart is unavailable, and the bridge answers DM !vm itself"
    assert f"- sentinel stale, no mark: {down}" in reply(vm).text
    vm.sentinel = "unknown"  # the read couldn't ask systemd: nothing known to be down
    assert "- sentinel unknown" in reply(vm).text.split("\n")
    a, b = alice(), *vm_rows(["bob"], 0)
    a.sentinel, b.sentinel = "ok", "down"
    assert reply(a, b).text.split("\n")[5:6] == ["- **bob**: sentinel down"]
    b.sentinel = "rm -rf ~"  # the VM's own text: not a state
    assert "sentinel" not in reply(a, b).text


#: `df -Pk /` on a VM with 15 GB (14.9 GiB) left of its 200 GiB volume: 92% as `df` counts
#: it, used / (used + available), rounded up (#552).
FULL = "/dev/root 203056560 178000000 15600000 92% /"


def full_disk(vm):
    """`vm` as status.py reads it from `FULL`: its disk column and the numbers of --json."""
    vm.disk = STATUS.load_columns(FULL)["disk"]
    vm.metrics = {**vm.metrics, **STATUS.load_numbers(FULL)}
    return vm


def test_a_disk_under_strain_names_its_free_space():
    """#552: `!status` keeps the disk a percentage, and names the free space of a disk under
    strain (status.py's ` !`, 90% full), in binary GB as the console has them; a read without
    the sizes (an older status.py's) names none, as before."""
    assert STATUS.load_columns(FULL)["disk"] == "92% ! (15 GB free)"  # status.py's table
    line = reply(full_disk(alice())).text.split("\n")[7]
    assert line == "- load 0.1 of 8 CPUs, memory 2.6 of 32 GB, disk 92% (15 GB free) " + (
        "(under strain: disk)"
    )
    roomy = alice()
    roomy.metrics = {**roomy.metrics, **STATUS.load_numbers(FULL.replace("92%", "88%"))}
    assert "- load 0.1 of 8 CPUs, memory 2.6 of 32 GB, disk 88%" in reply(roomy).text.split("\n")
    a, d = vm_rows(["alice", "dave"], 0)
    d.load = "9.7/8 !"
    text = reply(a, full_disk(d)).text
    assert "\n- **dave**: under strain: load, disk (15 GB free)\n" in text
    for key in ("disk_size_kib", "disk_used_kib", "disk_available_kib"):
        del d.metrics[key]  # the percentage alone, as a read before #552 has it
    assert "\n- **dave**: under strain: load, disk\n" in reply(a, d).text


def test_a_vms_own_text_never_renders():
    """What a VM printed is scrubbed again, on one line, cut, in a code span no backtick run
    closes; a name that isn't a slug or a backend's, a time that isn't status.py's, a number
    that isn't finite are left out or `?`. So nothing a VM says can mention or format."""
    vm = alice()
    vm.last_error = "status: @**all** ``` *x*\n<b>" + "y" * 200
    vm.running_turns = [{"bot": "c", "backend": "@**all**", "started_at": "yesterday"}]
    vm.last_activity = None
    doc = STATUS.fleet_json([vm])
    doc["vms"][0]["slug"] = "@**all**"
    doc["vms"][0]["load"] = {"load1": float("nan"), "cpus": 8}
    text = status_reply(json.dumps(doc), Scrubber(), limit=10000).text
    assert "- running: ?\n" in text and "last activity" not in text and "load " not in text
    last = text.split("\n")[-1]
    # the scrubber breaks a mention with U+200B too; the span holds it whatever it says
    assert re.fullmatch(
        r"- last error: (`{4,})status: @\u200b?\*\*all\*\* ``` \*x\* <b>y+…\1", last
    )
    assert len(last) < planning.DETAIL_CHARS + 30
    doc["vms"].append(doc["vms"][0])
    text = status_reply(json.dumps(doc), Scrubber(), limit=10000).text
    assert re.search(r"\n- (`+)@\u200b?\*\*all\*\*\1: last error: ", text)  # a span, not bold


@pytest.mark.parametrize("hidden", ["split", "after a tab"])
@pytest.mark.parametrize("field", ["last_verdict", "doctor_failures", "last_error"])
def test_a_token_a_vm_hid_is_redacted_in_the_lines(field, hidden):
    """As the plan's status read (#380): each value is scrubbed again once decoded."""
    value, tail = (SPLIT, "Z" * 30) if hidden == "split" else ("\t" + JWT, JWT[-8:])
    vm = alice()
    if field == "last_verdict":
        vm.idle_stop = {**vm.idle_stop, "last_verdict": f"stopped? {value}"}
    elif field == "doctor_failures":
        vm.doctor_failures = [f"codex:connection: {value}"]
    else:
        vm.last_error = f"status: {value}"
    scrubber = Scrubber()
    for rows in ([vm], [vm, vm]):  # one VM's lines, or a line each (no idle-stop verdict)
        answer = status_reply(scrubber.json_text(read(*rows)), scrubber, limit=10000)
        assert tail not in answer.text, (field, answer.text)
        assert ("[redacted" in answer.text) is (len(rows) == 1 or field != "last_verdict")


def test_a_long_answer_is_cut_whole_lines_and_attached():
    """The table is cut by its line cap, the lines under it by the room. At 5,000 characters
    the table's 50 lines, about 2,500 characters, leave the cap well inside the table's three
    quarters of the room; at 4,000 they had about a row to spare (#764)."""
    rows = vm_rows([f"vm-{n:03d}" for n in range(60)], 0)
    for vm in rows:
        vm.bridge, vm.last_error = "active, doctor FAIL", "zulip:claude: 401 " + "u" * 100
        vm.doctor_failures = [vm.last_error]
    answer = reply(*rows, limit=5000)
    text = answer.text
    table = text.split("```")[1].split("\n")[1:-1]
    assert len(table) == planning.STATUS_LINES == 50 and table[0].startswith("VM  ")
    details = [line for line in text.split("\n") if line.startswith("- ")]
    assert 0 < len(details) < planning.DETAIL_LINES  # by room, not by count
    assert all(line.count("`") == 2 for line in details)  # whole lines, every span closed
    left = 60 - len(details)
    assert text.endswith(f"*Cut to fit: 11 lines of the table, {left} lines under it left out. "
                         f"{ATTACH}*")  # fmt: skip
    assert len(text) <= 5000 - planning.MARGIN
    full = answer.full
    assert full is not None and full.count("- **vm-") == 60 and "vm-059  active" in full
    assert "\nsigned in: 60 both, 0 Codex only" in full
    assert json.loads(full.split("The status read, as status.py printed it:\n", 1)[1])


def test_one_line_left_out_is_one_line():
    """Review of #453: `1 line of the table`, not `1 lines`."""
    text = reply(*vm_rows([f"vm-{n:02d}" for n in range(50)], 0)).text  # 51 lines of table
    assert text.endswith(f"*Cut to fit: 1 line of the table left out. {ATTACH}*")


def workshop_rows(n):
    """`n` VMs as wide as a real workshop's: 25-character slugs, `active, sign-in pending`, a
    two-digit patch release, and one VM with turns of both agents."""
    rows = vm_rows([f"vm-{i:03d}-{'x' * 18}" for i in range(n)], 0)
    for vm in rows:
        vm.zab_version = "0.2.16"
    rows[0].running_turns = [
        {"bot": b, "backend": b, "started_at": "2027-01-15T08:00:00Z"} for b in ("claude", "codex")
    ]
    return rows


def test_a_workshop_of_49_vms_shows_its_table_whole():
    """#764: 50 lines, header included, so that a workshop of about 40 VMs isn't cut; within
    Zulip's 10,000 characters with room to spare."""
    rows = workshop_rows(49)
    assert len(rows[0].slug) == 25
    answer = reply(*rows)
    assert answer.full is None and "Cut to fit" not in answer.text
    table = answer.text.split("```")[1].split("\n")[1:-1]
    assert len(table) == 50 and table[0].startswith("VM  ")
    assert [line.split()[0] for line in table[1:]] == [vm.slug for vm in rows]
    assert table[1].endswith("  active, sign-in pending  0.2.16  15 min     1 claude, 1 codex")
    assert len(answer.text) < 5000


@pytest.mark.parametrize(("vms", "left"), [(50, "1 line"), (60, "11 lines")])
def test_a_workshop_of_50_vms_or_more_is_cut_at_50_lines(vms, left):
    """#764: the header and 49 rows; the note names the lines left out, and the attachment
    holds every row. The cap cuts it, not the room: the message is far below Zulip's limit."""
    rows = workshop_rows(vms)
    answer = reply(*rows)
    table = answer.text.split("```")[1].split("\n")[1:-1]
    assert len(table) == 50 and table[0].startswith("VM  ")
    assert [line.split()[0] for line in table[1:]] == [vm.slug for vm in rows[:49]]
    assert answer.text.endswith(f"*Cut to fit: {left} of the table left out. {ATTACH}*")
    assert len(answer.text) < 5000
    full = answer.full
    assert full is not None
    assert all(f"\n{vm.slug}  active, sign-in pending  0.2.16" in full for vm in rows)


def test_a_read_without_an_answer_is_shown_fenced_as_it_is():
    answer = status_reply("Traceback (most recent call last):\n  boom\n── x exited 1\n",
                          Scrubber(), limit=10000)  # fmt: skip
    assert answer.text == "```text\nTraceback (most recent call last):\n  boom\n── x exited 1\n```"


class Reads:
    """The runner's `!status` read, faked: status.py's answer for alice, stopped bob."""

    def __init__(self, text):
        self.text = text

    async def read(self, action, fleet):
        return self.text

    async def prepare(self, plan, action, fleet):
        raise AssertionError("no plan here")

    def busy(self, workshop):
        return None

    async def apply(self, job):
        raise AssertionError("no plan here")


async def test_the_bot_answers_status_with_the_table(tmp_path):
    """Through the bot's own planner: one read, the table, no JSON; cut, attached."""
    site = make_site(tmp_path)
    runner = Reads(read(alice(), down("bob", "stopped")))
    server = site.server(planner=Planning(runner))
    await server.start()
    harvard = next(b for b in server.bots if b.user_id == HARVARD)
    try:
        await harvard.on_message(incoming(OLGA, "!status all", mid=5000))
        while server.planner.tasks:
            await next(iter(server.planner.tasks))
        text = site.realm.posts(HARVARD)[-1]["content"]
        assert text.startswith("```text\nVM     state    zab    idle stop  running turns\n")
        assert "\nbob    stopped  ?      ?          ?\n```\n\nsigned in: 0 both, 1 Codex" in text
        assert '"schema"' not in text and text.split("\n")[-1].startswith("`audit #")
        assert not site.realm.uploads
        runner.text = read(*vm_rows([f"vm-{n}" for n in range(55)], 0))  # 56 lines, 50 shown
        await harvard.on_message(incoming(OLGA, "!status all", mid=5001))
        while server.planner.tasks:
            await next(iter(server.planner.tasks))
        text = site.realm.posts(HARVARD)[-1]["content"]
        assert "*Cut to fit: 6 lines of the table left out. The whole status: [status.txt](" in text
        (attached,) = [v for k, v in site.realm.uploads.items() if k.endswith("status.txt")]
        assert b"vm-54  active" in attached and b'"schema": 1' in attached
    finally:
        await server.close()
