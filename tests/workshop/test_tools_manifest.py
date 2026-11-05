"""The image's tools manifest (issue #474): `files/tools.toml`, the renderer
`files/bin/tools_manifest.py` that checks every tool and writes /etc/zab/tools.json, and the
build and smoke steps that run it. Offline: the renderer runs real `bash -c` commands, all local."""

from __future__ import annotations

import datetime
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tomllib
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

AMI = Path(__file__).resolve().parents[2] / "workshop" / "ami"
FILES = AMI / "files"
SCRIPT = FILES / "bin" / "tools_manifest.py"
SOURCE = FILES / "tools.toml"
BUILD_SH = AMI / "build.sh"

#: The fixed areas, in display order (tools.toml's header documents them; `!tools` groups by them).
AREAS = [
    "Number theory",
    "General computer algebra",
    "Groups and representation theory",
    "Commutative algebra and algebraic geometry",
    "Polyhedral geometry and combinatorics",
    "Low-dimensional topology",
    "Optimization and constraint solving",
    "Numerics, statistics and data",
    "Formal proof",
    "Documents and figures",
]

#: Drew's decisions of Oct 18 and what the image already had: each must have an entry.
REQUIRED_TOOLS = {
    # already on the image
    "PARI/GP", "cypari2", "LMFDB", "SageMath", "Magma", "Oscar.jl", "GAP", "Julia", "Python",
    "NumPy", "SymPy", "matplotlib", "Jupyter", "Lean 4 and Mathlib", "comparator", "TeX Live",
    # inside Oscar and Sage
    "GAP 4.16 (Oscar)", "Singular (Oscar)", "polymake (Oscar)", "Giac",
    # "Add now"
    "Macaulay2", "4ti2", "gfan", "lrslib", "msolve", "nauty", "Normaliz", "TOPCOM", "CSDP",
    "Singular", "polymake", "Maxima", "R", "SciPy", "pandas", "NetworkX", "statsmodels",
    "scikit-learn", "seaborn", "HiGHS", "SCIP (PySCIPOpt)", "PuLP", "CVXPY", "SnapPy", "Regina",
    "Graphviz", "gnuplot", "ImageMagick", "Poppler", "librsvg", "Ghostscript", "Z3", "CaDiCaL",
    # "For Harvard, or later", and atlas
    "HomotopyContinuation.jl", "IRkernel", "OR-Tools", "pandoc", "Octave", "atlas",
}  # fmt: skip
EXCLUDED = re.compile(r"wolfram|mathematica|agda|rocq|\bcoq\b|isabelle", re.IGNORECASE)


def load():
    loader = SourceFileLoader("tools_manifest", str(SCRIPT))
    spec = importlib.util.spec_from_loader("tools_manifest", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tools_manifest"] = module  # dataclasses look their module up
    loader.exec_module(module)
    return module


tm = load()


def source_text(*tools: str, areas: list[str] | None = None) -> str:
    head = f"schema = 1\nareas = {json.dumps(areas or ['Number theory'])}\n"
    return head + "".join(tools)


def tool(name: str, **kw: object) -> str:
    fields = {
        "area": "Number theory",
        "call": f"{name.lower()} file",
        "summary": f"{name} does things",
        "version": "echo 1.2.3",
        "check": "echo working; echo 42",
        "expect": "42",
        **kw,
    }
    lines = [f"\n[[tool]]\nname = {json.dumps(name)}"]
    lines += [f"{k} = {json.dumps(v)}" for k, v in fields.items() if v is not None]
    return "\n".join(lines) + "\n"


def parse(text: str):
    return tm.parse_source(tomllib.loads(text))


# ------------------------------------------------------------------------- the source file


def test_the_source_parses_and_names_every_required_tool_once():
    source = tm.load_source(str(SOURCE))
    names = [t.name for t in source.tools]
    assert list(source.areas) == AREAS
    assert REQUIRED_TOOLS <= set(names), REQUIRED_TOOLS - set(names)
    assert len(names) == len({n.casefold() for n in names})
    assert not [n for n in names if EXCLUDED.search(n)]
    # grouped: the entries of one area are contiguous and the areas come in their order
    order = [AREAS.index(t.area) for t in source.tools]
    assert order == sorted(order)


def test_the_source_states_one_call_and_a_real_check_for_every_tool():
    source = tm.load_source(str(SOURCE))
    for t in source.tools:
        assert "\n" not in t.call and len(t.call) <= tm.MAX_CALL, t.name
        assert "\n" not in t.summary and len(t.summary) <= tm.MAX_SUMMARY, t.name
        # a computation, not a version string: the check is not the version command
        assert t.check.strip() != t.version.strip(), t.name
        assert "--version" not in t.check and " -V" not in t.check, t.name
    by_name = {t.name: t for t in source.tools}
    assert {t.name for t in source.tools if t.requires} == {"Magma", "SageMath", "Giac"}
    assert by_name["Magma"].requires == "magma" and by_name["Giac"].requires == "sage"
    # what lives inside Oscar is reached through Julia, and says so
    for name in ("GAP 4.16 (Oscar)", "Singular (Oscar)", "polymake (Oscar)"):
        assert by_name[name].call.startswith("julia -e 'using Oscar;"), name
        assert "using Oscar" in by_name[name].check, name
    # the slow ones (Julia loading Oscar, Lean importing Mathlib) get the longer timeout
    for name in ("Oscar.jl", "GAP 4.16 (Oscar)", "HomotopyContinuation.jl", "Lean 4 and Mathlib"):
        assert by_name[name].timeout >= 900, name


def test_the_source_file_documents_its_format_and_areas():
    header = SOURCE.read_text().split("schema = 1", 1)[0]
    for key in ("name", "area", "call", "summary", "version", "check", "expect", "timeout"):
        assert f"#   {key} " in header, key
    assert "requires" in header and "services'\n# PATH" in header
    assert "A failing check fails the build" in header


# ---------------------------------------------------------------------------- the schema


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (source_text(tool("A")).replace("schema = 1", "schema = 2"), "schema must be 1"),
        ("extra = 1\n" + source_text(tool("A")), "unknown top-level keys"),
        (source_text(tool("A", colour="red")), "unknown keys ['colour']"),
        (source_text(tool("A", area="Topology")), "area 'Topology' is not in areas"),
        (source_text(tool("A"), tool("a")), "duplicate name"),
        (source_text(tool("A", summary="two\nlines")), "summary must be one line"),
        (source_text(tool("A", call="x" * 101)), "call must be one line of at most 100"),
        (source_text(tool("A", expect="")), "expect must be a non-empty string"),
        (source_text(tool("A", check=None)), "check must be a non-empty string"),
        (source_text(tool("A", requires="maple")), "requires must be one of"),
        (source_text(tool("A", timeout=0)), "timeout must be 1 to 3600"),
        (source_text(tool("A"), areas=["Number theory", "Topology"]), "areas without a tool"),
        ("schema = 1\nareas = ['Number theory']\n", "no [[tool]] entries"),
        (source_text(tool("A", remote="yes")), "remote must be true or false"),
        (
            source_text(tool("A", area="B"), tool("C"), areas=["Number theory", "B"]),
            "area 'Number theory' comes after 'B'",
        ),
    ],
)
def test_the_schema_refuses_what_its_header_rules_out(text, message):
    with pytest.raises(tm.SourceError, match=re.escape(message)):
        parse(text)


# ----------------------------------------------------------------------------- the renderer


def write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "tools.toml"
    path.write_text(text)
    return str(path)


def run_main(argv: list[str], capsys) -> tuple[int, str, str]:
    code = tm.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_build_prints_the_manifest_contract_after_every_check_passed(tmp_path, capsys):
    text = source_text(
        tool("Alpha", version="echo 'Alpha 9.1' | cut -d' ' -f2", check="pwd; echo 42"),
        tool("Beta", version="printf '2.0\\nnoise\\n'", check="printf 'x\\r42\\n'"),
        tool("Gamma", requires="sage"),
    )
    code, out, err = run_main(
        ["build", "--source", write(tmp_path, text), "--image", "img-1"], capsys
    )
    assert code == 0, err
    data = json.loads(out)
    assert list(data) == ["schema", "image", "built", "tools"]
    assert data["schema"] == 1 and data["image"] == "img-1"
    assert datetime.date.fromisoformat(data["built"]) == datetime.datetime.now(datetime.UTC).date()
    assert [list(t) for t in data["tools"]] == [["name", "version", "area", "call", "summary"]] * 2
    assert [(t["name"], t["version"]) for t in data["tools"]] == [("Alpha", "9.1"), ("Beta", "2.0")]
    assert data["tools"][0]["call"] == "alpha file" and data["tools"][0]["area"] == "Number theory"
    assert "ok   Alpha 9.1" in err and "Gamma" not in err  # requires sage: not on this image
    assert "2 of 2 tools checked" in err
    # with the component, the tool is checked and listed
    code, out, err = run_main(
        ["build", "--source", write(tmp_path, text), "--image", "i", "--have", "sage,magma"], capsys
    )
    assert code == 0 and [t["name"] for t in json.loads(out)["tools"]] == ["Alpha", "Beta", "Gamma"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ({"check": "echo 41"}, "check gave '41', expected '42' (exit 0)"),
        ({"check": "echo 42; exit 3"}, "(exit 3)"),
        ({"check": "echo oops >&2; false"}, "| oops"),
        ({"version": "echo"}, "version command gave ''"),
        ({"version": "echo version 1.2"}, "version command gave 'version 1.2'"),
        ({"version": "echo unknown"}, "version command gave 'unknown'"),
        ({"version": "false"}, "(exit 1)"),
    ],
)
def test_a_failing_tool_fails_the_build_and_nothing_is_written(tmp_path, capsys, bad, reason):
    text = source_text(tool("Good"), tool("Bad", **bad))
    code, out, err = run_main(["build", "--source", write(tmp_path, text), "--image", "i"], capsys)
    assert code == 1 and out == ""
    assert "ok   Good 1.2.3" in err and "FAIL Bad: " in err and reason in err
    assert "NOT written: 1 of 2 checks failed: Bad" in err


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_failure_logs_no_hardware_address(tmp_path, capsys):
    """Review of #595: a licence refusal can end with the host's hardware addresses, and a
    failure's last output lines go to the build log. Every form is replaced before anything is
    cut short. The addresses are made here, so the tree holds none."""
    octets = ["0a", "1b", "2c", "3d", "4e", "5f"]
    colon, dash = ":".join(octets), "-".join(octets).upper()
    refusal = f"echo 'Error: not licensed on this host'; echo {colon}; echo {dash} >&2; exit 1"
    long_line = "x" * 190 + colon  # glued to other text, and past the 200 characters kept
    text = source_text(
        tool("Refused", version=refusal),
        tool("Late", check=f"echo {long_line} >&2; echo {colon}; exit 2"),
        tool("Clock", check="echo 12:34:56 >&2; echo 1-2-3; false"),
    )
    code, _, err = run_main(["build", "--source", write(tmp_path, text), "--image", "i"], capsys)
    assert code == 1 and "FAIL Refused: version command gave 'Error: not licensed" in err
    assert "| <hardware address>" in err  # the dash form, on stderr, which the detail prefers
    assert "check gave '<hardware address>'" in err  # the reason, from the last stdout line
    assert "| " + "x" * 190 + "<hard" in err  # replaced first, then cut at 200
    for octet in octets:
        assert octet not in err.lower(), octet
    assert "| 12:34:56" in err  # other colon-separated output is left alone


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_hung_check_is_killed_with_its_children(tmp_path, capsys):
    text = source_text(tool("Slow", check="sleep 30 & sleep 30; echo 42", timeout=1))
    code, _, err = run_main(["build", "--source", write(tmp_path, text), "--image", "i"], capsys)
    assert code == 1 and "check timed out after 1 s" in err
    assert float(re.search(r"\((\d+\.\d) s\)", err.split("FAIL Slow")[1]).group(1)) < 15


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_each_command_runs_in_its_own_empty_scratch_directory(tmp_path, capsys):
    text = source_text(tool("Files", version="touch v; ls | wc -l; echo 1", check="ls | wc -l"))
    text = text.replace('expect = "42"', 'expect = "0"')
    code, _, err = run_main(["build", "--source", write(tmp_path, text), "--image", "i"], capsys)
    assert code == 0, err
    assert "ok   Files 1 " in err  # the version's own file was not there before it ran


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_check_runs_every_tool_again_against_the_recorded_versions(tmp_path, capsys):
    text = source_text(
        tool("Alpha"), tool("Beta", version="echo 2.0"), tool("Opt", requires="magma")
    )
    src = write(tmp_path, text)
    code, out, _ = run_main(["build", "--source", src, "--image", "i"], capsys)
    manifest = tmp_path / "tools.json"
    manifest.write_text(out)
    code, _, err = run_main(["check", "--source", src, "--manifest", str(manifest)], capsys)
    assert code == 0 and "ok   Alpha 1.2.3" in err and "ok   Beta 2.0" in err, err
    assert "2 checked, all as recorded" in err
    code, _, err = run_main(
        ["check", "--source", src, "--manifest", str(manifest), "--only", "Beta"], capsys
    )
    assert code == 0 and "Alpha" not in err and "1 checked" in err
    # a version that moved since the build
    write(tmp_path, text.replace("echo 2.0", "echo 2.1"))
    code, _, err = run_main(["check", "--source", src, "--manifest", str(manifest)], capsys)
    assert code == 1 and "FAIL Beta: the manifest records" in err and "'2.1'" in err
    # a manifest without a tool the source has for this image (here: Magma's, with --have)
    write(tmp_path, text)
    code, _, err = run_main(
        ["check", "--source", src, "--manifest", str(manifest), "--have", "magma"], capsys
    )
    assert code == 1 and "lacks ['Opt']" in err
    # a broken manifest
    manifest.write_text("{")
    code, _, err = run_main(["check", "--source", src, "--manifest", str(manifest)], capsys)
    assert code == 1 and "FAIL" in err


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_remote_tool_is_tried_three_times_and_may_move(tmp_path, capsys, monkeypatch):
    """Review on #480: the LMFDB server is off the VM. Its check is retried (a blink doesn't
    fail the build, an outage does), and `check` reports a moved server version, no FAIL."""
    monkeypatch.setattr(tm, "REMOTE_TRIES", (3, 0))
    count = tmp_path / "count"
    flaky = f"echo x >> {count}; test $(wc -l < {count}) -ge 3 && echo 42"
    text = source_text(tool("Server", check=flaky, remote=True))
    src = write(tmp_path, text)
    code, out, err = run_main(["build", "--source", src, "--image", "i"], capsys)
    assert code == 0 and "ok   Server 1.2.3" in err, err
    assert count.read_text().count("x") == 3
    manifest = tmp_path / "tools.json"
    manifest.write_text(out)
    write(tmp_path, text.replace("echo 1.2.3", "echo 1.3.0"))
    code, _, err = run_main(["check", "--source", src, "--manifest", str(manifest)], capsys)
    assert code == 0 and "ok   Server 1.3.0" in err and "the build recorded '1.2.3'" in err, err
    # a recorded version that isn't one (only a hand edit makes it) fails (round 2 on #480)
    broken = json.loads(out)
    broken["tools"][0]["version"] = ""
    manifest.write_text(json.dumps(broken))
    code, _, err = run_main(["check", "--source", src, "--manifest", str(manifest)], capsys)
    assert code == 1 and "FAIL Server: the manifest records" in err
    manifest.write_text(out)
    # down for all three: the build fails, nothing is written
    write(tmp_path, source_text(tool("Server", check="false", remote=True)))
    code, out, err = run_main(["build", "--source", src, "--image", "i"], capsys)
    assert code == 1 and out == "" and "(attempt 3 of 3)" in err
    # not remote: a moved version still fails `check` (the existing test), and no retry
    count.unlink()
    write(tmp_path, source_text(tool("Local", check=flaky)))
    code, _, err = run_main(["build", "--source", src, "--image", "i"], capsys)
    assert code == 1 and count.read_text().count("x") == 1


@pytest.mark.parametrize(
    ("recorded", "message"),
    [
        ("[]", "not an object with a list of tool objects"),
        ('{"tools": 5}', "not an object with a list of tool objects"),
        ('{"tools": ["x"]}', "not an object with a list of tool objects"),
        (
            '{"schema": 1, "image": "i", "built": "2026-10-19", "tools": ['
            '{"name": "A"}, {"name": "A"}]}',
            "lists a tool twice",
        ),
    ],
)
def test_check_refuses_a_malformed_manifest_cleanly(tmp_path, capsys, recorded, message):
    """Review on #480: valid JSON of the wrong shape, or a tool listed twice, is a FAIL line,
    not a traceback (and not a pass)."""
    manifest = tmp_path / "tools.json"
    manifest.write_text(recorded)
    src = write(tmp_path, source_text(tool("A")))
    code, _, err = run_main(["check", "--source", src, "--manifest", str(manifest)], capsys)
    assert code == 1 and message in err and "Traceback" not in err


def test_lint_and_unknown_components(tmp_path, capsys):
    code, _, err = run_main(["lint", "--source", str(SOURCE)], capsys)
    assert code == 0 and f"in {len(AREAS)} areas" in err
    bad = write(tmp_path, source_text(tool("A", area="Elsewhere")))
    code, _, err = run_main(["lint", "--source", bad], capsys)
    assert code == 1 and "is not in areas" in err
    with pytest.raises(SystemExit, match="unknown components"):
        tm.main(["build", "--source", str(SOURCE), "--image", "i", "--have", "maple"])


# --------------------------------------------------------------------- the build and smoke


def test_build_sh_writes_the_manifest_last_from_the_checked_tools():
    """AC 2: build.sh runs the renderer as agent with the services' PATH, after the other smoke
    checks and before the smoke verdict, and installs /etc/zab/tools.json 0644 only on success."""
    text = BUILD_SH.read_text()
    smoke = text.split("smoke test (workshop/ami/README.md)", 1)[1].split("housekeeping, last", 1)[
        0
    ]
    step = smoke[smoke.index("The tools manifest") : smoke.index('if [ "$fail" -ne 0 ]')]
    assert 'tools_manifest.py" build' in step and '--source "$FILES/tools.toml"' in step
    assert '--image "$IMAGE_NAME" --have "$have"' in step
    assert 'as_agent env -i HOME="$AGENT_HOME"' in step and 'PATH="$SVC_PATH"' in step
    assert "sudo chmod 0644 /etc/zab/tools.json" in step and "fail=1" in step
    assert "have=magma" in step and '"${WITH_SAGE:-0}" = "1"' in step
    assert smoke.index("OSCAR_SMOKE") < smoke.index("The tools manifest")
    assert 'IMAGE_NAME="${IMAGE_NAME:-unnamed}"' in text


def test_build_ami_passes_the_name_and_smoke_checks_every_tool_again():
    sys.path.insert(0, str(AMI))
    try:
        import build_ami as ba
    finally:
        sys.path.remove(str(AMI))
    opts = ba.argparse.Namespace(
        codex_version="c", claude_version="k", zab_tag="t", pari_version="p", with_sage=False,
        name="zab-fleet-20261019",
    )  # fmt: skip
    assert ba.build_knobs(opts, {})["IMAGE_NAME"] == "zab-fleet-20261019"
    smoke = ba.SMOKE_SCRIPT
    assert "tools_manifest.py check --source $w/tools.toml" in smoke
    assert "--manifest /etc/zab/tools.json" in smoke and 'PATH="$svc"' in smoke
    assert 'if [ "$MAGMA" = 1 ]; then have=magma; fi' in smoke
    assert smoke.index("tools_manifest.py check") < smoke.index('echo "SMOKE-RESULT $fail"')
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", "-c", smoke], check=True)  # noqa: S603, S607


# ----------------------------------------------------------------- review round 1 (#480)


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_the_sage_wrapper_puts_the_environment_first_on_path(tmp_path):
    """Review on #480: conda's `sage` launcher leaves the environment off PATH, so Sage's
    giac(), r() and gap() fail; ~/.local/bin/sage is a wrapper doing what `conda activate`
    does. Here the "environment" is a stub whose `sage` prints its PATH and arguments."""
    wrapper = FILES / "bin" / "sage"
    assert wrapper.stat().st_mode & 0o111
    env = tmp_path / "envs" / "sage"
    (env / "bin").mkdir(parents=True)
    stub = env / "bin" / "sage"
    stub.write_text('#!/bin/sh\necho "$PATH"\necho "$@"\n')
    stub.chmod(0o755)
    out = subprocess.run(  # noqa: S603
        [str(wrapper), "-c", "print(1)"],
        env={"SAGE_ENV": str(env), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert out == [f"{env}/bin:/usr/bin:/bin", "-c print(1)"]
    assert 'SAGE_ENV="${SAGE_ENV:-/home/agent/miniforge/envs/sage}"' in wrapper.read_text()
    text = BUILD_SH.read_text()
    assert 'as_agent install -m 0755 "$FILES/bin/sage" "$AGENT_HOME/.local/bin/sage"' in text
    assert "ln -sf ~/miniforge/envs/sage/bin/sage" not in text
    sage = {t.name: t for t in tm.load_source(str(SOURCE)).tools}["SageMath"]
    for interface in ("giac(", "gap(", "r("):  # the summary's claims, checked
        assert interface in sage.check, interface


def test_review_480_fixes_in_the_source_and_the_docs():
    tools = {t.name: t for t in tm.load_source(str(SOURCE)).tools}
    # Jupyter's check runs its call
    assert "jupyter nbconvert --to notebook --execute nb.ipynb" in tools["Jupyter"].check
    assert tools["Jupyter"].version == "jupyter nbconvert --version"
    # LMFDB: remote, and the SSE lines are printed once
    assert tools["LMFDB"].remote and "remote" in tools["LMFDB"].summary
    assert "sed -n 's/^data: //p; t; /^{/p'" in tools["LMFDB"].check
    assert [t.name for t in tools.values() if t.remote] == ["LMFDB"]
    # comparator's COMMIT is written only after a build whose suite passed
    text = BUILD_SH.read_text()
    block = text[text.index('if ! [ -x "$LIB/comparator" ]') :]
    block = block[: block.index("\nfi\n")]
    assert 'echo "$COMPARATOR_COMMIT" | sudo tee "$LIB/COMMIT"' in block
    assert '[ "$(cat "$LIB/COMMIT" 2>/dev/null)" != "$COMPARATOR_COMMIT" ]' in block
    assert block.index("All tests passed") < block.index('sudo tee "$LIB/COMMIT"')
    # the README's one-tool check runs with the services' PATH, not sudo's
    readme = (AMI / "README.md").read_text()
    assert "sudo -u agent -H env -i HOME=/home/agent" in readme
    assert "PATH=/home/agent/venv/bin:/home/agent/.elan/bin:/home/agent/.local/bin:" in readme


#: Tools that wait at their prompt after a script unless stdin is closed (or the script quits):
#: a Magma run from an agent's background shell sat there for 14 to 26 minutes (Oct 18).
WAITS_ON_STDIN = {"PARI/GP", "GAP", "Magma", "Singular"}


def test_every_call_is_a_command_to_paste_and_exits_by_itself():
    """#478's review: `!tools` shows `call` as a command, so it is one, with no prose in
    parentheses; the Magma case: a REPL-style tool's call closes stdin."""
    tools = {t.name: t for t in tm.load_source(str(SOURCE)).tools}
    for t in tools.values():
        assert not re.search(r"\s{2,}\(|\((or|also|import|in a|Debian)\b", t.call), (t.name, t.call)
        assert "…" not in t.call and not t.call.startswith("the "), (t.name, t.call)
    for name in WAITS_ON_STDIN & set(tools):
        assert tools[name].call.endswith(" </dev/null"), name
    assert "quit;" in tools["Magma"].summary and "waits" in tools["Magma"].summary
    # review round 2 on #481: atlas reads a file argument silently, as a prelude, and resolves
    # a relative name in its library first; on stdin the script runs, and EOF ends it
    if "atlas" in tools:
        assert tools["atlas"].call == "atlas < file.at"
        assert "absolute path" in tools["atlas"].summary
    # review round 2 on #480: pdflatex stops at its `?` prompt on an error and waits
    assert "-interaction=nonstopmode -halt-on-error" in tools["TeX Live"].call
