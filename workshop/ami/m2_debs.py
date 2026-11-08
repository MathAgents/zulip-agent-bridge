"""The Macaulay2 packages of the workshop image: a GitHub release, pinned by SHA-256 (#945).
PARI's data packages come the same way (`PARI_DATA`, below).

The image's Macaulay2, gfan and msolve are the Macaulay2 team's PPA builds for noble. A PPA keeps
only its newest upload, and Launchpad stops serving a snapshot of it about 16 days after the PPA
publishes again (docs/DECISIONS.md, verification log, 2026-11-07). So the five .debs and the
source packages they were built from live in a GitHub release (tag `RELEASE`) of the repository
the build runs from (its `origin`, `default_release`), and `files/apt/macaulay2-debs.sha256`
pins every file's SHA-256. `build_ami.py` fetches them with the
deployer's `gh`, on the deployer's machine (`fetch`), and copies them to the builder; `build.sh`
checks them again and installs them. README.md, "Macaulay2's packages", has the whole procedure.

    python3 workshop/ami/m2_debs.py fetch [--set pari-data] [--release OWNER/REPO@TAG] DIR
    python3 workshop/ami/m2_debs.py capture --snapshot YYYYMMDDTHHMMSSZ DIR

`capture` takes a new set from one of Launchpad's snapshots of the PPA while it still serves it.
It checks InRelease against the committed key, the indexes against InRelease, and every file
against its index, then writes the files, `InRelease` and `SHA256SUMS` to DIR for a new release.
Standalone: the standard library, `git` (for the `origin`), `gh` for `fetch` and `gpg`
for `capture`.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "files" / "apt" / "macaulay2-debs.sha256"
KEY = HERE / "files" / "apt" / "macaulay2-ppa.asc"
#: The PPA's signing key, as Launchpad publishes it for the archive (issue #474): the committed
#: `files/apt/macaulay2-ppa.asc`, against whose signed index a new set is checked (README.md)
KEY_FPR = "5B17CD31DA995CFAA1B0D4FD08416FF271BC6AA8"
#: The pinned set's release tag, in the repository the build runs from (`default_release`);
#: `--deps-release OWNER/REPO@TAG` in build_ami.py names another. The manifest pins the bytes.
RELEASE = "deps/macaulay2-1.26.06+ds-5"
#: A GitHub remote, as `git remote get-url` gives it once its userinfo is gone
#: (`without_userinfo`): https, ssh:// or scp-like ssh
ORIGIN_RE = re.compile(
    r"(?:https://github\.com/|ssh://github\.com/|github\.com:)"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*?)(?:\.git)?/?"
)
#: Each part starts with a letter or digit, so that none reads as an option to `gh`
RELEASE_RE = re.compile(
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)"
    r"@(?P<tag>[A-Za-z0-9][A-Za-z0-9_.+/-]*)"
)
#: PARI/GP's five data packages (build.sh step 3), whose names on PARI's site carry no version
PARI_RELEASE = "deps/pari-data-20261107"
SNAPSHOTS = "https://snapshot.ppa.launchpadcontent.net/macaulay2/macaulay2/ubuntu"
BINARIES = ("macaulay2", "macaulay2-common", "gfan", "msolve", "libmsolve3")
SNAPSHOT_RE = re.compile(r"\d{8}T\d{6}Z")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+~_-]*")
LINE_RE = re.compile(r"(?P<sha>[0-9a-f]{64})  (?P<name>\S+)")


class DepsError(Exception):
    """A pinned file is missing or isn't the pinned one; the message says which and the fix."""


@dataclass(frozen=True)
class Pinned:
    """One set of files in a release: what the messages call it, its manifest, its default
    release, its README section, build_ami.py's options (`<option>-release`, `<option>-dir`),
    and its attribute (`opts.<attr>`, the tarball `<attr>.tar` with `_` as `-`, the knob
    `<ATTR>`)."""

    what: str
    manifest: Path
    release: str
    section: str
    option: str
    attr: str

    @property
    def tarball(self) -> str:
        return self.attr.replace("_", "-") + ".tar"

    @property
    def knob(self) -> str:
        return self.attr.upper()


M2 = Pinned(
    "the pinned Macaulay2 packages", MANIFEST, RELEASE, "Macaulay2's packages", "--deps", "m2_debs"
)
PARI_DATA = Pinned(
    "PARI's pinned data packages",
    HERE / "files" / "pari-data.sha256",
    PARI_RELEASE,
    "PARI's data packages",
    "--pari-data",
    "pari_data",
)
SETS = (M2, PARI_DATA)


def read_manifest(path: Path = MANIFEST) -> dict[str, str]:
    """Each file's name and SHA-256, in `sha256sum`'s format; anything else is refused."""
    pinned: dict[str, str] = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        found = LINE_RE.fullmatch(line)
        if not found or not NAME_RE.fullmatch(found["name"]) or found["name"] in pinned:
            raise DepsError(f"{path}:{number}: not '<sha256>  <file name>', or a name twice")
        pinned[found["name"]] = found["sha"]
    if not pinned:
        raise DepsError(f"{path} pins no file")
    return pinned


def asset_name(name: str) -> str:
    """The name GitHub gives a release asset uploaded as `name`: it stores each `~` (any
    character but letters, digits, `.`, `_`, `+` and `-`) as `.` (verification log, Nov 7)."""
    return re.sub(r"[^A-Za-z0-9._+-]", ".", name)


def package(name: str) -> str:
    """`macaulay2-common_1.26.06…_all.deb` → `macaulay2-common`, `elldata.tgz` → `elldata`."""
    return name.split("_", 1)[0].removesuffix(".tgz")


def parse_release(spec: str) -> tuple[str, str]:
    found = RELEASE_RE.fullmatch(spec)
    if not found:
        raise DepsError(f"{spec!r} is not OWNER/REPO@TAG")
    return found["repo"], found["tag"]


def without_userinfo(url: str) -> str:
    """`url` without its userinfo, everything up to the last `@` of the authority: a remote may
    carry a token (`https://x-access-token:<token>@github.com/o/r.git`, `https://<token>@…`),
    which must never be printed (review round 2 on #950). A scp-like `[user@]host:path` loses its
    user the same way."""
    scheme, sep, rest = url.partition("://")
    if sep:
        authority, slash, path = rest.partition("/")
        return scheme + sep + authority.rpartition("@")[2] + slash + path
    host, colon, path = url.partition(":")
    return host.rpartition("@")[2] + colon + path if colon else url


def origin_repo(
    run: Callable[..., subprocess.CompletedProcess] | None = None, flag: str = "--deps-release"
) -> str:
    """OWNER/REPO of the checkout's `origin` remote on GitHub, which holds the release by default
    (#950: the private development repository and its public copy each hold their own). The URL
    is matched, and shown, only without its userinfo."""
    argv = ["git", "-C", str(HERE), "remote", "get-url", "origin"]
    try:
        done = (run or subprocess.run)(
            argv, capture_output=True, text=True, timeout=30, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        done = None
    url = without_userinfo(done.stdout.strip()) if done is not None and done.returncode == 0 else ""
    found = ORIGIN_RE.fullmatch(url)
    if not found:
        raise DepsError(
            f"no GitHub origin to take the release from (git remote get-url origin, without any"
            f" credentials: {url!r}); pass {flag} OWNER/REPO@TAG"
        )
    return found["repo"]


def default_release(
    tag: str = RELEASE,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
    flag: str = "--deps-release",
) -> str:
    """`OWNER/REPO@TAG` for a tag of the release in the checkout's `origin`; `flag` is the option
    the message names when there is no GitHub origin."""
    return f"{origin_repo(run, flag)}@{tag}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def collect(
    source: Path, dest: Path, pinned: dict[str, str], where: str, kind: Pinned = M2
) -> None:
    """Every pinned file from `source` (by its own name or its asset name) into `dest` under its
    own name, checked against its SHA-256; one message for all that are missing or wrong."""
    problems = []
    for name, want in pinned.items():
        found = next((p for p in (source / name, source / asset_name(name)) if p.is_file()), None)
        if found is None:
            problems.append(f"  {name} ({package(name)}): missing")
        elif (got := sha256(found)) != want:
            problems.append(f"  {name} ({package(name)}): sha256 {got}, pinned {want}")
        elif found != dest / name:
            shutil.copyfile(found, dest / name)
    if problems:
        raise DepsError(
            f"{kind.what} from {where} don't match {kind.manifest.name}:\n"
            + "\n".join(problems)
            + "\nRestore the release's files from a copy that matches the manifest, or pin a new"
            f' set (workshop/ami/README.md, "{kind.section}").'
        )


def fetch(
    spec: str,
    dest: Path,
    pinned: dict[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    kind: Pinned = M2,
) -> list[Path]:
    """Download release `spec` with the deployer's `gh` and leave the pinned files in `dest`,
    each checked; returns their paths. Raises DepsError, naming the files and the fix."""
    pinned = read_manifest(kind.manifest) if pinned is None else pinned
    repo, tag = parse_release(spec)
    with tempfile.TemporaryDirectory(prefix="zab-m2-debs.") as raw:
        argv = ["gh", "release", "download", tag, "--repo", repo, "--dir", raw]
        for name in pinned:
            argv += ["--pattern", asset_name(name)]
        try:
            done = run(argv, capture_output=True, text=True, timeout=1800, check=False)
        except FileNotFoundError:
            raise DepsError(
                f"{kind.what} come from a GitHub release, fetched with `gh`, which isn't"
                f" installed here (https://cli.github.com), or pass {kind.option}-dir"
            ) from None
        except subprocess.TimeoutExpired:
            raise DepsError(
                f"`gh release download {tag}` took over 30 minutes; try again"
            ) from None
        if done.returncode != 0:
            raise DepsError(
                f"`gh release download {tag} --repo {repo}` failed ({done.returncode}):"
                f" {done.stderr.strip()[-500:]}\nCheck `gh auth status` and that you can read"
                f" {repo}, or pass {kind.option}-release or {kind.option}-dir"
                f' (workshop/ami/README.md, "{kind.section}").'
            )
        collect(Path(raw), dest, pinned, f"release {spec}", kind)
    return [dest / name for name in pinned]


def from_dir(
    source: Path, dest: Path, pinned: dict[str, str] | None = None, kind: Pinned = M2
) -> list[Path]:
    """The pinned files from a local copy of the release (`--deps-dir`), each checked."""
    pinned = read_manifest(kind.manifest) if pinned is None else pinned
    collect(source, dest, pinned, str(source), kind)
    return [dest / name for name in pinned]


def stanzas(text: str) -> list[dict[str, str]]:
    """A Debian index (Packages, Sources, Release) as its paragraphs' fields."""
    out = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        key = ""
        for line in block.splitlines():
            if line.startswith(" ") and key:
                fields[key] += "\n" + line.strip()
            elif ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        if fields:
            out.append(fields)
    return out


def checksums(field: str) -> dict[str, str]:
    """A `SHA256:` or `Checksums-Sha256:` field: name → sha256."""
    sums = {}
    for row in field.splitlines():
        if row.strip():
            sha, _size, name = row.split()
            sums[name] = sha
    return sums


def source_of(fields: dict[str, str]) -> tuple[str, str]:
    """A binary package's source package and version: its `Source:` field, `name` or
    `name (version)` (Debian Policy §5.6.1), falling back to its own Package and Version."""
    field = fields.get("Source", "")
    found = re.fullmatch(r"([a-z0-9][a-z0-9.+-]*)(?: \(([A-Za-z0-9.+~:-]+)\))?", field)
    if field and not found:
        raise DepsError(
            f"{fields.get('Package')}: a Source field {field!r} that isn't name (version)"
        )
    name = found.group(1) if found else fields.get("Package", "")
    version = found.group(2) if found and found.group(2) else fields.get("Version", "")
    return name, version


def gpg_verify(signed: bytes, key: Path = KEY) -> str:
    """The text of a clearsigned InRelease, once gpg, given `key` alone, found a valid signature
    by the PPA's key (`KEY_FPR`, the primary key's fingerprint on gpg's VALIDSIG line)."""
    if not key.is_file():
        raise DepsError(f"no key file at {key}: capture checks InRelease with the committed key")
    with tempfile.TemporaryDirectory() as home:
        imported = ["gpg", "-q", "--homedir", home, "--import", str(key)]
        try:
            subprocess.run(imported, check=True, capture_output=True)  # noqa: S603 - no shell
            done = subprocess.run(  # noqa: S603 - an argument list, no shell
                ["gpg", "--homedir", home, "--status-fd", "2", "--decrypt"],  # noqa: S607
                input=signed,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            raise DepsError("capture checks InRelease with gpg, which isn't installed") from None
        except subprocess.CalledProcessError as exc:
            reason = exc.stderr.decode(errors="replace").strip()[-200:] if exc.stderr else ""
            raise DepsError(f"gpg couldn't import {key}: {reason}") from None
    status = done.stderr.decode(errors="replace")
    valid = re.search(rf"^\[GNUPG:\] VALIDSIG \S+ .* {KEY_FPR}$", status, re.M)
    if done.returncode != 0 or not valid:
        raise DepsError(f"InRelease isn't validly signed by {KEY_FPR}: {status.strip()[-300:]}")
    return done.stdout.decode()


def capture(
    snapshot: str,
    dest: Path,
    get: Callable[[str], bytes] | None = None,
    verify: Callable[[bytes], str] = gpg_verify,
) -> dict[str, str]:
    """The five .debs and the source packages they were built from (each binary's `Source:` and
    version, `source_of`) from Launchpad's snapshot at `snapshot`, each checked against the signed
    index, into `dest` with `InRelease` and `SHA256SUMS`; nothing is written unless all are."""
    if not SNAPSHOT_RE.fullmatch(snapshot):
        raise DepsError(f"{snapshot!r} is not a snapshot time YYYYMMDDTHHMMSSZ")
    if dest.exists() and any(dest.iterdir()):  # its every file goes into the release (README.md)
        raise DepsError(f"{dest} isn't empty: capture into a new directory")
    if get is None:

        def get(url: str) -> bytes:
            with urllib.request.urlopen(url, timeout=300) as r:  # noqa: S310 - a fixed https base
                return r.read()

    base = f"{SNAPSHOTS}/{snapshot}"
    inrelease = get(f"{base}/dists/noble/InRelease")
    release = stanzas(verify(inrelease))[0]
    if release.get("Codename") != "noble":  # as apt checks it: a signed index of another series
        raise DepsError(f"InRelease is for {release.get('Codename')!r}, not noble")
    listed = checksums(release.get("SHA256", ""))
    index = {}
    for name in ("main/binary-amd64/Packages.gz", "main/source/Sources.gz"):
        data = get(f"{base}/dists/noble/{name}")
        if hashlib.sha256(data).hexdigest() != listed.get(name):
            raise DepsError(f"{name} doesn't match the signed InRelease")
        index[name] = gzip.decompress(data).decode()
    wanted: dict[str, tuple[str, str]] = {}  # name → (sha256, pool path)
    needed: set[tuple[str, str]] = set()  # the binaries' (source, version)
    binaries = [
        f for f in stanzas(index["main/binary-amd64/Packages.gz"]) if f.get("Package") in BINARIES
    ]
    names = sorted(f["Package"] for f in binaries)
    if names != sorted(BINARIES):
        raise DepsError(f"the snapshot lists {names}, not each of {sorted(BINARIES)} once")
    for fields in binaries:
        wanted[fields["Filename"].rsplit("/", 1)[1]] = (fields["SHA256"], fields["Filename"])
        needed.add(source_of(fields))
    # Each binary's source, by name and version, listed exactly once: a snapshot taken during an
    # upload may list a source newer than the binaries, or none (Codex round 1 on #951).
    sources = stanzas(index["main/source/Sources.gz"])
    for source, version in sorted(needed):
        found = [f for f in sources if (f.get("Package"), f.get("Version")) == (source, version)]
        if len(found) != 1:
            listed = [f.get("Version") for f in sources if f.get("Package") == source]
            raise DepsError(
                f"the binaries were built from source {source} {version}, which the snapshot's"
                f" Sources lists {len(found)} times (its {source}: {listed or 'none'})"
            )
        for name, sha in checksums(found[0].get("Checksums-Sha256", "")).items():
            wanted[name] = (sha, f"{found[0]['Directory']}/{name}")
    files: dict[str, bytes] = {}  # all checked before anything is written
    for name, (sha, path) in sorted(wanted.items()):
        if not NAME_RE.fullmatch(name):
            raise DepsError(f"{name!r}: not a plain file name")
        data = get(f"{base}/{path}")
        if hashlib.sha256(data).hexdigest() != sha:
            raise DepsError(f"{name} doesn't match its index")
        files[name] = data
    dest.mkdir(parents=True, exist_ok=True)
    pinned = {}
    for name, data in files.items():
        (dest / name).write_bytes(data)
        pinned[name] = wanted[name][0]
    (dest / "InRelease").write_bytes(inrelease)
    (dest / "SHA256SUMS").write_text("".join(f"{s}  {n}\n" for n, s in pinned.items()))
    return pinned


def release_tag(pinned: dict[str, str]) -> str:
    """The release's tag for a captured set: macaulay2's source version, without the PPA's
    `~ubuntu…` suffix (`deps/macaulay2-1.26.06+ds-5`)."""
    dsc = next(n for n in pinned if n.startswith("macaulay2_") and n.endswith(".dsc"))
    return "deps/macaulay2-" + dsc.removeprefix("macaulay2_").removesuffix(".dsc").split("~")[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help="the pinned set from its release, checked")
    f.add_argument("--set", choices=("macaulay2", "pari-data"), default="macaulay2")
    f.add_argument(
        "--release", help="OWNER/REPO@TAG (default: the set's tag, of the origin's repository)"
    )
    f.add_argument("dir", type=Path)
    c = sub.add_parser("capture", help="a new set from a Launchpad snapshot, for a new release")
    c.add_argument("--snapshot", required=True, help="UTC time, YYYYMMDDTHHMMSSZ")
    c.add_argument("dir", type=Path)
    opts = parser.parse_args(argv)
    try:
        if opts.command == "fetch":
            opts.dir.mkdir(parents=True, exist_ok=True)
            kind = PARI_DATA if opts.set == "pari-data" else M2
            release = opts.release or default_release(kind.release, flag="--release")
            for path in fetch(release, opts.dir, kind=kind):
                print(path)
            return 0
        pinned = capture(opts.snapshot, opts.dir)
    except DepsError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"captured {len(pinned)} files in {opts.dir}; the manifest, {MANIFEST.name}:")
    sys.stdout.write("".join(f"{s}  {n}\n" for n, s in pinned.items()))
    tag = release_tag(pinned)
    print(
        "\nthen: gh release create "
        f"'{tag}' --latest=false --notes-file <provenance> {opts.dir}/*  (README.md)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
