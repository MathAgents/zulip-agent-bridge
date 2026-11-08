#!/usr/bin/env python3
"""`keep_until.py --roster R --set SLUG=VALUE [--set SLUG=VALUE …] [--dry-run]` (docs/ADMIN.md
§11.5; #570).

Each named row's `keep_until`, the end of its kept VM: the admin bot's `!keep-until`. VALUE is a
date, `2026-11-30`, whose end is at `KEEP_UNTIL_HOUR` (15:00) UTC that day; a date and a time in
UTC, `2026-11-30T15:00`; or `none`, which clears it. Each row prints one line, its value before
and after, and the end in words. A dry run that changes nothing exits 3 (`NOTHING`), which the
bot answers with no plan (#708).

This script checks each value's form and date, and that each slug is a row of the roster, named
once. The bot checks the rest, at its plan and again at its apply: that the end is in the
future and within a year, and that the row is neither retired nor excluded. By hand, check those
yourself.

**Refused, nothing written:** a slug not in the roster, a slug named twice or a value of another
form (exit 2); a roster that changed while the script ran (exit 1). The roster is written
atomically, with a `.bak` (`write_roster`). It is personal data: never committed.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from admin_common import (
    KEEP_UNTIL_HOUR,
    keep_until_value,
    load_roster,
    participants,
    slug_of,
    write_roster,
)

COLUMN = "keep_until"
NOTHING = 3  # a dry run that changes no row: the admin bot's `actions.NOTHING` (#708)


def end_words(value: str) -> str:
    """The end a `keep_until` value names, as the bot's plans say it:
    `Monday, November 30, 2026, 15:00 UTC`."""
    day, _, at = value.partition("T")
    year, month, mday = (int(x) for x in day.split("-"))
    hour, minute = (int(x) for x in at.split(":")) if at else (KEEP_UNTIL_HOUR, 0)
    t = time.gmtime(calendar.timegm((year, month, mday, hour, minute, 0)))
    return time.strftime(f"%A, %B {t.tm_mday}, %Y, %H:%M UTC", t)


def parse_sets(pairs: list[str], slugs: set[str]) -> dict[str, str]:
    """`--set` pairs → slug → its new value, "" for `none`; ValueError for anything else."""
    out: dict[str, str] = {}
    for pair in pairs:
        slug, equals, text = pair.partition("=")
        if not equals or slug not in slugs:
            raise ValueError(f"{slug or pair!r}: not a row of the roster")
        if slug in out:
            raise ValueError(f"{slug}: named twice")
        value = "" if text == "none" else keep_until_value(text)
        if value is None:
            raise ValueError(
                f"{slug}: {text!r} is not a date like 2026-11-30, or 2026-11-30T15:00 (UTC), "
                "or none"
            )
        out[slug] = value
    return out


def line(slug: str, before: str, after: str) -> str:
    if before == after:
        return f"{slug}: keep_until {before or '(none)'}, unchanged"
    end = f" (its VM ends {end_words(after)})" if after else " (no end)"
    return f"{slug}: keep_until {before or '(none)'} → {after or '(none)'}{end}"


def digest(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="keep_until.py", description="Set each row's keep_until (docs/ADMIN.md §11.5)."
    )
    parser.add_argument("--roster", required=True)
    parser.add_argument(
        "--set",
        action="append",
        required=True,
        metavar="SLUG=VALUE",
        help="a row and its end: 2026-11-30, 2026-11-30T15:00 (UTC) or none (repeatable)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    args = parser.parse_args(argv)
    try:
        before = digest(args.roster)
        rows = load_roster(args.roster)
        sets = parse_sets(args.set, {slug_of(row) for row in participants(rows)})
    except (OSError, ValueError, csv.Error) as exc:
        print(f"refused, nothing written: {exc}", file=sys.stderr)
        return 2
    changed = 0
    for row in participants(rows):
        slug = slug_of(row)
        if slug in sets:
            old = row.get(COLUMN, "")
            print(line(slug, old, sets[slug]))
            changed += old != sets[slug]
            row[COLUMN] = sets[slug]
    if not changed:
        print("nothing to change: each row already has that keep_until")
        return NOTHING if args.dry_run else 0
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    try:
        if digest(args.roster) != before:
            print("refused: the roster changed while this ran; nothing written", file=sys.stderr)
            return 1
        write_roster(args.roster, rows)
    except OSError as exc:
        print(f"roster NOT written: {exc}", file=sys.stderr)
        return 1
    print(f"written: {args.roster} (the previous one is {args.roster}.bak)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
