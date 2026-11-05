"""`workshop/aws/import_key_pair.py` (docs/ADMIN.md §1.6, §5): the server's fleet key as each
workshop's EC2 key pair, tagged at creation, against a fake EC2."""

from __future__ import annotations

import base64

import aws_common as ac
import import_key_pair as ikp
import pytest

_HEAD = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
KEY = "ssh-ed25519 " + base64.b64encode(_HEAD + bytes(range(32))).decode()
OTHER = "ssh-ed25519 " + base64.b64encode(_HEAD + bytes(range(1, 33))).decode()
NAME = "zab-admin-zab-test"
SCOPE = {"workshop": "zab-test", "zab-role": "test"}


class FakeEc2:
    def __init__(self, pairs=()):
        self.pairs = {p["KeyName"]: p for p in pairs}
        self.imported: list[tuple[str, str, dict[str, str]]] = []

    def key_pair(self, name):
        return self.pairs.get(name)

    def import_key_pair(self, name, public_key, tags):
        self.imported.append((name, public_key, dict(tags)))


def run(ec2, text=KEY + " zab-admin@server\n", **kw):
    lines: list[str] = []
    code = ikp.import_key_pair(ec2, NAME, text, SCOPE, log=lines.append, **kw)
    return code, lines


def test_a_missing_key_pair_is_imported_with_the_scope_tags():
    ec2 = FakeEc2()
    code, lines = run(ec2)
    assert code == 0
    assert ec2.imported == [(NAME, KEY, SCOPE)]  # the comment is not sent
    assert "imported" in lines[0]


def test_a_dry_run_imports_nothing():
    ec2 = FakeEc2()
    assert run(ec2, dry_run=True)[0] == 0
    assert ec2.imported == []


def test_the_same_key_with_its_tags_is_left_alone():
    ec2 = FakeEc2([{"KeyName": NAME, "PublicKey": f"{KEY} {NAME}", "Tags": ac.tag_list(SCOPE)}])
    code, lines = run(ec2)
    assert code == 0
    assert ec2.imported == []
    assert "present" in lines[0]


@pytest.mark.parametrize(
    ("pair", "error"),
    [
        ({"PublicKey": OTHER, "Tags": ac.tag_list(SCOPE)}, "another key"),
        (
            {"PublicKey": KEY, "Tags": ac.tag_list({"workshop": "zab-test"})},
            "without workshop=zab-test, zab-role=test",
        ),
    ],
)
def test_a_different_key_pair_is_never_replaced(pair, error):
    ec2 = FakeEc2([{"KeyName": NAME, **pair}])
    code, lines = run(ec2)
    assert code == 1
    assert ec2.imported == []
    assert error in lines[0]


def test_only_an_ed25519_key_is_taken():
    ecdsa = (
        "ecdsa-sha2-nistp256 "
        + base64.b64encode(
            b"\x00\x00\x00\x13ecdsa-sha2-nistp256\x00\x00\x00\x08nistp256\x00\x00\x00\x41\x04"
            + bytes(64)
        ).decode()
    )
    ec2 = FakeEc2()
    code, lines = run(ec2, ecdsa)
    assert code == 1
    assert ec2.imported == []
    assert "ssh-ed25519" in lines[0]


def test_the_client_is_asked_by_filter_and_tags_at_creation():
    calls = []

    class Client:
        def describe_key_pairs(self, **kw):
            calls.append(("describe", kw))
            return {"KeyPairs": []}

        def import_key_pair(self, **kw):
            calls.append(("import", kw))

    ec2 = ac.Ec2.__new__(ac.Ec2)
    ec2._ec2 = Client()
    assert ec2.key_pair(NAME) is None
    ec2.import_key_pair(NAME, KEY, SCOPE)
    tagged = [{"ResourceType": "key-pair", "Tags": ac.tag_list(SCOPE)}]
    assert calls == [
        (
            "describe",
            {"Filters": [{"Name": "key-name", "Values": [NAME]}], "IncludePublicKey": True},
        ),
        (
            "import",
            {"KeyName": NAME, "PublicKeyMaterial": KEY.encode(), "TagSpecifications": tagged},
        ),
    ]


def test_the_command_line_needs_a_scope(tmp_path, capsys):
    key = tmp_path / "id_ed25519.pub"
    key.write_text(KEY + "\n")
    with pytest.raises(SystemExit):
        ikp.main(["--region", "us-east-2", "--key-name", NAME, "--public-key", str(key)])
    assert "--tag workshop=NAME" in capsys.readouterr().err
