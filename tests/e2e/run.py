"""Run the end-to-end suite against the test VM (issue #263), from the development machine.

    uv run --with boto3 python tests/e2e/run.py [--commit REV] [--scenarios all|fast|slow|1,2,5-7]
        [--no-deploy] [--keep-running] [--merge-into REPORT.json]

1. Finds the test VM by its tags (`zab-role=test`, the scope, `participant=<slug>`), takes its
   lock (`<report dir>/<instance>.lock`: one run per VM), starts it if it is stopped
   (`harness/aws.py`: test infrastructure only), and checks that the roster's address is this
   VM's.
2. Builds the wheel from the commit under test, in a temporary `git worktree`, and deploys it
   with that commit's `push_config.py --wheel … --only <slug> --claude-steer` (the rig's roster,
   out dir, known_hosts and key), then records `<commit> <wheel>` on the VM. `--no-deploy`
   tests what is installed and reports that recorded build; it refuses a `--commit` the VM
   doesn't have, and a VM without a record fails the report (unknown provenance).
3. Runs the selected scenarios of THIS checkout's `tests/e2e/` (the reference suite), with
   `ZAB_LIVE=e2e`, and collects their results.
4. Whatever ended the run (an error, Ctrl-C, SIGTERM or SIGHUP, each turned into an exception;
   pytest is given SIGINT and two minutes to run its own clean-up), undoes what a scenario may
   have left (`harness/cleanup.py`: the Zulip block, a frozen bridge, idle stop) and stops the
   VM unless `--keep-running`; a step that fails is a run problem in the report. A scenario
   whose own clean-up failed is one too, and when it left the VM unfit for the scenarios after
   it (idle stop still installed), pytest stops there (`conftest`, #517).
5. Writes `report.json`, `summary.md` and `pytest.log` (pytest's whole output, #517) under
   `ZAB_E2E_REPORT_DIR/<time>-<sha>/`, named after the build tested (the results merged into
   `--merge-into`, which must exist: the slow scenarios run once per batch), and prints the
   summary.

The exit code follows the report's verdict, a merge gate: 0 for PASS (every selected scenario
passed); 1 for FAIL (a scenario failed, a `--require`d one did not pass, or the run had a
problem); 3 for NO-RESULT (nothing failed, but nothing passed or a scenario was skipped); 2 when
the run could not start. A release candidate runs with `--require rc`: every scenario must
pass. The rig's inputs: `harness/config.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # `e2e.harness` as a script too

from e2e.harness import config, report
from e2e.harness.admin import failure
from e2e.harness.cleanup import ATTEMPTS, PAUSE_S, UNBLOCK
from e2e.harness.secret_files import described
from e2e.harness.vm import Result

HERE = Path(__file__).resolve().parent
Command = Callable[..., Result]
#: What the last successful deploy put on the VM: `<commit> <wheel>` (`--no-deploy` reads it).
BUILD_FILE = "/var/lib/zab-e2e/build"
#: The exit code of each verdict (2: the run could not start).
EXIT = {"PASS": 0, "FAIL": 1, "NO-RESULT": 3}


def lock_dir() -> Path:
    """Where the per-instance locks live: fixed, whatever the report directory."""
    return Path.home() / ".local" / "state" / "zab-e2e" / "locks"


_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_WHEEL_RE = re.compile(r"^[A-Za-z0-9_.+-]+\.whl$")


class Interrupted(Exception):
    """SIGTERM or SIGHUP: raised so that every `finally` runs (the VM is stopped, the scenarios'
    leftovers undone) instead of the process just ending (reviewer on #283)."""


def _interrupt(signum: int, frame: Any) -> None:
    raise Interrupted(signal.Signals(signum).name)


#: How long an interrupted program (pytest) gets to finish after SIGINT before it is killed:
#: the scenarios' own `finally` blocks run in that time.
GRACE_S = 120.0


def command(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 3600,
    grace: float = 0.0,
    log: Path | None = None,
) -> Result:
    """Run a local program (an argument list) in its own session, so a Ctrl-C reaches only this
    runner; on an interrupt or the timeout it gets SIGINT and `grace` seconds to finish, then is
    killed; an interrupt is raised again afterwards. Its whole output goes to `log` (stdout,
    then stderr), however it ended: only the tail is printed (#517)."""
    print("$", " ".join(argv[:6]) + (" …" if len(argv) > 6 else ""), flush=True)
    proc = subprocess.Popen(  # noqa: S603 - argument lists only
        argv, cwd=cwd, env=dict(env) if env else None, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )  # fmt: skip
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        out, err = _wind_down(proc, grace)
        err += f"\ntimed out after {timeout:.0f} s"
        _keep(log, out, err)
        return Result(124, out, err)
    except BaseException:
        _keep(log, *_wind_down(proc, grace))
        raise
    _keep(log, out, err)
    sys.stdout.write(out[-4000:])
    sys.stderr.write(err[-4000:])
    return Result(proc.returncode, out, err)


def _keep(log: Path | None, out: str, err: str) -> None:
    """The output to `log`, mode 0600: a traceback prints its frames' arguments, which might
    hold a key (reviewer on #518). A log that can't be written never ends the run, and one
    whose mode can't be set (another user's) is not written; its descriptor is closed (#519)."""
    if log is not None:
        with contextlib.suppress(OSError):
            log.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                os.fchmod(f.fileno(), 0o600)  # an existing file's mode too
                f.write(out + ("\n--- stderr ---\n" + err if err else ""))


def _signal_all(proc: subprocess.Popen, sig: int) -> None:
    """`sig` to the program and everything it started (its own session: `command`)."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):  # not a group leader, or gone already
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)


def _wind_down(proc: subprocess.Popen, grace: float) -> tuple[str, str]:
    """SIGINT to the whole group and `grace` seconds to finish, then SIGKILL to the whole group
    and at most 10 s to read what is left: a descendant that still holds the output pipes
    can't hold up the clean-up and the stop (codex on #283). Interrupts are ignored meanwhile,
    and the SIGKILL is in a `finally`: a second Ctrl-C must not leave an orphaned pytest that
    could start the VM again after the runner stopped it (reviewer on #283)."""
    held = {s: signal.signal(s, signal.SIG_IGN) for s in _ALL_SIGNALS}
    try:
        if grace > 0:
            _signal_all(proc, signal.SIGINT)
            with contextlib.suppress(subprocess.TimeoutExpired):
                out, err = proc.communicate(timeout=grace)
                return out or "", err or ""
        _signal_all(proc, signal.SIGKILL)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.wait()
            return "", "(output lost: a descendant held the pipes)"
        return out or "", err or ""
    finally:
        if proc.poll() is None:  # whatever cut this short, the program does not outlive it
            _signal_all(proc, signal.SIGKILL)
            proc.wait()
        for s, handler in held.items():
            signal.signal(s, handler)


@dataclass
class Run:
    cfg: config.RigConfig
    ec2: Any
    run: Command = command
    vm_factory: Callable[[str], Any] | None = None
    sleep: Callable[[float], None] = time.sleep

    def git(self, *args: str) -> str:
        res = self.run(["git", "-C", str(config.CHECKOUT), *args], timeout=300)
        if not res.ok:
            raise RuntimeError(f"git {args[0]}: {res.stderr.strip()[:300]}")
        return res.stdout.strip()

    def ensure_running(self, instance_id: str) -> str:
        """Start the VM if needed; its address once SSH answers. A VM `stopping` (the run
        before stopped it) is waited for until it leaves that state, for `stopped`, `pending` or
        `running`: the test waker may start it again for that run's last messages (SPEC §13.4),
        and the polls can miss the moment it was stopped (#305). Only a VM read `stopped` is
        started; insisting on `stopped` is for whoever stops the VM."""
        state = self.ec2.state(instance_id)
        if state == "stopping":
            state = self.ec2.wait_while(instance_id, "stopping", timeout=600)
        if state == "stopped":
            self.ec2.start(instance_id)
        if state != "running":
            self.ec2.wait_state(instance_id, "running", timeout=600)
        ip = self.ec2.public_ip(instance_id)
        vm = self.vm_factory(ip) if self.vm_factory else None
        for _ in range(60):
            if vm is None or vm.reachable():
                return ip
            self.sleep(10)
        raise TimeoutError(f"{ip}: no SSH 10 minutes after the start")

    def vm(self, ip: str) -> Any:
        if self.vm_factory is None:
            raise RuntimeError("no SSH to the VM")
        return self.vm_factory(ip)

    def installed_build(self, ip: str) -> tuple[str, str] | None:
        """(commit, wheel) the last deploy recorded on the VM, or None when unknown."""
        res = self.vm(ip).run(["cat", BUILD_FILE], check=False)
        parts = res.stdout.split() if res.ok else []
        if len(parts) == 2 and _SHA_RE.match(parts[0]) and _WHEEL_RE.match(parts[1]):
            return parts[0], parts[1]
        return None

    @contextlib.contextmanager
    def tree(self, sha: str, work: Path) -> Iterator[Path]:
        """A temporary `git worktree` of the tested build, for its wheel and its admin scripts
        (push_config, idle_stop: codex on #285), removed afterwards."""
        tree = work / "tree"
        self.git("worktree", "add", "--detach", str(tree), sha)
        try:
            yield tree
        finally:
            self.git("worktree", "remove", "--force", str(tree))

    def build_and_deploy(self, sha: str, tree: Path, ip: str = "") -> tuple[str, str]:
        """(wheel name, push_config's last line); RuntimeError when the deploy failed. The VM
        records the build only after a successful deploy (`installed_build`, codex on #283)."""
        vm = self.vm(ip) if ip else None
        if vm:
            vm.run(["sudo", "rm", "-f", BUILD_FILE])  # unknown until this deploy succeeds
        dist = tree.parent / "dist"
        built = self.run(["uv", "build", "--wheel", "--out-dir", str(dist)], cwd=tree)
        wheels = sorted(dist.glob("*.whl")) if built.ok else []
        if len(wheels) != 1:
            raise RuntimeError(f"wheel build failed: {built.stderr.strip()[-300:]}")
        deployed = self.push_config(tree, "--wheel", str(wheels[0]), "--claude-steer")
        last = (deployed.stdout.strip().splitlines() or [""])[-1][:200]
        if not deployed.ok:
            raise RuntimeError(f"push_config failed (exit {deployed.returncode}): {last}")
        if vm:
            vm.run(["sudo", "install", "-d", "-m", "0755", str(Path(BUILD_FILE).parent)])
            vm.script('printf "%s %s\\n" "$1" "$2" > "$3"', sha, wheels[0].name, BUILD_FILE,
                      sudo=True)  # fmt: skip
        return wheels[0].name, last

    def push_config(self, tree: Path | None, *extra: str) -> Result:
        """The tested build's `push_config.py` (this checkout's without a tree), for the rig's
        participant only, with its roster, out dir, known_hosts and key (`ZAB_SSH_IDENTITY`)."""
        base = (tree or config.CHECKOUT) / "workshop" / "admin"
        push = [
            sys.executable,
            str(base / "push_config.py"),
            *("--roster", str(self.cfg.roster), "--only", self.cfg.slug),
            *("--out-dir", str(self.cfg.out_dir), "--known-hosts", str(self.cfg.known_hosts)),
            *extra,
        ]
        env = {**os.environ, "ZAB_SSH_IDENTITY": str(self.cfg.ssh_key)}
        return self.run(push, cwd=tree or config.CHECKOUT, env=env, timeout=1800)

    def roster_ip(self) -> str:
        """The address the roster gives the rig's participant: push_config deploys there."""
        with open(self.cfg.roster, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                clean = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k}
                if (clean.get("slug") or clean.get("github", "").lower()) == self.cfg.slug:
                    return clean.get("ip", "")
        return ""

    def cleanup(self, instance_id: str, selected: list[int], tree: Path | None) -> list[str]:
        """Undo what an interrupted scenario may have left (`harness/cleanup.py`): the Zulip
        block (4), a frozen bridge (3), idle stop (11). Best effort: what failed is returned,
        for the report. A stopped VM keeps nothing of 3 and 4, so it is only started for 11."""
        problems: list[str] = []
        try:
            if self.ec2.state(instance_id) != "running":
                if 11 not in selected:
                    return problems
                self.ensure_running(instance_id)
            vm = self.vm(self.ec2.public_ip(instance_id))
        except Exception as exc:
            return [f"cleanup: no VM to clean: {type(exc).__name__}: {exc}"]

        def thaw() -> None:
            pid = vm.unit().get("MainPID", "0")
            if pid.isdigit() and int(pid) > 1:
                vm.signal(int(pid), "CONT")

        def idle_stop_off() -> None:  # tried as scenario 11's clean-up is (#517)
            for attempt in range(1, ATTEMPTS + 1):
                done = self.push_config(tree, "--idle-stop", "0")
                if done.ok:
                    return
                if attempt < ATTEMPTS:
                    self.sleep(PAUSE_S)
            raise RuntimeError(f"push_config --idle-stop 0: {failure(done, self.cfg.slug)}")

        steps = [("unblock Zulip", lambda: vm.script(UNBLOCK, sudo=True)), ("SIGCONT", thaw)]
        if 11 in selected:
            steps.append(("idle stop off", idle_stop_off))
        for what, step in steps:
            try:
                step()
            except Exception as exc:
                problems.append(f"cleanup: {what}: {type(exc).__name__}: {exc}")
        return problems

    def scenarios(
        self, spec: str, json_path: Path, admin: Path | None = None, log: Path | None = None
    ) -> report.Report:
        """The selected scenarios' results. Every selected scenario has a row (one without a
        recorded outcome, after an interrupt, failed), and a pytest exit status other than 0
        is a run problem even when every row passed (codex on #283). `admin` is the tested
        build's `workshop/admin`, which the scenarios' push_config runs come from; pytest's
        whole output goes to `log`."""
        env = {**os.environ, "ZAB_LIVE": "e2e", "ZAB_E2E_SCENARIOS": spec}
        env["ZAB_E2E_REPORT_JSON"] = str(json_path)
        # pytest runs in the checkout and loads the rig again: give it this run's absolute
        # paths, not relative ones from where run.py started (codex on #283)
        cfg = self.cfg
        env.update(
            ZAB_E2E_ROSTER=str(cfg.roster),
            ZAB_E2E_TESTER_ZULIPRC=str(cfg.tester_zuliprc),
            ZAB_E2E_TESTER2_ZULIPRC=str(cfg.tester2_zuliprc),
            ZAB_E2E_SSH_KEY=str(cfg.ssh_key),
            ZAB_E2E_OUT_DIR=str(cfg.out_dir),
            ZAB_E2E_KNOWN_HOSTS=str(cfg.known_hosts),
            ZAB_E2E_REPORT_DIR=str(cfg.report_dir),
        )
        if admin is not None:
            env["ZAB_E2E_ADMIN_DIR"] = str(admin)
        argv = [sys.executable, "-m", "pytest", str(HERE), "-m", "e2e", "-p", "no:cacheprovider"]
        done = self.run([*argv, "-q", "-rs"], cwd=config.CHECKOUT, env=env, timeout=4 * 3600,
                        grace=GRACE_S, log=log)  # fmt: skip
        ran = report.Report.read(json_path) if json_path.exists() else report.Report()
        seen = {r.number for r in ran.results}
        for n in report.select(spec):
            if n not in seen:
                name = report.SCENARIOS[n][0]
                ran.results.append(report.ScenarioResult(n, name, "failed", reason="no result"))
        ran.results.sort(key=lambda r: r.number)
        if done.returncode != 0:
            ran.problems.append(f"pytest exited {done.returncode}")
        return ran


def required(spec: str) -> list[int]:
    """`--require`: nothing, `rc` (every scenario: a release candidate's run), or a selection."""
    if not spec.strip():
        return []
    return report.select("all" if spec.strip() == "rc" else spec)


def main(
    argv: list[str] | None = None, *, make: Callable[[config.RigConfig], Run] | None = None
) -> int:
    parser = argparse.ArgumentParser(prog="tests/e2e/run.py", description=__doc__.split("\n")[0])
    parser.add_argument(
        "--commit",
        help="the commit under test (default HEAD); with --no-deploy, the build the VM must have",
    )
    parser.add_argument("--scenarios", default="all", help="all, fast, slow, or 1,2,5-7")
    parser.add_argument("--no-deploy", action="store_true", help="test what is installed")
    parser.add_argument("--keep-running", action="store_true", help="leave the VM running")
    parser.add_argument("--merge-into", type=Path, help="an earlier report.json of this batch")
    parser.add_argument(
        "--require",
        default="",
        metavar="SPEC",
        help="scenarios that must pass: a skip of one fails the run (`rc`: all of them, for "
        "a release candidate; else as --scenarios)",
    )
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        report.select(args.scenarios)
        args.required = required(args.require)
    except ValueError as exc:
        print(f"e2e: {exc}", file=sys.stderr)
        return 2
    if cfg.missing():
        print("e2e: the rig's inputs are missing: " + "; ".join(cfg.missing()), file=sys.stderr)
        return 2
    if args.merge_into and not args.merge_into.is_file():
        print(f"e2e: --merge-into {args.merge_into}: no such report", file=sys.stderr)
        return 2
    runner = make(cfg) if make else _default_run(cfg)
    try:
        sha = runner.git("rev-parse", "--verify", f"{args.commit or 'HEAD'}^{{commit}}")
        instance_id = str(runner.ec2.find({"participant": cfg.slug})["InstanceId"])
    except (RuntimeError, LookupError) as exc:
        print(f"e2e: {exc}", file=sys.stderr)
        return 2
    # one run per VM at a time: another's deploy or stop would land in this one's scenarios. The
    # lock's place doesn't depend on the report directory (reviewer on #283)
    locks = lock_dir()
    locks.mkdir(parents=True, exist_ok=True)
    lock = open(locks / f"{instance_id}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"e2e: another run is using {instance_id} ({lock.name})", file=sys.stderr)
        return 2
    signals = (signal.SIGTERM, signal.SIGHUP)
    previous = {s: signal.signal(s, _interrupt) for s in signals}
    started = dt.datetime.now(dt.UTC)
    # pytest's output, kept until the report's directory (named after the build) is known
    log = cfg.report_dir / f".{started:%Y%m%dT%H%M%SZ}-{instance_id}-pytest.log"
    try:
        result = _run(runner, args, sha, instance_id, started, log)
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        lock.close()
    if args.merge_into:
        merged = report.Report.read(args.merge_into)
        try:
            merged.merge(result)  # the same build only (another build's results stay apart)
        except ValueError as exc:
            print(f"e2e: not merged into {args.merge_into}: {exc}", file=sys.stderr)
        else:
            merged.finished, merged.deploy = result.finished, result.deploy or merged.deploy
            merged.vm = result.vm or merged.vm
            result = merged
    # named after the build the report is about (with --no-deploy, the installed one)
    out = cfg.report_dir / f"{started:%Y%m%dT%H%M%SZ}-{(result.commit or sha)[:8]}"
    out.mkdir(parents=True, exist_ok=True)
    result.write(out / "report.json")
    summary = result.markdown()
    (out / "summary.md").write_text(summary, encoding="utf-8")
    if log.exists():
        log.replace(out / "pytest.log")
    print(summary)
    print(f"report: {out / 'report.json'}\nsummary: {out / 'summary.md'}")
    if (out / "pytest.log").exists():
        print(f"pytest's output: {out / 'pytest.log'}")
    return EXIT[result.verdict]


def _run(
    runner: Run,
    args: argparse.Namespace,
    sha: str,
    instance_id: str,
    started: dt.datetime,
    log: Path | None = None,
) -> report.Report:
    """Start, deploy, run the scenarios, then always clean up and stop (unless asked to keep
    it running), whatever ended the run: an error, Ctrl-C, SIGTERM or SIGHUP."""
    cfg = runner.cfg
    result = report.Report(started=f"{started:%Y-%m-%d %H:%M:%S}Z", required=args.required)
    selected = report.select(args.scenarios)
    tree: Path | None = None
    scenarios_ran = False
    with contextlib.ExitStack() as stack:
        try:
            ip = runner.ensure_running(instance_id)
            result.vm = {"instance": instance_id, "ip": ip, "region": cfg.region}
            listed = runner.roster_ip()
            if listed != ip:  # push_config deploys to the roster's address: it must be this VM
                raise RuntimeError(f"the roster says {listed or 'no ip'}, the test VM is {ip}")
            if args.no_deploy:
                # the results belong to the build the VM has, as the last deploy recorded it
                installed = runner.installed_build(ip)
                if installed is None:
                    result.problems.append("the installed build is unknown: deploy with run.py")
                elif args.commit and installed[0] != sha:
                    raise RuntimeError(f"the VM has {installed[0][:12]}, not {sha[:12]}")
                else:
                    result.commit, result.wheel = installed
            else:
                result.commit = sha
            work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="zab-e2e-")))
            if result.commit:
                tree = stack.enter_context(runner.tree(result.commit, work))
            if not args.no_deploy:
                result.wheel, result.deploy = runner.build_and_deploy(sha, tree, ip)
            admin = tree / "workshop" / "admin" if tree else None
            # past the address check: only now can a scenario leave anything to clean up
            scenarios_ran = True
            ran = runner.scenarios(args.scenarios, work / "pytest.json", admin, log)
            result.results, result.problems = ran.results, result.problems + ran.problems
        except (Exception, KeyboardInterrupt) as exc:  # the report says why the run stopped
            result.problems.append(f"aborted: {described(exc)}")  # never a secret's line
        finally:
            # a second signal must not cut the clean-up and the stop short
            held = {s: signal.signal(s, signal.SIG_IGN) for s in _ALL_SIGNALS}
            try:
                if scenarios_ran:  # never push_config to an address the guard refused (codex)
                    result.problems += runner.cleanup(instance_id, selected, tree)
                if not args.keep_running:
                    try:
                        runner.ec2.stop(instance_id)  # the rig stops when the run ends (#263)
                    except Exception as exc:
                        result.problems.append(f"the VM was not stopped: {exc}")
            finally:
                for s, handler in held.items():
                    signal.signal(s, handler)
            result.finished = f"{dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M:%S}Z"
    return result


_ALL_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _default_run(cfg: config.RigConfig) -> Run:
    from e2e.harness.aws import TestEc2
    from e2e.harness.vm import Vm

    if shutil.which("uv") is None:
        raise SystemExit("e2e: uv is needed to build the wheel")
    return Run(
        cfg,
        TestEc2(cfg.region, dict(cfg.scope)),
        vm_factory=lambda ip: Vm(ip, key=cfg.ssh_key, known_hosts=cfg.known_hosts),
    )


if __name__ == "__main__":
    raise SystemExit(main())
