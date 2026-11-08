#!/usr/bin/env python3
"""`trust_fix.py --roster R --known-hosts KH [--only SLUG …] [--apply [--upgrade-left-pips]]`
(README.md §10).

A VM launched from an image built before #816 keeps the trust stores of that image: its uv,
whose built-in roots hold emSign ECC Root CA - G3 up to 0.13.3 and through which `!rollout`
installs the bridge, unhashed; certifi 2026.7.22 in `~/venv`, `/opt/certbot`, lean-lsp-mcp's
environment, and the bridge's until a rollout; with Sage, miniforge's certifi and pip; and,
until its unattended upgrades run, the system's `ca-certificates`. A VM launched from an image
built before #882 also lacks `CLAUDE_CODE_CERT_STORE=system` in Claude Code's managed settings,
so the requests that take Claude Code's own CA list trust its bundled roots too (2.1.283's hold
that root). This brings each row's VM to the new image's, over SSH as `ubuntu` with the host
key pinned (`scan_hosts.py`):
`trust_fix_remote.py` runs there as root, read from stdin, with
`workshop/ami/files/bin/trust_stores.py`'s source for its check and `apt_repair.py`'s, which it
runs before its `apt-get update` (#947). Each step acts only where it
finds something older, missing or different from the image's, so a rerun, or a run on a VM from
a new image, changes nothing.

Without `--apply` nothing changes on a VM: each row says what would be done, and the check, the
image's smoke test's, fails where something is still to do. With `--apply`, each VM's run holds
off its idle stop (idle stop's hold file), from inside its lock on the VM: the remote half
writes it, renews it and removes it, so no other run can. A row without an `ip` has no VM and
is skipped. A stopped VM can't be
reached: `!start` it first. `--upgrade-left-pips` also upgrades the pips an agent installed in
`~/venv` or a uv tool's environment, which the check found holding the root (README.md §10).

One table, a row per VM: each step's outcome, and the check; then, per VM, what the check found,
what was left, and the commands that look into a run that ended badly. Exit 0 when every VM
reached passed the check (with `--apply`) or was reached (without); 1 otherwise. The remote
commands, and every command printed for the deployer, are fixed literals with the roster's
validated addresses and slugs: nothing from a VM reaches a shell or a printed command.
"""

import argparse
import base64
import json
import os
import pathlib
import pwd
import re
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from admin_common import (
    DEFAULT_KNOWN_HOSTS,
    Runner,
    first_line,
    format_table,
    host_of,
    load_roster,
    redact,
    run_parallel,
    select_rows,
    slug_of,
    ssh_argv,
    subprocess_runner,
)

REMOTE = pathlib.Path(__file__).resolve().parent / "trust_fix_remote.py"
TRUST_STORES = pathlib.Path(__file__).resolve().parents[1] / "ami/files/bin/trust_stores.py"
PLACEHOLDER = 'TRUST_STORES = ""  # trust_fix.py replaces this line\n'
#: The rollout's repair of an older image's expired apt source (#947), which the remote half runs
#: before its `apt-get update`, so that the update doesn't fail on that source.
APT_REPAIR = pathlib.Path(__file__).resolve().parent / "apt_repair.py"
APT_PLACEHOLDER = 'APT_REPAIR = ""  # trust_fix.py replaces this line\n'
#: Fixed literals: the script arrives on stdin and runs as root, from /.
REMOTE_CMD = "cd / && sudo /usr/bin/python3 -I -"
PROBE_CMD = "true"  # an --apply's first connection: the VM is reached, before anything runs
PIPS = "--upgrade-left-pips"
#: A VM takes minutes; this bounds one that hangs, inside the idle-stop hold's hour.
TIMEOUT_S = 3000
SSH_FAILED = 255  # ssh's own exit status: no connection, or one cut
STEPS = ["ca-certificates", "uv", "venv", "certbot", "uv tools", "conda", "claude store"]
COLUMNS = ["participant", "host", *STEPS, "check"]
#: The remote half's lock (`trust_fix_remote.LOCK`), which a run holds for as long as it lives.
LOCK = "/run/zab-trust-fix.lock"
#: The remote half and what it runs, as `ps` lists them, each pattern bracketed so that neither
#: the grep nor the shell around it matches itself.
PROCS = (
    "ps -eo pid,etime,args | grep -E '[/]usr/bin/python3 -I -( |$)|[c]onda install|[a]pt-get"
    "|[d]pkg |[u]v pip install|[a]pt_repair[.]py|[a]pt-mark '"
)
#: Whether a repair still runs there (Codex round 4 on #856), on its first line: `none running`
#: only when no run holds the lock and none of PROCS runs either, since a run killed can leave
#: a child going (review round 3, N1). Then those processes, with their PIDs.
LOOK_CMD = (
    f"p=$({PROCS}); if ! sudo flock -n {LOCK} true; then echo 'a repair is still running here"
    " (its lock is held)'; elif [ -n \"$p\" ]; then echo 'no repair holds the lock, but these"
    " still run:'; else echo 'none running'; fi; [ -z \"$p\" ] || printf '%s\\n' \"$p\""
)
#: A process of the look's list that never ends: PID, typed from that list (review round 3, N2).
KILL_CMD = "sudo kill PID"
DPKG_CMD = "sudo dpkg --configure -a"
#: conda's environments by the names the remote half gives them, with their fixed prefixes.
CONDA_PREFIXES = {"base": "/home/agent/miniforge", "sage": "/home/agent/miniforge/envs/sage"}
CONDA_LIST = "sudo -u agent -H /home/agent/miniforge/bin/conda list --revisions -p {}"
#: Back to revision N, typed from that list, through the system store as step 4 (round 3, N5).
CONDA_BACK = (
    "sudo -u agent -H env CONDA_SSL_VERIFY=/etc/ssl/certs/ca-certificates.crt"
    " /home/agent/miniforge/bin/conda install -y --revision N -p {}"
)
#: Claude Code's managed settings (`trust_fix_remote.MANAGED`), when step 5 left them as they
#: are, not being a JSON object, or found none (#882): the look, and the image's file, every key
#: build.sh and the first boot write, in its place.
MANAGED = "/etc/claude-code/managed-settings.json"
MANAGED_LOOK = f"sudo cat {MANAGED}"
MANAGED_IMAGE = (
    '{"env": {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_CERT_STORE": "system"},'
    ' "disableClaudeAiConnectors": true}'
)
MANAGED_PUT = f"echo '{MANAGED_IMAGE}' | sudo install -m 0644 /dev/stdin {MANAGED}"
#: A token in a conda channel's URL (`…/t/<token>/…`), which a conda error can quote.
CONDA_TOKEN = re.compile(r"(/t/)[A-Za-z0-9_-]{6,}(?=/)")


def scrub(text: str) -> str:
    """VM text as the table and the summary show it: one line, every character that doesn't
    print escaped (a newline in a file name would forge a line: review round 2 on #856, B1),
    `admin_common.redact`'s tokens and a conda channel's redacted."""
    text = CONDA_TOKEN.sub(r"\1[redacted]", redact(text))
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in text)


@dataclass(slots=True)
class FixResult:
    slug: str
    host: str = "-"
    report: dict[str, Any] = field(default_factory=dict)
    problem: str = ""
    skipped: bool = False
    cut: bool = False  # this script stopped waiting: the run may still be going on the VM

    def passed(self, apply: bool) -> bool:
        return self.skipped or (bool(self.report) and (self.report.get("ok") or not apply))

    def steps(self) -> dict[str, str]:
        steps = self.report.get("steps")
        return {str(k): str(v) for k, v in steps.items()} if isinstance(steps, dict) else {}

    def unfinished(self) -> bool:
        """A step of a VM reached was blocked or failed: a rerun may finish it."""
        return any("blocked" in s or "FAILED" in s for s in self.steps().values())

    def left(self) -> list[dict[str, str]]:
        return [x for x in self.report.get("left") or [] if isinstance(x, dict)]

    def pips_left(self) -> bool:
        return any(x.get("kind") == "pip" for x in self.left())

    def row(self) -> list[str]:
        if not self.report:
            check = first_line(self.problem) or "-"
        elif not self.report.get("ok"):
            check = "FAILED"
        else:
            check = f"ok, {len(self.left())} left (README §10)" if self.left() else "ok"
        cells = [self.steps().get(s, "-") for s in STEPS]
        return [self.slug, self.host, *(scrub(c) for c in cells), scrub(check)]


def remote_script() -> str:
    """trust_fix_remote.py with trust_stores.py's and apt_repair.py's sources in place of their
    placeholders."""
    text = REMOTE.read_text(encoding="utf-8")
    for placeholder, path in ((PLACEHOLDER, TRUST_STORES), (APT_PLACEHOLDER, APT_REPAIR)):
        name, count = placeholder.split(" ", 1)[0], text.count(placeholder)
        if count == 0:
            raise ValueError(f"{REMOTE.name}: its {name} placeholder is missing")
        if count > 1:  # #957's review, N1: exactly once, or the source goes in twice
            raise ValueError(f"{REMOTE.name}: its {name} placeholder is there {count} times")
        source = base64.b64encode(path.read_bytes()).decode("ascii")
        text = text.replace(placeholder, placeholder.replace('""', f'"{source}"', 1))
    return text


def parse_report(stdout: str) -> dict[str, Any] | None:
    """The JSON object on the last non-empty line of the remote script's output."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        report = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        return None
    return report if isinstance(report, dict) else None


def fix_vm(
    row: Mapping[str, str],
    runner: Runner,
    *,
    apply: bool,
    script: str,
    known_hosts: str,
    pips: bool = False,
) -> FixResult:
    slug = slug_of(dict(row))
    if not (row.get("ip") or "").strip():
        return FixResult(slug=slug, problem="no VM (no ip)", skipped=True)
    try:
        host = host_of(row)
    except ValueError as exc:
        return FixResult(slug=slug, problem=str(exc))

    def ssh(remote: str) -> list[str]:
        return ssh_argv(host, remote, known_hosts=known_hosts)

    cmd = REMOTE_CMD + (" --apply" if apply else "") + (f" {PIPS}" if apply and pips else "")
    if apply:  # reached, before the run: so a lost run's connection means it may have begun
        probe = runner(ssh(PROBE_CMD), timeout_s=60)
        if not probe.ok:  # unreachable, most often stopped: nothing else is tried
            return FixResult(slug=slug, host=host, problem=f"not reached: {cause(probe)}")
    # The idle-stop hold is the remote half's, inside its lock (Codex round 5 on #856): this
    # side never writes or removes it, so it can't remove another run's.
    res = runner(ssh(cmd), timeout_s=TIMEOUT_S, stdin=script)
    report = parse_report(res.stdout)
    cut = res.exit_code < 0 or res.exit_code == SSH_FAILED  # ssh's, or the runner's timeout
    if report is not None and report.get("running") is True:  # refused: another run's lock
        why = "a repair is still running here (its lock is held): nothing done"
        return FixResult(slug=slug, host=host, problem=why, cut=True)
    if report is not None:
        return FixResult(slug=slug, host=host, report=report)
    if res.exit_code == -1:  # this script's own timeout: the remote half isn't stopped by it
        why = f"cut after {TIMEOUT_S} s, and it may still be running there"
        return FixResult(slug=slug, host=host, problem=f"{why}: {cause(res)}", cut=True)
    if apply and cut:  # ssh's 255, or ssh killed, once the probe had worked (Codex round 3)
        why = "connection lost after the run began, and it may still be running there"
        return FixResult(slug=slug, host=host, problem=f"{why}: {cause(res)}", cut=True)
    why = "not reached" if cut else "no report"  # a dry run changes nothing either way
    return FixResult(slug=slug, host=host, problem=f"{why}: {cause(res)}")


def cause(res: Any) -> str:
    """The connection's last line of stderr, which ends a traceback with its exception."""
    lines = [x for x in res.stderr.splitlines() if x.strip()]
    return scrub(first_line(lines[-1], 100)) if lines else f"ssh exit {res.exit_code}"


def summary(result: FixResult, args: argparse.Namespace) -> list[str]:
    """Under the table, for one VM: a failed check's lines that name a store, or all of them,
    but what is left; what is left; and the commands that look into a run that ended badly."""
    out = []
    left_files = {str(x.get("file", "")) for x in result.left()}
    lines = [str(x).strip() for x in result.report.get("check") or []]
    lines = [x for x in lines if x.split(": ", 1)[-1] not in left_files]  # those come below
    if result.report and not result.report.get("ok"):
        why = [x for x in lines if x.startswith(("FAILED", "DISTRUSTED", "NO BUNDLE"))]
        out += [f"\n{result.slug}:", *(f"  {scrub(x)}" for x in why or lines)]
    if left := result.left():
        out.append(f"\n{result.slug}, left: installed by an agent, which no step installs:")
    for item in left:
        file = scrub(str(item.get("file", "")))
        if item.get("kind") == "pip":
            out.append(f"  a pip in {scrub(str(item.get('env', '')))}: {file}")
        elif item.get("kind") == "python":
            out.append(f"  a uv-managed CPython: {file}: leave it, and record it")
        else:
            out.append(f"  other software: {file}: leave it, and record it")
    if "left pips" in result.steps():
        out.append(f"  {PIPS}: {scrub(result.steps()['left pips'])}")
    apt = result.steps().get("apt source", "")  # before step 1's apt-get update (#947)
    if apt and not apt.startswith("apt: nothing to repair"):  # silent, as in the rollout's row
        out += [f"\n{result.slug}: the apt source, before ca-certificates' apt-get update:"]
        out += [f"  {scrub(apt)}"]
    return out + recovery(result, args)


def recovery(result: FixResult, args: argparse.Namespace) -> list[str]:
    """For a run that ended badly, the pinned-SSH commands that look into it (README.md §10):
    fixed literals, with the roster's address and conda's fixed prefixes."""

    def ssh(remote: str) -> str:
        return as_run(ssh_argv(result.host, remote, known_hosts=args.known_hosts))

    steps, out = result.steps(), []
    apt_killed = steps.get("ca-certificates", "").startswith("FAILED: apt-get install killed")
    conda = [e for e in CONDA_PREFIXES if f"FAILED: {e}:" in steps.get("conda", "")]
    if result.cut or apt_killed or conda:
        out += [f"\n{result.slug}: to see what still runs there, until it reads `none running`:"]
        out += [f"  {ssh(LOOK_CMD)}"]
        out += ["  one of them that never ends, by its PID from that list, then the dry run:"]
        out += [f"  {ssh(KILL_CMD)}"]
    if apt_killed:
        out += ["  then, as dpkg may be left half-configured:", f"  {ssh(DPKG_CMD)}"]
    for env in conda:
        prefix = CONDA_PREFIXES[env]
        out += [f"  then conda's {env} transactions, the last one this run's:"]
        out += [f"  {ssh(CONDA_LIST.format(prefix))}"]
        out += ["  if it broke the environment, back to the one before it, N from that list:"]
        out += [f"  {ssh(CONDA_BACK.format(prefix))}"]
    claude = steps.get("claude store", "")
    if claude.startswith(f"FAILED: {MANAGED} left as it is"):
        out += [f"\n{result.slug}: Claude Code's managed settings, which step 5 left as they are:"]
        out += [f"  {ssh(MANAGED_LOOK)}"]
        out += ["  once you know what the file held, the image's in its place, then the dry run:"]
        out += [f"  {ssh(MANAGED_PUT)}"]
    elif claude.startswith("absent"):  # step 5 writes its key alone (review round 1, N5)
        out += [f"\n{result.slug}: Claude Code's managed settings were missing, and with them"]
        out += ["  the image's other keys: the image's file in its place, then the dry run:"]
        out += [f"  {ssh(MANAGED_PUT)}"]
    return out


def as_run(argv: list[str]) -> str:
    """ARGV as this run was: through `sudo -u` if sudo ran it."""
    if os.environ.get("SUDO_USER"):
        argv = ["sudo", "-u", pwd.getpwuid(os.getuid()).pw_name, *argv]
    return shlex.join(argv)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trust_fix.py", description=__doc__.split("\n\n")[1])
    parser.add_argument("--roster", required=True)
    parser.add_argument("--only", action="append", metavar="SLUG", help="repeatable")
    parser.add_argument("--apply", action="store_true", help="change what is older")
    parser.add_argument(PIPS, action="store_true", help="with --apply: the pips left too")
    parser.add_argument("--parallel", type=int, default=6)
    parser.add_argument("--known-hosts", default=str(DEFAULT_KNOWN_HOSTS))
    args = parser.parse_args(argv)
    if args.upgrade_left_pips and not args.apply:
        parser.error(f"{PIPS} needs --apply")
    try:
        rows = select_rows(load_roster(args.roster), args.only)
        script = remote_script()
    except (OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    results = run_parallel(
        rows,
        lambda row: fix_vm(
            row,
            subprocess_runner,
            apply=args.apply,
            script=script,
            known_hosts=args.known_hosts,
            pips=args.upgrade_left_pips,
        ),
        parallel=args.parallel,
        on_error=lambda row, exc: FixResult(slug=slug_of(row), problem=f"{exc!r}"),
    )
    print(format_table(COLUMNS, [r.row() for r in results]))
    for r in results:
        if lines := summary(r, args):
            print(*lines, sep="\n")
    bad = [r.slug for r in results if not r.passed(args.apply)]
    skipped = sum(r.skipped for r in results)
    print(
        f"\n{len(results) - skipped - len(bad)} VM(s) "
        + ("fixed and checked" if args.apply else "reached (dry run: nothing changed)")
        + (f"; not done: {', '.join(bad)}" if bad else "")
        + (f"; {skipped} row(s) without a VM" if skipped else "")
    )
    pips(results, args)
    # A run that may still be going is never rerun beside itself (Codex round 3 on #856): it
    # gets its dry run, once the look above reads `none running`.
    going = [r.slug for r in results if r.cut]
    if args.apply and going:
        print(
            f"\nMay still be running on {', '.join(going)}: not in any rerun here. Run the first"
            " command under its name above until it reads `none running`, then its dry run:"
            f"\n  {rerun(args, going, apply=False)}"
        )
    # not reached (stopped since the dry run, say), failed, or with a step blocked or failed
    again = [r.slug for r in results if (not r.passed(args.apply) or r.unfinished()) and not r.cut]
    if args.apply and again:
        print(
            f"\nNot done on {', '.join(again)}: `!start` a VM not reached; wait out apt's lock"
            " where a step names it (unattended upgrades: a few minutes); fix any other cause the"
            f" rows give. Then rerun them:\n  {rerun(args, again)}"
        )
    return 1 if bad else 0


def pips(results: list[FixResult], args: argparse.Namespace) -> None:
    """Where pips were left: the command that upgrades them, but only where the VM's uv and uvx
    are 0.13.4 or newer (review round 2 on #856, B2): the download comes through their roots."""
    if args.upgrade_left_pips:
        return
    ready = [r.slug for r in results if r.pips_left() and r.report.get("uv_ok") is True]
    old = [r.slug for r in results if r.pips_left() and r.report.get("uv_ok") is not True]
    if ready:
        print(
            f"\nPips left on {', '.join(ready)}: to upgrade them to 26.2.2 or newer (as agent, from"
            " PyPI, unhashed, through uv's roots, which lack the root now; the participants'"
            f" environments, README §10):\n  {rerun(args, ready, pips=True)}"
        )
    if old:
        print(
            f"\nPips left on {', '.join(old)}, whose uv is older than 0.13.4: they can be upgraded"
            " once an --apply has brought it to 0.13.7, not through it."
        )


def rerun(
    args: argparse.Namespace, slugs: list[str], *, pips: bool | None = None, apply: bool = True
) -> str:
    """This run's command again for SLUGS, as it was given, through `sudo -u` if sudo ran it;
    without APPLY, its dry run."""
    pips = apply and (args.upgrade_left_pips if pips is None else pips)
    argv = [sys.executable, "-E", "-s", sys.argv[0]]  # this script, as the command named it
    argv += ["--roster", args.roster, "--known-hosts", args.known_hosts]
    argv += [x for slug in slugs for x in ("--only", slug)]
    return as_run([*argv, *(["--apply"] if apply else []), *([PIPS] if pips else [])])


if __name__ == "__main__":
    raise SystemExit(main())
