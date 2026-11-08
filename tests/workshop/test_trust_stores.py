"""Trust stores without a distrusted root (#671): `files/bin/trust_stores.py`, the certifi that
every file of the repository pins, and where the image build checks its stores.

Offline. The cacert.pem read is the one this environment installed from uv.lock (CI runs
`uv sync --locked`), checked against its wheel's RECORD. Every other file pins the same release,
wheel and sdist hash for hash, or bounds it from below at that release, so the one file read
covers them all."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime
from importlib.machinery import SourceFileLoader
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parents[2]
AMI = ROOT / "workshop" / "ami"
FILES = AMI / "files"
SCRIPT = FILES / "bin" / "trust_stores.py"
BUILD_SH = AMI / "build.sh"

#: emSign ECC Root CA - G3, as certifi 2026.7.22 still had it: a public root certificate.
EMSIGN_ECC_G3 = """\
-----BEGIN CERTIFICATE-----
MIICTjCCAdOgAwIBAgIKPPYHqWhwDtqLhDAKBggqhkjOPQQDAzBrMQswCQYDVQQG
EwJJTjETMBEGA1UECxMKZW1TaWduIFBLSTElMCMGA1UEChMcZU11ZGhyYSBUZWNo
bm9sb2dpZXMgTGltaXRlZDEgMB4GA1UEAxMXZW1TaWduIEVDQyBSb290IENBIC0g
RzMwHhcNMTgwMjE4MTgzMDAwWhcNNDMwMjE4MTgzMDAwWjBrMQswCQYDVQQGEwJJ
TjETMBEGA1UECxMKZW1TaWduIFBLSTElMCMGA1UEChMcZU11ZGhyYSBUZWNobm9s
b2dpZXMgTGltaXRlZDEgMB4GA1UEAxMXZW1TaWduIEVDQyBSb290IENBIC0gRzMw
djAQBgcqhkjOPQIBBgUrgQQAIgNiAAQjpQy4LRL1KPOxst3iAhKAnjlfSU2fySU0
WXTsuwYc58Byr+iuL+FBVIcUqEqy6HyC5ltqtdyzdc6LBtCGI79G1Y4PPwT01xyS
fvalY8L1X44uT6EYGQIrMgqCZH0Wk9GjQjBAMB0GA1UdDgQWBBR8XQKEE9TMipuB
zhccLikenEhjQjAOBgNVHQ8BAf8EBAMCAQYwDwYDVR0TAQH/BAUwAwEB/zAKBggq
hkjOPQQDAwNpADBmAjEAvvNhzwIQHWSVB7gYboiFBS+DCBeQyh+KTOgNG3qxrdWB
CUfvO6wIBHxcmbHtRwfSAjEAnbpV/KlK6O3t5nYBQnvI+GDZjVGLVTv7jHvrZQnD
+JbNR6iC8hZVdyR+EhCVBCyj
-----END CERTIFICATE-----
"""

#: Every file that names certifi as a requirement, and how: `==` with the release's hashes, or
#: `>=` the release. A new one must join (test_no_other_file_requires_certifi).
HASHED = ["workshop/ami/files/venv-requirements.txt", "workshop/ami/files/certbot-requirements.txt"]
FLOORS = ["workshop/waker/requirements.txt"]
#: Where a later `--exclude-newer` cutoff for certifi alone lets uv.lock's release in.
CUTOFFS = [
    "workshop/ami/build.sh",
    "workshop/ami/files/venv-requirements.in",
    "workshop/ami/files/venv-requirements.txt",
    "workshop/ami/files/certbot-requirements.in",
    "workshop/ami/files/certbot-requirements.txt",
]


def load():
    loader = SourceFileLoader("trust_stores", str(SCRIPT))
    spec = importlib.util.spec_from_loader("trust_stores", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ts = load()


def run(*paths: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a fixed argument list
        [sys.executable, "-I", str(SCRIPT), *map(str, paths)], capture_output=True, text=True
    )


def locked() -> dict:
    """uv.lock's certifi: its version, its wheel's and sdist's hashes, the wheel's upload time."""
    (package,) = [
        p
        for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
        if p["name"] == "certifi"
    ]
    (wheel,) = package["wheels"]
    return {
        "version": package["version"],
        "hashes": {wheel["hash"], package["sdist"]["hash"]},
        "uploaded": datetime.fromisoformat(wheel["upload-time"]),
    }


def test_the_listed_fingerprint_is_emsign_ecc_root_ca_g3():
    der = base64.b64decode("".join(EMSIGN_ECC_G3.splitlines()[1:-1]))
    assert b"emSign ECC Root CA - G3" in der  # the subject's common name, as DER holds it
    assert ts.DISTRUSTED == {hashlib.sha256(der).hexdigest(): "emSign ECC Root CA - G3"}
    assert ts.fingerprints(EMSIGN_ECC_G3.encode()) == list(ts.DISTRUSTED)


def test_the_check_finds_the_root_in_a_bundle_and_through_a_link(tmp_path):
    """A bundle that adds the root to certifi's is caught, by file and, as /etc/ssl/certs links
    its files, through a linked .pem in a directory; certifi's own passes."""
    store, certs = tmp_path / "store", tmp_path / "certs"
    store.mkdir()
    certs.mkdir()
    clean = Path(certifi.where()).read_text()
    (store / "clean.crt").write_text(clean)
    (store / "bundle.crt").write_text(clean + EMSIGN_ECC_G3)
    (certs / "emSign.pem").symlink_to(store / "bundle.crt")
    (certs / "dangling.pem").symlink_to(store / "gone.pem")
    for path in (store / "bundle.crt", certs):
        out = run(path)
        assert out.returncode == 1, out
        assert "  DISTRUSTED emSign ECC Root CA - G3: " in out.stdout, out.stdout
        assert out.stdout.endswith("FAILED: the DISTRUSTED or NO BUNDLE lines above\n")
    out = run(store / "clean.crt")
    assert out.returncode == 0, out
    assert out.stdout.endswith("ok: no bundle holds emSign ECC Root CA - G3\n")


def test_nothing_to_read_fails_and_a_missing_path_is_an_error(tmp_path):
    """A directory without a bundle proves nothing: a .pem of DH parameters and a .txt with a
    certificate are not read as one. A path that isn't there is a usage error."""
    dh = "-----BEGIN DH PARAMETERS-----\nMAYCAQACAQI=\n-----END DH PARAMETERS-----\n"
    (tmp_path / "dhparams.pem").write_text(dh)
    (tmp_path / "notes.txt").write_text(EMSIGN_ECC_G3)
    out = run(tmp_path)
    assert out.returncode == 1, out
    assert f"  NO BUNDLE: nothing was checked under {tmp_path}" in out.stdout
    out = run(tmp_path / "missing")
    assert out.returncode == 2 and "no such file or directory" in out.stderr, out
    assert run().returncode == 2


def test_the_report_names_each_certifi_and_its_version(tmp_path):
    site = tmp_path / "lib" / "python3.12" / "site-packages"
    (site / "certifi").mkdir(parents=True)
    (site / "certifi-2026.10.23.dist-info").mkdir()
    (site / "certifi" / "cacert.pem").write_text(Path(certifi.where()).read_text())
    out = run(tmp_path)
    assert out.returncode == 0, out
    assert f"  certifi 2026.10.23: {site / 'certifi' / 'cacert.pem'}\n" in out.stdout


def test_the_locked_certifi_installed_here_holds_no_distrusted_root():
    """The certifi this environment runs is uv.lock's release, its cacert.pem is the one its
    wheel's RECORD lists (unchanged since it was installed), and it holds no distrusted root."""
    lock = locked()
    dist = importlib.metadata.distribution("certifi")
    assert dist.version == lock["version"], "sync this environment from uv.lock: uv sync --locked"
    pem = Path(certifi.where())
    assert pem == Path(dist.locate_file("certifi/cacert.pem"))  # not a system store's patch
    (record,) = [f for f in dist.files if f.as_posix() == "certifi/cacert.pem"]
    digest = base64.urlsafe_b64encode(hashlib.sha256(pem.read_bytes()).digest()).rstrip(b"=")
    assert record.hash.mode == "sha256" and record.hash.value == digest.decode()
    out = run(pem)
    assert out.returncode == 0, out.stdout


def test_every_certifi_pin_is_uv_locks_release():
    lock = locked()
    pin = re.compile(r"^certifi==(\S+) \\\n((?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", re.M)
    for rel in HASHED:
        m = pin.search((ROOT / rel).read_text())
        assert m, rel
        assert m.group(1) == lock["version"], rel
        assert {f"sha256:{h}" for h in re.findall(r"[0-9a-f]{64}", m.group(2))} == lock["hashes"]
    for rel in FLOORS:
        assert re.findall(r"^certifi(\S*)$", (ROOT / rel).read_text(), re.M) == [
            f">={lock['version']}"
        ], rel


def test_no_other_file_requires_certifi():
    """Any requirement, constraint or lock file of the repository that names certifi is one the
    test above checks (hidden directories, such as virtualenvs and worktrees, left out)."""
    named = set()
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
        for name in files:
            if not re.search(r"requirements|constraints|\.lock$|\.in$|pyproject\.toml$", name):
                continue
            path = Path(root, name)
            text = path.read_text(errors="replace")
            if re.search(r'^\s*"?certifi\b(?![-\w])', text, re.M) or 'name = "certifi"' in text:
                named.add(path.relative_to(ROOT).as_posix())
    assert named == {"uv.lock", *HASHED, *FLOORS}


def test_each_certifi_cutoff_admits_uv_locks_release():
    uploaded = locked()["uploaded"]
    cutoff = re.compile(r"--exclude-newer-package certifi=(\S+?)(?:\s|\\|$)", re.M)
    for rel in CUTOFFS:
        found = cutoff.findall((ROOT / rel).read_text())
        assert found, rel
        for when in found:
            assert datetime.fromisoformat(when) > uploaded, (rel, when)


def test_the_image_build_checks_its_trust_stores():
    """build.sh: ca-certificates from the archive and /etc/ssl/certs checked before the long
    steps; conda's two environments get ca-certificates and certifi at uv.lock's release at least;
    the smoke test checks the system store and every Python environment the build creates."""
    text = BUILD_SH.read_text()
    step1 = text[text.index("# --- 1. system packages") : text.index("# --- 1b. LaTeX")]
    assert re.search(r"^\s+curl .* ca-certificates \\$", step1, re.M)
    assert '/usr/bin/python3 -I "$FILES/bin/trust_stores.py" /etc/ssl/certs \\\n  || {' in step1
    version = locked()["version"]
    assert f"'ca-certificates>={version}' 'certifi>={version}'" in text
    assert re.search(r"^  for env in base sage; do$", text, re.M)  # Sage's block, nothing else
    # build.sh reruns on a builder: each pinned environment's install runs every time, not only
    # when the environment is missing, so that a rerun takes newer pins (certifi's, #671). The
    # uv tool installs are --force, which installs the environment afresh.
    uv = '"$AGENT_HOME/.local/bin/uv"'
    for line in (
        f'as_agent {uv} pip install -q --python "$AGENT_HOME/venv/bin/python" \\\n',
        f"as_agent {uv} tool install -q --force --exclude-newer 2026-10-04T00:00:00Z \\\n",
        f"sudo env VIRTUAL_ENV=/opt/certbot {uv} pip install -q --require-hashes \\\n",
    ):
        assert f"\n{line}" in text, line  # at the top level, under no `if`
    smoke = text[text.index("# --- 13. smoke test") : text.index("# --- 14. housekeeping")]
    paths = re.search(r"^TRUST_PATHS=\((.*)\)$", smoke, re.M).group(1).split()
    venvs = ['"$AGENT_HOME/venv"', "/opt/certbot", '"$AGENT_HOME/.local/share/uv/tools"']
    assert paths == ["/etc/ssl/certs", *venvs]
    conda = '"$AGENT_HOME/miniforge/ssl" "$AGENT_HOME/miniforge/envs/sage/ssl"'
    assert f"TRUST_PATHS+=({conda})" in smoke
    # #816: what conda's certifi and pip's vendored certifi return, whatever their builds ship,
    # fail-closed; pip at a release whose vendored certifi lacks the root; a uv-managed CPython.
    assert "'pip>=26.2.2'" in text
    where = "print(certifi.where()); print(v.where())'"
    assert where in smoke and 'where="$py: certifi.where() failed"' in smoke
    assert 'TRUST_PATHS+=("$line")' in smoke
    assert 'TRUST_PATHS+=("$AGENT_HOME/.local/share/uv/python")' in smoke
    run_line = 'sudo /usr/bin/python3 -I "$FILES/bin/trust_stores.py" "${TRUST_PATHS[@]}"'
    assert smoke.index(run_line) < smoke.index('if [ "$fail" -ne 0 ]')
    assert "fail=1" in smoke[smoke.index(run_line) :]


def test_der_certificates_and_hashed_names_are_read_once(tmp_path):
    """#816: a DER `.cer`, and a CApath's hashed name for a file whose own name has no bundle's
    suffix, are read; a hashed name for a `.pem` the walk reads too counts once."""
    der = base64.b64decode("".join(EMSIGN_ECC_G3.splitlines()[1:-1]))
    assert ts.fingerprints(der) == list(ts.DISTRUSTED)
    for other in (der[:-1], b"0\x03\x02\x01\x00", b"0\x82\x00\x00", b"0\x81\x01\x02"):
        assert ts.fingerprints(other) == [], other  # cut short, short-form, empty, no SEQUENCE
    # exactly one certificate: one with a byte after it isn't read (review round 1 on #853, N1)
    assert ts.fingerprints(der + b"\n") == []
    # OpenSSL's TRUSTED CERTIFICATE: the certificate, then its trust settings (an empty SEQUENCE)
    trusted = base64.encodebytes(der + b"0\x00").decode()
    block = f"-----BEGIN TRUSTED CERTIFICATE-----\n{trusted}-----END TRUSTED CERTIFICATE-----\n"
    assert ts.fingerprints(block.encode()) == list(ts.DISTRUSTED)
    certs, hashed, elsewhere = tmp_path / "certs", tmp_path / "hashed", tmp_path / "elsewhere"
    for path in (certs, hashed, elsewhere):
        path.mkdir()
    (certs / "emSign.cer").write_bytes(der)
    (certs / "trailing.cer").write_bytes(der + b"\n")  # not read, and the report says so
    out = run(certs)
    assert out.returncode == 1, out
    assert f"  DISTRUSTED emSign ECC Root CA - G3: {certs / 'emSign.cer'}\n" in out.stdout
    assert f"  not read, no certificate in it: {certs / 'trailing.cer'}\n" in out.stdout
    assert out.stdout.startswith(f"{certs}: 1 bundles, 1 certificates\n"), out.stdout
    (elsewhere / "emsign-root").write_text(EMSIGN_ECC_G3)
    clean = Path(certifi.where())
    (hashed / "certifi.pem").symlink_to(clean)
    (hashed / "0a1b2c3d.0").symlink_to(hashed / "certifi.pem")
    (hashed / "c1a2b3d4.0").symlink_to(elsewhere / "emsign-root")
    (hashed / "c1a2b3d4.r0").write_text(EMSIGN_ECC_G3)  # a CRL's name: not a certificate's
    out = run(hashed)
    assert out.returncode == 1, out
    count = len(ts.fingerprints(clean.read_bytes())) + 1
    assert out.stdout.startswith(f"{hashed}: 2 bundles, {count} certificates\n"), out.stdout
    assert f"  DISTRUSTED emSign ECC Root CA - G3: {hashed / 'c1a2b3d4.0'}\n" in out.stdout


def test_the_pinned_uvs_lack_the_root():
    """#816: uv 0.13.4 dropped emSign ECC Root CA - G3 from its built-in roots (DECISIONS,
    verification log): the admin server's pin is at least that, and the image's smoke test
    requires it of the VMs' uv, through which `!rollout` installs the bridge."""
    deploy = (ROOT / "workshop" / "adminserver" / "deploy.sh").read_text()
    pinned = re.search(r"^UV_VERSION=(\d+)\.(\d+)\.(\d+)$", deploy, re.M)
    assert pinned and tuple(map(int, pinned.groups())) >= (0, 13, 4)
    build = BUILD_SH.read_text()
    floor = 'test "$(printf "0.13.4\\n%s\\n" "$v" | sort -V | head -n1)" = 0.13.4\''
    assert re.search(
        r"^UV_FLOOR_OK='v=\$\(~/\.local/bin/uv --version .*" + re.escape(floor), build, re.M
    )
    # step 4 installs one where there is none or an older one (review round 1 on #853, N3)
    step4 = build[build.index("# --- 4. uv") : build.index('log "python venv ~/venv"')]
    assert 'if ! as_agent bash -c "$UV_FLOOR_OK"; then\n  as_agent bash -c \'curl ' in step4
    smoke = build.split("# --- 13. smoke test")[1]
    assert '\ncheck as_agent bash -c "$UV_FLOOR_OK"\n' in smoke
    # deploy.sh's install_uv checks the binary's version too (review round 1 on #853, N4)
    install = deploy[
        deploy.index("install_uv() {") : deploy.index("\n}\n", deploy.index("install_uv() {"))
    ]
    assert 'case $("$UV.new" --version 2>/dev/null) in' in install
    assert install.index('"$UV.new" --version') < install.index('mv -f -T -- "$UV.new" "$UV"')
    # die whatever rm does (review round 2 on #853, N6): before `&&`, a failed rm skipped die
    # and set -e, and the wrong uv was installed; before `;`, set -e exits without die's message
    assert '*) rm -f -- "$UV.new" || :; die "uv $UV_VERSION: the pinned tarball' in install
