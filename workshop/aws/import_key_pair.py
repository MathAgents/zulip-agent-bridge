"""Import the admin server's fleet key as a workshop's EC2 key pair (docs/ADMIN.md §1.6, §5).

    python3 workshop/aws/import_key_pair.py --region us-east-2 --key-name zab-admin-harvard-2026 \\
        --public-key /var/lib/zab-admin/.ssh/id_ed25519.pub --tag workshop=harvard-2026 [--dry-run]

`deploy.sh --bootstrap` runs it on the admin server, as `zab-admin` with the instance role, once
per workshop of admin.toml. The key pair carries the workshop's scope tags from its creation:
the role may import a key pair only so tagged, and launch only with one so tagged.

Idempotent: a key pair of that name with the same key and the scope tags is left alone. One with
another key, or without the tags, is refused, never replaced: replacing a fleet's key pair is a
deployer's decision, made by hand. `--dry-run` reads and prints, and changes nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aws_common import Ec2, add_tag_argument, has_tags, parse_public_key, parse_tags, scope_text


def import_key_pair(
    ec2: Any,
    name: str,
    public_key: str,
    tags: dict[str, str],
    *,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> int:
    key = " ".join(parse_public_key(public_key, "the fleet key").split()[:2])  # no comment
    if not key.startswith("ssh-ed25519 "):
        log(f"{name}: the fleet key must be ssh-ed25519")
        return 1
    existing = ec2.key_pair(name)
    if existing is not None:
        theirs = " ".join((existing.get("PublicKey") or "").split()[:2])
        if theirs != key:
            log(f"{name}: exists with another key; replace it by hand if that is intended")
            return 1
        if not has_tags(existing, tags):
            log(f"{name}: exists without {scope_text(tags)}, so the role can't launch with it")
            return 1
        log(f"{name}: present, with this key and {scope_text(tags)}")
        return 0
    if dry_run:
        log(f"{name}: would import, tagged {scope_text(tags)}")
        return 0
    ec2.import_key_pair(name, key, tags)
    log(f"{name}: imported, tagged {scope_text(tags)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the fleet key as a workshop's key pair.")
    parser.add_argument("--region", required=True)
    parser.add_argument("--key-name", required=True)
    parser.add_argument("--public-key", required=True, type=Path)
    add_tag_argument(parser)
    parser.add_argument("--dry-run", action="store_true")
    opts = parser.parse_args(argv)  # --tag is required (`add_tag_argument`)
    try:
        tags = parse_tags(opts.tag)
        text = opts.public_key.read_text(encoding="ascii")
        return import_key_pair(Ec2(opts.region), opts.key_name, text, tags, dry_run=opts.dry_run)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
