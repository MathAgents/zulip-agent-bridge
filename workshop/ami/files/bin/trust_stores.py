#!/usr/bin/env python3
"""trust_stores.py: no CA bundle under the given paths holds a distrusted root (#671).

    trust_stores.py PATH...

Each PATH is a bundle, or a directory searched for files named `*.pem`, `*.crt` or `*.cer`, or
by OpenSSL's hashed names (`<8 hex digits>.<n>`). A linked file is read, as /etc/ssl/certs has
them, once however many names link to it; a linked directory is not followed. A bundle is PEM
(OpenSSL's TRUSTED CERTIFICATE blocks included), or exactly one DER certificate (a `.cer`,
say). A certificate is known by the SHA-256 of its DER encoding, so a bundle's comments and
labels play no part. One line per PATH: how many bundles and certificates it holds; under it, a
line for each certifi among them, with its version, one for each file of a directory read
without finding a certificate in it, and one for each bundle that holds a distrusted root.

Exit status 0 when none does; 1 when one does, or when a directory holds no bundle at all (a
check that read nothing proves nothing); 2 for a PATH that doesn't exist.

`build.sh` runs it on /etc/ssl/certs after the system packages, and in its smoke test on that and
on the certifi of every Python environment it creates; tests/workshop/test_trust_stores.py on the
certifi that uv.lock pins. Standard library only: the VM's system Python runs it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path

#: The roots that no trust store zab ships or pins may hold: the SHA-256 of the DER certificate,
#: and its name. emSign ECC Root CA - G3: eMudhra's intermediate CA compromise of October 2026,
#: whose intermediates' private keys can be recovered from their certificates. certifi 2026.10.23
#: and Ubuntu's ca-certificates 20260816ubuntu0.24.04.1 (USN-8944-1) don't have it.
DISTRUSTED = {
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b": "emSign ECC Root CA - G3",
}
SUFFIXES = (".pem", ".crt", ".cer")
#: A CApath directory's names for its certificates (`openssl rehash`, `update-ca-certificates`):
#: the subject's hash and a counter, whatever the file linked to is called (#816).
HASHED_NAME = re.compile(r"[0-9a-f]{8}\.[0-9]+")
#: A certificate, or OpenSSL's TRUSTED CERTIFICATE: the certificate's DER, then its trust settings.
PEM = re.compile(
    rb"-----BEGIN (TRUSTED )?CERTIFICATE-----([A-Za-z0-9+/=\s]*?)"
    rb"-----END (?:TRUSTED )?CERTIFICATE-----"
)


def fingerprints(data: bytes) -> list[str]:
    """The SHA-256 of each certificate of a PEM bundle, in order; a block that isn't base64 is
    skipped. A file with no PEM block that is exactly one DER certificate is that certificate."""
    out = []
    for m in PEM.finditer(data):
        try:
            der = base64.b64decode(b"".join(m.group(2).split()), validate=True)
        except binascii.Error:
            continue
        if m.group(1) and (end := certificate_end(der)) is not None:
            der = der[:end]  # the certificate, without the trust settings after it
        out.append(hashlib.sha256(der).hexdigest())
    if not out and certificate_end(data) == len(data):
        out.append(hashlib.sha256(data).hexdigest())
    return out


def certificate_end(data: bytes) -> int | None:
    """Where the DER SEQUENCE that DATA starts with ends, if it has an X.509 certificate's
    outline (a long-form length, and a SEQUENCE first, its tbsCertificate); else None."""
    if len(data) < 4 or data[0] != 0x30 or not 0x81 <= data[1] <= 0x84:
        return None
    start = 2 + (data[1] & 0x7F)  # where the SEQUENCE's contents begin
    end = start + int.from_bytes(data[2:start], "big")
    return end if start < len(data) and end <= len(data) and data[start] == 0x30 else None


def candidates(path: Path) -> Iterator[Path]:
    """PATH itself if it is a file, else the files under it with a bundle's suffix or a hashed
    name, sorted, each file read once whatever links to it."""
    if not path.is_dir():
        yield path
        return
    seen = set()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            if not (name.lower().endswith(SUFFIXES) or HASHED_NAME.fullmatch(name)):
                continue
            file = Path(root, name)
            # is_file(): a link that resolves to a file, not a dangling one
            if file.is_file() and (real := os.path.realpath(file)) not in seen:
                seen.add(real)
                yield file


def certifi_version(bundle: Path) -> str | None:
    """The version of the certifi whose `cacert.pem` this is, from its dist-info beside it."""
    if bundle.name != "cacert.pem" or bundle.parent.name != "certifi":
        return None
    for info in sorted(bundle.parent.parent.glob("certifi-*.dist-info")):
        return info.name.removeprefix("certifi-").removesuffix(".dist-info")
    return "of unknown version"


def check(paths: list[Path]) -> tuple[list[str], bool]:
    """The report's lines, and whether every PATH passed."""
    lines, ok = [], True
    for path in paths:
        found = certs = 0
        certifis, unread, bad = [], [], []
        for file in candidates(path):
            prints = fingerprints(file.read_bytes())
            if not prints:  # a key or DH parameters, say: named as a bundle, but none
                unread += [f"  not read, no certificate in it: {file}"] if path.is_dir() else []
                continue
            found += 1
            certs += len(prints)
            if (version := certifi_version(file)) is not None:
                certifis.append(f"  certifi {version}: {file}")
            bad += [f"  DISTRUSTED {DISTRUSTED[p]}: {file}" for p in prints if p in DISTRUSTED]
        lines += [f"{path}: {found} bundles, {certs} certificates", *certifis, *unread, *bad]
        if not found:
            lines.append(f"  NO BUNDLE: nothing was checked under {path}")
            ok = False
        ok = ok and not bad
    return lines, ok


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    paths = [Path(arg) for arg in argv]
    if missing := [str(p) for p in paths if not p.exists()]:
        print(f"trust_stores.py: no such file or directory: {', '.join(missing)}", file=sys.stderr)
        return 2
    lines, ok = check(paths)
    print("\n".join(lines))
    names = ", ".join(sorted(DISTRUSTED.values()))
    print(
        f"ok: no bundle holds {names}" if ok else "FAILED: the DISTRUSTED or NO BUNDLE lines above"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
