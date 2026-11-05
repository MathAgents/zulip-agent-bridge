"""Shared pieces of `launch.py` and `teardown.py` (README.md; the roster:
workshop/admin/README.md §4).

Standalone scripts, not part of the `zab` package: run them with `uv run --with boto3 python
workshop/aws/launch.py …` from a machine with AWS credentials (Drew's laptop). boto3 is imported
lazily inside `Ec2`, so this module, the roster helpers and the tests need no AWS library.
Everything that touches AWS goes through `Ec2`; tests replace it with a fake.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import csv
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

#: The tag that scopes every lookup, creation and teardown to this workshop (README.md, "The VMs").
#: The scripts take `--tag KEY=VALUE` to override it (for example for a dry-run fleet), repeatable:
#: the test rig's VM carries `zab-role=test` and `workshop=zab-test` (issue #263). Every scope tag
#: goes on every resource created, and every lookup requires all of them (`parse_tags`).
WORKSHOP_TAG = ("workshop", "nt-agents-2026")
#: Where a release tag installs from, `git+URL@TAG`: the public repository, whose tags are the
#: releases (#593). `ZAB_REPO` overrides it in push_config.py, build_ami.py and build.sh; it
#: must be a URL uv installs from (https:// or ssh://), not scp-style `git@host:path`.
ZAB_REPO_URL = "https://github.com/MathAgents/zulip-agent-bridge"
DEFAULT_TAG_SPEC = f"{WORKSHOP_TAG[0]}={WORKSHOP_TAG[1]}"
#: Tags set per participant (`launch.participant_tags`), never part of a scope.
RESERVED_TAG_KEYS = frozenset({"Name", "participant"})
#: The admin server's fleet-wide scope tag (docs/ADMIN.md §5): one value for every workshop of
#: a server, so it names none of them in `spares.json` keys (`scope_label`).
FLEET_SCOPE_KEY = "zab-scope"
ROSTER_COLUMNS = [
    "slug",
    "github",
    "first_name",
    "last_name",
    "email",
    "repo",
    "host",
    "ip",
    "region",
    "host_keys",
    "zulip_user_id",
    "zulip_invite",
    "zulip_channel_id",
    "notes",
]

#: Keys `read_roster` adds to a row for the scripts' own use, never written back.
INTERNAL_KEYS = frozenset({"slug_given"})
#: The participant's identity since the no-GitHub onboarding (issue #36, Sun Oct 4): the VM
#: (`ws-<slug>`, a DNS label), the `participant` tag, the bots (`Claude · <slug>`, short names
#: `claude-<slug>`), the channel (`<slug>-sandbox`), the repository (`MathAgents/<slug>`) and
#: `~/work/<slug>` all come from it. Lower-case letters, digits and inner hyphens, 1 to 32
#: characters: valid in every one of those places, and short enough for Zulip's 60-character
#: channel names and bot e-mail local parts.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
_EMAIL_RE = re.compile(r"^[^@\s\"'<>,;]+@[^@\s\"'<>,;]+\.[^@\s\"'<>,;]+$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
#: Spare VMs live in their own namespace so that no participant can collide with one: the tag
#: `participant=zab-spare-N` and the name `ws-zab-spare-N`. A roster slug matching this pattern
#: is refused before anything is launched.
_SPARE_RE = re.compile(r"^zab-spare-[0-9]{1,3}$")


# ------------------------------------------------------------------------------------ roster


def read_roster(path: str) -> list[dict[str, str]]:
    """Every row of the roster (workshop/admin/README.md §4), every column kept (unknown ones too),
    stripped.

    Rows without a `slug` (and without a `github` login to take it from) are kept so that
    `write_roster` can give the file back whole; `participants()` is the view the launch works
    on, and only those rows are validated. A roster from before the slug column (GitHub logins
    only) still works: the slug is the login in lower case, written back on the next save.
    """
    # utf-8-sig: a byte-order mark (Excel, Numbers) would otherwise become part of the first
    # column's name, every row would lose its `github` value and the roster would look empty.
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    out: list[dict[str, str]] = []
    for row in rows:
        # header names are normalised (`GitHub` → `github`): a capitalised header would hide
        # every participant just as the byte-order mark did
        clean = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k is not None}
        for col in ROSTER_COLUMNS:
            clean.setdefault(col, "")
        if not clean["slug"] and clean["github"]:
            clean["slug"] = clean["github"].lower()
        elif clean["slug"]:
            clean["slug_given"] = "1"  # not a column: launch.py's legacy-tag lookup reads it
        if clean["slug"]:
            validate_row(clean)
        out.append(clean)
    check_duplicates(out)
    return out


def check_duplicates(rows: list[dict[str, str]]) -> None:
    """Refuse a roster in which two rows share a slug or an e-mail (case-insensitive).

    The launch is keyed by the slug: with a duplicate the second row silently adopted the first
    row's VM, so two participants would have shared one machine (ORGANIZERS §7.2's hand-over would
    then put both on it). Two rows with one e-mail would bind one Zulip user, and its invite,
    to two VMs in `zulip_provision.py` (workshop/admin/README.md §1). The `github` column is
    `provision_repos.py`'s (issue #36, Mon Oct 5) and plays no part in a launch.
    """
    for column in ("slug", "email"):
        seen: dict[str, str] = {}
        for row in participants(rows):
            value = row.get(column, "")
            if not value:
                continue
            key = value.casefold()
            if key in seen and seen[key] != value:
                raise ValueError(f"duplicate {column} {seen[key]!r} / {value!r} (same {column})")
            if key in seen:
                raise ValueError(f"duplicate {column} {value!r}")
            seen[key] = value


def slug_of(row: dict[str, str]) -> str:
    """The row's slug. A row built without one (a roster from before the column, or a caller
    that only knows the login) has the GitHub login in lower case, as `read_roster` assigns."""
    return row.get("slug") or row.get("github", "").lower()


def column_snapshot(
    rows: list[dict[str, str]], columns: Iterable[str]
) -> dict[str, dict[str, str]]:
    """`slug -> {column: value}` for `rows`, taken when a script reads the roster, so that it can
    later write back only what it changed (`roster_changes`, `merge_roster_changes`)."""
    cols = tuple(columns)
    return {slug_of(r): {c: r.get(c, "") for c in cols} for r in rows if slug_of(r)}


def roster_changes(
    rows: list[dict[str, str]], before: Mapping[str, Mapping[str, str]]
) -> dict[str, dict[str, str]]:
    """`slug -> {column: new value}` for the snapshot's columns that this run changed."""
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        old = before.get(slug_of(row))
        if old is None:
            continue
        changed = {c: row.get(c, "") for c, v in old.items() if row.get(c, "") != v}
        if changed:
            out[slug_of(row)] = changed
    return out


def merge_roster_changes(
    path: str,
    changes: Mapping[str, Mapping[str, str]],
    expect: Mapping[str, Mapping[str, str]] | None = None,
) -> list[str]:
    """Apply `changes` to the roster file as it is now: re-read it just before writing, set only
    the changed columns of the rows whose slug is still there, and write it back.

    A script that writes back the whole roster it read at its start would undo what another one
    wrote in the meantime: a `zulip_user_id` from `zulip_provision --only`, an `ip` from a spare
    hand-over (PM on issue #36, Mon 20:15; `zulip_provision.merge_roster` does the same for its
    own columns). `launch.py` writes after a launch of 20 to 40 minutes (PM, Mon 22:30).
    `expect` makes a change conditional: `slug -> {column: value}` sets that column only while
    the file still holds that value there. A stale `ip` cleared (#604) is cleared so, and a new
    address written meanwhile, a spare's handed over, say, stays (Codex on #606).
    Returns the slugs no longer in the roster (renamed or deleted meanwhile), whose changes are
    dropped. Nothing is written when there is nothing to apply. Raises ValueError when the
    roster no longer reads (`read_roster`), and then writes nothing."""
    if not changes:
        return []
    current = read_roster(path)
    present = {slug_of(r): r for r in current if slug_of(r)}
    gone = sorted(s for s in changes if s not in present)
    applied = False
    for slug, values in changes.items():
        row = present.get(slug)
        if row is None:
            continue
        wanted = (expect or {}).get(slug, {})
        values = {c: v for c, v in values.items() if c not in wanted or row.get(c) == wanted[c]}
        if values:
            row.update(values)
            applied = True
    if applied:
        write_roster(path, current)
    return gone


def save_roster_changes(
    path: str,
    rows: list[dict[str, str]],
    before: Mapping[str, Mapping[str, str]],
    *,
    script: str,
    expect: Mapping[str, Mapping[str, str]] | None = None,
) -> bool:
    """Write back this run's changes to `before`'s columns (`merge_roster_changes`, with its
    `expect`), reporting on stderr. False when the roster no longer reads or cannot be written
    (someone saved a broken row meanwhile, say): nothing is written, and this run's changes are
    printed, so that nothing is lost before the rerun (PM nit on #166)."""
    changes = roster_changes(rows, before)
    try:
        gone = merge_roster_changes(path, changes, expect)
    except (OSError, ValueError) as exc:
        print(
            f"roster NOT written: {exc}. Fix {path} and rerun {script}. This run's changes:",
            file=sys.stderr,
        )
        for slug, values in sorted(changes.items()):
            cells = ", ".join(f"{c}={v}" for c, v in values.items())
            print(f"  {slug}: {cells}", file=sys.stderr)
        return False
    if gone:
        print(f"warning: no longer in the roster, not recorded: {', '.join(gone)}", file=sys.stderr)
    return True


def participants(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """The rows with a slug: the ones that get a VM."""
    return [row for row in rows if slug_of(row)]


def stale_ip_rows(rows: list[dict[str, str]], name: str, ip: str) -> list[dict[str, str]]:
    """The roster rows other than `name`'s whose `ip` is `ip`, an address EC2 has just shown on
    `name`'s VM: theirs is stale. EC2 can give an Elastic IP released with one VM (a teardown)
    to the next one allocated, in this workshop too; a row that kept it would share it with the
    new VM's in the roster, which every admin script then refuses (`check_duplicates`), and
    would reach that VM as its own (#604)."""
    if not ip:
        return []
    return [r for r in participants(rows) if slug_of(r) != name and r.get("ip", "") == ip]


def roster_fieldnames(path: str, rows: list[dict[str, str]]) -> list[str]:
    """The file's own header order when it exists, else the roster's (workshop/admin/README.md
    §4), plus any column only the rows have."""
    fields: list[str] = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            fields = [c.strip().lower() for c in (next(csv.reader(f), []) or []) if c.strip()]
    if not fields:
        fields = list(ROSTER_COLUMNS)
    for col in ROSTER_COLUMNS:
        if col not in fields:
            fields.append(col)
    for row in rows:
        for col in row:
            if col not in fields and col not in INTERNAL_KEYS:
                fields.append(col)
    return fields


def write_roster(path: str, rows: list[dict[str, str]]) -> None:
    """Write the roster back atomically, keeping every row and column, with a `.bak` of the old.

    The temp file is written next to the roster and `os.replace`d over it, so a crash never leaves
    a half-written roster; the previous version survives as `<path>.bak`. The temp file's name is
    unique (`mkstemp`), so two scripts writing at once never share it (PM on issue #36: a
    15-minute `provision_repos --invites-only` loop overlaps other runs).
    """
    fields = roster_fieldnames(path, rows)
    # The roster is personal data: the temp file is created 0600 (never the umask default)
    # and takes the original's mode before it replaces it; `.bak` keeps that mode via copy2.
    mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o600
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                w.writerow({col: row.get(col, "") for col in fields})
        os.chmod(tmp, mode)
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)
        raise


def validate_row(row: dict[str, str]) -> None:
    """Slugs, e-mails and repository names are restricted so they are safe in tags, names and
    shell.

    Slugs of the form `zab-spare-N` are reserved for spare VMs (`spare_rows`): a participant
    with such a slug would share a spare's tags, so the roster is refused. The e-mail is checked
    here when present and required by the scripts that use it (`launch.py` for git's identity,
    `zulip_provision.py`, `push_config.py`). The `github` column is not checked here: the
    launch ignores it, and `provision_repos.py` validates the login it invites (issue #36).
    """
    slug = slug_of(row)
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"invalid slug {slug!r}: lower-case letters, digits and inner hyphens, 1-32 characters"
        )
    if _SPARE_RE.match(slug):
        raise ValueError(f"slug {slug!r} collides with the spare VM names (zab-spare-N)")
    if row.get("email") and not _EMAIL_RE.match(row["email"]):
        raise ValueError(f"invalid e-mail {row['email']!r} for {slug}")
    if row.get("repo") and not _REPO_RE.match(row["repo"].split("/")[-1]):
        raise ValueError(f"invalid repository name {row['repo']!r} for {slug}")


def repo_name(repo: str) -> str:
    """`owner/name` or `name` → `name`."""
    name = repo.strip().rstrip("/").split("/")[-1]
    return name[:-4] if name.endswith(".git") else name


def spare_rows(count: int) -> list[dict[str, str]]:
    """Synthetic rows for spare VMs, held for late joiners and broken VMs, named `zab-spare-N`.

    They carry `spare="1"` and are not part of the roster.
    """
    rows = []
    for i in range(1, count + 1):
        row = {col: "" for col in ROSTER_COLUMNS}
        row["slug"] = f"zab-spare-{i}"
        row["spare"] = "1"
        rows.append(row)
    return rows


def is_spare(row: dict[str, str]) -> bool:
    return bool(row.get("spare")) and is_spare_name(slug_of(row))


def is_spare_name(name: str) -> bool:
    """`zab-spare-N`: a spare's `participant` tag, never a roster slug."""
    return bool(_SPARE_RE.match(name))


# ------------------------------------------------------------------------------- user data

#: Decisions taken after the AMI was specified (issue #36; ../ami/README.md, "First boot"), applied
#: at every first boot so that they hold whether or not the image already carries them. Every step
#: is idempotent and runs as root (cloud-init). Mirrored in `workshop/ami/build.sh`.
#: - An 8 GiB swap file: Ubuntu AMIs have no swap, and a participant's big Magma or Sage job
#:   would otherwise get an agent, or the bridge, OOM-killed.
#: - Claude Code's auto-updater off through the managed settings file, which the participant's
#:   own settings cannot override and which the CLI reads under `zab.service` too (verified on
#:   2.1.285: `claude doctor` reports "Auto-updates: disabled (set by env: DISABLE_AUTOUPDATER)").
#: - Codex's update check off (`check_for_update_on_startup`, in the 0.157.1 config schema),
#:   written at the top of the file so that it stays a root key even when the file ends inside a
#:   table such as `[projects."…"]` (codex round 1 on PR #53); the MCP table goes at the end.
#: - The LMFDB MCP server for both CLIs: `mcp_servers.<id>.url` is Codex's streamable-HTTP
#:   form; `claude mcp add --scope user` writes only `mcpServers` and first-start bookkeeping to
#:   `~/.claude.json`, so the participant's first login still runs onboarding (verified 2.1.285).
COMMON_FIRST_BOOT = """\
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  fallocate -l 8G /swapfile && chmod 0600 /swapfile && mkswap /swapfile >/dev/null \\
    && swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw,nofail 0 0' >> /etc/fstab
managed=/etc/claude-code/managed-settings.json
install -d -m 0755 /etc/claude-code
python3 - "$managed" <<'PY'
import json, os, sys
path = sys.argv[1]
data = json.load(open(path)) if os.path.exists(path) else {}
data.setdefault("env", {})["DISABLE_AUTOUPDATER"] = "1"
data["disableClaudeAiConnectors"] = True  # claude.ai connectors off (issue #36, DECISIONS)
with open(path, "w") as f:
    json.dump(data, f)
PY
chmod 0644 "$managed"
codexrc=/home/agent/.codex/config.toml
install -d -o agent -g agent -m 0755 /home/agent/.codex
[ -f "$codexrc" ] || install -o agent -g agent -m 0644 /dev/null "$codexrc"
if ! grep -q '^check_for_update_on_startup' "$codexrc"; then
  { printf 'check_for_update_on_startup = false\\n'; cat "$codexrc"; } > "$codexrc.tmp"
  cat "$codexrc.tmp" > "$codexrc" && rm -f "$codexrc.tmp"
fi
grep -q '^\\[mcp_servers\\.lmfdb\\]' "$codexrc" \\
  || printf '\\n[mcp_servers.lmfdb]\\nurl = "https://mcp.lmfdb.org/mcp"\\n' >> "$codexrc"
claude_bin=/home/agent/.local/bin/claude
bootlog=/var/log/zab-first-boot.log
if ! timeout 120 sudo -u agent -H "$claude_bin" mcp get lmfdb >>"$bootlog" 2>&1; then
  timeout 120 sudo -u agent -H "$claude_bin" mcp add --transport http --scope user lmfdb \\
    https://mcp.lmfdb.org/mcp >>"$bootlog" 2>&1 || echo "claude mcp add failed ($?)" >>"$bootlog"
fi
"""

#: The trees a fresh VM reads first, most needed first. A volume restored from the AMI's snapshot
#: fetches each block from S3 on its first read: on a fresh v2 instance the first `import Mathlib`
#: ran past 15 minutes, against seconds once read (build report on PR #79). The first boot reads
#: them once, in parallel, at the lowest CPU and I/O priority, in a transient systemd unit (so
#: that nothing of cloud-init's can kill it); VMs launch days before the homework.
WARM_PATHS = (
    "/home/agent/.elan",
    # the shared Mathlib (issue #205): ~/lean/workspace only links to it, and `find` doesn't
    # follow the link
    "/opt/lean",
    "/home/agent/lean",
    "/home/agent/.cache/mathlib",
    "/usr/share/texlive",
    "/usr/share/texmf",
    "/home/agent/miniforge",
    "/usr/local/magma",
    "/usr/local/share/pari",
    "/home/agent/.julia",  # AMI v3: Oscar's precompiled depot, then Julia itself
    "/opt/julia-*",
    # issue #474: the trees a first M2, R, Python or polymake session reads at start (the
    # packages they load), about 1.9 GB together; their binaries are single files, fetched fast
    "/usr/share/Macaulay2",
    "/usr/lib/x86_64-linux-gnu/Macaulay2",
    "/usr/lib/R",
    "/home/agent/venv",
    "/usr/share/polymake",
    "/usr/lib/polymake",
)
WARM_VOLUME = (
    f'for d in {" ".join(WARM_PATHS)}; do if [ -d "$d" ]; then find "$d" -type f -print0; fi; done'
    " | xargs -0 -r -P 32 -n 256 cat > /dev/null 2>&1"
)
WARM_FIRST_BOOT = f"""\
if [ ! -f /var/lib/zab-warm.done ]; then
  systemd-run --unit=zab-warm-volume --nice=19 -p IOSchedulingClass=best-effort \\
    -p IOSchedulingPriority=7 bash -c 'start=$SECONDS; {WARM_VOLUME}
    echo "volume warmed in $((SECONDS - start)) s" >> /var/log/zab-warm.log
    touch /var/lib/zab-warm.done' >>/var/log/zab-first-boot.log 2>&1
fi
"""
COMMON_FIRST_BOOT += WARM_FIRST_BOOT

#: The participant's first boot (workshop/ami/README.md, "First boot"). Participants get no SSH
#: (issue #36, Mon Oct 5: a mistyped GitHub login would have handed a stranger a shell with sudo);
#: organizers keep `ubuntu` with their own keys, and the roster's GitHub columns play no part: git
#: commits as the roster names and e-mail. Participants reach their work through their repository
#: (`provision_repos.py`).
PARTICIPANT_USER_DATA = """#!/bin/bash
# zab workshop first boot (workshop/ami/README.md). No `set -e`: every step runs even when an
# earlier one fails.
set -uo pipefail
hostnamectl set-hostname ws-{slug}
{admin_keys}sudo -u agent git config --global user.name {full_name}
sudo -u agent git config --global user.email {git_email}
install -d -o agent -g agent /home/agent/work
echo {participant_json} > /etc/zab-participant.json
# swap, CLI update pins and the LMFDB MCP server (decisions after the image, idempotent)
{common}exit 0
"""

#: A spare VM (ORGANIZERS §7.2), handed over with `push_config.py --adopt-spare`.
SPARE_USER_DATA = """#!/bin/bash
# zab workshop spare VM first boot (workshop/ami/README.md): no participant yet, no key import.
set -uo pipefail
hostnamectl set-hostname ws-{name}
{admin_keys}install -d -o agent -g agent /home/agent/work
echo {participant_json} > /etc/zab-participant.json
# swap, CLI update pins and the LMFDB MCP server (decisions after the image, idempotent)
{common}exit 0
"""


#: `launch.py --admin-key FILE` (PM on issue #36, Mon 07:10): more organizer keys for `ubuntu`,
#: next to the EC2 key pair's, on participant VMs and spares alike. Appended at first boot unless
#: already present; every key is validated (`parse_public_key`) and shell-quoted.
ADMIN_KEYS = """# organizers' extra keys for ubuntu (launch.py --admin-key)
install -d -m 0700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
ak=/home/ubuntu/.ssh/authorized_keys
[ -f "$ak" ] || install -m 0600 -o ubuntu -g ubuntu /dev/null "$ak"
for key in {keys}; do
  if ! grep -qxF -- "$key" "$ak"; then
    if [ -s "$ak" ] && [ -n "$(tail -c 1 "$ak")" ]; then echo >> "$ak"; fi
    printf '%s\\n' "$key" >> "$ak"
  fi
done
"""
#: OpenSSH public key types `authorized_keys` accepts for an organizer (no certificates), with
#: the fields of their key blob after the type name (RFC 4253 §6.6, RFC 5656 §3.1, RFC 8709 §4,
#: OpenSSH PROTOCOL.u2f): "mpint", "ed25519" (a 32-byte string), "point:N" (an N-byte EC point),
#: "curve:NAME" (a string equal to NAME), "string" (any non-empty string).
PUBKEY_FIELDS: dict[str, tuple[str, ...]] = {
    "ssh-ed25519": ("ed25519",),
    "ssh-rsa": ("mpint", "mpint"),
    "ecdsa-sha2-nistp256": ("curve:nistp256", "point:65"),
    "ecdsa-sha2-nistp384": ("curve:nistp384", "point:97"),
    "ecdsa-sha2-nistp521": ("curve:nistp521", "point:133"),
    "sk-ssh-ed25519@openssh.com": ("ed25519", "string"),
    "sk-ecdsa-sha2-nistp256@openssh.com": ("curve:nistp256", "point:65", "string"),
}
PUBKEY_TYPES = tuple(PUBKEY_FIELDS)
#: The smallest RSA modulus OpenSSH accepts (`RequiredRSASize`, 1024 bits by default since 9.1).
MIN_RSA_BITS = 1024


def _ssh_strings(blob: bytes) -> list[bytes]:
    """The blob as a sequence of SSH `string`s; ValueError unless it splits exactly."""
    out, pos = [], 0
    while pos < len(blob):
        if pos + 4 > len(blob):
            raise ValueError("truncated")
        size = int.from_bytes(blob[pos : pos + 4], "big")
        pos += 4
        if size == 0 or pos + size > len(blob):
            raise ValueError("truncated")
        out.append(blob[pos : pos + size])
        pos += size
    return out


def _check_key_blob(key_type: str, blob: bytes) -> None:
    """Every field the key type needs, of the right size, and nothing else (codex on #144: a
    truncated key file must not reach the fleet)."""
    fields = _ssh_strings(blob)
    spec = PUBKEY_FIELDS[key_type]
    if not fields or fields[0] != key_type.encode() or len(fields) != len(spec) + 1:
        raise ValueError(f"not a complete {key_type} key")
    for kind, value in zip(spec, fields[1:], strict=True):
        if kind == "ed25519" and len(value) != 32:
            raise ValueError("an ed25519 key is 32 bytes")
        if kind.startswith("curve:") and value != kind[len("curve:") :].encode():
            raise ValueError(f"the curve is not {kind[len('curve:') :]}")
        if kind.startswith("point:") and (len(value) != int(kind[6:]) or value[0] != 4):
            raise ValueError("not an uncompressed EC point of the right size")
    if key_type == "ssh-rsa" and int.from_bytes(fields[2], "big").bit_length() < MIN_RSA_BITS:
        raise ValueError(f"an RSA key needs at least {MIN_RSA_BITS} bits")


#: A key comment: printable ASCII, spaces included (`Drew's laptop`); it is shell-quoted in the
#: first-boot script and lands on one `authorized_keys` line, so no control characters.
_COMMENT_RE = re.compile(r"^[\x20-\x7e]{0,100}$")


def parse_public_key(text: str, source: str = "key") -> str:
    """The one OpenSSH public key in `text` (a `.pub` file), as `type base64 [comment]`.

    Refuses anything else: no key or several, `authorized_keys` options, a private key, an
    unknown type, base64 that does not decode to a complete key blob of that type (every field
    present and of its size, as `ssh-keygen` would read it), or a comment that is not printable
    ASCII of at most 100 characters. A comment may contain spaces, as `ssh-keygen -C` allows
    (PM nit on #144): runs of whitespace become one space, and the key is shell-quoted where it
    lands in the first-boot script.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if len(lines) != 1:
        raise ValueError(f"{source}: expected exactly one public key line, found {len(lines)}")
    parts = lines[0].split(maxsplit=2)
    if len(parts) < 2 or parts[0] not in PUBKEY_TYPES:
        raise ValueError(f"{source}: not an OpenSSH public key (type {parts[0][:40]!r})")
    if len(parts) == 3:
        parts[2] = " ".join(parts[2].split())
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError as exc:
        raise ValueError(f"{source}: the key is not valid base64") from exc
    try:
        _check_key_blob(parts[0], blob)
    except ValueError as exc:
        raise ValueError(f"{source}: the key data is not a {parts[0]} key ({exc})") from exc
    if len(parts) == 3 and not _COMMENT_RE.match(parts[2]):
        raise ValueError(
            f"{source}: the key comment {parts[2][:40]!r} is not printable ASCII of at most "
            "100 characters"
        )
    return " ".join(parts)


def admin_keys_script(keys: list[str] | tuple[str, ...]) -> str:
    """The first-boot step that appends `keys` (each from `parse_public_key`) for `ubuntu`."""
    if not keys:
        return ""
    return ADMIN_KEYS.format(keys=" ".join(shlex.quote(parse_public_key(k)) for k in keys))


def work_repo(row: dict[str, str]) -> str:
    """The checkout under `~/work/`: the roster's `repo` if given, else the slug (the
    repository `MathAgents/<slug>`, issue #36)."""
    return repo_name(row.get("repo", "")) or slug_of(row)


def git_identity(row: dict[str, str]) -> tuple[str, str]:
    """(`user.name`, `user.email`) of `agent`'s git: the row's `name` (its Zulip full name, which
    `roster_sync.py` records, #627), else its first and last names (a CSV's), else the slug; and
    the roster e-mail. Set at a participant's first boot, and by `push_config.py --adopt-spare`
    on a spare handed over (PM on issue #36, Wed 21:20: a spare's first boot sets none)."""
    first_last = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip()
    return (row.get("name") or "").strip() or first_last or slug_of(row), git_email(row)


def git_email(row: dict[str, str]) -> str:
    """The participant's commit address: the roster e-mail (issue #36, Mon Oct 5: GitHub
    logins play no part in the launch)."""
    if row.get("email"):
        return row["email"]
    raise ValueError(f"{slug_of(row)}: no e-mail for git's user.email")


def owner_record(row: dict[str, str]) -> str:
    """`/etc/zab-participant.json`: who the VM belongs to (push_config's owner check,
    workshop/admin/README.md §2 step 0) and the checkout `zab preflight` looks at. No GitHub
    login: none is imported (issue #36)."""
    return json.dumps({"slug": slug_of(row), "repo": work_repo(row)})


def render_user_data(row: dict[str, str], admin_keys: list[str] | tuple[str, ...] = ()) -> str:
    """The first-boot script for one row, every value shell-quoted (workshop/ami/README.md,
    "First boot")."""
    if is_spare(row):
        participant = json.dumps({"slug": "", "repo": "", "spare": slug_of(row)})
        return SPARE_USER_DATA.format(
            name=slug_of(row),
            admin_keys=admin_keys_script(admin_keys),
            participant_json=shlex.quote(participant),
            common=COMMON_FIRST_BOOT,
        )
    validate_row(row)
    full_name, email = git_identity(row)
    return PARTICIPANT_USER_DATA.format(
        slug=slug_of(row),  # validated: [a-z0-9-]
        admin_keys=admin_keys_script(admin_keys),
        git_email=shlex.quote(email),
        full_name=shlex.quote(full_name),
        participant_json=shlex.quote(owner_record(row)),
        common=COMMON_FIRST_BOOT,
    )


def ssh_config_snippet(row: dict[str, str]) -> str:
    """An `~/.ssh/config` block for the organizer: `ubuntu`, with the organizer's own key
    (participants get no SSH; issue #36)."""
    note = (
        "  # spare: hand over with `sudo -u agent ssh-import-id gh:<login>` on request"
        if is_spare(row)
        else ""
    )
    return f"Host ws-{slug_of(row)}{note}\n    HostName {row['ip']}\n    User ubuntu\n"


# ---------------------------------------------------------------------------- host keys

_FINGERPRINT_BLOCK_RE = re.compile(
    r"BEGIN SSH HOST KEY FINGERPRINTS-----(.*?)-----END SSH HOST KEY FINGERPRINTS", re.S
)


def console_host_keys(text: str) -> list[str]:
    """The `SHA256:…` host-key fingerprints cloud-init printed to the console at first boot,
    in its order, or [] while the block is not there (yet). Lines carry a syslog prefix
    (`<14>Oct  5 10:52:31 cloud-init: 256 SHA256:… root@ip-… (ED25519)` on AMI v3, read with
    `get_console_output(Latest=True)` on 2026-10-05); the block is printed once per instance."""
    block = _FINGERPRINT_BLOCK_RE.search(text)
    if not block:
        return []
    found = re.findall(r"SHA256:[A-Za-z0-9+/]+={0,2}", block.group(1))
    return list(dict.fromkeys(found))


# ------------------------------------------------------------------------- security group


def parse_cidr(text: str) -> str:
    """`203.0.113.7` or `203.0.113.0/24` → the network's canonical form (`203.0.113.7/32`)."""
    try:
        return str(ipaddress.ip_network(text.strip(), strict=False))
    except ValueError as exc:
        raise ValueError(f"not an IP address or CIDR block: {text!r}") from exc


def validate_security_group(sg: dict[str, Any], ssh_from: Iterable[str] = ()) -> None:
    """The group must not be the VPC default, and its ingress must be exactly TCP 22 (README.md).

    The default group admits no SSH from the internet (nobody could log in, and `wait_for_ssh`
    would wait 300 s per VM) while letting every VM reach every other, and the agents have sudo.
    With `ssh_from` (`launch.py --ssh-from`), TCP 22 must admit those CIDR blocks only: no other
    range, no other security group, no prefix list (issue #263: the test VM is reachable from
    the development machine alone).
    """
    allowed = {parse_cidr(c) for c in ssh_from}
    name = str(sg.get("GroupName", ""))
    if name == "default":
        raise ValueError(
            f"{sg.get('GroupId')} is the VPC default security group; create one for the workshop"
        )
    rules = sg.get("IpPermissions") or []
    if not rules:
        raise ValueError(f"{sg.get('GroupId')} ({name}) has no inbound rule; it needs TCP 22")
    for rule in rules:
        if (
            rule.get("IpProtocol") != "tcp"
            or rule.get("FromPort") != 22
            or rule.get("ToPort") != 22
        ):
            raise ValueError(
                f"{sg.get('GroupId')} ({name}) admits more than TCP 22 inbound: "
                f"{rule.get('IpProtocol')} {rule.get('FromPort')}-{rule.get('ToPort')}"
            )
        if not allowed:
            continue
        sources = [r.get("CidrIp", "") for r in rule.get("IpRanges") or []]
        sources += [r.get("CidrIpv6", "") for r in rule.get("Ipv6Ranges") or []]
        others = [str(g.get("GroupId", "?")) for g in rule.get("UserIdGroupPairs") or []]
        others += [str(p.get("PrefixListId", "?")) for p in rule.get("PrefixListIds") or []]
        others += [s for s in sources if parse_cidr(s) not in allowed]
        if others:
            raise ValueError(
                f"{sg.get('GroupId')} ({name}) admits SSH from {', '.join(others)}, not only "
                f"from {', '.join(sorted(allowed))}"
            )


# ------------------------------------------------------------------------------------ AWS

#: `Ec2.wait_terminated`: botocore's own `InstanceTerminated` waiter (every 15 s, 40 times),
#: made explicit so that its worst case, 600 s, is pinned where the bot's timeout is tested.
TERMINATED_WAITER = {"Delay": 15, "MaxAttempts": 40}


class Ec2:
    """The few EC2 calls the scripts make. Tests substitute a fake with the same methods."""

    def __init__(self, region: str, *, config: Mapping[str, Any] | None = None) -> None:
        """`config`: botocore `Config` arguments (timeouts, retries), for a script whose calls
        must give up soon (`power.EC2_CONFIG`); botocore's defaults without (60 s to connect
        and 60 s to read, 5 attempts)."""
        import boto3  # lazy: only the real client needs it

        options: dict[str, Any] = {}
        if config:
            from botocore.config import Config

            # a copy: botocore's client writes into `retries` (its `mode`), which Config keeps
            options["config"] = Config(**copy.deepcopy(dict(config)))
        self._ec2 = boto3.client("ec2", region_name=region, **options)
        self.region = region

    # --- queries

    def find_instances(
        self,
        tags: dict[str, str],
        states: tuple[str, ...],
        *,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> list[dict[str, Any]]:
        """Instances in `states` that carry every one of `tags`: one filter per tag (EC2 ANDs
        filters), checked again on the answer so that no scope tag is ever dropped (#263).

        `deadline` (on `clock`): ask for no page once it has passed, and raise TimeoutError
        instead, since a part of the scope would read as VMs gone (`power.settle`, #334). The
        first page is always asked for, and a last page past it is still a whole answer."""
        if not tags:
            raise ValueError("refusing an untagged lookup: it would match every instance")
        filters = [{"Name": f"tag:{k}", "Values": [v]} for k, v in tags.items()]
        filters.append({"Name": "instance-state-name", "Values": list(states)})
        pages = self._ec2.get_paginator("describe_instances").paginate(Filters=filters)
        found: list[dict[str, Any]] = []
        for page in pages:
            for res in page["Reservations"]:
                found.extend(i for i in res["Instances"] if has_tags(i, tags))
            if deadline is not None and page.get("NextToken") and clock() >= deadline:
                raise TimeoutError("the lookup's pages ran past the wait's deadline")
        return found

    def find_addresses(self, tags: dict[str, str]) -> list[dict[str, Any]]:
        """Elastic IPs that carry every one of `tags`, as `find_instances`."""
        if not tags:
            raise ValueError("refusing an untagged lookup: it would match every address")
        filters = [{"Name": f"tag:{k}", "Values": [v]} for k, v in tags.items()]
        found = self._ec2.describe_addresses(Filters=filters)["Addresses"]
        return [a for a in found if has_tags(a, tags)]

    def console_output(self, instance_id: str) -> str:
        """The latest console output (`Latest=True`: on Nitro instances the default, buffered
        output stays empty; build reports on PR #79 and #130). botocore decodes the base64."""
        res = self._ec2.get_console_output(InstanceId=instance_id, Latest=True)
        return str(res.get("Output") or "")

    def protection(self, instance_id: str) -> tuple[bool, str]:
        """(termination protection on, `InstanceInitiatedShutdownBehavior`) of one instance."""
        get = self._ec2.describe_instance_attribute
        dat = get(InstanceId=instance_id, Attribute="disableApiTermination")
        shut = get(InstanceId=instance_id, Attribute="instanceInitiatedShutdownBehavior")
        return (
            bool(dat["DisableApiTermination"]["Value"]),
            str(shut["InstanceInitiatedShutdownBehavior"]["Value"]),
        )

    def termination_protection(self, instance_id: str) -> bool:
        """Whether EC2 shows termination protection on, now: it may show a change minutes late
        (#378)."""
        get = self._ec2.describe_instance_attribute
        dat = get(InstanceId=instance_id, Attribute="disableApiTermination")
        return bool(dat["DisableApiTermination"]["Value"])

    def find_security_groups(
        self, tags: dict[str, str], name: str | None = None
    ) -> list[dict[str, Any]]:
        """Security groups that carry every one of `tags`, as `find_instances`; with `name`,
        only those of that name too."""
        if not tags:
            raise ValueError("refusing an untagged lookup: it would match every group")
        filters = [{"Name": f"tag:{k}", "Values": [v]} for k, v in tags.items()]
        if name is not None:
            filters.append({"Name": "group-name", "Values": [name]})
        found = self._ec2.describe_security_groups(Filters=filters)["SecurityGroups"]
        return [g for g in found if has_tags(g, tags) and name in (None, g.get("GroupName"))]

    def find_images(self, tags: dict[str, str]) -> list[dict[str, Any]]:
        """This account's available images that carry every one of `tags`."""
        if not tags:
            raise ValueError("refusing an untagged lookup: it would match every image")
        filters = [{"Name": f"tag:{k}", "Values": [v]} for k, v in tags.items()]
        filters.append({"Name": "state", "Values": ["available"]})
        found = self._ec2.describe_images(Owners=["self"], Filters=filters)["Images"]
        return [i for i in found if has_tags(i, tags)]

    def dry_run(self, call: str, **params: Any) -> str:
        """The error code of `call` with `DryRun=True`: `DryRunOperation` when the caller may
        make it, `UnauthorizedOperation` when its policy refuses it (ADMIN §2.9, V12)."""
        try:
            getattr(self._ec2, call)(DryRun=True, **params)
        except Exception as exc:  # botocore's ClientError, whose code says
            return error_code(exc) or type(exc).__name__
        return "no answer"  # a DryRun never succeeds: EC2 says what it would have done

    def volumes(self, volume_ids: list[str]) -> list[dict[str, Any]]:
        """The volumes of these IDs that exist: a filter, not VolumeIds, so that one gone is
        missing from the answer rather than an error."""
        filters = [{"Name": "volume-id", "Values": list(volume_ids)}]
        pages = self._ec2.get_paginator("describe_volumes").paginate(Filters=filters)
        return [v for page in pages for v in page["Volumes"]]

    def volume_modifications(self, volume_id: str) -> list[dict[str, Any]]:
        """The volume's latest modification, the one EC2 keeps, or none: by a filter, since
        `VolumeIds` makes a volume never modified an error."""
        filters = [{"Name": "volume-id", "Values": [volume_id]}]
        paginator = self._ec2.get_paginator("describe_volumes_modifications")
        pages = paginator.paginate(Filters=filters)
        return [m for page in pages for m in page["VolumesModifications"]]

    def describe_security_group(self, group_id: str) -> dict[str, Any]:
        groups = self._ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"]
        if not groups:
            raise ValueError(f"security group {group_id} not found")
        return groups[0]

    # --- mutations

    def run_instance(self, **params: Any) -> str:
        res = self._ec2.run_instances(MinCount=1, MaxCount=1, **params)
        return res["Instances"][0]["InstanceId"]

    def wait_running(self, instance_id: str) -> None:
        self._ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

    def start(self, instance_ids: list[str]) -> None:
        self._ec2.start_instances(InstanceIds=instance_ids)

    def stop(self, instance_ids: list[str]) -> None:
        self._ec2.stop_instances(InstanceIds=instance_ids)

    def allocate_address(self, tags: list[dict[str, str]]) -> str:
        res = self._ec2.allocate_address(
            Domain="vpc", TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": tags}]
        )
        return res["AllocationId"]

    def associate_address(self, allocation_id: str, instance_id: str) -> str:
        self._ec2.associate_address(AllocationId=allocation_id, InstanceId=instance_id)
        addr = self._ec2.describe_addresses(AllocationIds=[allocation_id])["Addresses"][0]
        return addr["PublicIp"]

    def terminate(self, instance_ids: list[str]) -> None:
        self._ec2.terminate_instances(InstanceIds=instance_ids)

    def wait_terminated(self, instance_ids: list[str]) -> None:
        """At most `TERMINATED_WAITER`'s `Delay` times `MaxAttempts` seconds (#378: the admin
        bot's timeout for `!teardown` covers it)."""
        waiter = self._ec2.get_waiter("instance_terminated")
        waiter.wait(InstanceIds=instance_ids, WaiterConfig=dict(TERMINATED_WAITER))

    def release_address(self, allocation_id: str) -> None:
        self._ec2.release_address(AllocationId=allocation_id)

    def set_termination_protection(self, instance_id: str, on: bool) -> None:
        self._ec2.modify_instance_attribute(
            InstanceId=instance_id, DisableApiTermination={"Value": on}
        )

    def set_shutdown_stop(self, instance_id: str) -> None:
        """An OS shutdown (`systemctl poweroff`) stops the instance rather than terminating it."""
        self._ec2.modify_instance_attribute(
            InstanceId=instance_id, InstanceInitiatedShutdownBehavior={"Value": "stop"}
        )

    def modify_volume(self, volume_id: str, size: int) -> None:
        """Grow a volume to `size` GB; its type, IOPS and throughput stay as they are."""
        self._ec2.modify_volume(VolumeId=volume_id, Size=size)

    def create_tags(self, resource_ids: list[str], tags: dict[str, str]) -> None:
        """Set (overwrite) these tags on instances, volumes or Elastic IP allocations."""
        self._ec2.create_tags(Resources=resource_ids, Tags=tag_list(tags))

    def key_pair(self, name: str) -> dict[str, Any] | None:
        """The key pair `name`, with its public key and tags, or None (a filter, not KeyNames,
        so that a missing key pair is an empty answer rather than an error)."""
        pairs = self._ec2.describe_key_pairs(
            Filters=[{"Name": "key-name", "Values": [name]}], IncludePublicKey=True
        )["KeyPairs"]
        return next((p for p in pairs if p.get("KeyName") == name), None)

    def import_key_pair(self, name: str, public_key: str, tags: dict[str, str]) -> None:
        """Import `public_key` as `name`, tagged at creation (the role's only way, ADMIN §5)."""
        self._ec2.import_key_pair(
            KeyName=name,
            PublicKeyMaterial=public_key.encode(),
            TagSpecifications=[{"ResourceType": "key-pair", "Tags": tag_list(tags)}],
        )


def parse_tag(spec: str) -> tuple[str, str]:
    """`KEY=VALUE` → (key, value), for the `--tag` option of both scripts."""
    if "=" not in spec:
        raise ValueError(f"expected KEY=VALUE, got {spec!r}")
    key, value = spec.split("=", 1)
    if not key or not value:
        raise ValueError(f"expected KEY=VALUE, got {spec!r}")
    return key, value


Scope = tuple[str, str] | Mapping[str, str]


def parse_tags(specs: str | Iterable[str] | None) -> dict[str, str]:
    """The scope from `--tag KEY=VALUE` (repeatable): every tag in it goes on every resource a
    script creates, and a lookup matches only resources that carry all of them (issue #263: the
    test VM is `zab-role=test` and `workshop=zab-test`). None or no spec is the default
    workshop tag; a single string is one spec (the option before it was repeatable). A key given
    twice with two values, and the per-participant keys `Name` and `participant`, are refused."""
    if specs is None:
        specs = []
    elif isinstance(specs, str):
        specs = [specs]
    scope: dict[str, str] = {}
    for spec in specs:
        key, value = parse_tag(spec)
        if key in RESERVED_TAG_KEYS:
            raise ValueError(f"--tag {key}=…: {key} is set per participant, not a scope tag")
        if scope.get(key, value) != value:
            raise ValueError(f"--tag {key} given twice: {scope[key]!r} and {value!r}")
        scope[key] = value
    return scope or {WORKSHOP_TAG[0]: WORKSHOP_TAG[1]}


def add_tag_argument(parser: Any) -> None:
    """The scripts' `--tag KEY=VALUE` option, repeatable; `parse_tags(opts.tag)` reads it."""
    parser.add_argument(
        "--tag",
        action="append",
        metavar="KEY=VALUE",
        help=f"workshop scope tag (repeatable: resources carry all of them, and lookups require "
        f"all of them; default {DEFAULT_TAG_SPEC})",
    )


def scope_tags(scope: Scope) -> dict[str, str]:
    """A scope as a dict: one `(key, value)` tag (the form before `--tag` was repeatable, still
    accepted) or a mapping of every scope tag. Never empty: that would match everything."""
    if isinstance(scope, Mapping):
        out = dict(scope)
    else:
        key, value = scope
        out = {key: value}
    if not out or not all(out.values()):
        raise ValueError("an empty scope would match every resource")
    return out


def scope_text(scope: Scope) -> str:
    """`workshop=nt-agents-2026` or `zab-role=test, workshop=zab-test`, for messages."""
    return ", ".join(f"{k}={v}" for k, v in scope_tags(scope).items())


def scope_label(scope: Scope) -> str:
    """The scope's name in `spares.json` keys: its tag's value when it has one tag (the keys
    written before `--tag` was repeatable), else every `key=value`, sorted and comma-joined.
    The admin server's `zab-scope` is left out: the same for all its workshops, it tells none
    apart, and the keys stay those written before it existed."""
    tags = scope_tags(scope)
    tags = {k: v for k, v in tags.items() if k != FLEET_SCOPE_KEY} or tags
    if len(tags) == 1:
        return next(iter(tags.values()))
    return ",".join(f"{k}={v}" for k, v in sorted(tags.items()))


def write_spares(path: str | os.PathLike[str], data: Mapping[str, Any]) -> None:
    """`spares.json` (`launch.SPARES_FILE`), written atomically, mode 0600, as `launch.py`,
    `adopt_spare.py` and `teardown.py` write it: a file beside it, then a rename."""
    target = os.fspath(path)
    tmp = f"{target.removesuffix('.json')}.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dict(data), f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def has_tags(resource: Mapping[str, Any], tags: Mapping[str, str]) -> bool:
    """True when the resource carries every one of `tags`, with its value."""
    have = {t["Key"]: t["Value"] for t in resource.get("Tags", [])}
    return all(have.get(k) == v for k, v in tags.items())


def tag_list(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def tags_of(resource: dict[str, Any]) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in resource.get("Tags", [])}


def has_participant(resource: Mapping[str, Any]) -> bool:
    """True for a participant's VM or a spare: a non-blank `participant` tag and no `zab-admin`
    tag. Nothing else in a workshop's scope may be selected for any action, even by the scope
    tags alone: the admin server, if someone tagged it `workshop=…` by mistake, has no
    `participant` tag, and even with one it keeps its own `zab-admin` tag (docs/ADMIN.md §1.1,
    §3.2). The role's deny covers the bot; this covers a manual run with another credential."""
    return not not_participant(resource)


def not_participant(resource: Mapping[str, Any]) -> str:
    """Why `has_participant` refuses the resource, or "". The `zab-admin` key in any case, as
    the admin bot's own rule has it (`zab.admin.starter.fleet_vm`), so that what a script may
    act on is never more than what the bot hashes for a plan (review of #354)."""
    tags = {t["Key"]: t["Value"] for t in resource.get("Tags", [])}
    if not tags.get("participant", "").strip():
        return "no participant tag"
    return "tagged zab-admin" if any(key.lower() == "zab-admin" for key in tags) else ""


def error_line(exc: BaseException) -> str:
    """`Type: first line` of an error, the text cut to 200 characters: what a script prints of a
    botocore error (an expired credential, `UnauthorizedOperation`) instead of a traceback."""
    lines = str(exc).splitlines()
    return f"{type(exc).__name__}: {lines[0][:200] if lines else ''}"


def error_code(exc: BaseException) -> str:
    """The AWS error code of a botocore `ClientError` (`OperationNotPermitted`, …), or ""."""
    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, dict) else None
    code = error.get("Code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else ""


def instance_ips(inst: Mapping[str, Any]) -> set[str]:
    """The public addresses of an instance: its Elastic IP stays listed while it is stopped (as
    `workshop/admin/status.py` reads them)."""
    ips = {inst.get("PublicIpAddress") or ""}
    for nic in inst.get("NetworkInterfaces") or []:
        ips.add((nic.get("Association") or {}).get("PublicIp") or "")
    return ips - {""}


def only_slugs(only: str | Iterable[str] | None) -> list[str]:
    """`--only SLUG` (repeatable) as a list without repeats, in order; a single string is one
    slug (the option before it was repeatable), None or nothing is no restriction."""
    if not only:
        return []
    return [only] if isinstance(only, str) else list(dict.fromkeys(only))


def wait_for_ssh(
    ip: str,
    *,
    timeout_s: float = 300.0,
    connect: Callable[[tuple[str, int], float], Any] = socket.create_connection,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """True once TCP port 22 accepts a connection, False after `timeout_s`."""
    deadline = clock() + timeout_s
    while clock() < deadline:
        if ssh_answers(ip, connect=connect):
            return True
        sleep(5.0)
    return False


def ssh_answers(
    ip: str, *, connect: Callable[[tuple[str, int], float], Any] = socket.create_connection
) -> bool:
    """One try: True if TCP port 22 accepts a connection within 5 s. TCP only, nothing sent."""
    try:
        with connect((ip, 22), 5.0):
            return True
    except OSError:
        return False
