"""The Python programs of `deploy.sh`'s laptop half, under this interpreter (#988).

deploy.sh runs them with the deployer's `python3`, whatever version their machine has (3.12 or
newer, BOOTSTRAP §1): the summaries of uv.lock and build-constraints.txt in every review
(`LOCK_PY`, `PINS_PY`) and `build_config`'s check of pyproject.toml before a `--wheel` build. The
deploy rig's tests (marked `deploy_rig`) run them inside deploy.sh, but a pull request runs those
under 3.12 alone, the admin server's Python. So here each program is taken from deploy.sh as it
stands and run as deploy.sh runs it (`python3 -I -c PROGRAM …`) under the interpreter of the run,
which CI's `test (3.x)` jobs make 3.12, 3.13 and 3.14. The `--wheel` build itself runs the pinned
hatchling, none of zab's code, and is left to the rig.
"""

import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEXT = (ROOT / "workshop" / "adminserver" / "deploy.sh").read_text()
PROGRAMS = dict(re.findall(r"^(LOCK_PY|PINS_PY)='(.*?)'$", TEXT, re.S | re.M))
(BUILD_CONFIG,) = re.findall(r"python3 -I -c '(import sys, tomllib\n.*?)' 2>&1\)", TEXT, re.S)


def run(program: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    argv = [sys.executable, "-I", "-c", program, *args]
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)  # noqa: S603


def summary(tmp_path: pathlib.Path, name: str, old: str, new: str) -> subprocess.CompletedProcess:
    (tmp_path / "old").write_text(old)
    (tmp_path / "new").write_text(new)
    return run(PROGRAMS[name], str(tmp_path / "old"), str(tmp_path / "new"))


def test_the_programs_are_found_in_deploy_sh():
    assert sorted(PROGRAMS) == ["LOCK_PY", "PINS_PY"]
    assert "def load(path)" in PROGRAMS["LOCK_PY"] and "def load(path)" in PROGRAMS["PINS_PY"]
    assert BUILD_CONFIG.rstrip().endswith('print(" ".join(bad))')


def wheel(name: str, digest: str, host: str = "https://files.pythonhosted.org") -> str:
    return f'{{ url = "{host}/{name}-py3-none-any.whl", hash = "sha256:{digest * 64}" }}'


OLD_LOCK = f"""\
version = 1
requires-python = ">=3.11"

[[package]]
name = "fakecli"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [{wheel("fakecli", "a")}]

[[package]]
name = "httpx"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [{wheel("httpx", "b")}]
"""
NEW_LOCK = f"""\
version = 1
requires-python = ">=3.12"

[[package]]
name = "fakecli"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
dependencies = [{{ name = "httpx", marker = "sys_platform == 'linux'" }}]
wheels = [{wheel("fakecli", "c")}]

[[package]]
name = "new\\nline"
version = "2.0"
source = {{ registry = "https://example.org/simple" }}
wheels = [{wheel("other", "d", "https://example.org")}]
"""


def test_the_lock_summary_shows_each_change_without_a_hash(tmp_path):
    """`LOCK_PY`: a lock-level field, a package at the same version with other files and a new
    dependency, one removed, one new from elsewhere, every string as JSON (D32)."""
    done = summary(tmp_path, "LOCK_PY", OLD_LOCK, NEW_LOCK)
    assert done.returncode == 0, done.stderr
    pypi = '"https://pypi.org/simple"'
    assert done.stdout.splitlines() == [
        '  (the lock) "requires-python": ">=3.11" -> ">=3.12"',
        f'  "fakecli" "1.0" -> "1.0"  {pypi}  ** SAME VERSION, OTHER FILES',
        '      "1.0" "dependencies": +{"marker": "sys_platform == \'linux\'", "name": "httpx"}',
        f'  "httpx" "1.0" -> (removed)  {pypi}',
        '  "new\\nline" (new) -> "2.0"  "https://example.org/simple"  ** NOT FROM PyPI'
        "  ** A FILE NOT ON files.pythonhosted.org",
    ]
    assert "sha256" not in done.stdout


def test_a_lock_the_summary_cant_read_fails_it(tmp_path):
    """deploy.sh then shows the raw diff in its place (`as_data`)."""
    assert summary(tmp_path, "LOCK_PY", OLD_LOCK, "[[package]\n").returncode != 0


def test_the_pins_summary_reads_every_hash_and_shows_the_rest_as_written(tmp_path):
    """`PINS_PY`: a hash on the pin's own line and after it, a requirement that isn't a pin. The
    hash on the pin's own line is the same in both: only the one after it changed."""
    old = f"fakebackend==1.0 --hash=sha256:{'b' * 64} \\\n    --hash=sha256:{'a' * 64}\n"
    new = (
        f"# pinned\nfakebackend==1.0 --hash=sha256:{'b' * 64} \\\n    --hash=sha256:{'c' * 64}\n"
        'other[cli]==2.0 ; python_version < "3.12"\n'
    )
    done = summary(tmp_path, "PINS_PY", old, new)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "  fakebackend 1.0 -> 1.0  ** SAME VERSION, OTHER HASHES",
        '  other[cli]==2.0 ; python_version < "3.12" (new) -> (as written)',
    ]


RIG_PYPROJECT = """\
[build-system]
requires = ["hatchling==1.0"]
build-backend = "hatchling.build"

[project]
name = "zulip-agent-bridge"
version = "0.3.1"
"""


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        ("", ""),
        ("\n[tool.uv]\nsources = {}\n", "tool.uv"),
        ('\n[tool.hatch.build.hooks.custom]\npath = "hook.py"\n', "tool.hatch"),
        ('dynamic = ["readme"]\n', "project.dynamic"),  # in [project], the table above
    ],
)
def test_the_build_config_check_names_what_would_steer_the_build(extra, said):
    done = run(BUILD_CONFIG, stdin=RIG_PYPROJECT + extra)
    assert (done.returncode, done.stdout.strip()) == (0, said), done.stderr


def test_the_build_config_check_passes_this_trees_pyproject_and_refuses_bad_toml():
    """The pyproject.toml a release ships passes (#436); one that isn't TOML stops the build."""
    done = run(BUILD_CONFIG, stdin=(ROOT / "pyproject.toml").read_text())
    assert (done.returncode, done.stdout.strip()) == (0, ""), done.stderr
    assert run(BUILD_CONFIG, stdin="[build-system\n").returncode != 0
