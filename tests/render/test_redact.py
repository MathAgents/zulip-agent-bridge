"""One pass of `redact_secrets` leaves nothing that a second pass would redact (SPEC §11.6).

#666: `AKIA<16>ghp_<36>` lost only its first 20 characters on one pass. The GitHub rule had run
already, and it saw a token start before `ghp_` only on a second pass, once the AWS placeholder
stood in front. Every match now takes the rest of its token, and a JWT glued behind it. A longer
match must not hide less: each test here that has a secret in it also checks that what the rules
before #666 hid, run until nothing changed, one pass now hides.
"""

from __future__ import annotations

import itertools
import random
import re
import tracemalloc

import pytest

from zab.render import redact
from zab.render.redact import SECRET_PATTERNS, redact_secrets

#: Every rule `redact_secrets` applies.
RULES = [pattern for pattern, _ in SECRET_PATTERNS] + [redact._ZULIP_KEY_ANY_LINE_END]
_ZULIP = r"\g<prefix>[redacted zulip api key]"
#: The rules as they were before #666 (0.2.18), in their order then.
BEFORE_666 = [
    (
        re.compile(
            r"-----BEGIN (?P<kind>[A-Z ]*)PRIVATE KEY-----"
            r".*?(?:-----END (?P=kind)PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        "[redacted private key]",
    ),
    (
        re.compile(
            r"-----BEGIN PGP PRIVATE KEY BLOCK-----.*?(?:-----END PGP PRIVATE KEY BLOCK-----|\Z)",
            re.DOTALL,
        ),
        "[redacted pgp private key]",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![A-Za-z0-9_-])"
        ),
        "[redacted jwt]",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9_])(?<![A-Za-z0-9_]-)sk-ant-[A-Za-z0-9_-]{20,}"),
        "[redacted anthropic key]",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9_])(?<![A-Za-z0-9_]-)sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
        "[redacted openai key]",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"),
        "[redacted github token]",
    ),
    (re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}"), "[redacted aws access key]"),
    (
        re.compile(
            r"^(?P<prefix>[+\- ]?[ \t]*)key[ \t]*=[ \t]*[A-Za-z0-9]{32}[ \t]*(?=\r?$)",
            re.MULTILINE,
        ),
        _ZULIP,
    ),
    (re.compile(r"(?P<prefix>ZULIP_API_KEY[ \t]*=[ \t]*)[\"']?[A-Za-z0-9]{32}[\"']?"), _ZULIP),
    (re.compile(r"(?P<prefix>(?:-u|--user)[ \t]+[\"']?[^\s\"':]+:)[A-Za-z0-9]{32}"), _ZULIP),
    (redact._ZULIP_KEY_ANY_LINE_END, _ZULIP),  # unchanged
]


def before_666(text: str) -> str:
    """The reference: the rules before #666, applied until nothing changes."""
    for _ in range(100):
        out = text
        for pattern, replacement in BEFORE_666:
            out = pattern.sub(replacement, out)
        if out == text:
            return out
        text = out
    raise AssertionError("no fixed point")


def matching_rules(text: str) -> list[str]:
    return [rule.pattern for rule in RULES if rule.search(text)]


def check(text: str, secrets: list[str]) -> str:
    """One pass of `text`: no rule matches the output, so a second pass changes nothing, and
    each of `secrets` that the reference hides is hidden."""
    out = redact_secrets(text)
    assert not matching_rules(out), (text, out)
    assert redact_secrets(out) == out, (text, out)
    reference = before_666(text)
    shown = [s for s in secrets if s in out and s not in reference]
    assert not shown, (text, out, shown)
    return out


ZKEY = "Q7xK" + "a1B2" * 7
ENV_KEY, CURL_KEY = "R9zM" + "c3D4" * 7, "T5wN" + "e6F8" * 7
JWT_PAYLOAD, JWT_SIGNATURE = "eyJzdWIiOiIxIn0", "c2lnbmF0dXJl"
#: A JWT header of 36 alphanumerics: a Zulip key's 32 and more.
HEADER = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
#: A signature that starts with `eyJ`, as one in 262,144 does (#821).
EYJ_SIGNATURE = "eyJzaWduYXR1cmU"
#: A whole match of each rule, the Zulip key's in each of its three forms and as a JWT, and its
#: secrets.
TOKENS = {
    "jwt": (f"eyJhbGciOiJIUzI1NiJ9.{JWT_PAYLOAD}.{JWT_SIGNATURE}", [JWT_PAYLOAD, JWT_SIGNATURE]),
    "anthropic": ("sk-ant-api03-" + "x" * 24, ["x" * 24]),
    "openai": ("sk-" + "a" * 24, ["a" * 24]),
    "openai-project": ("sk-proj-" + "b" * 24, ["b" * 24]),
    "github": ("ghp_" + "Z9y8X7w6" * 4 + "V5u4", ["Z9y8X7w6" * 4 + "V5u4"]),
    "github-pat": ("github_pat_" + "a_" * 30, ["a_" * 30]),
    "aws": ("AKIA" + "Q" * 16, ["Q" * 16]),
    "aws-temporary": ("ASIA" + "7" * 16, ["7" * 16]),
    "zuliprc": ("key=" + ZKEY, [ZKEY]),
    "zulip-env": ("ZULIP_API_KEY=" + ENV_KEY, [ENV_KEY]),
    "zulip-curl": ("-u bot@x.y:" + CURL_KEY, [CURL_KEY]),
    "zulip-jwt": (
        f"-u bot@x.y:{HEADER}.{JWT_PAYLOAD}.{EYJ_SIGNATURE}",
        [JWT_PAYLOAD, EYJ_SIGNATURE],
    ),
    "private-key": ("-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----", ["MIIE"]),
    "pgp": (
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOY\n-----END PGP PRIVATE KEY BLOCK-----",
        ["lQOY"],
    ),
}
LINE_ENDS = ["\n", "\r\n", "\r", "\u2028"]
#: None; the characters a rule's guard looks at; others; line ends.
SEPARATORS = ["", "-", "_", "x", "Q", "7", ".", " ", "=", ":", '"', *LINE_ENDS]


@pytest.mark.parametrize("name", list(TOKENS))
def test_each_token_is_redacted_whole(name: str) -> None:
    token, secrets = TOKENS[name]
    assert matching_rules(token)
    out = check(token, secrets)
    assert "[redacted" in out and not any(s in out for s in secrets)


@pytest.mark.parametrize(("first", "second"), list(itertools.product(TOKENS, repeat=2)))
def test_one_pass_after_two_tokens(first: str, second: str) -> None:
    """Every pair of tokens, with each separator between them: no rule matches the output, so a
    second pass changes nothing, and no secret shows that the rules before #666 hid. On their
    own lines, neither token is left whole. The key of `ZULIP_API_KEY=` or `curl -u` never
    shows: those rules have no token guard (review of #801: the rest of an AWS or GitHub token
    took their context)."""
    (a, a_secrets), (b, b_secrets) = TOKENS[first], TOKENS[second]
    for sep in SEPARATORS:
        out = check(a + sep + b, a_secrets + b_secrets)
        if sep in LINE_ENDS:
            assert a not in out and b not in out, (sep, out)
        if second in ("zulip-env", "zulip-curl"):
            assert b_secrets[0] not in out, (sep, out)


GHP, AKIA, SK = TOKENS["github"][0], TOKENS["aws"][0], TOKENS["openai"][0]
JWT = TOKENS["jwt"][0]
LONG_JWT = f"{HEADER}.{JWT_PAYLOAD}.{JWT_SIGNATURE}"
Z = "[redacted zulip api key]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # what one pass left before #666: a rule's token after another's placeholder
        (AKIA + GHP, "[redacted aws access key]"),
        (AKIA + AKIA + AKIA, "[redacted aws access key]"),
        (AKIA + "-" + SK, "[redacted aws access key]"),
        (GHP + "-" + SK, "[redacted github token]"),
        (TOKENS["github-pat"][0] + "-" + SK, "[redacted github token]"),
        ("ZULIP_API_KEY=" + ZKEY + AKIA, f"ZULIP_API_KEY={Z}"),
        ("ZULIP_API_KEY='" + ZKEY + "'" + AKIA, f"ZULIP_API_KEY={Z}[redacted aws access key]"),
        ("-u bot@x.y:" + ZKEY + GHP + " x", f"-u bot@x.y:{Z} x"),
        # the rest of a token goes with it; a character outside one ends it
        (AKIA + "abc", "[redacted aws access key]"),
        (AKIA + ".txt", "[redacted aws access key].txt"),
        (GHP + "_x-y", "[redacted github token]"),
        ("key=" + AKIA + "Q" * 12, "key=[redacted aws access key]"),
        # but not a Zulip key's context, glued behind a token (review of #801)
        (AKIA + "ZULIP_API_KEY=" + ZKEY, f"[redacted aws access key]ZULIP_API_KEY={Z}"),
        (AKIA + "_ZULIP_API_KEY=" + ZKEY, f"[redacted aws access key]ZULIP_API_KEY={Z}"),
        (GHP + "-u bot@x.y:" + ZKEY, f"[redacted github token]-u bot@x.y:{Z}"),
        (GHP + "--user bot@x.y:" + ZKEY, f"[redacted github token]--user bot@x.y:{Z}"),
        ("ZULIP_API_KEY=" + ZKEY + "-u b:" + ENV_KEY, f"ZULIP_API_KEY={Z}-u b:{Z}"),
        ("-u a@b:" + ZKEY + "--user c@d:" + ENV_KEY, f"-u a@b:{Z}--user c@d:{Z}"),
        (
            "ZULIP_API_KEY=" + ZKEY + "_ZULIP_API_KEY=" + ENV_KEY,
            f"ZULIP_API_KEY={Z}ZULIP_API_KEY={Z}",  # the `_` is the first one's rest
        ),
        ("-u " + GHP + ":" + ZKEY, f"-u [redacted github token]:{Z}"),  # a token as the user
        (JWT + "-u bot@x.y:" + ZKEY, f"[redacted jwt] bot@x.y:{Z}"),
        # a JWT glued behind a token goes with it, and each segment dot-joined after it
        (AKIA + JWT, "[redacted aws access key]"),
        (GHP + JWT, "[redacted github token]"),
        (SK + JWT, "[redacted openai key]"),
        ("ZULIP_API_KEY=" + ZKEY + JWT, f"ZULIP_API_KEY={Z}"),
        # §11.6's limit, as before: a JWT's last segment takes the header of one glued behind
        (JWT + JWT, f"[redacted jwt].{JWT_PAYLOAD}.{JWT_SIGNATURE}"),
        (AKIA + "." + JWT, "[redacted aws access key].[redacted jwt]"),
        (JWT + "." + JWT, "[redacted jwt].[redacted jwt]"),
        ("ZULIP_API_KEY=" + ZKEY + "." + JWT, f"ZULIP_API_KEY={Z}"),
        ("ZULIP_API_KEY=" + LONG_JWT, f"ZULIP_API_KEY={Z}"),
        ("ZULIP_API_KEY=" + LONG_JWT + "." + LONG_JWT, f"ZULIP_API_KEY={Z}"),
        (AKIA + ".eyJa.b", "[redacted aws access key]"),  # its shape, without the header
        (AKIA + ".eyJa.b.c.d", "[redacted aws access key]"),
        (AKIA + ".txt", "[redacted aws access key].txt"),  # no `eyJ`: no JWT
        # #821: a third segment that is a context's word; a signature that starts with `eyJ`
        (f"ZULIP_API_KEY={HEADER}.{JWT_PAYLOAD}.-u x:abc", f"ZULIP_API_KEY={Z}-u x:abc"),
        (
            f"ZULIP_API_KEY={HEADER}.{JWT_PAYLOAD}.ZULIP_API_KEY=",
            f"ZULIP_API_KEY={Z}ZULIP_API_KEY=",
        ),
        (f"{AKIA}{HEADER}.{JWT_PAYLOAD}.--user a", "[redacted aws access key]--user a"),
        (TOKENS["zulip-jwt"][0] + "." + LONG_JWT, f"-u bot@x.y:{Z}"),
        # one in a word is still no token, as before
        ("x" + AKIA + GHP, "x" + AKIA + GHP),
        ("key=" + ZKEY + "_" + GHP, "key=" + ZKEY + "_" + GHP),
        ("x" + JWT, "x" + JWT),
    ],
)
def test_a_match_takes_the_rest_of_its_token(text: str, expected: str) -> None:
    assert redact_secrets(text) == expected
    assert not matching_rules(expected)


#: The fuzz's pieces: whole tokens, a rule's prefix or part, runs of token characters, and
#: separators, the line ends among them.
PREFIXES = [
    "sk-", "sk-ant-", "ghp_", "github_pat_", "AKIA", "ASIA", "eyJ", "eyJa.", ".eyJ", "key=",
    "key = ", "+key=", "- key=", "ZULIP_API_KEY=", "ZULIP_API_KEY = '", "-u x@y:",
    "--user 'a@b:", "ZULIP_API_KEY", "-u ", "--user ", "-----BEGIN PRIVATE KEY-----",
    "-----END PRIVATE KEY-----",
    "-----BEGIN PGP PRIVATE KEY BLOCK-----", "-----END PGP PRIVATE KEY BLOCK-----",
]  # fmt: skip
RUNS = ["ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "abcdefghijklmnopqrstuvwxyz", "aZ09", "a_b-c"]
PIECE_SEPARATORS = [*SEPARATORS, "\t", "'", "+", "[", "]", "\x85", "\u3000"]


def _piece(rng: random.Random, secrets: list[str]) -> str:
    r = rng.random()
    if r < 0.4:
        token, token_secrets = rng.choice(list(TOKENS.values()))
        secrets += token_secrets
        return token
    if r < 0.55:
        return rng.choice(PREFIXES)
    if r < 0.75:
        n = rng.choice([1, 4, 12, 16, 20, 24, 31, 32, 33, 36, 40])
        return "".join(rng.choices(rng.choice(RUNS), k=n))
    return rng.choice(PIECE_SEPARATORS)


def _texts(seed: int, n: int) -> list[tuple[str, list[str]]]:
    rng = random.Random(seed)  # noqa: S311 - reproducible cases, not secrets
    texts = []
    for _ in range(n):
        secrets: list[str] = []
        texts.append(("".join(_piece(rng, secrets) for _ in range(rng.randint(1, 8))), secrets))
    return texts


def test_redaction_is_idempotent_on_seeded_fuzz() -> None:
    """#666's acceptance: on 5,000 seeded texts of 1 to 8 pieces, one pass leaves no rule
    matching, so `redact_secrets` is idempotent, and it shows no secret that the rules before
    #666 hid. Before #666, one pass failed on 371 of them, and on 50 of the 2,535 texts of the
    test of pairs above."""
    for text, secrets in _texts(666, 5000):
        check(text, secrets)


@pytest.mark.parametrize(
    ("text", "needed"),
    [
        ("a\nb\n", False),
        ("a\r\nb\r\n", False),  # CRLF: the first rule's `\r?$` takes it
        ("a\rb", True),
        ("a\r\rb\r\n", True),  # a `\r` before another `\r`, not before `\n`
        ("a\r\nb\r", True),
        ("a\r\nb\u2028", True),
        *[(f"a{end}b", True) for end in redact._OTHER_LINE_ENDS],
    ],
)
def test_crlf_text_skips_the_second_pass(text: str, needed: bool, monkeypatch) -> None:
    """#666: text whose every `\\r` is in a CRLF pair paid for the second zuliprc pass (about
    30% more time) although the first rule already takes its lines. `redact_secrets` runs it
    only where it is needed (review of #801: the test called the condition alone)."""
    real, runs = redact._ZULIP_KEY_ANY_LINE_END, []

    class Spy:
        def sub(self, replacement: str, text: str) -> str:
            runs.append(text)
            return real.sub(replacement, text)

    monkeypatch.setattr(redact, "_ZULIP_KEY_ANY_LINE_END", Spy())
    assert redact._other_line_end(text) is needed
    redact_secrets(text)
    assert len(runs) == needed


def test_the_second_pass_finds_nothing_in_crlf_text() -> None:
    """What the skip relies on: in text with `\\n` and CRLF line ends only, the second pass
    would change nothing after the first rules."""
    rng = random.Random(6660)  # noqa: S311 - reproducible cases, not secrets
    for text, _ in _texts(6660, 2000):
        for end in ["\r\n", "\r", *redact._OTHER_LINE_ENDS_BUT_CR]:
            text = text.replace(end, "\n")
        text = text.replace("\n", rng.choice(["\n", "\r\n"]))
        out = redact_secrets(text)
        assert not redact._other_line_end(out)
        second = redact._ZULIP_KEY_ANY_LINE_END.sub(_ZULIP, out)
        assert second == out, (text, out)


# ------------------------------------------------------------------------- memory (review of #828)

MB = 1 << 20
#: 1 MB inputs that run each rule as far as it goes: a tail, a dot run after a glued `.eyJ`, a
#: long segment, a lazy body, a run of blanks. `re` keeps a frame for each pass of a repeated
#: group, so a greedy `_CHAR*` held about 65 bytes per character, and the dot runs of #821
#: about 210: 94 MB and 260 MB here, before the tails were possessive.
LONG_INPUTS = {
    "private key, no END": "-----BEGIN PRIVATE KEY-----\n" + "ab\n" * (MB // 3),
    "private key, long kind": "-----BEGIN " + "A" * MB,
    "pgp, no END": "-----BEGIN PGP PRIVATE KEY BLOCK-----\n" + "ab\n" * (MB // 3),
    "env, tail": "ZULIP_API_KEY=" + ZKEY + "a" * MB,
    "env, dot run": "ZULIP_API_KEY=" + ZKEY + ".eyJa" + "." * MB,
    "env, blanks": "ZULIP_API_KEY" + " " * MB + "=",
    "curl, tail": "-u x:" + ZKEY + "a" * MB,
    "curl, user": "-u " + "a" * MB,
    "curl, dot segments": "-u x:" + ZKEY + ".eyJa" + ".a" * (MB // 2),
    "jwt, segments": "eyJ" + "a" * MB + ".eyJ" + "b" * MB + "." + "c" * MB,
    "anthropic, tail": "sk-ant-" + "a" * MB,
    "openai, dot run": "sk-" + "a" * MB + ".eyJa" + "." * MB,
    "github, tail": "ghp_" + "a" * MB,
    "github pat, tail": "github_pat_" + "a_" * (MB // 2),
    "aws, tail": AKIA + "a" * MB,
    "aws, dot run": AKIA + ".eyJa" + "." * MB,
    "aws, contexts": AKIA + "-u" * (MB // 2),
    "zuliprc, blanks": "key=" + ZKEY + " " * MB + "x",
    "zuliprc after \\r, blanks": "\rkey=" + ZKEY + " " * MB + "x",
}


@pytest.mark.parametrize("text", list(LONG_INPUTS.values()), ids=list(LONG_INPUTS))
def test_a_long_input_costs_bounded_memory(text: str) -> None:
    """Review of #828: one call on 1 MB allocates at most a few MB more than its output,
    whatever the input; `tracemalloc` sees `re`'s backtracking stack. A diff is redacted
    whole before its size is checked, and snapshots take files of 20 MB."""
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        redact_secrets(text)
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    assert peak < 16 * MB, f"{peak / MB:.0f} MB"


# The rules' structure: tests/test_regex_memory.py, with every other pattern of `zab`.
