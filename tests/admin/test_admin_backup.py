"""Encrypted backups and recovery (docs/ADMIN.md §1.7; #355, PR 13): the bots of `botkit` with
the real `Runner`, fake scripts, and the real `age` (the tests that need it skip without it:
`apt install age`). Keys are made here, in temporary directories."""

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from zab.admin import backup
from zab.admin import runner as runner_mod
from zab.admin.audit import verify
from zab.admin.bot import StartupError
from zab.admin.plans import DONE, PENDING

from .botkit import DEE, DREW, HARVARD, HARVARD_CH, RECIPIENT, make_site
from .test_admin_runner import approve, fake, records, request, settle, start

AGE, AGE_KEYGEN = shutil.which("age"), shutil.which("age-keygen")
needs_age = pytest.mark.skipif(AGE is None, reason="`age` is not installed (apt install age)")
X25519 = "age1zsee9qhslsf2tk8ptcts5kp3rtycaagl9z6xdwee3gdcmqwawu2qz38v4n"  # age-keygen's
PEM = "-----BEGIN {0} PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0B\n-----END {0} PRIVATE KEY-----\n"
LAUNCH = """\
    if not DRY:
        out = pathlib.Path({out!r})
        (out / "known_hosts").write_text("198.51.100.3 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK\\n")
        roster = pathlib.Path({roster!r})
        roster.write_text(roster.read_text().replace("carol-gh,,", "carol-gh,198.51.100.3,x"))
    print("launched carol")
"""
IMPORT = """\
    if not DRY:
        src = pathlib.Path(sys.argv[sys.argv.index("--input") + 1])
        dst = pathlib.Path(sys.argv[sys.argv.index("--roster") + 1])
        pathlib.Path(str(dst) + ".bak").write_bytes(dst.read_bytes())
        dst.write_bytes(src.read_bytes())
    print("roster: 1 row added")
"""


def run(argv, **kwargs):
    """A process, by its argument list (sync: ruff ASYNC221)."""
    return subprocess.run(argv, capture_output=True, check=False, **kwargs)  # noqa: S603


@pytest.fixture
def site(tmp_path, monkeypatch):
    site = make_site(tmp_path)
    layout = replace(site.paths.layout, release=str(tmp_path / "release"), python=sys.executable)
    site.paths = replace(site.paths, layout=layout, age=AGE or str(tmp_path / "no-age"))
    monkeypatch.setattr(runner_mod, "PROGRESS", 0.05)
    out = site.tmp / "ws" / "harvard-2026" / "out"
    roster = site.tmp / "ws" / "harvard-2026" / "roster.csv"
    fake(site, "aws/power.py", 'print("alice: stopped")')
    fake(site, "admin/status.py", 'print("{}")')
    fake(site, "aws/launch.py", LAUNCH.format(out=str(out), roster=str(roster)))
    fake(site, "admin/scan_hosts.py", 'print("scanned")')
    fake(site, "admin/roster_import.py", IMPORT)
    return site


@pytest.fixture
async def server(site):
    srv = await start(site)
    yield srv
    await srv.close()


@pytest.fixture
def key(site):
    """A deployer's ssh-ed25519 key, and with age-keygen an X25519 one, in backup_recipients."""
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is not installed")
    path = site.tmp / "laptop" / "backup"
    path.parent.mkdir(mode=0o700)
    keygen = shutil.which("ssh-keygen")
    run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", "dee@laptop", "-f", str(path)])
    lines = [path.with_suffix(".pub").read_text().strip()]
    if AGE_KEYGEN:
        made = run([AGE_KEYGEN, "-o", str(path) + ".age"])
        lines.append(made.stderr.decode().split("Public key: ")[1].strip())
    Path(site.paths.recipients).write_text("# deployers\n" + "\n".join(lines) + "\n")
    return path


def der(tag, *parts):
    """A DER element."""
    body = b"".join(parts)
    size = bytes([len(body)]) if len(body) < 128 else b"\x82" + len(body).to_bytes(2, "big")
    return bytes([tag]) + size + body


INT0, INT1, INT3 = der(2, b"\x00"), der(2, b"\x01"), der(2, b"\x03")
ALG = der(0x30, der(6, bytes.fromhex("2a864886f70d010101")), der(5))  # rsaEncryption
DER_KEYS = {  # the shapes of private keys
    "pkcs1.der": der(0x30, INT0, der(2, b"\x00\xc3" + bytes(127)), der(2, b"\x01\x00\x01")),
    "pkcs8.der": der(0x30, INT0, ALG, der(4, der(0x30, INT0))),
    "pkcs8v2.der": der(0x30, INT1, ALG, der(4, bytes(34))),
    "sec1.der": der(0x30, INT1, der(4, bytes(32)), der(0xA0, der(6, b"\x2a\x86"))),
    "encrypted.der": der(0x30, der(0x30, der(6, b"\x2a\x86\x48"), der(0x30)), der(4, bytes(48))),
    "bundle.p12": der(0x30, INT3, der(0x30, der(6, b"\x2a\x86\x48"))),
}
DER_KEPT = {  # and of what is public: a certificate, a public key
    "cert.der": der(0x30, der(0x30, der(0xA0, der(2, b"\x02")), der(2, b"\x07"), ALG), ALG),
    "pub.der": der(0x30, ALG, der(3, b"\x00" + bytes(32))),
}


def decoys(site):
    """`ws/` as careless scripts and hands could leave it: keys under every name and in every
    format, links, a hard-linked pair, a FIFO, a key directory, an export, a retired workshop
    and a linked one; and what belongs in a backup beside them."""
    ws, out = site.tmp / "ws", site.tmp / "ws" / "harvard-2026" / "out"
    fleet_key = Path(site.paths.ssh_key)
    fleet_key.parent.mkdir(mode=0o700)
    fleet_key.write_text(PEM.format("OPENSSH"))
    files = {
        "alice/alice-claude.zuliprc": "[api]\nkey=" + "k" * 32 + "\n",  # §1.7: kept
        "alice/id_ed25519": PEM.format("OPENSSH"),
        "alice/id_ed25519.pub": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK alice\n",
        "bob/deploy_rsa": "not even a key\n",
        "bob/zab_ecdsa_sk": "nor this\n",
        "notes.txt": "pasted by mistake:\n" + PEM.format("RSA"),
        "carol.ppk": "PuTTY-User-Key-File-3: ssh-ed25519\n",
        "keys/fleet": "anything\n",
        ".ssh/config": "Host *\n",
        "exports/export-alice.tar.gz": "a participant's work\n",
        "spares.json": "{}\n",
        "ssh2.txt": "---- BEGIN SSH2 ENCRYPTED PRIVATE KEY ----\nP2/56w==\n",  # RFC 4716
        "plain-a": "two names, one file\n",
    }
    for name, text in files.items():
        (out / name).parent.mkdir(mode=0o700, exist_ok=True)
        (out / name).write_text(text)
    for name, data in (DER_KEYS | DER_KEPT).items():
        (out / "der").mkdir(mode=0o700, exist_ok=True)
        (out / "der" / name).write_bytes(data)
    (out / "sub").mkdir(mode=0o700)
    os.link(out / "plain-a", out / "sub" / "plain-b")  # a hard link (the review of #373)
    os.mkfifo(out / "pipe")
    (out / "fleet-key").symlink_to(fleet_key)
    (out / "linked").symlink_to(site.tmp / "etc")
    for retired in ("old-2025", "keys"):  # retired: no longer in admin.toml (§11.5)
        (ws / retired / "out").mkdir(mode=0o700, parents=True)
        (ws / retired / "roster.csv").write_text(f"slug\n{retired}-bob\n")
        (ws / retired / "out" / "bob-claude.zuliprc").write_text("[api]\nkey=" + "r" * 32 + "\n")
    (site.tmp / "elsewhere").mkdir()
    (site.tmp / "elsewhere" / "roster.csv").write_text("slug\n")
    (ws / "linked-ws").symlink_to(site.tmp / "elsewhere")
    Path(site.paths.layout.retired).write_text("dave\n")
    return out


LEFT = {
    "ws/harvard-2026/out/.ssh: a key directory",
    "ws/harvard-2026/out/alice/id_ed25519: named like an SSH private key",
    "ws/harvard-2026/out/bob/deploy_rsa: named like an SSH private key",
    "ws/harvard-2026/out/bob/zab_ecdsa_sk: named like an SSH private key",
    "ws/harvard-2026/out/carol.ppk: named like an SSH private key",
    "ws/harvard-2026/out/exports: participants' exports",
    "ws/harvard-2026/out/fleet-key: a symbolic link",
    "ws/harvard-2026/out/keys: a key directory",
    "ws/harvard-2026/out/linked: a symbolic link",
    "ws/harvard-2026/out/notes.txt: holds a private key",
    "ws/harvard-2026/out/ssh2.txt: holds a private key",
    "ws/harvard-2026/out/pipe: not a regular file",
    *(f"ws/harvard-2026/out/der/{name}: holds a private key" for name in DER_KEYS),
    "ws/linked-ws: a symbolic link",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def expected(site):
    """Each file a backup must hold, by its name there, with its SHA-256 on the server: every
    regular file under `ws/`, but those `LEFT` names."""
    retired = Path(site.paths.layout.retired)
    found = {"retired-slugs.txt": sha(retired.read_bytes())} if retired.exists() else {}
    gone = [entry.split(": ")[0] for entry in LEFT]
    for path in (site.tmp / "ws").rglob("*"):  # never through a link
        name = "ws/" + path.relative_to(site.tmp / "ws").as_posix()
        out = any(name == g or name.startswith(g + "/") for g in gone)
        if path.is_file() and not path.is_symlink() and not out:
            found[name] = sha(path.read_bytes())
    return found


def plaintext(server):
    """The backup's plaintext tar and its `Made`, written in a thread that must end within 10 s
    (a FIFO opened without O_NONBLOCK never would); and GNU tar must read it cleanly."""
    sink, made = io.BytesIO(), backup.Made("x")
    thread = threading.Thread(target=backup.write, args=(sink, server, made), daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(10)
    assert not thread.is_alive() and time.monotonic() - started < 10, "the tar never ended"
    listed = run([shutil.which("tar"), "-tzf", "-"], input=sink.getvalue())
    assert listed.returncode == 0 and not listed.stderr, listed.stderr
    return sink.getvalue(), made


def _log(site, data):
    """`data`, an audit log as a backup holds it, as a file to `verify`."""
    path = site.tmp / "laptop" / "audit.jsonl"
    path.write_bytes(data)
    return path


def unpack(data):
    """name → bytes of every file of a gzipped tar, and the names of its directories. Every
    member is read: `retired-slugs.txt` and the audit log, when there is one, come last."""
    files, dirs = {}, set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
        last = [n for n in ("retired-slugs.txt", "audit.jsonl") if n in names]
        assert names[len(names) - len(last) :] == last
        for member in tar.getmembers():
            assert member.type in (tarfile.REGTYPE, tarfile.DIRTYPE), member.name  # no link
            if member.isfile():
                files[member.name] = tar.extractfile(member).read()
            else:
                dirs.add(member.name)
    return files, dirs


def dms(site, by=HARVARD):
    return [p for p in site.realm.posts(by) if p["type"] == "private"]


def topic(site, by=HARVARD):
    return [p["content"] for p in site.realm.posts(by) if p["type"] == "stream"]


def decrypt(site, key, data, where):
    """ADMIN §1.7's first step, as the README gives it: `age -d -i KEY -o backup.tar.gz FILE`."""
    where.mkdir(exist_ok=True)
    (where / "backup.tar.gz.age").write_bytes(data)
    done = run([AGE, "-d", "-i", str(key), "-o", "backup.tar.gz", "backup.tar.gz.age"], cwd=where)
    assert done.returncode == 0, done.stderr
    return (where / "backup.tar.gz").read_bytes()


async def launch(srv):
    plan = await request(srv, "!launch carol")
    assert plan.state == PENDING, plan.note
    await approve(srv, plan)
    await settle(srv)
    assert srv.store.get(plan.id).state == DONE
    return plan


# ------------------------------------------------------------------------------ recipients


def _ssh(blob: bytes, comment="") -> str:
    return f"ssh-ed25519 {base64.b64encode(blob).decode()}{comment}"


ED = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"


@pytest.mark.parametrize(
    ("text", "keys"),
    [
        (f"{RECIPIENT} dee@laptop\n", (RECIPIENT,)),
        (f"# deployers\n\n  {X25519}  \r\n{RECIPIENT}\n", (X25519, RECIPIENT)),
        (_ssh(ED + bytes(32), "\twith a tab"), (_ssh(ED + bytes(32)),)),
    ],
)
def test_recipients_are_read_as_age_takes_them(text, keys):
    assert backup.recipients(text) == keys


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "holds no key"),
        ("# only a comment\n\n", "holds no key"),
        ("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ drew@laptop", "line 1: not an ssh-ed25519"),
        ("sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29t", "line 1"),
        ("age1yubikey1" + "q" * 50, "line 1"),  # a plugin's: age would run its binary
        ("age1" + "b" * 58, "line 1"),  # not bech32
        (X25519[:-1] + "5", "line 1"),  # a wrong checksum, which age would refuse at a backup
        (_ssh(b"\x00\x00\x00\x07ssh-rsa" + bytes(40)), "line 1"),  # another key's blob
        (_ssh(ED + bytes(31) + b"\x00", "") + "=", "line 1"),
        (f"{RECIPIENT}\n{RECIPIENT} again", "line 2: a key given twice"),
        (f"--recipient {RECIPIENT}", "line 1"),
        (f"{RECIPIENT}\n-R /etc/shadow", "line 2"),
    ],
)
def test_anything_else_in_backup_recipients_is_refused(text, why):
    with pytest.raises(backup.BackupError, match=why):
        backup.recipients(text)


def damage(site, how):
    path = Path(site.paths.recipients)
    if how == "missing":
        path.unlink()
    elif how == "writable":
        path.chmod(0o666)
    else:
        path.write_text("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ drew@laptop\n")


@pytest.mark.parametrize("how", ["missing", "writable", "rsa"])
async def test_the_bots_dont_start_without_good_recipients(site, how):
    damage(site, how)
    srv = site.server()
    with pytest.raises(StartupError, match="no backups could be made"):
        await srv.start()
    await srv.close()


# ------------------------------------------------------------------------- what it holds


async def test_the_tar_holds_the_state_and_no_private_key(site, server):
    """Without `age`: the plaintext tar as `make` streams it, decoys in `out/`."""
    decoys(site)
    await server.audit("read", workshop="harvard-2026", verb="status")
    data, made = await asyncio.to_thread(plaintext, server)
    files, dirs = unpack(data)
    assert set(made.left) == LEFT
    audit = files.pop("audit.jsonl")
    assert audit == site.audit.read_bytes() and made.head == verify(site.audit).head
    assert {name: sha(data) for name, data in files.items()} == expected(site)
    assert not any(b"PRIVATE KEY" in data or b"PuTTY" in data for data in files.values())
    assert made.files == len(files) + 1
    assert {"ws", "ws/harvard-2026", "ws/harvard-2026/out", "ws/harvard-2026/out/alice"} <= dirs
    assert {"ws/old-2025/out", "ws/keys/out"} <= dirs  # retired workshops, whatever their name
    assert not {d for d in dirs if re.search(r"out/(keys|\.ssh|exports)|linked", d)}
    same = files["ws/harvard-2026/out/plain-a"]
    assert files["ws/harvard-2026/out/sub/plain-b"] == same == b"two names, one file\n"
    assert {f"ws/harvard-2026/out/der/{name}" for name in DER_KEPT} <= set(files)


def nest(site):
    """harvard-2026's roster and out/ one directory down, `data/`, as admin.toml may put them."""
    ws = site.tmp / "ws" / "harvard-2026"
    (ws / "data").mkdir()
    for name in ("roster.csv", "out"):
        (ws / name).rename(ws / "data" / name)
    (ws / "data" / "out" / "known_hosts").write_text("198.51.100.1 ssh-ed25519 AAAA\n")
    text = site.config_path.read_text()
    for name in ("roster.csv", "out"):
        text = text.replace(f"/harvard-2026/{name}", f"/harvard-2026/data/{name}")
    site.config_path.write_text(text)


async def test_the_paths_are_the_workshops_own(site):
    """Codex round 1: a roster and out/ in a subdirectory keep it in the backup, so a restore
    puts them where admin.toml says."""
    nest(site)
    data, _ = await asyncio.to_thread(plaintext, site.server())
    files, dirs = unpack(data)
    assert {name: sha(data) for name, data in files.items()} == expected(site)
    assert {"ws/harvard-2026/data/roster.csv", "ws/harvard-2026/data/out/known_hosts"} <= set(files)
    assert {"ws/harvard-2026", "ws/harvard-2026/data", "ws/harvard-2026/data/out"} <= dirs


async def test_ws_itself_a_symbolic_link_is_named_and_never_followed(site):
    """#375 (the approval of #373): the workshops' root, a link, is left out by name, as `a
    symbolic link`, and nothing under it is read, though the directory it names is full."""
    ws = Path(site.paths.workshops)
    await asyncio.to_thread(ws.rename, site.tmp / "elsewhere")
    await asyncio.to_thread(ws.symlink_to, site.tmp / "elsewhere")
    data, made = await asyncio.to_thread(plaintext, site.server())
    files, dirs = unpack(data)
    assert made.left == ["ws: a symbolic link"]
    assert not [n for n in [*files, *dirs] if n == "ws" or n.startswith("ws/")]


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_der_private_keys_are_found_and_public_ones_kept(tmp_path):
    """Real keys, made by openssl: every private key's DER is found, whatever its format, and
    neither a certificate nor a public key is taken for one."""

    def ssl(*args):
        done = run([shutil.which("openssl"), *args], cwd=tmp_path)
        assert done.returncode == 0, done.stderr

    ssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:1024", "-out", "rsa.pem")
    ssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", "ec.pem")
    ssl("genpkey", "-algorithm", "ed25519", "-outform", "DER", "-out", "ed25519.der")
    ssl("pkey", "-in", "rsa.pem", "-outform", "DER", "-out", "pkcs8.der")
    ssl("rsa", "-in", "rsa.pem", "-traditional", "-outform", "DER", "-out", "pkcs1.der")
    ssl("ec", "-in", "ec.pem", "-outform", "DER", "-out", "ec.der")
    ssl("pkcs8", "-topk8", "-in", "rsa.pem", "-v2", "aes-256-cbc", "-passout", "pass:x",
        "-outform", "DER", "-out", "encrypted.der")  # fmt: skip
    ssl("req", "-x509", "-new", "-key", "rsa.pem", "-subj", "/CN=x", "-days", "1", "-out", "c.pem")
    ssl("pkcs12", "-export", "-inkey", "rsa.pem", "-in", "c.pem", "-passout", "pass:x",
        "-out", "bundle.p12")  # fmt: skip
    ssl("x509", "-in", "c.pem", "-outform", "DER", "-out", "cert.der")
    ssl("pkey", "-in", "rsa.pem", "-pubout", "-outform", "DER", "-out", "pub.der")
    ssl("rsa", "-in", "rsa.pem", "-RSAPublicKey_out", "-outform", "DER", "-out", "rsapub.der")
    found = {p.name: backup._der_key(p.read_bytes()) for p in tmp_path.iterdir()}
    private = {"ed25519.der", "pkcs8.der", "pkcs1.der", "ec.der", "encrypted.der", "bundle.p12"}
    assert {name for name, key in found.items() if key} == private


@pytest.mark.parametrize("name", sorted(DER_KEYS))
def test_each_shape_of_a_der_key_is_found(name):
    assert backup._der_key(DER_KEYS[name])
    assert not backup._der_key(DER_KEYS[name] + b"\n")  # one element, the whole file


@pytest.mark.parametrize("name", sorted(DER_KEPT))
def test_public_der_is_kept(name):
    assert not backup._der_key(DER_KEPT[name])


async def test_a_large_file_is_left_out_and_named(site, server, monkeypatch):
    monkeypatch.setattr(backup, "MAX_FILE", 10)
    data, made = await asyncio.to_thread(plaintext, server)
    assert "ws/harvard-2026/roster.csv: larger than 0 MiB" in made.left
    assert "ws/harvard-2026/roster.csv" not in unpack(data)[0]


# ------------------------------------------------------------------------------- the flow


@needs_age
async def test_a_launch_is_backed_up_to_every_deployer(site, key):
    """A backup follows `!launch`: one upload, the ciphertext alone, DM'd to each deployer, the
    one who isn't subscribed to the channel too; decrypted, it holds the roster, `out/` and the
    audit log as the server has them, compared by SHA-256, and no private key."""
    decoys(site)
    srv = await start(site)
    try:
        before = {p for p in site.tmp.rglob("*")}
        uploads = len(site.realm.uploads)
        plan = await launch(srv)
    finally:
        await srv.close()
    assert DEE not in site.realm.subscribers[HARVARD_CH]
    assert sorted(json.loads(p["to"])[0] for p in dms(site)) == [DREW, DEE]
    [(name, data)] = list(site.realm.uploads.items())[uploads:]
    assert re.fullmatch(r"7/ab/\d+/backup-\d{8}T\d{6}Z\.tar\.gz\.age", name)
    assert data.startswith(b"age-encryption.org/v1\n-> ssh-ed25519 ")
    assert b"carol" not in data and b"PRIVATE" not in data
    text = dms(site)[0]["content"]
    assert f"after **{plan.name}** · `!launch carol` (harvard-2026): [" in text
    assert f"/user_uploads/{name})" in text and "`ws/harvard-2026/out/notes.txt:" in text
    record = records(site, "backup")[-1]
    assert record["outcome"] == "sent" and record["to"] == [DREW, DEE]
    assert record["sha256"] == sha(data) and record["plan"] == plan.name
    assert set(record["left_out"]) == LEFT
    assert topic(site)[-1].startswith(f"✅ **{plan.name}**")  # no failure said
    plain = decrypt(site, key, data, site.tmp / "laptop")
    listed = run([shutil.which("tar"), "-tzf", str(site.tmp / "laptop" / "backup.tar.gz")])
    assert listed.returncode == 0 and not listed.stderr  # the hard link is a whole file
    files, _ = unpack(plain)
    audit = files.pop("audit.jsonl")
    assert {n: sha(d) for n, d in files.items()} == expected(site)
    assert b"198.51.100.3" in files["ws/harvard-2026/roster.csv"]  # as the launch left it
    assert site.audit.read_bytes().startswith(audit)  # through the head the DM names
    assert record["holds"] == str(verify(_log(site, audit)).head)
    assert f"the audit log through `{record['holds']}`" in text
    assert not any(b"PRIVATE KEY" in d or b"PuTTY" in d for d in files.values())
    # nothing left behind: what is new is the launch's, the run's and the laptop's
    new = {p.relative_to(site.tmp).as_posix() for p in site.tmp.rglob("*")} - {
        p.relative_to(site.tmp).as_posix() for p in before
    }
    assert {n for n in new if not re.match(r"(state/(runs|tmp|audit\.jsonl)|laptop)\b", n)} == {
        "ws/harvard-2026/out/known_hosts",
        "calls.jsonl",
    }
    assert not any(site.tmp.joinpath("state", "tmp").iterdir())
    if AGE_KEYGEN:  # the X25519 key decrypts it too
        assert unpack(decrypt(site, str(key) + ".age", data, site.tmp / "laptop2"))[0]


@needs_age
async def test_a_roster_write_is_backed_up(site, key):
    srv = await start(site)
    try:
        csv = b"slug,first_name,email\nalice,Alice,alice@example.com\nzoe,Zoe,zoe@example.com\n"
        path = site.realm.upload("new-roster.csv", csv)
        plan = await request(srv, f"!roster import [new-roster.csv]({path})")
        assert plan.state == PENDING, plan.note
        await approve(srv, plan)
        await settle(srv)
        assert srv.store.get(plan.id).state == DONE
    finally:
        await srv.close()
    data = site.realm.uploads[re.search(r"/user_uploads/([^)]+)\)", dms(site)[-1]["content"])[1]]
    files, _ = unpack(decrypt(site, key, data, site.tmp / "laptop"))
    assert files["ws/harvard-2026/roster.csv"] == csv
    assert files["ws/harvard-2026/roster.csv.bak"].startswith(b"slug,first_name,last_name")


@pytest.mark.parametrize("writes", ["nothing", "an export"])
async def test_an_apply_that_changes_nothing_makes_no_backup(site, server, writes):
    """Nothing a backup holds changes: no backup, whatever the apply wrote elsewhere."""
    if writes == "an export":
        exports = site.tmp / "ws" / "harvard-2026" / "out" / "exports"
        exports.mkdir(mode=0o700)
        body = f'if not DRY: pathlib.Path({str(exports / "a.tar.gz")!r}).write_text("x")'
        fake(site, "aws/power.py", body)
    plan = await request(server, "!stop alice")
    await approve(server, plan)
    await settle(server)
    assert server.store.get(plan.id).state == DONE
    assert records(site, "backup") == [] and dms(site) == []


async def test_a_failed_backup_is_said_and_never_blocks(site):
    """Without `age` (`botkit`'s, or the site's when it isn't installed): the apply is done and
    its result posted; then the failure, in the topic, and its record. The next apply makes a
    backup whatever it changed."""
    site.paths = replace(site.paths, age=str(site.tmp / "no-age"))
    srv = await start(site)
    try:
        plan = await launch(srv)
        result, said = topic(site)[-2:]
        assert result.startswith(f"✅ **{plan.name}** · `!launch carol`: done")
        assert said.startswith(f"⚠ **{plan.name}**: the encrypted backup after it failed: age")
        assert "No such file or directory" in said and "The apply is unaffected" in said
        record = records(site, "backup")[-1]
        assert record["outcome"] == "failed" and record["reason"].startswith("age can't be run")
        assert said.endswith(f"`{verify(site.audit).head}`")
        assert dms(site) == [] and site.realm.uploads == {}
        stop = await request(srv, "!stop alice")  # changes nothing, but the last one failed
        await approve(srv, stop)
        await settle(srv)
        assert srv.store.get(stop.id).state == DONE
        assert [r["plan"] for r in records(site, "backup")] == [plan.name, stop.name]
    finally:
        await srv.close()


#: An `age` that does the job's shape without the cryptography: a header, a nonce, the input
#: and a tag per 64 KiB chunk, so that the tests of what follows a sent backup run without age.
LIKE_AGE = (
    "data = sys.stdin.buffer.read()\n"
    "out.write(HEAD + bytes(16) + data + bytes(16 * max(1, -(-len(data) // 65536))))"
)


def fake_age(site, body):
    path = site.tmp / "bin" / "age"
    path.parent.mkdir(exist_ok=True)
    head = "import sys\nout = sys.stdout.buffer\nHEAD = b'age-encryption.org/v1\\n'\n"
    path.write_text(f"#!{sys.executable}\n{head}{body}\n")
    path.chmod(0o755)
    return str(path)


async def owed(site):
    """Whether a freshly started server owes a backup."""
    srv = await start(site)
    try:
        return srv.backups.failed
    finally:
        await srv.close()


async def test_a_backup_owed_survives_a_restart_and_a_crash(site):
    """Codex rounds 3 and 4: at start a backup is owed unless the files are as the last sent
    backup recorded them, whatever happened since: a failed backup, a crash mid-apply, which
    writes no `apply_end`. Nothing changed, nothing owed."""
    site.paths = replace(site.paths, age=fake_age(site, LIKE_AGE))
    srv = await start(site)
    try:
        await launch(srv)
    finally:
        await srv.close()
    sent = records(site, "backup")[-1]
    assert sent["outcome"] == "sent" and re.fullmatch(r"[0-9a-f]{64}", sent["state"])
    assert not await owed(site)  # nothing changed since
    # a crash mid-apply: its step changed out/, and the log ends at its apply_start
    spares = site.tmp / "ws" / "harvard-2026" / "out" / "spares.json"
    spares.write_text('{"crashed": "mid-apply"}\n')
    srv = await start(site)
    try:
        assert srv.backups.failed
        await srv.audit("apply_start", workshop="harvard-2026", plan="P-90", verb="launch")
        stop = await request(srv, "!stop alice")  # changes nothing itself
        await approve(srv, stop)
        await settle(srv)
        assert records(site, "backup")[-1]["plan"] == stop.name
        assert records(site, "backup")[-1]["outcome"] == "sent"
    finally:
        await srv.close()
    assert not await owed(site)
    # a failed backup: still owed after a restart
    site.paths = replace(site.paths, age=str(site.tmp / "no-age"))
    spares.write_text('{"changed": "again"}\n')
    srv = await start(site)
    try:
        assert srv.backups.failed
        stop = await request(srv, "!stop alice", mid=5001)
        await approve(srv, stop)
        await settle(srv)
        assert records(site, "backup")[-1]["outcome"] == "failed"
    finally:
        await srv.close()
    assert await owed(site)


async def test_applies_and_no_backup_ever_sent_owe_one_and_neither_owes_none(site):
    """#375 (the approval of #373), the restart rule's two cases without a sent backup: a log
    with an `apply_start` owes a backup after a restart, and a log without one owes none. The
    apply's `apply_end` comes after it, as a finished apply writes it: an `apply_start`
    anywhere in the log counts, not only as its last record (review of #579)."""
    assert not await owed(site)  # a fresh server: no apply, no backup
    srv = await start(site)
    try:
        await srv.audit("apply_start", workshop="harvard-2026", plan="P-90", verb="launch")
        await srv.audit("apply_end", workshop="harvard-2026", plan="P-90", verb="launch")
    finally:
        await srv.close()
    assert records(site, "backup") == []
    assert await owed(site)


async def test_recipients_others_can_write_fail_the_backup_not_the_apply(site, server):
    damage(site, "writable")
    plan = await launch(server)
    assert "backup_recipients is writable by group or others" in topic(site)[-1]
    assert records(site, "backup")[-1]["outcome"] == "failed"
    assert server.store.get(plan.id).state == DONE


FAKE_AGE = {  # (its body, what the failure says): the review of #373
    "dies mid-stream": ("sys.stdin.buffer.read(4096); out.write(HEAD); exit(1)", "age exited 1"),
    "fails after reading it all": (
        "out.write(HEAD + bytes(32) + sys.stdin.buffer.read() + bytes(64)); sys.exit(1)",
        "age exited 1",
    ),
    "stops reading": (
        "out.write(HEAD + bytes(64))",
        "age (stopped reading the backup|'s output is not the whole)",
    ),
    "writes too little": (
        "sys.stdin.buffer.read(); out.write(HEAD + bytes(40))",
        "age's output is not the whole backup's ciphertext",
    ),
    # #375: as long as `LIKE_AGE`'s output, but no header
    "writes no header": (
        LIKE_AGE.replace("HEAD + ", "bytes(len(HEAD)) + "),
        "age's output is not the whole backup's ciphertext",
    ),
}


@pytest.mark.parametrize("how", sorted(FAKE_AGE))
async def test_an_age_that_fails_fails_the_backup(site, how):
    """A fake `age`, over more than a pipe's worth of input: the failure is said and recorded,
    nothing is uploaded or sent, and nothing is left behind."""
    body, said = FAKE_AGE[how]
    (site.tmp / "ws" / "harvard-2026" / "out" / "noise").write_bytes(os.urandom(256 * 1024))
    site.paths = replace(site.paths, age=fake_age(site, body))
    before = {p.relative_to(site.tmp).as_posix() for p in site.tmp.rglob("*")}
    srv = await start(site)
    try:
        plan = await launch(srv)
    finally:
        await srv.close()
    assert re.match(rf"⚠ \*\*{plan.name}\*\*: the encrypted backup after it failed: {said}",
                    topic(site)[-1]), topic(site)[-1]  # fmt: skip
    record = records(site, "backup")[-1]
    assert record["outcome"] == "failed" and re.match(said, record["reason"])
    assert site.realm.uploads == {} and dms(site) == []
    new = {p.relative_to(site.tmp).as_posix() for p in site.tmp.rglob("*")} - before
    ours = r"state/(runs|tmp|audit\.jsonl|state\.db)\b"  # the bot's own
    left = {n for n in new if not re.match(ours, n)}
    assert left == {"ws/harvard-2026/out/known_hosts", "calls.jsonl"}
    assert not [t for t in threading.enumerate() if isinstance(t, backup._Drain)]  # all read


async def test_a_backup_that_raises_never_reaches_the_apply(site, server, monkeypatch, caplog):
    async def broken(job):
        raise RuntimeError("anything")

    monkeypatch.setattr(server.backups, "send", broken)
    plan = await launch(server)
    assert topic(site)[-1].startswith(f"✅ **{plan.name}** · `!launch carol`: done")
    assert f"{plan.name}: the backup failed (RuntimeError)" in caplog.text


@needs_age
async def test_a_failed_upload_or_dm_is_said(site, key, server, monkeypatch):
    bot = next(b for b in server.bots if b.user_id == HARVARD)

    async def refused(name, data):
        raise RuntimeError("413")

    upload, send = bot.client.upload, bot.send
    monkeypatch.setattr(bot.client, "upload", refused)
    await launch(server)
    assert "the upload failed (RuntimeError)" in topic(site)[-1] and dms(site) == []
    record = records(site, "backup")[-1]
    assert record["outcome"] == "failed" and record["bytes"] > 0 and record["to"] == []
    monkeypatch.setattr(bot.client, "upload", upload)

    async def not_to_dee(**params):
        return None if params.get("to") == [DEE] else await send(**params)

    monkeypatch.setattr(bot, "send", not_to_dee)
    stop = await request(server, "!stop alice")  # the last one failed: this one tries again
    await approve(server, stop)
    await settle(server)
    assert topic(site)[-1].startswith(f"⚠ **{stop.name}**: the encrypted backup after it failed")
    assert f"not sent to user {DEE}" in topic(site)[-1]
    assert records(site, "backup")[-1]["to"] == [DREW]


@needs_age
async def test_a_backup_over_the_upload_limit_is_refused(site, key, server):
    next(b for b in server.bots if b.user_id == HARVARD).client.limits[
        "max_file_upload_size_mib"
    ] = 0
    await launch(server)
    assert "it is over 0 MiB, the upload limit" in topic(site)[-1]
    assert site.realm.uploads == {}


# ------------------------------------------------------------------------------- recovery


def bootstrapped(tmp_path):
    """A new server, as `deploy.sh --bootstrap` leaves it: no workshop has a file yet, its
    deploys.jsonl has its own first line, and `ws/` lies in the state directory (§1.3)."""
    (tmp_path / "new").mkdir()
    new = make_site(tmp_path / "new")
    for ws in ("harvard-2026", "zab-test"):
        shutil.rmtree(new.tmp / "ws" / ws)
    state = new.tmp
    retired = str(state / "retired-slugs.txt")
    layout = replace(new.paths.layout, runs=str(state / "runs"), retired=retired)
    new.paths = replace(new.paths, state=str(state), layout=layout)
    deploys = Path(new.paths.deploys)
    deploys.parent.mkdir(mode=0o755)
    line = {"ts": "2026-11-02T09:00:00Z", "event": "deploy", "version": "v0.3.2"}
    deploys.write_text(json.dumps(line | {"commit": "c" * 40}) + "\n")
    deploys.chmod(0o644)
    return new


def restore(new, plain, head):
    """The README's steps on the new server: `tar -xzpf ~/backup.tar.gz -C /var/lib/zab-admin`,
    then `audit verify --head SEQ:HEX`."""
    copied = new.tmp.parent / "server-home" / "backup.tar.gz"  # the SSH user's, not the state
    copied.parent.mkdir(mode=0o700)
    copied.write_bytes(plain)
    unpacked = run([shutil.which("tar"), "-xzpf", str(copied), "-C", new.paths.state])
    assert unpacked.returncode == 0, unpacked.stderr
    copied.unlink()
    argv = [sys.executable, "-m", "zab.admin", "audit", "verify", str(new.audit), "--head", head]
    return run(argv, text=True)


@needs_age
async def test_recovery_as_the_readme_gives_it(site, key, tmp_path):
    """workshop/adminserver/README.md, "Backups and recovery": decrypt the latest backup, unpack
    it over a new server's state directory, `audit verify` it against the head the DM names,
    and start the bots there. They read the restored files, and copy the new server's own
    deploys.jsonl line into the restored chain again, by digest (#344)."""
    decoys(site)
    srv = await start(site)
    try:
        await launch(srv)
    finally:
        await srv.close()
    text = dms(site)[-1]["content"]
    data = site.realm.uploads[re.search(r"/user_uploads/([^)]+)\)", text)[1]]
    plain = decrypt(site, key, data, tmp_path / "laptop")
    seq, hexa = re.search(r"audit log through `audit #(\d+) · chain ([0-9a-f]{12})`", text).groups()
    new = bootstrapped(tmp_path)
    srv = new.server()
    await srv.start()  # deploy.sh started the bots: their log has the new server's deploy
    await srv.close()
    assert [r["event"] for r in records(new)] == ["upgrade"]
    checked = restore(new, plain, f"{seq}:{hexa}")
    assert checked.returncode == 0 and checked.stdout.startswith(f"ok: {seq} records"), checked
    assert expected(new) == expected(site)  # the roster, out/, retired-slugs.txt
    out = new.tmp / "ws" / "harvard-2026" / "out"
    assert stat.S_IMODE(out.stat().st_mode) == 0o700 and not (out / "alice" / "id_ed25519").exists()
    srv = new.server()
    await srv.start()
    try:
        ws = srv.config.workshop("harvard-2026")
        assert [r.ip for r in srv.fleet(ws).rows if r.slug == "carol"] == ["198.51.100.3"]
        assert srv.ledger.floor >= 1  # no plan ID is handed out twice
    finally:
        await srv.close()
    assert records(new, "upgrade")[-1]["deploy"]["version"] == "v0.3.2"
    assert verify(new.audit, [(int(seq), hexa)]).ok
