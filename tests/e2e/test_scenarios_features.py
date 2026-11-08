"""Scenarios 6, 8, 9 and 12 of issue #263: `!vm`/`!usage`, Lean, the file server, history and
`!fork`. Each first asks what is installed (the zab build itself, `harness/installed.py`; the
shared Mathlib; the file server's `Vary`) and skips, naming the issue, only when the feature
isn't: a feature that is installed but misbehaves fails. `run.py --require rc` turns every
skip into a failure (a release candidate). #204, #205, #256, #248 and #202 land separately."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from dataclasses import dataclass

import pytest

from e2e.harness import files, github, installed
from e2e.harness.cleanup import VM_STOPPED, WARM_DISABLED, undo, vm_up
from e2e.harness.flows import DONE, WORKING_S, Chat, answer, echo, topic_name
from e2e.harness.vm import AGENT_HOME
from e2e.harness.zulip_user import topic_narrow

pytestmark = pytest.mark.e2e
UNKNOWN = r"Unknown command"


@dataclass(frozen=True)
class VmFormat:
    """What `!vm` and `!usage` print, as patterns that must all be found in the reply."""

    vm: tuple[str, ...]
    usage: str
    dm: tuple[str, ...] = ()  # also in a DM's reply, which lists the processes using the most


#: Before the redesign: `**ws-x** · load 0.4 / 0.6 / 0.7 (8 CPUs) · memory 3.1 of 30 GiB in
#: use, 27 available · … · disk 41 of 200 GiB (21%) · up 2 h`, then `bridge 0.2.2 · 1 turn
#: running · Claude signed in · …`; `!usage`: `VM: load 0.4/8 · memory 3/31 GiB · disk 41%`.
OLD = VmFormat(
    vm=(
        r"load [\d.]+ / [\d.]+ / [\d.]+ \(\d+ CPUs\) · memory [\d.]+ of [\d.]+ GiB in use, "
        r"[\d.]+ available · .*disk [\d.]+ of [\d.]+ GiB \(\d+%\)",
        r"bridge \S+ · \d+ turns? running · (Claude|Codex) .* · (Claude|Codex) ",
    ),
    usage=r"VM: load [\d.]+/\d+ · memory [\d.]+/[\d.]+ GiB · disk \d+%",
)
#: The redesign (#278-#281, as at #281's 7368c97: `vmstat.resources`): one line per resource,
#: the bar first, a line that couldn't be read says so; e.g. `🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜ 18% of 8 CPUs`,
#: `… 32% of 31 GB memory`, `… 46% of 97 GB disk` (`… disk at `/path`` for another file
#: system), `memory: unavailable`; then `1 turn running · up 2 hours · Codex and Claude signed
#: in · zab 0.2.1`; `!usage`: `VM: CPU 18% · memory 32% · disk 46%`. The lines' fields are
#: matched strictly, their place loosely (the host's line may still move).
LINES = VmFormat(
    vm=(
        r"(?m)^(\S+ \d+% of \d+ CPUs?|\d+ CPUs?: unavailable|CPU: unavailable)$",
        r"(?m)^(\S+ \d+% of \d+ GB memory|memory: unavailable)$",
        r"(?m)^(\S+ \d+% of \d+ GB disk|disk: unavailable)$",
        r"(?m)^(no turns running|\d+ turns? running) · up .+ · .*(signed in|unavailable).* · "
        r"zab \d\S*$",
    ),
    usage=r"VM: CPU (\d+%|unavailable) · memory (\d+%|unavailable) · disk (\d+%|unavailable)",
)
#: #441 (Drew, Oct 18): CPU use in CPUs, never a percentage: `🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜ 1.4 of 8 CPUs`,
#: `Using the most: `sage` 3.2 GB memory, 2.1 CPUs · …` in a DM, and `!usage`'s
#: `VM: 1.4 of 8 CPUs · memory 32% · disk 46%`. The rest as in `LINES`.
PROCESS = r"`[^`\n]*` [\d.]+ [MG]B memory, \d+\.\d CPUs"
CPUS = VmFormat(
    vm=(r"(?m)^(\S+ \d+\.\d of \d+ CPUs?|\d+ CPUs?: unavailable|CPU: unavailable)$", *LINES.vm[1:]),
    usage=r"VM: (\d+\.\d of \d+ CPUs?|CPU unavailable) · memory (\d+%|unavailable) · disk "
    r"(\d+%|unavailable)",
    dm=(rf"(?m)^Using the most: (unavailable|{PROCESS}( · {PROCESS})*)$",),
)


#: `zab sentinel`'s DM `!vm` (#204), once the build installs `zab-sentinel.service`: the same
#: lines, the processes using the most, and its own status line, which starts with the
#: bridge's state and has neither turns nor sign-in (`zab.tools.sentinel.Sentinel.bridge_line`),
#: e.g. `bridge running, last Zulip event 40 seconds ago · up 2 hours · zab 0.2.9`.
SENTINEL_STATUS = (
    r"(?m)^bridge (running|starting|stopping|not running|state unknown)[^·\n]*"
    r"( · up [^·\n]+)? · zab \d\S*$"
)
SENTINEL_VM = VmFormat(vm=(*CPUS.vm[:3], SENTINEL_STATUS), usage=CPUS.usage, dm=CPUS.dm)


def vm_format(vm) -> VmFormat:
    """The format of the installed build, asked of the build itself."""
    if not installed.has(vm, installed.VM_LINES):
        return OLD
    return CPUS if installed.has(vm, installed.VM_CPUS) else LINES


def check_vm(text: str, fmt: VmFormat, *, dm: bool = False) -> None:
    missing = [p for p in fmt.vm + (fmt.dm if dm else ()) if not re.search(p, text)]
    assert not missing, f"!vm lacks {missing[0]!r}: {text[:400]}"


#: Where #205's shared Mathlib is on the VM, read from the VM (#258/#259): the AMI workspace's
#: `.lake/packages` is a link into `/opt/lean/packages/<toolchain tag>`. Prints the target.
SHARED_PROBE = r"""p=$(readlink -f /home/agent/lean/workspace/.lake/packages 2>/dev/null) || exit 1
case "$p" in /opt/lean/packages/*) if [ -d "$p" ]; then echo "$p"; exit 0; fi ;; esac
exit 1
"""


def require_shared_mathlib(vm, scenario) -> str:
    """The shared Mathlib's directory, from the VM itself (`ZAB_E2E_LEAN_SHARED` may name it
    instead); skipped without one, which `--require 8` (an rc run) turns into a failure. Asked
    as `agent`: `ubuntu` can't enter /home/agent (0750; reviewer on #286)."""
    override = os.environ.get("ZAB_E2E_LEAN_SHARED")
    if override:
        found = override if vm.run(as_agent("test", "-d", override), check=False).ok else ""
    else:
        res = vm.run(as_agent("bash", "-c", SHARED_PROBE), check=False)
        found = res.stdout.strip() if res.ok else ""
    if not found:
        pytest.skip("no shared Mathlib (#205): ~/lean/workspace/.lake/packages is not a link "
                    "into /opt/lean/packages")  # fmt: skip
    scenario.notes.append(f"shared Mathlib: {found}")
    return found


AGENT_PATH = "PATH=/home/agent/.elan/bin:/usr/local/bin:/usr/bin:/bin"
#: `lake env lean FILE` from the project root that holds FILE, as agent; prints nothing on success.
LEAN_CHECK = r'cd "$1" && exec timeout 600 lake env lean "$2"'
#: A small Lean project for scenario 8 when the repository has none (#263 needs "a committed
#: Lean project"), in the directory $2 of the checkout $1: the AMI workspace's toolchain,
#: manifest and lakefile (so it pins the shared Mathlib), its default target switched to the
#: one library `E2eLean`, whose file holds a theorem; the workspace's own `Workspace` library
#: stays declared but unbuilt, its sources being left behind (codex on #286: `lake build`
#: failed on them). Committed as agent on the checkout's branch and pushed (reviewer on #286).
SEED = r"""set -eu
repo="$1"; dir="$2"; ws=/home/agent/lean/workspace
cd "$repo"
branch=$(git symbolic-ref --quiet --short HEAD)
mkdir -p "$dir"
cp "$ws/lean-toolchain" "$ws/lake-manifest.json" "$dir/"
echo 'defaultTargets = ["E2eLean"]' > "$dir/lakefile.toml"
grep -v '^defaultTargets[[:space:]]*=' "$ws/lakefile.toml" >> "$dir/lakefile.toml"
printf '\n[[lean_lib]]\nname = "E2eLean"\n' >> "$dir/lakefile.toml"
printf 'import Mathlib\n\ntheorem e2e_two : (1 : Nat) + 1 = 2 := rfl\n' > "$dir/E2eLean.lean"
git add -- "$dir"
git commit -q -m "e2e: a small Lean project for scenario 8 (issue #263)" -- "$dir"
git push -q origin "HEAD:$branch"
"""
SEED_DIR = "e2e-lean"


def work(rig) -> str:
    return f"/home/agent/work/{rig.repo.split('/')[-1]}"


def as_agent(*argv: str) -> list[str]:
    """`argv` run as `agent`, from its home (`vm.AGENT_HOME`): SSH lands in ubuntu's, which
    `agent` can't enter, and `find -exec` fails there before it runs anything."""
    return ["sudo", "-u", "agent", "-H", "env", "-C", AGENT_HOME, AGENT_PATH, *argv]


def megabytes(vm, path: str) -> int:
    out = vm.run(["sudo", "du", "-sm", path], check=False).stdout.split()
    return int(out[0]) if out and out[0].isdigit() else 0


# ---------------------------------------------------------------------------- 6: !vm, !usage


@pytest.mark.scenario(6)
async def test_vm_and_usage(chat: Chat, vm, scenario, nonce):
    """`!vm` in a DM and in a topic, and `!usage`'s `VM:` line, in the format of the installed
    build (asked of the build itself: the redesign of #278-#281 has `vmstat.resources`)."""
    if not installed.has(vm, installed.HAS_VM):
        pytest.skip("the installed zab has no !vm (#204)")
    fmt = vm_format(vm)
    name = (
        "per-resource, in CPUs" if fmt is CPUS else "per-resource" if fmt is LINES else "one-line"
    )
    scenario.notes.append(f"zab {installed.ask(vm, installed.VERSION)}: the {name} format")
    turn = await chat.dm("claude", "!vm")
    reply, took = await chat.reply(turn, r"\S", within=120)  # its first reply, whatever it is
    scenario.timing("DM !vm reply", took)
    if installed.has(vm, installed.HAS_SENTINEL_UNIT):  # the sentinel's (#204; scenario 15)
        scenario.notes.append("DM !vm: zab sentinel's reply")
        check_vm(reply["content"], SENTINEL_VM, dm=True)
    else:
        check_vm(reply["content"], fmt, dm=True)
    turn = await chat.say("codex", topic_name(6, nonce), "!vm")
    reply, took = await chat.reply(turn, r"\S", within=120)
    scenario.timing("topic !vm reply", took)
    check_vm(reply["content"], fmt)
    token = f"USAGE-{nonce}"
    turn = await chat.say("claude", topic_name(6, nonce, "usage"), echo(token))
    await chat.reply(turn, answer(token))
    usage = await chat.say("claude", topic_name(6, nonce, "usage"), "!usage", mention_bot=False)
    reply, _ = await chat.reply(usage, r"VM: ", within=120)
    assert re.search(fmt.usage, reply["content"]), reply["content"][-300:]
    await check_tools(chat, vm, scenario, nonce)


#: `!tools`' reply with a list (#475): the header, then at least one tool under its area
TOOLS_REPLY = r"^\*\*Software on this VM\*\*.*\n\n\*\*[^*\n]+\*\*\n- \*\*[^*\n]+\*\*"


async def check_tools(chat: Chat, vm, scenario, nonce: str) -> None:
    """`!tools` (#475) in a DM to Codex and in a session topic of Claude's, where the build has
    it: the bridge answers from the VM's list, which the rollout installs where the image has
    none, and the agents' instructions list the same tools."""
    if not installed.has(vm, installed.HAS_TOOLS):
        scenario.notes.append("the installed zab has no !tools (#475)")
        return
    turn = await chat.dm("codex", "!tools")
    reply, took = await chat.reply(turn, r"\S", within=120)
    scenario.timing("DM !tools reply", took)
    assert re.search(TOOLS_REPLY, reply["content"], re.S), reply["content"][:400]
    tools = await chat.say("claude", topic_name(6, nonce, "usage"), "!tools", mention_bot=False)
    reply, took = await chat.reply(tools, r"\S", within=120)  # the missing-list sentence too
    scenario.timing("topic !tools reply", took)
    assert re.search(TOOLS_REPLY, reply["content"], re.S), reply["content"][:400]
    assert "## Software on this VM" in installed.instructions(vm)


# ------------------------------------------------------------------------------------ 8: Lean


@pytest.mark.scenario(8)
@pytest.mark.parametrize("kind", ("claude", "codex"))
async def test_lean_proof(kind, chat: Chat, rig, vm, scenario, nonce):
    """ "Prove Fermat's little theorem in Lean" in a new topic: the check compiles in under 30 s
    (measured again here: `lake env lean` on the agent's file, a proxy for the tool time) and
    the worktrees grow by less than 50 MB (the shared Mathlib of #205, no private copy)."""
    require_shared_mathlib(vm, scenario)
    trees = f"{work(rig)}/.zab/worktrees"
    before = megabytes(vm, trees)
    name = f"Fermat{nonce.replace('-', '')}{kind.capitalize()}"
    token = f"LEAN-OK-{nonce}"
    turn = await chat.say(
        kind,
        topic_name(8, nonce, kind),
        f"Prove Fermat's little theorem in Lean, in a file `{name}.lean`, and check that it "
        f"compiles. When it does, reply with `{token}` and the file's path.",
    )
    _, took = await chat.reply(turn, answer(token), within=1200)
    scenario.timing(f"{kind}: the turn", took)
    grown = megabytes(vm, trees) - before
    scenario.notes.append(f"{kind}: the worktrees grew by {grown} MB")
    assert grown < 50, f"{kind}: the worktrees grew by {grown} MB (a private Mathlib?)"
    # no download of Mathlib: the turn's actions (its status lists the last 50) fetched nothing
    actions = " ".join(m["content"] for m in await chat.statuses(turn))
    fetched = [c for c in ("cache get", "lake update") if c in actions]
    assert not fetched, f"{kind}: the turn ran {', '.join(fetched)}"
    found = vm.run(as_agent("find", trees, "-name", f"{name}.lean"), check=False).stdout.split()
    assert found, f"{name}.lean is not in any topic worktree"
    root = vm.run(as_agent("bash", "-c", 'd=$(dirname "$1"); while [ "$d" != / ] && '
                           '! [ -e "$d/lean-toolchain" ]; do d=$(dirname "$d"); done; echo "$d"',
                           "bash", found[0])).stdout.strip()  # fmt: skip
    if root == "/":
        # no project above the file: a one-off proof, which the agent instructions check from
        # the scratch workspace (`lake env lean File.lean` in ~/lean/workspace)
        root = "/home/agent/lean/workspace"
        scenario.notes.append(f"{kind}: the file is in no project; checked in ~/lean/workspace")
    start = time.monotonic()
    checked = vm.run(as_agent("bash", "-c", LEAN_CHECK, "bash", root, found[0]), timeout=700,
                     check=False)  # fmt: skip
    assert checked.ok, f"lake env lean failed: {(checked.stderr or checked.stdout)[-300:]}"
    # a PROXY for the agent's tool time, which Zulip doesn't show: the agent's file compiled
    # again here, warm (accepted as such; #286)
    proxy = f"{kind}: tool time, PROXY (the file recompiled here)"
    scenario.timing(proxy, time.monotonic() - start, high=30)


@pytest.mark.scenario(8)
async def test_lean_committed_project(chat: Chat, rig, vm, scenario, nonce):
    """With a Lean project committed in the repository, a new topic builds it without any
    download: its worktree grows by less than 50 MB (no Mathlib of its own). When the
    repository has none, the scenario commits one itself (`SEED`) on the checkout's branch
    (main) and pushes it to the repository before the topic starts, so that the new topic's
    worktree has it from the start and #260's post-create hook links it (reviewer on #286)."""
    require_shared_mathlib(vm, scenario)
    listed = vm.run(as_agent("git", "-C", work(rig), "ls-files", "*lakefile.lean",
                             "*lakefile.toml"), check=False).stdout.split()  # fmt: skip
    if listed:
        project = os.path.dirname(listed[0]) or "."
    else:
        seeded = vm.run(as_agent("bash", "-c", SEED, "bash", work(rig), SEED_DIR), timeout=300,
                        check=False)  # fmt: skip
        assert seeded.ok, f"could not commit and push a Lean project: {seeded.stderr[-300:]}"
        scenario.notes.append(f"seeded {SEED_DIR}/ on main of {rig.repo}")
        project = SEED_DIR
    ask = f"Run `lake build` in `{project}`."
    trees = f"{work(rig)}/.zab/worktrees"
    before, token = megabytes(vm, trees), f"BUILT-{nonce}"
    turn = await chat.say(
        "claude",
        topic_name(8, nonce, "project"),
        f"{ask} If it succeeds, reply with exactly `{token}`.",
    )
    _, took = await chat.reply(turn, answer(token), within=1800)
    scenario.timing("committed project: the build turn", took)
    grown = megabytes(vm, trees) - before
    scenario.notes.append(f"committed project {project}: the worktrees grew by {grown} MB")
    assert grown < 50, f"the build downloaded or unpacked {grown} MB"


#: The page-cache warm-up after each boot (#461), as `push_config.py` installs it.
WARM_UNIT = "zab-warm-cache.service"
WARM_PROPS = ("LoadState", "UnitFileState", "ActiveState", "Result",
              "ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic")  # fmt: skip
#: How long the cold-import part waits, after the import, for the warm-up to finish.
WARM_WAIT_S = 600
#: How the script's journal lines start (`warm_cache.py`): its summary is the last of them.
WARM_SUMMARY = "zab-warm-cache: "


def warm_state(vm) -> dict[str, str]:
    """The warm-up unit's properties, from `systemctl show` (`LoadState=not-found`: absent)."""
    argv = ["systemctl", "show", WARM_UNIT, *(f"--property={p}" for p in WARM_PROPS)]
    lines = vm.run(argv, check=False).stdout.splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def warm_phase(state: dict[str, str]) -> str:
    """Where this boot's warm-up is: `absent` (no unit: a build before #461), `running` (a
    oneshot is `activating` until its script exits), `done` or `failed` (its main process
    exited), `pending` (enabled, not started yet) or `disabled` (enabled for no boot)."""
    if state.get("LoadState") != "loaded":
        return "absent"
    if state.get("ActiveState") == "activating":
        return "running"
    if state.get("ActiveState") == "failed":
        return "failed"
    if int(state.get("ExecMainExitTimestampMonotonic") or 0) > 0:
        return "done" if state.get("Result") == "success" else "failed"
    return "pending" if state.get("UnitFileState") == "enabled" else "disabled"


async def warm_finished(vm, state: dict[str, str]) -> tuple[dict[str, str], str]:
    """The warm-up's state and phase once it is neither running nor pending, or after
    WARM_WAIT_S."""
    phase = warm_phase(state)
    deadline = time.monotonic() + WARM_WAIT_S
    while phase in ("running", "pending") and time.monotonic() < deadline:
        await asyncio.sleep(5)
        state = warm_state(vm)
        phase = warm_phase(state)
    return state, phase


async def record_warm_up(
    vm, scenario, state: dict[str, str], import_done: float, *, off: bool = False
) -> None:
    """The warm-up's part in the cold import: whether it was still running when the import
    ended, `import_done` seconds after boot; then, once it has finished, when that was and its
    journal line, which says what it read and how fast. One that fails, or that is installed
    but disabled without `off` (`ZAB_E2E_WARM=off`), fails the scenario."""
    phase = warm_phase(state)
    if phase == "absent" or (phase == "disabled" and off):
        why = "no unit on the VM" if phase == "absent" else "disabled (ZAB_E2E_WARM=off)"
        scenario.notes.append(f"warm-up (#461): {why}; the time above is without it")
        return
    assert phase != "disabled", "the warm-up unit is installed but not enabled"
    scenario.notes.append(f"warm-up (#461): {phase} when the import ended, "
                          f"{import_done:.0f} s after boot")  # fmt: skip
    state, phase = await warm_finished(vm, state)
    # `-u` also has systemd's own lines about the unit (Finished, Consumed …): the script's
    # summary is the last line it printed itself (Codex round 1)
    journal = ["sudo", "journalctl", "-b", "-u", WARM_UNIT, "-o", "cat", "--no-pager"]
    lines = vm.run(journal, check=False).stdout.splitlines()
    summary = next((ln for ln in reversed(lines) if ln.startswith(WARM_SUMMARY)), "")
    tail = " / ".join(lines[-3:])[-300:]
    late = f" after {WARM_WAIT_S} s" if phase in ("running", "pending") else ""
    assert phase == "done", f"the warm-up is {phase}{late}: {summary[-300:] or tail}"
    assert summary, f"the warm-up finished without its summary in the journal: {tail}"
    scenario.timing("warm-up done, after boot", int(state["ExecMainExitTimestampMonotonic"]) / 1e6)
    scenario.notes.append(summary[:400])


@pytest.mark.scenario(8)
@pytest.mark.slow
async def test_lean_cold_import(rig, ec2, instance_id, vm, scenario):
    """The first `import Mathlib` with a cold page cache, after a VM stop and start: timed and
    recorded. It stops the VM, so it runs with the slow group (`--scenarios slow` or `all`),
    never in a fast run.

    The import starts as soon as SSH answers, while #461's warm-up runs, as on a VM woken for a
    Lean question: the part records the time from the start request to the finished import,
    whether the warm-up was still running then, and when it finished (`record_warm_up`).
    `ZAB_E2E_WARM=off` disables the unit for this boot, to measure the time without it;
    `ZAB_E2E_WARM=wait` starts the import only once the warm-up has finished, to show whether
    it covers what `import Mathlib` reads (then close to a warm import: 4.9 s on the test VM)."""
    if not vm.run(as_agent("test", "-d", "/home/agent/lean/workspace"), check=False).ok:
        pytest.skip("no ~/lean/workspace on the VM")
    mode = os.environ.get("ZAB_E2E_WARM", "")
    assert mode in ("", "off", "wait"), f"ZAB_E2E_WARM={mode!r}: off, wait or unset"
    present = warm_phase(warm_state(vm)) != "absent"
    toggle, after = mode == "off" and present, mode == "wait" and present
    if toggle:
        vm.run(["sudo", "systemctl", "disable", "--quiet", WARM_UNIT])
    try:
        ec2.stop(instance_id)
        ec2.wait_state(instance_id, "stopped", timeout=600)
        ec2.start(instance_id)
        started = time.monotonic()
        ec2.wait_state(instance_id, "running", timeout=600, every=2)
        for _ in range(60):
            if vm.reachable():
                break
            await asyncio.sleep(5)
        if after:
            await warm_finished(vm, warm_state(vm))  # record_warm_up checks how it ended
        probe = "/tmp/zab_e2e_cold.lean"  # noqa: S108 - on the VM, written by agent, removed below
        vm.run(as_agent("bash", "-c", 'printf "import Mathlib\\n" > "$1"', "bash", probe))
        start = time.monotonic()
        done = vm.run(as_agent("bash", "-c", LEAN_CHECK, "bash", "/home/agent/lean/workspace",
                               probe), timeout=1800, check=False)  # fmt: skip
        ended = time.monotonic()
        after_boot = float(vm.run(["cat", "/proc/uptime"]).stdout.split()[0])
        state = warm_state(vm)
        vm.run(as_agent("rm", "-f", probe), check=False)
    finally:  # the VM up for the later scenarios, the unit enabled for the later boots (#517)
        down = f"{VM_STOPPED}; {WARM_DISABLED}" if toggle else VM_STOPPED  # both, if both
        await undo(down, lambda: vm_up(ec2, instance_id, vm), attempts=1)
        if toggle:
            enable = ["sudo", "systemctl", "enable", "--quiet", WARM_UNIT]
            await undo(WARM_DISABLED, lambda: vm.run(enable), stops=False)
    assert done.ok, (done.stderr or done.stdout)[-300:]
    scenario.timing("import Mathlib after the warm-up" if after else "cold import Mathlib",
                    ended - start)  # fmt: skip
    scenario.timing("start to import Mathlib done", ended - started)
    await record_warm_up(vm, scenario, state, after_boot, off=toggle)


# ----------------------------------------------------------------------------- 9: file server


async def check_line_link(text: str, rig, vm, scenario, source: str) -> None:
    """#448 end to end: the answer's `source:2` links to the file server with `#L2`, and the
    rendered page has the row `id="L2"`. A bridge that predates #448 is noted, not checked."""
    if not installed.has(vm, installed.LINE_ANCHORS):
        scenario.notes.append("line link not checked: the installed bridge predates #448")
        return
    lined = [u for u in files.links(text, rig.files_url, source, fragments=True)
             if u.endswith("#L2")]  # fmt: skip
    assert lined, f"no file-server link to {source} with #L2 in the answer: {text[:300]}"
    page = await files.fetch(lined[0].removesuffix("#L2"), rig.files_url)
    assert page.status == 200, f"{lined[0]}: HTTP {page.status}"
    assert page.html and '<tr id="L2">' in page.text, f"{lined[0]}: no row L2 on the page"
    scenario.notes.append(f"line link {lined[0]}")


@pytest.mark.scenario(9)
async def test_file_server(chat: Chat, rig, vm, scenario, nonce):
    """Markdown with math renders, code is highlighted, `?raw=1` returns the file (#256), and
    a line of a source file links to its row (#448). A host without the rendering fails the
    scenario once the installed build tells agents that files render (#448), so that build
    can't pass its suite before the host update; only a build that predates that skips."""
    name, source = f"notes-{nonce}.md", f"check-{nonce}.py"
    turn = await chat.say(
        "claude",
        topic_name(9, nonce),
        f"Create `{name}` with a heading, the formula `$e^{{i\\pi}} + 1 = 0$` and a fenced "
        f"Python code block, and `{source}` with three lines of Python. Commit both, and reply "
        f"with `{name}` and `{source}:2`, each in backticks.",
    )
    reply, _ = await chat.reply(turn, answer(re.escape(name)), within=900)
    links = files.links(reply["content"], rig.files_url, name)
    assert links, f"no file-server link to {name} in the answer: {reply['content'][:300]}"
    page = await files.fetch(links[0], rig.files_url)
    assert page.status == 200, f"{links[0]}: HTTP {page.status}"
    if not page.renderer:  # the deployed server, not the page, decides (reviewer on #286)
        missing = "the file server has no #256 rendering deployed (no Vary: Sec-Fetch-Dest)"
        told = installed.files_render(vm)
        if told:
            pytest.fail(f"{missing}, but {' and '.join(told)} (#448)")
        pytest.skip(missing)
    assert page.html, f"a browser navigating got {page.content_type}, not a rendered page"
    assert files.renders_math(page), "no KaTeX/MathJax on the rendered page"
    assert files.highlights_code(page), "the code block is not highlighted"
    raw = await files.fetch(files.raw_url(links[0]), rig.files_url)
    assert raw.status == 200 and not raw.html and "e^{i" in raw.text, raw.content_type
    scenario.notes.append(f"rendered {links[0]}")
    await check_line_link(reply["content"], rig, vm, scenario, source)


#: How long a revoked share may take to stop here: `zab share --revoke` asks the server to
#: check it at once (#710); the hourly check, the other bound, is too long for the suite.
REVOKED_WITHIN = 120
#: `zab share --list` (#710) as the agent runs it, in the topic's worktree: the one of the
#: checkout $1, or the checkout itself, that holds the shared file $2; $3 is the installed zab.
#: A fixed script.
SHARE_LIST = r"""for d in "$1" "$1"/.zab/worktrees/*; do
  if [ -e "$d/$2" ]; then cd "$d" && exec "$3" share --list; fi
done
echo "neither the checkout nor a worktree of it holds $2" >&2
exit 3
"""
ZAB = "/home/agent/.local/bin/zab"


def shares_listed(vm, rig, name: str) -> dict[str, str]:
    """The shares that `zab share --list` lists, where the topic made the file `name`: each
    one's commit → its paths. What proves `zab share` made a link (#775): a turn's status cuts
    its commands short, so a `zab share` after `&&` may not show there."""
    argv = as_agent("bash", "-c", SHARE_LIST, "bash", work(rig), name, ZAB)
    res = vm.run(argv, check=False)
    assert res.ok, f"zab share --list failed: {(res.stderr or res.stdout)[-300:]}"
    return files.listed_shares(res.stdout)


@pytest.mark.scenario(9)
async def test_share_link(chat: Chat, rig, vm, scenario, nonce):
    """#710: asked for a link to share one file, the agent runs `zab share`: `zab share --list`
    in the topic's worktree lists the share of that file alone that the answer links (#775).
    The link opens that file, and its root lists that file alone; asked to revoke it, the agent
    does, and the link answers 404 within `REVOKED_WITHIN` seconds (the server's recheck)."""
    if not installed.has(vm, installed.HAS_SHARE):
        pytest.skip("zab share is not in the installed release (#710)")
    if not await files.serves_shares(rig.files_url):  # as #256's rendering: a build that has
        pytest.fail(  # `zab share` can't pass before the files host serves shares
            f"{rig.files_url} has no share links (#710) deployed, and the installed zab has "
            "`zab share`: update the files host (workshop/files/README.md) first"
        )
    shared, private, topic = (
        f"shared-{nonce}.md",
        f"private-{nonce}.md",
        topic_name(9, nonce, "share"),
    )
    turn = await chat.say(
        "claude",
        topic,
        f"Create `{shared}` containing the line `SHARED-{nonce}`, and `{private}` containing "
        f"the line `PRIVATE-{nonce}`. Then make me a link to share `{shared}` alone with a "
        "colleague, and reply with that link.",
    )
    reply, took = await chat.reply(turn, answer(re.escape(shared)), within=600)
    scenario.timing("share link answer", took)
    made = shares_listed(vm, rig, shared)
    # the answer may also link the turn's snapshot of the file: the share is the link whose
    # commit `zab share --list` lists, as a share of that file alone
    links = files.links(reply["content"], rig.files_url, shared)
    shares = [u for u in links if made.get(files.commit_of(u) or "") == shared]
    assert shares, (
        f"no link in the answer is a share of {shared} alone that `zab share --list` lists "
        f"({len(made)} listed): {reply['content'][:400]}"
    )
    url = shares[0]
    root = await files.fetch(url[: -len(shared)], rig.files_url, navigate=False)
    assert root.status == 200, f"the share's root: HTTP {root.status}"
    # the root's listing links that file alone (the reviewer on #744)
    names = {
        href.rstrip("/").rsplit("/", 1)[-1] for href in re.findall(r'href="([^"]*)"', root.text)
    }
    assert names == {shared}, f"the share's root lists {sorted(names)}"
    page = await files.fetch(url, rig.files_url, navigate=False)
    assert page.status == 200 and f"SHARED-{nonce}" in page.text, f"{url}: HTTP {page.status}"
    scenario.notes.append(f"share link {url}: zab share --list lists it; its root, {shared} alone")
    turn = await chat.say(
        "claude", topic, f"Revoke that share link now, then reply with `REVOKED-{nonce}`."
    )
    await chat.reply(turn, answer(f"REVOKED-{nonce}"), within=300)
    started = time.monotonic()
    while (status := (await files.fetch(url, rig.files_url, navigate=False)).status) != 404:
        if time.monotonic() - started > REVOKED_WITHIN:
            pytest.fail(f"{url}: still HTTP {status} {REVOKED_WITHIN} s after the revoke")
        await asyncio.sleep(5)
    scenario.timing("revoked share stops", time.monotonic() - started, high=REVOKED_WITHIN)
    gone = await files.fetch(url[: -len(shared)], rig.files_url, navigate=False)
    assert gone.status == 404, f"the share's root still answers HTTP {gone.status}"


# ------------------------------------------------------------------- 12: history and !fork


@pytest.mark.scenario(12)
async def test_history(chat: Chat, vm, scenario, nonce):
    """An agent reads another topic with `zab history` (#248)."""
    help_ = vm.run(as_agent("/home/agent/.local/bin/zab", "history", "--help"), check=False)
    if not help_.ok:
        pytest.skip("zab history is not in the installed release (#248)")
    source, code = topic_name(12, nonce, "source"), f"orchid-{nonce}"
    turn = await chat.say("claude", source, f"The codeword is `{code}`. " + echo(f"NOTED-{nonce}"))
    await chat.reply(turn, answer(f"NOTED-{nonce}"))
    turn = await chat.say(
        "codex",
        topic_name(12, nonce, "reader"),
        f"Read the topic `{source}` of this channel with `zab history` and reply with the "
        "codeword it contains, nothing else.",
    )
    _, took = await chat.reply(turn, answer(re.escape(code)), within=600)
    scenario.timing("history answer", took)
    used = [m for m in await chat.statuses(turn) if "zab history" in m["content"]]
    assert used, "the turn did not run `zab history`"


#: The pointer `⑂ forked to #**channel>topic**` the bridge posts in the parent topic (#202).
FORK_POINTER = re.compile(r"^⑂ forked to #\*\*[^*>\n]+>([^*\n]+)\*\*")


async def fork_topic(chat: Chat, fork, parent: str, expected: str) -> str:
    """The `!fork` message's pointer in the parent topic, and the fork topic's card linking
    back to the parent: the fork topic's name."""
    reply, _ = await chat.reply(fork, rf"⑂ forked to|⚠️|{UNKNOWN}", within=180)
    match = FORK_POINTER.match(reply["content"])
    assert match, f"no fork pointer: {reply['content'][:300]}"
    assert match[1] == expected, f"fork topic {match[1]!r}, expected {expected!r}"
    card = await chat.user.wait_reply(
        topic_narrow(chat.stream, expected),
        after_id=fork.message_id,
        senders=[fork.bot],
        pattern=r"^\*\*zab session\*\* · ⑂ Forked from #\*\*[^*>\n]+>" + re.escape(parent),
        within=60,
    )
    assert card["subject"] == expected
    return expected


@pytest.mark.scenario(12)
@pytest.mark.parametrize("kind", ("claude", "codex"))
async def test_fork(kind, chat: Chat, rig, vm, scenario, nonce):
    """`!fork` (#202) with each agent. From an idle topic: the fork's topic and card, the
    pointer in the parent, and a fork that knows the parent's conversation and commits as the
    agent (for Codex, that `thread/fork`'s config reaches the fork's shells; DECISIONS
    2026-10-23); with step 4, the line in the parent linking to the fork's answer, and the
    parent's next turn knowing it. While the parent's turn runs: the fork answers in its own
    topic before the parent's turn ends, and that turn completes as it would have. Every message
    mentions the agent: one without a mention would reach the other agent too (#713), which
    would start a session of its own in the topic, and fork it, into another fork topic."""
    if not installed.has(vm, installed.HAS_FORK):  # the build decides, not the reply
        pytest.skip("the installed zab has no !fork (#202)")
    topic, code = topic_name(12, nonce, f"fork-{kind}"), f"orchid-{nonce}"
    turn = await chat.say(kind, topic, f"The codeword is `{code}`. " + echo(f"BASE-{nonce}"))
    await chat.reply(turn, answer(f"BASE-{nonce}"))

    # from an idle topic
    path, subject = f"e2e/fork-{kind}-{nonce}.txt", f"e2e fork {nonce}"
    idle = await chat.say(
        kind,
        topic,
        f"!fork --name idle Create the file `{path}` containing the codeword from earlier in "
        f"this conversation, commit it with the message `{subject}` without pushing, then reply "
        "with the full commit hash only.",
    )
    first = await fork_topic(chat, idle, topic, f"{topic} ⑂ idle")
    reply = await chat.user.wait_reply(
        topic_narrow(chat.stream, first),
        after_id=idle.message_id,
        senders=[idle.bot],
        pattern=answer(r"\b[0-9a-f]{40}\b"),
        within=600,
    )
    sha = re.findall(r"\b[0-9a-f]{40}\b", reply["content"])[-1]
    checkout = work(rig)  # the fork's worktree shares the checkout's objects
    content = vm.run(as_agent("git", "-C", checkout, "show", f"{sha}:{path}")).stdout
    assert code in content, f"the fork's file lacks the parent's codeword: {content[:200]!r}"
    show = ["git", "-C", checkout, "show", "-s", f"--format={github.GIT_SHOW_FORMAT}", sha]
    seen = github.parse_git_show(sha, vm.run(as_agent(*show)).stdout)
    assert seen.subject == subject, f"{sha[:10]} is {seen.subject!r}, not this run's"
    problems = seen.problems(rig.bots[kind])
    assert not problems, f"{kind} fork commit {sha[:10]}: " + "; ".join(problems)
    scenario.notes.append(f"{kind}: fork {first!r} committed {sha[:10]} as the agent")
    if installed.has(vm, installed.HAS_FORK_REPORT):  # step 4: the answer reaches the parent
        report = rf"^⑂ ` {re.escape(first)} ` answered: \S+/near/{reply['id']}$"
        await chat.reply(idle, report, within=120)
        ask = await chat.say(
            kind,
            topic,
            "A fork of this conversation has answered. Reply with exactly the full commit hash "
            "it gave, and nothing else. Don't run any tools.",
        )
        await chat.reply(ask, answer(sha), within=300)
        scenario.notes.append(f"{kind}: the parent topic got the fork's answer")
    else:
        scenario.notes.append(f"{kind}: no report back in this build (#202 step 4)")

    # while the parent's turn runs
    done = f"DONE-{nonce}"
    running = await chat.say(
        kind,
        topic,
        f"Run the shell command `sleep 150`. When it has finished, reply with exactly `{done}`.",
    )
    # its status shows the `sleep` at the first look after it started, a minute into the turn
    # at the earliest (#754): the sleep runs on, for the fork to answer in
    await chat.reply(running, r"^⏳ \*\*Working\*\*.*sleep", within=WORKING_S)
    side = await chat.say(kind, topic, "!fork " + echo(f"SIDE-{nonce}"))
    second = await fork_topic(chat, side, topic, f"{topic} ⑂ 1")
    found = await chat.user.wait_reply(
        topic_narrow(chat.stream, second),
        after_id=side.message_id,
        senders=[side.bot],
        pattern=answer(f"SIDE-{nonce}"),
        within=600,
    )
    scenario.timing(f"{kind}: fork's answer while the parent runs", time.monotonic() - side.sent_at)
    ended, took = await chat.reply(running, answer(re.escape(done)), within=600)
    scenario.timing(f"{kind}: parent's answer", took)
    assert found["id"] < ended["id"], "the fork answered only after the parent's turn ended"
    statuses = await chat.statuses(running)
    assert len(statuses) == 1, f"{len(statuses)} turns ran in the parent; the fork started one"
    await chat.user.settled(statuses[0], DONE)  # Zulip may serve an older version (#877)


#: A fork's card in its topic (#202), the first fork's or one that joined it (#712).
FORK_CARD = r"^\*\*zab session\*\* · ⑂ Forked from "


async def each_answer(chat: Chat, topic: str, after_id: int, token: str) -> list[dict]:
    """Each agent's answer `token` in `topic` after the message `after_id`, at once."""
    narrow = topic_narrow(chat.stream, topic)
    return list(
        await asyncio.gather(
            *(
                chat.user.wait_reply(
                    narrow, after_id=after_id, senders=[bot], pattern=answer(token), within=600
                )
                for bot in chat.bots.values()
            )
        )
    )


@pytest.mark.scenario(12)
async def test_fork_both(chat: Chat, scenario, vm, nonce):
    """#712: in a topic where both agents answer, a `!fork` without a mention forks both into
    one topic, `⑂ 1`: one pointer line in the parent, a card from each bot, an answer from
    each, and one branch for the two sessions. A `!fork` that mentions one agent forks that
    agent alone, into `⑂ 2`."""
    if not installed.has(vm, installed.HAS_FORK_BOTH):
        pytest.skip("the installed zab forks each agent into a topic of its own (#712)")
    topic = topic_name(12, nonce, "fork-both")
    narrow = topic_narrow(chat.stream, topic)
    base = await chat.user.send(chat.stream, topic, echo(f"PAIR-{nonce}"))
    await each_answer(chat, topic, base, f"PAIR-{nonce}")

    sent = time.monotonic()
    fork = await chat.user.send(chat.stream, topic, "!fork " + echo(f"BOTH-{nonce}"))
    both = f"{topic} ⑂ 1"
    await each_answer(chat, both, fork, f"BOTH-{nonce}")
    scenario.timing("both agents' answers in one fork topic", time.monotonic() - sent)
    bots = set(chat.bots.values())
    pointers = [
        m
        for m in await chat.user.after(narrow, fork)
        if m["sender_id"] in bots and FORK_POINTER.match(m["content"])
    ]
    assert [FORK_POINTER.match(m["content"])[1] for m in pointers] == [both], pointers
    cards = [
        m["sender_id"]
        for m in await chat.user.after(topic_narrow(chat.stream, both), fork)
        if re.match(FORK_CARD, m["content"])
    ]
    assert sorted(cards) == sorted(bots), f"the cards in {both!r} are from {cards}"
    branches = {}
    for kind in chat.bots:
        ask = await chat.say(kind, both, "!session")
        reply, _ = await chat.reply(ask, r"^\*\*Session details\*\*", within=120)
        found = re.search(r"\(worktree\) · branch `+ (\S+) `+", reply["content"])
        assert found, f"{kind}'s !session has no worktree branch: {reply['content'][:300]}"
        branches[kind] = found[1]
    assert len(set(branches.values())) == 1, f"the fork's two sessions: {branches}"
    scenario.notes.append(f"one !fork: both agents in {both!r}, on {branches['codex']}")

    alone = await chat.say("codex", topic, "!fork " + echo(f"ALONE-{nonce}"))
    second = await fork_topic(chat, alone, topic, f"{topic} ⑂ 2")
    await chat.user.wait_reply(
        topic_narrow(chat.stream, second),
        after_id=alone.message_id,
        senders=[alone.bot],
        pattern=answer(f"ALONE-{nonce}"),
        within=600,
    )
    others = await chat.user.silent(
        topic_narrow(chat.stream, second),
        after_id=alone.message_id,
        senders=[chat.bots["claude"]],
        seconds=5,
    )
    assert not others, f"Claude took part in a !fork that mentions Codex: {others[0]['content']}"
    scenario.notes.append(f"a mentioned !fork: Codex alone in {second!r}")


# ------------------------------------------------------------------------------- 12: !btw

#: A side answer (#716): every part starts with the marker, which links the `!btw` message.
BTW_ANSWER = r"^💬 \[btw\]\(\S+/near/{}\)"
#: Each worktree of the checkout, its HEAD and `git status`, and the branches: what a side
#: question must leave as it was. A fixed script; $1 is the checkout.
TREES = r"""git -C "$1" worktree list --porcelain | sed -n 's/^worktree //p' | while read -r w; do
  echo "== $w"; git -C "$w" rev-parse HEAD; git -C "$w" status --porcelain
done
git -C "$1" branch --list
"""


def trees(vm, rig) -> str:
    return vm.run(as_agent("bash", "-c", TREES, "bash", work(rig))).stdout


#: The Claude account's transcripts: a side question must add none (`--no-session-persistence`).
TRANSCRIPTS = r"""find /home/agent/.claude/projects -name '*.jsonl' 2>/dev/null | sort"""


def transcripts(vm) -> set[str]:
    return set(vm.run(as_agent("bash", "-c", TRANSCRIPTS)).stdout.split())


@pytest.mark.scenario(12)
@pytest.mark.parametrize("kind", ("claude", "codex"))
async def test_btw(kind, chat: Chat, rig, vm, scenario, nonce):
    """`!btw` (#716) with each agent, while a turn runs:
    - the side question is answered from the conversation (a codeword from an earlier turn)
      before the running turn ends, and that turn completes as it would have, as one turn;
    - the notes say whether the answer names the running `sleep` (Codex: whether 0.157.1's
      `thread/items/list` reads a running turn, the VERIFY of SPEC §8.10);
    - no worktree, HEAD or branch changed, and (Claude) no transcript was added;
    - a follow-up afterwards finds no trace of the side question in the conversation."""
    if not installed.has(vm, installed.HAS_BTW):  # the build decides, not the reply
        pytest.skip("the installed zab has no !btw (#716)")
    if not installed.has(vm, installed.SIDE_QUESTIONS[kind]):
        pytest.skip(f"the installed zab's {kind} backend answers no side questions (#716)")
    topic, code, side_word = topic_name(12, nonce, f"btw-{kind}"), f"heron-{nonce}", f"SIDE-{nonce}"
    turn = await chat.say(kind, topic, f"The codeword is `{code}`. " + echo(f"BASE-{nonce}"))
    await chat.reply(turn, answer(f"BASE-{nonce}"))

    done = f"DONE-{nonce}"
    running = await chat.say(
        kind,
        topic,
        f"Run the shell command `sleep 150`. When it has finished, reply with exactly `{done}`.",
        mention_bot=False,
    )
    # the `sleep` shows at the status's first look after it started (#754), and runs on
    await chat.reply(running, r"^⏳ \*\*Working\*\*.*sleep", within=WORKING_S)
    before = trees(vm, rig)
    kept = transcripts(vm) if kind == "claude" else set()
    side = await chat.say(
        kind,
        topic,
        f"!btw What is the codeword from earlier in this conversation, and which shell command "
        f"is the current turn running? Reply with both, then `{side_word}`.",
        mention_bot=False,
    )
    pattern = BTW_ANSWER.format(side.message_id) + r"|^⚠️ \*\*Bridge\*\*"
    found, side_took = await chat.reply(side, pattern, within=180)
    if not found["content"].startswith("💬"):  # a refusal or a failure: the bridge's view
        for event, line in side_log(vm).items():
            scenario.notes.append(f"{kind} btw, the bridge's {event}: {line}")
    assert found["content"].startswith("💬"), f"no side answer: {found['content'][:300]}"
    scenario.timing(f"{kind}: side answer while a turn runs", side_took)
    assert code in found["content"], f"the side answer lacks the codeword: {found['content'][:300]}"
    sees = "sleep" in found["content"]
    scenario.notes.append(f"{kind}: the side answer {'names' if sees else 'does not name'} sleep")
    assert trees(vm, rig) == before, "the side question changed a worktree, a HEAD or a branch"
    if kind == "claude":
        added = transcripts(vm) - kept
        assert not added, f"the side question saved a transcript: {sorted(added)}"
        scenario.notes.append(f"claude: {len(kept)} transcripts before and after the side answer")
    ended, took = await chat.reply(running, answer(re.escape(done)), within=600)
    scenario.timing(f"{kind}: the running turn's answer", took)
    assert found["id"] < ended["id"], "the side answer came only after the running turn ended"
    statuses = await chat.statuses(running)
    assert len(statuses) == 1, f"{len(statuses)} turns ran; the side question started one"
    await chat.user.settled(statuses[0], DONE)  # Zulip may serve an older version (#877)

    follow = await chat.say(
        kind,
        topic,
        "List every word in this conversation so far that starts with `SIDE-`, or reply "
        f"`NONE-{nonce}` if there is none. Don't run any tools.",
        mention_bot=False,
    )
    reply, _ = await chat.reply(follow, answer(rf"(?:NONE-{nonce}|SIDE-)"), within=300)
    assert side_word not in reply["content"], f"the side question is in the conversation: {reply}"
    assert trees(vm, rig) == before
    scenario.notes.append(f"{kind}: side answer in {side_took:.0f} s, out of the conversation")


#: The rollouts of the agent's Codex threads: a materialized fork would add one.
ROLLOUTS = r"""find /home/agent/.codex/sessions -name 'rollout-*.jsonl' 2>/dev/null | sort"""
#: Whether the file a side question was asked to write exists, in the checkout or a worktree.
WRITTEN = r"""for f in "$1/$2" "$1"/.zab/worktrees/*/"$2"; do
  [ -e "$f" ] && echo "$f"
done
true
"""
#: The bridge's line when a side question's Codex turn reached for a tool (`codex.py`).
BTW_TOOL = r"Codex reached for a tool \(` (\w+) `\)"


def lean_servers(vm) -> int:
    """The VM's `lean-lsp-mcp` processes: each loaded Codex thread with the MCP server on
    starts one, so a side question's fork, with its MCP servers off, must add none."""
    args = vm.run(["ps", "-eo", "args="]).stdout.splitlines()
    # the program itself, run directly or by its interpreter: not a command line naming it
    return sum(
        any(os.path.basename(word) == "lean-lsp-mcp" for word in line.split()[:2]) for line in args
    )


async def peak_lean_servers(vm, waiting: asyncio.Future) -> int:
    """The most `lean-lsp-mcp` processes seen while `waiting` runs, sampled about every 2 s."""
    peak = 0
    while True:
        peak = max(peak, await asyncio.to_thread(lean_servers, vm))
        if waiting.done():
            return peak
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(waiting), 2)


#: The bridge's own log, JSON lines (`zab.core.logging`): what a side question turned off.
BRIDGE_LOG = "/home/agent/.local/state/zab/logs/zab.log"
SIDE_LOG_EVENTS = ("side_question_config", "side_question_mcp")


def side_log(vm) -> dict[str, str]:
    """The newest `side_question_config` and `side_question_mcp` lines of the bridge's log
    (`CodexBackend._side_config`, `_check_side_mcp`): names and states only."""
    text = vm.run(as_agent("tail", "-n", "2000", BRIDGE_LOG), check=False).stdout
    found: dict[str, str] = {}
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            record = json.loads(line)
            if isinstance(record, dict) and record.get("event") in SIDE_LOG_EVENTS:
                found[record["event"]] = str(record.get("msg", ""))[:400]
    return found


@pytest.mark.scenario(12)
async def test_btw_codex_limits(chat: Chat, rig, vm, scenario, nonce):
    """The VERIFY of SPEC §8.10 (#716): what a side question's read-only, ephemeral Codex turn
    can still do, its MCP servers, plugins, apps and web search turned off by `thread/fork`'s
    `config`, and its MCP servers checked off for its thread (`mcpServerStatus/list`), else
    refused. Asked to run a command, to write a file, to run Lean code that writes a file
    (an MCP tool of `lean-lsp`), and to look something up in the LMFDB (an MCP server's tool),
    each side question is a side answer (no tool used) or the bridge's ⚠️ that it reached for
    one and was stopped. The notes record which, and the item kind, and the bridge's log of
    what `config/read` reported and what was turned off. Then:
    - neither MCP ask reached an `mcpToolCall`;
    - no file is written, by the sandboxed shell or by an MCP tool outside it;
    - while each MCP ask is answered, no more `lean-lsp-mcp` run than before (sampled), and
      none is left over;
    - no worktree, HEAD or branch changes;
    - no rollout is added (`ephemeral`);
    - the topic's next turn sees none of the questions.
    """
    if not installed.has(vm, installed.SIDE_QUESTIONS["codex"]):
        pytest.skip("the installed zab's codex backend answers no side questions (#716)")
    topic, path = topic_name(12, nonce, "btw-limits"), f"e2e/btw-{nonce}.txt"
    outside = f"/home/agent/zab-e2e-btw-mcp-{nonce}.txt"  # Lean's, outside the workspace
    turn = await chat.say("codex", topic, echo(f"BASE-{nonce}"))
    await chat.reply(turn, answer(f"BASE-{nonce}"))
    before, rollouts = trees(vm, rig), vm.run(as_agent("bash", "-c", ROLLOUTS)).stdout
    servers = lean_servers(vm)
    asks = {
        "command": "!btw Run the shell command `ls -a` in the repository and tell me what it "
        "printed.",
        "write": f"!btw Create the file `{path}` containing `{nonce}`, then say done.",
        "lean (MCP)": "!btw With the Lean tool `lean_run_code`, run "
        f'`#eval IO.FS.writeFile "{outside}" "{nonce}"`, then say done.',
        "lmfdb (MCP)": "!btw With the LMFDB tool, count the elliptic curves over Q of conductor "
        "11 in the LMFDB, and tell me the count.",
    }
    for what, text in asks.items():
        side = await chat.say("codex", topic, text, mention_bot=False)
        pattern = BTW_ANSWER.format(side.message_id) + r"|^⚠️ \*\*Bridge\*\*"
        reply = asyncio.ensure_future(chat.reply(side, pattern, within=180))
        peak = await peak_lean_servers(vm, reply) if "MCP" in what else None
        found, took = await reply
        tool = re.search(BTW_TOOL, found["content"])
        if tool:
            seen = f"stopped at a {tool[1]} item"
        elif found["content"].startswith("💬"):
            seen = "answered without a tool"
        else:
            seen = f"other: {found['content'][:200]!r}"
        peaks = "" if peak is None else f", lean-lsp-mcp at most {peak} (before: {servers})"
        scenario.notes.append(f"codex btw ({what}): {seen}, {took:.0f} s{peaks}")
        if not (tool or found["content"].startswith("💬")):  # a refusal: its servers' states
            for event, line in side_log(vm).items():
                scenario.notes.append(f"codex btw, the bridge's {event}: {line}")
        assert tool or found["content"].startswith("💬"), found["content"][:300]
        if peak is not None:
            assert not tool or tool[1] != "mcpToolCall", f"{what}: an MCP tool was called"
            assert peak <= servers, f"{what}: the fork started a lean-lsp-mcp ({peak} > {servers})"
    for event, line in side_log(vm).items():
        scenario.notes.append(f"codex btw, the bridge's {event}: {line}")
    written = vm.run(as_agent("bash", "-c", WRITTEN, "bash", work(rig), path)).stdout.strip()
    assert not written, f"a side question wrote {written}"
    escaped = vm.run(as_agent("test", "-e", outside), check=False).ok
    vm.run(as_agent("rm", "-f", "--", outside), check=False)  # the scenario's clean-up
    assert not escaped, f"a side question's MCP tool wrote {outside}, outside the sandbox"
    after = lean_servers(vm)
    scenario.notes.append(f"codex btw: lean-lsp-mcp processes {servers} before, {after} after")
    assert after <= servers, "a side question's fork left a lean-lsp-mcp behind"
    assert trees(vm, rig) == before, "a side question changed a worktree, a HEAD or a branch"
    added = set(vm.run(as_agent("bash", "-c", ROLLOUTS)).stdout.split()) - set(rollouts.split())
    scenario.notes.append(f"codex btw: {len(added)} rollout(s) added")
    assert not added, f"the ephemeral fork left rollouts: {sorted(added)}"
    follow = await chat.say(
        "codex",
        topic,
        "Did I ask you to run `ls -a`, to create a file, to run Lean code or to search the "
        f"LMFDB in this conversation? Reply with exactly `YES-{nonce}` or `NO-{nonce}`. Don't "
        "run any tools.",
        mention_bot=False,
    )
    reply, _ = await chat.reply(follow, answer(rf"(?:YES|NO)-{nonce}"), within=300)
    assert f"NO-{nonce}" in reply["content"], f"a side question is in the history: {reply}"
