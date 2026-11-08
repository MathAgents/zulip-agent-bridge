#!/usr/bin/env python3
"""`push_config.py --roster roster.csv (--tag vX | --wheel PATH)` (README.md §2).

Over SSH as `ubuntu`, for every VM in the roster: check that the VM's own record of its owner
(`/etc/zab-participant.json` and the hostname, written at first boot) names this row's
participant, upgrade zab as `agent`, install the two zuliprc files from
`workshop/admin/out/<slug>/` into `/home/agent/.config/zab/` (0600, owner `agent`), write
`config.toml` from the workshop profile (SPEC §5.3), with the workshop's `--mention-after-s`
(#711), then run `zab service install && zab doctor --service-env --json` as `agent` and
collect the result into one table. Each VM is handled
independently and failures never abort the others; the exit code is non-zero if any VM failed.
A VM whose only failing doctor checks are the agents' sign-ins (`codex:login`, `claude account
<name>`) is `ok (sign-in pending)`, not a failure: nobody can sign in before the workshop. On a row
with no `zulip_user_id` yet, whose participant hasn't joined Zulip (or whose address the bots
can't see), the bots' `<bot>:allowlist` checks fail as well, and are pending too: `ok
(owner+sign-in pending)` (#401; `admin_common.allowlist_pending`). A bound row's rollout writes
the ID, so a failing allowlist check there stays a failure. During a hand-over (`--adopt-spare
<slug>`, ORGANIZERS §7.2) the row's checkout `~/work/<repo>` doesn't exist yet: `provision_repos.py`
clones it next. With that flag, and only for that row, the doctor's `project:` check of exactly that
path is pending while nothing at all is there (`ok (project+… pending)`, #458); the run without
the flag that ends the hand-over checks it in full.
Every release rollout also installs `warm_cache.py` and `zab-warm-cache.service`, which read the
shared Mathlib into the page cache after each boot (issue #461; README.md §2.3), and Oscar's
files where it was used lately (#475).
`--uninstall` is the rollback of README.md §6 (`zab service uninstall`, and the warm-up unit
removed). `--idle-stop MINUTES` installs `idle_stop.py` and `zab-idle-stop.timer`, so that a kept VM
powers itself off when nobody uses it (issue #207; README.md §8), and the user `zab-monitor` that
status.py reads as, which idle stop doesn't count as a login (#507); `0` removes them all, and
records that idle stop was turned off on purpose; without `--tag`/`--wheel` only that step runs.
The defaults of a rollout that names neither (#704), with a release: `--lean-shared-default` does
what `--lean-shared` does on a VM whose Mathlib isn't shared yet, and only probes one that has it;
`--idle-stop-default MINUTES` turns idle stop on, last, on a VM with no timer whose waker is on
(`--has-waker`), unless its idle stop was turned off on purpose. A spare's own idle stop (#715,
`spare_idle_stop.py`) is never a row's: the default replaces it where the waker is on, and
removes it elsewhere, and a hand-over (`--adopt-spare`) without either idle-stop flag removes it.
`--restart-codex` also stops the old bridge and restarts the Codex daemon before the new bridge
starts, unless a Codex turn is running (reported, never a failure): the first rollout of the per-bot
git identity needs it (issue #214).
Before the instructions, every release rollout installs the Oct 9 image's list of its software as
`/etc/zab/tools.json` where the image has none (`tools_list.py`, #475). Before that list, it
replaces the Oct 9 image's `sage` link by the new images' wrapper, which puts the Sage
environment on PATH (`sage_wrapper.py`, #482); the list then says Sage's interfaces work. Both
stay after `--uninstall`.

Nothing is copied to a VM that does not say it belongs to the participant: VMs are addressed by the
roster's `ip` column (never the `ws-<slug>` alias), their host keys must be in the `known_hosts`
file that `scan_hosts.py` records, a roster with two rows sharing a slug, login, e-mail, host, IP or
Zulip id is refused whole, and a spare VM (`/etc/zab-participant.json` with `"spare"`) is only
accepted for a row named in `--adopt-spare`, which rewrites its record and hostname first
(ORGANIZERS §7.2). PM review of PR #47.

Every remote command is either a fixed literal or an argument vector joined with `shlex`
(`admin_common.remote_chain`), so roster text never reaches a shell unquoted (CONTRIBUTING.md,
rule 3). Standalone: `uv run python workshop/admin/push_config.py …`.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import shlex
import sys
import tempfile
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from admin_common import (
    DEFAULT_KNOWN_HOSTS,
    GITHUB_OWNER,
    IDLE_HOLD_OFF_CMD,
    IDLE_HOLD_ON_CMD,
    REFSERVE_OWNER,
    REFSERVE_URL,
    Runner,
    RunResult,
    allowlist_pending,
    first_line,
    format_table,
    git_identity,
    host_of,
    keys_dir,
    load_roster,
    owner_record,
    pending_label,
    remote_chain,
    run_parallel,
    scp_argv,
    select_rows,
    sign_in_pending,
    slug_of,
    ssh_argv,
    subprocess_runner,
    work_repo,
)
from aws_common import ZAB_REPO_URL
from idle_stop import LOG as IDLE_STOP_LOG
from idle_stop import READ_HOME, READ_LOGIN, READ_LOGIN_TEXT, READ_SCRIPT_PATH, READ_USER
from status import READ_SCRIPT

#: Where a `--tag` release installs from (`uv tool install git+URL@TAG`): the public
#: repository, whose tags are the releases (#593), unless `ZAB_REPO` names another, as
#: workshop/ami/build.sh takes it. A private deployment installs a `--wheel` instead.
REPO_URL = ZAB_REPO_URL


def repo_url() -> str:
    """`ZAB_REPO`, or `REPO_URL`: a URL that `uv tool install git+URL@TAG` takes, https:// or
    ssh:// (not scp-style `git@host:path`)."""
    return os.environ.get("ZAB_REPO") or REPO_URL


AGENT = "agent"
AGENT_HOME = "/home/agent"
ZAB_CONFIG_DIR = f"{AGENT_HOME}/.config/zab"
UV = f"{AGENT_HOME}/.local/bin/uv"  # installed for agent by workshop/ami/build.sh
CODEX_BINARY = f"{AGENT_HOME}/.local/bin/codex"  # build.sh installs the release binary there
CLAUDE_BINARY = f"{AGENT_HOME}/.local/bin/claude"  # the native installer's location
#: This checkout's root, for `git show <tag>:…` (`artifact_config`).
CHECKOUT = pathlib.Path(__file__).resolve().parents[2]
OUT_DIR = pathlib.Path(__file__).resolve().parent / "out"
#: `[bridge] mention_after_s` (SPEC §4.8; #711): a turn that ends after running this many
#: seconds mentions its owner, so the topic shows Zulip's @ in the left sidebar. The bridge's
#: default, written out; `--mention-after-s` takes the workshop's, from 0 (a turn of any length)
#: to a day, as admin.toml does. Every release push_config installs has the key: it came before
#: `allow_emails` (#92), which the config always holds.
MENTION_AFTER_S, MENTION_AFTER_MAX = 120, 86400
#: The agents' standing instructions (PM on issue #36, Mon 19:00): push_config, not the AMI, owns
#: `~/.codex/AGENTS.md` (a managed block from this text, then the participant's own section) and
#: `~/.claude/CLAUDE.md` (`@~/.codex/AGENTS.md`). The splice runs on the VM, as agent, in
#: `agent_instructions.py` (uploaded with the text; argument lists only).
INSTRUCTIONS_TEXT = (
    pathlib.Path(__file__).resolve().parents[1] / "ami" / "files" / "agent-instructions.md"
)
INSTRUCTIONS_SCRIPT = pathlib.Path(__file__).resolve().parent / "agent_instructions.py"
VM_PYTHON = "/usr/bin/python3"  # the system Python: the script needs only the standard library
#: The VM's list of its software, `/etc/zab/tools.json` (#475): `!tools` shows it and the
#: instructions' tools section is made from it. New images bring their own; for one without,
#: `tools_list.py install` (as root, before the instructions) installs the Oct 9 image's
#: list, less what the VM lacks. An image's own list is never replaced. The instructions step
#: imports the same script, so both are uploaded.
TOOLS_SCRIPT = pathlib.Path(__file__).resolve().parent / "tools_list.py"
TOOLS_LIST = pathlib.Path(__file__).resolve().parent / "tools-nt-agents-2026-v3.json"
#: The instructions step's last line when the VM's list can't be read (agent_instructions.py)
TOOLS_NONE = "tools: none in the instructions"
#: The `sage` wrapper (#482): new images install `ami/files/bin/sage` as `~agent/.local/bin/sage`
#: (#480), which puts the Sage environment's bin first on PATH; the Oct 9 image has a link to the
#: environment's `sage` there, and Sage's giac(), r() and gap() fail. Before the tools list,
#: `sage_wrapper.py install` (as agent) replaces exactly that link by the same file, and leaves
#: anything else alone; where the wrapper is in place afterwards, the list's SageMath entry says
#: that the interfaces work (`tools_list.py install --with sage-wrapper`). `--uninstall` leaves it.
SAGE_SCRIPT = pathlib.Path(__file__).resolve().parent / "sage_wrapper.py"
#: `apt_repair.py repair`, as root (#947): the image's expired Macaulay2 snapshot source removed,
#: so that the VM's `apt-get update` works again, and Macaulay2's packages held.
APT_SCRIPT = pathlib.Path(__file__).resolve().parent / "apt_repair.py"
SAGE_WRAPPER = pathlib.Path(__file__).resolve().parents[1] / "ami" / "files" / "bin" / "sage"
SAGE_FEATURE = "sage-wrapper"
#: The script's outcomes after which `~agent/.local/bin/sage` is the wrapper
SAGE_IN_PLACE = ("sage: installed ", "sage: updated ", "sage: unchanged: ")
#: `--idle-stop MINUTES` (issue #207): a kept VM powers itself off once nobody uses it. The script
#: runs as root from a system timer; `--idle-stop 0` removes it again.
IDLE_STOP_SCRIPT = pathlib.Path(__file__).resolve().parent / "idle_stop.py"
#: `--lean-shared` (issue #205): one read-only Mathlib per VM, linked into every Lean project.
LEAN_PROJECT_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "ami/files/bin/lean-project"
LEAN_PROJECT_PATH = "/usr/local/bin/lean-project"
LEAN_WORKSPACE = "/home/agent/lean/workspace"
LEAN_PACKAGES = f"{LEAN_WORKSPACE}/.lake/packages"  # a link to the shared copy once shared
#: The instructions tell agents to use lean-project, so a rollout without --lean-shared appends
#: this probe to the instructions step and notes a VM that lacks it (review on #261). It also
#: says whether the workspace's Mathlib is shared, a link, or not yet, a directory, or whether
#: there is no workspace at all (another image, without Lean): `--lean-shared-default` (#704).
LEAN_PROJECT_MISSING = "---ZAB-NO-LEAN-PROJECT"
LEAN_NOT_SHARED = "---ZAB-LEAN-NOT-SHARED"
LEAN_NO_WORKSPACE = "---ZAB-NO-LEAN-WORKSPACE"
LEAN_PROJECT_PROBE = (
    f"{{ test -x {LEAN_PROJECT_PATH} || echo {LEAN_PROJECT_MISSING}; "
    f"if sudo test -L {LEAN_PACKAGES}; then :; elif sudo test -d {LEAN_PACKAGES}; "
    f"then echo {LEAN_NOT_SHARED}; else echo {LEAN_NO_WORKSPACE}; fi; }}"
)
LEAN_NO_WORKSPACE_NOTE = f"lean: no Lean workspace ({LEAN_PACKAGES}): no shared Mathlib to set up"
#: `--lean-shared-default` when the instructions step, which carries the probe, failed: the
#: same failure, broken markers in a file the agents edit, may recur at every rollout
LEAN_NOT_PROBED = (
    "lean: the shared Mathlib was not probed, since the instructions step failed: "
    "--lean-shared (lean-shared) for this row sets it up regardless"
)
#: The rollout's check fails above this (a cold page cache included); Drew's target for a
#: trivial proof is under 30 s of tool time, and the note records the times measured.
LEAN_CHECK_MAX_S = 120
#: `--lean-reclaim` looks for private Mathlib copies in the participant's checkouts (#205).
LEAN_RECLAIM_DIR = "/home/agent/work"
#: Private copies are never replaced by links to a shared copy whose step failed in the same
#: run (review on #262).
LEAN_RECLAIM_SKIPPED = "lean reclaim: skipped, since --lean-shared failed"
IDLE_STOP_PATH = "/usr/local/lib/zab/idle_stop.py"
IDLE_STOP_UNIT = "zab-idle-stop"
_UNIT_DIR = "/etc/systemd/system"
#: The shortest `--idle-stop` (idle_stop.py refuses less).
IDLE_STOP_MIN = 10
#: `--idle-stop 0` writes this record that idle stop was turned off on purpose (#704), which
#: `--idle-stop-default` respects; `--idle-stop N` removes it.
IDLE_STOP_OFF_RECORD = "/etc/zab/idle-stop.off"
#: `--idle-stop-default`'s look at a VM (#704), a fixed literal. Its state, the first that holds:
#: a spare's idle stop (#715: `idle_stop.py --spare`, which `spare_idle_stop.py` installs), by
#: its unit's flag, which is never the row's (spare: replaced, or removed where the row's waker
#: isn't on); its timer is installed and enabled (on: left as it is, whatever its minutes); idle
#: stop was turned off on purpose, by the record (off: left off); the timer is installed but not
#: enabled, by hand or after a failed `enable` (disabled: turned on again); the log of an idle
#: stop that ran there, which `--idle-stop 0` keeps and nothing else removes: a VM turned off
#: before the record existed (off); or none (unset: turned on). Then the VM's EC2 instance ID, as
#: cloud-init recorded it at boot, which `--has-waker SLUG=INSTANCE` must match (review of #705).
IDLE_STOP_ON, IDLE_STOP_OFF = "---ZAB-IDLE-STOP-ON", "---ZAB-IDLE-STOP-OFF"
IDLE_STOP_UNSET, IDLE_STOP_DISABLED = "---ZAB-IDLE-STOP-UNSET", "---ZAB-IDLE-STOP-DISABLED"
IDLE_STOP_SPARE = "---ZAB-IDLE-STOP-SPARE"
IDLE_STOP_STATES = (
    IDLE_STOP_ON, IDLE_STOP_OFF, IDLE_STOP_UNSET, IDLE_STOP_DISABLED, IDLE_STOP_SPARE,
)  # fmt: skip
INSTANCE_ID_FILE = "/var/lib/cloud/data/instance-id"
INSTANCE_MARK = "---ZAB-INSTANCE="
INSTANCE_RE = re.compile(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", re.ASCII)
_TIMER = f"{_UNIT_DIR}/{IDLE_STOP_UNIT}.timer"
_SERVICE = f"{_UNIT_DIR}/{IDLE_STOP_UNIT}.service"
#: The flag of a spare's idle stop (`idle_stop.py --spare`), as its unit's ExecStart ends.
SPARE_FLAG = "--spare"
_IS_SPARES = f"grep -qsF -- ' {SPARE_FLAG}' {_SERVICE}"
IDLE_STOP_PROBE_CMD = (
    f"if {_IS_SPARES}; then echo {IDLE_STOP_SPARE}; "
    f"elif test -e {_TIMER} && systemctl is-enabled --quiet {IDLE_STOP_UNIT}.timer; "
    f"then echo {IDLE_STOP_ON}; elif test -e {IDLE_STOP_OFF_RECORD}; then echo {IDLE_STOP_OFF}; "
    f"elif test -e {_TIMER}; then echo {IDLE_STOP_DISABLED}; "
    f"elif test -e {IDLE_STOP_LOG}; then echo {IDLE_STOP_OFF}; else echo {IDLE_STOP_UNSET}; fi; "
    f"echo {INSTANCE_MARK}$(cat {INSTANCE_ID_FILE} 2>/dev/null)"
)
#: The status read's one sudo rule (#507): its user may run the read as agent, without arguments.
READ_SUDOERS = f"/etc/sudoers.d/{READ_USER}"
READ_RULE = f'{READ_USER} ALL=({AGENT}) NOPASSWD: {READ_SCRIPT_PATH} ""\n'
#: A fixed literal: stop the timer, then end a tick under way (`systemctl stop`; it may be
#: rewriting the read user's keys); remove the units and the script, the read user's rule,
#: scripts and keys, and the user, even while a read runs (`--force`; absent is fine); then
#: record that it was turned off on purpose (#704). The log stays.
IDLE_STOP_OFF_CMD = (
    f"sudo systemctl disable --now {IDLE_STOP_UNIT}.timer 2>/dev/null; "
    f"sudo systemctl stop {IDLE_STOP_UNIT}.service 2>/dev/null; "
    f"sudo rm -f {_UNIT_DIR}/{IDLE_STOP_UNIT}.service {_UNIT_DIR}/{IDLE_STOP_UNIT}.timer "
    f"{IDLE_STOP_PATH} {READ_SUDOERS} {READ_SCRIPT_PATH} {READ_LOGIN} && "
    f"sudo rm -rf {READ_HOME} && {{ sudo userdel --force {READ_USER} 2>/dev/null; true; }} && "
    f"sudo systemctl daemon-reload && sudo mkdir -p {os.path.dirname(IDLE_STOP_OFF_RECORD)} && "
    f"sudo touch {IDLE_STOP_OFF_RECORD}"
)
#: A fixed literal (#715): a spare's idle stop, and nothing else, removed, where it is one: its
#: timer stopped, a tick under way ended, its units and script gone. Not off on purpose: no
#: record. The spare's log stays (`idle_stop.SPARE_LOG`). Prints `SPARE_TIMER_GONE` when it
#: removed one.
SPARE_TIMER_GONE = "---ZAB-SPARE-TIMER-REMOVED"
SPARE_TIMER_OFF_CMD = (
    f"if {_IS_SPARES}; then "
    f"sudo systemctl disable --now {IDLE_STOP_UNIT}.timer 2>/dev/null; "
    f"sudo systemctl stop {IDLE_STOP_UNIT}.service 2>/dev/null; "
    f"sudo rm -f {_SERVICE} {_TIMER} {IDLE_STOP_PATH} && sudo systemctl daemon-reload && "
    f"echo {SPARE_TIMER_GONE}; fi"
)
#: Every rollout (issue #461): after each boot, a waker's start included, `warm_cache.py` reads
#: the shared Mathlib and its toolchain into the page cache, so that a woken VM's first import
#: isn't cold (on the test VM, `import Mathlib` took 110 s without it, 10 s after it, 4.9 s
#: warm). Removed by `--uninstall`.
WARM_SCRIPT = pathlib.Path(__file__).resolve().parent / "warm_cache.py"
WARM_PATH = "/usr/local/lib/zab/warm_cache.py"
WARM_UNIT = "zab-warm-cache"
#: Its read caps, under the fleet's gp3 baseline of 125 MiB/s (131 MB/s) and 3000 IOPS, which the
#: bridge's and the agents' own reads share with it: cgroup v2 limits, which hold under any I/O
#: scheduler (Nitro's NVMe has none, so `ionice` alone would do nothing). Given for the root file
#: system and for Nitro's root disk by name: the same disk, so whichever systemd resolves applies.
WARM_READ_MAX = "80M"  # systemd's base 1000: 80 MB/s, about 60% of the bandwidth
WARM_IOPS_MAX = 1500  # half the IOPS: the tens of thousands of small `.trace`/`.hash` reads
WARM_DEVICES = ("/", "/dev/nvme0n1")
#: The script's own memory (its address space: about 0.5 GB at its peak, 0.2 GB resident), never
#: the page cache it fills, which `MemoryMax=` would count, cap and evict.
WARM_AS_MAX = "2G"
#: A fixed literal: stop and remove the unit and the script (absent is fine).
WARM_OFF_CMD = (
    f"sudo systemctl disable --now {WARM_UNIT}.service 2>/dev/null; "
    f"sudo rm -f {_UNIT_DIR}/{WARM_UNIT}.service {WARM_PATH} && sudo systemctl daemon-reload"
)

#: Fixed literals (no roster text): the staging directory, and the command of README.md §2
#: step 4, which needs the agent's runtime directory for `systemctl --user`.
MKTEMP_CMD = 'd=$(mktemp -d /tmp/zab-push.XXXXXX) && chmod 0755 "$d" && echo "$d"'
#: `sudo -u agent -H` changes HOME but keeps the cwd, and `ssh ubuntu@host` starts in
#: /home/ubuntu (0750 on Ubuntu 24.04), which agent cannot read: uv walks the cwd for its
#: configuration and fails with EACCES (codex round 3 on PR #47; the AMI builder does the
#: same `cd /`). Every command run as agent therefore starts with `cd /`.
AGENT_CWD = "/"
_SERVICE_ENV = (
    "uid=$(id -u agent) && sudo -u agent -H "
    "XDG_RUNTIME_DIR=/run/user/$uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus "
    "bash -lc "
)
#: `zab service install` restarts only units whose text changed, so after `uv tool install`
#: replaced the binary the running bridge would keep the old code; restart it explicitly
#: before the doctor check (README.md §2 step 4; codex round 2 on PR #47).
#: The doctor runs whatever the install or restart did (PM on #211, P3): with `Type=notify` the
#: restart waits for READY and fails if the bridge doesn't start, and the row should then show the
#: doctor's checks and the restart's error rather than "service install / doctor" alone.
SERVICE_FAILED_MARK = "---ZAB-SERVICE-FAILED"
#: The line `zab service install` prints when it couldn't give a running Codex daemon its new
#: open-file limit with `prlimit` (#664; `service.limit_note`): the limit then applies at the
#: daemon's next start. A note of the row, not a failure. Matched whole, since it comes from
#: the VM.
LIMIT_NOTE_RE = re.compile(
    r"^codex-app-server(?:-[0-9]{1,3})?\.service: its open-file limit \([0-9]{1,8}\) "
    r"applies at its next start$",
    re.MULTILINE,
)
#: `zab sentinel`'s unit (#204), stopped and removed before the installed release's own `zab
#: service install` or `uninstall`: a release that writes it writes it again (and so restarts the
#: sentinel on its new code), and one before it, or its uninstall, would otherwise leave a sentinel
#: running that answers the participant's DMs. Only a unit zab wrote (its first line) is removed.
#: A fixed literal, inside the single quotes of `bash -lc`.
SENTINEL_OFF = (
    'u="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/zab-sentinel.service"; '
    'if head -n 1 "$u" 2>/dev/null | grep -qxF "# Managed by zab service install"; then '
    'systemctl --user disable --now zab-sentinel.service; rm -f -- "$u"; '
    "systemctl --user daemon-reload; fi"
)
SERVICE_INSTALL_CMD = _SERVICE_ENV + (
    f"'cd / && {{ {SENTINEL_OFF}; zab service install && systemctl --user restart zab.service "
    f"|| echo {SERVICE_FAILED_MARK}; }}; zab doctor --service-env --json'"
)
SERVICE_UNINSTALL_CMD = _SERVICE_ENV + f"'cd / && {{ {SENTINEL_OFF}; zab service uninstall; }}'"
#: The doctor alone, for the retries below.
DOCTOR_CMD = _SERVICE_ENV + "'cd / && zab doctor --service-env --json'"
#: `--restart-codex` (issue #214, the v0.2.0 migration): Codex threads loaded in the daemon before
#: the per-bot git identity keep their old shell environment until the daemon restarts (VERIFY on
#: #230), so the first rollout restarts it, only while no Codex turn runs. Both fixed literals.
CODEX_STATUS_CMD = _SERVICE_ENV + "'cd / && zab status --json'"
CODEX_RESTART_CMD = _SERVICE_ENV + "'systemctl --user restart \"codex-app-server*.service\"'"
#: The old bridge is stopped first: running, it would reconnect to the restarted daemon and
#: reopen its threads without the identity before the new one starts (codex on #234).
BRIDGE_STOP_CMD = _SERVICE_ENV + "'systemctl --user stop zab.service'"
BRIDGE_START_CMD = _SERVICE_ENV + "'systemctl --user start zab.service'"
#: Prints the bridge's state ("active", "inactive", "failed", ...); exits 0 only when active.
BRIDGE_STATE_CMD = _SERVICE_ENV + "'systemctl --user is-active zab.service'"
#: `zab service install` starts codex-app-server.service, and the first doctor can run before
#: the daemon listens ("codex:connection: daemon unavailable"; Drew's rc3 rollout, where the unit
#: was up a few seconds later). While that is the case the doctor alone is retried every
#: DAEMON_POLL_S seconds for up to DAEMON_WAIT_S before the VM is reported. 15 s was too short
#: on a fresh t3.medium (the admin e2e's A18, Oct 15): the wait ends as soon as the daemon
#: answers, so a longer bound costs nothing when it is up.
DAEMON_WAIT_S = 60.0
DAEMON_POLL_S = 3.0
_STAGING_RE = re.compile(r"^/tmp/zab-push\.[A-Za-z0-9_]{6,}$")  # validates the mktemp output
#: The VM's own record of its owner, written by the first-boot script (../ami/README.md,
#: "First boot"): the participant JSON and the hostname, bracketed by markers so a missing file
#: cannot shift them.
OWNER_MARK = "---ZAB-OWNER-"
OWNER_CMD = (
    f"echo {OWNER_MARK}JSON; cat /etc/zab-participant.json 2>/dev/null; echo; "
    f"echo {OWNER_MARK}HOST; hostname; echo {OWNER_MARK}END"
)
#: `--adopt-spare`: rewrite the record and hostname of a spare VM for its new owner (ORGANIZERS
#: §7.2); the JSON arrives on stdin, the hostname is appended `shlex`-quoted (the login is
#: validated).
ADOPT_CMD = "sudo tee /etc/zab-participant.json >/dev/null && sudo hostnamectl set-hostname "
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")  # aws_common's rule


# ------------------------------------------------------------------------------ config.toml


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def _table(lines: list[str], header: str, data: Mapping[str, Any]) -> None:
    lines.append(header)
    for k, v in data.items():
        lines.append(f"{k} = {_toml_value(v)}")
    lines.append("")


def config_fields(source: str, cls: str) -> set[str]:
    """The field names of the dataclass `cls` in a `zab/config.py` source (`ast`, no import)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
    return set()


def artifact_config(wheel: str | None, tag: str, runner: Runner | None = None) -> str | None:
    """The `zab/config.py` of what push_config is about to install: read from the wheel, or
    from the tag in this checkout (`git show <tag>:src/zab/config.py`); None when unknown.
    zab refuses unknown config keys, so a key may only go into a VM's config when the release
    installed there knows it (codex on #167: the admin checkout may differ from the artifact)."""
    if wheel:
        try:
            with zipfile.ZipFile(wheel) as whl:
                return whl.read("zab/config.py").decode("utf-8")
        except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError):
            return None
    if tag:
        res = (runner or subprocess_runner)(
            ["git", "-C", str(CHECKOUT), "show", f"{tag}:src/zab/config.py"], timeout_s=30
        )
        return res.stdout if res.ok else None
    return None


def supports_multi_party(config_source: str | None) -> bool:
    """Whether that release has `bridge.multi_party` (#35)."""
    return config_source is not None and "multi_party" in config_fields(
        config_source, "BridgeConfig"
    )


def supports_claude_steer(config_source: str | None) -> bool:
    """Whether that release has `[backends.claude] steer` (PM on issue #36, Fri Oct 9)."""
    return config_source is not None and "steer" in config_fields(config_source, "BackendConfig")


def claude_steer_line(steer: bool | None) -> str:
    """The one line push_config prints about Claude steering (issue #310): ON or OFF."""
    if steer is None:
        return "claude steer: OFF (absent from this release: key left out of config.toml)"
    if steer:
        return "claude steer: ON (messages sent during a Claude turn steer it)"
    return "claude steer: OFF (--no-claude-steer: messages sent during a Claude turn are held)"


def supports_worktree_post_create(config_source: str | None) -> bool:
    """Whether that release has `bridge.worktree_post_create` (issue #205)."""
    return config_source is not None and "worktree_post_create" in config_fields(
        config_source, "BridgeConfig"
    )


#: Every new topic worktree links its committed Lean projects to the shared Mathlib (#205).
WORKTREE_POST_CREATE = ["/usr/local/bin/lean-project", "--repair-all"]


def multi_party_line(multi_party: bool | None) -> str:
    """The one line push_config prints about the switch (PM on #36, Mon 22:30)."""
    if multi_party is None:
        return "multi_party: absent from this release (key left out of config.toml)"
    if multi_party:
        return "multi_party: on (owner mentions required in channels with other participants)"
    return "multi_party: OFF (--no-multi-party: the behavior from before #35, no mention rule)"


def mention_after_line(seconds: int) -> str:
    """The one line push_config prints about `mention_after_s` (#711), which a plan's dry run
    shows."""
    if not seconds:
        return "mention after: 0 s (a turn that completes or fails mentions its owner, any length)"
    return (
        f"mention after: {seconds} s (a turn that ends after {seconds} s or more mentions its "
        "owner)"
    )


def default_lines(
    lean: bool, minutes: int | None, selected: set[str], woken: set[str]
) -> list[str]:
    """What the defaults of #704 will do, printed with the multi-party and steering lines, so
    that a plan's dry run says it: `lean`, `--lean-shared-default`; `minutes`, the default's
    idle stop, and `woken`, the `selected` rows whose waker is on."""
    out = []
    if lean:
        out.append(
            "shared Mathlib: by default, where a VM's isn't shared yet (only probed where it is)"
        )
    if minutes is not None:
        out.append(
            f"idle stop: by default {minutes} min, where the waker is on ({len(woken)} of "
            f"{len(selected)} VMs) and no timer is on, unless turned off on purpose"
        )
    return out


def default_project(row: Mapping[str, str]) -> str:
    """The row's checkout, the config's `bridge.default_project` (README.md §4)."""
    return f"~/work/{work_repo(dict(row))}"


def checkout_check(row: Mapping[str, str]) -> str:
    """The name of the doctor's check of `default_project`: `project:` and the path as the
    doctor resolves it, run as agent (`~` is agent's home)."""
    return "project:" + default_project(row).replace("~", AGENT_HOME, 1)


def checkout_absent_cmd(path: str) -> str:
    """Exits 0 only when nothing at all is at `path`, not even a dangling link: a checkout not
    cloned yet (#458). As root, which reads agent's home whatever its mode."""
    return remote_chain(["sudo", "test", "!", "-e", path], ["sudo", "test", "!", "-L", path])


def render_config_toml(
    row: Mapping[str, str],
    *,
    codex_binary: str = CODEX_BINARY,
    claude_binary: str = CLAUDE_BINARY,
    refserve_url: str = REFSERVE_URL,
    refserve_owner: str = REFSERVE_OWNER,
    multi_party: bool | None = None,
    claude_raw_log: str = "off",
    claude_steer: bool | None = None,
    worktree_hook: bool = False,
    mention_after_s: int = MENTION_AFTER_S,
) -> str:
    """The workshop profile (SPEC §5.2, §5.3) for one participant, round-trippable by tomllib.

    Bots are allowed only for the participant (§2 "owner"): `allow_emails` with the roster e-mail,
    so a participant who joins Zulip after the push is admitted without another push (issue #36; the
    key and its semantics are A's, PR on #36), plus `allow_user_ids` once `zulip_provision.py` has
    recorded the Zulip id. Codex runs with approvals off and full access, Claude with
    `bypassPermissions` (§5.3, §9.5: the VM is dedicated to the agent). Both start at effort
    `high` (Drew, Oct 4): the Claude CLI never reports the effort it defaults to, so zab passes
    one and the status line can show it. Everything is named after
    the slug; the checkout is `~/work/<slug>` (README.md §4). The services get the workshop venv and
    Lean's `elan` on PATH directly: Ubuntu's `~/.bashrc` returns early in non-interactive shells, so
    the lines `build.sh` appends there never reach the agents' tool commands (PM's AMI v2 list,
    item 6). `[render] refserve_url` is the central file server (SPEC §10.7): a turn that links
    a file pushes its snapshot to the participant's repository, and its links point there. Its
    `refserve_owner` beside it is the GitHub organization that server serves, the workshop's
    `github_org` (#693): the bridge publishes no snapshot for a checkout outside it.
    """
    slug = slug_of(dict(row))
    uid = (row.get("zulip_user_id") or "").strip()
    email = (row.get("email") or "").strip()
    if uid and not uid.isdigit():
        raise ValueError(f"{slug}: zulip_user_id {uid!r} is not a number")
    if not email and not uid:
        raise ValueError(f"{slug}: no e-mail in the roster (and no zulip_user_id) to allow")
    allow: dict[str, Any] = {}
    if email:
        allow["allow_emails"] = [email]
    if uid:
        allow["allow_user_ids"] = [int(uid)]
    lines: list[str] = []
    bridge: dict[str, Any] = {
        "machine": slug,
        "default_project": default_project(row),
        # per backend, 20 turns a VM (#919; 4 from Oct 9, 2 before): a turn waiting on the
        # model uses little of the VM, and SPEC §7.10 says why 10 fits an m7a.2xlarge. A raise
        # raises status.py's MAX_ITEMS too (a test ties them), or `!status` cuts the turns
        "max_concurrent_turns": 10,
        "mention_after_s": mention_after_s,  # the workshop's (admin.toml; #711)
        # every topic in its own git worktree (Drew's rehearsal, issue #36, Mon 18:15): in one
        # shared checkout the agents overwrite each other's files and the turn diff mixes
        # topics; `!new --shared` still opts out, and sessions keep the mode they began in
        "default_workspace": "worktree",
        # participants may make their sandboxes public and add their agents to public channels
        # (Drew, issue #36, Tue 06:30); the agents still answer only their owner, and only when
        # mentioned in a channel with other people (bridge.multi_party). A session whose topic
        # moves into a public channel stays live instead of turning read-only
        "allow_public_channels": True,
        "service_path_prepend": ["~/venv/bin", "~/.elan/bin"],
        "service_env": {"VIRTUAL_ENV": "~/venv"},
    }
    if claude_raw_log != "off":
        # each Claude turn's raw stream to ~/.local/state/zab/raw/ (PM on issue #36, Wed 14:45):
        # `meta` records shapes and lengths only, `content` the lines themselves
        bridge["service_env"] = {**bridge["service_env"], "ZAB_CLAUDE_RAW_LOG": claude_raw_log}
    if multi_party is not None:
        # Multi-party channels (issue #35, Drew Mon 19:20); `--no-multi-party` is the
        # fleet-wide kill switch. None (the release has no such key) leaves it out: zab
        # refuses unknown keys
        bridge["multi_party"] = multi_party
    if worktree_hook:
        # issue #205: committed Lean projects in a new topic's worktree link the shared
        # Mathlib (`push_config.py --lean-shared` installs lean-project; without it the hook
        # only logs that it couldn't start). Only for releases that have the key
        bridge["worktree_post_create"] = WORKTREE_POST_CREATE
    _table(lines, "[bridge]", bridge)
    for kind in ("codex", "claude"):
        _table(
            lines,
            "[[bots]]",
            {
                "name": kind,
                "zuliprc": f"~/.config/zab/{kind}.zuliprc",
                "backend": kind,
                **allow,
            },
        )
    _table(
        lines,
        "[backends.codex]",
        {
            "kind": "codex",
            "binary": codex_binary,
            "connect": "daemon",
            "defaults": {"approval": "never", "sandbox": "danger-full-access", "effort": "high"},
        },
    )
    _table(
        lines,
        "[backends.claude]",
        {
            "kind": "claude",
            "binary": claude_binary,
            "accounts": {"main": "~/.claude"},
            "default_account": "main",
            "defaults": {"approval": "bypassPermissions", "effort": "high"},
            # mid-turn steering (PM on issue #36, Fri Oct 9). `main()` passes True unless
            # `--no-claude-steer` (Drew, Oct 15, issue #310), written either way since releases
            # up to 0.2.3 default to off; None (the default, and a release without the key)
            # leaves it out, like the other release-dependent keys: zab refuses unknown keys
            **({} if claude_steer is None else {"steer": claude_steer}),
        },
    )
    if refserve_url:
        if not GITHUB_OWNER.fullmatch(refserve_owner):
            raise ValueError(f"refserve_owner {refserve_owner!r} is not a GitHub owner")
        # every release since 0.1.0rc6 has both keys; the owner written, never the bridge's
        # default, so that another organization's workshop gets its snapshots (#693)
        _table(lines, "[render]", {"refserve_url": refserve_url, "refserve_owner": refserve_owner})
    return "\n".join(lines).rstrip("\n") + "\n"


# -------------------------------------------------------------------------------- per VM


@dataclass(slots=True)
class VmResult:
    slug: str
    host: str
    installed: bool = False
    doctor_ok: bool | None = None  # None: no doctor output; True also when sign-in is pending
    doctor: list[dict[str, Any]] = field(default_factory=list)
    sign_in_pending: bool = False  # the only failing doctor checks are sign-in checks
    owner_pending: bool = False  # an unbound row's allowlist checks fail, and nothing else (#401)
    project_pending: bool = False  # --adopt-spare: the row's checkout isn't cloned yet (#458)
    failures: list[str] = field(default_factory=list)
    adopted: str = ""  # the spare this VM was, when --adopt-spare rewrote its record
    notes: list[str] = field(default_factory=list)  # reported, not failures

    @property
    def ok(self) -> bool:
        return not self.failures and self.installed and bool(self.doctor_ok)


@dataclass(frozen=True, slots=True)
class Options:
    tag: str = ""  # one of tag and wheel is required (main)
    wheel: str | None = None
    out_dir: pathlib.Path = OUT_DIR
    codex_binary: str = CODEX_BINARY
    claude_binary: str = CLAUDE_BINARY
    uv: str = UV
    uninstall: bool = False
    known_hosts: str = str(DEFAULT_KNOWN_HOSTS)
    adopt_spares: frozenset[str] = frozenset()
    refserve_url: str = REFSERVE_URL
    refserve_owner: str = REFSERVE_OWNER  # the workshop's github_org (#693)
    multi_party: bool | None = None  # None: the installed release has no such key
    claude_raw_log: str = "off"  # off | meta | content (ZAB_CLAUDE_RAW_LOG)
    claude_steer: bool | None = None  # [backends.claude] steer; None: left out (main decides)
    idle_stop: int | None = None  # minutes; 0 removes the timer; None leaves it as it is
    restart_codex: bool = False  # restart the Codex daemon before the bridge, when idle (#214)
    worktree_hook: bool = False  # the release has bridge.worktree_post_create (#205)
    mention_after_s: int = MENTION_AFTER_S  # [bridge] mention_after_s, the workshop's (#711)
    lean_shared: bool = False  # share the workspace's Mathlib and install lean-project (#205)
    lean_reclaim: str = ""  # "report" or "apply": private Mathlib copies → the link (#205)
    # #704, the defaults of a rollout that names neither `lean_shared` nor `idle_stop`:
    lean_shared_default: bool = False  # `lean_shared` where the Mathlib isn't shared yet
    idle_stop_default: int = 0  # minutes, where no timer is set; 0: no default
    #: the rows whose waker is on, the default's alone → the instance the waker names, which
    #: must be the VM's; "" for any
    has_waker: Mapping[str, str] = field(default_factory=dict)


def idle_stop_units(minutes: int, *, spare: bool = False) -> dict[str, str]:
    """The service and timer behind `--idle-stop` (issue #207), by file name; a `spare`'s
    (#715, `spare_idle_stop.py`) runs `idle_stop.py --spare`, the flag last."""
    flag = f" {SPARE_FLAG}" if spare else ""
    what = "spare" if spare else "VM"
    service = (
        "[Unit]\n"
        f"Description=Power this {what} off when nobody uses it (zab issue #207)\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "TimeoutStartSec=120\n"
        f"ExecStart={VM_PYTHON} {IDLE_STOP_PATH} --minutes {int(minutes)}{flag}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Check once a minute whether this VM is idle (zab issue #207)\n\n"
        "[Timer]\n"
        "OnBootSec=1min\n"
        "OnUnitActiveSec=1min\n"
        "AccuracySec=5s\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {f"{IDLE_STOP_UNIT}.service": service, f"{IDLE_STOP_UNIT}.timer": timer}


def read_user_files() -> dict[str, str]:
    """The status read, its user's login shell and its sudo rule (#507), by staged name."""
    rule = "# zab #507: the admin server's status read, as agent, and nothing else\n" + READ_RULE
    login = {"status_read_login": READ_LOGIN_TEXT}
    return {"status_read.sh": READ_SCRIPT, **login, f"{READ_USER}.sudoers": rule}


def idle_stop_on_cmd(staging: str) -> str:
    """Install the uploaded script and units as root and (re)start the timer; then the read user
    (#507): the read and the login shell that runs it, its sudo rule once `visudo` passes it, the
    user and its keys. Last, so that a failure there fails the step but leaves idle stop on.
    Once the timer runs, idle stop is no longer off on purpose: the record of that goes (#704)."""
    root = ["-o", "root", "-g", "root"]
    rule = f"{staging}/{READ_USER}.sudoers"
    return remote_chain(
        ["sudo", "install", "-D", *root, "-m", "0755", f"{staging}/idle_stop.py", IDLE_STOP_PATH],
        *[
            ["sudo", "install", *root, "-m", "0644", f"{staging}/{name}", f"{_UNIT_DIR}/{name}"]
            for name in idle_stop_units(0)
        ],
        ["sudo", "systemctl", "daemon-reload"],
        ["sudo", "systemctl", "enable", "--now", f"{IDLE_STOP_UNIT}.timer"],
        ["systemctl", "is-active", f"{IDLE_STOP_UNIT}.timer"],
        ["sudo", "rm", "-f", IDLE_STOP_OFF_RECORD],
        ["sudo", "install", *root, "-m", "0755", f"{staging}/status_read.sh", READ_SCRIPT_PATH],
        ["sudo", "install", *root, "-m", "0755", f"{staging}/status_read_login", READ_LOGIN],
        ["sudo", "visudo", "-cqf", rule],
        ["sudo", "install", *root, "-m", "0440", rule, READ_SUDOERS],
        ["sudo", VM_PYTHON, IDLE_STOP_PATH, "--setup-read-user"],
    )


def spare_idle_stop_on_cmd(staging: str) -> str:
    """A spare's idle stop (#715): the uploaded script and its units installed as root, the
    timer (re)started. No read user: status.py reads no spare, and the hand-over's
    `--idle-stop` sets it up with the row's (`idle_stop_on_cmd`)."""
    root = ["-o", "root", "-g", "root"]
    return remote_chain(
        ["sudo", "install", "-D", *root, "-m", "0755", f"{staging}/idle_stop.py", IDLE_STOP_PATH],
        *[
            ["sudo", "install", *root, "-m", "0644", f"{staging}/{name}", f"{_UNIT_DIR}/{name}"]
            for name in idle_stop_units(0)
        ],
        ["sudo", "systemctl", "daemon-reload"],
        ["sudo", "systemctl", "enable", "--now", f"{IDLE_STOP_UNIT}.timer"],
        ["systemctl", "is-active", f"{IDLE_STOP_UNIT}.timer"],
    )


def install_spare_idle_stop(
    host: str, spare: str, minutes: int, runner: Runner, *, known_hosts: str
) -> str:
    """`spare_idle_stop.py`'s work on one spare (#715), over SSH as ubuntu with `known_hosts`:
    the owner check, which must find the spare `spare` still (its record names no participant,
    its hostname is the spare's), then `idle_stop.py --spare --minutes N` and its units, staged
    and installed; the staging always removed. Why it failed, or "" once the timer runs."""

    def ssh(remote: str) -> list[str]:
        return ssh_argv(host, remote, known_hosts=known_hosts)

    def why(step: str, res: RunResult) -> str:
        return f"{step}: {first_line(res.stderr) or first_line(res.stdout) or res.exit_code}"

    if minutes < IDLE_STOP_MIN:
        return f"idle stop takes at least {IDLE_STOP_MIN} minutes"
    res = runner(ssh(OWNER_CMD), timeout_s=60)
    if not res.ok and OWNER_MARK not in res.stdout:
        return why("ssh", res)
    try:
        owner = parse_owner(res.stdout)
    except ValueError as exc:
        return str(exc)
    if owner.slug or owner.spare != spare or owner.hostname != f"ws-{spare}":
        said = owner.slug or owner.spare or "(no record)"
        return (
            f"the VM says it is {said!r} (hostname {owner.hostname!r}), not the spare {spare}: "
            "nothing installed"
        )
    res = runner(ssh(MKTEMP_CMD), timeout_s=60)
    staging = res.stdout.strip()
    if not res.ok or not _STAGING_RE.match(staging):
        return why("ssh", res) if not res.ok else f"ssh: unexpected staging {staging!r}"
    try:
        with tempfile.TemporaryDirectory(prefix="zab-spare-") as local:
            uploads = [(str(IDLE_STOP_SCRIPT), "idle_stop.py")]
            for name, text in idle_stop_units(minutes, spare=True).items():
                (pathlib.Path(local) / name).write_text(text)
                uploads.append((str(pathlib.Path(local) / name), name))
            for path, name in uploads:
                scp = scp_argv(path, host, f"{staging}/{name}", known_hosts=known_hosts)
                res = runner(scp, timeout_s=300)
                if not res.ok:
                    return why(f"scp {name}", res)
            res = runner(ssh(spare_idle_stop_on_cmd(staging)), timeout_s=120)
            return "" if res.ok else why("idle stop", res)
    finally:
        runner(ssh(remote_chain(["rm", "-rf", "--", staging])), timeout_s=60)


def warm_unit() -> str:
    """`zab-warm-cache.service` (issue #461): once per boot, after the boot's other units; the
    script waits for the bridge itself. `LimitAS=`, not `MemoryMax=`: the cache it reads is
    charged to its cgroup."""
    caps = "".join(
        f"IOReadBandwidthMax={dev} {WARM_READ_MAX}\nIOReadIOPSMax={dev} {WARM_IOPS_MAX}\n"
        for dev in WARM_DEVICES
    )
    return (
        "[Unit]\n"
        "Description=Read the agents' CLIs, the shared Mathlib, and Oscar where used, into the "
        "page cache after each boot (zab #461, #475, #483)\n"
        "After=multi-user.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={VM_PYTHON} {WARM_PATH}\n"
        "Nice=19\n"
        "IOSchedulingClass=best-effort\n"
        "IOSchedulingPriority=7\n"
        f"{caps}"
        f"LimitAS={WARM_AS_MAX}\n"
        "TimeoutStartSec=30min\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def warm_on_cmd(staging: str) -> str:
    """Install the uploaded script and unit as root and enable the unit for the next boots
    (not now: the VM is up); then the script's dry run, which counts what it would read."""
    root = ["-o", "root", "-g", "root"]
    unit = f"{WARM_UNIT}.service"
    return remote_chain(
        ["sudo", "install", "-D", *root, "-m", "0755", f"{staging}/warm_cache.py", WARM_PATH],
        ["sudo", "install", *root, "-m", "0644", f"{staging}/{unit}", f"{_UNIT_DIR}/{unit}"],
        ["sudo", "systemctl", "daemon-reload"],
        ["sudo", "systemctl", "enable", "--quiet", unit],
        ["sudo", VM_PYTHON, WARM_PATH, "--dry-run"],
    )


def lean_shared_on_cmd(staging: str) -> str:
    """Install lean-project as root, then make the workspace's packages the shared copy."""
    root = ["-o", "root", "-g", "root"]
    return remote_chain(
        [
            "sudo",
            "install",
            "-D",
            *root,
            "-m",
            "0755",
            f"{staging}/lean-project",
            LEAN_PROJECT_PATH,
        ],
        ["sudo", VM_PYTHON, LEAN_PROJECT_PATH, "--install", LEAN_WORKSPACE],
    )


#: After a failed check, the workspace's packages go back where they were, writable as before.
LEAN_SHARED_UNDO_CMD = remote_chain(
    ["sudo", VM_PYTHON, LEAN_PROJECT_PATH, "--uninstall", LEAN_WORKSPACE]
)
#: After that, the agents' instructions still send new Lean projects to lean-project, which
#: refuses them with no shared copy (review on #262).
LEAN_ROLLED_BACK_WARNING = (
    "lean: WARNING: the agents' instructions name lean-project, which refuses new projects "
    "while no shared copy is installed: fix the check's failure and rerun --lean-shared"
)


def running_turns(stdout: str) -> int | None:
    """All running turns in `zab status --json`; None when the output can't be read."""
    for line in reversed(stdout.splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "error" in data:
            return None
        return len([t for t in data.get("running_turns") or [] if isinstance(t, dict)])
    return None


def running_codex_turns(stdout: str) -> int | None:
    """Running Codex turns in `zab status --json`; None when the output can't be read."""
    for line in reversed(stdout.splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "error" in data:
            return None
        sessions = {s.get("id"): s for s in data.get("sessions") or [] if isinstance(s, dict)}
        turns = data.get("running_turns") or []
        return sum(
            1
            for turn in turns
            if isinstance(turn, dict)
            and sessions.get(turn.get("session_id"), {}).get("backend") == "codex"
        )
    return None


def restart_codex_step(result: VmResult, runner: Runner, ssh: Any) -> bool:
    """`--restart-codex`: stop the old bridge, then restart the daemon unless a Codex turn runs.

    Never a failure: the caller's service step starts the new bridge either way. Returns whether
    the bridge was stopped, so that the caller can start it again if that step fails.
    """
    stop = runner(ssh(BRIDGE_STOP_CMD), timeout_s=120)
    if not stop.ok:
        detail = first_line(stop.stderr) or f"exit {stop.exit_code}"
        result.notes.append(f"codex daemon NOT restarted: the bridge didn't stop ({detail})")
        return False
    # with the bridge stopped no new turn can start; a Codex turn already running goes on in
    # the daemon, and its row still says so
    status = runner(ssh(CODEX_STATUS_CMD), timeout_s=60)
    running = running_codex_turns(status.stdout) if status.ok else None
    if running is None:
        result.notes.append("codex daemon NOT restarted: zab status unreadable; rerun later")
    elif running:
        result.notes.append(
            f"codex daemon NOT restarted: {running} Codex turn(s) running; "
            "rerun with --restart-codex when idle"
        )
    else:
        res = runner(ssh(CODEX_RESTART_CMD), timeout_s=120)
        if res.ok:
            result.notes.append("codex daemon restarted")
        else:
            detail = first_line(res.stderr) or f"exit {res.exit_code}"
            result.notes.append(f"codex daemon restart failed: {detail}")
    return True


def as_agent(*argv: str) -> list[str]:
    """`sudo -u agent -H bash -lc 'cd / && <argv>'`: HOME set, cwd readable, values quoted."""
    inner = f"cd {shlex.quote(AGENT_CWD)} && {shlex.join(list(argv))}"
    return ["sudo", "-u", AGENT, "-H", "bash", "-lc", inner]


def lean_reclaim_cmd(apply: bool) -> str:
    """As agent: report (or, with `apply`, reclaim) private Mathlib copies in ~/work."""
    flags = ["--apply"] if apply else []
    return remote_chain(as_agent("lean-project", "--reclaim", *flags, LEAN_RECLAIM_DIR))


def lean_check_cmd() -> str:
    """As agent: a Mathlib file and a comparator run through the shared packages (the page
    cache may be cold), then the same file again, warm."""
    return remote_chain(
        as_agent("lean-project", "--check", "--comparator", "--max-seconds", str(LEAN_CHECK_MAX_S)),
        as_agent("lean-project", "--check"),
    )


def daemon_starting(doctor: list[dict[str, Any]]) -> bool:
    """A doctor result that only says the Codex daemon is not listening yet."""
    return any(not c.get("ok") and "daemon unavailable" in str(c.get("detail", "")) for c in doctor)


def parse_doctor(stdout: str) -> list[dict[str, Any]] | None:
    """`zab doctor --json` prints one JSON list; `service install` may print lines before it."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data
    return None


def _fail(result: VmResult, step: str, res: RunResult) -> None:
    detail = first_line(res.stderr) or first_line(res.stdout) or f"exit {res.exit_code}"
    result.failures.append(f"{step}: {detail}")


@dataclass(frozen=True, slots=True)
class VmOwner:
    """What a VM says about itself: `/etc/zab-participant.json` plus its hostname."""

    slug: str
    spare: str
    hostname: str
    github: str = ""  # the login whose SSH keys the first boot imported for agent ("" for none)


def parse_owner(stdout: str) -> VmOwner:
    """The record behind `OWNER_CMD`; a missing or unreadable file is an empty record."""
    text = stdout.replace("\r", "")
    try:
        json_part = text.split(f"{OWNER_MARK}JSON", 1)[1].split(f"{OWNER_MARK}HOST", 1)[0]
        host_part = text.split(f"{OWNER_MARK}HOST", 1)[1].split(f"{OWNER_MARK}END", 1)[0]
    except IndexError:
        raise ValueError("owner check: unexpected output") from None
    slug = spare = github = ""
    if json_part.strip():
        try:
            data = json.loads(json_part)
        except json.JSONDecodeError:
            raise ValueError("owner check: /etc/zab-participant.json is not JSON") from None
        if not isinstance(data, dict):
            raise ValueError("owner check: /etc/zab-participant.json is not an object")
        # records written before the slug (first boots from rc3 and earlier) name the login
        github = str(data.get("github") or "").strip()
        slug = str(data.get("slug") or github.lower()).strip()
        spare = str(data.get("spare") or "").strip()
    host_lines = host_part.strip().splitlines()
    host = host_lines[-1].strip() if host_lines else ""
    return VmOwner(slug=slug, spare=spare, hostname=host, github=github)


def owner_problem(owner: VmOwner, slug: str, *, adopt: bool, github: str = "") -> str | None:
    """None when the VM belongs to `slug`, else why nothing may be copied to it.

    A spare is accepted only with `--adopt-spare <slug>` (the caller then rewrites its record);
    any other VM must carry exactly this slug in its record and in its hostname, and the GitHub
    login it records (whose SSH keys its first boot imported for `agent`) must be the roster's,
    or none: after a login was corrected or removed in the roster, copying the bots' keys there
    would hand them to the previous account (codex round 7 on #97).
    """
    if owner.spare and not owner.slug:
        if adopt:
            return None
        return (
            f"VM is the spare {owner.spare!r} (hostname {owner.hostname!r}); "
            f"use --adopt-spare {slug} to hand it over"
        )
    if owner.slug.casefold() != slug.casefold():
        return (
            f"VM belongs to {owner.slug or '(no record)'!r} (hostname {owner.hostname!r}), "
            f"not {slug!r}: nothing copied; check the roster's ip column"
        )
    if owner.hostname.casefold() != f"ws-{slug}".casefold():
        return f"VM hostname is {owner.hostname!r}, expected ws-{slug}: nothing copied"
    if owner.github and owner.github.casefold() != github.casefold():
        now = f"{github!r} in the roster" if github else "no GitHub login in the roster"
        return (
            f"VM records GitHub login {owner.github!r}, whose SSH keys it imported for agent, but "
            f"{now}: nothing copied. As ubuntu, empty /home/agent/.ssh/authorized_keys, remove "
            "/etc/cron.d/zab-import-key"
            + (f", run `sudo -u agent ssh-import-id gh:{github}`" if github else "")
            + f', and set "github" to {github!r} in /etc/zab-participant.json; then rerun'
        )
    return None


def rollout_vm(row: Mapping[str, str], opts: Options, runner: Runner) -> VmResult:
    """README.md §2, steps 1 to 4, on one VM; stops at the first failed step, always cleans up
    staging."""
    result = VmResult(slug=slug_of(dict(row)), host="")
    try:
        result.host = host_of(row)
    except ValueError as exc:
        result.failures.append(str(exc))
        return result
    host = result.host
    kh = opts.known_hosts

    def ssh(remote: str) -> list[str]:
        return ssh_argv(host, remote, known_hosts=kh)

    if opts.uninstall:
        res = runner(ssh(SERVICE_UNINSTALL_CMD), timeout_s=180)
        if res.ok:
            result.installed = True
            result.doctor_ok = True
        else:
            _fail(result, "service uninstall", res)
        res = runner(ssh(WARM_OFF_CMD), timeout_s=60)  # the rollout's warm-up unit (#461)
        if not res.ok:
            _fail(result, "warm-up removal", res)
        return result

    keys, note = keys_dir(opts.out_dir, row)
    if note:
        result.notes.append(note)
    rcs = {kind: keys / f"{kind}.zuliprc" for kind in ("codex", "claude")}
    for kind, path in rcs.items():
        if not path.is_file():
            result.failures.append(f"missing {kind} zuliprc: run zulip_provision.py first")
    try:
        config = render_config_toml(
            row,
            codex_binary=opts.codex_binary,
            claude_binary=opts.claude_binary,
            refserve_url=opts.refserve_url,
            refserve_owner=opts.refserve_owner,
            multi_party=opts.multi_party,
            claude_raw_log=opts.claude_raw_log,
            claude_steer=opts.claude_steer,
            worktree_hook=opts.worktree_hook,
            mention_after_s=opts.mention_after_s,
        )
    except ValueError as exc:
        result.failures.append(str(exc))
    if result.failures:
        return result

    # Step 0: the VM must say it is this participant's before anything is copied to it.
    slug = result.slug
    adopting = slug in opts.adopt_spares
    owner = check_owner(result, row, runner, ssh, adopt=adopting)
    if owner is None:
        return result
    if owner.spare and adopting:
        if not _SLUG_RE.match(slug):
            result.failures.append(f"invalid slug {slug!r}")
            return result
        try:
            name, email = git_identity({**row, "slug": slug})
        except ValueError as exc:  # no e-mail in the row: nothing is changed on the spare
            result.failures.append(f"adopt spare: {exc}")
            return result
        # a spare's first boot sets no git identity for `agent`, so every commit would fail
        # ("Author identity unknown"); the participant's first boot sets these same values
        # (PM on issue #36, Wed 21:20). Before the owner record: once that names the
        # participant, a rerun no longer sees a spare, so a failure here must come first to
        # stay retryable (codex on #195); both steps are idempotent
        identity = remote_chain(
            as_agent("git", "config", "--global", "user.name", name),
            as_agent("git", "config", "--global", "user.email", email),
        )
        res = runner(ssh(identity), timeout_s=60)
        if not res.ok:
            _fail(result, "adopt spare: git identity", res)
            return result
        record = owner_record({**row, "slug": slug})
        res = runner(ssh(ADOPT_CMD + shlex.quote(f"ws-{slug}")), timeout_s=60, stdin=record)
        if not res.ok:
            _fail(result, "adopt spare", res)
            return result
        result.adopted = owner.spare

    res = runner(ssh(MKTEMP_CMD), timeout_s=60)
    staging = res.stdout.strip()
    if not res.ok or not _STAGING_RE.match(staging):
        if not res.ok:
            _fail(result, "ssh", res)
        else:
            result.failures.append(f"ssh: unexpected staging directory {staging!r}")
        if adopting:  # as in the `finally` below
            spare_timer_off_step(result, opts, runner)
        return result

    runner(ssh(IDLE_HOLD_ON_CMD), timeout_s=60)
    try:
        with tempfile.TemporaryDirectory(prefix="zab-push-") as local:
            cfg = pathlib.Path(local) / "config.toml"
            cfg.write_text(config)
            uploads = (
                [(str(cfg), "config.toml")]
                + [(str(p), f"{k}.zuliprc") for k, p in rcs.items()]
                + [
                    (str(INSTRUCTIONS_TEXT), INSTRUCTIONS_TEXT.name),
                    (str(INSTRUCTIONS_SCRIPT), INSTRUCTIONS_SCRIPT.name),
                    (str(TOOLS_SCRIPT), TOOLS_SCRIPT.name),
                    (str(TOOLS_LIST), TOOLS_LIST.name),
                    (str(SAGE_SCRIPT), SAGE_SCRIPT.name),
                    (str(SAGE_WRAPPER), SAGE_WRAPPER.name),
                    (str(APT_SCRIPT), APT_SCRIPT.name),
                ]
            )
            wheel_name = None
            if opts.wheel:
                wheel_name = pathlib.Path(opts.wheel).name
                uploads.append((opts.wheel, wheel_name))
            for local_path, name in uploads:
                res = runner(
                    scp_argv(local_path, host, f"{staging}/{name}", known_hosts=kh), timeout_s=300
                )
                if not res.ok:
                    _fail(result, f"scp {name}", res)
                    return result

            install_files = remote_chain(
                ["sudo", "install", "-d", "-o", AGENT, "-g", AGENT, "-m", "0700", ZAB_CONFIG_DIR],
                *[
                    [
                        "sudo",
                        "install",
                        "-o",
                        AGENT,
                        "-g",
                        AGENT,
                        "-m",
                        "0600",
                        f"{staging}/{name}",
                        f"{ZAB_CONFIG_DIR}/{name}",
                    ]
                    for name in ("codex.zuliprc", "claude.zuliprc", "config.toml")
                ],
            )
            res = runner(ssh(install_files), timeout_s=60)
            if not res.ok:
                _fail(result, "install files", res)
                return result

            def upgrade_and_check() -> None:
                """The release, the services and the doctor; returns at the first failure."""
                if wheel_name:
                    # The staging dir is 0755 and the wheel 0644 so that `agent` can read them (the
                    # same arrangement as workshop/ami/build.sh for ZAB_WHEEL).
                    upgrade = remote_chain(
                        ["sudo", "chmod", "0644", f"{staging}/{wheel_name}"],
                        as_agent(opts.uv, "tool", "install", "--force", f"{staging}/{wheel_name}"),
                    )
                else:
                    upgrade = remote_chain(
                        as_agent(
                            opts.uv, "tool", "install", "--force", f"git+{repo_url()}@{opts.tag}"
                        ),
                    )
                res = runner(ssh(upgrade), timeout_s=900)
                if not res.ok:
                    _fail(result, "uv tool install", res)
                    return
                result.installed = True

                stopped = opts.restart_codex and restart_codex_step(result, runner, ssh)
                res = runner(ssh(SERVICE_INSTALL_CMD), timeout_s=300)
                doctor = parse_doctor(res.stdout)
                result.notes += LIMIT_NOTE_RE.findall(res.stdout)
                # a bridge stopped for --restart-codex must not stay down, whatever the service
                # step did: `start` is a no-op when the restart brought it up (#237 makes the
                # doctor run even when the restart fails, so its output can't tell)
                if stopped:
                    runner(ssh(BRIDGE_START_CMD), timeout_s=300)
                deadline = time.monotonic() + DAEMON_WAIT_S
                while (
                    doctor is not None and daemon_starting(doctor) and time.monotonic() < deadline
                ):
                    time.sleep(DAEMON_POLL_S)
                    again = parse_doctor(runner(ssh(DOCTOR_CMD), timeout_s=120).stdout)
                    if again is not None:
                        doctor = again
                if SERVICE_FAILED_MARK in res.stdout:
                    detail = first_line(res.stderr) or "failed"
                    result.failures.append(f"service install / restart: {detail}")
                if doctor is None:
                    _fail(result, "service install / doctor", res)
                    return
                result.doctor = doctor
                failing = [c for c in doctor if not c.get("ok")]
                # an unbound row's allowlist: the participant hasn't joined Zulip yet (#401)
                unjoined = [c for c in failing if allowlist_pending(c, row)]
                # a hand-over's checkout, which provision_repos.py clones next (#458): exactly
                # this row's default project, and only while nothing is there; a directory that
                # isn't a git repository, or a probe that fails, is a real failure
                check = checkout_check(row)
                uncloned = [c for c in failing if adopting and c.get("name") == check]
                absent = checkout_absent_cmd(check.removeprefix("project:"))
                if uncloned and not runner(ssh(absent), timeout_s=60).ok:
                    uncloned = []
                bad = [c for c in failing if c not in unjoined and c not in uncloned]
                # only the agents' sign-ins missing: expected before the participants' `!login`;
                # set for an otherwise passing doctor only (PM on #36, Mon 13:45)
                result.sign_in_pending = sign_in_pending(bad)
                result.doctor_ok = not bad or result.sign_in_pending
                result.owner_pending = result.doctor_ok and bool(unjoined)
                result.project_pending = result.doctor_ok and bool(uncloned)
                if not result.doctor_ok:
                    result.failures.extend(
                        f"doctor: {c.get('name', '?')}: {first_line(str(c.get('detail', '')), 80)}"
                        for c in bad[:5]
                    )

            upgrade_and_check()
            # The `sage` wrapper where the image has its link (#482), then the VM's list of its
            # software (#475), which says what the wrapper changes, and which the instructions
            # below are made from
            wrapped = sage_step(result, runner, ssh, staging)
            tools_step(result, runner, ssh, staging, (SAGE_FEATURE,) if wrapped else ())
            # The image's expired apt source (#947), so that the VM's `apt-get update` works
            apt_step(result, runner, ssh, staging)
            # Last, and never a reason to stop the rollout: the agents' instructions (the
            # managed block replaced, the participant's own section kept; agent_instructions.py,
            # PM on issue #36, Mon 19:00). A file with broken markers, or not UTF-8, is left
            # alone and reported; config, release and services are in place by then (PM, Mon
            # 22:30)
            text, script, tools = (
                f"{staging}/{INSTRUCTIONS_TEXT.name}",
                f"{staging}/{INSTRUCTIONS_SCRIPT.name}",
                f"{staging}/{TOOLS_SCRIPT.name}",
            )
            instructions = remote_chain(
                ["sudo", "chmod", "0644", text, script, tools],
                as_agent(VM_PYTHON, script, text),
            )
            if not opts.lean_shared:
                instructions += f" && {LEAN_PROJECT_PROBE}"
            res = runner(ssh(instructions), timeout_s=60)
            lean = opts.lean_shared
            if not res.ok:
                _fail(result, "agent instructions", res)
                if opts.lean_shared_default:
                    result.notes.append(LEAN_NOT_PROBED)
            elif opts.lean_shared_default:
                lean = lean_default(result, res.stdout)
            elif LEAN_PROJECT_MISSING in res.stdout:
                result.notes.append(
                    "lean: the instructions name lean-project, which this VM lacks: "
                    "run --lean-shared"
                )
            none = [line for line in res.stdout.splitlines() if line.startswith(TOOLS_NONE)]
            if res.ok and none:  # the instructions then name no tool, and say why
                result.notes.append(first_line(none[0], 160))
            shared = lean_shared_step(result, opts, runner, staging) if lean else True
            if opts.lean_reclaim and not shared:
                result.notes.append(LEAN_RECLAIM_SKIPPED)
            elif opts.lean_reclaim:
                lean_reclaim_step(result, opts, runner)
            warm_step(result, opts, runner, staging, pathlib.Path(local))
            if opts.idle_stop is not None:
                idle_stop_step(result, opts, runner, staging, pathlib.Path(local))
            elif opts.idle_stop_default:
                idle_stop_default_step(result, opts, runner, staging, pathlib.Path(local))
            return result
    finally:
        # a spare's idle stop never outlives its hand-over (#715): a run with neither idle-stop
        # flag, and one that stopped before its idle-stop step or failed in it, would leave the
        # spare's minutes to power off what is the row's VM now (review of #728). It touches a
        # ` --spare` unit only: after the row's timer replaced it, it finds none
        if adopting:
            spare_timer_off_step(result, opts, runner)
        runner(ssh(remote_chain(["rm", "-rf", "--", staging])), timeout_s=60)
        runner(ssh(IDLE_HOLD_OFF_CMD), timeout_s=60)


def check_owner(
    result: VmResult, row: Mapping[str, str], runner: Runner, ssh: Any, *, adopt: bool
) -> VmOwner | None:
    """Step 0, the owner check; None, with the failure recorded, when nothing may be copied."""
    res = runner(ssh(OWNER_CMD), timeout_s=60)
    if not res.ok and OWNER_MARK not in res.stdout:
        _fail(result, "ssh", res)
        return None
    try:
        owner = parse_owner(res.stdout)
    except ValueError as exc:
        result.failures.append(str(exc))
        return None
    github = (row.get("github") or "").strip()
    problem = owner_problem(owner, result.slug, adopt=adopt, github=github)
    if problem:
        result.failures.append(problem)
        return None
    return owner


def idle_stop_step(
    result: VmResult,
    opts: Options,
    runner: Runner,
    staging: str,
    local: pathlib.Path,
    minutes: int | None = None,
) -> bool:
    """`--idle-stop` (or `minutes`, the default's): upload and start the timer, or remove it
    (0). A failure is recorded."""
    kh = opts.known_hosts
    minutes = opts.idle_stop if minutes is None else minutes
    if not minutes:
        res = runner(ssh_argv(result.host, IDLE_STOP_OFF_CMD, known_hosts=kh), timeout_s=60)
        if not res.ok:
            _fail(result, "idle stop off", res)
            return False
        result.notes.append("idle stop: off")
        return True
    uploads = [(str(IDLE_STOP_SCRIPT), "idle_stop.py")]
    for name, text in {**idle_stop_units(minutes), **read_user_files()}.items():
        (local / name).write_text(text)
        uploads.append((str(local / name), name))
    for local_path, name in uploads:
        res = runner(
            scp_argv(local_path, result.host, f"{staging}/{name}", known_hosts=kh), timeout_s=300
        )
        if not res.ok:
            _fail(result, f"scp {name}", res)
            return False
    res = runner(ssh_argv(result.host, idle_stop_on_cmd(staging), known_hosts=kh), timeout_s=120)
    if not res.ok:
        _fail(result, "idle stop", res)
        return False
    result.notes.append(f"idle stop: after {minutes} min")
    return True


def idle_stop_default_step(
    result: VmResult, opts: Options, runner: Runner, staging: str, local: pathlib.Path
) -> bool:
    """`--idle-stop-default MINUTES` (#704): `--idle-stop MINUTES` on a VM whose timer isn't on,
    and only where the row's waker is on (`--has-waker`) for this VM, the instance the waker
    names: the waker first, then idle stop (ORGANIZERS §4.2). Every VM is probed
    (`IDLE_STOP_PROBE_CMD`). Where the waker is on, a timer that is on stays as it is, quietly;
    idle stop turned off on purpose stays off, noted; a timer installed but not enabled is
    turned on again, noted. Any other row is left as it is, with a note: of the commands where
    no timer is on, and louder where one is, since that VM may stop with no way to wake it
    (review of #705). A spare's idle stop (#715) is never the row's: where the waker is on, the
    row's replaces it, and elsewhere it is removed, noted. A probe that fails, or answers nothing
    it knows, is a failure."""
    minutes, slug = opts.idle_stop_default, result.slug
    probe = ssh_argv(result.host, IDLE_STOP_PROBE_CMD, known_hosts=opts.known_hosts)
    res = runner(probe, timeout_s=60)
    words = res.stdout.split()
    state = next((s for s in IDLE_STOP_STATES if s in words), "")
    if not res.ok or not state:
        _fail(result, "idle-stop probe", res)
        return False
    vm = next((w.removeprefix(INSTANCE_MARK) for w in words if w.startswith(INSTANCE_MARK)), "")
    vm = vm if INSTANCE_RE.fullmatch(vm) else ""  # the VM's own text never reaches a note
    named = opts.has_waker.get(slug)
    why = "" if named is not None else "this row's waker isn't on"
    if named and named != vm:  # e.g. a broken VM replaced by hand, the waker not run again
        why = f"the waker names {named}, not this VM ({vm or 'unknown'})"
    if state == IDLE_STOP_SPARE and why:  # a spare's minutes, and no way to wake it
        then = f", since {why}: !waker on {slug}, then !idle-stop {minutes} {slug}"
        return spare_timer_off_step(result, opts, runner, then)
    if state == IDLE_STOP_SPARE:
        result.notes.append("idle stop: the spare's timer replaced by the row's")
    if state == IDLE_STOP_OFF and why:  # never `!idle-stop` alone without the waker (§4.2)
        result.notes.append(
            f"idle stop: left off, as it was turned off on purpose, and {why}: !waker on {slug}, "
            f"then !idle-stop {minutes} {slug}"
        )
    elif state == IDLE_STOP_OFF:
        result.notes.append(
            f"idle stop: left off, as it was turned off on purpose: !idle-stop {minutes} {slug} "
            "turns it on"
        )
    elif why and state == IDLE_STOP_ON:
        result.notes.append(
            f"idle stop: WARNING: it is on, but {why}: !waker on {slug}, or !idle-stop 0 {slug}"
        )
    elif why:
        result.notes.append(
            f"idle stop: left as it is, since {why}: !waker on {slug}, then !idle-stop "
            f"{minutes} {slug}"
        )
    if why or state in (IDLE_STOP_ON, IDLE_STOP_OFF):
        return True
    if state == IDLE_STOP_DISABLED:
        result.notes.append("idle stop: its timer was installed but not enabled: turned on again")
    return idle_stop_step(result, opts, runner, staging, local, minutes=minutes)


def spare_timer_off_step(result: VmResult, opts: Options, runner: Runner, then: str = "") -> bool:
    """A spare's idle stop removed, where the VM has one (#715, `SPARE_TIMER_OFF_CMD`): a
    hand-over (`--adopt-spare`), or a default, that gives the row no idle stop of its own, so
    that the spare's minutes never outlive it. Noted when one went, `then` after; a failure is
    recorded."""
    off = ssh_argv(result.host, SPARE_TIMER_OFF_CMD, known_hosts=opts.known_hosts)
    res = runner(off, timeout_s=60)
    if not res.ok:
        _fail(result, "spare's idle stop removal", res)
        return False
    if SPARE_TIMER_GONE in res.stdout.split():
        result.notes.append(f"idle stop: the spare's timer removed{then}")
    return True


def lean_default(result: VmResult, stdout: str) -> bool:
    """`--lean-shared-default` (#704), from the instructions step's `LEAN_PROJECT_PROBE`: whether
    to run `lean_shared_step`, on a VM whose Mathlib isn't shared yet or that lacks lean-project.
    A VM that has both is left as it is: no timing build. A VM with no Lean workspace is noted,
    never failed."""
    if LEAN_NO_WORKSPACE in stdout:
        result.notes.append(LEAN_NO_WORKSPACE_NOTE)
        return False
    return LEAN_NOT_SHARED in stdout or LEAN_PROJECT_MISSING in stdout


def sage_cmd(staging: str) -> str:
    """As agent: the wrapper as `~/.local/bin/sage` where that is the image's link."""
    script, wrapper = f"{staging}/{SAGE_SCRIPT.name}", f"{staging}/{SAGE_WRAPPER.name}"
    return remote_chain(
        ["sudo", "chmod", "0644", script, wrapper],
        as_agent(VM_PYTHON, script, "install", wrapper),
    )


def sage_step(result: VmResult, runner: Runner, ssh: Any, staging: str) -> bool:
    """Every release rollout (#482): whether `~agent/.local/bin/sage` is the wrapper afterwards,
    by the script's own look at the VM. Quiet when it was already; noted when installed,
    updated or left alone (skipped); a failure is recorded and holds nothing up."""
    res = runner(ssh(sage_cmd(staging)), timeout_s=60)
    if not res.ok:
        _fail(result, "sage wrapper", res)
        return False
    # its line, whatever agent's login shell may print before it
    line = next((first_line(x) for x in res.stdout.splitlines() if x.startswith("sage: ")), "")
    if not line.startswith(SAGE_IN_PLACE[2]):
        result.notes.append(line or "sage: the wrapper's step said nothing")
    return line.startswith(SAGE_IN_PLACE)


def apt_cmd(staging: str) -> str:
    """As root: `apt_repair.py`, the uploaded copy, with the system Python alone."""
    return remote_chain(["sudo", VM_PYTHON, f"{staging}/{APT_SCRIPT.name}"])


def apt_step(result: VmResult, runner: Runner, ssh: Any, staging: str) -> bool:
    """Every release rollout (#947): the image's expired Macaulay2 snapshot source removed and
    Macaulay2's packages held, where the VM has that source; a no-op where it hasn't. Noted when
    it removed it, or kept a file it couldn't vouch for (`apt: kept …`), so that a VM whose apt
    may still fail shows; a failure is recorded and holds nothing up."""
    res = runner(ssh(apt_cmd(staging)), timeout_s=180)
    line = next((first_line(x, 200) for x in res.stdout.splitlines() if x.startswith("apt: ")), "")
    if not res.ok:
        result.failures.append(f"apt source: {line or first_line(res.stderr) or 'failed'}")
        return False
    if line and not line.startswith("apt: nothing to repair"):
        result.notes.append(line)
    return True


def tools_cmd(staging: str, features: tuple[str, ...] = ()) -> str:
    """As root: the Oct 9 image's list as `/etc/zab/tools.json`, unless the image has its own,
    with the entries' fields for the `features` found on the VM."""
    script, listing = f"{staging}/{TOOLS_SCRIPT.name}", f"{staging}/{TOOLS_LIST.name}"
    flags = [arg for feature in features for arg in ("--with", feature)]
    return remote_chain(["sudo", VM_PYTHON, script, "install", listing, *flags])


def tools_step(
    result: VmResult, runner: Runner, ssh: Any, staging: str, features: tuple[str, ...] = ()
) -> bool:
    """Every release rollout (#475): the VM's list of its software, before the instructions
    that are made from it. Noted when it is first installed; a failure is recorded, and the
    instructions then say the list is missing."""
    res = runner(ssh(tools_cmd(staging, features)), timeout_s=60)
    if not res.ok:
        _fail(result, "tools list", res)
        return False
    line = first_line(res.stdout, 160)
    if line.startswith("tools: installed"):
        result.notes.append(line)
    return True


def warm_step(
    result: VmResult, opts: Options, runner: Runner, staging: str, local: pathlib.Path
) -> bool:
    """Every rollout (issue #461): upload and enable `zab-warm-cache.service`, then run the
    script's dry run, which checks that it runs on the VM. Quiet on success, since every row
    gets it; a failure is recorded."""
    kh = opts.known_hosts
    unit = local / f"{WARM_UNIT}.service"
    unit.write_text(warm_unit())
    for local_path, name in ((WARM_SCRIPT, "warm_cache.py"), (unit, unit.name)):
        res = runner(
            scp_argv(str(local_path), result.host, f"{staging}/{name}", known_hosts=kh),
            timeout_s=300,
        )
        if not res.ok:
            _fail(result, f"scp {name}", res)
            return False
    res = runner(ssh_argv(result.host, warm_on_cmd(staging), known_hosts=kh), timeout_s=300)
    if not res.ok:
        _fail(result, "warm-up", res)
        return False
    return True


def lean_shared_step(result: VmResult, opts: Options, runner: Runner, staging: str) -> bool:
    """`--lean-shared` (issue #205): lean-project in /usr/local/bin, the workspace's Mathlib made
    the shared read-only copy (a rename), then the check as agent. A failure is recorded.

    Fail-safe (review on #259): when the check fails right after this run moved the packages,
    they are moved back, so the VM's Lean works as before. A copy shared before this run stays
    shared, since projects may link to it by now."""
    kh = opts.known_hosts
    res = runner(
        scp_argv(str(LEAN_PROJECT_SCRIPT), result.host, f"{staging}/lean-project", known_hosts=kh),
        timeout_s=300,
    )
    if not res.ok:
        _fail(result, "scp lean-project", res)
        return False
    res = runner(ssh_argv(result.host, lean_shared_on_cmd(staging), known_hosts=kh), timeout_s=600)
    if not res.ok:
        _fail(result, "lean shared", res)
        return False
    moved = "lean-project: moved " in res.stdout
    res = runner(ssh_argv(result.host, lean_check_cmd(), known_hosts=kh), timeout_s=1800)
    if not res.ok:
        _fail(result, "lean check", res)
        if not moved:
            result.notes.append("lean: the shared copy predates this run, so it stays")
            return False
        undo = runner(ssh_argv(result.host, LEAN_SHARED_UNDO_CMD, known_hosts=kh), timeout_s=600)
        if undo.ok:
            result.notes.append("lean: rolled back; ~/lean/workspace has its own Mathlib again")
            result.notes.append(LEAN_ROLLED_BACK_WARNING)
        else:
            _fail(result, "lean rollback", undo)
        return False
    times = re.findall(r"checked in ([0-9.]+ s)", res.stdout)
    timing = f": a Mathlib file in {', then '.join(times)}" if times else ""
    result.notes.append(f"lean: shared Mathlib{timing}")
    return True


def lean_reclaim_step(result: VmResult, opts: Options, runner: Runner) -> bool:
    """`--lean-reclaim report|apply` (issue #205): private Mathlib copies on the shared pins in
    the participant's checkouts and topic worktrees, reported or replaced by the link.

    `apply` checks that no turn runs, so as not to interrupt a Claude turn; stops the bridge
    so that no turn can start; checks again, since a Codex turn that started in between keeps
    running in its daemon (codex round 7 on #262; `restart_codex_step` checks after stopping
    too); reclaims only if none runs; and starts the bridge again in `finally`. Messages that
    arrive meanwhile are caught up at start. A bridge that was already stopped, or failed,
    stays as it was (review on #262). lean-project also skips any project a `lake` or `lean`
    process is working in. A failure is recorded."""
    kh = opts.known_hosts
    apply = opts.lean_reclaim == "apply"

    def idle(when: str) -> bool:
        res = runner(ssh_argv(result.host, CODEX_STATUS_CMD, known_hosts=kh), timeout_s=120)
        running = running_turns(res.stdout) if res.ok else None
        if running == 0:
            return True
        why = "zab status unreadable" if running is None else f"{running} turn(s) running"
        result.notes.append(f"lean reclaim: not applied ({why}{when}); rerun it later")
        return False

    if not apply:
        res = runner(ssh_argv(result.host, lean_reclaim_cmd(False), known_hosts=kh), timeout_s=1800)
    else:
        if not idle(""):
            return True
        state = runner(ssh_argv(result.host, BRIDGE_STATE_CMD, known_hosts=kh), timeout_s=60)
        # only a clear "not running" skips the stop and the start; anything unclear stops it
        down = not state.ok and state.stdout.strip() in ("inactive", "failed")
        if not down:
            res = runner(ssh_argv(result.host, BRIDGE_STOP_CMD, known_hosts=kh), timeout_s=120)
            if not res.ok:
                _fail(result, "lean reclaim: stop the bridge", res)
                return False
        try:
            if not idle(" once the bridge stopped"):
                return True
            res = runner(
                ssh_argv(result.host, lean_reclaim_cmd(True), known_hosts=kh), timeout_s=1800
            )
        finally:
            if not down:
                start = runner(
                    ssh_argv(result.host, BRIDGE_START_CMD, known_hosts=kh), timeout_s=120
                )
                if not start.ok:
                    _fail(result, "lean reclaim: start the bridge", start)
    if not res.ok:
        _fail(result, "lean reclaim", res)
        return False
    lines = [line.removeprefix("lean-project: ") for line in res.stdout.splitlines()]
    count = sum(bool(re.search(r"(would be freed|: freed [0-9.]+ GB)$", line)) for line in lines)
    total = next((line for line in reversed(lines) if line.startswith("total:")), "total: ?")
    result.notes.append(f"lean reclaim: {count} project(s), {total}")
    return True


def steps_vm(row: Mapping[str, str], opts: Options, runner: Runner) -> VmResult:
    """`--lean-shared` and/or `--idle-stop` without a release: the owner check, then only
    those steps (the shared Mathlib first, the idle-stop timer last)."""
    result = VmResult(slug=slug_of(dict(row)), host="")
    try:
        result.host = host_of(row)
    except ValueError as exc:
        result.failures.append(str(exc))
        return result

    def ssh(remote: str) -> list[str]:
        return ssh_argv(result.host, remote, known_hosts=opts.known_hosts)

    if check_owner(result, row, runner, ssh, adopt=False) is None:
        return result
    done = True
    if opts.idle_stop == 0:
        done = idle_stop_step(result, opts, runner, "", pathlib.Path())
    if opts.lean_reclaim and not opts.lean_shared and not opts.idle_stop:
        runner(ssh(IDLE_HOLD_ON_CMD), timeout_s=60)
        try:
            done = lean_reclaim_step(result, opts, runner) and done
        finally:
            runner(ssh(IDLE_HOLD_OFF_CMD), timeout_s=60)
    if opts.idle_stop or opts.lean_shared:
        res = runner(ssh(MKTEMP_CMD), timeout_s=60)
        if not res.ok:
            _fail(result, "ssh", res)
            return result
        staging = res.stdout.strip()
        if not _STAGING_RE.match(staging):
            result.failures.append(f"ssh: unexpected staging directory {staging!r}")
            return result
        runner(ssh(IDLE_HOLD_ON_CMD), timeout_s=60)
        try:
            with tempfile.TemporaryDirectory(prefix="zab-push-") as local:
                shared = True
                if opts.lean_shared:
                    shared = lean_shared_step(result, opts, runner, staging)
                    done = shared and done
                if opts.lean_reclaim and not shared:
                    result.notes.append(LEAN_RECLAIM_SKIPPED)
                elif opts.lean_reclaim:
                    done = lean_reclaim_step(result, opts, runner) and done
                if opts.idle_stop:
                    done = (
                        idle_stop_step(result, opts, runner, staging, pathlib.Path(local)) and done
                    )
        finally:
            runner(ssh(remote_chain(["rm", "-rf", "--", staging])), timeout_s=60)
            runner(ssh(IDLE_HOLD_OFF_CMD), timeout_s=60)
    result.installed = result.doctor_ok = done
    return result


# ------------------------------------------------------------------------------------ CLI


def render(results: list[VmResult], *, uninstall: bool = False, steps_only: bool = False) -> str:
    whole = uninstall or steps_only  # ok or FAIL, no doctor
    rows = [
        [
            r.slug,
            r.host or "-",
            ("ok" if r.installed else "FAIL") if not whole else ("ok" if r.ok else "FAIL"),
            "-" if whole else _doctor_cell(r),
            "; ".join(
                ([f"was {r.adopted}"] if r.adopted else [])
                + r.failures
                + [f"note: {n}" for n in r.notes]
            ),
        ]
        for r in results
    ]
    head = "uninstall" if uninstall else "steps" if steps_only else "install"
    return format_table(["participant", "host", head, "doctor", "failures"], rows)


def _doctor_cell(r: VmResult) -> str:
    if r.doctor_ok:
        pending = pending_label(
            owner=r.owner_pending, sign_in=r.sign_in_pending, project=r.project_pending
        )
        return f"ok ({pending})" if pending else "ok"
    return "FAIL" if r.doctor_ok is False else "?"


#: The lines under the table that count the pending VMs end with what they wait for.
PENDING_SIGN_IN = "the participants send !login to their bots"
PENDING_OWNER = (  # #401
    "the participants join Zulip and !provision zulip (zulip_provision.py) records their IDs"
)
PENDING_PROJECT = (  # #458
    "provision_repos.py, next in the hand-over (ORGANIZERS §7.2), clones the checkout; "
    "the push_config.py after it checks it"
)
#: The dry run's answer to `MKTEMP_CMD`, so every staged path is this one.
DRY_STAGING = "/tmp/zab-push.DRYRUN"  # noqa: S108 - pretend output
#: `--dry-run` names each remote command, one line per VM (#361), so that an admin bot's plan
#: shows a few VMs' dry run inline. The commands are fixed literals of this script, or built
#: from its flags and the row: the name, with the VM and the files sent, is what can differ.
_DRY_NAMES = {
    OWNER_CMD: "owner check",
    MKTEMP_CMD: "staging dir",
    IDLE_HOLD_ON_CMD: "idle-stop hold",
    IDLE_HOLD_OFF_CMD: "hold released",
    SERVICE_INSTALL_CMD: "service install + doctor",
    SERVICE_UNINSTALL_CMD: "service uninstall",
    DOCTOR_CMD: "doctor",
    CODEX_STATUS_CMD: "zab status",
    CODEX_RESTART_CMD: "codex daemon restart",
    BRIDGE_STOP_CMD: "bridge stop",
    BRIDGE_START_CMD: "bridge start",
    BRIDGE_STATE_CMD: "bridge state",
    IDLE_STOP_OFF_CMD: "idle stop removed",
    IDLE_STOP_PROBE_CMD: "idle-stop probe",
    SPARE_TIMER_OFF_CMD: "spare's idle stop removal",
    WARM_OFF_CMD: "warm-up removed",
    warm_on_cmd(DRY_STAGING): "warm-up on",
    apt_cmd(DRY_STAGING): "apt source",
    LEAN_SHARED_UNDO_CMD: "lean rollback",
    remote_chain(["rm", "-rf", "--", DRY_STAGING]): "staging removed",
    idle_stop_on_cmd(DRY_STAGING): "idle stop installed",
    lean_shared_on_cmd(DRY_STAGING): "lean-project + shared Mathlib",
    lean_check_cmd(): "lean check",
    lean_reclaim_cmd(False): "lean reclaim report",
    lean_reclaim_cmd(True): "lean reclaim apply",
}
#: The commands built from the row, the release or the staged files: a part of each, in order.
_DRY_PARTS = (
    (shlex.join(["sudo", "install", "-d", "-o", AGENT, "-g", AGENT]), "config installed"),
    (" tool install --force ", "zab installed"),
    (INSTRUCTIONS_SCRIPT.name, "agent instructions"),
    (f"{TOOLS_SCRIPT.name} install ", "tools list"),
    (f"{SAGE_SCRIPT.name} install ", "sage wrapper"),
    (" git config --global user.name ", "git identity"),
    (shlex.join(["sudo", "test", "!", "-e"]), "checkout probe"),
)


def dry_name(remote: str) -> str:
    """The short name of a remote command; an unknown one is shown cut, never left out."""
    if remote in _DRY_NAMES:
        return _DRY_NAMES[remote]
    if remote.startswith(ADOPT_CMD):
        return "owner record"
    return next((name for part, name in _DRY_PARTS if part in remote), None) or (
        "ssh " + first_line(remote, 60)
    )


class DryRunner:
    """Answers as a healthy VM would (no SSH at all), and records each command. `lines()`: one
    line per VM, its commands by name in order, each run of `scp` as the files it sends; with
    `verbose`, every argument vector is printed whole as it comes instead. The defaults' probes
    (#704) answer as a new VM does, with no shared Mathlib and no idle-stop timer, so that the
    dry run names every step the defaults may take."""

    def __init__(
        self,
        logins: dict[str, str] | None = None,
        *,
        verbose: bool = False,
        instances: Mapping[str, str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.logins = logins or {}  # ip → slug, so the owner check answers as the row's VM
        self.instances = instances or {}  # ip → the instance the waker names for its row
        self.verbose = verbose
        self.steps: dict[str, list[tuple[str, str]]] = {}  # host → (kind, name), in order

    def __call__(
        self,
        argv: list[str],
        *,
        timeout_s: float = 120.0,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> RunResult:
        self.calls.append(argv)
        if self.verbose:
            print("dry-run:", " ".join(argv[:-1]), repr(argv[-1]) if argv[0] == "ssh" else argv[-1])
        self._record(argv)
        if argv[0] == "ssh" and argv[-1] == OWNER_CMD:
            ip = argv[-3].split("@", 1)[1]
            login = self.logins.get(ip, "")
            record = json.dumps({"slug": login, "github": "", "repo": ""})
            return RunResult(
                0,
                f"{OWNER_MARK}JSON\n{record}\n\n{OWNER_MARK}HOST\nws-{login}\n{OWNER_MARK}END\n",
                "",
            )
        if argv[0] == "ssh" and argv[-1] == MKTEMP_CMD:
            return RunResult(0, DRY_STAGING + "\n", "")
        if argv[0] == "ssh" and argv[-1] == SERVICE_INSTALL_CMD:
            return RunResult(0, '[{"name": "dry-run", "ok": true}]\n', "")
        if argv[0] == "ssh" and f"{SAGE_SCRIPT.name} install " in argv[-1]:
            return RunResult(0, f"{SAGE_IN_PLACE[2]}dry run\n", "")  # no note on every row
        if argv[0] == "ssh" and argv[-1].endswith(LEAN_PROJECT_PROBE):
            return RunResult(0, LEAN_NOT_SHARED + "\n", "")
        if argv[0] == "ssh" and argv[-1] == IDLE_STOP_PROBE_CMD:
            vm = self.instances.get(argv[-3].split("@", 1)[1], "")
            return RunResult(0, f"{IDLE_STOP_UNSET}\n{INSTANCE_MARK}{vm}\n", "")
        return RunResult(0, "", "")

    def _record(self, argv: list[str]) -> None:
        """An scp is named by the file as the VM gets it; a bot key also by the directory it
        comes from, so the line shows whose keys go to the VM."""
        if argv[0] == "ssh" and len(argv) >= 3 and "@" in argv[-3]:
            host, step = argv[-3].split("@", 1)[1], ("ssh", dry_name(argv[-1]))
        elif argv[0] == "scp" and len(argv) >= 2 and "@" in argv[-1] and ":" in argv[-1]:
            host, path = argv[-1].split("@", 1)[1].rsplit(":", 1)
            local = pathlib.PurePath(argv[-2])
            name = pathlib.PurePath(path).name
            step = ("scp", f"{local.parent.name}/{name}" if name.endswith(".zuliprc") else name)
        else:
            host, step = "", ("run", first_line(shlex.join(argv), 60))
        self.steps.setdefault(host, []).append(step)

    def lines(self) -> list[str]:
        if self.verbose:
            return []
        out = []
        for host, steps in self.steps.items():
            parts: list[str] = []
            for n, (kind, name) in enumerate(steps):
                if kind == "scp" and n and steps[n - 1][0] == "scp":
                    parts[-1] += f" {name}"
                else:
                    parts.append(f"scp {name}" if kind == "scp" else name)
            who = f"{self.logins.get(host, '?')} ({host})" if host else "local"
            out.append(f"dry-run {who}: {'; '.join(parts)}")
        return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="push_config.py", description="Roll the bridge out to the workshop VMs (README.md §2)."
    )
    parser.add_argument("--roster", required=True)
    # No default release: the config rendered here needs a zab that knows its keys (allow_emails
    # needs #92; codex round 3 on #97), and while the repository is private only a wheel works
    # (the first rollouts). Name the release explicitly. A tag installs from repo_url().
    release = parser.add_mutually_exclusive_group()
    release.add_argument(
        "--tag",
        help="zab release tag, from ZAB_REPO (an https:// or ssh:// URL) or the public repo",
    )
    release.add_argument("--wheel", help="install this wheel (built from the release tag)")
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="where zulip_provision.py wrote")
    parser.add_argument("--only", action="append", metavar="SLUG")
    parser.add_argument("--parallel", type=int, default=8)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run nothing: name each VM's commands, one line per VM, then the table",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="with --dry-run: print every command whole"
    )
    parser.add_argument("--uninstall", action="store_true", help="the rollback (README.md §6)")
    parser.add_argument("--codex-binary", default=CODEX_BINARY)
    parser.add_argument("--claude-binary", default=CLAUDE_BINARY)
    parser.add_argument(
        "--known-hosts",
        default=str(DEFAULT_KNOWN_HOSTS),
        help="the VMs' host keys as recorded by scan_hosts.py (pinned; nothing else is trusted)",
    )
    parser.add_argument(
        "--adopt-spare",
        action="append",
        default=[],
        metavar="SLUG",
        help="this row's VM is a spare being handed over: rewrite its owner record first "
        "(ORGANIZERS §7.2); its checkout, which provision_repos.py clones next, is pending while "
        "absent",
    )
    parser.add_argument(
        "--claude-raw-log",
        choices=("off", "meta", "content"),
        default="off",
        help="record each Claude turn's raw stream in ~/.local/state/zab/raw/ on the VM: meta "
        "(types and lengths) or content (the lines too: the participant's work); default off",
    )
    parser.add_argument(
        "--claude-steer",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="[backends.claude] steer: a message sent during a Claude turn steers it (issue "
        "#36). On by default (#310); --no-claude-steer turns it off. --claude-steer changes "
        "nothing, except that a release without the key is then refused rather than left off",
    )
    parser.add_argument(
        "--multi-party",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="bridge.multi_party: agents in channels with other participants (issue #35); "
        "--no-multi-party is the fleet-wide kill switch",
    )
    parser.add_argument(
        "--refserve-url",
        default=REFSERVE_URL,
        help="the central file server for turn snapshots (SPEC §10.7); '' leaves links off",
    )
    parser.add_argument(
        "--refserve-owner",
        metavar="ORG",
        help="[render] refserve_owner (#693): the GitHub organization that --refserve-url "
        "serves, the workshop's github_org in admin.toml; the bridge publishes snapshots only "
        f"for repositories of it. Default {REFSERVE_OWNER}, the default --refserve-url's. "
        "Needs a release",
    )
    parser.add_argument(
        "--mention-after-s",
        type=int,
        metavar="SECONDS",
        help="[bridge] mention_after_s (SPEC §4.8; #711): a turn that ends after running SECONDS "
        f"or more mentions its owner, so the topic shows @ in Zulip; 0 to {MENTION_AFTER_MAX}, "
        f"0: at any length. The workshop's mention_after_s in admin.toml; default "
        f"{MENTION_AFTER_S}. Needs a release",
    )
    parser.add_argument(
        "--idle-stop",
        type=int,
        metavar="MINUTES",
        help="kept VMs (issue #207): power the VM off after MINUTES with no turn, no message, "
        "load under 0.5 and no login (60 suggested); 0 removes the timer, and records that it "
        "was turned off on purpose (#704). Without --tag/--wheel only this step runs; default: "
        "left as it is, or --idle-stop-default",
    )
    parser.add_argument(
        "--restart-codex",
        action="store_true",
        help="also restart the Codex daemon (before the bridge) when no Codex turn is running: "
        "the first rollout of the per-bot git identity needs it (issue #214); a VM with a "
        "running Codex turn is reported and keeps its daemon",
    )
    parser.add_argument(
        "--lean-shared",
        action="store_true",
        help="install lean-project and make ~/lean/workspace's Mathlib the VM's one shared, "
        "read-only copy that Lean projects link to (issue #205), then check a Mathlib file and "
        "a comparator run as agent; runs alone without --tag/--wheel",
    )
    parser.add_argument(
        "--lean-reclaim",
        choices=("report", "apply"),
        default="",
        help="private Mathlib copies (about 8 GB each) in the participant's checkouts and topic "
        "worktrees, on the shared pins: report them, or apply: replace each with the shared "
        "link (only when no turn runs; projects lean or lake is working in are skipped). Needs "
        "lean-project (--lean-shared, here or before)",
    )
    parser.add_argument(
        "--lean-shared-default",
        action="store_true",
        help="with a release (#704): --lean-shared on each VM whose Mathlib isn't shared yet; "
        "a VM that has it is only probed (no timing), one with no Lean workspace is noted",
    )
    parser.add_argument(
        "--idle-stop-default",
        type=int,
        metavar="MINUTES",
        help="with a release (#704): --idle-stop MINUTES on each VM with no idle-stop timer whose "
        "waker is on (--has-waker), unless its idle stop was turned off on purpose (--idle-stop "
        "0); every other VM is left as it is",
    )
    parser.add_argument(
        "--has-waker",
        action="append",
        default=[],
        metavar="SLUG[=INSTANCE]",
        help="this row's VM is opted in to the waker (repeatable; `all`: every selected row): "
        "--idle-stop-default turns idle stop on there alone; with =INSTANCE, the waker's "
        "instance ID for the row, only on the VM that is that instance",
    )
    args = parser.parse_args(argv)
    if args.restart_codex and (args.uninstall or not (args.tag or args.wheel)):
        parser.error("--restart-codex needs a release (--tag or --wheel)")
    mention_after_s = MENTION_AFTER_S if args.mention_after_s is None else args.mention_after_s
    if not 0 <= mention_after_s <= MENTION_AFTER_MAX:
        parser.error(f"--mention-after-s takes 0 to {MENTION_AFTER_MAX} seconds")
    if args.mention_after_s is not None and (args.uninstall or not (args.tag or args.wheel)):
        parser.error("--mention-after-s needs a release (--tag or --wheel): it is written there")
    refserve_owner = REFSERVE_OWNER if args.refserve_owner is None else args.refserve_owner
    if not GITHUB_OWNER.fullmatch(refserve_owner):
        parser.error("--refserve-owner must be a GitHub organization's name")
    if args.refserve_owner is not None and (args.uninstall or not (args.tag or args.wheel)):
        parser.error("--refserve-owner needs a release (--tag or --wheel): it is written there")
    if args.idle_stop is not None and (args.idle_stop < 0 or 0 < args.idle_stop < IDLE_STOP_MIN):
        parser.error(f"--idle-stop takes at least {IDLE_STOP_MIN} minutes (0 removes the timer)")
    if args.idle_stop is not None and args.uninstall:
        parser.error("--idle-stop can't be combined with --uninstall")
    if (args.lean_shared or args.lean_reclaim) and args.uninstall:
        parser.error("--lean-shared and --lean-reclaim can't be combined with --uninstall")
    defaults = args.lean_shared_default or args.idle_stop_default is not None
    if defaults and (args.uninstall or not (args.tag or args.wheel)):
        parser.error("the defaults (--lean-shared-default, --idle-stop-default) need a release")
    if args.lean_shared_default and args.lean_shared:
        parser.error("give --lean-shared or --lean-shared-default, not both")
    if args.idle_stop_default is not None and args.idle_stop is not None:
        parser.error("give --idle-stop or --idle-stop-default, not both")
    if args.idle_stop_default is not None and args.idle_stop_default < IDLE_STOP_MIN:
        parser.error(f"--idle-stop-default takes at least {IDLE_STOP_MIN} minutes")
    bad = [w for w in args.has_waker if "=" in w and not INSTANCE_RE.fullmatch(w.partition("=")[2])]
    if bad:
        parser.error(f"--has-waker takes SLUG or SLUG=i-<instance ID>, not {bad[0]!r}")
    if "all" in (w.lower() for w in args.has_waker) and len(args.has_waker) > 1:
        parser.error("--has-waker all names every selected row: give it alone")
    # a slug given twice would keep its last value, and a bare one drops the instance check
    # (review round 3 of #705)
    given = [w.lower().partition("=")[0] for w in args.has_waker]
    twice = next((s for s in given if given.count(s) > 1), None)
    if twice is not None:
        parser.error(f"--has-waker names {twice} twice: give each row once")
    if args.has_waker and args.idle_stop_default is None:
        parser.error("--has-waker only says where --idle-stop-default applies")
    steps_only = (
        args.idle_stop is not None or args.lean_shared or bool(args.lean_reclaim)
    ) and not (args.tag or args.wheel)
    if not args.uninstall and not steps_only and not (args.tag or args.wheel):
        parser.error("name the release: --tag vX (public repository) or --wheel PATH")
    if args.refserve_url and (
        not args.refserve_url.startswith("https://") or any(c.isspace() for c in args.refserve_url)
    ):
        parser.error("--refserve-url must be an https:// URL (or '' for none)")

    try:
        rows = select_rows(load_roster(args.roster), args.only)
        pairs = [w.lower().partition("=") for w in args.has_waker if w.lower() != "all"]
        named = {slug: instance for slug, _, instance in pairs}
        select_rows(load_roster(args.roster), list(named))  # an unknown slug is an error too
    except ValueError as exc:
        print(f"roster: {exc}", file=sys.stderr)
        return 1
    if "all" in (w.lower() for w in args.has_waker):
        named = {slug_of(r): "" for r in rows}
    woken = list(named)
    multi_party: bool | None = args.multi_party
    no_release = args.uninstall or steps_only  # nothing installed: no release config to read
    source = None if no_release else artifact_config(args.wheel, args.tag)
    if not no_release and source is None:
        # unknown is not "absent" (codex on #167): without the release's config, neither
        # writing the key nor leaving it out is safe
        what = f"the wheel {args.wheel}" if args.wheel else f"the tag {args.tag}"
        print(
            f"cannot read zab/config.py of {what}: fetch the tag (git fetch --tags) or pass "
            "the release's --wheel",
            file=sys.stderr,
        )
        return 2
    if not no_release and not supports_multi_party(source):
        if not args.multi_party:
            print(
                "note: --no-multi-party changes nothing: the release being installed has no "
                "multi-party feature (bridge.multi_party)",
                file=sys.stderr,
            )
        multi_party = None
    claude_steer: bool | None = args.claude_steer is not False  # on unless --no-claude-steer
    if not no_release and not supports_claude_steer(source):
        if args.claude_steer:  # asked for by name: never rolled out silently without it
            print(
                "--claude-steer: the release being installed has no [backends.claude] steer key",
                file=sys.stderr,
            )
            return 2
        claude_steer = None
    if not no_release:
        print(multi_party_line(multi_party))
        print(claude_steer_line(claude_steer))
        print(mention_after_line(mention_after_s))
        selected = {slug_of(r) for r in rows}
        for line in default_lines(args.lean_shared_default, args.idle_stop_default, selected,
                                  selected & set(woken)):  # fmt: skip
            print(line)
    opts = Options(
        tag=args.tag,
        wheel=args.wheel,
        out_dir=pathlib.Path(args.out_dir),
        codex_binary=args.codex_binary,
        claude_binary=args.claude_binary,
        uninstall=args.uninstall,
        known_hosts=args.known_hosts,
        adopt_spares=frozenset(args.adopt_spare),
        refserve_url=args.refserve_url.rstrip("/"),
        refserve_owner=refserve_owner,
        multi_party=multi_party,
        claude_raw_log=args.claude_raw_log,
        claude_steer=claude_steer,
        idle_stop=args.idle_stop,
        restart_codex=args.restart_codex,
        worktree_hook=not no_release and supports_worktree_post_create(source),
        mention_after_s=mention_after_s,
        lean_shared=args.lean_shared,
        lean_reclaim=args.lean_reclaim,
        lean_shared_default=args.lean_shared_default,
        idle_stop_default=args.idle_stop_default or 0,
        has_waker=named,
    )
    dry = (
        DryRunner(
            {r["ip"]: slug_of(r) for r in rows if r.get("ip")},
            verbose=args.verbose,
            instances={r["ip"]: named.get(slug_of(r), "") for r in rows if r.get("ip")},
        )
        if args.dry_run
        else None
    )
    runner: Runner = dry or subprocess_runner
    results = run_parallel(
        rows,
        lambda row: (steps_vm if steps_only else rollout_vm)(row, opts, runner),
        parallel=1 if args.dry_run else args.parallel,
        on_error=lambda row, exc: VmResult(
            slug=slug_of(row),
            host=row.get("host", ""),
            failures=[f"{type(exc).__name__}: {exc}"],
        ),
    )
    for line in dry.lines() if dry else []:
        print(line)
    print(render(results, uninstall=args.uninstall, steps_only=steps_only))
    bad = [r for r in results if not r.ok]
    pending = [
        f"{len(vms)} VM(s) ok, {what} pending: expected until {until}"
        for what, vms, until in (
            ("sign-in", [r for r in results if r.ok and r.sign_in_pending], PENDING_SIGN_IN),
            ("owner", [r for r in results if r.ok and r.owner_pending], PENDING_OWNER),
            ("project", [r for r in results if r.ok and r.project_pending], PENDING_PROJECT),
        )
        if vms
    ]
    if pending and not no_release:
        print("\n" + "\n".join(pending))
    if bad:
        print(f"\n{len(bad)} VM(s) failed", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
