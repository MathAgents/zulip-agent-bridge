"""`workshop/ami/m2_debs.py` (#945): the manifest of the pinned Macaulay2 packages, fetching them
from their release with `gh` (a fake one here), and capturing a new set from a Launchpad snapshot
(a fake one here; the real fetch and capture are recorded in docs/DECISIONS.md)."""

from __future__ import annotations

import gzip
import hashlib
import re
import subprocess
from pathlib import Path

import m2_debs
import pytest


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: A small pinned set: three files, one with `~` in its name, as GitHub renames it
FILES = {
    "macaulay2_1.0+ds-5~ubuntu24.04.1_amd64.deb": b"m2 deb",
    "gfan_0.8~beta-2~ubuntu24.04.1.dsc": b"gfan dsc",
    "msolve_0.9.4.orig.tar.gz": b"msolve orig",
}
PINNED = {name: sha(data) for name, data in FILES.items()}


def test_the_committed_manifest_pins_fourteen_files_by_sha256():
    pinned = m2_debs.read_manifest()
    assert len(pinned) == 14 and len([n for n in pinned if n.endswith(".deb")]) == 5
    assert all(len(s) == 64 for s in pinned.values())


@pytest.mark.parametrize(
    "text",
    [
        "",
        f"{'0' * 64} a.deb\n",  # one space
        f"{'0' * 64}  ../a.deb\n",  # a path, not a name
        f"{'0' * 64}  a.deb\n{'1' * 64}  a.deb\n",  # twice
        f"{'G' * 64}  a.deb\n",
        "# a comment\n",
    ],
)
def test_a_manifest_line_that_isnt_sha256sums_is_refused(tmp_path, text):
    path = tmp_path / "m.sha256"
    path.write_text(text)
    with pytest.raises(m2_debs.DepsError):
        m2_debs.read_manifest(path)


def test_asset_names_are_githubs_and_releases_are_owner_repo_at_tag():
    assert m2_debs.asset_name("gfan_0.8~beta-2~ubuntu24.04.1_amd64.deb") == (
        "gfan_0.8.beta-2.ubuntu24.04.1_amd64.deb"
    )
    assert m2_debs.asset_name("macaulay2_1.26.06+ds.orig.tar.xz") == (
        "macaulay2_1.26.06+ds.orig.tar.xz"
    )
    assert m2_debs.RELEASE == "deps/macaulay2-1.26.06+ds-5"  # a tag: the repository is origin's
    assert m2_debs.parse_release("o/r@" + m2_debs.RELEASE) == ("o/r", m2_debs.RELEASE)
    for bad in ("repo@tag", "o/r", "o/r@", "o/r@t;rm", "o/r@t x", "-o/r@t", "o/-r@t", "o/r@-t"):
        with pytest.raises(m2_debs.DepsError):
            m2_debs.parse_release(bad)


class FakeGh:
    """`gh release download TAG --repo R --dir D --pattern …`: writes the release's assets."""

    def __init__(self, assets: dict[str, bytes], code: int = 0) -> None:
        self.assets, self.code, self.argv = assets, code, []

    def __call__(self, argv, **kw):
        self.argv = argv
        out = Path(argv[argv.index("--dir") + 1])
        for name, data in self.assets.items():
            (out / name).write_bytes(data)
        return subprocess.CompletedProcess(argv, self.code, "", "HTTP 404: release not found")


def test_fetch_takes_the_assets_by_their_github_names_and_checks_each(tmp_path):
    gh = FakeGh({m2_debs.asset_name(n): d for n, d in FILES.items()} | {"InRelease": b"x"})
    paths = m2_debs.fetch("o/r@deps/x", tmp_path, PINNED, run=gh)
    assert [p.name for p in paths] == list(FILES)
    assert all(p.read_bytes() == FILES[p.name] for p in paths)
    assert gh.argv[:7] == ["gh", "release", "download", "deps/x", "--repo", "o/r", "--dir"]
    patterns = [gh.argv[i + 1] for i, a in enumerate(gh.argv) if a == "--pattern"]
    assert patterns == [m2_debs.asset_name(n) for n in FILES]
    assert not (tmp_path / "InRelease").exists()


def test_fetch_names_each_file_missing_or_changed_and_the_fix(tmp_path):
    assets = {m2_debs.asset_name(n): d for n, d in FILES.items()}
    assets["gfan_0.8.beta-2.ubuntu24.04.1.dsc"] = b"another gfan"
    del assets["msolve_0.9.4.orig.tar.gz"]
    with pytest.raises(m2_debs.DepsError) as err:
        m2_debs.fetch("o/r@deps/x", tmp_path, PINNED, run=FakeGh(assets))
    text = str(err.value)
    assert "release o/r@deps/x" in text and "macaulay2-debs.sha256" in text
    assert f"gfan_0.8~beta-2~ubuntu24.04.1.dsc (gfan): sha256 {sha(b'another gfan')}" in text
    assert "msolve_0.9.4.orig.tar.gz (msolve): missing" in text
    assert "macaulay2_" not in text  # the good one isn't named
    assert '"Macaulay2\'s packages"' in text  # where the fix is


def test_fetch_says_how_to_get_gh_working(tmp_path):
    with pytest.raises(m2_debs.DepsError, match="gh auth status") as err:
        m2_debs.fetch("o/r@deps/x", tmp_path, PINNED, run=FakeGh({}, code=1))
    assert "HTTP 404" in str(err.value) and "--deps-dir" in str(err.value)

    def no_gh(argv, **kw):
        raise FileNotFoundError("gh")

    with pytest.raises(m2_debs.DepsError, match="isn't installed"):
        m2_debs.fetch("o/r@deps/x", tmp_path, PINNED, run=no_gh)


def test_a_local_copy_may_use_either_name(tmp_path):
    copy, out = tmp_path / "copy", tmp_path / "out"
    copy.mkdir()
    out.mkdir()
    names = list(FILES)
    (copy / names[0]).write_bytes(FILES[names[0]])  # its own name
    for name in names[1:]:
        (copy / m2_debs.asset_name(name)).write_bytes(FILES[name])  # as downloaded
    paths = m2_debs.from_dir(copy, out, PINNED)
    assert [p.read_bytes() for p in paths] == list(FILES.values())


def git_remote(url: str, code: int = 0):
    """`git remote get-url origin`, faked."""

    def run(argv, **kw):
        assert argv[:2] == ["git", "-C"] and argv[3:] == ["remote", "get-url", "origin"]
        return subprocess.CompletedProcess(argv, code, url + "\n", "error: No such remote")

    return run


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/someone/zab",
        "https://github.com/someone/zab.git",
        "https://github.com/someone/zab/",
        "git@github.com:someone/zab.git",
        "ssh://git@github.com/someone/zab.git",
        "https://ghp_FAKETOKEN0123@github.com/someone/zab.git",  # review round 2 on #950
        "https://x-access-token:ghp_FAKETOKEN0123@github.com/someone/zab.git",
        "ssh://user:ghp_FAKETOKEN0123@github.com/someone/zab",
    ],
)
def test_the_default_release_is_in_the_checkouts_origin(url):
    """Review of #950: the release's OWNER/REPO is the checkout's own GitHub origin, so the
    private repository and its public copy each name their own; nothing is hard-coded."""
    assert m2_debs.default_release(run=git_remote(url)) == f"someone/zab@{m2_debs.RELEASE}"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("", 2),  # no origin
        ("https://gitlab.com/someone/zab.git", 0),
        ("https://github.com/-someone/zab", 0),
        ("https://github.com/someone/zab/extra", 0),
        ("file:///srv/zab", 0),
    ],
)
def test_without_a_github_origin_the_release_must_be_named(url, code):
    with pytest.raises(m2_debs.DepsError, match="pass --deps-release OWNER/REPO@TAG"):
        m2_debs.default_release(run=git_remote(url, code))

    def no_git(argv, **kw):
        raise FileNotFoundError("git")

    with pytest.raises(m2_debs.DepsError, match="no GitHub origin"):
        m2_debs.default_release(run=no_git)


@pytest.mark.parametrize(
    "url",
    [
        "https://ghp_FAKETOKEN0123@gitlab.com/someone/zab.git",
        "https://x-access-token:ghp_FAKETOKEN0123@github.com/someone/zab/extra",
        "ssh://ghp_FAKETOKEN0123@example.org/zab",
        "ghp_FAKETOKEN0123@example.org:zab",
    ],
)
def test_a_refused_origin_never_shows_its_credentials(url):
    """Review round 2 on #950: the message shows the origin without its userinfo."""
    with pytest.raises(m2_debs.DepsError) as err:
        m2_debs.default_release(run=git_remote(url))
    assert "FAKETOKEN" not in str(err.value) and "pass --deps-release" in str(err.value)


# ------------------------------------------------------------------------------ capture


#: The binaries' Source fields, as the PPA's Packages gives them (none: the binary's own name)
SOURCE_OF = {"macaulay2-common": "macaulay2", "libmsolve3": "msolve"}


def snapshot_files(
    *, drop: str = "", corrupt: str = "", versions: dict[str, list[str]] | None = None
) -> dict[str, bytes]:
    """A fake snapshot's URLs: the five binaries (1.0~u1), the source packages (`versions`, each
    source's listed versions, 1.0~u1 by default), the indexes and an InRelease (its signature
    checked by a fake `verify`, which returns the signed text)."""
    pool, packages, sources = {}, [], []
    for name in m2_debs.BINARIES:
        if name == drop:
            continue
        deb = f"{name}_1.0~u1_amd64.deb"
        pool[f"pool/main/x/{name}/{deb}"] = f"{name} deb".encode()
        source = f"Source: {SOURCE_OF[name]}\n" if name in SOURCE_OF else ""
        packages.append(
            f"Package: {name}\n{source}Version: 1.0~u1\nFilename: pool/main/x/{name}/{deb}\n"
            f"SHA256: {sha(pool[f'pool/main/x/{name}/{deb}'])}\n"
        )
    versions = versions or {n: ["1.0~u1"] for n in ("macaulay2", "gfan", "msolve")}
    for name, listed in versions.items():
        for version in listed:
            rows = []
            for file in (f"{name}_{version}.dsc", f"{name}_1.0.orig.tar.gz"):
                pool[f"pool/main/x/{name}/{file}"] = f"{file} bytes".encode()
                rows.append(f" {sha(pool[f'pool/main/x/{name}/{file}'])} 9 {file}")
            sources.append(
                f"Package: {name}\nVersion: {version}\nDirectory: pool/main/x/{name}\n"
                "Checksums-Sha256:\n" + "\n".join(rows)
            )
    if corrupt:
        pool[corrupt] = b"tampered"
    indexes = {
        "main/binary-amd64/Packages.gz": gzip.compress("\n".join(packages).encode()),
        "main/source/Sources.gz": gzip.compress("\n\n".join(sources).encode()),
    }
    release = "Origin: LP-PPA-macaulay2\nCodename: noble\nSHA256:\n" + "\n".join(
        f" {sha(data)} {len(data)} {name}" for name, data in indexes.items()
    )
    urls = {f"dists/noble/{n}": d for n, d in indexes.items()} | pool
    urls["dists/noble/InRelease"] = release.encode()
    return urls


def fake_get(urls: dict[str, bytes]):
    base = f"{m2_debs.SNAPSHOTS}/20261106T000000Z/"

    def get(url: str) -> bytes:
        return urls[url.removeprefix(base)]

    return get


def test_capture_checks_the_signed_index_and_every_file(tmp_path):
    urls = snapshot_files()
    pinned = m2_debs.capture(
        "20261106T000000Z", tmp_path, get=fake_get(urls), verify=lambda b: b.decode()
    )
    assert len(pinned) == 5 + 6
    assert (tmp_path / "InRelease").read_bytes() == urls["dists/noble/InRelease"]
    sums = (tmp_path / "SHA256SUMS").read_text()
    assert f"{sha(b'gfan deb')}  gfan_1.0~u1_amd64.deb\n" in sums
    assert (tmp_path / "msolve_1.0~u1.dsc").read_bytes() == b"msolve_1.0~u1.dsc bytes"


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"corrupt": "pool/main/x/gfan/gfan_1.0~u1_amd64.deb"}, "doesn't match its index"),
        ({"drop": "libmsolve3"}, "not each of"),
    ],
)
def test_capture_refuses_a_file_or_a_set_that_isnt_the_indexs(tmp_path, kw, message):
    with pytest.raises(m2_debs.DepsError, match=message):
        m2_debs.capture(
            "20261106T000000Z",
            tmp_path / "out",
            get=fake_get(snapshot_files(**kw)),
            verify=bytes.decode,
        )
    assert not (tmp_path / "out").exists()  # nothing written, the files checked before it


def test_capture_refuses_an_index_the_signature_doesnt_cover_and_a_bad_time(tmp_path):
    urls = snapshot_files()
    urls["dists/noble/main/source/Sources.gz"] = gzip.compress(b"Package: other\n")
    with pytest.raises(m2_debs.DepsError, match="doesn't match the signed InRelease"):
        m2_debs.capture("20261106T000000Z", tmp_path, get=fake_get(urls), verify=bytes.decode)

    def unsigned(data: bytes) -> str:
        raise m2_debs.DepsError("InRelease isn't validly signed")

    with pytest.raises(m2_debs.DepsError, match="validly signed"):
        m2_debs.capture("20261106T000000Z", tmp_path, get=fake_get(urls), verify=unsigned)
    with pytest.raises(m2_debs.DepsError, match="snapshot time"):
        m2_debs.capture("2026-11-06", tmp_path, get=fake_get(urls))


#: gpg's status line for a good signature by a subkey `SUB` of the primary key that ends it
VALID = "[GNUPG:] VALIDSIG SUB 2026-10-25 0 4 0 1 10 01 {}\n"


@pytest.mark.parametrize(
    ("code", "status", "ok"),
    [
        (0, "[GNUPG:] GOODSIG X\n" + VALID.format(m2_debs.KEY_FPR), True),
        (0, VALID.format("A" * 40), False),
        (1, VALID.format(m2_debs.KEY_FPR), False),
        (0, f"[GNUPG:] BADSIG {m2_debs.KEY_FPR}\n", False),
    ],
)
def test_gpg_verify_wants_a_valid_signature_by_the_ppas_primary_key(monkeypatch, code, status, ok):
    """The text gpg verified comes back only when its status names the pinned key as the
    signature's primary key and gpg succeeded; gpg itself runs with the committed key alone."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, b"Origin: x\n", status.encode())

    monkeypatch.setattr(m2_debs.subprocess, "run", run)
    if ok:
        assert m2_debs.gpg_verify(b"signed") == "Origin: x\n"
    else:
        with pytest.raises(m2_debs.DepsError, match="isn't validly signed"):
            m2_debs.gpg_verify(b"signed")
    assert calls[0][-2:] == ["--import", str(m2_debs.KEY)]
    assert calls[1][-1] == "--decrypt" and calls[0][3] == calls[1][2]  # one fresh home


@pytest.mark.parametrize(
    ("versions", "message"),
    [
        # Codex round 1 on #951: binaries of 1.0 with the sources of 2.0, uploaded meanwhile
        ({"macaulay2": ["2.0~u1"], "gfan": ["1.0~u1"], "msolve": ["1.0~u1"]}, "macaulay2 1.0~u1"),
        ({"macaulay2": ["1.0~u1"], "gfan": ["1.0~u1"]}, "source msolve 1.0~u1, which"),  # none
        ({"macaulay2": ["1.0~u1"], "gfan": ["1.0~u1", "1.0~u1"], "msolve": ["1.0~u1"]}, "2 times"),
    ],
)
def test_capture_wants_each_binarys_own_source_once(tmp_path, versions, message):
    """Each binary's source is its `Source:` name, or its own, at its version, listed exactly
    once; otherwise nothing is captured, and nothing written."""
    urls = snapshot_files(versions=versions)
    with pytest.raises(m2_debs.DepsError, match=message):
        m2_debs.capture(
            "20261106T000000Z", tmp_path / "out", get=fake_get(urls), verify=bytes.decode
        )
    assert not (tmp_path / "out").exists()


def test_a_source_field_may_give_the_sources_version():
    """`Source: name (version)`, for a binary whose version differs from its source's."""
    assert m2_debs.source_of(
        {"Package": "libmsolve3", "Version": "1.0+b1", "Source": "msolve (1.0)"}
    ) == ("msolve", "1.0")
    assert m2_debs.source_of({"Package": "libmsolve3", "Version": "1.0", "Source": "msolve"}) == (
        "msolve",
        "1.0",
    )
    assert m2_debs.source_of({"Package": "gfan", "Version": "0.8"}) == ("gfan", "0.8")
    with pytest.raises(m2_debs.DepsError, match="isn't name"):
        m2_debs.source_of({"Package": "x", "Version": "1", "Source": "x (1) y"})


def test_the_committed_manifests_sources_are_its_binaries_own():
    """Each .deb of the manifest has its source's .dsc and debian.tar.xz at its own version (the
    PPA's Source fields: macaulay2-common is macaulay2's, libmsolve3 msolve's), and no other."""
    pinned = m2_debs.read_manifest()
    wanted = set()
    for name in (n for n in pinned if n.endswith(".deb")):
        package, version, _arch = name.removesuffix(".deb").split("_")
        source = SOURCE_OF.get(package, package)
        wanted |= {f"{source}_{version}.dsc", f"{source}_{version}.debian.tar.xz"}
    assert wanted == {n for n in pinned if n.endswith((".dsc", ".debian.tar.xz"))}
    assert len([n for n in pinned if ".orig.tar." in n]) == 3


def test_capture_refuses_a_binary_listed_twice(tmp_path):
    """Review of #951, N1: each of the five once."""
    urls = snapshot_files()
    packages = gzip.decompress(urls["dists/noble/main/binary-amd64/Packages.gz"]).decode()
    gfan = next(s for s in packages.split("\n\n") if s.startswith("Package: gfan\n"))
    doubled = gzip.compress((packages.rstrip("\n") + "\n\n" + gfan).encode())
    urls["dists/noble/main/binary-amd64/Packages.gz"] = doubled
    release = urls["dists/noble/InRelease"].decode()
    old = f" {sha(gzip.compress(packages.encode()))} "
    urls["dists/noble/InRelease"] = release.replace(
        old.strip(), sha(doubled)
    ).encode()  # the index as InRelease lists it
    with pytest.raises(m2_debs.DepsError, match="not each of"):
        m2_debs.capture(
            "20261106T000000Z", tmp_path / "out", get=fake_get(urls), verify=bytes.decode
        )
    assert not (tmp_path / "out").exists()


def test_capture_wants_an_empty_directory_and_a_noble_index(tmp_path):
    """Review of #951, N2 and N3: every file in the directory goes into the release, and a
    validly signed InRelease of another series isn't noble's."""
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "old.deb").write_bytes(b"x")
    urls = snapshot_files()
    with pytest.raises(m2_debs.DepsError, match="isn't empty"):
        m2_debs.capture(
            "20261106T000000Z", tmp_path / "used", get=fake_get(urls), verify=bytes.decode
        )
    urls["dists/noble/InRelease"] = urls["dists/noble/InRelease"].replace(b"noble", b"jammy")
    with pytest.raises(m2_debs.DepsError, match="'jammy', not noble"):
        m2_debs.capture(
            "20261106T000000Z", tmp_path / "new", get=fake_get(urls), verify=bytes.decode
        )
    (tmp_path / "empty").mkdir()  # an empty one is fine
    m2_debs.capture(
        "20261106T000000Z", tmp_path / "empty", get=fake_get(snapshot_files()), verify=bytes.decode
    )


def test_gpg_verify_says_what_is_missing_in_one_line(monkeypatch, tmp_path):
    """Review of #951, N4: no key file, no gpg, a key gpg can't import: a DepsError each."""
    with pytest.raises(m2_debs.DepsError, match="no key file"):
        m2_debs.gpg_verify(b"x", key=tmp_path / "missing.asc")

    def no_gpg(argv, **kw):
        raise FileNotFoundError("gpg")

    monkeypatch.setattr(m2_debs.subprocess, "run", no_gpg)
    with pytest.raises(m2_debs.DepsError, match="gpg, which isn't installed"):
        m2_debs.gpg_verify(b"x")

    def bad_key(argv, **kw):
        raise subprocess.CalledProcessError(2, argv, b"", b"gpg: no valid OpenPGP data found.")

    monkeypatch.setattr(m2_debs.subprocess, "run", bad_key)
    with pytest.raises(m2_debs.DepsError, match=r"couldn't import .*no valid OpenPGP data"):
        m2_debs.gpg_verify(b"x")


def test_the_release_tag_is_macaulay2s_source_version():
    """Review of #951, N5: the `.dsc` suffix comes off whether or not the version has a `~`."""
    assert m2_debs.release_tag({"macaulay2_1.26.06+ds-5~ubuntu24.04.1.dsc": ""}) == (
        "deps/macaulay2-1.26.06+ds-5"
    )
    assert m2_debs.release_tag({"macaulay2_1.27+ds-1.dsc": ""}) == "deps/macaulay2-1.27+ds-1"


def test_paris_data_manifest_is_build_shs_pins():
    """files/pari-data.sha256, which the release's copy is checked against, holds exactly the
    sha256 build.sh pins for each of PARI's data packages (step 3)."""
    text = (m2_debs.HERE / "build.sh").read_text()
    pins = dict(re.findall(r'^\s+\[(\w+)\]="([0-9a-f]{64})"$', text, re.M))
    assert len(pins) == 5
    assert m2_debs.read_manifest(m2_debs.PARI_DATA.manifest) == {
        f"{name}.tgz": sha for name, sha in pins.items()
    }
    assert m2_debs.package("elldata.tgz") == "elldata"


def test_the_pari_data_set_speaks_for_itself(tmp_path):
    with pytest.raises(m2_debs.DepsError) as err:
        m2_debs.fetch(
            "o/r@p", tmp_path, {"elldata.tgz": "0" * 64}, run=FakeGh({}), kind=m2_debs.PARI_DATA
        )
    text = str(err.value)
    assert text.startswith("PARI's pinned data packages from release o/r@p don't match pari-data")
    assert "elldata.tgz (elldata): missing" in text and '"PARI\'s data packages"' in text
    with pytest.raises(m2_debs.DepsError, match="--pari-data-release or --pari-data-dir"):
        m2_debs.fetch(
            "o/r@p", tmp_path, {"a.tgz": "0" * 64}, run=FakeGh({}, code=1), kind=m2_debs.PARI_DATA
        )


def test_the_pari_sets_default_release_is_the_origins_too():
    """Review of #952, B1: the PARI set's release is a tag of the checkout's origin, like the
    Macaulay2 set's; the message without an origin names its own flag."""
    assert m2_debs.PARI_RELEASE == "deps/pari-data-20261107"
    url = "git@github.com:someone/zab.git"
    assert m2_debs.default_release(m2_debs.PARI_RELEASE, run=git_remote(url)) == (
        f"someone/zab@{m2_debs.PARI_RELEASE}"
    )
    with pytest.raises(m2_debs.DepsError, match="pass --pari-data-release OWNER/REPO@TAG"):
        m2_debs.default_release(
            m2_debs.PARI_RELEASE, run=git_remote("", 2), flag="--pari-data-release"
        )
