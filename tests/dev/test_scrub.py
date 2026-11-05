"""`zab dev scrub` (SPEC §15.2): secrets, paths and machine details never reach a fixture."""

import json

import pytest

from zab.dev import scrub
from zab.dev.scrub import Scrubber, default_rules


@pytest.fixture
def scrubber():
    return Scrubber(
        default_rules(
            home="/home/claude",
            cwd="/var/scratch/zab-record-abc",
            config_dir="/home/claude/.claude2",
            hostname="devbox",
        )
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123", "key [redacted anthropic key]"),
        ("sk-proj-abcdefghijklmnopqrstuvwxyz", "[redacted openai key]"),
        ("ghp_" + "a" * 36, "[redacted github token]"),
        ("github_pat_" + "b" * 60, "[redacted github token]"),
        ("AKIAABCDEFGHIJKLMNOP", "[redacted aws access key]"),
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
            "[redacted private key]",
        ),
        (
            "email=x@y.zz\nkey=" + "k" * 32 + "\n",
            "email=user@example.com\nkey=[redacted zulip api key]\n",
        ),
        ("/home/claude/.claude2/projects/x", "/home/user/.claude/projects/x"),
        ("cwd /var/scratch/zab-record-abc/hello.txt", "cwd /work/scratch/hello.txt"),
        ("/home/claude/other", "/home/user/other"),
        ("mail someone@example.org now", "mail user@example.com now"),
        ("on devbox today", "on host today"),
        ("key=" + "k" * 32 + "\r\n", "key=[redacted zulip api key]\r\n"),  # CRLF line
        ("  key=" + "k" * 32 + "\r", "  key=[redacted zulip api key]\r"),
        ("nothing to see", "nothing to see"),
        ("sk-short", "sk-short"),  # too short to be a key
    ],
)
def test_text_rules(scrubber, text, expected):
    assert scrubber.text(text) == expected


def test_json_blanks_machine_keys_and_scrubs_nested_strings(scrubber):
    obj = {
        "type": "system",
        "cwd": "/var/scratch/zab-record-abc",
        "tools": ["Bash", "Read"],
        "slash_commands": ["lean4", "watch-codex"],
        "agents": ["claude"],
        "mcp_servers": [{"name": "claude.ai lmfdb"}],
        "message": {"content": [{"type": "thinking", "signature": "ErQDCrIB…", "thinking": ""}]},
        "n": 3,
    }
    out = scrubber.json(obj)
    assert out["cwd"] == "/work/scratch"
    assert out["tools"] == ["Bash", "Read"]
    nested = scrubber.json(
        {
            "tools": [
                "mcp__x__y",
                {"description": "sk-ant-" + "x" * 24, "path": "/home/claude/private"},
                ["mcp__a__b", "/home/claude/z"],
            ]
        }
    )
    assert nested["tools"] == [
        {"description": "[redacted anthropic key]", "path": "/home/user/private"},
        ["mcp__a__b", "/home/user/z"],  # only string entries of `tools` itself are dropped
    ]
    assert out["slash_commands"] == [] and out["agents"] == [] and out["mcp_servers"] == []
    assert out["message"]["content"][0]["signature"] == ""
    assert out["n"] == 3


def test_identity_keys_tools_and_encoded_paths(scrubber):
    obj = {
        "email": "someone@example.org",
        "orgId": "12345678-1234-4123-8123-123456789abc",
        "orgName": "Real Org",
        "skills": ["x"],
        "memory_paths": ["/home/claude/.claude2/memory"],
        "messaging_socket_path": "/run/user/1000/sock",
        "plugins": ["p"],
        "tools": ["Bash", "mcp__claude_ai_LMFDB__run_sql", "Read"],
        "cwd_key": "-var-scratch-zab-record-abc",
        "home_key": "-home-claude-.claude2",
        "n": 1,
    }
    out = scrubber.json(obj)
    assert out["email"] == "user@example.com" and out["orgId"] == scrub.ZERO_UUID
    assert out["orgName"] == "" and out["skills"] == [] and out["memory_paths"] == []
    assert out["messaging_socket_path"] == "" and out["plugins"] == []
    assert out["tools"] == ["Bash", "Read"]
    nested = scrubber.json(
        {
            "tools": [
                "mcp__x__y",
                {"description": "sk-ant-" + "x" * 24, "path": "/home/claude/private"},
                ["mcp__a__b", "/home/claude/z"],
            ]
        }
    )
    assert nested["tools"] == [
        {"description": "[redacted anthropic key]", "path": "/home/user/private"},
        ["mcp__a__b", "/home/user/z"],  # only string entries of `tools` itself are dropped
    ]
    assert out["cwd_key"] == "-work-scratch"
    assert out["home_key"].startswith("-home-user")
    assert out["n"] == 1


def test_committed_fixtures_are_scrubbed():
    """CI guard: `zab dev scrub --check` on every committed fixture (SPEC §15.2)."""
    import os
    import pathlib
    import socket

    fixtures = sorted(
        str(p)
        for p in (pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "claude").rglob(
            "*.jsonl"
        )
        if "raw" not in p.parts
    )
    assert fixtures
    rc = scrub.main(
        [
            "--check",
            "--home",
            os.path.expanduser("~"),
            "--hostname",
            socket.gethostname(),
            *fixtures,
        ]
    )
    assert rc == 0


def test_line_keeps_json_and_passes_text_through(scrubber):
    line = json.dumps({"kind": "stderr", "line": "/home/claude/x"})
    assert json.loads(scrubber.line(line)) == {"kind": "stderr", "line": "/home/user/x"}
    assert scrubber.line("not json /home/claude") == "not json /home/user"
    assert scrubber.line("") == ""


def test_private_key_split_across_records(scrubber):
    """The recorder stores each stderr line as its own record (codex round 4 on PR #15)."""
    records = [
        json.dumps({"kind": "stderr", "line": "warning: -----BEGIN OPENSSH PRIVATE KEY-----"}),
        json.dumps(
            {"kind": "stderr", "line": "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB"}
        ),
        json.dumps({"kind": "stderr", "line": "AAAAMwAAAAtzc2gtZWQyNTUxOQAAACA="}),
        json.dumps({"kind": "stderr", "line": "-----END OPENSSH PRIVATE KEY----- done"}),
        json.dumps({"kind": "stderr", "line": "after the key"}),
    ]
    scrubbed = [json.loads(line) for line in scrub.scrub_lines(records, scrubber)]
    assert [r["kind"] for r in scrubbed] == ["stderr"] * 5  # structure survives the block
    out = [r["line"] for r in scrubbed]
    assert out == [
        "warning: [redacted private key]",
        "[redacted private key]",
        "[redacted private key]",
        "[redacted private key] done",
        "after the key",
    ]
    # a complete block in one string, and a block that starts after a complete one
    one = Scrubber(scrubber.rules)
    assert one.text("x -----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY----- y") == (
        "x [redacted private key] y"
    )
    two = (
        "-----BEGIN A PRIVATE KEY-----\nq\n-----END A PRIVATE KEY-----\n"
        "-----BEGIN B PRIVATE KEY-----"
    )
    assert one.text(two) == "[redacted private key]\n[redacted private key]"
    assert one.text("tail -----END B PRIVATE KEY----- rest") == "[redacted private key] rest"
    assert one.text("plain") == "plain"


def test_split_key_inside_stdout_records_keeps_structure(scrubber):
    """A key that continues across stdout objects redacts only the text fields."""
    records = [
        json.dumps(
            {
                "kind": "stdout",
                "t": 1,
                "data": {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "key: -----BEGIN EC PRIVATE KEY-----"}]
                    },
                },
            }
        ),
        json.dumps(
            {
                "kind": "stdout",
                "t": 2,
                "data": {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "MHQCAQEEIBody"}]},
                },
            }
        ),
        json.dumps(
            {
                "kind": "stdout",
                "t": 3,
                "data": {
                    "type": "result",
                    "subtype": "success",
                    "result": "-----END EC PRIVATE KEY----- ok",
                },
            }
        ),
        json.dumps({"kind": "exit", "t": 4, "code": 0}),
    ]
    scrubbed = [json.loads(line) for line in scrub.scrub_lines(records, scrubber)]
    assert [r["kind"] for r in scrubbed] == ["stdout", "stdout", "stdout", "exit"]
    assert scrubbed[0]["data"]["type"] == "assistant"
    assert scrubbed[0]["data"]["message"]["content"][0]["type"] == "text"
    assert scrubbed[0]["data"]["message"]["content"][0]["text"] == "key: [redacted private key]"
    assert scrubbed[1]["data"]["message"]["content"][0]["text"] == "[redacted private key]"
    assert scrubbed[2]["data"]["subtype"] == "success"
    assert scrubbed[2]["data"]["result"] == "[redacted private key] ok"
    assert scrubbed[3] == {"kind": "exit", "t": 4, "code": 0}


def test_check_flags_split_key(tmp_path):
    f = tmp_path / "fx.jsonl"
    f.write_text(
        json.dumps({"kind": "stderr", "line": "-----BEGIN EC PRIVATE KEY-----"})
        + "\n"
        + json.dumps({"kind": "stderr", "line": "MHQCAQEEIBody"})
        + "\n"
        + json.dumps({"kind": "stderr", "line": "-----END EC PRIVATE KEY-----"})
        + "\n"
    )
    assert scrub.main(["--home", "/home/claude", "--hostname", "nohost", "--check", str(f)]) == 1
    assert scrub.main(["--home", "/home/claude", "--hostname", "nohost", str(f)]) == 0
    assert "MHQCAQEEIBody" not in f.read_text()
    assert scrub.main(["--home", "/home/claude", "--hostname", "nohost", "--check", str(f)]) == 0


def test_pem_state_does_not_leak_between_files(tmp_path):
    """codex round 7 on PR #15: an unterminated block in one recording must not redact the
    next fixture scrubbed in the same invocation."""
    truncated = tmp_path / "truncated.jsonl"
    truncated.write_text(
        json.dumps({"kind": "stderr", "line": "-----BEGIN EC PRIVATE KEY-----"}) + "\n"
    )
    clean = tmp_path / "clean.jsonl"
    clean_text = (  # compact form, as the scrubber writes it
        json.dumps(
            {"kind": "stdout", "t": 1, "data": {"type": "result", "result": "pong"}},
            separators=(",", ":"),
        )
        + "\n"
    )
    clean.write_text(clean_text)
    args = ["--home", "/home/claude", "--hostname", "nohost"]
    assert scrub.main([*args, "--check", str(truncated), str(clean)]) == 1  # only the truncated one
    assert scrub.main([*args, str(truncated), str(clean)]) == 0
    assert clean.read_text() == clean_text
    assert "[redacted private key]" in truncated.read_text()
    assert scrub.main([*args, "--check", str(clean)]) == 0


def test_cli_in_place_and_check(tmp_path, capsys):
    f = tmp_path / "fx.jsonl"
    f.write_text(json.dumps({"a": "/home/claude/z", "signature": "s"}) + "\n")
    assert scrub.main(["--home", "/home/claude", "--hostname", "nohost", "--check", str(f)]) == 1
    assert scrub.main(["--home", "/home/claude", "--hostname", "nohost", str(f)]) == 0
    assert json.loads(f.read_text()) == {"a": "/home/user/z", "signature": ""}
    assert scrub.main(["--home", "/home/claude", "--hostname", "nohost", "--check", str(f)]) == 0


def test_cli_extra_replacement_and_output_file(tmp_path):
    src = tmp_path / "in.jsonl"
    src.write_text('{"x": "SECRETWORD in /home/claude"}\n')
    dst = tmp_path / "out.jsonl"
    rc = scrub.main(
        [
            "--home",
            "/home/claude",
            "--hostname",
            "nohost",
            "--replace",
            "SECRETWORD=word",
            "-o",
            str(dst),
            str(src),
        ]
    )
    assert rc == 0
    assert json.loads(dst.read_text()) == {"x": "word in /home/user"}
    assert "SECRETWORD" in src.read_text()  # the input is untouched with -o
