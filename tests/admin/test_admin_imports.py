"""The import closure of `zab.admin` stays small (docs/ADMIN.md §1.6).

The admin server runs only what `release-paths` lists (ADMIN §1.6): `zab.admin` and the few
`zab` modules it imports, never the bridge. This test fails when that
closure gains a `zab` module outside `zab.admin` that is not on ALLOWED, or a third-party
package not on THIRD_PARTY. Adding one is a deliberate edit here, in the reviewed diff.

The closure is computed statically, from the source tree that the release's `.pth` file points
at (§1.6). It follows every import statement, those inside functions too, and refuses the
names that import by a computed name or run code built from a string (`import_module`,
`__import__`, `resolve_name`, `locate`, `exec`, `eval`, `compile`, …), imported under another
name too, which no static
closure could follow. `getattr(builtins, "__imp" + "ort__")` is beyond any static check: the
review of the signed diff covers it (§1.6). A second check imports every `zab.admin` module in
a fresh interpreter and compares `sys.modules`.
"""

import ast
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

#: `zab` modules outside `zab.admin` that the admin server may import. Every one of them is part
#: of every release's reviewed diff (§1.6, D31).
ALLOWED = frozenset(
    {"zab"}
    # zab.admin.scrub: the SPEC §11.6 patterns and mention disarming, shared with the bridge's
    # renderer. Each imports only `re`; `zab.render` is their package (#319).
    | {"zab.render", "zab.render.redact", "zab.render.mentions"}
    # zab.admin.bot reuses the bridge's Zulip transport (#341): the client, its request budget
    # and the two stdlib-only helpers they import (the metrics counters, `waiting`). Both
    # packages' __init__ files are empty.
    | {"zab.zulip", "zab.zulip.client", "zab.zulip.budget"}
    | {"zab.core", "zab.core.metrics", "zab.core.watchdog"}
)
#: Third-party top-level packages it may import (the `admin` dependency group, §1.6): the
#: starter's EC2 client (`zab.admin.starter.sdk`, PR 21), imported inside a function, and
#: `httpx`, the Zulip client's (#341), a dependency of zab itself.
THIRD_PARTY: frozenset[str] = frozenset({"boto3", "botocore", "httpx"})
#: Names that import a module named at run time, or run code built from a string. `compile`
#: counts as a bare name or an imported one, not as an attribute: `re.compile` is everywhere.
DYNAMIC = frozenset(
    {"__import__", "import_module", "run_module", "run_path", "spec_from_file_location"}
    | {"exec_module", "load_module", "find_spec", "find_module", "exec", "eval", "compile"}
    | {"resolve_name", "locate"}  # pkgutil.resolve_name, pydoc.locate
)


def module_file(src: Path, name: str) -> Path | None:
    base = src.joinpath(*name.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def package_modules(src: Path, package: str) -> list[str]:
    root = src.joinpath(*package.split("."))
    names = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(src).with_suffix("").parts
        names.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


def with_parents(name: str) -> list[str]:
    """`zab.render.redact` → `zab`, `zab.render`, `zab.render.redact`: importing a module runs
    its parent packages first."""
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]


def closure(
    src: Path, package: str, *, start: list[str] | None = None
) -> tuple[set[str], set[str], list[str]]:
    """The `zab` modules reachable from every module of `package`, or from the modules `start`
    names (parent packages included, since importing a module runs them), the third-party
    top-level packages they import, and every dynamic-import call among them."""
    todo, seen, third, dynamic = [], set[str](), set[str](), []
    for name in package_modules(src, package) if start is None else start:
        todo += with_parents(name)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        path = module_file(src, name)
        assert path is not None, f"{name} is imported but has no source file"
        is_package = path.name == "__init__.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = name.split(".")[: len(name.split(".")) - node.level + is_package]
                    base = ".".join(parts + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                targets = [base] + [
                    f"{base}.{alias.name}"
                    for alias in node.names
                    if module_file(src, f"{base}.{alias.name}")
                ]
            elif isinstance(node, ast.Name):
                if node.id in DYNAMIC:
                    dynamic.append(f"{name}:{node.lineno} {node.id}")
            elif isinstance(node, ast.Attribute):
                if node.attr in DYNAMIC - {"compile"}:
                    dynamic.append(f"{name}:{node.lineno} {node.attr}")
            elif isinstance(node, ast.alias):  # from importlib import import_module as im
                if node.name.rsplit(".", 1)[-1] in DYNAMIC:
                    dynamic.append(f"{name}:{node.lineno} {node.name}")
            for target in targets:
                top = target.split(".")[0]
                if top == "zab":
                    todo += with_parents(target)
                elif top not in sys.stdlib_module_names:
                    third.add(top)
    return seen, third, dynamic


def outside(modules: set[str]) -> set[str]:
    return {m for m in modules if m != "zab.admin" and not m.startswith("zab.admin.")}


def unlisted(modules: set[str], allowed: frozenset[str]) -> tuple[set[str], set[str]]:
    """The modules outside `zab.admin` that the closure reaches and `allowed` lacks, and the
    stale entries of `allowed` that it no longer reaches: the list must say exactly what runs."""
    reached = outside(modules)
    return reached - allowed, allowed - reached


def test_the_admin_closure_is_allowlisted():
    modules, third, dynamic = closure(SRC, "zab.admin")
    assert "zab.admin.grammar" in modules
    assert unlisted(modules, ALLOWED) == (set(), set()), (
        "the import closure of zab.admin changed: a module the admin server would run must be "
        "added to ALLOWED (docs/ADMIN.md §1.6), and one it no longer runs removed"
    )
    assert third <= THIRD_PARTY, f"third-party imports not in THIRD_PARTY: {third - THIRD_PARTY}"
    assert dynamic == [], f"dynamic imports defeat the closure check: {dynamic}"


def test_importing_the_admin_package_loads_nothing_else():
    """A fresh interpreter imports every module of `zab.admin`: `sys.modules` agrees."""
    code = (
        "import importlib, json, sys\n"
        f"for name in {package_modules(SRC, 'zab.admin')!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] == 'zab')))\n"
    )
    # Our own interpreter and code, as an argument list.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert outside(set(json.loads(result.stdout))) <= ALLOWED


#: What root imports in `zab-admin-config.service` (ADMIN §1.6, #437): the helper, the two
#: modules it uses, and their packages. The standard library besides, nothing third-party, and no
#: `site` (`-S`: no `.pth` line of the venv runs). A module joining this set is a decision: what
#: it imports at import time runs as root.
HELPER_IMPORTS = frozenset(
    {"zab", "zab.admin", "zab.admin.checks", "zab.admin.config", "zab.admin.confighelper"}
)
#: What root imports in the upgrade helpers' units (#569): the config helper's set, whose `take`
#: they use, and their own module.
UPGRADE_IMPORTS = HELPER_IMPORTS | {"zab.admin.upgradehelper"}
UNITS = SRC.parent / "workshop" / "adminserver"
HELPER_UNIT = UNITS / "zab-admin-config.service"
#: Each root helper's unit → its module, its imports and its program's name (`--help`).
UPGRADE = ("zab.admin.upgradehelper", UPGRADE_IMPORTS, "zab-admin-upgrade")
HELPERS = {
    "zab-admin-config.service": ("zab.admin.confighelper", HELPER_IMPORTS, "zab-admin-config"),
    "zab-admin-upgrade-plan.service": UPGRADE,
    "zab-admin-upgrade.service": UPGRADE,
}
CURRENT = "/opt/zab-admin/current"
#: The unit's code (argv[1]) run as `-c` runs it; at exit, what was imported, by name and file.
HELPER_PROBE = """
import atexit, sys

code = sys.argv.pop(1)


def report():
    found = {name: getattr(m, "__file__", None) for name, m in list(sys.modules.items())}
    import json, sysconfig  # after the record

    stdlib = [sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib")]
    flags = [sys.flags.isolated, sys.flags.no_site, sys.flags.dont_write_bytecode]
    names = sorted(sys.stdlib_module_names)
    report = {"modules": found, "path": sys.path, "stdlib": stdlib, "names": names}
    print(json.dumps(report | {"flags": flags}))


atexit.register(report)
exec(compile(code, "ExecStart", "exec"), {"__name__": "__main__"})
"""


@pytest.mark.parametrize("unit", sorted(HELPERS))
def test_the_helpers_closure_is_its_own(unit):
    """Statically, the imports inside functions included: from each root helper, its imports
    (HELPERS), no third-party package and no dynamic import."""
    module, imports, _ = HELPERS[unit]
    modules, third, dynamic = closure(SRC, "zab.admin", start=[module])
    assert (modules, third, dynamic) == (imports, set(), [])


@pytest.mark.parametrize("unit", sorted(HELPERS))
def test_the_helpers_command_imports_only_its_closure(unit):
    """The unit's own command, with `current` mapped to this checkout and `--help` added, run
    by its own interpreter under its flags: it answers, and imported its HELPERS set from src/,
    the standard library from the stdlib's directories, and nothing else; no `site`, so no
    `.pth`; and its path is the stdlib's and src/ alone. A third-party import at import time
    fails here, since -S leaves every site-packages off the path. The interpreter is the
    system's, never the venv's, which zab-build builds while third-party code runs, and whose
    pyvenv.cfg would choose the stdlib (review of #438)."""
    module, imports, program = HELPERS[unit]
    text = (UNITS / unit).read_text()
    (line,) = [x for x in text.splitlines() if x.startswith("ExecStart=")]
    python, *flags, dash_c, code = shlex.split(line.removeprefix("ExecStart="))
    assert (python, flags, dash_c) == ("/usr/bin/python3", ["-I", "-B", "-S"], "-c")
    assert f"from {module} import main" in code
    root = SRC.parent
    code = code.replace(CURRENT, str(root))
    code = re.sub(r"main\(\[[^]]*\]\)", "main()", code)  # its action's argument: `--help` here
    argv = [python, *flags, "-c", HELPER_PROBE, code]
    done = subprocess.run(  # noqa: S603
        [*argv, "--help"], capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, done.stderr
    usage, report = done.stdout.rsplit("\n", 2)[0], json.loads(done.stdout.splitlines()[-1])
    assert usage.startswith(f"usage: {program}")
    assert report["flags"] == [1, 1, 1]
    modules = {k: v for k, v in report["modules"].items() if k != "__main__"}
    assert "site" not in modules and "sitecustomize" not in modules
    ours = {name for name in modules if name.split(".")[0] == "zab"}
    assert ours == imports
    assert all(Path(modules[name]).is_relative_to(SRC) for name in ours)
    others = {name: file for name, file in modules.items() if name not in ours}
    third = {name for name in others if name.split(".")[0] not in report["names"]}
    assert third == set(), f"not the standard library's: {third}"
    stray = {n: f for n, f in others.items() if f and not f.startswith(tuple(report["stdlib"]))}
    assert stray == {}, f"outside the stdlib's directories: {stray}"
    *stdlib, last = report["path"]
    assert last == str(SRC)
    lib = tuple(str(Path(p).parent) for p in report["stdlib"])  # its zip lies beside it
    assert not [p for p in stdlib if "-packages" in p or not p.startswith(lib)], stdlib


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, source in files.items():
        path = tmp_path.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return tmp_path


def test_the_check_sees_what_it_must(tmp_path):
    """On a made-up tree: an import inside a function, a relative import, a module reached
    through a parent package, a third-party package and dynamic imports all count, the last
    ones also under another name or through `exec`; `re.compile` does not."""
    src = _tree(
        tmp_path,
        {
            "zab/__init__.py": "",
            "zab/admin/__init__.py": "from . import bot\n",
            "zab/admin/bot.py": "def run():\n    from zab.core import bridge\n",
            "zab/admin/scrub.py": "from ..render.redact import scrub\nimport httpx\n",
            "zab/admin/later.py": "import importlib\nimportlib.import_module('zab.core.bridge')\n",
            "zab/admin/alias.py": "from importlib import import_module as im\nim('zab.core')\n",
            "zab/admin/ex.py": "def f():\n    exec('import zab.core.bridge')\n",
            "zab/admin/ev.py": "x = eval('1')\nc = compile('1', 'x', 'eval')\n",
            "zab/admin/res.py": "import pkgutil\n\nm = pkgutil.resolve_name('zab.core')\n",
            "zab/admin/loc.py": "from pydoc import locate\n\nm = locate('zab.core.bridge')\n",
            "zab/core/__init__.py": "",
            "zab/core/bridge.py": "import websockets\n",
            "zab/render/__init__.py": "",
            "zab/render/redact.py": "import re\n\nPATTERN = re.compile('x')\n",
        },
    )
    modules, third, dynamic = closure(src, "zab.admin")
    assert outside(modules) == {"zab", "zab.core", "zab.core.bridge", "zab.render"} | {
        "zab.render.redact"
    }
    assert third == {"httpx", "websockets"}
    assert sorted(dynamic) == [
        "zab.admin.alias:1 import_module",
        "zab.admin.ev:1 eval",
        "zab.admin.ev:2 compile",
        "zab.admin.ex:2 exec",
        "zab.admin.later:2 import_module",
        "zab.admin.loc:1 locate",
        "zab.admin.loc:3 locate",
        "zab.admin.res:3 resolve_name",
    ]
    reached = {"zab", "zab.core", "zab.core.bridge", "zab.render", "zab.render.redact"}
    assert unlisted(modules, frozenset(reached)) == (set(), set())
    assert unlisted(modules, frozenset(reached | {"zab.config"})) == (set(), {"zab.config"})
    assert unlisted(modules, frozenset(reached - {"zab.core"})) == ({"zab.core"}, set())
