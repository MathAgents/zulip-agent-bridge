#!/usr/bin/env python3
"""Stage a fleet's roster and keys for the admin server, its kept rows only (docs/ADMIN.md §8.4).

    uv run python workshop/admin/stage_server.py --roster roster.csv [--keep keep.txt] \\
        --to STAGE [--out-dir workshop/admin/out] \\
        [--config /etc/zab-admin/admin.toml --workshop NAME --retired FILE]

A fleet run from a laptop, such as the kept `nt-agents-2026` VMs of Oct 9, moves to the admin
server with its state, which the server's scripts read from `ws/<name>/` (§1.3):

- `STAGE/roster.csv`: the rows `--keep` names (one slug per line; `#` starts a comment; without
  it, every row), every column as the roster has it. Only those: a row torn down keeps its old
  `ip`, which may be another tenant's address by now, and a bot's `all` means every roster row;
- `STAGE/out/<slug>/{codex,claude}.zuliprc`: those rows' bot keys, as `zulip_provision.py`
  wrote them; the other participants' keys stay where they are;
- `STAGE/out/known_hosts`: the pinned host keys (public).

**On the server,** as `zab-admin`, it runs again on that stage, with `--to` the workshop's own
`ws/<name>/`, and with `--config`, `--workshop` and `--retired`. A slug is unique on the server
for its whole life (§2.1), so it then refuses a row whose slug another workshop of `admin.toml`
has in its roster, or a retired workshop had, as `!roster import` refuses it
(`roster_import.taken_slugs`).

STAGE must not exist: it is made 0700, and every file in it 0600. A kept slug that the roster
lacks, a row without a VM, a slug taken on the server, or a key file that is missing or a
symbolic link refuses the whole run, which then writes nothing. Nothing is printed from the keys.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
import tomllib

from admin_common import keys_dir, load_roster, read_keep, select_rows, slug_of, write_roster
from roster_import import taken_refusal, taken_slugs
from zulip_provision import BOT_KINDS, OUT_DIR


def taken_on_server(config: str, workshop: str, retired: str | None) -> dict[str, str]:
    """Slug -> the other workshop of `config` whose roster has it, or "" for a retired one. A
    workshop whose roster doesn't exist yet takes no slug, as for `!roster import`."""
    entries = tomllib.loads(pathlib.Path(config).read_text(encoding="utf-8")).get("workshop", [])
    entries = [w for w in entries if isinstance(w, dict)]
    if workshop not in [w.get("name") for w in entries]:
        raise ValueError(f"--workshop {workshop}: not a workshop of {config}")
    others = [
        f"{w['name']}={w['roster']}"
        for w in entries
        if w.get("name") != workshop and os.path.isfile(str(w.get("roster")))
    ]
    return taken_slugs(retired, others)


def stage(
    roster: str,
    keep: str | None,
    out_dir: pathlib.Path,
    to: pathlib.Path,
    taken: dict[str, str] | None = None,
) -> int:
    rows = select_rows(load_roster(roster), read_keep(keep) if keep else None)
    if not rows:
        raise ValueError(f"{roster}: no participant rows")
    refusals = [taken_refusal(s, taken[s]) for s in map(slug_of, rows) if s in (taken or {})]
    if refusals:
        raise ValueError("; ".join(refusals))
    copies = [(out_dir / "known_hosts", "out/known_hosts")]
    for row in rows:
        slug = slug_of(row)
        if not (row.get("ip") or "").strip():
            raise ValueError(f"{slug}: no VM in the roster (no ip)")
        for name in (f"{kind}.zuliprc" for kind in BOT_KINDS):
            copies.append((keys_dir(out_dir, row)[0] / name, f"out/{slug}/{name}"))
    for source, _ in copies:
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"missing regular file {source}")
    umask = os.umask(0o077)
    try:
        to.mkdir()  # refuses an existing STAGE
        write_roster(str(to / "roster.csv"), rows)
        for source, name in copies:
            (to / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, to / name)
    finally:
        os.umask(umask)
    print(f"staged {len(rows)} rows, {2 * len(rows)} bot keys and known_hosts in {to}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--roster", required=True)
    parser.add_argument("--keep", help="the kept slugs, one per line; without it, every row")
    parser.add_argument("--to", required=True, type=pathlib.Path, help="a new directory")
    parser.add_argument("--out-dir", type=pathlib.Path, default=OUT_DIR)
    server = parser.add_argument_group("on the server, as zab-admin (§2.1: slugs unique)")
    server.add_argument("--config", help="admin.toml: every other workshop's roster")
    server.add_argument("--workshop", help="this fleet's name in admin.toml")
    server.add_argument("--retired", help="the server's retired-slugs.txt")
    args = parser.parse_args(argv)
    if bool(args.config) != bool(args.workshop) or (args.retired and not args.config):
        parser.error("--config and --workshop go together, and --retired with them")
    try:
        taken = taken_on_server(args.config, args.workshop, args.retired) if args.config else None
        return stage(args.roster, args.keep, args.out_dir, args.to, taken)
    except (ValueError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"stage refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
