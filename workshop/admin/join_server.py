#!/usr/bin/env python3
"""Give the admin server's fleet key to VMs launched before it, or take it back (docs/ADMIN.md
§8.4, §1.7).

    uv run python workshop/admin/join_server.py --roster ROSTER [--keep keep.txt | --only SLUG …] \\
        --identity ~/.ssh/zab-deployer --server-key zab-admin-fleet.pub \\
        [--known-hosts workshop/admin/out/known_hosts] [--parallel 8] [--remove] [--dry-run]

A VM gets the server's fleet key at launch, as its EC2 key pair (ADMIN §5). One launched before
the server, such as the kept `nt-agents-2026` fleet of Oct 9, has only the keys it got then,
the deployers' break-glass keys among them (`launch.py --admin-key`). A deployer runs this from
their laptop. It reaches each roster VM as `ubuntu` through the pinned host keys, with a
break-glass key (`--identity`) as the only identity: `-F /dev/null` leaves out the deployer's
ssh config, and `IdentitiesOnly` the agent's other keys. ssh runs in batch mode, several at
once, so a passphrase-protected identity, such as the deployer's dedicated key (docs/BOOTSTRAP.md
§2.1), must be in the agent first. It then appends the server's public key to
`~ubuntu/.ssh/authorized_keys` unless that key is there already, whatever its comment. It runs
on the laptop, not on the server, because the server never holds a break-glass key (§1.7) and
its own key reaches none of these VMs yet. A replacement server's key joins the same way (§1.7).
Any `ssh-ed25519` public key may stand for the server's: the kept fleet's cutover moved it from
the old break-glass keys to the dedicated one with three runs (ADMIN §8.4 at v0.2.15, O5, "The
join"), one adding it over an old key, then one `--remove` of each old key over it.

`--remove` takes the server's key off again, for a server lost or compromised (§1.7), as the kept
fleet's fallback did (ADMIN §8.4 at v0.2.15): every line whose key fields are that key, whatever
options come before them or comment after them (never a mention inside an option or a comment),
and says how many. It refuses to remove the key it connects with, which it reads from the
identity itself (`ssh-keygen -y`) or its `.pub`, and refuses when it can read neither.

The key goes on stdin, and the remote program is a fixed text (CONTRIBUTING.md, rule 3). Both
directions are idempotent. `--dry-run` connects and says what would change. A row without an
`ip` is skipped. A stopped VM fails to connect: start it (`!start` through the bot needs no
SSH) and run again.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import shlex
import sys
from pathlib import Path

from admin_common import (
    DEFAULT_KNOWN_HOSTS,
    first_line,
    format_table,
    host_of,
    load_roster,
    read_keep,
    run_parallel,
    select_rows,
    slug_of,
    ssh_options,
    subprocess_runner,
)
from aws_common import parse_public_key

#: Runs on the VM as `ubuntu`, under `python3 -I -c`; argv[1] is `check`, `add`, `count` or
#: `remove`, and the key line comes on stdin. Adding, a line is the key when its first two
#: fields are the key's type and base64: a line with options before the key restricts it, so it
#: doesn't count, and the plain key is appended. Removing, a line goes when its key fields are
#: the key's, whatever options come before them or comment after them; text inside an option
#: or a comment never counts. The options end as OpenSSH's `sshkey_advance_past_options` ends
#: them: at the first blank outside double quotes, where `\"` is an escaped quote and any other
#: backslash an ordinary character; unterminated quotes make the line no key. `count` is its dry
#: run, and the number removed is printed. Lines are split at `\n` alone, as sshd splits them
#: (never at a lone `\r`, `\x0c` or `\x85`, as `splitlines` would), and the lines kept are
#: written back byte for byte. A line's fields are split at blanks and tabs only, and the
#: base64 is compared without the whitespace `b64_pton` skips (`\v`, `\f`, `\r`, …), so such a
#: character can't hide the key.
REMOTE = """\
import os, re, sys
key = sys.stdin.read().strip()
kind, blob = key.split()[:2]
path = os.path.expanduser("~/.ssh/authorized_keys")
try:
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        text = f.read()
except FileNotFoundError:
    text = ""
lines = [part + "\\n" for part in text.split("\\n")]
lines[-1] = lines[-1][:-1]
plain = any(line.split()[:2] == [kind, blob] for line in lines)
def key_of(line):
    line = line.lstrip(" \\t")
    if not line.strip() or line.startswith("#"):
        return None
    fields = re.split("[ \\t]+", line)
    if not fields[0].startswith(("ssh-", "ecdsa-", "sk-")):
        quoted, i = False, 0
        while i < len(line) and (quoted or line[i] not in " \\t"):
            if line[i] == "\\\\" and line[i + 1:i + 2] == '"':
                i += 1
            elif line[i] == '"':
                quoted = not quoted
            i += 1
        if quoted:
            return None
        fields = re.split("[ \\t]+", line[i:].lstrip(" \\t"))
    if len(fields) < 2:
        return None
    return [fields[0], re.sub("[ \\t\\n\\v\\f\\r]", "", fields[1])]
held = [key_of(line) == [kind, blob] for line in lines]
mode = sys.argv[1]
if mode == "check":
    print("present" if plain else "absent")
elif mode == "count":
    print("held", sum(held))
elif mode == "remove":
    if any(held):
        new = path + ".zab-new"
        fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write("".join(line for line, gone in zip(lines, held) if not gone))
        os.replace(new, path)
    print("removed", sum(held))
elif plain:
    print("present")
else:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(("\\n" if text and not text.endswith("\\n") else "") + key + "\\n")
    print("added")
"""


def verdict(words: list[str], *, dry_run: bool, remove: bool) -> str | None:
    """The row's result from the remote program's output, or None when it is unexpected."""
    if remove:
        tag = "held" if dry_run else "removed"
        if len(words) != 2 or words[0] != tag or not words[1].isdigit():
            return None
        count = int(words[1])
        if count == 0:
            return "ok: not there"
        return f"would remove {count}" if dry_run else f"ok: removed {count}"
    found = {"present": "ok: already there"}
    found.update({"absent": "would add"} if dry_run else {"added": "ok: added"})
    return found.get(words[0]) if len(words) == 1 else None


def own_key(identity: str, runner) -> list[str] | None:
    """The type and base64 of `--identity`'s public half: derived from the private key
    (`ssh-keygen -y`, which works unencrypted), else read from its `.pub`; None when neither
    can be read, so that `--remove` can't take off the key it connects with unknowingly."""
    result = runner(["ssh-keygen", "-y", "-P", "", "-f", identity], timeout_s=30.0)
    text = result.stdout if result.ok else ""
    pub = Path(identity + ".pub")
    if not text.strip() and pub.is_file():
        text = pub.read_text()
    words = text.split()
    return words[:2] if len(words) >= 2 else None


def fingerprint(key: str) -> str:
    """`SHA256:…`, as `ssh-keygen -lf` prints it, to compare with the server's own key."""
    digest = hashlib.sha256(base64.b64decode(key.split()[1])).digest()
    return "SHA256:" + base64.b64encode(digest).rstrip(b"=").decode("ascii")


def join_argv(
    host: str, identity: str, known_hosts: str, *, dry_run: bool, remove: bool = False
) -> list[str]:
    mode = ("count" if remove else "check") if dry_run else "remove" if remove else "add"
    remote = shlex.join(["python3", "-I", "-c", REMOTE, mode])
    options = ssh_options(known_hosts, identity=identity)
    return ["ssh", "-F", "/dev/null", *options, f"ubuntu@{host}", "--", remote]


def why_failed(stderr: str, stdout: str, code: int) -> str:
    """One line a deployer can act on. OpenSSH frames its two likeliest refusals here in a
    banner of `@`, which says nothing on its own."""
    if "Host key verification failed" in stderr or "REMOTE HOST IDENTIFICATION" in stderr:
        return "the VM's host key is not the pinned one (Host key verification failed)"
    if "UNPROTECTED PRIVATE KEY FILE" in stderr:
        return "the identity file's permissions are too open: chmod 600 it"
    lines = [line for line in stderr.splitlines() if line.strip().strip("@")]
    return first_line("\n".join(lines)) or first_line(stdout) or f"exit {code}"


def join(
    row, key: str, identity: str, known_hosts: str, *, dry_run: bool, remove: bool, runner
) -> list[str]:
    slug = slug_of(row)
    if not (row.get("ip") or "").strip():
        return [slug, "", "skip: no VM in the roster"]
    host = host_of(row)
    argv = join_argv(host, identity, known_hosts, dry_run=dry_run, remove=remove)
    result = runner(argv, timeout_s=90.0, stdin=key + "\n")
    found = verdict(result.stdout.split(), dry_run=dry_run, remove=remove)
    if result.ok and found:
        return [slug, host, found]
    return [slug, host, f"FAILED: {why_failed(result.stderr, result.stdout, result.exit_code)}"]


def main(argv: list[str] | None = None, *, runner=subprocess_runner) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--roster", required=True)
    parser.add_argument("--identity", required=True, help="the break-glass private key file")
    parser.add_argument(
        "--server-key",
        required=True,
        help="the public key to add or remove (the server's fleet key, or a break-glass key)",
    )
    parser.add_argument("--known-hosts", default=str(DEFAULT_KNOWN_HOSTS))
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--only", action="append", metavar="SLUG")
    which.add_argument("--keep", metavar="FILE", help="the kept slugs, one per line")
    parser.add_argument("--parallel", type=int, default=8)
    parser.add_argument("--remove", action="store_true", help="take the key off")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        identity = os.path.expanduser(args.identity)
        if identity.endswith(".pub") or not Path(identity).is_file():
            raise ValueError(f"--identity: expected a private key file, got {args.identity}")
        key = parse_public_key(Path(args.server_key).read_text(), "--server-key")
        if not key.startswith("ssh-ed25519 "):
            raise ValueError("--server-key: the key must be ssh-ed25519")
        if args.remove:
            own = own_key(identity, runner)
            if own is None:
                raise ValueError("--remove: --identity's public key can't be read: refused")
            if own == key.split()[:2]:
                raise ValueError("--remove: that is the key this run connects with: refused")
        only = read_keep(args.keep) if args.keep else args.only
        rows = select_rows(load_roster(args.roster), only)
        if not 1 <= args.parallel <= 32:
            raise ValueError("--parallel takes 1 to 32")
    except (ValueError, OSError) as exc:
        print(f"join refused: {exc}", file=sys.stderr)
        return 2
    what = "remove" if args.remove else "add"
    print(f"{'DRY RUN: ' if args.dry_run else ''}{what} the key {fingerprint(key)}")
    table = run_parallel(
        rows,
        lambda row: join(
            row,
            key,
            identity,
            args.known_hosts,
            dry_run=args.dry_run,
            remove=args.remove,
            runner=runner,
        ),
        parallel=args.parallel,
        on_error=lambda row, exc: [slug_of(row), "", f"FAILED: {first_line(str(exc))}"],
    )
    print(format_table(["slug", "ip", "result"], table))
    failed = sum(row[2].startswith("FAILED") for row in table)
    print(f"{len(table) - failed} of {len(table)} ok" + (f", {failed} FAILED" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
