"""`workshop/admin/scan_hosts.py` (workshop/admin/README.md §0): record and pin the VMs' host
keys."""

from __future__ import annotations

import admin_common as ac
import pytest
import scan_hosts as sh

ROSTER = """github,first_name,last_name,email,repo,github_id,host,ip,zulip_user_id,notes
alice,Alice,Smith,alice@example.org,alice/thesis,1,ws-alice,203.0.113.5,101,
bob,Bob,Jones,bob@example.org,sandbox,2,ws-bob,203.0.113.6,102,
carol,Carol,Lee,carol@example.org,carol/x,3,ws-carol,,103,
"""
# A real ed25519 public key and the fingerprint `ssh-keygen -lf` prints for it.
KEY_A = "AAAAC3NzaC1lZDI1NTE5AAAAIIw1k/2uq+sR/xmp2rpvzxiv4aHSdMJQPSmgtlFJv958"
FP_A = "SHA256:tvhLNl4yO00Wj4H1lib8AsdIejwHRJSNthLmCqy6NlU"
KEY_B = "AAAAC3NzaC1lZDI1NTE5AAAAIKCMJVOcFbqMFsn/rqi0Xz4db4Uxor8qO33BGCEfpXVq"


class FakeKeyscan:
    def __init__(self, keys: dict[str, str]):
        self.keys = keys
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        self.calls.append(list(argv))
        ip = argv[-1]
        if ip not in self.keys:
            return ac.RunResult(1, "", f"getaddrinfo {ip}: Name or service not known")
        out = f"# {ip}:22 SSH-2.0-OpenSSH_9.6p1\n{ip} ssh-ed25519 {self.keys[ip]}\n"
        return ac.RunResult(0, out, f"# {ip}:22 SSH-2.0-OpenSSH_9.6p1\n")


def test_host_key_fingerprint_matches_ssh_keygen():
    assert ac.host_key_fingerprint(f"203.0.113.5 ssh-ed25519 {KEY_A}") == FP_A
    with pytest.raises(ValueError):
        ac.host_key_fingerprint("203.0.113.5 ssh-ed25519")
    with pytest.raises(ValueError):
        ac.host_key_fingerprint("203.0.113.5 ssh-ed25519 ???")


def test_scan_records_prints_fingerprints_and_refuses_changed_keys(tmp_path, monkeypatch, capsys):
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER)
    kh = tmp_path / "out" / "known_hosts"
    keyscan = FakeKeyscan({"203.0.113.5": KEY_A, "203.0.113.6": KEY_B})
    monkeypatch.setattr(sh, "subprocess_runner", keyscan)
    code = sh.main(["--roster", str(roster), "--known-hosts", str(kh)])
    assert code == 1  # carol has no ip yet
    out = capsys.readouterr()
    assert FP_A in out.out and "added" in out.out and "no IP address for 'carol'" in out.out
    assert "2 key(s) written" in out.out and "0 matched the console" in out.out
    # no host_keys in this roster: recorded, with a warning to compare by hand
    assert "not verified (no host_keys in the roster): alice, bob" in out.err
    assert "get-console-output --latest" in out.err
    assert kh.read_text() == f"203.0.113.5 ssh-ed25519 {KEY_A}\n203.0.113.6 ssh-ed25519 {KEY_B}\n"
    assert all(c[:5] == ["ssh-keyscan", "-T", "10", "-t", "ed25519"] for c in keyscan.calls)
    # a second run changes nothing
    code = sh.main(["--roster", str(roster), "--known-hosts", str(kh), "--only", "alice"])
    assert code == 0 and "unchanged" in capsys.readouterr().out
    # a changed key is refused and the file keeps the recorded one
    keyscan.keys["203.0.113.5"] = KEY_B
    code = sh.main(["--roster", str(roster), "--known-hosts", str(kh), "--only", "alice"])
    out = capsys.readouterr()
    assert code == 1 and "CHANGED" in out.out and f"recorded {FP_A}" in out.out
    assert "1 host(s) need attention" in out.err
    assert f"203.0.113.5 ssh-ed25519 {KEY_A}\n" in kh.read_text()
    # --replace accepts it and says what it replaced
    code = sh.main(
        ["--roster", str(roster), "--known-hosts", str(kh), "--only", "alice", "--replace"]
    )
    out = capsys.readouterr().out
    assert code == 0 and "replaced" in out and f"was {FP_A}" in out
    assert kh.read_text() == f"203.0.113.5 ssh-ed25519 {KEY_B}\n203.0.113.6 ssh-ed25519 {KEY_B}\n"
    # an unreachable VM is a row, not a crash, and the file is untouched
    keyscan.keys.pop("203.0.113.6")
    before = kh.read_text()
    code = sh.main(["--roster", str(roster), "--known-hosts", str(kh), "--only", "bob"])
    assert code == 1 and "unreachable" in capsys.readouterr().out and kh.read_text() == before


def test_known_hosts_file_keeps_foreign_lines(tmp_path):
    kh = tmp_path / "known_hosts"
    kh.write_text(
        "# comment\nother.example.org ssh-rsa AAAAB3 extra\n203.0.113.5 ssh-ed25519 OLD\n"
    )
    assert sh.read_known_hosts(kh) == {"203.0.113.5": "203.0.113.5 ssh-ed25519 OLD"}
    sh.write_known_hosts(kh, {"203.0.113.5": f"203.0.113.5 ssh-ed25519 {KEY_A}"})
    assert kh.read_text() == (
        f"# comment\nother.example.org ssh-rsa AAAAB3 extra\n203.0.113.5 ssh-ed25519 {KEY_A}\n"
    )
    assert sh.key_line(
        f"# c\n203.0.113.5 ssh-rsa X\n203.0.113.5 ssh-ed25519 {KEY_A} \n", "203.0.113.5"
    ) == (f"203.0.113.5 ssh-ed25519 {KEY_A}")
    assert sh.key_line("203.0.113.9 ssh-ed25519 X\n", "203.0.113.5") is None


def test_scan_refuses_duplicate_rows(tmp_path, monkeypatch, capsys):
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER + "dave,D,K,d@example.org,d/x,4,ws-dave,203.0.113.5,104,\n")
    keyscan = FakeKeyscan({})
    monkeypatch.setattr(sh, "subprocess_runner", keyscan)
    assert sh.main(["--roster", str(roster), "--known-hosts", str(tmp_path / "kh")]) == 1
    err = capsys.readouterr().err
    assert "roster: duplicate ip '203.0.113.5': rows alice and dave" in err and not keyscan.calls
    assert "launch.py --only <the row whose VM has it> (the bot's !launch) clears" in err  # #604


def test_scan_checks_the_key_against_the_console_fingerprints(tmp_path, monkeypatch, capsys):
    """PM on #36 (Mon 07:10): launch.py records the console's fingerprints in `host_keys`, and
    the scanned key must be one of them; a mismatch is refused even with --replace."""
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "slug,email,host,ip,host_keys\n"
        f"alice,alice@example.org,ws-alice,203.0.113.5,SHA256:ecdsa-one {FP_A} SHA256:rsa-one\n"
        "bob,bob@example.org,ws-bob,203.0.113.6,SHA256:someone-else\n"
    )
    kh = tmp_path / "known_hosts"
    keyscan = FakeKeyscan({"203.0.113.5": KEY_A, "203.0.113.6": KEY_B})
    monkeypatch.setattr(sh, "subprocess_runner", keyscan)
    for extra in ([], ["--replace"]):
        code = sh.main(["--roster", str(roster), "--known-hosts", str(kh), *extra])
        out = capsys.readouterr()
        assert code == 1 and "1 matched the console" in out.out and "verified" in out.out
        assert "MISMATCH" in out.out and "SHA256:someone-else" in out.out
        assert "not verified" not in out.err
        assert kh.read_text() == f"203.0.113.5 ssh-ed25519 {KEY_A}\n"  # bob's key not recorded


def test_a_key_the_console_printed_replaces_a_previous_vms_at_that_address(
    tmp_path, monkeypatch, capsys
):
    """#604: EC2 gave alice's new VM the address of a VM torn down before, whose key known_hosts
    pins. Her row's `host_keys`, from her VM's console, vouch for the new key: it replaces the
    old one, without --replace. A key the console didn't print is still refused (CHANGED)."""
    roster = tmp_path / "roster.csv"
    roster.write_text(
        f"slug,email,host,ip,host_keys\nalice,alice@example.org,ws-alice,203.0.113.5,{FP_A}\n"
        "bob,bob@example.org,ws-bob,203.0.113.6,\n"
    )
    kh = tmp_path / "known_hosts"
    kh.write_text(f"203.0.113.5 ssh-ed25519 {KEY_B}\n203.0.113.6 ssh-ed25519 {KEY_A}\n")
    keyscan = FakeKeyscan({"203.0.113.5": KEY_A, "203.0.113.6": KEY_B})
    monkeypatch.setattr(sh, "subprocess_runner", keyscan)
    assert sh.main(["--roster", str(roster), "--known-hosts", str(kh)]) == 1  # bob's: CHANGED
    out = capsys.readouterr().out
    assert "the console printed this one: the recorded key was a previous VM's here" in out
    assert "CHANGED" in out and "not replaced (use --replace)" in out
    assert kh.read_text() == f"203.0.113.5 ssh-ed25519 {KEY_A}\n203.0.113.6 ssh-ed25519 {KEY_A}\n"
