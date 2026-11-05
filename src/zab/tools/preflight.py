"""`zab preflight [--json]`: workshop readiness checks (SPEC §13.1; workshop/admin/README.md §3).

The checks make real calls on purpose: `codex login status` and friends can report "logged in"
with a stale token (§13.1), so each agent answers a one-line smoke prompt on its cheapest
model, and the prompt makes it run one shell command: an agent that can answer but not run
tools (AMI v1's Codex without `codex-code-mode-host`) fails here, at homework time. Each check
is a function taking an injected `Runner` and returns a `Check` (core/types.py); a failing or
crashing check never stops the others. The report is written to
`~/.local/state/zab/preflight.json`, which `workshop/admin/status.py` gathers
(workshop/admin/README.md §3).

Nothing printed can carry a token: details are the first line of an output, redacted with the
§11.6 patterns.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from zab import __version__
from zab.core.types import Check
from zab.tools.common import Runner, first_line, short_host, subprocess_runner
from zab.tools.setup_repo import record_path, verify_write_access

#: The agent must run `cat` on a file holding a fresh nonce and reply with its output: a reply it
#: could make up proves nothing, and a broken tool runner answers but cannot run the command.
SMOKE_PROMPT = (
    "Use your shell tool to run exactly this command: cat {path}\nThen reply with only its output."
)
SMOKE_TIMEOUT_S = 120.0
MIN_FREE_GB = 20
PARTICIPANT_FILE = "/etc/zab-participant.json"

#: Removed from the agents' environment for the smoke prompts (SPEC §9.1).
SCRUBBED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

#: cypari2 exposes no `__version__` (checked on 2.2.4): the import is the test, the version is
#: a courtesy from the package metadata, and the verdict is the exit code alone.
CYPARI2_PROBE = (
    "import cypari2\n"
    "try:\n"
    "    import importlib.metadata as m\n"
    "    print(m.version('cypari2'))\n"
    "except Exception:\n"
    "    print('installed')\n"
)


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


@contextmanager
def _nonce_file() -> Iterator[tuple[str, str]]:
    """A fresh nonce in a file under /tmp (no spaces in the path, so it fits one shell word)."""
    nonce = "zab-" + secrets.token_hex(8)
    fd, path = tempfile.mkstemp(prefix="zab-preflight-", suffix=".txt", dir="/tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(nonce + "\n")
        yield path, nonce
    finally:
        os.unlink(path)


def _smoke_verdict(name: str, result: Any, nonce: str) -> Check:
    if result.exit_code != 0:
        why = first_line(result.stderr) or first_line(result.stdout) or f"exit {result.exit_code}"
        return Check(name=name, ok=False, detail=why)
    if nonce in result.stdout:
        return Check(name=name, ok=True, detail="answered and ran a shell command")
    return Check(
        name=name,
        ok=False,
        detail="answered, but did not run the shell command: " + first_line(result.stdout),
    )


def check_codex_login(run: Runner) -> Check:
    """`codex login status` (§13.1)."""
    result = run(["codex", "login", "status"], timeout_s=30, env=_env())
    detail = first_line(result.stdout) or first_line(result.stderr) or f"exit {result.exit_code}"
    return Check(name="codex login", ok=result.ok, detail=detail)


def check_codex_smoke(run: Runner, cwd: str) -> Check:
    """A one-line `codex exec` prompt (§13.1). VERIFY the flags on the pinned Codex version."""
    with _nonce_file() as (path, nonce):
        result = run(
            ["codex", "exec", "--skip-git-repo-check", SMOKE_PROMPT.format(path=path)],
            cwd=cwd,
            timeout_s=SMOKE_TIMEOUT_S,
            env=_env(),
        )
    return _smoke_verdict("codex smoke", result, nonce)


def check_claude_smoke(run: Runner, cwd: str) -> Check:
    """A one-line `claude -p` prompt on the cheapest model, API-key variables removed (§9.1).

    `--allowedTools=` takes the one command in its `=` form: the space-separated form is
    variadic and would swallow the prompt (Claude Code 2.1.288)."""
    with _nonce_file() as (path, nonce):
        result = run(
            [
                "claude",
                "-p",
                "--model",
                "haiku",
                f"--allowedTools=Bash(cat {path})",
                SMOKE_PROMPT.format(path=path),
            ],
            cwd=cwd,
            timeout_s=SMOKE_TIMEOUT_S,
            env=_env(),
        )
    return _smoke_verdict("claude smoke", result, nonce)


def check_git_push(run: Runner, repo_dir: str | None, host: str) -> Check:
    """The write-access test of `zab setup-repo` step 6 (§13.1)."""
    if not repo_dir:
        return Check(
            name="git push", ok=False, detail="no repository: run zab setup-repo OWNER/REPO"
        )
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return Check(name="git push", ok=False, detail=f"{repo_dir} is not a git checkout")
    try:
        ok, detail = verify_write_access(run, repo_dir, host)
    except Exception as exc:
        return Check(name="git push", ok=False, detail=f"{type(exc).__name__}: {exc}")
    return Check(name="git push", ok=ok, detail=detail)


def check_math_tools(run: Runner, home: str) -> list[Check]:
    """`gp` and `cypari2` required, `sage` informational (§13.1; workshop/ami/README.md)."""
    checks: list[Check] = []
    gp = run(["gp", "--version-short"], timeout_s=30)
    checks.append(
        Check(
            name="gp",
            ok=gp.ok,
            detail=first_line(gp.stdout) or first_line(gp.stderr) or f"exit {gp.exit_code}",
        )
    )
    venv_python = os.path.join(home, "venv", "bin", "python")
    python = venv_python if os.path.exists(venv_python) else "python3"
    cy = run([python, "-c", CYPARI2_PROBE], timeout_s=60)
    checks.append(
        Check(
            name="cypari2",
            ok=cy.ok,
            detail=(first_line(cy.stdout) if cy.ok else first_line(cy.stderr)) + f" ({python})",
        )
    )
    sage = run(["sage", "--version"], timeout_s=60)
    checks.append(
        Check(
            name="sage",
            ok=True,
            warning=not sage.ok,
            detail=first_line(sage.stdout) if sage.ok else "not installed (optional)",
        )
    )
    return checks


def check_disk_space(home: str, disk_usage: Callable[[str], Any] | None = None) -> Check:
    """At least 20 GB free on the home file system (§13.1)."""
    try:
        free_gb = (disk_usage or shutil.disk_usage)(home).free / 1e9
    except OSError as exc:
        return Check(name="disk space", ok=False, detail=str(exc))
    return Check(name="disk space", ok=free_gb >= MIN_FREE_GB, detail=f"{free_gb:.1f} GB free")


def participant_repo_dir(home: str, participant_file: str = PARTICIPANT_FILE) -> str | None:
    """`~/work/<repo>` from `/etc/zab-participant.json` (workshop/ami/README.md,
    "First boot"), if present."""
    try:
        with open(participant_file, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    repo = str(data.get("repo") or "").strip().rstrip("/")
    if not repo:
        return None
    name = repo.split("/")[-1]
    name = name[:-4] if name.endswith(".git") else name
    return os.path.join(home, "work", name)


def _is_checkout(path: str | None) -> bool:
    return bool(path) and os.path.isdir(os.path.join(str(path), ".git"))


def recorded_repo_dir(home: str) -> str | None:
    """The checkout `zab setup-repo` configured last, if it still exists."""
    try:
        with open(record_path(home), encoding="utf-8") as f:
            path = f.read().strip()
    except OSError:
        return None
    return path if _is_checkout(path) else None


def sole_work_checkout(home: str) -> str | None:
    """`~/work/<name>` when it is the only git checkout in `~/work`."""
    work = os.path.join(home, "work")
    try:
        names = sorted(os.listdir(work))
    except OSError:
        return None
    found = [os.path.join(work, n) for n in names if _is_checkout(os.path.join(work, n))]
    return found[0] if len(found) == 1 else None


def find_repo_dir(home: str, participant_file: str = PARTICIPANT_FILE) -> str | None:
    """The checkout for the push test, when `--repo` is not given.

    In order: the one `zab setup-repo` recorded; the roster's repository (its first boot's record)
    if it is checked out; the only checkout in `~/work`. Participants create their repository during
    the homework, so its name need not match the roster. Falls back to the roster's path (or None),
    so that the push check names what it looked for.
    """
    recorded = recorded_repo_dir(home)
    if recorded:
        return recorded
    from_roster = participant_repo_dir(home, participant_file)
    if _is_checkout(from_roster):
        return from_roster
    return sole_work_checkout(home) or from_roster


def _guard(fn: Callable[[], Check | list[Check]], name: str) -> list[Check]:
    try:
        result = fn()
    except Exception as exc:
        return [Check(name=name, ok=False, detail=f"{type(exc).__name__}: {exc}")]
    return result if isinstance(result, list) else [result]


def run_all(
    run: Runner, *, home: str, repo_dir: str | None, host: str, cwd: str | None = None
) -> list[Check]:
    """All §13.1 checks, in order, each isolated from the others."""
    cwd = cwd or home
    checks: list[Check] = []
    checks += _guard(lambda: check_codex_login(run), "codex login")
    checks += _guard(lambda: check_codex_smoke(run, cwd), "codex smoke")
    checks += _guard(lambda: check_claude_smoke(run, cwd), "claude smoke")
    checks += _guard(lambda: check_git_push(run, repo_dir, host), "git push")
    checks += _guard(lambda: check_math_tools(run, home), "math tools")
    checks += _guard(lambda: check_disk_space(home), "disk space")
    return checks


def all_ok(checks: list[Check]) -> bool:
    return all(c.ok for c in checks)


def report(checks: list[Check], *, host: str) -> dict[str, Any]:
    return {
        "host": host,
        "time": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "version": __version__,
        "ok": all_ok(checks),
        "checks": [dataclasses.asdict(c) for c in checks],
    }


def write_report(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def format_table(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks) if checks else 0
    lines = []
    for c in checks:
        mark = "OK " if c.ok and not c.warning else ("WARN" if c.ok else "FAIL")
        lines.append(f"{mark:<5}{c.name:<{width}}  {c.detail}")
    lines.append(
        "all good" if all_ok(checks) else "some checks failed; post this in your workshop's channel"
    )
    return "\n".join(lines)


def main(
    argv: list[str] | None = None,
    *,
    run: Runner = subprocess_runner,
    home: str | None = None,
    participant_file: str = PARTICIPANT_FILE,
) -> int:
    parser = argparse.ArgumentParser(
        prog="zab preflight", description="Workshop readiness checks (SPEC §13.1)."
    )
    parser.add_argument("--json", action="store_true", help="print the JSON report")
    parser.add_argument("--repo", help="checkout for the push test (default: from the VM)")
    parser.add_argument(
        "--state-dir", help="where preflight.json goes (default ~/.local/state/zab)"
    )
    args = parser.parse_args(argv)
    home = home or os.path.expanduser("~")
    host = short_host(socket.gethostname)
    repo_dir = os.path.expanduser(args.repo) if args.repo else find_repo_dir(home, participant_file)
    checks = run_all(run, home=home, repo_dir=repo_dir, host=host)
    data = report(checks, host=host)
    state_dir = args.state_dir or os.path.join(home, ".local", "state", "zab")
    path = os.path.join(state_dir, "preflight.json")
    try:
        write_report(path, data)
    except OSError as exc:
        print(f"could not write {path}: {exc}", file=sys.stderr)
    if args.json:
        print(json.dumps(data, indent=1))
    else:
        print(format_table(checks))
        print(f"report: {path}", file=sys.stderr)
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
