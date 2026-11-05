"""zab.admin.scrub (docs/ADMIN.md §2.7): exact secrets, the §11.6 patterns, mentions, fences."""

from __future__ import annotations

import base64
import json
import os
import random
import re
import time
import tracemalloc
import unicodedata
from pathlib import Path
from re import _constants as sre  # the regex parser's opcodes: Python 3.11 to 3.13
from re import _parser as sre_parser
from typing import Any

import pytest

from zab.admin import scrub as scrub_module
from zab.admin.scrub import (
    _GUARDED_START,
    _RELAXED,
    ATTACHMENT_BYTES,
    INLINE_CHARS,
    MAX_SCRUB_CHARS,
    RECORD_VALUE_CHARS,
    REDACTED,
    Scrubber,
    fence,
    read_secrets,
    span,
    strip,
)

ZULIP_KEY = "Zq7Wm2Kp9Lx4Vb6Nc8Rt1Ys3Uh5Jd0Fg"  # 32 alphanumerics, made up
GH_TOKEN = "github_pat_11ABCDEFG0" + "x" * 60
OTHER = "Pr0visionerKeyAbCdEfGhIjKlMnOpQr"
AWS_KEY = "w" * 20 + "/" + "K" * 19  # the shape of a secret access key, made up
AWS_TOKEN = "IQoJb3JpZ2lu" + "Xy+/" * 40 + "=="
_ACTIVE = re.compile(r"@(?:\*\*[^*\n]+\*\*|\*[^*\n]+\*)")  # a literal Zulip mention form


@pytest.fixture
def scrub() -> Scrubber:
    return Scrubber([ZULIP_KEY, GH_TOKEN, OTHER])


# ---------------------------------------------------------------------- step 1: exact secrets

EXACT = [
    f"https://bot:{ZULIP_KEY}@chat.example.org/api",
    f"x{ZULIP_KEY}y",
    f"{ZULIP_KEY}{ZULIP_KEY}",
    f"error: token={GH_TOKEN}; retry",
    f"{OTHER}{ZULIP_KEY}",  # adjacent
    f'{{"api_key":"{OTHER}"}}',
    f"a {ZULIP_KEY[:10]}\x1b[0m{ZULIP_KEY[10:]} b",  # split by an escape sequence
    f"a {ZULIP_KEY[:10]}\x00\x07{ZULIP_KEY[10:]} b",  # split by control characters
    f"a {ZULIP_KEY[:10]}\u202e{ZULIP_KEY[10:]} b",  # split by a bidi override
    f"\x1b{ZULIP_KEY}",  # reviewer: ESC Z is an escape, which ate the Z
    f"\x1b[{ZULIP_KEY}",  # and so is ESC [ Z
    f"vm says \x1b[1;31m{ZULIP_KEY}",
]


@pytest.mark.parametrize("text", EXACT)
def test_exact_secret_inside_a_longer_string(scrub: Scrubber, text: str) -> None:
    out = scrub(text)
    assert REDACTED in out
    for secret in (ZULIP_KEY, GH_TOKEN, OTHER):
        for k in range(len(secret) - 11):
            assert secret[k : k + 12] not in out, (text, out)


def test_overlapping_secrets_leave_no_fragment() -> None:
    a, b = "AAAAAAAAAAAAbbbbbb", "bbbbbbCCCCCCCCCCCC"  # b starts inside a
    out = Scrubber([a, b])("<AAAAAAAAAAAAbbbbbbCCCCCCCCCCCC>")
    assert out == f"<{REDACTED}>"


def test_a_secret_inside_another_leaves_no_fragment() -> None:
    inner = "InnerSecret0123456789"
    outer = f"x-access-token:{inner}:tail-of-the-outer-one"
    assert Scrubber([outer, inner])(f"<{outer}>") == f"<{REDACTED}>"


B64_SECRET = "Tok~~~???en0123456789abcdefgh"  # noqa: S105 - base64 has + and / at any shift


@pytest.mark.parametrize("shift", range(3))
@pytest.mark.parametrize("encode", [base64.b64encode, base64.urlsafe_b64encode])
@pytest.mark.parametrize("pad", [True, False])
def test_the_base64_of_a_secret_is_redacted(shift: int, encode: Any, pad: bool) -> None:
    raw = B64_SECRET.encode()
    enc = encode(b"p" * shift + raw + b"\x00tail").decode()
    enc = enc if pad else enc.rstrip("=")
    out = Scrubber([B64_SECRET])(f"Authorization: Basic {enc}")
    assert out.count(REDACTED) == 1
    left = len(out) - len("Authorization: Basic ") - len(REDACTED)
    assert len(enc) - left >= 4 * len(raw) // 3 - 4  # all but a character at each end


def test_git_basic_auth_header(scrub: Scrubber) -> None:
    """`provision_repos.git_env` sends the token as `basic base64(x-access-token:<token>)`."""
    enc = base64.b64encode(f"x-access-token:{GH_TOKEN}".encode()).decode()
    out = scrub(f"http.extraHeader=AUTHORIZATION: basic {enc}")
    assert out.startswith("http.extraHeader=AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46")
    assert enc[20:120] not in out and REDACTED in out


def test_record_keeps_a_field_name_beside_its_value(scrub: Scrubber) -> None:
    """Patterns that need the name see it in a mapping too (codex round 3)."""
    value = {
        "response": {"SecretAccessKey": AWS_KEY, "AccessKeyId": "ASIA" + "B" * 16},
        "sessions": [{"SessionToken": AWS_TOKEN}, {"key": "k" * 32}],
        "results": {"turkey": "ok", "key-holder": "k" * 31},  # slugs: nothing to see
        "argv": [f"--token={ZULIP_KEY}", "@**all**"],
        "note": f"pre {ZULIP_KEY} post",  # partial redaction kept where context doesn't matter
        f"x{OTHER}": 7,
        "n": None,
    }
    assert scrub.record(value) == {
        "response": {"SecretAccessKey": REDACTED, "AccessKeyId": "[redacted aws access key]"},
        "sessions": [{"SessionToken": REDACTED}, {"key": REDACTED}],
        "results": {"turkey": "ok", "key-holder": "k" * 31},
        "argv": [f"--token={REDACTED}", "@\u200b**all**"],
        "note": f"pre {REDACTED} post",
        f"x{REDACTED}": 7,
        "n": None,
    }


@pytest.mark.parametrize(
    "value",
    [
        {"SecretAccessKey": AWS_KEY[:10] + "\x00" + AWS_KEY[10:]},  # in the value
        {"Secret\x00AccessKey": AWS_KEY},  # in the name
        {"SessionToken": AWS_TOKEN[:30] + "\x1b[0m" + AWS_TOKEN[30:]},
        {"api_key": "k" * 16 + "\x07" + "k" * 16},
        {"api\x00_key": "k" * 32},
        {"\x1bSecretAccessKey": AWS_KEY},  # ESC goes, the S stays
        # codex round 5: mixes
        {"\x1bSecretAccessKey": AWS_KEY[:10] + "\x00" + AWS_KEY[10:]},
        {"\x1bSecret\x1b[0mAccessKey": AWS_KEY},  # the escape-free view
        {"Secret" + chr(0x200B) + "AccessKey": AWS_KEY},
        {"SecretAccessKey": AWS_KEY[:10] + chr(0x200B) + AWS_KEY[10:]},
        {"Token": chr(0xFEFF) + AWS_TOKEN},
    ],
)
def test_record_sees_through_control_characters(value: dict[str, str]) -> None:
    """Codex rounds 4 and 5: stripping makes the name and the secret contiguous."""
    out = Scrubber().record(value)
    assert list(out.values()) == [REDACTED], out


#: What `json.dumps` writes for a character the strip deletes: a VM's text inside
#: `status.py --json` (#361; the review of #380).
_SPLITTERS = [
    "\\u200b", "\\u0000", "\\u001b[31m", "\\u009b", "\\u00ad", "\\ud800", "\\u2028",
    "\\u202e", "\\ufeff", "\\u007f", "\\r", "\\b", "\\f",
]  # fmt: skip


@pytest.mark.parametrize("splitter", _SPLITTERS)
@pytest.mark.parametrize(
    "shape",
    [
        '{{\n  "last_error": "{secret}"\n}}\n',  # a document at a line's start
        'EC2: {{"last_error": "{secret}"}}\n',  # after text, on the same line
        '{{"a": 1}} {{"b": ["{secret}"]}}\n',  # two documents on one line
        '{{"vms": [{{"last_error": "{secret}", "ok": tr\n',  # partial, cut by a kill
    ],
)
def test_json_text_sees_through_each_escape_wherever_the_json_lies(
    scrub: Scrubber, shape: str, splitter: str
) -> None:
    """The strip deletes each of these characters, but in their escaped form a scan sees two
    printable runs; `json_text` makes them the characters again, then scrubs the text."""
    for secret in (ZULIP_KEY, "ghp_" + "Q" * 36):
        text = shape.format(secret=secret[:10] + splitter + secret[10:])
        assert secret[10:] in scrub(text)  # the scan alone misses it
        out = scrub.json_text(text)
        assert secret[10:] not in out and "[redacted " in out, out
        assert len(out) <= len(text) + len(REDACTED)


def test_json_text_keeps_json_valid_and_other_escapes_as_they_are(scrub: Scrubber) -> None:
    value = {"a": 'quote " backslash \\ line\nand\ttab', "é": "😀 é", "lit": "\\u200b"}
    text = json.dumps(value, indent=2)
    assert json.loads(scrub.json_text(text)) == value  # the characters, not the escapes
    assert '\\" backslash \\\\ line\\nand\\ttab' in scrub.json_text(text)
    assert scrub.json_text("\\\\u200b \\\\r") == "\\\\u200b \\\\r"  # an escaped backslash
    kept = '"\\u0022 \\u005c \\u000a \\u0009 \\" \\\\ \\n \\t"'  # each would end a string or line
    assert scrub.json_text(kept) == kept and json.loads(kept) == '" \\ \n \t " \\ \n \t'
    # an escaped backslash, then `n` glued to a token: decoded, no boundary, and no tab or
    # newline for the view to read either (it reads backslashes in pairs, as JSON does)
    glued = f'"\\\\n{_GUARDED_TOKENS[0]}"'
    assert scrub(json.loads(glued)) == json.loads(glued) and scrub.json_text(glued) == glued
    assert scrub.json_text("a\\u00e9\\ud83d\\ude00\\ud800b") == "aé😀b"  # a pair, a lone half


#: Tokens whose patterns want a boundary before them (the review of #380).
_GUARDED_TOKENS = [
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlLXZhbHVl",  # a JWT
    "ghp_" + "R" * 36,
    "sk-ant-oat01-" + "S" * 40,
    "token=" + "T" * 24,
]
#: The escapes `json_text` keeps, each ending in a word character.
_KEPT = ["\\t", "\\n", "\\u0009", "\\u000a", "\\u0022", "\\u005c"]


@pytest.mark.parametrize("escape", _KEPT)
@pytest.mark.parametrize("token", _GUARDED_TOKENS)
def test_json_text_finds_a_whole_token_right_after_a_kept_escape(
    scrub: Scrubber, token: str, escape: str
) -> None:
    """The review of #380's 24 cases: a kept escape ends in a word character, which a pattern
    reads as glued to the token after it; decoded, a tab or a quote is there. So spans are
    also found on a view where each kept escape reads as spaces."""
    text = f'{{"doctor_failures": ["auth.json{escape}{token} tail"]}}\n'
    decoded = json.loads(text)["doctor_failures"][0]
    assert token[-8:] not in scrub(decoded)  # what a scrub of the string finds
    out = scrub.json_text(text)
    assert token[-8:] not in out and "[redacted " in out, out
    assert json.loads(out)["doctor_failures"][0].endswith(" tail")  # still JSON


def test_json_text_finds_what_a_scrub_of_the_decoded_strings_finds(scrub: Scrubber) -> None:
    """A fuzz on status.py-shaped documents: hostile strings of words, separators and tokens,
    a token sometimes split by an invisible character (in scope, §2.7), never glued to a
    printable letter (out of scope); dumped with and without `indent` and `ensure_ascii`.
    Every token a scrub of its decoded string redacts is redacted by `json_text` too, and the
    output stays JSON."""
    rng = random.Random(380)  # noqa: S311 - reproducible cases, not secrets
    tokens = [*_GUARDED_TOKENS, ZULIP_KEY, GH_TOKEN, OTHER, "AKIA" + "Q" * 16]
    separators = [" ", "\t", "\n", '"', "\\", "/", ":", "=", ", ", "\r", "\x0c", "\x00",
                  "\u200b", "\x1b[31m", "\x9b", "\xad", "\u202e", "é", "@**all**"]  # fmt: skip
    invisible = ["\u200b", "\x00", "\xad", "\ufeff", "\x1b"]

    def hostile() -> tuple[str, list[str]]:
        parts, planted = [], []
        for _ in range(rng.randint(1, 4)):
            parts += [rng.choice(["status", "codex:login", "x"]), rng.choice(separators)]
            token = rng.choice(tokens)
            planted.append(token)
            if rng.random() < 0.3:
                cut = rng.randint(4, len(token) - 4)
                token = token[:cut] + rng.choice(invisible) + token[cut:]
            parts += [token, rng.choice(separators)]
        return "".join(parts), planted

    leaks = []
    for n in range(1000):
        strings = [hostile() for _ in range(4)]
        vm = {"slug": f"vm-{n}", "bridge": "active", "last_error": strings[0][0],
              "doctor_failures": [strings[1][0], strings[2][0]],
              "idle_stop": {"enabled": True, "last_verdict": strings[3][0]}}  # fmt: skip
        text = json.dumps(
            {"schema": 1, "vms": [vm]},
            indent=rng.choice([None, 2]),
            ensure_ascii=rng.choice([True, False]),
        )
        out = scrub.json_text(text)
        assert json.loads(out)["vms"][0]["slug"] == f"vm-{n}"
        for value, planted in strings:
            caught = scrub(value)
            leaks += [(text, t) for t in planted if t[-8:] not in caught and t[-8:] in out]
    assert leaks == []


def _hostile(n: int) -> list[str]:
    """Shapes that cost a decoder dearly (the review of #380), and the escapes that undo into
    the scan's slowest path, about `n` characters each."""
    nested = "[" * 900 + '"' + ZULIP_KEY[:9] + "\\u200b" + ZULIP_KEY[9:] + '"' + "]" * 900
    introducers = "\\u009beyJ" * 400 + "\n"  # a U+009B before every token-like place
    return [
        "[\n" * (n // 2),  # each line a nesting decode
        ("[" + "1," * 499 + "\n") * (n // 1000),  # unterminated, each try to the end
        (nested + "\n") * (n // 1900),  # deep documents, each redacted
        json.dumps([f"a{i}" for i in range(n // 11)], indent=1),  # a string per line
        "\\" * n,  # escape characters only
        "\\u0041" * (n // 6),  # escapes only
        introducers * (n // len(introducers)),
        ("\\t" + introducers) * (n // (len(introducers) + 2)),  # and the view scanned too
    ]


#: The two introducer shapes (a U+009B before every token-like place), the slowest: 2.5 and
#: 4 s here at a million characters, 13 s on CI. A pull request times them at a quarter of
#: that, where their ratios hold (1.07 and 2.10, against 1.09 and 2.11); a push to main,
#: whole (`slow`, #473). Being against a scan of the same text, they bound `json_text`'s own
#: passes, not the scan: a slower scan of these places slows both sides, and they pass (the
#: review of #476). The scan's work per place is the twice-main test's, below (#485).
@pytest.mark.parametrize(
    ("index", "n"),
    [
        *(pytest.param(i, 1_000_000, id=str(i)) for i in range(6)),
        *(pytest.param(i, 250_000, id=f"{i}-quarter") for i in (6, 7)),
        *(pytest.param(i, 1_000_000, marks=pytest.mark.slow, id=str(i)) for i in (6, 7)),
    ],
)
def test_json_text_costs_two_scans_at_most_and_never_grows_the_text(index: int, n: int) -> None:
    """§2.7 item 5: two linear passes, then a scan of the text undone, and of its view when a
    kept escape makes one. Timed against one scan of the text undone, in the same run, best of
    three, so that CI's slower or busy cores cancel out. Here: about 1 on most shapes, 2.1 on
    the slowest, `\\u009beyJ` with a tab on each line (0.7 s for its million characters), 3.2
    on backslashes alone, and 4.1 on escapes alone, whose scan is short (0.01 s). The output
    is never longer than the input, but for the redactions' own words."""
    text = _hostile(n)[index]
    undone = scrub_module._JSON_ESCAPE.sub(scrub_module._unescape, text)
    scrub = Scrubber([ZULIP_KEY, GH_TOKEN, OTHER])

    def best(call: Any, arg: str) -> float:
        times = []
        for _ in range(3):
            start = time.process_time()
            call(arg)
            times.append(time.process_time() - start)
        return min(times)

    assert best(scrub.json_text, text) < 6 * best(scrub, undone) + 0.2
    out = scrub.json_text(text)
    assert len(out) <= len(text) + len(REDACTED) * (text.count("\\u200b") + 1)
    assert ZULIP_KEY[9:] not in out


def test_json_text_bounds_its_input_before_its_pass() -> None:
    """`MAX_SCRUB_CHARS` cuts the text before the escapes are undone, so their shrinking can't
    let more than a million characters through, and the note counts what was cut."""
    line = "\\u0041" * 999 + "\n"
    text = line * (2 * MAX_SCRUB_CHARS // len(line))
    out = Scrubber().json_text(text)
    assert len(out) < MAX_SCRUB_CHARS // 5  # what fits of a million characters, undone
    assert out.endswith(" more characters not scrubbed]\n"), out[-120:]


def test_record_keeps_a_harmless_value_under_a_mangled_name() -> None:
    assert Scrubber().record({"note\x00": "ok", "re\x1b[0msult": "ok"}) == {
        "note": "ok",
        "re[0msult": "ok",
    }


def test_codex_round_7_eight_bit_csi(scrub: Scrubber) -> None:
    """U+009B is a CSI introducer in the escape-free view, as `ESC [` is."""
    out = scrub(OTHER[:10] + "\x9b31m" + OTHER[10:])
    assert out == REDACTED
    assert scrub.record({"note": OTHER[:10] + "\x9b31m" + OTHER[10:]}) == {"note": REDACTED}


def test_codex_round_6_suffixes() -> None:
    """Nothing is redacted before stripping, so no first pass can split a secret."""
    out = Scrubber()("SecretAccessKey=" + "w" * 20 + "\x00" + "/" + "K" * 19)
    assert out == "SecretAccessKey=[redacted aws secret]"
    registered = "sk-ant-api03-" + "x" * 20 + "!tail-of-it-0123456789"  # a pattern takes its start
    out = Scrubber([registered])(f"<{registered[:30]}\x00{registered[30:]}>")
    assert out.count("[redacted") == 1 and "tail-of-it" not in out and "0123456789" not in out


@pytest.mark.parametrize("sep", ["\x9b", "\x00", chr(0x200B), "\x1b[0m", "\x1b[1;31m\x00"])
def test_an_invisible_separator_still_starts_a_token(sep: str) -> None:
    """`x<sep>ghp_…` shows as `xghp_…`, but the token started after a separator."""
    token = "ghp_" + "A" * 36
    out = Scrubber()(f"x{sep}{token}")
    assert token[4:12] not in out and "[redacted github token]" in out
    assert Scrubber()(f"x{token}") == f"x{token}"  # glued in the input too: §11.6 leaves it


def test_a_secret_found_twice_is_one_redaction(scrub: Scrubber) -> None:
    """Registered, and a zuliprc line too: the spans are merged, the first marker kept."""
    out = scrub(f"key={ZULIP_KEY}")
    assert ZULIP_KEY[:8] not in out and out.count("[redacted") == 1


def test_short_and_blank_values_are_not_registered() -> None:
    s = Scrubber(["", "  ", "abc", "elevenchars", f"  {ZULIP_KEY}\n"])
    assert len(s) == 1
    assert s("abc elevenchars") == "abc elevenchars"
    assert s(ZULIP_KEY) == REDACTED


def test_set_secrets_replaces_the_registry(scrub: Scrubber) -> None:
    new = "NewBotKey0123456789abcdefghijklm"
    scrub.set_secrets([new])
    assert scrub(new) == REDACTED
    assert scrub(f"<{OTHER}>") == f"<{OTHER}>"


def test_repr_shows_no_secret(scrub: Scrubber) -> None:
    assert repr(scrub) == "Scrubber(3 secrets)"
    assert ZULIP_KEY not in repr(scrub) and ZULIP_KEY not in str(scrub)


def test_read_secrets(tmp_path: Path) -> None:
    rc = tmp_path / "bot.zuliprc"
    rc.write_text(f"[api]\nemail=bot@example.org\nkey = {ZULIP_KEY}\nsite=https://x\n")
    token = tmp_path / "github-token"
    token.write_text(GH_TOKEN + "\n")
    key = tmp_path / "id_ed25519"
    body = ["b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW", "QyNTUx"]
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n" + "\n".join(body) + "\n"
                   "-----END OPENSSH PRIVATE KEY-----\n")  # fmt: skip
    found = read_secrets(
        zuliprcs=[rc, tmp_path / "missing.zuliprc"], files=[token, key, tmp_path / "nope"]
    )
    assert found == {ZULIP_KEY, GH_TOKEN, *body}
    s = Scrubber(found)
    assert body[0] in found and s(f"x{body[0]}y") == f"x{REDACTED}y"
    assert s("-----BEGIN OPENSSH PRIVATE KEY-----") != REDACTED  # armor is no secret


# A zuliprc's key in every form configparser, the loaders' reader, accepts (#526). Each is a
# decoy: 32 alphanumerics no pattern knows without a `key=` or `key:` right before it.
DECOY = "Dq3Rm8Kw2Lz5Vn7Bx4Ct9Hs6Jp1Fg0Ya"
CONTINUED = ("Ct8Wq2Ln5Rx7Zm3Kp", "Vb9Hd4Js6Fy1Gt0Ue")  # one value, on two lines
ZULIPRC_FORMS = {
    "key=": f"[api]\nemail=bot@example.org\nkey={DECOY}\nsite=https://chat.example.org\n",
    "key = ": f"[api]\nkey = {DECOY}\n",
    "key: ": f"[api]\nemail: bot@example.org\nkey: {DECOY}\n",
    "key:": f"[api]\nkey:{DECOY}\n",
    "key : ": f"[api]\nkey : {DECOY}\n",
    "KEY = ": f"[api]\nKEY = {DECOY}\n",
    "Key: ": f"[api]\nKey: {DECOY}\n",
    "tabs": f"[api]\nkey\t=\t{DECOY}\t\n",
    "wide spaces": f"[api]\nkey  \u00a0=\u3000 {DECOY}  \n",
    "indented, first in its section": f"[api]\n    key = {DECOY}\n",
    "on the next line": f"[api]\nkey =\n    {DECOY}\nsite=https://chat.example.org\n",
    "after a blank line, indented": f"[api]\nkey =\n\n    {DECOY}\n",
    "in [DEFAULT]": f"[DEFAULT]\nkey: {DECOY}\n[api]\nsite=https://chat.example.org\n",
    "in another section": f"[other]\nKey={DECOY}\n",
    "before any section": f"key: {DECOY}\n[api]\nsite=https://chat.example.org\n",
    "no section at all": f"KEY = {DECOY}\n",
    "quoted": f'[api]\nkey = "{DECOY}"\n',
    "a duplicate, first": f"[api]\nkey: {DECOY}\nkey = {'Zz9' * 11}\n",
    "a duplicate, last": f"[api]\nkey = {'Zz9' * 11}\nKEY: {DECOY}\n",
    "a duplicate section": f"[api]\nkey={'Zz9' * 11}\n[api]\nkey: {DECOY}\n",
    "after a line no parser reads": f"[api]\nthis line has no separator\nkey: {DECOY}\n",
    "indented, read as a continuation": f"[api]\nemail = bot@example.org\n  key: {DECOY}\n",
    "with a BOM and CRLF": f"\ufeff[api]\r\nKey : {DECOY}\r\n",
    # kept in the value by configparser and the loaders, which read no inline comment
    "an inline comment": f"[api]\nkey = {DECOY} ; regenerated 2026-10-19\n",
    "quoted, an inline comment": f"[api]\nKEY: '{DECOY}'  # the new one\n",
    "a word before an inline comment": f"[api]\nkey = {DECOY} new # mine\n",
    # a value continued in pieces shorter than MIN_SECRET_LEN: their join is the key
    "continued in short pieces": f"[api]\nkey = {DECOY[:11]}\n  {DECOY[11:22]}\n  {DECOY[22:]}\n",
}


@pytest.mark.parametrize("text", ZULIPRC_FORMS.values(), ids=ZULIPRC_FORMS.keys())
def test_a_zuliprc_key_is_learned_in_every_form(tmp_path: Path, text: str) -> None:
    """Each form is learned, and the key is then scrubbed bare from what would be posted, which
    no pattern does without the registry (#526)."""
    rc = tmp_path / "codex.zuliprc"
    rc.write_text(text, encoding="utf-8")
    found = read_secrets(zuliprcs=[rc])
    assert DECOY in found
    posted = f"step 3 failed: auth {DECOY} refused\n"
    assert Scrubber()(posted) == posted  # the registry alone redacts it
    assert Scrubber(found)(posted) == f"step 3 failed: auth {REDACTED} refused\n"
    assert DECOY not in Scrubber(found)(text)  # the file's own text, quoted in an error


def test_a_continued_zuliprc_key_is_learned_whole_and_by_line(tmp_path: Path) -> None:
    """configparser reads a value continued on indented lines as one, joined by newlines (#526):
    the value is registered whole, as a loader would send it, and each line on its own."""
    first, second = CONTINUED
    rc = tmp_path / "codex.zuliprc"
    rc.write_text(f"[api]\nkey = {first}\n    {second}\nsite=https://chat.example.org\n")
    found = read_secrets(zuliprcs=[rc])
    assert {f"{first}\n{second}", first, second} <= found
    s = Scrubber(found)
    assert s(f"a {first} b {second} c") == f"a {REDACTED} b {REDACTED} c"
    basic = base64.b64encode(f"bot@example.org:{first}\n{second}".encode()).decode()
    assert REDACTED in s(f"Authorization: Basic {basic}")
    assert first not in s(rc.read_text()) and second not in s(rc.read_text())


def test_nothing_but_a_zuliprc_key_is_learned(tmp_path: Path) -> None:
    """Other options, names that only contain `key`, comments and section names are no key, in
    a well-formed file and in a malformed one alike (#526)."""
    others = {
        "email": "codex-bot-alice@chat.example.org",
        "site": "https://chat.example.org",
        "api_key": "Ak4" * 11,
        "keyring": "Kr5" * 11,
        "key file": "Kf6" * 11,
        "client": "ZulipAgentBridge/0.2",
    }
    lines = [f"{name} = {value}" for name, value in others.items()]
    comments = ["# key = Cm7Cm7Cm7Cm7Cm7Cm7Cm7Cm7", "; KEY: Cm8Cm8Cm8Cm8Cm8Cm8Cm8"]
    sections = ["[key = Sk9Sk9Sk9Sk9Sk9Sk9]", "[api]"]
    for body in (
        "\n".join([sections[0], *comments, sections[1], *lines, f"key={DECOY}", ""]),
        "\n".join([*lines, *comments, f"KEY: {DECOY}", "no separator", "[unclosed", ""]),
    ):
        rc = tmp_path / "bot.zuliprc"
        rc.write_text(body)
        assert read_secrets(zuliprcs=[rc]) == {DECOY}
        s = Scrubber(read_secrets(zuliprcs=[rc]))
        prose = " ".join([*others.values(), "Cm7Cm7Cm7Cm7Cm7Cm7Cm7Cm7", "Sk9Sk9Sk9Sk9Sk9Sk9"])
        assert s(prose) == prose


NAMELESS = "Nm2" * 11  # the value of an option with no name: no key
#: A file the loaders refuse, or read oddly, and exactly what is learned from it.
MALFORMED: dict[str, tuple[str | bytes, set[str]]] = {
    "garbage": ("\x00\x01 not ini at all \xff\n[[[\n===\n:::\n", set()),
    "options with no name": (f"[api]\n= {NAMELESS}\n: {NAMELESS}\n", set()),
    "an unclosed section": (f"[api\nkey: {DECOY}\n", {DECOY}),
    "an empty section name": (f"[]\nKEY = {DECOY}\n", {DECOY}),
    "a key with no value": ("[api]\nkey =\nkey:\n  \n", set()),
    "interpolation syntax, kept": (f"[api]\nkey = %(x)s{DECOY}%\n", {f"%(x)s{DECOY}%"}),
    "invalid UTF-8, replaced": (b"[api]\nkey = \xff" + DECOY.encode() + b"\n", {"\ufffd" + DECOY}),
    "empty": ("", set()),
    "only blank lines": ("\n\n   \n", set()),
}


@pytest.mark.parametrize(("content", "learned"), MALFORMED.values(), ids=MALFORMED.keys())
def test_a_malformed_zuliprc_never_fails_the_reload(
    tmp_path: Path, content: str | bytes, learned: set[str]
) -> None:
    """A file the loaders refuse raises nothing, and still gives each line that reads as a `key`
    option; its text, quoted in an error, keeps none of them (#526)."""
    rc = tmp_path / "bot.zuliprc"
    if isinstance(content, bytes):
        rc.write_bytes(content)
    else:
        rc.write_text(content, encoding="utf-8")
    found = read_secrets(zuliprcs=[rc])
    assert found == learned
    text = rc.read_text(encoding="utf-8", errors="replace")
    assert not any(value in Scrubber(found)(text) for value in learned)


#: The slow shapes (the review of #530), each a key line and then a body of `n` characters:
#: configparser before 3.13 is quadratic in the first three, a line with no `=` or `:`, and in
#: the fourth, its count of unreadable lines; the last was the line pattern's, when its value
#: was lazy (about 4 s for 40 KB).
SLOW_SHAPES = {
    "no separator": lambda n: "x" + " " * n + "y\n",
    "a name, no separator": lambda n: "key" + " " * n + "x\n",
    "wide spaces, no separator": lambda n: "key" + "\u3000" * n + "x\n",
    "unreadable lines": lambda n: "x\n" * (n // 2),
    "a key line's trailing spaces": lambda n: f"key = {DECOY[:16]}" + " " * n + "x\n",
}


@pytest.mark.parametrize("shape", SLOW_SHAPES.values(), ids=SLOW_SHAPES.keys())
@pytest.mark.parametrize("over", [False, True], ids=["at the bound", "over it"])
def test_reading_a_zuliprc_takes_no_time(tmp_path: Path, shape: Any, over: bool) -> None:
    """At most `_PARSE_MAX` characters, configparser reads the file too (its worst case: 0.07 s
    on 3.11 and 3.12); beyond it, the linear line reading alone. 400,000 characters through
    configparser would take minutes on 3.11 and 3.12, the server's; here, milliseconds."""
    head = f"[api]\nkey = {DECOY}\n"
    room = scrub_module._PARSE_MAX - len(head) - len(shape(0))
    body = shape(400_000 if over else room)
    assert (len(head + body) > scrub_module._PARSE_MAX) == over
    rc = tmp_path / "bot.zuliprc"
    rc.write_text(head + body)
    start = time.process_time()
    found = read_secrets(zuliprcs=[rc])
    assert time.process_time() - start < 2
    assert DECOY in found


def test_configparser_reads_a_zuliprc_up_to_the_bound(tmp_path: Path) -> None:
    """A value continued on indented lines, which only configparser reads, is learned in a file
    of `_PARSE_MAX` characters; one character more, its first line alone is (#530's review)."""
    first, second = CONTINUED
    text = f"[api]\nkey = {first}\n    {second}\n"
    filler = "#" * (scrub_module._PARSE_MAX - len(text) - 1) + "\n"  # a comment line
    rc = tmp_path / "bot.zuliprc"
    rc.write_text(filler + text)
    assert {f"{first}\n{second}", first + second} <= read_secrets(zuliprcs=[rc])
    rc.write_text("#" + filler + text)
    assert read_secrets(zuliprcs=[rc]) == {first}


def _pad(rng: random.Random) -> str:
    return rng.choice(["", " ", "\t", "  \t"])


def test_every_key_line_learned_before_is_still_learned(tmp_path: Path) -> None:
    """No regression (#526): every value main's `key=` rule learned is still learned, on random
    lines that rule matches, indented ones too, which configparser reads as continuations."""
    before = re.compile(r"^[ \t]*key[ \t]*=[ \t]*(\S+)[ \t]*$", re.MULTILINE)  # main's rule
    rng = random.Random(526)  # noqa: S311 - reproducible cases, not secrets
    alphabet = "AbCdEfGhJkMnPqRsTuVwXyZ0123456789+/=_-.:"
    rc = tmp_path / "bot.zuliprc"
    for _ in range(200):
        values = ["".join(rng.choices(alphabet, k=rng.randint(12, 48))) for _ in range(3)]
        lines = ["[api]", "email = bot@example.org"]
        lines += [f"{_pad(rng)}key{_pad(rng)}={_pad(rng)}{v}{_pad(rng)}" for v in values]
        text = "\n".join(lines) + "\n"
        rc.write_text(text)
        old = {m.group(1) for m in before.finditer(text)}
        assert old == set(values) and old <= read_secrets(zuliprcs=[rc])


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_read_secrets_skips_an_unreadable_file(tmp_path: Path) -> None:
    token = tmp_path / "github-token"
    token.write_text(GH_TOKEN)
    token.chmod(0)
    assert read_secrets(files=[token]) == set()


# --------------------------------------------------------------------- step 2: the patterns

PATTERNS = [
    ("sk-" + "a" * 24, "[redacted openai key]"),
    ("sk-proj-" + "Ab_-" * 6, "[redacted openai key]"),
    ("sk-ant-api03-" + "x" * 30, "[redacted anthropic key]"),
    ("ghp_" + "A" * 36, "[redacted github token]"),
    ("github_pat_" + "B" * 60, "[redacted github token]"),
    ("AKIA" + "ABCDEFGHIJKLMNOP", "[redacted aws access key]"),
    ("ASIA" + "ABCDEFGHIJKLMNOP", "[redacted aws access key]"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", "[redacted jwt]"),
    (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----",
        "[redacted private key]",
    ),
    (
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nAAAA\n-----END PGP PRIVATE KEY BLOCK-----",
        "[redacted pgp private key]",
    ),
    ("key = " + "k" * 32, "key = [redacted zulip api key]"),
    ("ZULIP_API_KEY='" + "k" * 32 + "'", "ZULIP_API_KEY=[redacted zulip api key]"),
    ("curl -u bot@x:" + "k" * 32, "curl -u bot@x:[redacted zulip api key]"),
    # admin_common.redact's rule, anywhere in a line, and a Zulip API response
    ("GET /x?api_key=" + "k" * 32 + "&a=1", "GET /x?api_key=[redacted zulip api key]&a=1"),
    (
        '{"result":"success","api_key":"' + "k" * 32 + '"}',
        '{"result":"success","api_key":"[redacted zulip api key]"}',
    ),
    ("KEY: " + "k" * 32, "KEY: [redacted zulip api key]"),
    # AWS secret keys and session tokens after their names (IMDS, STS, export-credentials)
    ("aws_secret_access_key = " + AWS_KEY, "aws_secret_access_key = [redacted aws secret]"),
    (
        "export AWS_SECRET_ACCESS_KEY=" + AWS_KEY,
        "export AWS_SECRET_ACCESS_KEY=[redacted aws secret]",
    ),
    ("AWS_SESSION_TOKEN=" + AWS_TOKEN, "AWS_SESSION_TOKEN=[redacted aws secret]"),
    ('"SecretAccessKey" : "' + AWS_KEY + '",', '"SecretAccessKey" : "[redacted aws secret]",'),
    ('"SessionToken": "' + AWS_TOKEN + '"', '"SessionToken": "[redacted aws secret]"'),
    ('"Token" : "' + AWS_TOKEN + '"', '"Token" : "[redacted aws secret]"'),
]


@pytest.mark.parametrize(("secret", "expected"), PATTERNS)
def test_patterns(secret: str, expected: str) -> None:
    out = Scrubber()(f"before {secret} after")
    assert out == f"before {expected} after"


@pytest.mark.parametrize(("secret", "expected"), PATTERNS)
def test_patterns_survive_a_leading_escape(secret: str, expected: str) -> None:
    """An escape can't eat a pattern's first characters (`ESC [ g` is CSI): reviewer finding 1.

    Stripping deletes only the ESC: the `[` stays, and so does all of the marker.
    """
    for prefix in ("\x1b[", "\x1b", "\x1b[1m", "x\x1b[0m"):
        out = Scrubber()(f"{prefix}{secret}")
        marker = re.search(r"\[redacted [a-z ]+\]", expected)
        assert marker is not None and marker.group() in out, (prefix, out)
        assert secret[2:] not in out, (prefix, out)  # a bare `key = …` line goes whole


@pytest.mark.parametrize(
    "text",
    [
        "https://example.org/disk-usage-report-for-all-the-vms-today",
        AWS_KEY,  # no name, no redaction
        f"sha {AWS_KEY} and Token: abc",
        '{"tokens": "' + AWS_TOKEN + '"}',
        "nextpagetoken=q7Xr2mPzLk9VbN4tGh8s",  # `token` only where a word starts
    ],
)
def test_no_false_positive(text: str) -> None:
    assert Scrubber()(text) == text


# ---------------------------------------------------------------------- step 3: mentions

MENTIONS = [
    "@**all**",
    "@**everyone**",
    "@**channel**",
    "@**stream**",
    "@**topic**",
    "@**Drew Sutherland**",
    "@**Workshop admin|12** !approve P-42 3f9a2c",
    "@*organizers*",
    "```\n@**all**\n```",
]


@pytest.mark.parametrize("text", MENTIONS)
def test_mentions_are_disarmed(scrub: Scrubber, text: str) -> None:
    out = scrub(text)
    assert not _ACTIVE.search(out), out
    assert out.replace("\u200b", "") == text


# ------------------------------------------------------------- step 0, fences and limits


def test_strip() -> None:
    """Each invisible character goes on its own; nothing visible does, `[31m` included."""
    rlo, zwj, ls = chr(0x202E), chr(0x200D), chr(0x2028)
    raw = f"a\x07b \x1b[1;31mred\x1b[0m\r\nc{rlo}d\x7f\x85{zwj}{ls} \x9b2Jx\tz"
    assert strip(raw) == "ab [1;31mred[0m\ncd 2Jx\tz"


# ------------------------------------------------- the invariant, as properties (review)

#: Characters that print nothing: C0 and C1 controls (ESC, the 8-bit CSI), line and paragraph
#: separators, zero-width characters, bidi controls, BOM, soft hyphen, a variation selector.
INVISIBLES = [
    chr(c)
    for c in (0x1B, 0x9B, 0x9D, 0x00, 0x07, 0x0D, 0x7F, 0x85, 0x2028, 0x2029, 0x200B, 0x200C,
              0x200D, 0x2060, 0x202E, 0x2066, 0xFEFF, 0x00AD, 0xFE0F, 0x180E, 0xE0041)
]  # fmt: skip
ZK = "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"  # a Zulip key's shape, made up
AK = "q7Xr/2mPz+Lk9VbN4tGh8sWe1cYd6fJu3aRo5iKl"  # an AWS secret key's shape, made up
FIELDS = [  # (text, the secret in it): pattern-only secrets in their usual contexts
    (f"aws_secret_access_key = {AK}", AK),
    (f"export AWS_SESSION_TOKEN={AK}{ZK}", f"{AK}{ZK}"),
    (f'{{"SecretAccessKey": "{AK}"}}', AK),
    (f'"Token" : "{AK}"', AK),
    (f"GET /x?api_key={ZK}&a=1", ZK),
    (f'{{"api_key":"{ZK}"}}', ZK),
    (f"key = {ZK}", ZK),
    (f"ZULIP_API_KEY='{ZK}'", ZK),
    (f"curl -u bot@example.org:{ZK} https://x", ZK),
    (f"token ghp_{ZK}1234", f"ghp_{ZK}1234"),
    (f"use sk-ant-api03-{ZK} now", f"sk-ant-api03-{ZK}"),
    ("id AKIA" + "Q7XR2MPZLK9VBN4T", "AKIAQ7XR2MPZLK9VBN4T"),
    (f"jwt eyJ{ZK[:9]}.eyJ{ZK[9:20]}.{ZK[20:]}", f"eyJ{ZK[:9]}.eyJ{ZK[9:20]}.{ZK[20:]}"),
]


def _sprinkle(rng: random.Random, text: str, chars: list[str], k: int) -> str:
    out = list(text)
    for _ in range(k):
        out.insert(rng.randrange(len(out) + 1), rng.choice(chars))
    return "".join(out)


def _runs(secret: str, out: str, n: int = 8) -> list[str]:
    """The runs of `n` consecutive characters of `secret` that `out` shows."""
    return [secret[i : i + n] for i in range(len(secret) - n + 1) if secret[i : i + n] in out]


def test_no_invisible_character_lets_a_registered_secret_through() -> None:
    secrets = [ZULIP_KEY, GH_TOKEN, OTHER, B64_SECRET]
    scrub = Scrubber(secrets)
    for seed in range(400):
        rng = random.Random(seed)  # noqa: S311 - reproducible cases, not secrets
        secret = rng.choice(secrets)
        text = _sprinkle(rng, f"pre {secret} post", INVISIBLES, rng.randint(1, 12))
        out = scrub(text)
        assert not _runs(secret, out), (seed, text, out)


def test_no_invisible_character_lets_a_field_secret_through() -> None:
    """Inside the name, between it and `=`, inside the value: anywhere."""
    scrub = Scrubber()
    for seed in range(600):
        rng = random.Random(seed)  # noqa: S311 - reproducible cases, not secrets
        text, secret = rng.choice(FIELDS)
        sprinkled = _sprinkle(rng, text, INVISIBLES, rng.randint(1, 10))
        out = scrub(sprinkled)
        assert not _runs(secret, out), (seed, sprinkled, out)


def test_no_escape_sequence_lets_a_secret_through() -> None:
    """Colors inside a secret, with invisible characters anywhere, even inside the colors."""
    sgr = ["\x1b[0m", "\x1b[1;31m", "\x1b[38;5;196m", "\x1b[K", "\x1b[2J", "\x9b31m", "\x9b0m"]
    # Colors inside a secret go beyond the threat model, which covers sequences next to or
    # around one. This test still covers them, but without a lone U+009B: spliced into an 8-bit
    # SGR inside the secret (`U+009B 0 U+009B m`), it leaves `0m` behind under either reading.
    invisibles = [c for c in INVISIBLES if c != "\x9b"]
    scrub = Scrubber([ZULIP_KEY, GH_TOKEN, OTHER])
    cases = [(f"pre {s} post", s) for s in (ZULIP_KEY, GH_TOKEN, OTHER)] + FIELDS
    for seed in range(600):
        rng = random.Random(seed)  # noqa: S311 - reproducible cases, not secrets
        text, secret = rng.choice(cases)
        colored = _sprinkle(rng, text, sgr, rng.randint(1, 4))
        sprinkled = _sprinkle(rng, colored, invisibles, rng.randint(0, 6))
        out = scrub(sprinkled)
        assert not _runs(secret, out), (seed, sprinkled, out)


def test_record_fields_resist_invisible_characters() -> None:
    scrub = Scrubber()
    names = {"SecretAccessKey": AK, "SessionToken": AK, "Token": AK, "api_key": ZK}
    for seed in range(300):
        rng = random.Random(seed)  # noqa: S311 - reproducible cases, not secrets
        name, secret = rng.choice(sorted(names.items()))
        key = _sprinkle(rng, name, INVISIBLES, rng.randint(0, 3))
        value = _sprinkle(rng, secret, INVISIBLES, rng.randint(0, 3))
        out = scrub.record({"response": {key: value}})
        assert not _runs(secret, str(out)), (seed, key, value, out)


def _closes(fenced: str) -> list[int]:
    """Lines at which Zulip would end the block: a line equal to the opening fence."""
    lines = fenced.split("\n")
    opening = re.match(r"`+", lines[0])
    assert opening is not None
    return [i for i, line in enumerate(lines) if i and line.rstrip() == opening.group()]


FENCE_BREAKS = [
    "```",
    "````\n[click](https://evil)\n````",
    "x\n```\n@**all**\n```quote\ny",
    "~~~\n`````\n~~~",
    "",
    "trailing `` and ```",
]


@pytest.mark.parametrize("text", FENCE_BREAKS)
def test_nothing_inside_closes_the_fence(text: str) -> None:
    fenced = fence(text)
    lines = fenced.split("\n")
    assert _closes(fenced) == [len(lines) - 1], fenced  # only the last line closes it
    assert lines[0].endswith("text")


@pytest.mark.parametrize("text", FENCE_BREAKS)
def test_block_is_fenced(scrub: Scrubber, text: str) -> None:
    fenced = scrub.block(text).inline
    assert _closes(fenced) == [len(fenced.split("\n")) - 1]


def test_span() -> None:
    assert span("a`b") == "``a`b``"
    assert span("`x`") == "`` `x` ``"
    assert span("one\ntwo") == "`one two`"


def test_block_keeps_40_lines_inline(scrub: Scrubber) -> None:
    text = "\n".join(f"line {i} {ZULIP_KEY}" for i in range(100)) + "\n"
    b = scrub.block(text)
    inner = b.inline.split("\n")[1:-1]
    assert len(inner) == 40 and inner[-1] == f"line 39 {REDACTED}"
    assert b.truncated
    assert b.full.count("\n") == 100 and ZULIP_KEY not in b.full


def test_block_cuts_long_lines_after_scrubbing(scrub: Scrubber) -> None:
    b = scrub.block("x" * 3980 + ZULIP_KEY, max_chars=4000)
    assert b.truncated and len(b.inline) <= 4000 and ZULIP_KEY[:10] not in b.inline
    assert b.inline.split("\n")[1] == "x" * 3980 + REDACTED[:8]
    assert not scrub.block("short\n").truncated


@pytest.mark.parametrize(
    ("text", "truncated"),
    [
        ("`" * 4000, True),  # codex round 1: the fences grow with the backtick run
        ("`" * 1990 + "\n" + "y" * 3000, True),
        ("z" * 3988, False),  # exactly INLINE_CHARS with "```text\n" and "\n```"
        ("z" * 3989, True),
        ("ab``" * 2000, True),
    ],
)
def test_inline_fits_with_its_fences(text: str, truncated: bool) -> None:
    b = Scrubber().block(text)
    assert len(b.inline) <= INLINE_CHARS and b.truncated is truncated
    assert _closes(b.inline) == [len(b.inline.split("\n")) - 1]


def test_block_caps_the_attachment() -> None:
    b = Scrubber().block("é" * ATTACHMENT_BYTES)
    assert len(b.full.encode("utf-8")) <= ATTACHMENT_BYTES
    assert b.full.endswith(f"[cut at {ATTACHMENT_BYTES} bytes]\n")


def test_idempotent(scrub: Scrubber) -> None:
    text = f"@**all** {ZULIP_KEY} sk-{'a' * 24} key={'k' * 32}\r\n\x1b[0m```"
    once = scrub(text)
    assert scrub(once) == once


# ------------------------------------------------------- cost on a VM's output (re-review 1)


@pytest.mark.parametrize(
    "unit",
    [
        pytest.param("\x1b[32mline 12345\x1b[0m ok\n", id="colored"),  # ordinary output
        # dense escapes, 8-bit CSI and invisible characters, in lines, so that all of the
        # megabyte is scrubbed rather than cut away: 8 s under tracemalloc, on main only
        pytest.param(
            ("a\x1bb\x9bc" + chr(0x200B) + "d" + chr(0x202E) + "\x00") * 12 + "\n",
            marks=pytest.mark.slow,
            id="dense",
        ),
    ],
)
def test_ten_megabytes_cost_a_bounded_time_and_memory(unit: str) -> None:
    """One call scrubs at most MAX_SCRUB_CHARS: 10 MB of a VM's output costs what 1 MB does.

    The limits are generous for CI: here such a call takes under a second and peaks under
    50 MB of Python allocations; before the bound and the compact indexes, 10 MB of colored
    output took 14 s and 1.26 GB. The dense case is `slow` (#473): a pull request runs the
    colored one, and `test_a_long_text_is_cut_at_a_line_before_scrubbing`, the bound itself.
    """
    text = unit * (10_000_000 // len(unit))
    scrub = Scrubber([ZULIP_KEY, GH_TOKEN, OTHER])
    start = time.process_time()
    scrub.block(text)
    assert time.process_time() - start < 30
    tracemalloc.start()
    try:
        scrub.block(text)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 250_000_000


def test_a_long_text_is_cut_at_a_line_before_scrubbing() -> None:
    lines = [f"line {n} " + "x" * 90 for n in range(20_000)]  # about 2 MB
    lines[15_000] = f"far away {ZULIP_KEY}"
    out = Scrubber()("\n".join(lines))
    assert len(out) <= MAX_SCRUB_CHARS + 100
    assert out.endswith(" more characters not scrubbed]\n") and "[output truncated: " in out
    assert ZULIP_KEY not in out and lines[-1] not in out
    assert out.rsplit("\n", 2)[-3].startswith("line ")  # whole lines only
    one_line = Scrubber()("y" * (MAX_SCRUB_CHARS + 1))
    assert one_line.startswith("[output truncated: ")  # no newline: only the note


def test_record_cuts_a_long_value() -> None:
    big = ("\x1b[31mline\x1b[0m ok\n" * 200_000) + ZULIP_KEY
    out = Scrubber().record({"output": big, "note": "short"})
    assert len(out["output"]) <= RECORD_VALUE_CHARS + 100
    assert "[output truncated: " in out["output"] and ZULIP_KEY not in out["output"]
    assert out["note"] == "short"
    (listed,) = Scrubber().record([big])  # a string in a list, or the record itself
    assert len(listed) <= RECORD_VALUE_CHARS + 100 and "[output truncated: " in listed


def test_a_token_file_saved_with_a_bom(tmp_path: Path) -> None:
    """A BOM is stripped from the registered value too, so the bare token is found (re-review 2)."""
    token = "Plain0Token0Value0123456789"  # noqa: S105 - made up; no pattern knows it
    path = tmp_path / "github-token"
    path.write_text(chr(0xFEFF) + token + "\r\n", encoding="utf-8")
    assert read_secrets(files=[path]) == {token}
    assert Scrubber(read_secrets(files=[path]))(f"<{token}>") == f"<{REDACTED}>"
    assert Scrubber([chr(0xFEFF) + token])(f"<{token}>") == f"<{REDACTED}>"
    assert Scrubber()(f"<{token}>") == f"<{token}>"


def test_botocore_credentials_repr() -> None:
    text = f"ReadOnlyCredentials(access_key='ASIA{'Q' * 16}', secret_key='{AK}', token='{AK}{ZK}')"
    out = Scrubber()(text)
    assert AK[:8] not in out and ZK[:8] not in out and "Q" * 8 not in out
    assert out.count("[redacted aws secret]") == 2


def test_the_invisible_table_matches_unicodedata() -> None:
    """The fixed table (Unicode 15.0) against the running interpreter's Unicode."""
    marks = {0x034F, *range(0x180B, 0x1810), *range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0)}
    invisible = {
        c
        for c in range(0x110000)
        if c not in (0x09, 0x0A)
        and (unicodedata.category(chr(c)) in {"Cc", "Cf", "Zl", "Zp", "Cs"} or c in marks)
    }
    table = {c for c in range(0x110000) if strip(chr(c)) == ""}
    assert invisible <= table, sorted(invisible - table)[:10]  # a newer Unicode: extend it
    extra = table - invisible
    assert all(unicodedata.category(chr(c)) == "Cn" for c in extra), sorted(extra)[:10]


# ------------------------------------------- a token glued only by what was removed (round 8)

GUARDED = [  # (token, the secret in it): every pattern that wants a boundary before it
    (f"ghp_{ZK}Q7xR", f"ghp_{ZK}Q7xR"),
    (f"github_pat_{ZK}{AK.replace('/', 'x').replace('+', 'y')}", f"github_pat_{ZK}"),
    (f"sk-{ZK}", f"sk-{ZK}"),
    (f"sk-proj-{ZK}", f"sk-proj-{ZK}"),
    (f"sk-ant-api03-{ZK}", f"sk-ant-api03-{ZK}"),
    ("AKIAQ7XR2MPZLK9VBN4T", "AKIAQ7XR2MPZLK9VBN4T"),
    ("ASIAQ7XR2MPZLK9VBN4T", "ASIAQ7XR2MPZLK9VBN4T"),
    (f"eyJ{ZK[:9]}.eyJ{ZK[9:20]}.{ZK[20:]}", f"eyJ{ZK[:9]}.eyJ{ZK[9:20]}.{ZK[20:]}"),
    (f"token='{AK}'", AK),
    # a prefix inside the token (codex round 9): nothing may split it there
    ("ghp_" + "A" * 15 + "token" + "B" * 16, "ghp_" + "A" * 15 + "token" + "B" * 16),
    (f"ghp_{ZK[:12]}eyJ{ZK[12:]}Q", f"ghp_{ZK[:12]}eyJ{ZK[12:]}Q"),
    (f"sk-{ZK[:10]}AKIA{ZK[10:]}", f"sk-{ZK[:10]}AKIA{ZK[10:]}"),
    (f"token='{AK[:10]}token{AK[10:]}'", f"{AK[:10]}token{AK[10:]}"),
]


def test_codex_round_9_a_prefix_inside_the_token() -> None:
    key = "ghp_" + "A" * 15 + "token" + "B" * 16
    assert Scrubber()("x\x00" + key) == "x[redacted github token]"
    assert Scrubber().record({"note": "x\x00" + key}) == {"note": "x[redacted github token]"}


@pytest.mark.parametrize(
    "text",
    [
        "x\x00ghp_" + "A" * 18 + "\x01" + "B" * 18,  # a removal before and one inside
        "x\x1b[31mghp_" + "A" * 36,  # a color just before
        "x\x9b31mghp_" + "A" * 36,  # an 8-bit color just before
        "word\x1b[1;31m\x1b[0msk-ant-api03-" + "C" * 30,  # two colors, then the token
        "x\x00token='" + AK + "'",  # a bare `token=` glued by a removal
    ],
)
def test_boundaries_recovered_without_splitting(text: str) -> None:
    out = Scrubber()(text)
    assert "[redacted " in out
    secret = strip(text)[-30:].strip("'")
    assert not _runs(secret, out), out


def test_codex_round_8_boundary_and_inside() -> None:
    text = "x\x00ghp_" + "A" * 18 + "\x00" + "A" * 18
    assert Scrubber()(text) == "x[redacted github token]"
    assert Scrubber().record({"note": text}) == {"note": "x[redacted github token]"}


def test_a_removed_boundary_and_removals_inside_a_token() -> None:
    """Glued to a word only by what was removed, and split inside by more of it: no view may
    lose the boundary and the token's contiguity at once."""
    scrub = Scrubber()
    separators = [*INVISIBLES, "\x1b[0m", "\x1b[1;31m", "\x9b32m"]
    for seed in range(600):
        rng = random.Random(seed)  # noqa: S311 - reproducible cases, not secrets
        token, secret = rng.choice(GUARDED)
        inside = _sprinkle(rng, token, INVISIBLES, rng.randint(1, 4))
        text = f"word{rng.choice(separators)}{inside} tail"
        out = scrub(text)
        assert not _runs(secret, out), (seed, text, out)


TOKEN_TAIL = "Ab3" + "31m" + "Xy4zQ7xRk9VbN4tGh8sWe1cYd6fJu3"  # 36 after `ghp_`, with "31m"
CSI8 = "\x9b"


@pytest.mark.parametrize(
    ("text", "why"),
    [
        # the 8-bit SGR is glued before the token and `U+009B 31m` sits inside it: only the
        # run rule (a removal anywhere in the run before the token) restores the boundary
        (f"word{CSI8}31mghp_Ab3{CSI8}{TOKEN_TAIL[3:]}", "run rule"),
        # a 7-bit SGR before, `U+009B` before `31m` inside: the 8-bit view breaks the token,
        # the stripped text keeps `[1m` glued; only the 7-bit view has both
        (f"word\x1b[1mghp_Ab3{CSI8}{TOKEN_TAIL[3:]}", "7-bit view"),
        # an 8-bit SGR before, a lone U+009B before a letter inside (fuzz seed 582): an
        # introducer in any shape would eat the letter
        ("pre \x00" + CSI8 + "0mgh" + chr(0xFEFF) + "p_Ab3dEf6" + chr(0x200D)
         + "hIj9kLm2nOp5qRs8tUv1" + CSI8 + "wXy4zQ7xR post", "SGR shape only"),
    ],
)  # fmt: skip
def test_codex_round_8_readings_of_u009b(text: str, why: str) -> None:
    out = Scrubber()(text)
    assert "[redacted github token]" in out, (why, out)
    assert not _runs(strip(text).split("ghp_")[1][:30], out), (why, out)


def test_a_glued_token_with_a_removal_elsewhere_stays_glued() -> None:
    """A gap needs a removal in the run before the token: `xghp_…` is no token (§11.6)."""
    text = "\x00 xghp_" + "A" * 36
    assert Scrubber()(text) == " xghp_" + "A" * 36


# ------------------------------------------------- a token's end glued by a removal (round 10)

TRAILING = [  # (text before the glued suffix, the secret): patterns with a trailing guard
    (f"key={ZK}", ZK),
    (f"KEY: {ZK}", ZK),
    (f"GET /x?api_key={ZK}", ZK),
    (f"jwt eyJ{ZK[:9]}.eyJ{ZK[9:20]}.{ZK[20:]}", f"eyJ{ZK[:9]}.eyJ{ZK[9:20]}.{ZK[20:]}"),
]


@pytest.mark.parametrize("sep", ["\x00", chr(0x202E), "\x1b[0m", "\x9b31m"])
def test_codex_round_10_a_removal_after_the_key(sep: str) -> None:
    out = Scrubber()("key=" + "K" * 32 + sep + "suffix")
    assert "K" * 8 not in out and "[redacted zulip api key]" in out and out.endswith("suffix")
    record = Scrubber().record({"api_key": "K" * 32 + sep + "x"})
    assert "K" * 8 not in str(record)


def test_a_key_glued_in_the_input_stays_glued() -> None:
    """No removal at its end: `key=<40 characters>` is no Zulip key (§11.6), with a removal
    inside it or not."""
    for text in ("key=" + "K" * 40, "key=" + "K" * 16 + "\x00" + "K" * 16 + "suffix"):
        assert Scrubber()(text) == strip(text)


def test_a_removed_boundary_after_a_token() -> None:
    scrub = Scrubber()
    separators = [*INVISIBLES, "\x1b[0m", "\x1b[1;31m", "\x9b32m"]
    for seed in range(400):
        rng = random.Random(seed)  # noqa: S311 - reproducible cases, not secrets
        text, secret = rng.choice(TRAILING)
        i = text.index(secret)
        inside = _sprinkle(rng, secret, INVISIBLES, rng.randint(0, 3))
        sample = f"{text[:i]}{inside}{rng.choice(separators)}suffix"
        out = scrub(sample)
        assert not _runs(secret, out), (seed, sample, out)


# ------------------------------------- an escape's tail glued before a token (issue #345)


def test_issue_345_a_non_colour_8_bit_csi_before_a_token() -> None:
    """No view removes `U+009B 107;254u` (or `U+009B 2K`): only an SGR is read as 8-bit. What
    it leaves is glued to the token, and `;` ends the run of word characters before the
    removed introducer is reached."""
    for text in (
        f"x \x9b107;254usk-{ZK}",
        f"\x9b107;254usk-{ZK}",  # at the start of the text
        f"x \x9b2Kghp_{ZK}1234",
        "x \x9b2KAKIAQ7XR2MPZLK9VBN4T",
        f"x \x9b2Kto\x00ken='{AK}'",
    ):
        out = Scrubber()(text)
        assert "[redacted " in out and ZK[:8] not in out and AK[:8] not in out, (text, out)
        assert "Q7XR2MPZ" not in out, (text, out)
        assert Scrubber().record({"note": text}) == {"note": out}


def test_issue_345_b_an_8_bit_sgr_before_and_a_lone_u009b_inside() -> None:
    """The 8-bit view reads `U+009B 48m` inside the token as an SGR and breaks it; the
    stripped text keeps the token whole, with `58m` glued before it."""
    text = " \x9b4;219;58mghp_6winqj\x9b48mx" + "Q" * 26 + " post"
    assert Scrubber()(text) == " 4;219;58m[redacted github token] post"


@pytest.mark.parametrize(
    "text",
    [
        f"x \x1b(Bghp_{ZK}1234",  # ESC ( B, as `tput sgr0` prints it, before the token
        f"x \x1bMghp_{ZK}1234",  # ESC M
        f"x \x9b1 qsk-{ZK}",  # an 8-bit CSI with an intermediate byte
        f"x \x9b!\x00psk-{ZK}",
        f"x \x9b\x00?25lsk-{ZK}",  # a control character after the introducer
        # a diff's deletion marker between the escape and the key, which §11.6 counts as a
        # key (the review of #349): a terminal shows ` -sk-…`
        f"x \x9b2K-sk-{ZK}",
        f"x \x1b(B-sk-{ZK}",
        f"x \x1b7-sk-{ZK}",
        f"x \x9b?25l-sk-proj-{ZK}",
    ],
)
def test_an_escape_with_intermediates_or_none_before_a_token(text: str) -> None:
    out = Scrubber()(text)
    assert "[redacted " in out and ZK[:8] not in out, out


def test_the_last_introducer_counts_not_an_earlier_one() -> None:
    """A colour long before, and an 8-bit CSI right before the token (the review of #349)."""
    text = "\x1b[1mxeyJ\x1b[0m " + "x" * 100 + " \x9b2Kghp_" + "A" * 36
    assert Scrubber()(text).endswith(" 2K[redacted github token]")


@pytest.mark.parametrize(
    "text",
    [
        # a whole sequence before a word (`ls --color`): §11.6's `task-`, `disk-` and slugs
        "\x1b[0m\x1b[01;34mflask-app-configuration-files\x1b[0m",
        "see \x1b[32mdisk-usage-report-for-all-the-vms-today\x1b[0m now",
        "\x1b[1mnextpagetoken=q7Xr2mPzLk9VbN4tGh8s",
        "\x1b[32mstatus\x1b[0m=ok;task-scheduler-configuration-v2",
        "\x9b31mtask-scheduler-configuration-v2",
        # a removal, but no escape's tail, before a word glued to a prefix
        "\x00xghp_" + "A" * 36,
        # an escape's tail, then a letter glued to the prefix
        "x \x9b2Kxghp_" + "A" * 36,
        "x \x1bMxghp_" + "A" * 36,
        "x \x9b12ghp_" + "A" * 36,  # a CSI never ends on a parameter: `12g` is one sequence
        # an ESC sequence has no parameters: its final (`7`, `8`, `=`, `>`) ends it, and the
        # next letter is text (`tput sc`, `rc`, `smkx`, `rmkx`; the review of #349)
        "\x1b[?1h\x1b=ask-the-maintainers-before-merging-this",
        "\x1b[?1l\x1b>ask-the-maintainers-before-merging-this",
        "\x1b7xtoken=abcdefghijklmnopqrstuv",
        "\x1b8Ask-the-maintainers-before-merging-this-one",
        "\x1b=xtoken=abcdefghijklmnopqrstuv",
        "\x1b7ask-the-maintainers-before-merging-this",
        # a whole sequence before one letter: it leaves nothing that could be a tail
        "\x1b[1mask-the-maintainers-before-merging-this",
        "\x1b[0mxghp_" + "A" * 36,
        "\x9b31mask-the-maintainers-before-merging-this",
        # after an escape, `-eyJ` is still no JWT (§11.6)
        f"x \x1b7-eyJ{ZK[:9]}.eyJ{ZK[9:20]}.{ZK[20:]}",
    ],
)
def test_a_word_glued_to_a_prefix_stays_glued_after_an_escape(text: str) -> None:
    """Each of these is left as main leaves it: stripped, nothing redacted."""
    assert Scrubber()(text) == strip(text)


#: Whole escape sequences of every shape: 7- and 8-bit, colours and others, with parameters,
#: intermediates or neither, and ESC's two- and three-character forms.
ESCAPES = [
    "\x1b[0m", "\x1b[1;31m", "\x1b[2K", "\x1b[?25l", "\x1b[1 q", "\x1b(B", "\x1b(B\x1b[m",
    "\x1bM", "\x1b7", "\x1b=", "\x9b31m", "\x9b4;219;58m", "\x9b0m", "\x9bK", "\x9b2K",
    "\x9b107;254u", "\x9b?25h", "\x9b>4;1m", "\x9b1;1H", "\x9b6n", "\x9b1 q", "\x9b!p",
]  # fmt: skip
#: Controls a terminal acts on inside an escape sequence without ending it.
CONTROLS = ["\x00", "\x07", "\x0d", "\x7f"]
#: A token holding `48m`, an SGR's tail once a lone U+009B is put before it (finding (b)):
#: 36 characters after `ghp_`, the fewest the pattern takes.
INNER_SGR = f"ghp_{ZK[:6]}48m{ZK[6:]}Q"


def test_an_escape_sequence_before_a_pattern_token() -> None:
    """Any escape sequence right before a pattern-only token, after a word, a blank or
    nothing; controls inside the sequence, invisible characters inside the token."""
    scrub = Scrubber()
    tokens = [*GUARDED, (INNER_SGR, INNER_SGR)]
    for seed in range(2000):
        rng = random.Random(seed)  # noqa: S311 - reproducible cases, not secrets
        token, secret = rng.choice(tokens)
        if token == INNER_SGR and rng.random() < 0.5:
            token = token.replace("48m", "\x9b48m")
        escape = _sprinkle(rng, rng.choice(ESCAPES), CONTROLS, rng.randint(0, 1))
        inside = _sprinkle(rng, token, INVISIBLES, rng.randint(0, 3))
        text = f"{rng.choice(['word', 'x ', ''])}{escape}{inside} tail"
        out = scrub(text)
        assert not _runs(secret, out), (seed, text, out)
        assert scrub.record({"note": text}) == {"note": out}, (seed, text)


@pytest.mark.parametrize(
    ("decoys", "token"),
    [
        # escapes checked in vain (`U+009B ;x` is no tail), then a removal in a word's run
        ("\x9b ;xeyJ " * 5000, f"word\x00ghp_{'A' * 36}"),
        # escapes with a tail but no token after it (codex round 1), then the same
        ("\x9b;xeyJ " * 4096, f"word\x00ghp_{'A' * 36}"),
        ("\x9b;xeyJ " * 5000, f"word\x00ghp_{'A' * 36}"),
        # removals in a word's run with no token after them, then an escape's tail
        ("x\x00eyJ " * 5000, f"x \x9b2Kghp_{'A' * 36}"),
        # places inside tokens found (8 per token), then a removal in a word's run (codex
        # round 3), and then an escape's tail
        (("x\x00sk-" + "AKIA" * 8 + " ") * 512, f"word\x00ghp_{'A' * 36}"),
        (("x\x00sk-" + "AKIA" * 8 + " ") * 1000, f"word\x00ghp_{'A' * 36}"),
        (("x\x00sk-" + "AKIA" * 8 + " ") * 1000, f"x \x9b2Kghp_{'A' * 36}"),
        # the same, then a key whose prefix a tail's match takes in: main finds that key, and
        # so must a place no longer inside the tokens found the way main finds them
        (("x\x00sk-" + "AKIA" * 8 + " ") * 1000, "\x9b2KAKIA" + "Q" * 12 + "\x00ASIA" + "A" * 16),
        # both budgets of run places spent, then `-sk-` after a tail: only a tail is checked,
        # and this one ends one place before the key (the re-review)
        ("x\x00" + ("sk-" + "eyJ" * 2730) * 2 + "eyJ" * 4200, f"\nx \x9b2K-sk-{'A' * 36}"),
    ],
)
def test_escape_tails_have_their_own_budget(decoys: str, token: str) -> None:
    """Neither kind of place uses up the other's budget: thousands of one kind before a token
    of the other kind leave it found. Before the decoys, a token is found anyway."""
    assert "A" * 8 not in Scrubber()(decoys + token)
    assert "A" * 8 not in Scrubber()(f"{token} {decoys}")


@pytest.mark.parametrize(
    "text",
    [
        # a tail before the first key, whose 16 characters take in the second key's prefix
        # (codex round 2); main found only the second
        "\x9b2KAKIA" + "Q" * 12 + "\x00ASIA" + "Z" * 16,
        # the same after a removal in the run, which main missed too
        "x\x00AKIA" + "Q" * 12 + "\x00ASIA" + "Z" * 16,
    ],
)
def test_a_token_starting_inside_one_found_is_checked(text: str) -> None:
    out = Scrubber()(text)
    assert "Q" * 8 not in out and "Z" * 8 not in out, out
    assert out.endswith("[redacted aws access key]"), out


def test_the_known_limits_hold_as_stated() -> None:
    """The threat model and ADMIN.md §2.7 accept these limits (issue #351). If one changes, the
    docs change with it. Registered secrets are found regardless."""
    token = "ghp_" + "A" * 36
    assert "A" * 8 not in Scrubber()("x \x9b" + "1" * 63 + "K" + token)  # a 64-character tail
    assert "A" * 8 in Scrubber()("x \x9b" + "1" * 64 + "K" + token)  # 65: not checked
    for intro, fill, final in (("\x9b", "1", "K"), ("\x1b", "(", "B")):  # before `-sk-` too
        assert ZK[:8] not in Scrubber()(f"x {intro}{fill * 63}{final}-sk-{ZK}"), intro
        assert ZK[:8] in Scrubber()(f"x {intro}{fill * 64}{final}-sk-{ZK}"), intro
    decoys = "\x9b;xeyJ " * 4096  # tails, each checked once
    assert "A" * 8 in Scrubber()(f"{decoys}x \x9b2K{token}")  # past the tail budget
    assert OTHER[:8] not in Scrubber([OTHER])(f"{decoys}x \x9b2K{OTHER}")  # a registered one


def test_a_tail_check_never_rescans_a_long_run() -> None:
    """Each tail check looks at most 64 characters back: 200,000 parameter characters before
    4000 places take a fraction of a second, not the 15 s of an unbounded check."""
    text = "\x9b" + "1" * 200_000 + "eyJ" * 4000 + "\n"
    start = time.process_time()
    Scrubber()(text)
    assert time.process_time() - start < 5


#: The whole input takes 7 s here (#473): pushes to main time it, and a pull request times a
#: quarter of it, since the ratio less the prefix's time doesn't depend on the size (#485).
@pytest.mark.timing
@pytest.mark.parametrize(
    "size",
    [pytest.param(232_500, id="quarter"), pytest.param(930_000, marks=pytest.mark.slow, id="1-MB")],
)
def test_an_introducer_before_every_place_costs_about_twice_main(size: int) -> None:
    """The slowest input measured (ADMIN.md §2.7) is a U+009B before every token-like place,
    once the run budgets are spent. It examines every place, about 1.4 s for 1 MB on a fast
    x86 core, where main takes 0.6 s: an accepted limit (issue #351), which this test keeps
    from growing.

    The bound is on time, since every place is examined already and only the work per place
    can grow. Three texts are timed in turn, three times, and each one's least time counts: a
    prefix that spends the run budgets, then a U+009B before every place; the same with NUL in
    place of each U+009B; the prefix alone. Less the prefix's time, the ratio is the work per
    place's, the same at any size, and its bound is 3. As shipped it reads 2.2 to 2.6, here
    (Zen 5) and on CI's EPYC 7763, 9V45 and 9V74 and Xeon 8370C, 8573C and 6973P-C, under
    Python 3.11 to 3.14. When that work (`_anchored`) doubles it reads 3.3 to 4.1, at a quarter
    and at 1 MB, and fails both measurements in 80 of 80 CI runs and 20 of 20 here; when it
    triples, 4.5 to 5.4 here (#615).

    The prefix spends both budgets of run places as cheaply as a place can (#615): 512 tokens
    found from a place with a removal in its run, 8 places inside each, then 4096 places more
    with a removal in theirs, each match a few characters long. After it, the body makes the
    same searches, tail checks and matches as after the slowest input's own prefix, runs of
    glued `eyJ` whose places each match up to the 8192-character window. That prefix took
    0.18 s here, over half the NUL text's time at a quarter. On CI's EPYC 7763, Python 3.13
    and 3.14 took 1.3 to 1.6 times as long over it as 3.11 and 3.12, and under 3.13 its time
    varied from one runner to the next (0.43 to 0.58 s on two 9V74s). Subtracted from both
    texts, that variation fell on a difference of 0.15 to 0.4 s: under 3.13 the ratio less the
    prefix read 2.0 to 2.9 across CI's runners as shipped, and 3.2 and 3.9 where it failed both
    measurements (7 of 223 CI jobs, issue #615), and as little as 3.0 with the work doubled.
    This prefix takes 0.05 s here, 28 % of the NUL text's time at a quarter and 9 % at 1 MB,
    and its short matches scale with the CPU and the Python as the places after it do.

    CI runs this test alone, in its `timing` job (pyproject.toml), and an xdist worker skips it
    (tests/conftest.py): beside another worker on the core's second thread, the whole ratio
    read 3.1 once, and the one less the prefix 5.9 (#485). The texts are scrubbed once untimed
    first, since a fresh process's first calls ran slow, and a measurement that fails a bound
    is taken again, the test failing only if the second fails too (#508). The whole ratio's
    bound of 3 is implied: with the ratio less the prefix under 3, the U+009B text takes less
    than 3 times the NUL text's time less twice the prefix's."""
    spend = ("x\x00sk-" + "AKIA" * 8 + " ") * 512 + "x\x00eyJ " * 4096
    prefix = "\x1b\x9b" + spend + "\n"
    assert "A" * 8 in Scrubber()(f"{prefix}word\x00ghp_{'A' * 36}")  # the run budget is spent

    def lines(unit: str) -> str:
        s = unit * (size // len(unit))
        return "\n".join(s[i : i + 4000] for i in range(0, len(s), 4000)) + "\n"

    def cpu(text: str) -> float:
        start = time.process_time()
        Scrubber()(text)
        return time.process_time() - start

    def measured() -> tuple[list[list[float]], tuple[bool, bool]]:
        """Three runs of the three texts; the bounds on their least times: their order (else
        the difference measures nothing), the ratio less the prefix's."""
        times = [[cpu(text) for text in texts] for _ in range(3)]
        worst, reference, spent = (min(run[i] for run in times) for i in range(3))
        return times, (worst > reference > spent, worst - spent < 3 * (reference - spent))

    texts = (prefix + lines("\x9beyJ"), prefix + lines("\x00eyJ"), prefix)
    for text in texts:  # a first, untimed pass: a fresh process's first calls ran slow (#508)
        Scrubber()(text)
    times, bounds = measured()
    tries = [times]
    if not all(bounds):  # a slow call can fail one measurement alone (#508)
        times, bounds = measured()
        tries.append(times)
    ordered, per_place = bounds
    assert ordered, tries
    assert per_place, tries


class _CountingSearch:
    """`_GUARDED_START` with its `search` calls counted."""

    def __init__(self, pattern: re.Pattern[str]) -> None:
        self.pattern, self.calls = pattern, 0

    def search(self, text: str, pos: int) -> re.Match[str] | None:
        self.calls += 1
        return self.pattern.search(text, pos)


def test_spent_budgets_skip_places_no_tail_reaches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once only a tail could be checked, places beyond every tail's reach are skipped, up to
    the next introducer, rather than stepped through one by one: without that, the review's
    1 MB inputs took 0.9 s against main's 0.56 s."""
    counting = _CountingSearch(scrub_module._GUARDED_START)
    monkeypatch.setattr(scrub_module, "_GUARDED_START", counting)
    spend = "x\x00" + ("sk-" + "eyJ" * 2730) * 2 + "eyJ" * 4200  # both budgets of run places
    rest = (";xeyJ" * 800 + "\n") * 50  # 40,000 places no tail reaches
    Scrubber()("\x1b\x9b" + spend + "\n" + rest + "\x9b\n")  # an introducer at the very end
    assert counting.calls < 3 * 12_000, counting.calls  # 3 views, about 9700 places each
    # An introducer every 66 characters keeps every place within 64 of one, but its tail has
    # one end: the places after it are skipped too (the re-review: 1.1 s, not 0.7 s, on 1 MB).
    counting.calls = 0
    rest = ("x\x9b" + "eyJ" * 21 + " ") * 3000  # 63,000 places, 3 after each introducer
    Scrubber()("\x1b\x9b" + spend + "\n" + rest + "\n")
    assert counting.calls < 3 * 22_000, counting.calls  # about 9700 + 9000 each


def _literal_starts(items: Any, icase: bool = False) -> tuple[set[str], bool]:
    """The strings a parsed pattern's literal beginning can spell, and whether that's all of
    it. Literals, classes of literals, groups and branches are followed; anything else, such
    as a repeat, ends the walk."""
    starts = {""}
    for op, arg in items:
        if op == sre.LITERAL:
            chars = {chr(arg)}
        elif op == sre.IN and all(o == sre.LITERAL for o, _ in arg):
            chars = {chr(a) for _, a in arg}
        elif op == sre.SUBPATTERN:
            inner, whole = _literal_starts(arg[3], icase or bool(arg[1] & re.IGNORECASE))
            starts = {s + t for s in starts for t in inner}
            if not whole:
                return starts, False
            continue
        elif op == sre.BRANCH:
            inner = set().union(*(_literal_starts(b, icase)[0] for b in arg[1]))
            return {s + t for s in starts for t in inner}, False
        else:
            return starts, False
        if icase:
            chars = {v for c in chars for v in (c.lower(), c.upper())}
        starts = {s + c for s in starts for c in chars}
    return starts, True


def test_every_guarded_pattern_starts_where_anchors_are_tried() -> None:
    """`_GUARDED_START` is kept by hand: a guarded pattern starting with something it doesn't
    list would silently get no boundary recovery (issue #345)."""
    assert len(_RELAXED) >= 6  # the §11.6 JWT, OpenAI, Anthropic, GitHub, AWS and `token`
    for pattern, _ in _RELAXED:
        parsed = sre_parser.parse(pattern.pattern, pattern.flags)
        starts, _ = _literal_starts(parsed, bool(pattern.flags & re.IGNORECASE))
        assert starts and "" not in starts, pattern.pattern
        for start in starts:
            assert _GUARDED_START.match(start), (pattern.pattern, start)
