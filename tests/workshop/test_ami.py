"""`workshop/ami/build.sh` (workshop/ami/README.md): package list and shell syntax, unrun."""

from __future__ import annotations

import io
import pathlib
import re
import shutil
import subprocess

import pytest

BUILD_SH = pathlib.Path(__file__).resolve().parents[2] / "workshop" / "ami" / "build.sh"

#: workshop/ami/README.md, "What the image holds": PARI/GP is built from source (D18), so its apt
#: packages are gone and the build needs GMP and readline headers; python3-dev stays for a
#: cypari2 source build (codex review round 1 on PR #19).
REQUIRED_APT = {
    "build-essential",
    "git",
    "python3",
    "python3-venv",
    "pipx",
    "tmux",
    "screen",
    "htop",
    "ripgrep",
    "jq",
    "unzip",
    "curl",
    "ssh-import-id",
    "libgmp-dev",
    "libreadline-dev",
    "libflint-dev",
    "gap",
    "python3-dev",
}


def apt_packages(script: str) -> set[str]:
    """The packages of the `apt-get install` command, following backslash continuations."""
    text = script.replace("\\\n", " ")
    m = re.search(r"(?:apt-get|\$APT) install[^\n]*", text)
    assert m, "no apt-get install line"
    words = m.group(0).split()
    return {w for w in words[words.index("install") + 1 :] if not w.startswith("-")}


def test_apt_packages_cover_the_workshop_list_and_cypari2_headers():
    text = BUILD_SH.read_text()
    packages = apt_packages(text)
    assert REQUIRED_APT <= packages
    # no apt PARI in our list (the 2.17.4 build is the gp on PATH), but no purge either: Ubuntu's
    # pari-gp comes in as gap-alnuth's dependency and must stay (codex review on #56, D18)
    assert not any(p.startswith(("pari-", "libpari")) for p in packages)
    assert "purge" not in text
    assert "gap-alnuth" in packages
    assert 'LoadPackage(\\"alnuth\\")' in text and '" = true' in text
    assert 'test "$(command -v gp)" = /usr/local/bin/gp' in text


def test_pari_is_built_from_source_with_pinned_checksums():
    """D18: PARI/GP 2.17.4 into /usr/local with GMP and readline; the source tarball and the five
    data packages are fetched from the PARI site and checked against recorded sha256 values."""
    text = BUILD_SH.read_text()
    assert 'PARI_VERSION="${PARI_VERSION:-2.17.4}"' in text
    assert re.search(r'PARI_SHA256="[0-9a-f]{64}"', text)
    for name in ("elldata", "galdata", "seadata", "galpol", "nflistdata"):
        assert re.search(rf'\[{name}\]="[0-9a-f]{{64}}"', text), name
    assert "./Configure --prefix=/usr/local --with-gmp --with-readline" in text
    assert "sudo make install" in text
    assert 'fetch_checked "$PARI_SHA256" "$src/pari-$PARI_VERSION.tar.gz"' in text
    assert (
        '"$PARI_URL/unix/OLD/${PARI_VERSION%.*}/pari-$PARI_VERSION.tar.gz"' in text
    )  # next release
    assert (
        'fetch_checked "${PARI_DATA_SHA256[$name]}" "/tmp/$name.tgz" "$PARI_URL/packages/$name.tgz"'
        in text
    )
    assert "curl -fsSL --retry 5 --retry-all-errors" in text  # a mirror cut a transfer short once
    pari = text.split("# --- 3. PARI", 1)[1].split("# --- 3b.", 1)[0]
    assert "sudo make install-nodata" in pari and "sudo make install " not in pari  # no TeX docs
    # pari-gp's /etc/gprc colours and cuts piped output (PM review on #60): deleted, and the
    # smoke test runs gp without -f so that any gprc would show
    assert "sudo rm -f /etc/gprc" in text and text.index("sudo rm -f /etc/gprc") > text.index(
        "gap-alnuth"
    )
    assert 'out=$(echo "matid(60)" | gp -q 2>/dev/null)' in text
    assert 'check bash -c "$GP_PLAIN"' in text and 'check as_agent bash -lc "$GP_PLAIN"' in text
    assert (
        "-C /usr/local/share/pari --strip-components=1" in text
    )  # the tarballs unpack as data/<name>/
    assert "sha256sum --check" in text
    # the smoke test exercises each data package (DECISIONS verification log, 2026-10-02)
    for probe in (
        'ellinit(\\"11a1\\")',
        "polgalois(x^8-2)",
        "ellsea(ellinit(",
        "galoisgetpol(10,1)",
        'nflist(\\"A5\\"',
    ):
        assert probe in text, probe
    assert "gp --version-short" in text
    # built before the venv: a cypari2 sdist build needs the headers (codex round 7 on PR #56)
    assert text.index("./Configure --prefix=/usr/local") < text.index(
        '--require-hashes -r "$FILES/venv-requirements.txt"'
    )


def magma_step(text: str) -> str:
    """build.sh's step 8, Magma."""
    return "# --- 8. Magma" + text.split("# --- 8. Magma")[1].split("# --- 9.")[0]


def run_magma_step(tmp_path: pathlib.Path, setup: str, start_fails: bool = False):
    """Step 8 under bash with `sudo` recorded and not run: (exit code, stdout, stderr, the sudo
    command lines, the unit file written). The paths it is given need not exist."""
    tmp_path.mkdir()
    calls, unit = tmp_path / "sudo-calls", tmp_path / "unit"
    stubs = (
        "set -euo pipefail\n"
        'log() { echo "==> $*"; }\n'
        "magma_layout() { echo flat; }\n"
        "grep() { return 1; }\n"  # the driver checks read /usr/local/magma/magma, absent here
        f'sudo() {{ printf "%s\\n" "$*" >> {calls}; if [ "$1" = tee ]; then cat > {unit}; fi\n'
        '  [ "$*" != "systemctl start magma-licence.service" ] || [ -z "$START_FAILS" ]; }\n'
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "MAGMA_TARBALL": "/nowhere/magma.tar.gz",
        "MAGMA_PASSFILE": "/nowhere/magmapassfile",
        "MAGMA_LICENCE_SETUP": setup,
        "START_FAILS": "1" if start_fails else "",
    }
    script = stubs + magma_step(BUILD_SH.read_text())
    run = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)  # noqa: S603, S607 - build.sh's own step
    sudo = calls.read_text().splitlines() if calls.exists() else []
    return run.returncode, run.stdout, run.stderr, sudo, unit.read_text() if unit.exists() else ""


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_magma_step_installs_and_starts_the_licence_setup_without_reading_it(tmp_path):
    """#593: with MAGMA_LICENCE_SETUP the step installs the script root-only, writes and enables
    the unit and starts it once; a failed start stops the build; without it, Magma is installed
    and the log says the licence file must suffice. The script itself is never opened (its path
    here does not even exist)."""
    code, out, err, sudo, unit = run_magma_step(tmp_path / "a", "/nowhere/setup")
    assert code == 0, err
    assert "install -m 0644 /nowhere/magmapassfile /usr/local/magma/magmapassfile" in sudo
    assert sudo[-5:] == [
        "install -o root -g root -m 0700 /nowhere/setup /usr/local/sbin/magma-licence-setup",
        "tee /etc/systemd/system/magma-licence.service",
        "systemctl daemon-reload",
        "systemctl enable magma-licence.service",
        "systemctl start magma-licence.service",
    ]
    assert "ExecStart=/usr/local/sbin/magma-licence-setup" in unit.splitlines()
    code, out, err, sudo, unit = run_magma_step(tmp_path / "b", "/nowhere/x", start_fails=True)
    assert code == 1 and "the Magma licence setup failed" in err
    code, out, err, sudo, unit = run_magma_step(tmp_path / "c", "")
    assert code == 0, err
    assert "ln -sf /usr/local/magma/magma /usr/local/bin/magma" in sudo
    assert not any("magma-licence" in line for line in sudo) and unit == ""
    assert "no MAGMA_LICENCE_SETUP: Magma runs only where its licence file alone suffices" in out


def test_magma_is_optional_and_its_licence_setup_an_opaque_hook():
    """D19, D39 (#593): Magma only with the tarball and its licence file, installed to
    /usr/local/magma, the licence as /usr/local/magma/magmapassfile; the deployer's licence setup,
    optional, installed root-only and run as root at every boot by a oneshot ordered before
    logins and user services, with a bounded start and stop. build.sh never reads the script."""
    text = BUILD_SH.read_text()
    assert 'if [ -n "$MAGMA_TARBALL" ] && [ -z "$MAGMA_PASSFILE" ]; then' in text
    assert 'if [ -n "$MAGMA_LICENCE_SETUP" ] && [ -z "$MAGMA_TARBALL" ]; then' in text
    step = magma_step(text)
    assert 'sudo install -m 0644 "$MAGMA_PASSFILE" /usr/local/magma/magmapassfile' in step
    assert "sudo ln -sf /usr/local/magma/magma /usr/local/bin/magma" in step
    install = (
        'sudo install -o root -g root -m 0700 "$MAGMA_LICENCE_SETUP"'
        " /usr/local/sbin/magma-licence-setup"
    )
    assert install in step
    unit = step.split("magma-licence.service >/dev/null <<'EOF'\n")[1].split("\nEOF\n")[0]
    for line in (
        "Wants=network-online.target",
        "After=network-online.target",
        "Before=systemd-user-sessions.service",  # user@.service, so the bridge, comes after
        "Type=oneshot",
        "ExecStart=/usr/local/sbin/magma-licence-setup",
        "RemainAfterExit=yes",
        "TimeoutStartSec=120",
        "TimeoutStopSec=10",  # after a start timeout, SIGTERM then SIGKILL each wait this long
        "WantedBy=multi-user.target",
    ):
        assert line in unit.splitlines(), line
    assert "systemctl enable magma-licence.service" in step
    assert "if ! sudo systemctl start magma-licence.service; then" in step
    # without the script, Magma is installed and the log says what that means
    assert "no MAGMA_LICENCE_SETUP: Magma runs only where its licence file alone suffices" in step
    # opaque: the script is installed and run, never read, searched or parsed
    uses = [line.strip() for line in step.splitlines() if "$MAGMA_LICENCE_SETUP" in line]
    assert uses == ['if [ -n "$MAGMA_LICENCE_SETUP" ]; then', install], uses
    # the smoke test checks the unit where the image has it and that Magma computes, nothing else
    smoke = text.split("smoke test (workshop/ami/README.md)")[1]
    block = smoke.split('if [ -n "$MAGMA_TARBALL" ]; then\n')[1].split("\nfi\n")[0]
    commands = [line.strip() for line in block.splitlines() if not line.strip().startswith("#")]
    assert commands == [
        'if [ -n "$MAGMA_LICENCE_SETUP" ]; then'
        " check systemctl is-active magma-licence.service; fi",
        'if as_agent bash -c \'test "$(echo "print 2+2;" | timeout 120 magma -b 2>/dev/null'
        ' | tr -d "[:space:]")" = 4\'; then',
        'echo "ok   magma -b runs print 2+2"',
        "else",
        'echo "FAIL magma -b does not print 4 for print 2+2: is its licence accepted on this'
        ' host? (workshop/ami/README.md)"',
        "fail=1",
        "fi",
    ], commands
    # nothing Magma-related is in the repository besides the script
    ami_dir = BUILD_SH.parent
    assert not any("magma" in p.name.lower() for p in ami_dir.iterdir()), list(ami_dir.iterdir())
    gitignore = (ami_dir.parents[1] / ".gitignore").read_text()
    assert "workshop/ami/magma*" in gitignore and "*magmapassfile*" in gitignore


def magma_layout_fn() -> str:
    """The `magma_layout` function as defined in build.sh."""
    text = BUILD_SH.read_text()
    start = text.index("magma_layout() {")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_magma_layout_survives_pipefail_on_a_large_archive(tmp_path):
    """Codex round 1 on PR #56: `tar -tzf … | head -n1` got SIGPIPE on a large listing and, under
    `set -euo pipefail`, aborted the build. The listing is consumed whole; 5,002 entries here."""
    import tarfile

    nested = tmp_path / "nested.tar.gz"
    with tarfile.open(nested, "w:gz") as tar:
        for i in range(5000):
            info = tarfile.TarInfo(f"magma/libs/data/f{i:05d}")
            tar.addfile(info, io.BytesIO(b""))
        tar.addfile(tarfile.TarInfo("magma/magma.exe"), io.BytesIO(b""))
        tar.addfile(tarfile.TarInfo("magma/magma"), io.BytesIO(b""))
    flat = tmp_path / "flat.tar.gz"
    with tarfile.open(flat, "w:gz") as tar:
        for i in range(5000):
            tar.addfile(tarfile.TarInfo(f"libs/data/f{i:05d}"), io.BytesIO(b""))
        tar.addfile(tarfile.TarInfo("magma.exe"), io.BytesIO(b""))
    script = "set -euo pipefail\n" + magma_layout_fn() + '\nmagma_layout "$1"\n'
    for archive, expected in ((nested, "nested"), (flat, "flat")):
        res = subprocess.run(  # noqa: S603
            ["bash", "-c", script, "bash", str(archive)],  # noqa: S607
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == expected
    text = BUILD_SH.read_text()
    assert (
        'tar -tzf "$MAGMA_TARBALL" | head' not in text
        and "| grep -q" not in text.split("magma_layout()")[1].split("\n}\n")[0]
    )


def test_agent_home_is_only_read_as_agent():
    """Codex round 2 on PR #56: /home/agent is 0750 on Ubuntu 24.04, so a `test`, `grep` or `cat`
    of anything under it run as ubuntu fails, and under `set -euo pipefail` aborts the build."""
    readers = ("[ ", "test ", "grep ", "cat ", "jq ", "head ", "tail ")
    for line in joined_lines(BUILD_SH.read_text()):
        code = line.split("#", 1)[0]
        if ("$AGENT_HOME" in code or "$codexrc" in code) and any(r in code for r in readers):
            if "install -d" in code or "install -o" in code:
                continue  # writes as root, which can create anywhere
            assert "as_agent" in code or 'sudo -u "$AGENT"' in code, code.strip()


def test_sage_is_pinned():
    text = BUILD_SH.read_text()
    assert (
        'SAGE_VERSION="${SAGE_VERSION:-10.9}"' in text
        and 'SAGE_PYTHON="${SAGE_PYTHON:-3.12}"' in text
    )
    assert "'sage=$SAGE_VERSION' 'python=$SAGE_PYTHON'" in text


def test_pins_and_mcp_mirror_the_first_boot():
    """Drew's decisions on issue #36: the updaters off, the LMFDB server on both CLIs, with the same
    lines as the first-boot script (workshop/aws/aws_common.py), and checked by the smoke test."""
    text = BUILD_SH.read_text()
    assert 'data.setdefault("env", {})["DISABLE_AUTOUPDATER"] = "1"' in text
    assert 'data["disableClaudeAiConnectors"] = True' in text  # AMI v3 (issue #36)
    assert "/etc/claude-code/managed-settings.json" in text
    assert "if ! as_agent grep -q '^check_for_update_on_startup'" in text  # prepended, root key
    assert '\'\\n[mcp_servers.lmfdb]\\nurl = "%s"\\n\' "$LMFDB_MCP_URL"' in text
    assert 'mcp add --transport http --scope user lmfdb "$LMFDB_MCP_URL"' in text
    assert 'jq "del(.machineID, .userID)"' in text
    assert "claude doctor </dev/null 2>&1 | grep -q 'Auto-updates: disabled'" in text
    assert "mcp get lmfdb 2>/dev/null | grep -q '$LMFDB_MCP_URL'" in text


def test_build_script_is_strict_and_non_interactive():
    text = BUILD_SH.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert "DEBIAN_FRONTEND=noninteractive" in text
    assert "set -x" not in text
    assert "loginctl enable-linger" in text and "visudo -cf" in text
    assert BUILD_SH.stat().st_mode & 0o111, "build.sh must be executable"


def test_supplied_wheel_is_staged_for_the_agent_user():
    """`ZAB_WHEEL` under /home/ubuntu is unreadable by `agent` (0750 homes): it must be copied
    into an agent-readable place before `uv tool install` runs as `agent` (codex round 2)."""
    text = BUILD_SH.read_text()
    installs = [line for line in text.splitlines() if "tool install" in line]
    assert installs, "no uv tool install line"
    assert not any('--force "$ZAB_WHEEL"' in line for line in installs), "wheel passed to uv as is"
    assert any('--force "$staged/' in line for line in installs), "uv must install the staged copy"
    assert re.search(r'sudo install -o "\$AGENT" -g "\$AGENT" -m 0644 "\$ZAB_WHEEL"', text)
    staged = re.search(r"staged=\$\(mktemp -d [^)]*\)\n\s*chmod 0755 \"\$staged\"", text)
    assert staged, "the staging directory must be world-searchable"


def joined_lines(script: str) -> list[str]:
    return script.replace("\\\n", " ").splitlines()


def test_every_directory_under_agent_home_is_created_agent_owned():
    """`install -d` applies -o/-g only to the directories named on the command line and creates
    missing parents as root (coreutils), so each intermediate directory under ~agent must be
    named explicitly, and nothing else may create directories there as root (codex round 4)."""
    created: set[str] = set()
    for line in joined_lines(BUILD_SH.read_text()):
        code = line.split("#", 1)[0]
        paths = re.findall(r'"\$AGENT_HOME(/[^"]*)"', code)
        if "install -d" in code:
            dirs = [p for p in paths if p]
            if not dirs:
                continue  # a system directory (/etc/claude-code, /usr/local/…): root's is right
            assert '-o "$AGENT" -g "$AGENT"' in code, f"root-owned install -d: {code.strip()}"
            for d in dirs:
                parent = d.rsplit("/", 1)[0]
                assert parent == "" or parent in created, (
                    f"{d} would get a root-owned parent {parent}: name it first"
                )
                created.add(d)
        elif "mkdir" in code and paths:
            assert "as_agent" in code or 'sudo -u "$AGENT"' in code, code.strip()
    assert {"/.local", "/.local/bin", "/.config", "/.codex", "/.ssh", "/work"} <= created


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_build_script_parses():
    subprocess.run(["bash", "-n", str(BUILD_SH)], check=True)  # noqa: S603, S607 - syntax only


def test_housekeeping_runs_last_and_apt_is_non_interactive():
    """PM review on #56: the smoke test ran claude after the id scrub, putting the builder's ids
    back into the image; housekeeping is now the last step. apt waits for the lock, retries the
    index update, and gets DEBIAN_FRONTEND through sudo's environment reset."""
    text = BUILD_SH.read_text()
    assert text.index("smoke test (workshop/ami/README.md)") < text.index("housekeeping, last")
    assert text.index('jq "del(.machineID, .userID)"') > text.index(
        "smoke test (workshop/ami/README.md)"
    )
    assert text.count('jq "del(.machineID, .userID)"') == 1
    assert text.index("cloud-init clean --logs --machine-id") > text.index(
        "smoke test (workshop/ami/README.md)"
    )
    assert "cloud-init status --wait" in text
    assert (
        'APT="sudo env DEBIAN_FRONTEND=noninteractive apt-get -q -o DPkg::Lock::Timeout=600"'
        in text
    )
    assert "sudo apt-get" not in text
    assert "for attempt in 1 2 3" in text
    # the Magma driver must not point back at /opt/magma
    assert "grep -q '/opt/magma' /usr/local/magma/magma" in text
    # the Magma check runs as agent; image hygiene in the housekeeping
    assert 'if as_agent bash -c \'test "$(echo "print 2+2;" | timeout 120 magma -b' in text
    for needle in (
        "$APT clean",
        'conda" clean -a -y',
        "journalctl --rotate --vacuum-time=1s",
        'uv" cache clean',
    ):
        assert needle in text, needle


# ------------------------------------------------------------------ AMI v2 (issue #36, Sat Oct 3)

FILES = BUILD_SH.parent / "files"


def install_lines(script: str) -> list[set[str]]:
    """The package sets of every `$APT install` command."""
    text = script.replace("\\\n", " ")
    sets = []
    for m in re.finditer(r"\$APT install[^\n]*", text):
        words = m.group(0).split()
        sets.append({w for w in words[words.index("install") + 1 :] if not w.startswith("-")})
    return sets


def test_latex_covers_the_pm_list():
    """PM's list, item 1: the TeX packages, biber and the tools, in a separate apt step."""
    tex = install_lines(BUILD_SH.read_text())[1]
    assert {
        "texlive-latex-extra",
        "texlive-science",
        "texlive-pictures",
        "texlive-publishers",
        "texlive-bibtex-extra",
        "texlive-fonts-recommended",
        "texlive-fonts-extra",
        "cm-super",
        "texlive-luatex",
        "texlive-xetex",
        "biber",
        "latexmk",
        "chktex",
        "latexdiff",
    } <= tex


def test_codex_comes_from_the_pinned_package_with_its_code_mode_host():
    """AMI v1 shipped Codex without codex-code-mode-host (`find | head -n1`; PM on #36)."""
    text = BUILD_SH.read_text()
    assert "head -n1)" not in text.split("# --- 5.", 1)[1].split("# --- 6.", 1)[0]
    assert re.search(r'CODEX_PKG_SHA256="\$\{CODEX_PKG_SHA256:-[0-9a-f]{64}\}"', text)
    assert 'fetch_checked "$CODEX_PKG_SHA256"' in text
    assert "codex-package-x86_64-unknown-linux-musl.tar.gz" in text
    assert (
        "bin/codex bin/codex-code-mode-host codex-package.json codex-path codex-resources" in text
    )
    assert 'ln -sfn "$CODEX_DIR/bin/codex" "$AGENT_HOME/.local/bin/codex"' in text
    assert 'check as_agent "$CODEX_DIR/bin/codex-code-mode-host" --help' in text


def test_every_new_download_is_pinned():
    """elan, Go and rustup by sha256 through fetch_checked; git sources by full commit."""
    text = BUILD_SH.read_text()
    for name in ("ELAN_SHA256", "GO_SHA256", "RUSTUP_SHA256"):
        assert re.search(rf"^{name}=[0-9a-f]{{64}}", text, re.M), name
        assert f'fetch_checked "${name}"' in text, name
    for name in ("LANDRUN_COMMIT", "COMPARATOR_COMMIT", "NANODA_COMMIT", "LEAN_COMMIT"):
        assert re.search(rf"^{name}=[0-9a-f]{{40}}$", text, re.M), name
    assert re.search(r'LEAN4_SKILLS_SHA="\$\{LEAN4_SKILLS_SHA:-[0-9a-f]{40}\}"', text)
    assert '[ "$got" = "$LEAN4_SKILLS_SHA" ]' in text  # the tag is checked against the commit
    assert 'grep -q "commit $LEAN_COMMIT"' in text  # elan's toolchain download is unchecked
    assert '"lean-lsp-mcp==$LEAN_LSP_MCP_VERSION"' in text


def test_lean_tools_are_registered_with_both_agents():
    text = BUILD_SH.read_text()
    assert 'mcp add --scope user lean-lsp -- "$AGENT_HOME/.local/bin/lean-lsp-mcp"' in text
    assert "[mcp_servers.lean-lsp]\\ncommand" in text and "tool_timeout_sec = 900" in text
    assert "plugin install lean4@lean4-skills" in text
    assert "codex plugin add lean4@lean4-skills" in text
    assert "lake build Mathlib" in text  # the trace files comparator's sandbox cannot write


def test_agent_instructions_are_installed_for_both_agents_and_say_how_to_share():
    text = BUILD_SH.read_text()
    assert '"$FILES/agent-instructions.md" "$AGENT_HOME/.claude/CLAUDE.md"' in text
    assert '"$FILES/agent-instructions.md" "$AGENT_HOME/.codex/AGENTS.md"' in text
    doc = (FILES / "agent-instructions.md").read_text()
    for needle in ("PDF", "PNG", "SVG", "~/lean/workspace", "comparator-run"):
        assert needle in doc, needle
    # the tools themselves (latexmk, the lmfdb server, …) come from the VM's list at rollout
    # (#475; tests/workshop/test_tools_list.py)
    assert doc.count("\n<!-- zab:tools -->\n") == 1
    # since the file server, HTML and SVG open in the browser (PM on issue #36, Mon 19:00);
    # push_config installs this text on the VMs, the AMI's copy is only the starting point
    assert "the workshop's file server" in doc and "only as downloads" not in doc


def test_agent_instructions_send_agents_to_zab_history_and_leave_lean_servers_to_zab():
    """Issues #248 and #249: other topics through `zab history`, never the bridge's zuliprc.
    Lean servers: zab stops Codex's after 30 idle minutes in any directory (SPEC §7.12), so the
    agents stop none themselves: a topic's two agents, and ~/lean/workspace's topics, can't
    tell their servers apart (the plan on #249, Oct 25). Never `lean-lsp-mcp` or a build."""
    doc = " ".join((FILES / "agent-instructions.md").read_text().split())
    for needle in (
        "zab history --list-topics --match",
        "zab history --channel",
        "Never read `~/.config/zab/*.zuliprc` or call the Zulip API directly",
        "A Lean server is one `lake serve` and every process under it",
        "Claude's exit when its turn ends.",
        "zab stops each one that has been idle for 30 minutes, wherever it runs "
        "(`~/lean/workspace` included)",
        "the next Lean tool call starts a fresh one",
        "don't stop Lean servers yourself",
        "Never stop `lean-lsp-mcp`",
        "a build (`lake build`, `lake env lean`)",
        "`free -g`",
        "`!restart codex`",
    ):
        assert needle in doc, needle
    # the manual procedure of #253 is gone: it covered only the topic's own worktree
    for gone in ("kill <pid>", "/proc/<pid>/stat", "--forest", "stop your idle servers"):
        assert gone not in doc, gone


def test_shipped_files_exist_and_the_wrapper_is_executable():
    wrapper = FILES / "bin" / "comparator-run"
    assert wrapper.stat().st_mode & 0o111
    assert "RestrictAddressFamilies=~AF_UNIX" in wrapper.read_text()
    for name in ("smoke.tex", "smoke.bib", "Smoke.lean"):
        assert (FILES / "smoke" / name).is_file()
    project = FILES / "smoke" / "comparator"
    assert (project / "lean-toolchain").read_text().strip() == "leanprover/lean4:v4.34.1"
    assert "sorry" in (project / "Challenge.lean").read_text()
    assert "sorry" not in (project / "Solution.lean").read_text()


def test_smoke_covers_the_v2_additions():
    smoke = BUILD_SH.read_text().split("smoke test (workshop/ami/README.md)", 1)[1]
    for needle in (
        'codex-code-mode-host" --help',
        "mcp get lean-lsp",
        "lean4@lean4-skills",
        "latexmk -pdf",
        "Smoke.lean) && grep -qx 1024",  # lean's own exit status counts
        "comparator-run config.json",
        "command -v python3",
        "bash -lc 'command -v lean",  # the participant's login shell, not a supplied PATH
    ):
        assert needle in smoke, needle
    assert '"\\.elan/bin" ~/"$1"' in BUILD_SH.read_text()  # ~/.profile and ~/.bashrc


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_comparator_wrapper_parses():
    subprocess.run(["bash", "-n", str(FILES / "bin" / "comparator-run")], check=True)  # noqa: S603, S607


def test_the_ami_shares_one_mathlib_and_smoke_tests_it():
    """Issue #205: lean-project installed, the workspace's packages shared right after they
    are built, and the smoke test checks a project through them."""
    text = BUILD_SH.read_text()
    install = 'sudo install -m 0755 "$FILES/bin/lean-project" /usr/local/bin/lean-project'
    share = 'lean-project --install "$AGENT_HOME/lean/workspace"'
    assert text.index("lake build Mathlib") < text.index(install) < text.index(share)
    assert "check as_lean lean-project --check --comparator --max-seconds 120" in text
    assert (FILES / "bin" / "lean-project").stat().st_mode & 0o111


def test_agent_instructions_use_the_shared_mathlib():
    """Issue #205: scratch in ~/lean/workspace, projects through lean-project, narrow imports,
    none of the commands that fail or stall on the shared copy, and the plugin's per-worktree
    cache advice overridden."""
    doc = (FILES / "agent-instructions.md").read_text()
    for needle in (
        "~/lean/workspace",
        "`lean-project DIR`",
        "lean-project --repair DIR",
        "Before the first `lake` command in",  # whether or not the hook linked it (codex, #261)
        "Import narrowly",
        "`lake update`, `lake cache get`",
        "`lake exe cache get` or `lake clean`",
        "lean4 plugin's advice to prime a cache",
        "ask the participant",
        "With their OK, `rm .lake/packages`",  # the private copy, as DECISIONS says (#246)
    ):
        assert needle in doc, needle
    assert "unpacks its own copy" not in doc  # the per-project Mathlib advice of #224 is gone
