"""Run the admin end-to-end suite on the rig (docs/ADMIN.md §7; issue #356), from the development
machine.

    ZAB_LIVE=admin-e2e uv run --with boto3 python tests/e2e/admin/run.py --dry-run
    ZAB_LIVE=admin-e2e uv run --with boto3 python tests/e2e/admin/run.py [--commit REV]
        [--scenarios go|all|A1,A10,A12-A15] [--require SPEC] [--setup-only] [--keep-server]
    ZAB_LIVE=admin-e2e uv run --with boto3 python tests/e2e/admin/run.py --teardown

Run it from a checkout of the revision under test: its harness and scenarios are used.

1. **Setup** (`harness/setup.py`): the rig's keys, bots, channels, groups, role and server,
   made or reused; a new signed rc of the commit under test deployed with its own `deploy.sh`
   (`--bootstrap` the first time), once SSH answers steadily, and again when it could not
   connect (#804); the test workshops emptied; the fleet wheel placed. The report's
   *Start-up* lists the server's start, each SSH probe and each deploy.sh run again.
2. **The scenarios** selected (default `go`: A1 to A7, A10 and A12 to A15, ADMIN §8.2), run by
   pytest from this checkout's `tests/e2e/admin/`, with `ZAB_LIVE=admin-e2e`.
3. **Teardown, always**, whatever ended the run (an error, Ctrl-C, SIGTERM or SIGHUP): every
   fake participant terminated and its Elastic IP released, A13's probe interface deleted,
   then the server stopped (unless `--keep-server`); then **checked**: a participant left
   alive, a probe interface left, or a server not stopped, is a run problem, which fails the
   report.
4. `report.json` and `summary.md` (the #263 format) under `ZAB_ADMIN_E2E_REPORT_DIR/<time>-
   <sha>/`, with `deploy.log` (every deploy.sh run, whole), `pytest.log` (pytest's whole
   output, #517, mode 0600, any rig secret in it redacted and a run problem) and the rendered
   role policy. A secrets file that can't be read or parsed is named in the report, never
   quoted (`harness/secret_files.py`, #519).

`--dry-run` makes every lookup (AWS, Zulip) and prints every change it would make, the
teardown's included, and runs no scenario. `--teardown` only tears down. One run at a time
(`<report dir>/run.lock`). Exit codes as #263's: 0 PASS, 1 FAIL, 3 NO-RESULT, 2 could not start.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import os
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # `e2e` as a script too

from e2e.admin.harness import rig as rigs
from e2e.admin.harness import upgrade
from e2e.admin.harness.aws_rig import AwsRig
from e2e.admin.harness.gate import Aws, Gate
from e2e.admin.harness.release import Releases
from e2e.admin.harness.setup import Rigged, Setup
from e2e.admin.harness.zulip_rig import secret_values
from e2e.harness import report
from e2e.harness.config import CHECKOUT
from e2e.harness.secret_files import described
from e2e.run import _ALL_SIGNALS, EXIT, GRACE_S, Interrupted, command

HERE = Path(__file__).resolve().parent
TITLE = "Admin end-to-end"


def _interrupt(signum: int, frame: Any) -> None:
    raise Interrupted(signal.Signals(signum).name)


@dataclass
class Parts:
    """What a run drives; the offline tests pass fakes."""

    gate: Gate
    aws: AwsRig
    setup: Setup
    pytest: Callable[..., Any] = command


def build(rig: rigs.AdminRig, gate: Gate, out: Path) -> Parts:
    import boto3  # lazy: `uv run --with boto3`

    from e2e.harness.zulip_user import zulip_client

    aws = AwsRig(Aws(gate, boto3.client("ec2", region_name=rig.region), boto3.client("iam")), rig)

    def clients() -> tuple[Any, Any]:  # a malformed zuliprc named, never quoted (#519)
        return zulip_client(rig.tester_zuliprc), zulip_client(rig.tester2_zuliprc)

    setup = Setup(rig, gate, aws, Releases(rig), zulip_clients=clients, out=out)
    return Parts(gate, aws, setup)


def teardown(aws: AwsRig, *, keep_server: bool) -> list[str]:
    """Every instance of the test workshops terminated (participant or not), their Elastic IPs
    released and their volumes gone, A13's probe interface deleted, the server stopped, then
    all of it checked: what is left is a run problem (#356: "even when a scenario fails")."""
    problems: list[str] = []
    try:
        aws.terminate_participants()
    except Exception as exc:
        problems.append(f"teardown: the participants: {type(exc).__name__}: {exc}")
    try:
        aws.drop_probe_interfaces()
    except Exception as exc:
        problems.append(f"teardown: A13's probe interface: {type(exc).__name__}: {exc}")
    try:
        aws.drop_throwaway_key_pairs()
    except Exception as exc:
        problems.append(f"teardown: A16's key pairs: {type(exc).__name__}: {exc}")
    server = None
    try:
        server = aws.server()
        if server is not None and not keep_server:
            aws.stop(server)
    except Exception as exc:
        problems.append(f"teardown: the server was not stopped: {type(exc).__name__}: {exc}")
    if aws.dry:
        return problems

    def gone(found: list[Any]) -> bool:
        return not found

    def stopped(found: Any) -> bool:
        return found is None or keep_server or found["State"]["Name"] == "stopped"

    # each read until EC2 shows the teardown's changes (`settle`), bounded: what is left then
    try:
        fleet = aws.until(aws.fleet, gone, what="the test workshops' instances", strict=False)
        left = [i["InstanceId"] for i in fleet]  # a participant tag or not
        if left:
            problems.append(f"teardown check: instances not terminated: {', '.join(left)}")
        addresses = aws.until(aws.addresses, gone, what="their Elastic IPs", strict=False)
        kept = [a.get("AllocationId", "?") for a in addresses]
        if kept:
            problems.append(f"teardown check: Elastic IPs not released: {', '.join(kept)}")
        volumes = [v.get("VolumeId", "?") for v in aws.volumes_settled(gone=True)]
        if volumes:
            problems.append(f"teardown check: volumes not deleted: {', '.join(volumes)}")
        enis = aws.until(aws.probe_interfaces, gone, what="A13's probe interface", strict=False)
        if enis:
            left = ", ".join(i.get("NetworkInterfaceId", "?") for i in enis)
            problems.append(f"teardown check: A13's probe interface not deleted: {left}")
        server = aws.until(aws.server, stopped, what="the server stopped", strict=False)
        state = server["State"]["Name"] if server else "absent"
        if server is not None and not keep_server and state != "stopped":
            problems.append(f"teardown check: the server is {state}, not stopped")
    except Exception as exc:
        problems.append(f"teardown check failed: {type(exc).__name__}: {exc}")
    return problems


def scenarios(parts: Parts, spec: str, state: Path, out: Path) -> report.Report:
    """The selected scenarios' rows, from pytest's run of this checkout's admin suite."""
    json_path = out / "pytest.json"
    env = {**os.environ, "ZAB_LIVE": "admin-e2e", "ZAB_ADMIN_E2E_SCENARIOS": spec}
    env |= {"ZAB_ADMIN_E2E_REPORT_JSON": str(json_path), "ZAB_ADMIN_E2E_STATE": str(state)}
    argv = [sys.executable, "-m", "pytest", str(HERE), "-m", "admin_e2e", "-p", "no:cacheprovider"]
    done = parts.pytest([*argv, "-q", "-rs"], cwd=CHECKOUT, env=env, timeout=5 * 3600,
                        grace=GRACE_S, log=out / "pytest.log")  # fmt: skip
    ran = report.Report.read(json_path) if json_path.exists() else report.Report()
    seen = {r.number for r in ran.results}
    for n in rigs.select(spec):
        if n not in seen:
            label, title, _ = rigs.SCENARIOS[n]
            ran.results.append(report.ScenarioResult(n, title, "failed", reason="no result",
                                                     label=label))  # fmt: skip
    ran.results.sort(key=lambda r: r.number)
    if done.returncode != 0:
        ran.problems.append(f"pytest exited {done.returncode}")
    return ran


#: A10b's number among ADMIN §7's scenarios (`rigs.SCENARIOS`).
A10B = 110


def swept_values(rig, a10b: bool = True) -> tuple[dict[str, str], list[str]]:
    """Label → value of every secret pytest's output must not hold, and the run problems found
    on the way. Always the rig's own (A12's list), whatever A10b's token file is, so that no
    trouble with it turns off the rig's sweep (#902's review, B2). Then A10b's token for the
    test release repository, labelled by its variable (#902's re-review, B1); or, when
    `release_token` refuses it, every long word of the file (`raw_words`) and the variable's
    own value, a token put there by mistake say (#902's Codex round 2; review, B1): a
    traceback of that refusal could have printed them. That refusal is a run problem only for
    a run that selects A10b (`a10b`), naming the file or saying it is no path, never quoting
    the token. The file is read again after pytest ended: changed in between, it is swept for
    its new content."""
    values, problems = dict(secret_values(rig)), []
    label = f"${upgrade.TOKEN_ENV}"
    try:
        token = upgrade.release_token()
    except Exception as exc:
        if a10b:
            problems.append(f"A10b's token file was refused: {described(exc)}")
        for n, word in enumerate(upgrade.raw_words(), 1):
            values[f"{label} (word {n})"] = word.value
        given = os.environ.get(upgrade.TOKEN_ENV, "").strip()
        if len(given) >= 20:
            values[f"{label} (its value)"] = given
    else:
        if token:
            values[label] = token.value
    return values, problems


def sweep(log: Path, values: Mapping[str, str]) -> list[str]:
    """Each of the rig's secrets (A12's list) in pytest's output replaced by its label, as a
    run problem: a traceback prints its frames' arguments, a zuliprc's text among them."""
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    found = [label for label, value in values.items() if value and value in text]
    for label in found:
        text = text.replace(values[label], f"<{label}>")
    if not found:
        return []
    log.write_text(text, encoding="utf-8")  # the file stays 0600 (`command`)
    return [f"pytest.log held rig secrets, now redacted: {', '.join(found)}"]


def main(argv: list[str] | None = None, *, make: Callable[..., Parts] = build) -> int:
    parser = argparse.ArgumentParser(
        prog="tests/e2e/admin/run.py", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--commit", help="the commit under test (default HEAD)")
    parser.add_argument("--scenarios", default="go", help="go, all, or A1,A10,A12-A15")
    parser.add_argument("--require", default="", help="scenarios that must pass (go, all, …)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print every change; make none")
    mode.add_argument("--setup-only", action="store_true", help="set up and deploy, then stop")
    mode.add_argument("--teardown", action="store_true", help="only terminate and stop")
    parser.add_argument("--keep-server", action="store_true", help="leave the server running")
    args = parser.parse_args(argv)
    if os.environ.get("ZAB_LIVE") != "admin-e2e":
        print("admin-e2e: set ZAB_LIVE=admin-e2e (it acts on AWS and Zulip)", file=sys.stderr)
        return 2
    try:
        rig = rigs.load()
        rigs.select(args.scenarios)
        required = rigs.select(args.require) if args.require.strip() else []
    except ValueError as exc:
        print(f"admin-e2e: {exc}", file=sys.stderr)
        return 2
    if rig.missing():
        print("admin-e2e: missing inputs: " + "; ".join(rig.missing()), file=sys.stderr)
        return 2
    started = dt.datetime.now(dt.UTC)
    rig.report_dir.mkdir(parents=True, exist_ok=True)
    lock = open(rig.report_dir / "run.lock", "w")  # held for the whole run
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"admin-e2e: another run holds {lock.name}", file=sys.stderr)
        return 2
    out = rig.report_dir / f"{started:%Y%m%dT%H%M%SZ}-{'dry' if args.dry_run else 'run'}"
    out.mkdir(parents=True, exist_ok=True)
    gate = Gate(dry_run=args.dry_run)
    parts = make(rig, gate, out)
    result = report.Report(started=f"{started:%Y-%m-%d %H:%M:%S}Z", required=required, title=TITLE)
    previous = {s: signal.signal(s, _interrupt) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        _run(parts, args, result, out)
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        lock.close()
    try:  # written after A12 scanned the run's files: swept here (reviewer on #518)
        values, problems = swept_values(rig, A10B in rigs.select(args.scenarios))
        result.problems += problems + sweep(out / "pytest.log", values)
    except Exception as exc:  # a malformed secrets file named, never quoted (#519)
        result.problems.append(f"pytest.log not swept for secrets: {described(exc)}")
    if args.dry_run:
        print(f"\nadmin-e2e: dry run: {len(gate.writes)} changes planned; none made ({out}).")
        for problem in result.problems:
            print(f"admin-e2e: {problem}")
        return 1 if result.problems else 0
    final = out.with_name(f"{started:%Y%m%dT%H%M%SZ}-{(result.commit or 'none')[:8]}")
    with contextlib.suppress(OSError):
        out = out.rename(final)
    result.write(out / "report.json")
    summary = result.markdown()
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    print(f"report: {out / 'report.json'}\nsummary: {out / 'summary.md'}")
    if args.setup_only or args.teardown:
        return 1 if result.problems else 0
    return EXIT[result.verdict]


def _run(parts: Parts, args: argparse.Namespace, result: report.Report, out: Path) -> None:
    """Set up, run, and always tear down and check, whatever ended the run."""
    try:
        if not args.teardown:
            sha = parts.setup.releases.commit_of(args.commit or "HEAD")
            result.commit = sha
            rigged: Rigged = parts.setup.run(sha)
            state = out / "state.json"
            rigged.write(state)
            result.wheel = rigged.wheel
            result.vm = {"server": rigged.server, "ip": rigged.ip, "release": rigged.release}
            result.deploy = "; ".join(rigged.deploys)
            if not (args.dry_run or args.setup_only):
                ran = scenarios(parts, args.scenarios, state, out)
                result.results, result.problems = ran.results, result.problems + ran.problems
    except (Exception, KeyboardInterrupt) as exc:
        result.problems.append(f"aborted: {described(exc)}")  # never a secret's line
    finally:
        held = {s: signal.signal(s, signal.SIG_IGN) for s in _ALL_SIGNALS}
        try:
            parts.gate.note("teardown")
            result.problems += teardown(parts.aws, keep_server=args.keep_server)
        finally:
            for s, handler in held.items():
                signal.signal(s, handler)
        timeline = getattr(parts.setup, "timeline", None)  # the server's start-up (#804)
        result.boot = list(timeline.lines) if timeline is not None else []
        result.finished = f"{dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M:%S}Z"


if __name__ == "__main__":
    raise SystemExit(main())
