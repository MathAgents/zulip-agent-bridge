"""The image's wider stack (issue #474): Macaulay2's pinned packages (#945), noble's packages,
atlas, the hash-pinned venv, HomotopyContinuation.jl in the Oscar environment and the first-boot
warm-up, checked offline against build.sh and the committed files."""

from __future__ import annotations

import base64
import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import m2_debs

AMI = Path(__file__).resolve().parents[2] / "workshop" / "ami"
FILES = AMI / "files"
BUILD_SH = AMI / "build.sh"
EXCLUDED = re.compile(r"wolfram|mathematica|agda|rocq|\bcoq\b|isabelle", re.IGNORECASE)


def armored_key_fingerprint(text: str) -> str:
    """The v4 fingerprint of the first public-key packet of an ASCII-armored key (RFC 4880
    §12.2): SHA-1 over 0x99, the two-byte length and the packet body."""
    body = text.split("-----BEGIN PGP PUBLIC KEY BLOCK-----", 1)[1].split("-----END", 1)[0]
    b64 = "".join(line for line in body.split("\n\n", 1)[1].split() if not line.startswith("="))
    data = base64.b64decode(b64)
    head = data[0]
    assert head & 0x80, "not an OpenPGP packet"
    if head & 0x40:  # new format
        tag, first = head & 0x3F, data[1]
        if first < 192:
            length, start = first, 2
        elif first < 224:
            length, start = ((first - 192) << 8) + data[2] + 192, 3
        else:
            length, start = int.from_bytes(data[2:6], "big"), 6
    else:
        tag, kind = (head >> 2) & 0x0F, head & 0x03
        size = {0: 1, 1: 2, 2: 4}[kind]
        length, start = int.from_bytes(data[1 : 1 + size], "big"), 1 + size
    assert tag == 6, f"first packet is tag {tag}, not a public key"
    packet = data[start : start + length]
    assert packet[0] == 4, "not a v4 key"
    return hashlib.sha1(b"\x99" + len(packet).to_bytes(2, "big") + packet).hexdigest().upper()  # noqa: S324


def test_macaulay2_comes_from_the_pinned_packages_of_its_release():
    """#945: Launchpad's snapshots of the PPA expire, so the five .debs and the source packages
    they were built from come from a GitHub release, each pinned by its SHA-256 in
    files/apt/macaulay2-debs.sha256. build.sh checks them again, installs them as files, holds
    them and keeps the sources, and writes no apt source for the PPA."""
    text = BUILD_SH.read_text()
    versions = {
        k: re.search(rf"^{k}=(\S+)", text, re.M).group(1)
        for k in ("M2_VERSION", "M2_GFAN_VERSION", "M2_MSOLVE_VERSION")
    }
    assert versions == {
        "M2_VERSION": "1.26.06+ds-5~ubuntu24.04.1",
        "M2_GFAN_VERSION": "0.8~beta-2~ubuntu24.04.1",
        "M2_MSOLVE_VERSION": "0.9.4-2ppa2~ubuntu24.04.1",
    }
    m2, gfan, msolve = versions.values()
    pinned = m2_debs.read_manifest()
    assert {n for n in pinned if n.endswith(".deb")} == {
        f"macaulay2_{m2}_amd64.deb",
        f"macaulay2-common_{m2}_all.deb",
        f"gfan_{gfan}_amd64.deb",
        f"msolve_{msolve}_amd64.deb",
        f"libmsolve3_{msolve}_amd64.deb",
    }
    for deb in (
        "macaulay2_${M2_VERSION}_amd64.deb",
        "macaulay2-common_${M2_VERSION}_all.deb",
        "gfan_${M2_GFAN_VERSION}_amd64.deb",
        "msolve_${M2_MSOLVE_VERSION}_amd64.deb",
        "libmsolve3_${M2_MSOLVE_VERSION}_amd64.deb",
    ):
        assert f'"{deb}"' in text[text.index("m2_install=(") :], deb
    assert '$APT install -y --no-install-recommends "${m2_install[@]/#/$m2/}"' in text
    for source, version in (("macaulay2", m2), ("gfan", gfan), ("msolve", msolve)):
        assert {f"{source}_{version}.dsc", f"{source}_{version}.debian.tar.xz"} <= set(pinned)
        assert [n for n in pinned if n.startswith(f"{source}_") and ".orig.tar." in n]
    assert len(pinned) == 14
    assert m2_debs.RELEASE == "deps/macaulay2-" + m2.split("~")[0]
    assert 'sha256sum --check --strict --quiet "$FILES/apt/macaulay2-debs.sha256"' in text
    assert "sudo apt-mark hold macaulay2 macaulay2-common gfan msolve libmsolve3" in text
    assert "sudo install -d -m 0755 /usr/src/macaulay2-ppa" in text
    # no source written (#947's removal of an old one, at the build's end, may name the file)
    assert "URIs: https://snapshot.ppa" not in text and "tee /etc/apt/sources" not in text
    assert "M2_VERSION:-" not in text  # not a knob: build_ami.py forwards none
    assert text.index('[ -z "$M2_DEBS" ]') < text.index("# --- 1. system packages")  # early
    # the committed key stays, for capturing a new set (m2_debs.py capture)
    key = (FILES / "apt" / "macaulay2-ppa.asc").read_text()
    assert key.count("-----BEGIN PGP PUBLIC KEY BLOCK-----") == 1
    assert armored_key_fingerprint(key) == m2_debs.KEY_FPR


def test_the_wider_apt_stack_is_installed_and_nothing_excluded_is():
    text = BUILD_SH.read_text().replace("\\\n", " ")
    packages = set()
    for m in re.finditer(r"\$APT install[^\n]*", text):
        words = m.group(0).split()
        packages |= {w.split("=")[0].strip('"') for w in words[words.index("install") + 1 :]}
    assert {
        "singular",
        "polymake",
        "maxima",
        "r-base-core",
        "r-recommended",
        "r-cran-tidyverse",
        "r-cran-data.table",
        "r-cran-rmarkdown",
        "r-cran-knitr",
        "r-cran-irkernel",
        "octave",
        "graphviz",
        "gnuplot-nox",
        "imagemagick",
        "poppler-utils",
        "librsvg2-bin",
        "ghostscript",
        "z3",
        "cadical",
        "pandoc",
        "4ti2",
        "lrslib",
        "nauty",
        "normaliz",
        "topcom",
        "coinor-csdp",
        "bison",
    } <= packages  # fmt: skip; Macaulay2, gfan and msolve are .deb files (test above)
    assert not [p for p in packages if EXCLUDED.search(p)]
    assert "wolfram" not in BUILD_SH.read_text().lower()


def test_atlas_is_built_from_a_pinned_archive():
    text = BUILD_SH.read_text()
    assert re.search(r"^ATLAS_COMMIT=[0-9a-f]{40}$", text, re.M)
    assert re.search(r"^ATLAS_SHA256=[0-9a-f]{64}$", text, re.M)
    assert 'fetch_checked "$ATLAS_SHA256" "$src/atlas.tar.gz"' in text
    assert "archive/$ATLAS_COMMIT.tar.gz" in text
    assert "INSTALLDIR=/usr/local/atlas BINDIR=/usr/local/bin" in text
    # the messages directory is compiled in, so INSTALLDIR is given to the build too
    assert "optimize=true INSTALLDIR=/usr/local/atlas >/dev/null" in text
    # built after the compilers and readline (step 1) and bison (step 1c) are there
    assert text.index("libreadline-dev") < text.index("bison") < text.index("ATLAS_COMMIT=")


def test_the_venv_is_hash_pinned_and_covers_the_requested_packages():
    req = (FILES / "venv-requirements.txt").read_text()
    pins = dict(re.findall(r"^([a-z0-9_.-]+)==(\S+) \\$", req, re.M))
    assert len(pins) == len(re.findall(r"^[a-z0-9]", req, re.M))  # every requirement pinned
    for block in re.split(r"\n(?=[a-z0-9])", req.split("\n", 2)[2]):
        assert "--hash=sha256:" in block, block[:60]
    wanted = [
        line.split("<")[0]
        for line in (FILES / "venv-requirements.in").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert set(wanted) <= set(pins), set(wanted) - set(pins)
    assert {"scipy", "pandas", "networkx", "statsmodels", "scikit-learn", "seaborn", "highspy",
            "pyscipopt", "pulp", "cvxpy", "ortools", "snappy", "regina"} <= set(wanted)  # fmt: skip
    assert pins["pulp"].startswith("3.")  # PuLP 4 dropped CBC and the LpVariable constructor
    text = BUILD_SH.read_text()
    assert '--require-hashes -r "$FILES/venv-requirements.txt"' in text
    assert 'IRkernel::installspec(name = "ir"' in text
    # build.sh reruns (review on #481): current uv refuses an existing venv without this
    assert 'venv --python 3.12 -q --allow-existing "$AGENT_HOME/venv"' in text


def test_the_sdists_build_backend_is_hash_pinned_too():
    """Review on #481: `--require-hashes` doesn't cover what uv fetches to build PyX's sdist;
    `--build-constraints` with hashes does, with the same setuptools as the requirements."""
    req = (FILES / "venv-requirements.txt").read_text()
    constraints = (FILES / "venv-build-constraints.txt").read_text()

    def block(text: str, name: str) -> str:
        m = re.search(
            rf"^{name}==\S+ \\\n(?:    --hash=sha256:[0-9a-f]{{64}}(?: \\)?\n)+", text, re.M
        )
        assert m, name
        return m.group(0).replace(" \\\n", "\n").rstrip()

    assert block(constraints, "setuptools") == block(req, "setuptools")
    assert "pyx==" in req
    assert '--build-constraints "$FILES/venv-build-constraints.txt"' in BUILD_SH.read_text()


def test_homotopy_continuation_joins_the_pinned_oscar_environment():
    project = tomllib.loads((FILES / "julia" / "Project.toml").read_text())["deps"]
    assert set(project) == {"Oscar", "HomotopyContinuation"}
    deps = tomllib.loads((FILES / "julia" / "Manifest.toml").read_text())["deps"]
    assert deps["Oscar"][0]["version"] == "1.8.2" and deps["Oscar"][0].get("pinned")
    hc = deps["HomotopyContinuation"][0]
    assert hc["version"] == "2.22.4" and hc.get("pinned")  # like Oscar (review on #481)
    text = BUILD_SH.read_text()
    assert "using Oscar, HomotopyContinuation" in text
    # both smoke tests: HC and Oscar's extension for it load from the cache (review on #481)
    smoke = text.split("smoke test (workshop/ami/README.md)", 1)[1]
    assert "Base.get_extension(Oscar, :HomotopyContinuationExt) !== nothing" in smoke
    assert 'check as_agent bash -c "$HC_SMOKE"' in smoke
    sys.path.insert(0, str(AMI))
    try:
        import build_ami as ba
    finally:
        sys.path.remove(str(AMI))
    script = ba.SMOKE_SCRIPT
    assert ":HomotopyContinuationExt) !== nothing" in script
    assert script.index("oscar_check\n") < script.index("using Oscar, HomotopyContinuation")
    assert script.index("using Oscar, HomotopyContinuation") < script.index(
        "tools_manifest.py check"
    )


def test_the_first_boot_warms_the_new_big_trees():
    sys.path.insert(0, str(AMI.parent / "aws"))
    try:
        import aws_common as ac
    finally:
        sys.path.remove(str(AMI.parent / "aws"))
    for path in ("/usr/share/Macaulay2", "/usr/lib/R", "/home/agent/venv", "/usr/share/polymake"):
        assert path in ac.WARM_PATHS, path
        assert path in ac.WARM_VOLUME
    # Lean first, as before (the first import of Mathlib is what a participant waits for)
    assert ac.WARM_PATHS.index("/opt/lean") < ac.WARM_PATHS.index("/usr/share/Macaulay2")


def run_macaulay2_step(tmp_path: Path, tamper: str = "", tarball: bool = False, unlisted: str = ""):
    """Step 1d under bash, `sudo` and apt recorded, not run, on a small fake set under the pinned
    names: (exit code, stderr, the sudo and apt command lines)."""
    text = BUILD_SH.read_text()
    step = text[text.index("# --- 1d.") : text.index("# --- 2. the participant's user")]
    versions = dict(re.findall(r"^(M2_\w*VERSION)=(\S+)", step, re.M))
    m2, gfan = versions["M2_VERSION"], versions["M2_GFAN_VERSION"]
    msolve = versions["M2_MSOLVE_VERSION"]
    names = [
        f"macaulay2_{m2}_amd64.deb", f"macaulay2-common_{m2}_all.deb", f"gfan_{gfan}_amd64.deb",
        f"msolve_{msolve}_amd64.deb", f"libmsolve3_{msolve}_amd64.deb", f"gfan_{gfan}.dsc",
    ]  # fmt: skip
    tmp_path.mkdir()
    files, debs, calls = tmp_path / "files", tmp_path / "debs", tmp_path / "calls"
    (files / "apt").mkdir(parents=True)
    debs.mkdir()
    for name in names:
        (debs / name).write_bytes(name.encode())
    (files / "apt" / "macaulay2-debs.sha256").write_text(
        "".join(f"{hashlib.sha256(n.encode()).hexdigest()}  {n}\n" for n in names if n != unlisted)
    )
    if tamper:
        (debs / tamper).write_bytes(b"another")
    source = debs
    if tarball:
        source = tmp_path / "m2-debs.tar"
        tar = ["tar", "-cf", str(source), "-C", str(debs), *names]
        subprocess.run(tar, check=True)  # noqa: S603 - an argument list
    stubs = (
        "set -euo pipefail\n"
        'log() { echo "==> $*"; }\n'
        f'sudo() {{ printf "%s\\n" "$*" >> {calls}; }}\n'
        f'apt_get() {{ printf "apt %s\\n" "$*" >> {calls}; }}\n'
        f"APT=apt_get\nFILES={files}\nM2_DEBS={source}\n"
    )
    run = subprocess.run(  # noqa: S603 - build.sh's own step
        ["bash", "-c", stubs + step],  # noqa: S607
        env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    return run.returncode, run.stderr, calls.read_text().splitlines() if calls.exists() else []


def test_the_macaulay2_step_installs_the_checked_files_and_keeps_the_sources(tmp_path):
    for tarball in (False, True):
        code, err, calls = run_macaulay2_step(tmp_path / str(tarball), tarball=tarball)
        assert code == 0, err
        apt = next(c for c in calls if c.startswith("apt install"))
        assert apt.count(".deb") == 5 and "--no-install-recommends" in apt
        assert "/macaulay2-common_1.26.06+ds-5~ubuntu24.04.1_all.deb" in apt
        assert not [c for c in calls if "apt-mark" in c]  # held in step 14, after the smoke test
        installs = [c for c in calls if c.startswith("install -m 0644")]
        assert len(installs) == 2  # the one source file, and the manifest as SHA256SUMS
        assert installs[0].endswith("/gfan_0.8~beta-2~ubuntu24.04.1.dsc /usr/src/macaulay2-ppa/")
        assert installs[1].endswith("/usr/src/macaulay2-ppa/SHA256SUMS")


def test_the_macaulay2_step_stops_on_a_file_that_isnt_the_pinned_one(tmp_path):
    deb = "gfan_0.8~beta-2~ubuntu24.04.1_amd64.deb"
    code, err, calls = run_macaulay2_step(tmp_path / "t", tamper=deb)
    assert code == 1 and "don't match files/apt/macaulay2-debs.sha256" in err
    assert f"{deb}: FAILED" in err and "build_ami.py" in err
    assert not [c for c in calls if c.startswith("apt ")]


def test_the_macaulay2_step_installs_only_what_the_manifest_lists(tmp_path):
    """Review of #950: a .deb the install line names but the manifest doesn't (a version bumped
    in build.sh alone) stops the step: sha256sum --check covers the manifest's lines only."""
    deb = "libmsolve3_0.9.4-2ppa2~ubuntu24.04.1_amd64.deb"
    code, err, calls = run_macaulay2_step(tmp_path / "u", unlisted=deb)
    assert code == 1 and f"{deb} isn't in files/apt/macaulay2-debs.sha256" in err
    assert not [c for c in calls if c.startswith("apt ")]


def test_a_mistyped_pari_data_stops_the_build_early():
    """Review of #952, N2: a PARI_DATA that names nothing stops the build at its start, rather
    than the realpath loop emptying it and every package coming from PARI's site."""
    text = BUILD_SH.read_text()
    check = 'if [ -n "$PARI_DATA" ] && ! [ -e "$PARI_DATA" ]; then'
    assert text.index(check) < text.index("for var in ZAB_WHEEL") < text.index("# --- 1. system")
