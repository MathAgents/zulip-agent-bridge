#!/usr/bin/env python3
"""A VM's `apt-get update` repaired: the expired Macaulay2 snapshot source removed (#947).

    sudo /usr/bin/python3 apt_repair.py [--sources PATH] [--apt-mark CMD] [--dpkg-query CMD]

An image built with the Macaulay2 PPA pinned to a Launchpad snapshot (`build.sh`'s step 1d, the
Oct 19 image's among them) leaves `/etc/apt/sources.list.d/macaulay2.sources` naming
`https://snapshot.ppa.launchpadcontent.net/…/<time>`. Launchpad expires such snapshots, and an
expired one makes every `apt-get update` exit 100 ("no longer has a Release file"), so any
`apt-get update && apt-get install …` on the VM fails (#945).

Every release rollout of `push_config.py` runs this as root, with only the standard library, and
so does `trust_fix.py` before its `apt-get update` (`trust_fix_remote.step_apt`). It
looks at that one file (`--sources`), read as deb822 (comments and other fields ignored), and acts
only on what it recognises exactly:

- a regular file whose sources are that snapshot's and nothing else's (every `URIs:` value in
  it on that host, and one at least in a stanza not `Enabled: no`): the installed ones of
  macaulay2, macaulay2-common, gfan, msolve and libmsolve3 held (`apt-mark hold`), so that a
  source added later can't change them unasked, then the file removed. Their packages stay as
  they are. A hold that fails, on dpkg's lock say, or a `dpkg-query` that can't read dpkg's
  database keeps the file, so that the next rollout tries both again, and exits 1, which the
  rollout records;
- no file there, or one whose live sources don't name the snapshot (the PPA itself, say, with
  the snapshot's line commented out): nothing to do ("nothing to repair");
- a file that names the snapshot beside other sources, a link or any other kind of file, or a
  file too large to be a sources file: left alone, but said ("kept"), since apt may still fail
  there and only a person can tell what the file is for.

So a rerun changes nothing once it has acted. It prints one line, `apt: …`, which the rollout
notes unless it says "nothing to repair"; it exits 1, with a line saying why, when something
fails, the file kept unless the line says otherwise. Every program runs from an argument list,
never a shell.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import urllib.parse

SOURCES = "/etc/apt/sources.list.d/macaulay2.sources"
#: What marks the expiring source: Launchpad's snapshots of a PPA.
SNAPSHOT_HOST = "snapshot.ppa.launchpadcontent.net"
#: The packages `build.sh` installs from that source, held once it is gone.
PACKAGES = ("macaulay2", "macaulay2-common", "gfan", "msolve", "libmsolve3")
LIMIT = 65536  # a sources file is a few lines: more than this is no source of ours
#: deb822's `Enabled:` values that turn a stanza off (apt's spellings of a false boolean).
OFF = ("no", "false", "off", "without", "disable", "0")


def installed(dpkg_query: str, package: str) -> bool:
    """Whether dpkg has `package` installed, held or not (`ii`, `hi`), by dpkg-query's own status.
    dpkg-query exits 0 too for a package it knows but hasn't installed (`un`, `rc`): the status's
    second letter tells. It exits 1 for a package it doesn't know, and 2 for a fatal error, its
    database unreadable or malformed, say (dpkg-query(1), EXIT STATUS): anything but 0 or 1
    raises, so that nothing is removed unheld (#949's Codex round 2), and `main` says why."""
    done = subprocess.run(  # noqa: S603 - an argument list
        [dpkg_query, "-W", "-f=${db:Status-Abbrev}", package],
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    if done.returncode not in (0, 1):
        why = " ".join(done.stderr.split()) or "no message"
        raise subprocess.SubprocessError(f"dpkg-query {package}: exit {done.returncode}: {why}")
    return done.returncode == 0 and done.stdout[1:2] == "i"


def stanzas(text: str) -> list[dict[str, str]]:
    """A deb822 file's stanzas, each as {field, lower-cased: value}, by apt's rules: a line ends
    at "\\n" alone, less a trailing "\\r"; comment lines dropped; a folded line, one of blanks
    included, joined to its field; only an empty line between stanzas."""
    found: list[dict[str, str]] = []
    fields: dict[str, str] = {}
    name = ""
    for line in text.split("\n"):  # never splitlines(), which also breaks at \v, \f, \x85…
        line = line.removesuffix("\r")
        if line.startswith("#"):
            continue
        if not line:
            if fields:
                found.append(fields)
            fields, name = {}, ""
        elif line[:1] in (" ", "\t"):
            if name:
                fields[name] += " " + line.strip()
        else:
            key, colon, value = line.partition(":")
            name = key.strip().lower() if colon else ""
            if name:
                fields[name] = value.strip()
    if fields:
        found.append(fields)
    return found


def host(uri: str) -> str | None:
    """A URI's host, lower-cased; None for one without a host, or one malformed."""
    try:
        return urllib.parse.urlsplit(uri).hostname
    except ValueError:  # an unclosed "[", say: no source apt could use either
        return None


def hosts(group: list[dict[str, str]]) -> set[str | None]:
    """The hosts of the stanzas' `URIs:` values."""
    return {host(uri) for one in group for uri in one.get("uris", "").split()}


def repair(sources: str, apt_mark: str, dpkg_query: str) -> tuple[int, str]:
    """The exit status and the line to print (module doc)."""
    try:
        found = os.lstat(sources)
    except FileNotFoundError:
        return 0, "apt: nothing to repair"
    odd = f"apt: kept {sources}: not a regular file, so apt may still fail there; look at it"
    if not stat.S_ISREG(found.st_mode):
        return 0, odd
    # never through a link swapped in since, never waiting on a FIFO
    fd = os.open(sources, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):  # on the descriptor: fdopen refuses a directory
        os.close(fd)
        return 0, odd
    with os.fdopen(fd, "rb") as f:
        data = f.read(LIMIT + 1)
    if len(data) > LIMIT:
        return 0, f"apt: kept {sources}: over {LIMIT} bytes, so no sources file of the image's"
    every = stanzas(data.decode("utf-8", "replace"))
    live = [one for one in every if one.get("enabled", "yes").lower() not in OFF]
    if SNAPSHOT_HOST not in hosts(live):
        return 0, f"apt: nothing to repair: {sources} has no live {SNAPSHOT_HOST} source"
    if hosts(every) != {SNAPSHOT_HOST}:
        return 0, (
            f"apt: kept {sources}: it names other sources beside the expired {SNAPSHOT_HOST} "
            "one, so apt still fails there; fix it by hand"
        )
    # the holds first, the source's removal only once they hold (#949's Codex round 1): a hold
    # that fails, on dpkg's lock say, leaves the source, so the next rollout tries both again
    held = [package for package in PACKAGES if installed(dpkg_query, package)]
    if held:
        done = subprocess.run(  # noqa: S603 - an argument list
            [apt_mark, "hold", *held], capture_output=True, text=True, check=False, timeout=120
        )
        if done.returncode != 0:
            why = (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1]
            return 1, f"apt: hold failed, so {sources} is kept for the next rollout: {why}"
    try:
        os.unlink(sources)
    except OSError as exc:
        return 1, f"apt: {sources} not removed: {exc.strerror}"
    holds = f"held {', '.join(held)}" if held else "none of its packages installed"
    return 0, f"apt: removed {sources}, an expired {SNAPSHOT_HOST} source; {holds}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apt_repair.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--sources", default=SOURCES, help=f"the file to look at ({SOURCES})")
    parser.add_argument("--apt-mark", default="/usr/bin/apt-mark", help=argparse.SUPPRESS)
    parser.add_argument("--dpkg-query", default="/usr/bin/dpkg-query", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        code, line = repair(args.sources, args.apt_mark, args.dpkg_query)
    except (OSError, subprocess.SubprocessError) as exc:
        # a program missing or cut off, the file unreadable: all before the removal, so it stays
        why = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        code, line = 1, f"apt: {args.sources} kept for the next rollout: {why}"
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
