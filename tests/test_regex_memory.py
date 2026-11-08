"""No regular expression in `zab` or its admin scripts repeats a group greedily or lazily
without a small bound (#828, #833, #836).

`re` keeps a backtracking frame for each pass of a greedy or lazy repeat whose body is more
than one character, a group: from about 65 bytes per character of the text it runs over to 210,
so a megabyte of an agent's answer or a VM's output could take hundreds of megabytes. A repeat
of one character (`[a-z]*`, `.*?`) and a possessive repeat (`*+`, `++`) cost constant memory.

The check covers every pattern that `src/zab` and `workshop/admin` compile (`ROOTS`):

- each one compiled while all their modules are imported, whatever holds it then (a dict, a
  dataclass, a `partial`, a `MappingProxyType`, nothing at all): an interpreter of its own
  imports them with `re`'s compile watched (`compiled_at_import`); and each one reachable from
  a module's globals, under its path (`module_patterns`);
- each literal pattern of an `re` call anywhere in their source, positional or `pattern=`;
- a pattern built from other text in a function or a lambda, which runs later: each such call
  is listed in `BUILT_IN_FUNCTIONS`, read by hand.

The scan reads `re.<function>(pattern, …)` after `import re`. Any other way to reach those
functions (`from re import compile`, `import re as rx`, `map(re.compile, …)`, `getattr(re, …)`)
or another engine fails `test_re_is_used_as_the_scan_reads_it`. A pattern whose input is always
short is in `ALLOWED`, with the reason.
"""

from __future__ import annotations

import ast
import importlib
import json
import pkgutil
import re
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from re import _constants as sre  # the regex parser's opcodes, as tests/admin/test_scrub.py
from re import _parser as sre_parser
from typing import Any

import pytest

import zab

SRC = Path(zab.__file__).parent
WORKSHOP = Path(__file__).resolve().parents[1] / "workshop"
#: The code checked, by the prefix of its files' names here: the package, and the admin scripts,
#: which run on the organizers' machines and the admin server (#836).
ROOTS = {
    "": SRC,
    "workshop/admin/": WORKSHOP / "admin",
    "workshop/adminserver/": WORKSHOP / "adminserver",
}
#: The `re` functions that take a pattern, as the scan reads them: `re.<name>(pattern, …)`.
FUNCTIONS = frozenset(
    {"compile", "match", "search", "sub", "subn", "fullmatch", "findall", "finditer", "split"}
)
#: What compiles patterns without those functions, or reaches them by another name: modules,
#: and names of `re` (`re.Scanner` compiles with `_compiler` directly: review of #857, N1).
ENGINES = frozenset({"regex", "sre_compile", "sre_parse", "re._compiler", "re._parser"})
UNREAD = frozenset({"_compile", "_compiler", "_parser", "Scanner", "*"})
#: The longest repeat of a group allowed without a reason: its frames are bounded.
SMALL = 64

#: Patterns that repeat a group, with why their input is short.
ALLOWED = {
    r"(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)+\.pub": "a key file in admin.toml; possessive, it would "
    "keep `.pub` in the last segment and never match",
    r"^(?:\(\?<![^)]*\))+": "the source of the §11.6 rules, at import (`zab.admin.scrub`)",
}

#: `re` calls inside a function whose pattern is built from other text, by
#: (file, as `ROOTS` names it, function): what the pattern is, read by hand.
BUILT_IN_FUNCTIONS = {
    ("admin/actions.py", "normalize_dry_run"): "an escaped path, an optional fixed suffix",
    ("admin/actions.py", "_build_roster_import"): "an escaped path and fixed text",
    ("admin/config.py", "_config"): "`RELEASE_PATTERNS`, no group repeated",
    ("admin/scrub.py", "_class"): "one character class",
    ("admin/starter.py", "fleet_vm"): "an escaped region and a letter",
    ("backends/claude.py", "_rewrite_paths"): "an escaped path between two lookarounds",
    ("config.py", "fail"): "`.*?`, an escaped name, `\\s*=`",
    ("core/addressing.py", "mention"): "an escaped name and fixed text",
    ("core/addressing.py", "typed"): "an escaped name and fixed text",
    ("core/addressing.py", "typed_address"): "`typed`'s and a lookahead",
    ("core/addressing.py", "unaddressed"): "mentions, then a possessive `\\s|,|:(?!…)`",
    ("core/peer_context.py", "bot_address"): "escaped names in one alternation",
    ("core/processes.py", "leader_identity"): "an escaped session id and fixed text",
    ("dev/scrub.py", "default_rules"): "escaped literals",
    ("tools/setup_repo.py", "ensure_ssh_config"): "an escaped repository name and blanks",
}


def _single(item: Any) -> bool:
    """One character: what `re` repeats in constant memory (`_compiler._simple`)."""
    if len(item) != 1:
        return False
    op, av = item[0]
    if op is sre.SUBPATTERN:
        return av[0] is None and _single(av[-1])
    return op in (sre.LITERAL, sre.NOT_LITERAL, sre.IN, sre.ANY)


def repeats(parsed: Any) -> list[int]:
    """The upper bounds of the greedy or lazy repeats of a group in a parsed pattern."""
    found = []
    for op, av in parsed:
        if op in (sre.MAX_REPEAT, sre.MIN_REPEAT, sre.POSSESSIVE_REPEAT):
            _low, high, item = av
            if op is not sre.POSSESSIVE_REPEAT and not _single(item):
                found.append(high)
            found += repeats(item)
        elif op is sre.SUBPATTERN:
            found += repeats(av[-1])
        elif op is sre.BRANCH:
            for branch in av[1]:
                found += repeats(branch)
        elif op in (sre.ASSERT, sre.ASSERT_NOT):
            found += repeats(av[1])
        elif op is sre.ATOMIC_GROUP:
            found += repeats(av)
        elif op is sre.GROUPREF_EXISTS:  # (?(1)yes|no)
            found += repeats(av[1]) + (repeats(av[2]) if av[2] else [])
    return found


def risky(pattern: str | bytes, flags: int = 0) -> bool:
    return any(high > SMALL for high in repeats(sre_parser.parse(pattern, flags)))


def reachable(value: Any, path: str, depth: int = 0) -> list[tuple[str, re.Pattern[Any]]]:
    """Each pattern in `value`, with its path from `path`: `RULES[3][0]`, `TABLE['key']`. Each
    one apart, as a container holds several (codex round 1 on #834: one per name kept only the
    first of the ten §11.6 rules). A set is walked in a fixed order, as the test ids must be the
    same in every xdist worker."""
    if isinstance(value, re.Pattern):
        return [(path, value)]
    if depth > 3:
        return []
    if isinstance(value, dict):
        items = [(f"{path}[{k!r}]", v) for k, v in value.items()]
    elif isinstance(value, (tuple, list)):
        items = [(f"{path}[{i}]", v) for i, v in enumerate(value)]
    elif isinstance(value, (set, frozenset)):
        items = [(f"{path}{{{i}}}", v) for i, v in enumerate(sorted(value, key=repr))]
    else:
        return []
    return [found for where, v in items for found in reachable(v, where, depth + 1)]


def module_patterns() -> dict[str, re.Pattern[Any]]:
    """Each pattern reachable from a module's globals, or a class's, after importing all; in
    each module, one entry per pattern object, under the first path that reaches it."""
    found: dict[str, re.Pattern[Any]] = {}
    for info in pkgutil.walk_packages(zab.__path__, "zab."):
        if info.name.endswith("__main__"):
            continue  # its literal patterns are read from its source below
        module = importlib.import_module(info.name)
        seen: set[int] = set()
        for name, value in vars(module).items():
            values = [(f"{info.name}.{name}", value)]
            if isinstance(value, type) and value.__module__ == module.__name__:
                values = [(f"{info.name}.{name}.{k}", v) for k, v in vars(value).items()]
            for where, pattern in (f for w, v in values for f in reachable(v, w)):
                if id(pattern) not in seen:
                    seen.add(id(pattern))
                    found[where] = pattern
    return found


def _files(roots: dict[str, Path]) -> Iterator[tuple[str, ast.Module]]:
    for prefix, root in roots.items():
        for path in sorted(root.rglob("*.py")):
            yield prefix + path.relative_to(root).as_posix(), ast.parse(path.read_text(), str(path))


def _is_re(node: ast.AST) -> bool:
    """`re.<attribute>`."""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "re"
    )


def _pattern(call: ast.Call) -> ast.expr | None:
    """The pattern an `re` call is given: its first argument, or `pattern=` (#836)."""
    if call.args and not isinstance(call.args[0], ast.Starred):
        return call.args[0]
    return next((k.value for k in call.keywords if k.arg == "pattern"), None)


def _calls(roots: dict[str, Path] = ROOTS) -> list[tuple[str, str | None, ast.Call, ast.expr]]:
    """Every `re.<function>(pattern, …)` in the code checked: its file, the function or lambda
    it runs in (None: at import), the call and its pattern. A lambda outside a function runs
    later, as a function's body does (#836)."""
    out = []
    for file, tree in _files(roots):

        def visit(node: ast.AST, function: str | None, file: str = file) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = node.name
            elif isinstance(node, ast.Lambda) and function is None:
                function = "<lambda>"
            if (
                isinstance(node, ast.Call)
                and _is_re(node.func)
                and node.func.attr in FUNCTIONS
                and (pattern := _pattern(node)) is not None
            ):
                out.append((file, function, node, pattern))
            for child in ast.iter_child_nodes(node):
                visit(child, function)

        visit(tree, None)
    return out


def literal_patterns(roots: dict[str, Path] = ROOTS) -> dict[str, tuple[str | bytes, int]]:
    """Each literal pattern of an `re` call, with `re.X` if the call names it."""
    found = {}
    for file, _function, call, arg in _calls(roots):
        if isinstance(arg, ast.Constant) and isinstance(arg.value, (str, bytes)):
            verbose = any(
                isinstance(n, ast.Attribute) and n.attr in ("X", "VERBOSE") for n in ast.walk(call)
            )
            found[f"{file}:{call.lineno}"] = (arg.value, re.VERBOSE if verbose else 0)
    return found


def built_in_functions(roots: dict[str, Path] = ROOTS) -> set[tuple[str, str]]:
    """The (file, function) of each `re` call in a function or lambda whose pattern is built."""
    return {
        (file, function)
        for file, function, _call, arg in _calls(roots)
        if function is not None and not isinstance(arg, ast.Constant)
    }


def unsupported(roots: dict[str, Path] = ROOTS) -> list[str]:
    """Each way the scan can't read of reaching `re`'s functions, or another engine (#836)."""
    found = []
    for file, tree in _files(roots):
        held = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for node in ast.walk(tree):
            where = f"{file}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ENGINES or (
                        alias.name == "re" and alias.asname not in (None, "re")
                    ):
                        found.append(f"{where}: import {alias.name} as {alias.asname}")
            elif isinstance(node, ast.ImportFrom):
                names = {a.name for a in node.names}
                if node.module in ENGINES or (node.module == "re" and names & (FUNCTIONS | UNREAD)):
                    found.append(f"{where}: from {node.module} import {', '.join(sorted(names))}")
            elif isinstance(node, ast.Name) and node.id == "re" and id(node) not in held:
                found.append(f"{where}: `re` as a value")
            elif _is_re(node) and node.attr in UNREAD:
                found.append(f"{where}: `re.{node.attr}`")
            elif _is_re(node) and node.attr in FUNCTIONS and id(node) not in called:
                found.append(f"{where}: `re.{node.attr}` as a value")
            elif (
                isinstance(node, ast.Call)
                and _is_re(node.func)
                and node.func.attr in FUNCTIONS
                and _pattern(node) is None
            ):
                found.append(f"{where}: `re.{node.func.attr}` without its pattern as such")
    return found


#: Run by `compiled_at_import` in an interpreter of its own, with the roots as JSON: imports
#: every module of each (a package, or a directory of scripts) with `re._compile`, which every
#: `re` function calls, watched; prints each pattern compiled from a file under a root, where,
#: and with which flags, and each module that failed to import.
CAPTURE = r"""
import importlib, json, os, pkgutil, re, sys

roots = json.loads(sys.argv[1])
inside = os.path.dirname(os.path.realpath(re.__file__))
compile_, found, failed = re._compile, [], []


def where(frame):
    while frame is not None:
        path = os.path.realpath(frame.f_code.co_filename)
        if os.path.dirname(path) != inside:
            for prefix, root in roots.items():
                if path.startswith(root + os.sep):
                    name = os.path.relpath(path, root).replace(os.sep, "/")
                    return f"{prefix}{name}:{frame.f_lineno}"
            return None
        frame = frame.f_back
    return None


def watched(pattern, flags):
    if isinstance(pattern, (str, bytes)) and (at := where(sys._getframe(1))):
        found.append([at, repr(pattern), int(flags)])
    return compile_(pattern, flags)


re._compile = watched
for prefix, root in roots.items():
    if os.path.exists(os.path.join(root, "__init__.py")):
        sys.path.insert(0, os.path.dirname(root))
        top = os.path.basename(root)
        walked = pkgutil.walk_packages([root], top + ".")
        names = [top] + [i.name for i in walked if not i.name.endswith("__main__")]
    else:
        sys.path.insert(0, root)
        names = sorted(f[:-3] for f in os.listdir(root) if f.endswith(".py"))
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
print(json.dumps({"compiled": found, "failed": failed}))
"""


def unwatched(
    compiled: list[tuple[str, str | bytes, int]], roots: dict[str, Path] = ROOTS
) -> list[str]:
    """Each `re` call at a module's top level that the watch didn't see run, at its own file
    and lines (review of #857): the import didn't reach it (`if __name__ == "__main__":`, a
    branch not taken, a module not imported), or the watch of its root stopped. A `__main__`
    module never runs at import, so its patterns there must be literals, which the scan reads."""
    seen = {tuple(where.rsplit(":", 1)) for where, _pattern, _flags in compiled}
    return [
        f"{file}:{call.lineno}"
        for file, function, call, pattern in _calls(roots)
        if function is None
        and not (file.endswith("__main__.py") and isinstance(pattern, ast.Constant))
        and not any((file, str(n)) in seen for n in range(call.lineno, (call.end_lineno or 0) + 1))
    ]


def compiled_at_import(
    roots: dict[str, Path] = ROOTS,
) -> tuple[list[tuple[str, str | bytes, int]], list[str]]:
    """Each pattern compiled while every module of the roots is imported, in a fresh
    interpreter: where, the pattern and its flags; and the modules that failed to import."""
    given = json.dumps({prefix: str(root.resolve()) for prefix, root in roots.items()})
    run = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", CAPTURE, given], capture_output=True, text=True, timeout=300
    )
    assert run.returncode == 0, run.stderr[-3000:]
    out = json.loads(run.stdout.splitlines()[-1])  # the last line: a module may print
    return [(w, ast.literal_eval(p), f) for w, p, f in out["compiled"]], out["failed"]


MODULE_PATTERNS = module_patterns()
LITERAL_PATTERNS = literal_patterns()


@pytest.mark.parametrize("where", sorted(MODULE_PATTERNS))
def test_no_module_pattern_repeats_a_group(where: str) -> None:
    pattern = MODULE_PATTERNS[where]
    if risky(pattern.pattern, pattern.flags):
        assert pattern.pattern in ALLOWED, f"{where}: {pattern.pattern!r}"


@pytest.mark.parametrize("where", sorted(LITERAL_PATTERNS))
def test_no_literal_pattern_repeats_a_group(where: str) -> None:
    pattern, flags = LITERAL_PATTERNS[where]
    if risky(pattern, flags):
        assert pattern in ALLOWED, f"{where}: {pattern!r}"


def test_every_pattern_built_in_a_function_was_read() -> None:
    """A pattern built inside a function from other text can't be parsed here: each such call
    is listed, and an entry with no call left goes too."""
    assert built_in_functions() == set(BUILT_IN_FUNCTIONS)


def test_every_pattern_compiled_at_import_is_checked() -> None:
    """Whatever holds a pattern compiled at import, or nothing does (#836): `reachable` walks
    only dicts, sequences and sets four deep. Every module imports, each pattern `reachable`
    finds is among those watched, and so is each `re` call at a module's top level in every
    root, at its own line, so the watch works (review of #857)."""
    compiled, failed = compiled_at_import()
    assert not failed
    assert {p.pattern for p in MODULE_PATTERNS.values()} <= {p for _, p, _ in compiled}
    missed = unwatched(compiled)
    assert not missed, (
        "`re` calls at a module's top level that the import didn't run: move each into a "
        "function (listed in BUILT_IN_FUNCTIONS if its pattern is built), or make it run at "
        "import:\n" + "\n".join(missed)
    )
    bad = [f"{w}: {p!r}" for w, p, flags in compiled if risky(p, flags) and p not in ALLOWED]
    assert not bad, "\n".join(bad)


def test_re_is_used_as_the_scan_reads_it() -> None:
    """The scan reads `re.<function>(pattern, …)` after `import re`: any other way of reaching
    those functions, run later, would go unchecked (#836)."""
    found = unsupported()
    assert not found, (
        "call `re.<function>(pattern, …)` after `import re`, so that the scan reads the "
        "pattern:\n" + "\n".join(found)
    )


def test_each_pattern_of_a_container_is_found() -> None:
    """Codex round 1 on #834: a tuple of rules, some built by concatenation, in a dict and a
    set; each pattern is found under its own path."""
    tail = "(?:ab)*"
    rules = (
        (re.compile("x" + tail), "one"),
        (re.compile(r"y\d+"), "two"),
        (re.compile("z" + tail + "w"), "three"),
    )
    found = dict(reachable({"rules": rules, "more": {re.compile("v"), re.compile("u")}}, "M"))
    assert found == {
        "M['rules'][0][0]": rules[0][0],
        "M['rules'][1][0]": rules[1][0],
        "M['rules'][2][0]": rules[2][0],
        "M['more']{0}": re.compile("u"),
        "M['more']{1}": re.compile("v"),
    }
    assert [w for w, p in found.items() if risky(p.pattern)] == [
        "M['rules'][0][0]",
        "M['rules'][2][0]",
    ]


def test_every_redaction_rule_is_checked() -> None:
    """Each §11.6 rule is one of the patterns checked, though six are built by concatenation
    and so have no literal to read (codex round 1 on #834)."""
    from zab.render import redact

    checked = {id(p) for p in MODULE_PATTERNS.values()}
    rules = [p for p, _ in redact.SECRET_PATTERNS] + [redact._ZULIP_KEY_ANY_LINE_END]
    assert all(id(p) in checked for p in rules)


def test_the_allowed_patterns_are_still_used() -> None:
    used = {p.pattern for p in MODULE_PATTERNS.values()} | {p for p, _ in LITERAL_PATTERNS.values()}
    assert set(ALLOWED) <= used


def test_the_check_finds_a_group_repeat_in_each_construct() -> None:
    """The walk enters every construct that can hold a repeat: a conditional group too."""
    group = "(?:ab)*"
    for pattern in [
        group, "(?:ab)*?", "(ab)*", f"(?:x{group})", f"(?:x|y{group})", f"(?={group})x",
        f"(?!{group}z)x", f"(?:x{group})++", f"(?>{group})", f"(x)?(?(1){group}|y)",
        f"(x)?(?(1)y|{group})", "(?:ab){0,65}",
    ]:  # fmt: skip
        assert risky(pattern), pattern
    for pattern in ["(?:ab)*+", "(?:ab)++", "[ab]*", ".*?", "(?i:a)*", "(?:a|b)*", "(?:ab){0,64}"]:
        assert not risky(pattern), pattern


def test_the_scan_reads_or_refuses_each_form_it_missed(tmp_path: Path) -> None:
    """#836, from the review of #834: a `pattern=` keyword is read, and so is a lambda outside a
    function, which runs later; each other form the scan missed is refused, named."""
    gaps = tmp_path / "gaps"
    gaps.mkdir()
    sources = {
        "keyword.py": "import re\n\ndef f():\n    return re.compile(pattern=r'(?:ab)*')\n",
        "lambda.py": "import re\n\nF = lambda x: re.compile(x + r'(?:ab)*')\n",
        "from_import.py": "from re import compile\n",
        "alias.py": "import re as rx\n",
        "mapped.py": "import re\n\ndef f(xs):\n    return list(map(re.compile, xs))\n",
        "named.py": "import re\n\ndef f(x):\n    return getattr(re, 'compile')(x)\n",
        "starred.py": "import re\n\ndef f(args):\n    return re.compile(*args)\n",
        "private.py": "import re\n\ndef f(x):\n    return re._compile(x, 0)\n",
        "engine.py": "import regex\n",
        "scanner.py": "import re\nfrom re import Scanner\n\nS = re.Scanner([(r'a', None)])\n",
    }
    for name, source in sources.items():
        (gaps / name).write_text(source)
    roots = {"gaps/": gaps}
    assert {k: risky(*v) for k, v in literal_patterns(roots).items()} == {"gaps/keyword.py:4": True}
    assert built_in_functions(roots) == {("gaps/lambda.py", "<lambda>")}
    refused = {where.split(":")[0] for where in unsupported(roots)}
    assert refused == {f"gaps/{name}" for name in sources} - {"gaps/keyword.py", "gaps/lambda.py"}


def test_the_watch_finds_a_pattern_whatever_holds_it(tmp_path: Path) -> None:
    """#836, from the review of #834: a pattern compiled at import is found whatever holds it
    (a `MappingProxyType`, a dataclass, a `partial`, lists five deep, a class body that keeps
    nothing, `map(re.compile, …)`), in each module of a package, and a module that fails to
    import is named. A call at a module's top level that the import doesn't run is named too,
    except a literal in a `__main__` (review of #857)."""
    package = tmp_path / "held"
    package.mkdir()
    (package / "__init__.py").write_text(
        textwrap.dedent(
            """\
            import dataclasses
            import functools
            import re
            import types

            PROXY = types.MappingProxyType({"k": re.compile(r"(?:a1)*")})


            @dataclasses.dataclass(frozen=True)
            class Holder:
                pattern: re.Pattern


            HELD = Holder(re.compile(r"(?:a2)*"))
            PARTIAL = functools.partial(re.sub, re.compile(r"(?:a3)*"))
            DEEP = [[[[[re.compile(r"(?:a4)*")]]]]]


            class Unkept:
                re.compile("x" + r"(?:a5)*")


            MAPPED = list(map(re.compile, [r"(?:a6)*"]))
            """
        )
    )
    (package / "sub.py").write_text('import re\n\nSUB = {"k": [re.compile(r"(?:a7)*")]}\n')
    (package / "broken.py").write_text('raise RuntimeError("at import")\n')
    guarded = 'import re\n\nif __name__ == "__main__":\n    X = re.compile("x" + r"(?:a8)*")\n'
    (package / "guarded.py").write_text(guarded)
    main = 'import re\n\nLITERAL = re.compile(r"y")\nBUILT = re.compile("y" + "z")\n'
    (package / "__main__.py").write_text(main)
    compiled, failed = compiled_at_import({"held/": package})
    assert failed == ["held.broken: RuntimeError: at import"]
    assert unwatched(compiled, {"held/": package}) == ["held/__main__.py:4", "held/guarded.py:4"]
    found = {p: where for where, p, _flags in compiled if risky(p)}
    assert found == {
        "(?:a1)*": "held/__init__.py:6",
        "(?:a2)*": "held/__init__.py:14",
        "(?:a3)*": "held/__init__.py:15",
        "(?:a4)*": "held/__init__.py:16",
        "x(?:a5)*": "held/__init__.py:20",
        "(?:a6)*": "held/__init__.py:23",
        "(?:a7)*": "held/sub.py:3",
    }
