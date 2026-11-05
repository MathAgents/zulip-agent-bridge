"""#388: no file tracked by git holds a literal invisible character, which a reviewer can't see.

Write an escape in code (`"\\u200b"`) and a description in prose ("U+3164"). The set is the
scrubber's (ADMIN §2.7), plus what is blank on screen but not in it: the Hangul fillers, which
are letters, and every space but the ASCII one, as `deploy.sh`'s `sanitize()` shows them.
U+FE0F, the emoji presentation selector, passes right after a non-ASCII character that isn't
flagged itself, as in ⚠️; a doubled one is flagged, and so is a keycap on an ASCII base
(`#`, U+FE0F, U+20E3), to be written as escapes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from zab.admin.scrub import _INVISIBLE_POINTS, _class

ROOT = Path(__file__).resolve().parents[2]
#: Blank on screen, not in the scrubber's set (#388): the Hangul fillers (Lo, a valid identifier
#: character that shows as nothing), and the spaces (Zs) but U+0020, which read as one.
_BLANK = frozenset(
    {0x115F, 0x1160, 0x3164, 0xFFA0}
    | {0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000}
)
_FLAGGED = _class(_INVISIBLE_POINTS | _BLANK)
#: Tracked files allowed to hold one, each with its reason: a fixture that must, say.
ALLOWED: dict[str, str] = {}


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], capture_output=True)  # noqa: S603, S607 - fixed argv


def invisible_characters(text: str) -> list[tuple[int, int, str]]:
    """(line, column, `U+XXXX`) of each flagged character in `text`."""
    found = []
    for hit in _FLAGGED.finditer(text):
        i = hit.start()
        point = ord(text[i])
        if point == 0xFE0F and i and ord(text[i - 1]) > 0x7F and not _FLAGGED.match(text, i - 1):
            continue
        column = i - text.rfind("\n", 0, i)
        found.append((text.count("\n", 0, i) + 1, column, f"U+{point:04X}"))
    return found


def tracked_findings(root: Path) -> list[str]:
    """`path:line:column: U+XXXX` for each flagged character in a file `git ls-files` lists
    under `root`, or in its name. A file that isn't UTF-8 is a binary (images, fonts): skipped."""
    listed = _git("-C", str(root), "ls-files", "-z")
    assert listed.returncode == 0, listed.stderr
    findings = []
    for name in (os.fsdecode(n) for n in listed.stdout.split(b"\0") if n):
        if name in ALLOWED:
            continue
        findings += [f"{name!r} (its name): {p}" for _, _, p in invisible_characters(name)]
        path = root / name
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings += [f"{name}:{line}:{col}: {p}" for line, col, p in invisible_characters(text)]
    return findings


def test_no_tracked_file_holds_an_invisible_character():
    top = _git("-C", str(ROOT), "rev-parse", "--show-toplevel") if shutil.which("git") else None
    if top is None or top.returncode or Path(os.fsdecode(top.stdout.strip())).resolve() != ROOT:
        pytest.skip("not a git checkout of zab")
    findings = tracked_findings(ROOT)
    assert not findings, "write these as escapes or descriptions (#388):\n" + "\n".join(findings)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_check_finds_planted_characters(tmp_path):
    assert _git("init", "-q", str(tmp_path)).returncode == 0
    files = {
        "clean.py": 'ZWSP = "\\u200b"\nWARN = "⚠️"\n',
        "zwsp.py": 'ZWSP = "\u200b"\n',
        "notes.md": "# Notes\n\nan `ok\u3164` and\n\ufe0fx\ufeff\n",
        "bidi\u202ecod.py": "",
        "untracked.md": "\u200b",
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff" + "\u200b".encode())
    tracked = ["clean.py", "zwsp.py", "notes.md", "bidi\u202ecod.py", "image.png"]
    assert _git("-C", str(tmp_path), "add", "--", *tracked).returncode == 0
    assert sorted(tracked_findings(tmp_path)) == [
        "'bidi\\u202ecod.py' (its name): U+202E",
        "notes.md:3:7: U+3164",
        "notes.md:4:1: U+FE0F",
        "notes.md:4:3: U+FEFF",
        "zwsp.py:1:9: U+200B",
    ]
    assert invisible_characters("a\u2060b\n\x1b[31m\r\n") == [
        (1, 2, "U+2060"),
        (2, 1, "U+001B"),
        (2, 6, "U+000D"),
    ]
    assert invisible_characters("tab\tand\nnewline, \u2764\ufe0f, \U0001f642\ufe0f") == []
    # a doubled selector, a keycap on an ASCII base, a wide and a no-break space
    assert invisible_characters("\u26a0\ufe0f\ufe0f #\ufe0f\u20e3 \u3000\u00a0") == [
        (1, 3, "U+FE0F"),
        (1, 6, "U+FE0F"),
        (1, 9, "U+3000"),
        (1, 10, "U+00A0"),
    ]
