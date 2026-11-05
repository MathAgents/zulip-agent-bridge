"""zab.admin.audit (docs/ADMIN.md §2.8): the hash chain detects an edited, deleted or moved line."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zab.admin import __main__ as cli
from zab.admin.audit import GENESIS, AuditError, AuditLog, Head, line_hash, verify
from zab.admin.scrub import REDACTED, Scrubber

SECRET = "Zq7Wm2Kp9Lx4Vb6Nc8Rt1Ys3Uh5Jd0Fg"  # noqa: S105 - made up
N = 6


def _now() -> datetime:
    return datetime(2026, 10, 31, 14, 2, 11, tzinfo=UTC)


def _log(path: Path) -> AuditLog:
    return AuditLog(path, scrub=Scrubber([SECRET]), now=_now)


@pytest.fixture
def written(tmp_path: Path) -> tuple[Path, list[Head]]:
    """A log of N records, and the head each append returned (as posted in #organizers)."""
    path = tmp_path / "audit.jsonl"
    log = _log(path)
    heads = [
        log.append("request", plan=f"P-{i}", verb="rollout", args=["v0.2.2", "alice"])
        for i in range(1, N + 1)
    ]
    return path, heads


def _lines(path: Path) -> list[bytes]:
    return path.read_bytes().split(b"\n")[:-1]


def _write(path: Path, lines: list[bytes]) -> None:
    path.write_bytes(b"".join(line + b"\n" for line in lines))


def _published(heads: list[Head]) -> list[tuple[int, str]]:
    return [(h.seq, h.chain) for h in heads]


# ------------------------------------------------------------------------------- appending


def test_append_chains_records(written: tuple[Path, list[Head]]) -> None:
    path, heads = written
    lines = _lines(path)
    assert [h.seq for h in heads] == list(range(1, N + 1))
    prev = GENESIS
    for i, line in enumerate(lines):
        record = json.loads(line)
        assert record["seq"] == i + 1 and record["prev"] == prev
        assert record["ts"] == "2026-10-31T14:02:11Z" and record["event"] == "request"
        assert list(record)[:3] == ["seq", "ts", "event"] and list(record)[-1] == "prev"
        assert heads[i] == Head(i + 1, line_hash(line))
        prev = line_hash(line)
    assert _log(path).head() == heads[-1]
    assert str(heads[-1]) == f"audit #{N} · chain {heads[-1].sha256[:12]}"  # 12 hex: Drew
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_records_are_scrubbed_and_ascii(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _log(path).append(
        "apply_end",
        requester={"id": 102, "name": "Alicé @**all**"},
        argv=[["push_config.py", f"--token={SECRET}"]],
        results={f"x{SECRET}": "ok"},
        exit=0,
    )
    raw = path.read_bytes()
    assert SECRET.encode() not in raw
    assert raw.isascii()
    record = json.loads(raw)
    assert record["argv"] == [["push_config.py", f"--token={REDACTED}"]]
    assert record["results"] == {f"x{REDACTED}": "ok"}
    assert record["requester"]["name"] == "Alicé @\u200b**all**"
    assert verify(path).ok


def test_structured_credentials_are_redacted(tmp_path: Path) -> None:
    """A secret known only by its field's name is redacted in a record too (codex round 3)."""
    path = tmp_path / "audit.jsonl"
    aws_key, zulip_key = "w" * 20 + "/" + "K" * 19, "k" * 32
    creds = {"Credentials": {"SecretAccessKey": aws_key, "Token": aws_key * 3}}
    hidden = {"Secret\x00AccessKey": aws_key[:9] + "\x00" + aws_key[9:]}  # codex round 4
    _log(path).append(
        "read", response=creds, bot={"api_key": zulip_key}, results={"turkey": "ok"}, x=hidden
    )
    raw = path.read_text()
    assert aws_key not in raw and zulip_key not in raw
    assert json.loads(raw)["x"] == {"SecretAccessKey": REDACTED}
    record = json.loads(raw)
    assert record["response"]["Credentials"]["SecretAccessKey"] == REDACTED
    assert record["bot"] == {"api_key": REDACTED} and record["results"] == {"turkey": "ok"}


def test_unknown_event_and_reserved_fields_are_refused(tmp_path: Path) -> None:
    log = _log(tmp_path / "audit.jsonl")
    with pytest.raises(ValueError, match="unknown audit event"):
        log.append("hello")
    with pytest.raises(ValueError, match="reserved"):
        log.append("read", prev="0" * 64)
    # names that only become reserved once scrubbed (reviewer finding 2)
    for name in ("se\x00q", "ev\x07ent", "\x1bts", "pr\u202eev"):
        with pytest.raises(ValueError, match="reserved"):
            log.append("read", **{name: 99})
    assert not (tmp_path / "audit.jsonl").exists()


def test_empty_and_missing_logs(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    assert _log(path).head() == Head(0, GENESIS)
    path.touch()
    assert _log(path).head() == Head(0, GENESIS)
    assert verify(path).ok and verify(path).records == 0
    assert not verify(tmp_path / "missing.jsonl").ok


def test_a_partial_line_stops_appends(written: tuple[Path, list[Head]]) -> None:
    path, _ = written
    with path.open("ab") as f:
        f.write(b'{"seq": 7, "ts"')
    with pytest.raises(AuditError, match="partial line"):
        _log(path).append("read")
    result = verify(path)
    assert not result.ok and result.line == N + 1 and "incomplete" in (result.problem or "")


def test_a_last_line_that_is_no_record_stops_appends(written: tuple[Path, list[Head]]) -> None:
    path, _ = written
    with path.open("ab") as f:
        f.write(b"garbage\n")
    with pytest.raises(AuditError, match="not a record"):
        _log(path).append("read")


def test_long_records_and_a_long_tail(tmp_path: Path) -> None:
    """The head is read backwards from the end, in growing chunks."""
    path = tmp_path / "audit.jsonl"
    log = _log(path)
    for size in (10, 5000, 70_000, 3):
        head = log.append("plan", output="y" * size)
        assert log.head() == head
    assert verify(path).ok


def test_concurrent_writers_keep_one_chain(tmp_path: Path) -> None:
    """Each append reads the head under the lock, so separate processes can't fork the chain."""
    path = tmp_path / "audit.jsonl"
    script = (
        "import sys; from pathlib import Path; from zab.admin.audit import AuditLog\n"
        "from zab.admin.scrub import Scrubber\n"
        "log = AuditLog(Path(sys.argv[1]), scrub=Scrubber())\n"
        "for i in range(40): log.append('read', writer=sys.argv[2], i=i)\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(path), str(w)])  # noqa: S603
        for w in range(4)
    ]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    result = verify(path)
    assert result.ok and result.records == 160


# ------------------------------------------------------------------------- tampering: detect


@pytest.mark.parametrize("k", range(N - 1))
def test_an_edited_line_is_detected(written: tuple[Path, list[Head]], k: int) -> None:
    path, _ = written
    lines = _lines(path)
    lines[k] = lines[k].replace(b'"alice"', b'"mallory"')
    _write(path, lines)
    result = verify(path)
    assert not result.ok and result.line == k + 2 and "edited" in (result.problem or "")


@pytest.mark.parametrize("k", range(N - 1))
def test_a_deleted_line_is_detected(written: tuple[Path, list[Head]], k: int) -> None:
    path, _ = written
    lines = _lines(path)
    del lines[k]
    _write(path, lines)
    result = verify(path)
    assert not result.ok and result.line == k + 1


@pytest.mark.parametrize("k", range(N - 1))
def test_reordered_lines_are_detected(written: tuple[Path, list[Head]], k: int) -> None:
    path, _ = written
    lines = _lines(path)
    lines[k], lines[k + 1] = lines[k + 1], lines[k]
    _write(path, lines)
    result = verify(path)
    assert not result.ok and result.line == k + 1


def test_an_inserted_line_is_detected(written: tuple[Path, list[Head]]) -> None:
    path, _ = written
    lines = _lines(path)
    lines.insert(2, lines[2])
    _write(path, lines)
    assert not verify(path).ok


def test_the_last_line_is_covered_by_the_published_head(written: tuple[Path, list[Head]]) -> None:
    """Nothing follows the last line inside the file: the head posted in #organizers does."""
    path, heads = written
    lines = _lines(path)
    lines[-1] = lines[-1].replace(b'"alice"', b'"mallory"')
    _write(path, lines)
    assert verify(path).ok  # a chain can't vouch for its own last line
    result = verify(path, _published(heads))
    assert not result.ok and result.line == N and "rewritten" in (result.problem or "")


def test_the_last_lines_seq_is_checked(written: tuple[Path, list[Head]]) -> None:
    """Only the seq check covers the last line's seq: no later prev does (reviewer finding 4)."""
    path, _ = written
    lines = _lines(path)
    lines[-1] = lines[-1].replace(f'"seq":{N},'.encode(), f'"seq":{N + 1},'.encode())
    _write(path, lines)
    result = verify(path)
    assert not result.ok and result.line == N and "seq" in (result.problem or "")


def test_a_deleted_last_line_is_covered_by_the_published_head(
    written: tuple[Path, list[Head]],
) -> None:
    path, heads = written
    _write(path, _lines(path)[:-1])
    assert verify(path).ok
    result = verify(path, _published(heads))
    assert not result.ok and "missing" in (result.problem or "")


def test_a_rewritten_chain_fails_against_any_later_published_head(
    written: tuple[Path, list[Head]],
) -> None:
    """Re-chaining every line after an edit passes inside the file, not against the channel."""
    path, heads = written
    lines = _lines(path)
    prev = GENESIS
    for i, line in enumerate(lines):
        record = json.loads(line)
        if i == 2:
            record["args"] = ["v0.2.2", "mallory"]
        record["prev"] = prev
        lines[i] = json.dumps(record, separators=(",", ":")).encode()
        prev = line_hash(lines[i])
    _write(path, lines)
    assert verify(path).ok
    assert verify(path, [(2, heads[1].chain)]).ok  # before the edit
    for h in heads[2:]:
        assert not verify(path, [(h.seq, h.chain)]).ok


def test_every_published_head_is_checked(written: tuple[Path, list[Head]]) -> None:
    """A wrong head is not hidden by a right one for the same seq (codex round 1)."""
    path, heads = written
    right = (N, heads[-1].chain)
    assert verify(path, [right, right]).ok
    for pair in ([(N, "000000"), right], [right, (N, "000000")]):
        result = verify(path, pair)
        assert not result.ok and "rewritten" in (result.problem or "")


# ------------------------------------------------------------------------------------- CLI


def test_cli_verify(written: tuple[Path, list[Head]], capsys: pytest.CaptureFixture[str]) -> None:
    path, heads = written
    head = f"#{heads[-1].seq}:{heads[-1].chain}"
    assert (
        cli.main(["audit", "verify", str(path), "--head", head, "--head", f"2:{heads[1].chain}"])
        == 0
    )
    out = capsys.readouterr().out
    assert out.startswith(f"ok: {N} records; audit #{N} · chain {heads[-1].chain}")
    assert cli.main(["audit", "verify", str(path), "--head", f"{N}:000000"]) == 1
    assert "FAILED at line 6" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["audit", "verify", str(path), "--head", "6:xyz"])
    assert cli.main([]) == 2


def test_cli_takes_a_head_as_posted(
    written: tuple[Path, list[Head]], capsys: pytest.CaptureFixture[str]
) -> None:
    path, heads = written
    for pasted in (
        str(heads[-1]),
        f" audit #{N} · chain {heads[-1].chain[:6]} ",
        f"#2 · chain {heads[1].chain}",
    ):
        assert cli.main(["audit", "verify", str(path), "--head", pasted]) == 0, pasted
    assert cli.main(["audit", "verify", str(path), "--head", f"audit #{N} · chain 000000"]) == 1
    capsys.readouterr()
    assert cli.main(["audit", "verify", str(path.with_name("missing.jsonl"))]) == 1
    assert capsys.readouterr().err.startswith("FAILED: cannot read")  # no "at line None"


def test_python_m_zab_admin(written: tuple[Path, list[Head]]) -> None:
    path, _ = written
    res = subprocess.run(  # noqa: S603 - this interpreter, a fixed argument list
        [sys.executable, "-m", "zab.admin", "audit", "verify", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0 and res.stdout.startswith(f"ok: {N} records")
