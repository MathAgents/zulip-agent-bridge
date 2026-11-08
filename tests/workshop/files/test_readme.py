"""The files host's runbook (workshop/files/README.md; issue #442) against what it runs: its shell
blocks parse, the staged unit is the shipped one with the three changes the runbook shows, the
import check passes on this tree, and the selftest takes the options the runbook gives it."""

from __future__ import annotations

import importlib.util
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DIRECTORY = ROOT / "workshop" / "files"
TEXT = (DIRECTORY / "README.md").read_text()
BLOCKS = re.findall(r"^```bash\n(.*?)^```$", TEXT, re.S | re.M)
UPDATE = TEXT[TEXT.index("## Updating a running server") :]


def block(marker: str) -> str:
    """The one block of the update runbook that contains `marker`."""
    found = [b for b in re.findall(r"^```bash\n(.*?)^```$", UPDATE, re.S | re.M) if marker in b]
    assert len(found) == 1, marker
    return found[0]


@pytest.mark.parametrize("number", range(len(BLOCKS)))
def test_every_shell_block_parses(number):
    # a placeholder left in is a syntax error by design (BOOTSTRAP's convention): one word here
    text = re.sub(r"<[^<>\n]*>", "X", BLOCKS[number])
    result = subprocess.run(
        ["/bin/bash", "-n"], input=text, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


#: A shell assignment that starts a command, `NAME=value`, its value a `<placeholder>` (quoted
#: or not, its quotes dropped) or one word; not one quoted in a message, nor a command's
#: `env NAME=value`.
ASSIGNED = re.compile(r"(?:^\s*|[;&|(]\s*)([A-Z][A-Z0-9_]*)=(?:(['\"]?)(<[^<>\n]*>)\2|([^\s;]+))")


def test_a_variable_the_deployer_sets_is_never_assigned_anything_else():
    """#768: the share probe's temporary repository took `P`, the AWS profile that the update's
    step 8 still needed in the same terminal to close port 22. A variable the deployer sets,
    `NAME=<placeholder>`, always takes the same placeholder, and no block assigns it anything
    else."""
    values: dict[str, set[str]] = {}
    for body in BLOCKS:
        for line in body.splitlines():
            if not line.lstrip().startswith("#"):  # an expected output, not a command
                for name, _, placeholder, word in ASSIGNED.findall(line.split(" #", 1)[0]):
                    values.setdefault(name, set()).add(placeholder or word)
    given = {name: found for name, found in values.items() if any(v[0] == "<" for v in found)}
    assert {"P", "FR", "FILES_HOST", "FILES_ORG", "SCRATCH", "URL"} <= set(given)
    assert {name: found for name, found in given.items() if len(found) > 1} == {}
    assert values["PD"] == {"$(mktemp"}  # the probe's repository has a name of its own


def test_the_staged_unit_is_the_shipped_one_with_three_changes(tmp_path):
    text = block("sudo tee /run/systemd/system/zab-files-staged.service")
    command = text[text.index("sed ") : text.index(" |\n")]
    # the runbook's `sed`, as written, on the shipped unit (only its path changes)
    argv = shlex.split(command.replace("\\\n", " "))
    unit = DIRECTORY / "zab-files.service"
    argv[-1] = str(unit)
    staged = tmp_path / "zab-files-staged.service"
    staged.write_bytes(subprocess.check_output(argv))  # noqa: S603
    shown = re.search(r"^diff [^\n]*\n((?:# [^\n]*\n)+)", text, re.M)
    expected = "".join(line[2:] + "\n" for line in shown.group(1).splitlines())
    diff = subprocess.run(  # noqa: S603
        ["/usr/bin/diff", str(unit), str(staged)], capture_output=True, text=True, check=False
    )
    assert diff.stdout == expected


def test_the_import_check_passes_on_this_tree(tmp_path):
    text = block("<<'CHECK'")
    script = re.search(r"<<'CHECK'\n(.*?)\nCHECK\n", text, re.S).group(1)
    script = script.replace("/opt/zab-files.new/workshop/files/", f"{DIRECTORY}/")
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(" rendering on"), result.stdout + result.stderr


def test_selftest_runs_take_its_options():
    source = (DIRECTORY / "selftest.py").read_text()
    options = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', source))
    runs = [line for b in BLOCKS for line in b.splitlines() if "selftest.sh" in line]
    assert len(runs) >= 4
    for line in runs:
        used = set(re.findall(r"(?<=\s)--[a-z-]+", line.split("selftest.sh", 1)[1]))
        assert used <= options, line
    assert any("--url http://127.0.0.1:8081" in line for line in runs)


def test_the_update_pushes_its_selftests_over_ssh():
    """#470: over HTTPS, the push used whatever credentials git had stored, and GitHub answered
    `Repository not found`; steps 5 and 7 push over SSH, to the one repository they name."""
    blocks = re.findall(r"^```bash\n(.*?)^```$", UPDATE, re.S | re.M)
    runs = [line for b in blocks for line in b.splitlines() if "selftest.sh" in line]
    assert len(runs) == 2
    for line in runs:
        assert line.split("selftest.sh", 1)[1].startswith(' "$SCRATCH" --org "$FILES_ORG" '), line
        assert line.endswith(' --push-url "git@github.com:$FILES_ORG/$SCRATCH.git"'), line


def server_module():
    name = "files_server_readme"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, DIRECTORY / "server.py")
        sys.modules[name] = module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # registered first: its dataclasses look it up
    return sys.modules[name]


def test_the_organization_is_where_the_unit_reads_it_and_the_runbook_writes_it():
    """#693: the unit's `EnvironmentFile` is the file the server's error names, §3 and the
    update's step 3 write the variable the server reads into it, the steps that start the
    service look for its `serving` line, and no selftest run goes without `--org`."""
    server = server_module()
    unit = (DIRECTORY / "zab-files.service").read_text()
    assert f"\nEnvironmentFile=-{server.ORG_FILE}\n" in unit
    # no organization: no restart loop, on the server's own status alone (review of #770, N4)
    assert f"\nRestartPreventExitStatus={server.EX_CONFIG}\n" in unit
    assert "MathAgents" not in unit
    writes = [b for b in BLOCKS if f"| sudo tee {server.ORG_FILE}" in b]
    assert len(writes) == 2  # §3, and the update's step 3
    for body in writes:
        # root's, 0644, before its one line: the owner and mode the update's note names
        made = f"sudo install -m 0644 -o root -g root /dev/null {server.ORG_FILE} &&"
        assert made in body
        assert f"printf '{server.ORG_ENV}=%s\\n' \"$FILES_ORG\" | sudo tee " in body
        # written only in the form the server takes (review of #770, N3)
        bash_pattern = server.ORG_NAME.pattern.replace("(?:", "(")  # POSIX ERE has no (?:
        assert f"[[ $FILES_ORG =~ ^{bash_pattern}$ ]]" in body
    assert "sudo test -f /etc/zab-files/zab-files.env ||" in writes[1]  # never overwritten
    serving = [b for b in BLOCKS if "grep -e serving -e organization" in b]
    assert len(serving) == 3  # §3, the staged server (step 5), the switch's check (step 7)
    runs = [line for b in BLOCKS for line in b.splitlines() if "selftest.sh" in line]
    assert all(" --org " in line for line in runs)
    assert "MathAgents" not in "".join(BLOCKS)  # every command takes the deployment's own


def test_the_server_logs_the_line_the_runbook_looks_for(tmp_path, monkeypatch, caplog):
    from unittest.mock import MagicMock, Mock

    server = server_module()
    monkeypatch.setattr(server, "Server", MagicMock())
    monkeypatch.setattr(server.Checker, "start", Mock())
    monkeypatch.setenv(server.ORG_ENV, "Example-Org")
    assert server.main(["--cache", str(tmp_path / "c"), "--shares", str(tmp_path / "s")]) == 0
    expected = re.search(r"# (serving the repositories of )<org>\n", TEXT).group(1)
    assert f"{expected}Example-Org" in caplog.text


#: `sudo` as a shell function on one file, `$ENVFILE`, standing in for `zab-files.env`
#: (`$ORG_FILE`): each command the runbook's blocks run on it, word for word; `stat` reads the
#: stand-in's mode, and names root, which owns no file of the test's.
ENV_STUBS = """
sudo() {
  case "$*" in
    "test -f $ORG_FILE") [ -f "$ENVFILE" ] ;;
    "install -m 0644 -o root -g root /dev/null $ORG_FILE")
      : > "$ENVFILE" && chmod 0644 "$ENVFILE" ;;
    "tee $ORG_FILE") cat > "$ENVFILE"; cat "$ENVFILE" ;;
    "stat -c %a %U:%G $ORG_FILE") echo "$(stat -c %a "$ENVFILE") root:root" ;;
    "cat $ORG_FILE") cat "$ENVFILE" ;;
    *) echo "unexpected: sudo $*"; return 9 ;;
  esac
}
"""
ORGS = ["Example-Org", "MathAgents", "a", "x" * 39, "", "-x", "x-", "a b", "a/b", "a.b", "x" * 40,
        "Org;true", "Org\nx"]  # fmt: skip


@pytest.mark.parametrize("org", ORGS)
@pytest.mark.parametrize("where", ["§3", "step 3"])
def test_the_runbook_writes_only_an_organization_the_server_takes(tmp_path, org, where):
    """Review of #770, N3: §3's line and the update's step 3 write `zab-files.env` only for a
    `FILES_ORG` that the server takes, so that a typo stops there, not at a failed start;
    step 3 says `stopped` then, and never changes a file that is there."""
    server = server_module()
    writes = [b for b in BLOCKS if f"| sudo tee {server.ORG_FILE}" in b]
    body = (
        writes[1]
        if where == "step 3"
        else next(
            line for line in writes[0].splitlines() if f"| sudo tee {server.ORG_FILE}" in line
        )
    )
    envfile = tmp_path / "zab-files.env"
    env = {
        "PATH": "/usr/bin:/bin",
        "ENVFILE": str(envfile),
        "ORG_FILE": server.ORG_FILE,
        "FILES_ORG": org,
    }
    result = subprocess.run(  # noqa: S603 — the runbook's own block, with sudo stubbed
        ["/bin/bash", "-c", ENV_STUBS + body], capture_output=True, text=True, check=False, env=env
    )
    assert "unexpected" not in result.stdout
    if server.ORG_NAME.fullmatch(org):
        assert envfile.read_text() == f"ZAB_FILES_ORG={org}\n"
        assert envfile.stat().st_mode & 0o7777 == 0o644
        assert result.stdout.endswith(f"ZAB_FILES_ORG={org}\n")
        if where == "step 3":  # its read-back, as the runbook expects it
            assert result.stdout == f"644 root:root\nZAB_FILES_ORG={org}\n"
    else:
        assert not envfile.exists()
        assert ("stopped: FILES_ORG=" in result.stdout) is (where == "step 3"), result.stdout
    if where == "step 3":
        envfile.write_text("ZAB_FILES_ORG=Kept-Org\n")
        again = subprocess.run(  # noqa: S603
            ["/bin/bash", "-c", ENV_STUBS + body], capture_output=True, text=True, env=env
        )
        assert envfile.read_text() == "ZAB_FILES_ORG=Kept-Org\n"  # never overwritten
        assert ("Kept-Org" in again.stdout) is bool(server.ORG_NAME.fullmatch(org))


#: The port 22 blocks of *SSH to the files host*, which the update repeats where it runs them
#: (#470: step 0 opens, step 8 closes), and how many copies it has of each.
SSH = TEXT[TEXT.index("## SSH to the files host") : TEXT.index("## Updating a running server")]
PORT_22 = {
    "home": (lambda b: 'HOMEIP="$(curl' in b, 1),
    "group": (lambda b: "FSG=$(aws ec2 describe-instances" in b, 1),
    "open": (lambda b: "authorize-security-group-ingress" in b, 1),
    "close": (lambda b: "revoke-security-group-ingress" in b and "authorize" not in b, 1),
    "read back": (lambda b: "describe-security-groups " in b, 2),
}


def commands(body: str) -> str:
    """A block without its comment lines, which say what it prints where it runs."""
    return "".join(line for line in body.splitlines(keepends=True) if not line.startswith("#"))


@pytest.mark.parametrize("kind", PORT_22)
def test_the_update_repeats_the_port_22_blocks_exactly(kind):
    matches, copies = PORT_22[kind]
    [original] = [b for b in re.findall(r"^```bash\n(.*?)^```$", SSH, re.S | re.M) if matches(b)]
    repeated = [b for b in re.findall(r"^```bash\n(.*?)^```$", UPDATE, re.S | re.M) if matches(b)]
    assert len(repeated) == copies
    assert all(commands(b) == commands(original) for b in repeated)


#: Every rule for port 22 alone, whatever its source (IPv4, IPv6, a group, a prefix list); never
#: a wider one, which may also carry 80 or 443 (review on #445).
RULES = "SecurityGroupRules[?!IsEgress && FromPort==`22` && ToPort==`22`].SecurityGroupRuleId"
DOCS = {
    "workshop/files/README.md": (TEXT, 4),  # Open and Close, and their copies in the update
    "docs/BOOTSTRAP.md": ((ROOT / "docs" / "BOOTSTRAP.md").read_text(), 1),
}


@pytest.mark.parametrize("doc", DOCS)
def test_port_22_is_revoked_by_rule_id_and_never_opened_to_home(doc):
    text, count = DOCS[doc]
    blocks = re.findall(r"^```(?:bash|sh)\n(.*?)^```$", text, re.S | re.M)
    revoking = [b for b in blocks if "revoke-security-group-ingress" in b]
    assert len(revoking) == count
    for body in revoking:
        assert "--cidr" not in body and "IpRanges[].CidrIp' --output text); do" not in body
        assert RULES in body and "describe-security-group-rules" in body
        assert '--security-group-rule-ids "$R" --query Return --output text' in body
        if "authorize-security-group-ingress" in body:
            # the development machine's address is the home network's: refused, and required to be
            # known
            assert '== "$HOMEIP" ]]' in body and "${HOMEIP:-} == none" in body
        result = subprocess.run(
            ["/bin/bash", "-n"], input=body, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr


#: `aws` and `curl` as shell functions: checkip answers 192.0.2.7, the group holds two 22 rules.
STUBS = """
aws() {
  echo "aws $*" >> "$LOG"
  case "$*" in
    *describe-security-group-rules*) echo "sgr-1	sgr-2" ;;
    *describe-security-groups*) echo "sg-0abc" ;;
    *--query\\ Return*) echo True ;;
  esac
}
curl() { echo 192.0.2.7; }
FSG=sg-0abc
"""


@pytest.mark.parametrize("doc", DOCS)
@pytest.mark.parametrize(
    ("home", "opens"),
    [(None, False), ("", False), ("192.0.2.7/32", False), ("/32", False), ("none", True),
     ("198.51.100.1/32", True)],
)  # fmt: skip
def test_opening_refuses_home_and_revokes_each_rule(tmp_path, doc, home, opens):
    text, _ = DOCS[doc]
    blocks = re.findall(r"^```(?:bash|sh)\n(.*?)^```$", text, re.S | re.M)
    body = next(b for b in blocks if "authorize-security-group-ingress" in b and "HOMEIP" in b)
    log = tmp_path / "aws.log"
    log.touch()
    env = {"PATH": "/usr/bin:/bin", "LOG": str(log)} | ({} if home is None else {"HOMEIP": home})
    result = subprocess.run(  # noqa: S603 — the runbook's own block, with aws and curl stubbed
        ["/bin/bash", "-c", STUBS + body], capture_output=True, text=True, check=False, env=env
    )
    calls = log.read_text().splitlines()
    revokes = [c for c in calls if "revoke-security-group-ingress" in c]
    authorizes = [c for c in calls if "authorize-security-group-ingress" in c]
    if opens:
        assert [r.split("--security-group-rule-ids ")[1].split()[0] for r in revokes] == [
            "sgr-1",
            "sgr-2",
        ]
        assert len(authorizes) == 1 and "CidrIp=192.0.2.7/32" in authorizes[0]
    else:
        assert not revokes and not authorizes, calls
        assert "stopped" in result.stdout, result.stdout


@pytest.mark.parametrize("doc", DOCS)
@pytest.mark.parametrize("group", ["None", "", "sg-0abc\tsg-0def", "sg-0abc; true"])
def test_opening_needs_one_group_id(tmp_path, doc, group):
    """Review of #445 (#435): the open touches the group only when its lookup gave one ID
    (`sg-…`), in BOOTSTRAP §3.1's refresh as in the files host's Open. `None` is what the AWS
    CLI prints when no group matches."""
    text, _ = DOCS[doc]
    blocks = re.findall(r"^```(?:bash|sh)\n(.*?)^```$", text, re.S | re.M)
    body = next(b for b in blocks if "authorize-security-group-ingress" in b and "HOMEIP" in b)
    stubs = STUBS.replace('echo "sg-0abc"', f"echo '{group}'").replace(
        "FSG=sg-0abc", f"FSG='{group}'"
    )
    assert stubs.count(f"'{group}'") == 2  # both stand-ins replaced
    log = tmp_path / "aws.log"
    log.touch()
    env = {"PATH": "/usr/bin:/bin", "LOG": str(log), "HOMEIP": "198.51.100.1/32"}
    result = subprocess.run(  # noqa: S603 — the runbook's own block, with aws and curl stubbed
        ["/bin/bash", "-c", stubs + body], capture_output=True, text=True, check=False, env=env
    )
    calls = log.read_text().splitlines()
    assert not [c for c in calls if "-security-group-rules" in c or "-ingress" in c], calls
    assert "stopped: group" in result.stdout, result.stdout
