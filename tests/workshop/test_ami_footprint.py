"""The image's first-day footprint (#997; workshop/ami/README.md, "The image's footprint"):
build.sh's documentation filter, its cache steps and the smoke test's offline `lake build`,
checked offline against build.sh and build_ami.py."""

from __future__ import annotations

import fnmatch
import pathlib
import re
import shutil
import subprocess

import build_ami as ba
import pytest

BUILD_SH = pathlib.Path(__file__).resolve().parents[2] / "workshop" / "ami" / "build.sh"


def dpkg_filter() -> list[tuple[str, str]]:
    """The filter's rules, in order: (`path-exclude` or `path-include`, glob)."""
    text = BUILD_SH.read_text()
    block = text[text.index("printf '%s\\n' '# zab workshop image: no package documentation") :]
    block = block[: block.index("| sudo tee /etc/dpkg/dpkg.cfg.d/zab-no-doc")]
    rules = re.findall(r"'(path-(?:ex|in)clude=[^']+)'", block)
    return [tuple(rule.split("=", 1)) for rule in rules]


def kept(path: str) -> bool:
    """dpkg's verdict for `path`: the last rule that matches decides, and `*` matches `/` too
    (fnmatch(3) without FNM_PATHNAME, as dpkg calls it; dpkg(1), `--path-exclude`)."""
    verdict = True
    for kind, glob in dpkg_filter():
        if fnmatch.fnmatchcase(path, glob):
            verdict = kind == "path-include"
    return verdict


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/usr/share/doc/git/README.md", False),
        ("/usr/share/doc/texlive-doc/latex/koma-script/scrguien.pdf", False),  # what texdoc shows
        ("/usr/share/doc/r-cran-ggplot2/tests/testthat.R", False),
        ("/usr/share/doc/git/copyright", True),
        ("/usr/share/doc/gap-doc/copyright", True),
        # macaulay2's postinst builds `help`'s databases from these dumps
        ("/usr/share/doc/Macaulay2/Macaulay2Doc/dump/rawdocumentation.dump", True),
        ("/usr/share/doc/Macaulay2/Core/html/index.html", True),
        ("/usr/share/man/man1/git.1.gz", True),  # man pages and info files are elsewhere
        ("/usr/share/info/Macaulay2Doc.info.gz", True),
        ("/usr/share/gap/doc/ref/manual.six", True),  # GAP's help reads its own tree
        ("/usr/share/texlive/texmf-dist/tex/latex/amsmath/amsmath.sty", True),
    ],
)
def test_dpkg_leaves_out_the_documentation_but_copyright_and_macaulay2(path, expected):
    assert kept(path) is expected, path


def test_the_filter_comes_before_the_first_install_and_macaulay2s_help_is_checked():
    """It applies to every package the build installs: it is written before step 1's first
    `$APT install`. After step 1d's, the help database that macaulay2's postinst builds from
    /usr/share/doc/Macaulay2 must exist, or the build stops (the postinst says nothing)."""
    text = BUILD_SH.read_text()
    assert [k for k, _ in dpkg_filter()] == ["path-exclude", "path-include", "path-include"]
    written = text.index("sudo tee /etc/dpkg/dpkg.cfg.d/zab-no-doc")
    assert written < text.index("$APT install")
    m2 = text.index('$APT install -y --no-install-recommends "${m2_install[@]/#/$m2/}"')
    db = "/usr/lib/x86_64-linux-gnu/Macaulay2/Macaulay2Doc/cache/rawdocumentation-dcba-8.db"
    db = f"sudo test -s {db}"
    assert m2 < text.index(db) < text.index("# --- 2. the participant's user")


def test_the_caches_go_before_the_smoke_test_each_measured():
    """conda's whole package cache, apt's, and Mathlib's downloaded archives in a step of its
    own, all before the smoke test, which then checks every tool without them; each one's
    saving goes to the build's log. A failing conda clean stops the build (set -e)."""
    text = BUILD_SH.read_text()
    caches = text.index("# --- 12b. package caches (#997")
    archives = text.index("# --- 12c. Mathlib's downloaded archives (#997)")
    smoke = text.index("# --- 13. smoke test")
    assert text.index("# --- 12. instructions") < caches < archives < smoke
    step = text[caches:archives]
    assert 'conda" clean -afy -q >/dev/null\n' in step  # no `|| true`
    assert step.count('freed "$before"') == 2 and "$APT clean" in step
    step = text[archives:smoke]
    assert 'as_agent rm -rf "$AGENT_HOME/.cache/mathlib"' in step
    assert 'freed "$before" "Mathlib' in step
    assert "clean -a -y" not in text  # step 14's quiet conda clean is gone


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("into_cache", [False, True], ids=["cache's own links", "env into cache"])
def test_the_conda_clean_stops_the_build_only_on_a_link_it_left_dangling(tmp_path, into_cache):
    """Review round 1 of #1006: step 12b run against a fake ~/miniforge whose `conda clean -afy`
    removes `pkgs/`. The cache's extracted packages hold links that dangle by design, as
    libblas' to libopenblas' library, resolved once both are linked into an environment: they
    go with the cache and pass. An environment's symlink into the cache, left dangling by the
    clean, stops the build and is named."""
    text = BUILD_SH.read_text()
    step = text[text.index("# --- 12b. package caches") : text.index("# --- 12c. Mathlib")]
    forge = tmp_path / "home" / "miniforge"
    for d in ("bin", "pkgs/libblas-3.11.0/lib", "pkgs/libopenblas-0.3.34/lib", "envs/sage/lib"):
        (forge / d).mkdir(parents=True)
    (forge / "bin" / "conda").write_text('#!/bin/sh\nrm -rf "$(dirname "$0")/../pkgs"\n')
    (forge / "bin" / "conda").chmod(0o755)
    so = "libopenblasp-r0.3.34.so"
    (forge / "pkgs/libopenblas-0.3.34/lib" / so).write_text("so")
    (forge / "pkgs/libblas-3.11.0/lib/libblas.so").symlink_to(so)  # dangles in the cache
    (forge / "envs/sage/lib" / so).write_text("so")  # a hard link or a copy, in conda's terms
    (forge / "envs/sage/lib/libblas.so").symlink_to(so)
    if into_cache:
        (forge / "envs/sage/lib/libsoft.so").symlink_to(
            f"../../../pkgs/libopenblas-0.3.34/lib/{so}"
        )
    stubs = (
        'set -euo pipefail\nlog() { :; }\nas_agent() { "$@"; }\nAPT=true\n'
        'sudo() { if [ "$1" = du ]; then echo 0; else "$@"; fi; }\n'
        f"AGENT_HOME={tmp_path / 'home'}\n"
    )
    run = subprocess.run(["bash", "-c", stubs + step], capture_output=True, text=True)  # noqa: S603, S607
    assert not (forge / "pkgs").exists()
    if into_cache:
        assert run.returncode == 1, run.stderr
        assert "conda clean -f left links dangling" in run.stderr
        assert run.stderr.rstrip().endswith(str(forge / "envs/sage/lib/libsoft.so"))
    else:
        assert run.returncode == 0, run.stderr
        assert "freed" in run.stdout and "conda's package cache" in run.stdout


def fake_lean(tmp_path: pathlib.Path, lake: str) -> dict[str, str]:
    """A HOME, a shared Mathlib and stubs for `lean-project` (a new project linked to it) and
    `lake` (`lake`'s body), on PATH before the system's."""
    home, shared, bin_ = tmp_path / "home", tmp_path / "shared", tmp_path / "bin"
    for d in (home, shared, bin_):
        d.mkdir()
    (shared / "mathlib").mkdir()
    (bin_ / "lean-project").write_text(
        f'#!/bin/sh\nmkdir -p "$1/.lake" && ln -s {shared} "$1/.lake/packages"\n'
    )
    (bin_ / "lake").write_text(f"#!/bin/sh\n{lake}\n")
    for stub in bin_.iterdir():
        stub.chmod(0o755)
    (tmp_path / "tmp").mkdir()
    return {"HOME": str(home), "PATH": f"{bin_}:/usr/bin:/bin", "TMPDIR": str(tmp_path / "tmp")}


def offline_builds() -> dict[str, str]:
    """The smoke checks' scripts: build.sh's `LAKE_FRESH`, and build_ami.py's `fresh`, which
    allows an older image's ~/.cache/mathlib if it stays as it was."""
    text = BUILD_SH.read_text()
    lake_fresh = text.split("LAKE_FRESH='", 1)[1].split("'\n", 1)[0]
    lines = [ln for ln in ba.SMOKE_SCRIPT.splitlines() if ln.startswith("fresh=")]
    return {"build.sh": lake_fresh, "build_ami.py": "\n".join(lines) + '\nbash -c "$fresh"'}


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("where", ["build.sh", "build_ami.py"])
@pytest.mark.parametrize(
    ("lake", "had_cache", "ok"),
    [
        ("exit 0", False, True),
        ("mkdir -p ~/.cache/mathlib", False, False),  # something ran Mathlib's cache
        ("exit 1", False, False),  # offline, a download fails the build
        ("exit 0", True, None),  # an image from before #997: build_ami.py's passes
    ],
)
def test_the_offline_lake_build_check(tmp_path, where, lake, had_cache, ok):
    """Criterion 3 of #997: the smoke checks pass only when the fresh project's `lake build`
    succeeds and ~/.cache/mathlib didn't appear; build.sh's (its own image, step 12c deleted the
    archives) wants none at all. The scratch project goes at the exit, passed or not (review
    round 1 of #1006), its link before it, so the cleanup never reaches the shared Mathlib."""
    env = fake_lean(tmp_path, lake)
    if had_cache:
        (pathlib.Path(env["HOME"]) / ".cache" / "mathlib").mkdir(parents=True)
    script = offline_builds()[where]
    run = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)  # noqa: S603, S607
    expected = ok if ok is not None else where == "build_ami.py"
    assert (run.returncode == 0) is expected, run.stderr
    assert (tmp_path / "shared" / "mathlib").is_dir()
    assert not any((tmp_path / "tmp").iterdir())


def test_the_offline_lake_build_runs_with_no_network_in_both_smoke_tests():
    """A network namespace of its own (`unshare --net`, as root, then agent): any fetch fails."""
    text = BUILD_SH.read_text()
    smoke = text[text.index("# --- 13. smoke test") : text.index("# --- 14. housekeeping")]
    assert 'check sudo unshare --net -- runuser -u "$AGENT" -- env -i HOME="$AGENT_HOME"' in smoke
    assert 'bash -c "$LAKE_FRESH"' in smoke
    script = ba.SMOKE_SCRIPT
    assert "check sudo unshare --net -- runuser -u agent -- env -i HOME=/home/agent" in script
    assert script.index('bash -c "$fresh"') < script.index("SMOKE-RESULT")
