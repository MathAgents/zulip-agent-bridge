"""A18's laptop: the development machine as Drew's admin checkout in §8.4 (docs/ADMIN.md §7; #356,
#358).

A18 rehearses the kept fleet's move onto the admin server on the rig's second workshop,
`zab-admin-e2e2`; the steps named here (O4 to O6, the fallback's) are those of that move's
runbook, ADMIN §8.4 at v0.2.15. The development machine plays the laptop of §8.4: `Laptop` keeps
a private directory (0700) with the workshop's roster, its bots' keys (`out/`), the pinned host
keys and the server's public key, as `~/zab-admin` holds them, and runs this checkout's admin
scripts on it, as argument lists, with the rig's inputs: the development machine's AWS
credentials (the rig's account) for `launch.py`, a test user's zuliprc for the Zulip scripts,
and the rig's key (`zab-test`, the break-glass key here) for `join_server.py`. What runs on the
server is a fixed script with its inputs as arguments (`FRESH`, `STAGE`; CONTRIBUTING.md, rule
3).

The pure helpers are tested offline (`test_harness_kept.py`): `row_of` reads the scripts'
tables, `stage_tar` packs a stage as O4 sends it, `refused_stage` makes one that also holds
another workshop's slug, `restore_bots` makes a previous run's bots active again, the test
provisioner's (`zab-tester`), without ever changing a bot's owner (#758), and
`restore_sandbox` moves a sandbox zab-tester can't see out of its way, before
`zulip_provision.py` looks for them. Waits are `settle.until`'s.
"""

from __future__ import annotations

import base64
import configparser
import contextlib
import csv
import io
import os
import re
import shutil
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from e2e.admin.harness.rig import E2E2, PARTICIPANT_TYPE, AdminRig
from e2e.harness.admin import Local, local
from e2e.harness.config import CHECKOUT
from e2e.harness.secret_files import label_of, read_secret
from e2e.harness.vm import Result, Vm, say

#: The second workshop's one row (§7): `zab-tester2`'s address, launched only by A18.
SLUG = "zab-test-c"
KINDS = ("codex", "claude")
#: The laptop's directory, in the rig's (`laptop_dir`).
LAPTOP_PREFIX = "a18-laptop-"
#: The bound of a stop, a start or a wake to show in EC2, and of SSH to answer after one: a
#: transition of a minute or two, and a read that lags behind it (#378, `settle`).
TRANSITION_S = 300.0
#: TEST-NET-1 (RFC 5737): the address of the refused stage's extra row, never a VM.
NOWHERE = "192.0.2.10"
#: The laptop roster's row torn down since, left out of `keep.txt`, and its old address.
GONE, GONE_IP = "zab-test-d", "192.0.2.11"
#: What the refused stage holds instead of every bot key: none travels for a stage refused.
PLACEHOLDER_RC = (
    "[api]\nemail=placeholder@example.invalid\nkey=none\nsite=https://example.invalid\n"
)

#: As `ubuntu` on the VM, over the break-glass key: **a stand-in until Q9** for the project
#: checkout that Oct 9's `zab setup-repo` left on every kept VM. push_config.py makes it the
#: bridge's `default_project`, `~/work/<repo>`, and the rollout's doctor fails without a git
#: repository there. Here, an empty repository of `agent`'s, checked as the doctor checks it
#: (`git rev-parse --show-toplevel`), made once: a rerun says `exists` and changes nothing. Once
#: the MathAgents token is placed (Q9), `provision_repos.py` makes the real checkout instead.
#: $1 is the checkout's name: zab-test-c only.
STANDIN = r"""set -eu
case $1 in zab-test-c) ;; *) echo "refused: $1" >&2; exit 2 ;; esac
cd /
dir=/home/agent/work/$1
if sudo -u agent git -C "$dir" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "exists"
else
  sudo -u agent mkdir -p -- "$dir"
  sudo -u agent git init -q -- "$dir"
  echo "initialized"
fi
"""
#: As root: the second workshop's state removed, as it was before O4 (the setup's reset
#: made `ws/zab-admin-e2e2/`, and A15 imported a roster into it): stage_server.py makes the
#: directory itself, and refuses one that exists. zab-admin-e2e2 only.
FRESH = r"""set -eu
case $1 in zab-admin-e2e2) ;; *) echo "refused: $1" >&2; exit 2 ;; esac
rm -rf -- "/var/lib/zab-admin/ws/$1"
"""
#: As root: O4's steps 2 and 3. The stage comes on standard input (`stage_tar`: a tar,
#: in base64), is unpacked as `zab-admin` into a new 0700 directory, and `stage_server.py` runs
#: on it as `zab-admin`, from the release, with `--config`, `--workshop` and `--retired`, into
#: `ws/$1/`. The stage is removed whatever happens; the exit code is stage_server.py's.
STAGE = r"""set -euo pipefail
case $1 in zab-admin-e2e2) ;; *) echo "refused: $1" >&2; exit 2 ;; esac
t=$(mktemp -d)
trap 'rm -rf -- "$t"' EXIT
chown zab-admin:zab-admin "$t"
base64 -d | sudo -u zab-admin tar -xf - --no-same-owner -C "$t"
sudo -u zab-admin env -i PATH=/usr/bin:/bin HOME=/var/lib/zab-admin LANG=C.UTF-8 \
  /opt/zab-admin/current/.venv/bin/python -E -s \
  /opt/zab-admin/current/workshop/admin/stage_server.py --roster "$t/roster.csv" \
  --out-dir "$t/out" --to "/var/lib/zab-admin/ws/$1" \
  --config /etc/zab-admin/admin.toml --workshop "$1" \
  --retired /var/lib/zab-admin/retired-slugs.txt
"""


@contextlib.contextmanager
def laptop_dir(rig: AdminRig) -> Iterator[Path]:
    """The laptop's directory: it holds the bots' zuliprcs, so it lives in the rig's directory
    (0700, where §7 keeps the rig's keys), never in /tmp, and goes when A18 ends, whatever
    ended it. One that a killed run left there is removed first (one run at a time: run.lock)."""
    for stale in rig.dir.glob(f"{LAPTOP_PREFIX}*"):
        shutil.rmtree(stale, ignore_errors=True)
    path = Path(tempfile.mkdtemp(prefix=LAPTOP_PREFIX, dir=rig.dir))  # 0700
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def tail(done: Result, lines: int = 12) -> str:
    """A failed script's last lines, for the assertion (the scripts print no key)."""
    text = (done.stdout + "\n" + done.stderr).strip().splitlines()
    return f"exit {done.returncode}: " + "\n".join(text[-lines:])


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@dataclass
class Laptop:
    """§8.4's laptop, in `dir` (0700, the caller's): A18's admin checkout on the development
    machine."""

    dir: Path
    rig: AdminRig
    run: Local = local

    @property
    def roster(self) -> Path:
        return self.dir / "roster.csv"

    @property
    def out(self) -> Path:
        return self.dir / "out"

    @property
    def known_hosts(self) -> Path:
        return self.out / "known_hosts"

    @property
    def server_key(self) -> Path:
        """The server's public key, as O4 brings it back (`zab-admin-fleet.pub`)."""
        return self.dir / "zab-admin-fleet.pub"

    def script(self, rel: str, *args: str, timeout: float = 300) -> Result:
        """`workshop/<rel>` of this checkout with `args`, in the development machine's environment
        (its AWS credentials, for launch.py), without `ZAB_SSH_IDENTITY`: each is given its key."""
        argv = [sys.executable, str(CHECKOUT / "workshop" / rel), *args]
        env = {k: v for k, v in os.environ.items() if k != "ZAB_SSH_IDENTITY"}
        return self.run(argv, env, timeout)

    @property
    def keep(self) -> Path:
        """`keep.txt`, the kept slugs (§8.4), which stage, join and move select by."""
        return self.dir / "keep.txt"

    def write_roster(self, email: str, region: str) -> None:
        """The roster as Oct 9's is now: the kept row, and one torn down since, its address
        perhaps someone else's by now (`GONE`), left out of `keep.txt`, so that nothing of §8.4
        that selects by `--keep` ever reaches it."""
        kept = {"slug": SLUG, "first_name": "Zab", "last_name": "Test C", "email": email,
                "repo": f"MathAgents/{SLUG}", "region": region, "host": "", "ip": "",
                "notes": "A18: kept"}  # fmt: skip
        gone = {"slug": GONE, "first_name": "Zab", "last_name": "Test D",
                "email": "zab-admin-e2e-d@example.com", "repo": f"MathAgents/{GONE}",
                "region": region, "host": f"ws-{GONE}", "ip": GONE_IP,
                "notes": "A18: torn down, not kept"}  # fmt: skip
        write_csv(self.roster, [kept, gone])
        self.keep.write_text(f"# the kept VMs (docs/ADMIN.md §8.4)\n{SLUG}\n", encoding="utf-8")

    def row(self) -> dict[str, str]:
        """The kept row, as the scripts have filled it in."""
        return next(r for r in read_csv(self.roster) if r["slug"] == SLUG)

    def bot_email(self, kind: str) -> str:
        """The e-mail of the row's bot of `kind`, from the zuliprc zulip_provision.py wrote; a
        failure names the file in the laptop's directory, never quotes it (#519)."""
        path = self.out / SLUG / f"{kind}.zuliprc"

        def read() -> str:
            config = configparser.ConfigParser(interpolation=None)
            config.read(path, encoding="utf-8")
            return config.get("api", "email").strip()

        return read_secret(label_of(path, self.dir), read)

    def provision(self, zuliprc: Path, *flags: str) -> Result:
        """zulip_provision.py for the row with `zuliprc`'s key (`--hold`, `--dry-run`, …)."""
        return self.script("admin/zulip_provision.py", "--roster", str(self.roster),
                           "--zuliprc", str(zuliprc), "--out-dir", str(self.out),
                           "--only", SLUG, *flags)  # fmt: skip

    def launch(self, *, ami: str, group: str, ssh_from: list[str]) -> Result:
        """launch.py as on Oct 9: the rig's key pair (the development machine's key), the second
        workshop's scope with `zab-role=test` (test infrastructure only), and no `--admin-key`: no
        server key. With `--ssh-from` for each address the group should admit (the server's, the
        development machine's), so that launch.py refuses a group that admits any other source,
        IPv6, group or prefix list included, before it launches."""
        if not ssh_from:
            raise ValueError("launch.py is always given the group's expected SSH sources")
        scope = [arg for key, value in E2E2.tags.items() for arg in ("--tag", f"{key}={value}")]
        sources = [arg for cidr in ssh_from for arg in ("--ssh-from", cidr)]
        return self.script(
            "aws/launch.py", "--roster", str(self.roster), "--ami", ami,
            "--security-group-id", group, "--region", self.rig.region,
            "--instance-type", PARTICIPANT_TYPE, "--key-name", self.rig.key_pair, "--only", SLUG,
            "--spares-file", str(self.dir / "spares.json"), *scope, *sources, timeout=2400,
        )  # fmt: skip

    def scan(self) -> Result:
        return self.script("admin/scan_hosts.py", "--roster", str(self.roster),
                           "--known-hosts", str(self.known_hosts), "--only", SLUG)  # fmt: skip

    def stage(self, to: Path) -> Result:
        """O4's step 1: the kept rows (`--keep`), their bots' keys and known_hosts, into the
        new directory `to`."""
        return self.script("admin/stage_server.py", "--roster", str(self.roster),
                           "--keep", str(self.keep), "--out-dir", str(self.out),
                           "--to", str(to))  # fmt: skip

    def stand_in_checkout(self, log: Callable[[str], None] = say) -> Result:
        """`STANDIN` on the row's VM (the roster's `ip`), as `ubuntu`, with the break-glass key
        and the host key scan_hosts.py pinned: how the laptop reaches the kept VMs (§8.4). Its
        connect failures go to `log` (the scenario's notes: `conftest.note_ssh`)."""
        vm = Vm(self.row()["ip"], key=self.rig.ssh_key, known_hosts=self.known_hosts, log=log)
        env = {k: v for k, v in os.environ.items() if k != "ZAB_SSH_IDENTITY"}
        return vm.call(["bash", "-c", STANDIN, "bash", SLUG], 120,  # retried as `Vm.run` (#517)
                       lambda argv, seconds: self.run(argv, env, seconds))  # fmt: skip

    def join(self, *, dry_run: bool = False, remove: bool = False) -> Result:
        """O5 (or the fallback's step 5, `remove`) over the break-glass key, the rig's, on the
        kept rows (`--keep`)."""
        flags = ["--remove"] * remove + ["--dry-run"] * dry_run
        return self.script(
            "admin/join_server.py", "--roster", str(self.roster), "--keep", str(self.keep),
            "--identity", str(self.rig.ssh_key), "--server-key", str(self.server_key),
            "--known-hosts", str(self.known_hosts), *flags,
        )  # fmt: skip

    def move(self, zuliprc: Path, to: Path, name: str, uid: int, *, apply: bool) -> Result:
        """O6: the kept rows' bots (`--keep`) to the provisioner of the zuliprc `to`, pinned by
        its full name and user ID; a dry run unless `apply`, which needs an organization
        administrator's key (#758)."""
        return self.script(
            "admin/move_bots.py", "--roster", str(self.roster), "--keep", str(self.keep),
            "--zuliprc", str(zuliprc), "--to", "provisioner", "--provisioner-zuliprc", str(to),
            "--provisioner-name", name, "--provisioner-id", str(uid), "--out-dir", str(self.out),
            *(["--apply"] if apply else []),
        )  # fmt: skip


def table(text: str, first: str) -> list[dict[str, str]]:
    """The rows of the table `admin_common.format_table` printed in `text`, whose first header
    is `first`, each as {header: cell}: cut where the separator's dashes start, since a cell
    may hold spaces. A row is one whose first cell is a slug: a summary printed right after
    the table (`1 of 1 ok`) isn't. AssertionError when there is no such table."""
    lines = text.splitlines()
    for i, line in enumerate(lines[:-1]):
        if line.split("  ")[0] == first and re.fullmatch(r"-+(  -+)*", lines[i + 1].rstrip()):
            starts = [m.start() for m in re.finditer(r"-+", lines[i + 1])]
            spans = list(zip(starts, [*starts[1:], None], strict=True))
            heads = [line[a:b].strip() for a, b in spans]
            rows = []
            for body in lines[i + 2 :]:
                if not body.strip():
                    break
                rows.append({h: body[a:b].strip() for h, (a, b) in zip(heads, spans, strict=True)})
            return [r for r in rows if re.fullmatch(r"[a-z0-9][a-z0-9-]*", r[first])]
    raise AssertionError(f"no table whose first column is {first!r} in: {text[-500:]}")


def row_of(done: Result, first: str) -> dict[str, str]:
    """The one row of `SLUG` in the table a script printed (`table`)."""
    rows = [r for r in table(done.stdout + "\n" + done.stderr, first) if r[first] == SLUG]
    if len(rows) != 1:
        raise AssertionError(f"{len(rows)} rows of {SLUG}: {tail(done)}")
    return rows[0]


def stage_tar(stage: Path) -> str:
    """The stage as O4 sends it (`tar -cf - roster.csv out`), in base64 for `STAGE`'s standard
    input: its directories and regular files only, by relative path, with no owner (the server
    unpacks with `--no-same-owner`)."""

    def plain(info: tarfile.TarInfo) -> tarfile.TarInfo:
        if not (info.isfile() or info.isdir()):
            raise ValueError(f"{info.name}: neither a file nor a directory")
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in ("roster.csv", "out"):
            tar.add(stage / name, arcname=name, filter=plain)
    return base64.b64encode(data.getvalue()).decode("ascii") + "\n"


def refused_stage(stage: Path, to: Path, slug: str) -> None:
    """At `to` (new, 0700): `stage`'s rows and one more, `slug`, which another workshop's roster
    holds (§2.1: refused on the server). That row's VM is nowhere (`NOWHERE`), and every key file
    is a placeholder: nothing is to land, and no key travels for it."""
    rows = read_csv(stage / "roster.csv")
    extra = dict.fromkeys(rows[0], "") | {
        "slug": slug, "email": "zab-admin-e2e-a18-refused@example.com", "ip": NOWHERE,
        "region": rows[0].get("region", ""),
    }  # fmt: skip
    to.mkdir(mode=0o700)
    write_csv(to / "roster.csv", [*rows, extra])
    (to / "out").mkdir(mode=0o700)
    shutil.copyfile(stage / "out" / "known_hosts", to / "out" / "known_hosts")
    for row in (*rows, extra):
        (to / "out" / row["slug"]).mkdir(mode=0o700)
        for kind in KINDS:
            (to / "out" / row["slug"] / f"{kind}.zuliprc").write_text(PLACEHOLDER_RC)


def bot_emails(domain: str) -> dict[str, str]:
    """The row's bots' e-mails before any zuliprc: Zulip names a bot `<short name>-bot@<the
    realm's bot domain>`, and zulip_provision.py's short names are `<kind>-<slug>`."""
    return {kind: f"{kind}-{SLUG}-bot@{domain}" for kind in KINDS}


def bot_name(kind: str) -> str:
    """The bot's full name, as zulip_provision.py gives it (`bot_full_name`)."""
    return f"{kind.capitalize()} · {SLUG}"


async def restore_bots(tester: Any, ids: Any, emails: dict[str, str]) -> list[str]:
    """The row's bots as `zulip_provision.py --hold` must find them with the test provisioner's
    key, `zab-tester`'s: active, and zab-tester's, as they are after the kept fleet's move (O6).
    Since Nov 2, Zulip Cloud lets only an organization administrator change a bot's owner, the
    owner included (#758), and no test user is one: nothing here ever changes an owner. A
    previous A18 deactivated the bots at its end; a deactivated bot keeps its e-mail and full
    name, so no new bot can take them, and its owner reactivates it (`can_admin_user`).

    Each is looked up by its e-mail (a bot's address is visible to every key), and matched as
    move_bots.py and `--deactivate` match a bot, by full name and owner, all before anything
    changes: a bot not made yet (Zulip's "no such user") is left to zulip_provision.py. One of
    zab-tester2's, as A18 left them before #758, is refused (LookupError), naming the one step
    that needs an administrator; so is anything else: a user that isn't a bot, another name,
    another owner. Any other error is raised. `tester` is zab-tester's client (`request`).
    One line a bot."""
    from zab.zulip.client import ZulipError

    found: dict[str, dict[str, Any] | None] = {}
    for kind, email in emails.items():
        try:
            user = (await tester.request("GET", f"users/{quote(email, safe='@')}"))["user"]
        except ZulipError as exc:
            if (exc.status, exc.code) != (400, "BAD_REQUEST"):  # a 401 or a 429 isn't "none"
                raise
            found[kind] = None
            continue
        owner = user.get("bot_owner_id")
        if user.get("is_bot") and owner == ids.tester2:
            raise LookupError(
                f"{email} is zab-tester2's, as A18 left it before #758: only an organization "
                "administrator can give it to zab-tester (in Zulip, Organization settings → "
                "Bots, its owner), and no new bot can take its name: left alone"
            )
        if not user.get("is_bot") or owner != ids.tester:
            raise LookupError(f"{email}: not a bot of zab-tester's: left alone")
        if user.get("full_name") != bot_name(kind):
            raise LookupError(f"{email} is named {user.get('full_name')!r}: left alone")
        found[kind] = user
    done = []
    for kind, user in found.items():
        if user is None:
            done.append(f"{kind}: none yet")
            continue
        uid = int(user["user_id"])
        if user.get("is_active") is False:
            await tester.request("POST", f"users/{uid}/reactivate")
        after = (await tester.request("GET", f"users/{uid}"))["user"]
        if after.get("is_active") is False or after.get("bot_owner_id") != ids.tester:
            raise LookupError(
                f"{emails[kind]}: active {after.get('is_active')}, "
                f"owner {after.get('bot_owner_id')}"
            )
        was = "reactivated" if user.get("is_active") is False else "active already"
        done.append(f"{kind}: zab-tester's, {was}")
    return done


def sandbox_name() -> str:
    """The row's sandbox, as zulip_provision.py names it (`channel_name`)."""
    return f"{SLUG}-sandbox"


async def restore_sandbox(tester: Any, tester2: Any) -> str:
    """The row's sandbox as `zulip_provision.py --hold` with zab-tester's key must find it:
    one zab-tester can see, or none, which it then creates. Until #758, A18 provisioned with
    zab-tester2's key, which made the sandbox zab-tester2's to administer, and invisible to
    zab-tester (the provisioner's dry run said `channel would create`, report
    `20261102T061801Z-ab0cca61`), while Zulip refuses a new channel whose name is taken
    (`check_stream_name_available`). Such a sandbox is renamed out of the way once, by its
    administrator, zab-tester2 (`PATCH /streams/{id}`, `new_name`); nothing else is changed. A
    refused rename is a LookupError that says what to do by hand. Names compare as Zulip
    compares them, ignoring case. One line."""
    from zab.zulip.client import ZulipError

    name = sandbox_name().casefold()

    async def named(client: Any) -> list[dict[str, Any]]:
        streams = (await client.request("GET", "streams", include_all_active=True))["streams"]
        return [s for s in streams if str(s.get("name", "")).casefold() == name]

    if await named(tester):
        return f"{sandbox_name()}: seen by zab-tester"
    theirs = await named(tester2)
    if not theirs:
        return f"{sandbox_name()}: none yet"
    sid = int(theirs[0]["stream_id"])
    old = f"{sandbox_name()}-old-{sid}"
    try:
        await tester2.request("PATCH", f"streams/{sid}", new_name=old)
    except ZulipError as exc:
        raise LookupError(
            f"{sandbox_name()} ({sid}) is zab-tester2's, and zab-tester can't see it, but "
            f"zab-tester2 could not rename it {old}: {exc}. Rename it so in Zulip, signed in as "
            "zab-tester2 or an organization administrator, then run A18 again"
        ) from exc
    return f"{sandbox_name()}: zab-tester2's, renamed {old} by zab-tester2 (#758)"
