"""What the installed zab offers, asked of the build itself (reviewer on #286), and what the
agents' instructions installed with it tell them (#448).

A scenario skips only when its feature is not installed, never because the feature misbehaved:
these probes import the installed package with its own Python (the `uv tool` environment
behind `~agent/.local/bin/zab`) and print one fact. A probe that can't run raises: an unknown
build is an error, not a skip. Fixed code, run as `agent`, with no input but the snippet.
"""

from __future__ import annotations

import re

#: The installed zab's Python: the tool environment `~/.local/bin/zab` links to, else uv's
#: default tool directory. $1 is a fixed snippet of this module.
PROBE = r"""for py in "$(dirname "$(readlink -f /home/agent/.local/bin/zab)")/python" \
    /home/agent/.local/share/uv/tools/zulip-agent-bridge/bin/python; do
  if [ -x "$py" ]; then exec "$py" -c "$1"; fi
done
echo "no installed zab" >&2
exit 127
"""

VERSION = "import zab; print(zab.__version__)"
#: `!vm` (#204) at all, and its redesign (#278-#281: a line per resource, `VM: CPU …%`)
HAS_VM = "import importlib.util as u; print(u.find_spec('zab.tools.vmstat') is not None)"
VM_LINES = "import zab.tools.vmstat as v; print(hasattr(v, 'resources'))"
#: ... with CPU use in CPUs, never a percentage (#441: `1.4 of 8 CPUs`, `2.1 CPUs`)
VM_CPUS = "import zab.tools.vmstat as v; print(hasattr(v, 'cpus_text'))"
#: `!tools` (#475): the VM's list of its software, answered by the bridge
HAS_TOOLS = "import importlib.util as u; print(u.find_spec('zab.core.software') is not None)"
#: `zab sentinel`'s unit, which `zab service install` writes (#204): DM `!vm` and `!restart`
#: are then the sentinel's
HAS_SENTINEL_UNIT = "from zab.commands import service as s; print(hasattr(s, 'SENTINEL'))"
#: ... and `zab doctor`'s `<bot>:sentinel` checks (#588)
HAS_SENTINEL_CHECK = "from zab.commands import doctor as d; print(hasattr(d, 'sentinel_checks'))"
#: Commands as people type them (#709): `!Login`, "`!login`", a leading mention in a DM
HAS_COMMAND_TEXT = (
    "import importlib.util as u; print(u.find_spec('zab.core.command_text') is not None)"
)
#: `!fork` (#202): the help text lists it once the command exists
HAS_FORK = "from zab.core import commands as c; print('!fork' in c.HELP)"
#: ... and its report back to the parent topic and the parent's next input (#202 step 4)
HAS_FORK_REPORT = "import zab.core.peer_context as p; print(hasattr(p, 'fork_context'))"
#: ... and one `!fork` that both bots get forks both into one topic (#712)
HAS_FORK_BOTH = "import zab.core.fork as f; print(hasattr(f, 'fork_of_message'))"
#: `!btw` (#716): the help text lists it once the command exists, and each backend answers
#: side questions once it implements them
HAS_BTW = "from zab.core import commands as c; print('!btw' in c.HELP)"
SIDE_QUESTIONS = {
    "claude": "from zab.backends.claude import ClaudeBackend as B; "
    "print(getattr(B.capabilities, 'side_question', False))",
    "codex": "from zab.backends.codex import CodexBackend as B; print(hasattr(B, 'side_question'))",
}
#: file-server links to a rendered source file keep their `#L` line anchor (#448)
LINE_ANCHORS = "import zab.render.links as l; print(hasattr(l, 'SERVED_LINE_EXT'))"
#: `zab share` (#710): share links, which the file server serves once it is updated too.
HAS_SHARE = "import importlib.util as u; print(u.find_spec('zab.tools.share') is not None)"

#: The agents' instructions, as push_config installs them with `agent_instructions.py`: the
#: organizers' block of this file, between its markers (`START`, `END` there).
INSTRUCTIONS = "/home/agent/.codex/AGENTS.md"
MANAGED = re.compile(r"<!-- zab:managed:start -->\n(.*?)<!-- zab:managed:end -->", re.S)
#: #448's line of `workshop/ami/files/agent-instructions.md`: files render on the file server,
#: and a line can be linked
FILES_RENDER = re.compile(r"Markdown renders as on GitHub.*?#L\d", re.S)


def ask(vm, snippet: str) -> str:
    """The installed zab's answer to `snippet`; RuntimeError when it can't be asked."""
    argv = ["sudo", "-u", "agent", "-H", "bash", "-c", PROBE, "bash", snippet]
    res = vm.run(argv, check=False, timeout=120)
    if not res.ok:
        raise RuntimeError(f"cannot ask the installed zab: {(res.stderr or res.stdout)[-200:]}")
    return res.stdout.strip()


def has(vm, snippet: str) -> bool:
    answer = ask(vm, snippet)
    if answer not in ("True", "False"):
        raise RuntimeError(f"unexpected answer from the installed zab: {answer[:100]!r}")
    return answer == "True"


def instructions(vm) -> str:
    """The organizers' block of the installed instructions, "" when the file has none;
    RuntimeError when it can't be read."""
    argv = ["sudo", "-u", "agent", "-H", "cat", "--", INSTRUCTIONS]
    res = vm.run(argv, check=False, timeout=60)
    if not res.ok:
        raise RuntimeError(f"cannot read {INSTRUCTIONS}: {(res.stderr or res.stdout)[-200:]}")
    # Without markers the file is the AMI's copy of the organizers' text (workshop/ami/build.sh),
    # which push_config has not yet replaced: no rollout's instructions, so "" (not told). An
    # e2e deploy always runs push_config, and the wheel probe of `files_render` sees the build.
    block = MANAGED.search(res.stdout)
    return block.group(1) if block else ""


def files_render(vm) -> list[str]:
    """What the installed build tells participants about the file server's rendering (#448):
    its instructions say files render, its bridge links lines there; empty when it predates
    both."""
    told = []
    if FILES_RENDER.search(instructions(vm)):
        told.append("the agents' instructions say files render")
    if has(vm, LINE_ANCHORS):
        told.append("the bridge links lines (#L) there")
    return told
