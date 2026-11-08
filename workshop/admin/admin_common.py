"""Shared pieces of the admin scripts (README.md).

The admin scripts are standalone, not part of the `zab` package: Drew runs them from a checkout
with `uv run python workshop/admin/<script>.py …`. The roster helpers come from `workshop/aws/`
(unchanged). Everything that reaches a VM goes through a `Runner` (an argument-list subprocess
seam, never a shell string built from roster text; CONTRIBUTING.md, rule 3), and everything that
reaches Zulip goes through `zulip_provision.ZulipAdmin`; tests replace both with fakes.
No API key is ever printed or logged (CONTRIBUTING.md, rule 1).
"""

from __future__ import annotations

import base64
import configparser
import datetime
import hashlib
import ipaddress
import os
import pathlib
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol, TypeVar

AWS_DIR = pathlib.Path(__file__).resolve().parents[1] / "aws"
if str(AWS_DIR) not in sys.path:
    sys.path.insert(0, str(AWS_DIR))

from aws_common import (  # noqa: E402  - path set up above
    ROSTER_COLUMNS,
    column_snapshot,
    git_identity,
    merge_roster_changes,
    owner_record,
    participants,
    read_roster,
    repo_name,
    roster_changes,
    save_roster_changes,
    slug_of,
    validate_row,
    work_repo,
    write_roster,
)

__all__ = [
    "ALLOWLIST_PENDING",
    "DEFAULT_KNOWN_HOSTS",
    "KEEP_UNTIL_HOUR",
    "REFSERVE_URL",
    "ROSTER_COLUMNS",
    "RunResult",
    "Runner",
    "allowlist_pending",
    "check_duplicates",
    "column_snapshot",
    "first_line",
    "format_table",
    "git_identity",
    "host_key_fingerprint",
    "host_of",
    "keep_until_value",
    "keys_dir",
    "load_roster",
    "load_zuliprc",
    "merge_roster_changes",
    "move_legacy_keys",
    "owner_record",
    "pending_label",
    "read_roster",
    "redact",
    "remote_chain",
    "repo_name",
    "roster_changes",
    "run_parallel",
    "save_roster_changes",
    "scp_argv",
    "select_rows",
    "sign_in_check",
    "sign_in_pending",
    "slug_of",
    "ssh_argv",
    "ssh_options",
    "subprocess_runner",
    "validate_row",
    "work_repo",
    "write_roster",
]

T = TypeVar("T")
R = TypeVar("R")


# ---------------------------------------------------------------------------------- subprocess


@dataclass(frozen=True, slots=True)
class RunResult:
    exit_code: int  # -1 when the command timed out or could not be started
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Runner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        timeout_s: float = 120.0,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> RunResult: ...


def subprocess_runner(
    argv: list[str],
    *,
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> RunResult:
    """Run an argument vector; a timeout or a missing program is reported, never raised."""
    try:
        proc = subprocess.run(  # noqa: S603 - argument list from our own code, no shell
            argv,
            env=dict(env) if env is not None else None,
            input=stdin,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return RunResult(-1, "", f"timed out after {timeout_s:g} s: {argv[0]}")
    except FileNotFoundError:
        return RunResult(-1, "", f"not found: {argv[0]}")
    except OSError as exc:
        return RunResult(-1, "", f"cannot run {argv[0]}: {exc}")
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


# ------------------------------------------------------------------------------------------ SSH

#: The VMs' host keys, recorded once by `scan_hosts.py` after the launch and pinned ever after
#: (README.md §0). Public data, kept next to the other per-workshop state in the git-ignored `out/`.
DEFAULT_KNOWN_HOSTS = pathlib.Path(__file__).resolve().parent / "out" / "known_hosts"
#: The central file server that serves the turn snapshots (SPEC §10.7; Drew, issue #36, Mon Oct 5):
#: `push_config.py` writes it as `[render] refserve_url`, and "Start here" names its host, where
#: the agents' file links go (`zulip_provision.py`, #709). No secret in it. `--refserve-url ""`
#: leaves it out (links off).
REFSERVE_URL = "https://files.mathagents.org"
#: The GitHub organization whose repositories that server serves, the workshop's `github_org`
#: (#693): `push_config.py --refserve-owner` writes it as `[render] refserve_owner`, and the
#: bridge publishes a turn's snapshot only for a checkout in it (SPEC §10.7). Like
#: `REFSERVE_URL`, a hand run's default, the MathAgents workshops'; the admin bot passes both.
REFSERVE_OWNER = "MathAgents"
#: A GitHub user or organization name, as GitHub allows one.
GITHUB_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", re.ASCII)


def ssh_options(
    known_hosts: str | os.PathLike[str] = DEFAULT_KNOWN_HOSTS, *, identity: str | None = None
) -> list[str]:
    """Non-interactive SSH with the host key pinned (README.md §0; PM review of PR #47).

    `StrictHostKeyChecking=yes` with only our own `known_hosts` file consulted: a VM whose key is
    not recorded, or has changed, is refused instead of receiving a participant's zuliprc files.
    No forwarding of any kind, so a compromised VM can reach neither the admin's keys nor the
    admin's display: `ForwardAgent=no` (`-a`), `ForwardX11=no` (`-x`), and `ClearAllForwardings`
    for the port forwardings an ssh config may add (`ssh -G` shows that it clears only those;
    codex round 4 on PR #47).
    """
    path = os.fspath(known_hosts)
    if '"' in path or "\n" in path or "\r" in path:
        raise ValueError("known_hosts path must not contain quotes or newlines")
    # `ZAB_SSH_IDENTITY=<private key>`: that key only, for a VM whose key pair is not the
    # admin's default identity (the test VM of issue #263; tests/e2e/run.py sets it). An
    # explicit `identity` wins (join_server.py's break-glass key, docs/ADMIN.md §8.4).
    identity = os.environ.get("ZAB_SSH_IDENTITY", "") if identity is None else identity
    if any(c in identity for c in '"\r\n'):
        raise ValueError("the SSH identity path must not contain quotes or newlines")
    key = ["-i", identity, "-o", "IdentitiesOnly=yes"] if identity else []
    return [
        *key,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f'UserKnownHostsFile="{path}"',
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ClearAllForwardings=yes",
    ]


def keys_dir(out_dir: pathlib.Path, row: Mapping[str, str]) -> tuple[pathlib.Path, str]:
    """Where `zulip_provision.py` keeps the row's bot zuliprcs, and a note when it is legacy.

    `out/<slug>/` normally. When that is missing, the legacy `out/<github>/` that the rc3 script
    wrote, named after the login with its capitals: paths are case-sensitive, so a login such as
    `AndrewVSutherland` is not found under its slug (Drew's rc4 push, issue #36).

    Only a name that differs from the slug **by case alone** is taken. Slugs are lower case
    (`validate_row`), so such a name is never another row's `out/<slug>/`, even with `--only`:
    a `github` that names another participant can never hand that participant's keys to this
    row (codex review on #114).
    """
    slug = slug_of(dict(row))
    path = out_dir / slug
    login = (row.get("github") or "").strip()
    if path.exists() or login == slug or login.lower() != slug:
        return path, ""
    legacy = out_dir / login
    if not legacy.is_dir():
        return path, ""
    return (
        legacy,
        f"keys from the legacy out/{login}/ (zulip_provision.py moves them to out/{slug}/)",
    )


def move_legacy_keys(out_dir: pathlib.Path, row: Mapping[str, str], *, dry_run: bool) -> str:
    """Rename the legacy `out/<github>/` to `out/<slug>/` (`keys_dir`); what was done, or ''."""
    path, note = keys_dir(out_dir, row)
    if not note:
        return ""
    target = out_dir / slug_of(dict(row))
    if dry_run:
        return f"would move out/{path.name}/ to out/{target.name}/"
    path.rename(target)
    return f"moved out/{path.name}/ to out/{target.name}/"


def host_of(row: Mapping[str, str]) -> str:
    """The VM's IP address, from the roster's `ip` column (filled in by launch.py; README.md §4).

    The `host` column is the alias `ws-<slug>`, which resolves only through the admin's own
    `~/.ssh/config`; a stale or mistyped alias would send a participant's files wherever that
    name happens to resolve, so it is never used for connections (PM review of PR #47).
    """
    ip = (row.get("ip") or "").strip()
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        raise ValueError(
            f"no IP address for {slug_of(dict(row))!r} in the roster "
            f"(launch.py fills in `ip`): {ip!r}"
        ) from None


def ssh_argv(
    host: str,
    remote: str,
    *,
    user: str = "ubuntu",
    known_hosts: str | os.PathLike[str] = DEFAULT_KNOWN_HOSTS,
) -> list[str]:
    """`ssh … user@host -- <remote>`; `remote` is one shell command the VM's shell parses.

    Build it with `remote_chain` (every value `shlex.quote`d) or use a fixed literal; never
    interpolate roster text into it unquoted.
    """
    return ["ssh", *ssh_options(known_hosts), f"{user}@{host}", "--", remote]


def scp_argv(
    local: str,
    host: str,
    remote_path: str,
    *,
    user: str = "ubuntu",
    known_hosts: str | os.PathLike[str] = DEFAULT_KNOWN_HOSTS,
) -> list[str]:
    return ["scp", *ssh_options(known_hosts), "--", local, f"{user}@{host}:{remote_path}"]


def host_key_fingerprint(key_line: str) -> str:
    """`SHA256:…` of a `known_hosts` or `ssh-keyscan` line, as `ssh-keygen -l` prints it."""
    parts = key_line.split()
    if len(parts) < 3 or not parts[1].startswith(("ssh-", "ecdsa-")):
        raise ValueError("expected `<host> <key type> <base64 key>`")
    try:
        blob = base64.b64decode(parts[2], validate=True)
    except ValueError as exc:
        raise ValueError("bad base64 in host key") from exc
    digest = base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=").decode("ascii")
    return f"SHA256:{digest}"


#: The doctor checks that only say an agent is not signed in yet (PM on issue #36, Mon 07:35):
#: Codex's `codex:login` (backends/codex.py) and Claude's `claude account <name>`, one per
#: account (backends/claude.py). Matched by name, never by message text. Until the gifts
#: arrive on Friday nobody can sign in, so a VM whose only failing checks are these is
#: `ok (sign-in pending)`, not a failure (push_config.py, status.py).
#: `claude account <name>` is the sign-in answer alone: the Claude doctor names a missing
#: config directory `claude config <name>` and a failed `auth status` `claude auth <name>`, and
#: those stay failures (codex on #152).
SIGN_IN_CHECKS = frozenset({"codex:login"})
SIGN_IN_PREFIXES = ("claude account ",)


def sign_in_check(name: str) -> bool:
    """Whether a doctor check is an agent's sign-in check (`SIGN_IN_CHECKS`)."""
    return name in SIGN_IN_CHECKS or name.startswith(SIGN_IN_PREFIXES)


def sign_in_pending(failing: list[Mapping[str, object]]) -> bool:
    """Whether failing doctor checks are all sign-in checks, and there is at least one."""
    return bool(failing) and all(sign_in_check(str(c.get("name", ""))) for c in failing)


#: `zab doctor`'s `<bot>:allowlist` check fails while the bot has the roster e-mail to allow, no
#: user ID, and no Zulip user it can see has that e-mail: the profile allows the participant's
#: Zulip ID only once `zulip_provision.py` has bound their row, after they joined
#: (push_config.render_config_toml), and the bots (members) can read an address only when it is
#: visible to members. On an **unbound** row (no `zulip_user_id`) that is pending, not a
#: failure, like a sign-in: the participant hasn't joined, or the next provisioning (a
#: Moderator) records their ID and the rollout after it writes it (#401). From #401 on the
#: doctor resolves the e-mails as the bridge does (SPEC §5.2) and its detail starts with this (a
#: copy of `zab.commands.doctor.ALLOWLIST_PENDING`: these scripts import nothing from zab); a
#: doctor before #401 (0.2.3) leaves it empty, so the row decides. Any other detail ("nobody
#: allowed: …"), and the check on a bound row, stay failures: a bound row's rollout writes the
#: ID, and a VM rolled out before its row was bound, whose bots can't see the address, needs one.
ALLOWLIST_PENDING = "no allowed user yet"


def allowlist_pending(check: Mapping[str, object], row: Mapping[str, str]) -> bool:
    """Whether a failing doctor check is an unbound row's allowlist, waiting for its
    participant to join Zulip (`ALLOWLIST_PENDING`)."""
    name, detail = str(check.get("name", "")), str(check.get("detail") or "")
    if check.get("ok") or not name.endswith(":allowlist"):
        return False
    if (row.get("zulip_user_id") or "").strip() or not (row.get("email") or "").strip():
        return False  # bound, or no e-mail to wait for: a real failure
    return not detail or detail.startswith(ALLOWLIST_PENDING)


def pending_label(*, owner: bool, sign_in: bool, project: bool = False) -> str:
    """`owner+sign-in pending`, `owner pending`, `sign-in pending`, or empty; short enough
    that `active, ` and it fit the admin plan's status cell (32 characters, `planning._cell`).
    `project` comes first (`project+owner+sign-in pending`): only push_config.py's own table
    shows it, for a hand-over's checkout that provision_repos.py makes next (#458)."""
    which = [w for w, on in (("project", project), ("owner", owner), ("sign-in", sign_in)) if on]
    return f"{'+'.join(which)} pending" if which else ""


def remote_chain(*argvs: Iterable[str]) -> str:
    """Join argument vectors into one `a && b && c` remote command, each value quoted."""
    return " && ".join(shlex.join(list(a)) for a in argvs)


#: The idle-stop hold (`idle_stop.py`'s `HOLD`): an expiry an hour ahead, by the VM's clock, so
#: that no tick of idle stop powers the VM off while an admin script works on it; removed at
#: the end. `push_config.py` holds it around a rollout (PM on #218), `final_sync.py` around a
#: sync (#370). One file, not counted: whichever run ends first removes it. A run killed before
#: its end leaves it, and idle stop then ignores it from its expiry; /run is cleared at boot.
#: Fixed literals; harmless on a VM without idle stop.
IDLE_HOLD_ON_CMD = "echo $(( $(date +%s) + 3600 )) | sudo tee /run/zab-idle-stop.hold >/dev/null"
IDLE_HOLD_OFF_CMD = "sudo rm -f /run/zab-idle-stop.hold"


# -------------------------------------------------------------------------------------- hygiene

_SECRET_RES = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{50,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    # The admin bots' JWT and AWS secret rules (`zab.render.redact`, `zab.admin.scrub`), copied:
    # VM text reaches status.py's cells (`first_line`), and these scripts import nothing from
    # `zab` (the review of #380). Before the `key=` rule, which would take 32 characters of an
    # `aws_secret_access_key=` and leave the rest.
    re.compile(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![A-Za-z0-9_-])"
    ),
    re.compile(
        r"((?i:aws_secret_access_key|secretaccesskey|secret_key|sessiontoken)"
        r"[\"']?[ \t]*[:=][ \t]*[\"']?)[A-Za-z0-9/+=]{16,}"
    ),
    re.compile(r"(?<![A-Za-z0-9])((?i:token)[\"']?[ \t]*[:=][ \t]*[\"']?)[A-Za-z0-9/+=]{16,}"),
    re.compile(r"(?i)(key\s*=\s*)[A-Za-z0-9]{32}"),
)


#: docs/ADMIN.md §2.7 item 0's characters, as Unicode 15.0 has them: Cc but newline and tab;
#: Cf (bidi controls, zero-width characters, BOM, soft hyphen); Zl and Zp; surrogates; the
#: variation selectors. The admin bots' table (`zab.admin.scrub._INVISIBLE_RANGES`), copied,
#: since these scripts import nothing from `zab`; a test keeps the two equal.
_INVISIBLE_RANGES = (
    (0x0000, 0x0008), (0x000B, 0x001F), (0x007F, 0x009F), (0x00AD, 0x00AD), (0x034F, 0x034F),
    (0x0600, 0x0605), (0x061C, 0x061C), (0x06DD, 0x06DD), (0x070F, 0x070F), (0x0890, 0x0891),
    (0x08E2, 0x08E2), (0x180B, 0x180F), (0x200B, 0x200F), (0x2028, 0x202E), (0x2060, 0x2064),
    (0x2066, 0x206F), (0xD800, 0xDFFF), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD), (0x110CD, 0x110CD), (0x13430, 0x1343F), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0001, 0xE0001), (0xE0020, 0xE007F), (0xE0100, 0xE01EF),
)  # fmt: skip
_INVISIBLE = dict.fromkeys(c for a, b in _INVISIBLE_RANGES for c in range(a, b + 1) if c < 128)
_INVISIBLE_RE = re.compile(
    "[" + "".join(f"{re.escape(chr(a))}-{re.escape(chr(b))}" for a, b in _INVISIBLE_RANGES) + "]+"
)


def strip_invisible(text: str) -> str:
    """§2.7 item 0's strip: each of its characters deleted (`_INVISIBLE_RANGES`). A token one
    of them splits is whole again, so that a pattern can match it before a cut leaves only its
    head. In C either way: `str.translate` for ASCII (2 ms for 4M characters), a class
    otherwise (65 ms, where a loop over characters took 0.43 s)."""
    return text.translate(_INVISIBLE) if text.isascii() else _INVISIBLE_RE.sub("", text)


def redact(text: str) -> str:
    """The SPEC §11.6 patterns, so no VM output or error can echo a token; on the text with
    its invisible characters stripped (`strip_invisible`), as the admin bots' scrubber does."""
    text = strip_invisible(text)
    for pattern in _SECRET_RES:
        text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[redacted]", text)
    return text


def first_line(text: str, limit: int = 160) -> str:
    """The first non-empty line, for a table cell: stripped of invisible characters and
    redacted (`redact`), and only then shortened, so no cut splits a token they would join."""
    for line in text.splitlines():
        line = redact(line).strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


# ---------------------------------------------------------------------------------- roster etc.


def select_rows(rows: list[dict[str, str]], only: list[str] | None) -> list[dict[str, str]]:
    """The participants (rows with a slug, README.md §4); `--only SLUG` (repeatable) narrows them,
    and an unknown slug is an error."""
    rows = participants(rows)
    if not only:
        return rows
    wanted = {o.lower() for o in only}
    known = {slug_of(r) for r in rows}
    missing = sorted(wanted - known)
    if missing:
        raise ValueError(f"not in the roster: {', '.join(missing)}")
    return [r for r in rows if slug_of(r) in wanted]


#: A kept VM's end in the roster's `keep_until` (docs/ADMIN.md §11.5): a date, at `KEEP_UNTIL_HOUR`
#: UTC that day, or a date and a time in UTC; `t` is read as `T`. The admin bot's grammar reads
#: `!keep-until`'s the same way (`zab.admin.grammar.keep_until_value`, `END_HOUR`): a test keeps
#: the two equal.
KEEP_UNTIL_HOUR = 15
_KEEP_UNTIL_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[Tt]([0-9]{2}):([0-9]{2}))?", re.ASCII
)


def keep_until_value(text: str) -> str | None:
    """`text` as the roster's `keep_until` holds it, `2026-11-30` or `2026-11-30T15:00`, when it
    names a real date and time; None otherwise."""
    match = _KEEP_UNTIL_RE.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute = match.groups()
    try:
        datetime.datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0))
    except ValueError:
        return None
    return f"{year}-{month}-{day}" + (f"T{hour}:{minute}" if hour else "")


def read_keep(path: str) -> list[str]:
    """A kept-slugs file (`keep.txt`, docs/ADMIN.md §8.4): one slug per line, `#` comments,
    at least one, none twice."""
    with open(path, encoding="utf-8") as f:
        slugs = [line.split("#", 1)[0].strip().lower() for line in f]
    slugs = [slug for slug in slugs if slug]
    if not slugs or len(set(slugs)) != len(slugs):
        raise ValueError(f"{path}: expected distinct slugs, one per line")
    return slugs


def _id_key(value: str) -> str:
    """A Zulip id as a number when it is ASCII digits, so that `0101` and `101` collide."""
    return str(int(value)) if value.isascii() and value.isdigit() and len(value) <= 18 else value


#: Columns that identify one participant, one VM or one Zulip user: a value shared by two rows
#: would put one person's keys and allowlist on another's VM (PM review of PR #47).
_UNIQUE_COLUMNS: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("slug", str.casefold),
    ("github", str.casefold),
    ("email", str.casefold),
    ("host", str.casefold),
    ("ip", str),
    ("zulip_user_id", _id_key),
    ("zulip_channel_id", str),
)


#: What two rows with one `ip` usually are, and the fix (#604): EC2 gave an Elastic IP released
#: with one row's VM (a teardown before `teardown.py --roster` cleared it) to another row's.
#: `launch.py --only <that row>` clears the other's, and so does the bot's `!launch <that row>`.
STALE_IP = (
    ". One of them kept an address released with its VM, which EC2 has given to the other's: "
    "launch.py --only <the row whose VM has it> (the bot's !launch) clears the stale one"
)


def check_duplicates(rows: list[dict[str, str]]) -> None:
    """Refuse a roster in which two rows share a slug, login, e-mail, host, IP, Zulip id or
    sandbox channel id."""
    for column, normalize in _UNIQUE_COLUMNS:
        seen: dict[str, str] = {}
        for row in rows:
            value = (row.get(column) or "").strip()
            if not value:
                continue
            key = normalize(value)
            if key in seen:
                raise ValueError(
                    f"duplicate {column} {value!r}: rows {seen[key]} and {slug_of(row) or '?'}"
                    + (STALE_IP if column == "ip" else "")
                )
            seen[key] = slug_of(row) or "?"


def load_roster(path: str) -> list[dict[str, str]]:
    """`read_roster` plus the duplicate checks: what every admin script starts from."""
    rows = read_roster(path)
    check_duplicates(rows)
    return rows


def load_zuliprc(path: str) -> tuple[str, str, str]:
    """(site, email, key) from a zuliprc; the key is returned, never logged.

    A file that doesn't parse, isn't UTF-8 or lacks `[api]`'s site, email or key is a
    ValueError that names the file and says what failed in words of this function's own: a
    parse error by its exception's type, never its text, since configparser's messages quote
    the offending line, which may be `key=…` (#523). It is raised outside the handler, so no
    traceback chains the original. A file that can't be opened is the OSError, which names
    only the path."""
    cp = configparser.ConfigParser(interpolation=None)
    try:
        with open(pathlib.Path(path).expanduser(), encoding="utf-8") as f:
            cp.read_file(f)
    except (configparser.Error, UnicodeDecodeError) as exc:
        why = type(exc).__name__
    else:
        missing = [name for name in ("site", "email", "key") if not cp.has_option("api", name)]
        if not missing:
            site = cp.get("api", "site").rstrip("/")
            if not site.startswith("https://"):
                raise ValueError(f"{path}: the Zulip site must use HTTPS")
            return site, cp.get("api", "email"), cp.get("api", "key")
        why = f"no {missing[0]} in [api]" if cp.has_section("api") else "no [api] section"
    raise ValueError(f"{path}: {why}") from None


def run_parallel(
    items: list[T],
    fn: Callable[[T], R],
    *,
    parallel: int,
    on_error: Callable[[T, BaseException], R],
) -> list[R]:
    """Apply `fn` to every item, `parallel` at a time; an exception becomes `on_error(...)`.

    Results keep the input order, so one VM's failure never hides another's row (README.md §2, §3).
    """

    def guarded(item: T) -> R:
        try:
            return fn(item)
        except Exception as exc:  # per-row isolation is the point
            return on_error(item, exc)

    if parallel <= 1 or len(items) <= 1:
        return [guarded(item) for item in items]
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        return list(pool.map(guarded, items))


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """A plain fixed-width table for the terminal (and for pasting into Zulip as code)."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * w for w in widths)
    body = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)) for r in rows]
    return "\n".join([line, sep, *body])
