"""`workshop/admin/keep_until.py` (ADMIN §11.5, #570): each named row's `keep_until`, the end of
its kept VM. Synthetic rosters only: fake names, `example.org` addresses, documentation IPs."""

from __future__ import annotations

import csv
import os
import stat

import admin_common
import keep_until as ku
import pytest

ROSTER = """\
slug,first_name,last_name,email,host,ip,notes
alice,Alice,Example,alice@example.org,ws-alice,203.0.113.10,
bob,Bob,Example,bob@example.org,ws-bob,203.0.113.11,kept
carol,Carol,Example,carol@example.org,,,
"""


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def rows(path) -> dict[str, dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return {row["slug"]: row for row in csv.DictReader(f)}


def run(roster, *sets: str, dry: bool = False) -> int:
    argv = ["--roster", str(roster), *(w for s in sets for w in ("--set", s))]
    return ku.main([*argv, "--dry-run"] if dry else argv)


def test_it_sets_extends_and_clears_a_rows_end(roster, capsys):
    assert run(roster, "bob=2026-11-30", "carol=2026-12-01T09:30", dry=True) == 0
    out = capsys.readouterr().out
    assert "bob: keep_until (none) → 2026-11-30 (its VM ends Monday, November 30, 2026, " in out
    assert "15:00 UTC)" in out  # a date alone: at KEEP_UNTIL_HOUR
    assert "carol: keep_until (none) → 2026-12-01T09:30 (its VM ends Tuesday, December 1, " in out
    assert out.endswith("dry run: nothing written\n")
    assert "keep_until" not in rows(roster)["bob"]  # the dry run wrote nothing
    assert run(roster, "bob=2026-11-30", "carol=2026-12-01t09:30") == 0
    after = rows(roster)
    assert (after["bob"]["keep_until"], after["carol"]["keep_until"]) == (
        "2026-11-30",
        "2026-12-01T09:30",  # `t` is read as `T`
    )
    assert after["alice"]["keep_until"] == "" and after["bob"]["notes"] == "kept"
    assert after["alice"]["ip"] == "203.0.113.10"  # every other column as it was
    assert stat.S_IMODE(os.stat(roster).st_mode) == 0o600  # personal data: its mode kept
    assert (roster.parent / "roster.csv.bak").read_text(encoding="utf-8") == ROSTER
    capsys.readouterr()
    assert run(roster, "bob=2026-12-07", "carol=none") == 0  # an extension, and a clear
    out = capsys.readouterr().out
    assert "bob: keep_until 2026-11-30 → 2026-12-07 (its VM ends Monday, December 7, " in out
    assert "carol: keep_until 2026-12-01T09:30 → (none) (no end)" in out
    assert (rows(roster)["bob"]["keep_until"], rows(roster)["carol"]["keep_until"]) == (
        "2026-12-07",
        "",
    )


def test_nothing_to_change_exits_3_on_a_dry_run(roster, capsys):
    """#708: the bot answers it with no plan, `☑`; the apply of the same writes nothing."""
    assert run(roster, "alice=none", dry=True) == ku.NOTHING == 3
    out = capsys.readouterr().out
    assert "alice: keep_until (none), unchanged" in out and "nothing to change" in out
    before = roster.read_bytes()
    assert run(roster, "alice=none") == 0
    assert roster.read_bytes() == before and not (roster.parent / "roster.csv.bak").exists()
    assert run(roster, "bob=2026-11-30") == 0
    capsys.readouterr()
    assert run(roster, "bob=2026-11-30", dry=True) == 3
    assert "bob: keep_until 2026-11-30, unchanged" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("pair", "why"),
    [
        ("dave=2026-11-30", "'dave': not a row of the roster"),
        ("bob", "'bob': not a row of the roster"),  # no `=`
        ("bob=2026-02-30", "bob: '2026-02-30' is not a date like 2026-11-30"),
        ("bob=11/30/2026", "bob: '11/30/2026' is not a date"),  # as a spreadsheet may write it
        ("bob=2026-11-30T24:00", "is not a date"),
        ("bob=2026-11-30 15:00", "is not a date"),
        ("bob=2026-11-30T15:00Z", "is not a date"),
        ("bob=", "is not a date"),
        ("bob=None", "is not a date"),
    ],
)
def test_a_value_or_a_row_it_cant_take_is_refused_and_nothing_is_written(roster, capsys, pair, why):
    assert run(roster, "alice=2026-11-30", pair) == 2
    err = capsys.readouterr().err
    assert err.startswith("refused, nothing written: ") and why in err
    assert roster.read_text(encoding="utf-8") == ROSTER


def test_a_row_named_twice_is_refused(roster, capsys):
    assert run(roster, "bob=2026-11-30", "bob=2026-12-07") == 2
    assert "bob: named twice" in capsys.readouterr().err
    assert roster.read_text(encoding="utf-8") == ROSTER


def test_a_roster_changed_while_it_ran_is_not_overwritten(roster, capsys, monkeypatch):
    real = ku.load_roster

    def meanwhile(path):
        loaded = real(path)
        with open(path, "a", encoding="utf-8") as f:  # another write, after this run's read
            f.write("dave,Dave,Example,dave@example.org,,,\n")
        return loaded

    monkeypatch.setattr(ku, "load_roster", meanwhile)
    assert run(roster, "bob=2026-11-30") == 1
    assert "the roster changed while this ran" in capsys.readouterr().err
    assert "keep_until" not in rows(roster)["bob"] and "dave" in rows(roster)


def test_the_bots_grammar_reads_keep_until_as_the_scripts_do():
    """Two copies, since the scripts don't import `zab.admin`: the same words, the same reading,
    and the same hour for a date alone."""
    from zab.admin import grammar

    words = [
        "2026-11-30",
        "2026-11-30T15:00",
        "2026-11-30t15:00",
        "2028-02-29",
        "2026-02-29",
        "2026-13-01",
        "2026-11-31",
        "2026-11-30T23:59",
        "2026-11-30T24:00",
        "2026-11-30T15:60",
        "2026-11-30T15",
        "26-11-30",
        "2026-1-30",
        "0000-01-01",
        "2026-11-30 15:00",
        " 2026-11-30",
        "2026-11-30\n",
        "\uff12\uff10\uff12\uff16-11-30",  # full-width digits: not ASCII
        "none",
        "",
    ]
    for word in words:
        assert grammar.keep_until_value(word) == admin_common.keep_until_value(word), word
    assert admin_common.keep_until_value("2026-11-30t15:00") == "2026-11-30T15:00"
    assert grammar.END_HOUR == admin_common.KEEP_UNTIL_HOUR == 15
    assert ku.end_words("2026-11-30") == "Monday, November 30, 2026, 15:00 UTC"
    assert ku.end_words("2026-11-30T09:05") == "Monday, November 30, 2026, 09:05 UTC"
