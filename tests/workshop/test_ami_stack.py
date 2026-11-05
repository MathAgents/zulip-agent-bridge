"""The image's wider stack (issue #474): the Macaulay2 PPA, noble's packages, atlas, the
hash-pinned venv, HomotopyContinuation.jl in the Oscar environment and the first-boot warm-up,
checked offline against build.sh and the committed files."""

from __future__ import annotations

import base64
import hashlib
import re
import sys
import tomllib
from pathlib import Path

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


def test_the_macaulay2_ppa_is_pinned_by_key_snapshot_and_versions():
    """Review on #481: a PPA keeps only its newest upload, so the source is Launchpad's snapshot
    of it at a fixed time; Macaulay2, gfan and msolve are pinned on top; the key file holds the
    one key (apt trusts every key in it), and its fingerprint is the pinned one."""
    text = BUILD_SH.read_text()
    key = (FILES / "apt" / "macaulay2-ppa.asc").read_text()
    assert key.count("-----BEGIN PGP PUBLIC KEY BLOCK-----") == 1
    fpr = re.search(r"^M2_PPA_FPR=([0-9A-F]{40})$", text, re.M).group(1)
    assert armored_key_fingerprint(key) == fpr
    assert re.search(r"^M2_PPA_SNAPSHOT=\d{8}T\d{6}Z$", text, re.M)
    assert re.search(r"^M2_VERSION=1\.26\.06\+ds-2~ubuntu24\.04\.1 ", text, re.M)
    assert re.search(r"^M2_GFAN_VERSION=0\.8~beta-2~ubuntu24\.04\.1$", text, re.M)
    assert re.search(r"^M2_MSOLVE_VERSION=0\.9\.4-2ppa2~ubuntu24\.04\.1 ", text, re.M)
    assert '"macaulay2=$M2_VERSION" "macaulay2-common=$M2_VERSION"' in text
    assert (
        '"gfan=$M2_GFAN_VERSION" "msolve=$M2_MSOLVE_VERSION" "libmsolve3=$M2_MSOLVE_VERSION"'
        in text
    )
    uri = "URIs: https://snapshot.ppa.launchpadcontent.net/macaulay2/macaulay2/ubuntu/$M2_PPA_SNAPSHOT"
    assert uri in text and "URIs: https://ppa.launchpadcontent.net" not in text
    assert "Signed-By: /etc/apt/keyrings/macaulay2-ppa.asc" in text
    assert '[ "$(grep -c \'^pub:\' <<<"$keys")" = 1 ]' in text
    assert "{print $10; exit}" not in text  # no early exit under pipefail (review on #481)
    assert "M2_VERSION:-" not in text  # not a knob: build_ami.py forwards none
    assert text.index("macaulay2-ppa.asc") < text.index(
        'apt_update\n$APT install -y --no-install-recommends "macaulay2'
    )


def test_the_wider_apt_stack_is_installed_and_nothing_excluded_is():
    text = BUILD_SH.read_text().replace("\\\n", " ")
    packages = set()
    for m in re.finditer(r"\$APT install[^\n]*", text):
        words = m.group(0).split()
        packages |= {w.split("=")[0].strip('"') for w in words[words.index("install") + 1 :]}
    assert {
        "singular", "polymake", "maxima", "r-base-core", "r-recommended", "r-cran-tidyverse",
        "r-cran-data.table", "r-cran-rmarkdown", "r-cran-knitr", "r-cran-irkernel", "octave",
        "graphviz", "gnuplot-nox", "imagemagick", "poppler-utils", "librsvg2-bin", "ghostscript",
        "z3", "cadical", "pandoc", "4ti2", "lrslib", "nauty", "normaliz", "topcom", "coinor-csdp",
        "macaulay2", "gfan", "msolve", "bison",
    } <= packages  # fmt: skip
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
