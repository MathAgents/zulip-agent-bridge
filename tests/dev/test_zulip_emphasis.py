"""#651: no Python string in `src/zab` or `workshop/` writes `_…_` or `__…__`, which Zulip lacks.

Zulip's Markdown reads `*…*` as italics and `**…**` as bold, and leaves underscores as they are,
so that `snake_case` survives (`zerver/lib/markdown`: "we disable _ for bold and emphasis"). The
admin bot's notes were written `_…_`, and `!status all` ended with `_Retired, so left out: …._`,
underscores and all.

This check reads every string literal of every Python file under those trees, `*.py` and any
file without an extension whose first line is a `python` shebang (`lean-project`), f-strings
included (each `{…}` read as `{x}`). It leaves out docstrings and the pattern of a call to `re`,
which never reach Zulip, and fails on any other string that holds a `_…_` phrase, or a `__…__`
one other than a dunder name (`__main__`). The notes' own rendering is checked in
`tests/admin/test_admin_notes.py`. Other files (shell, Markdown) are not read here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TREES = ("src/zab", "workshop")
#: Before an opening underscore: not a word character (a letter, a digit or `_`), nor a path's or
#: a variable's (`/`, `\\`, `.`, `$`, `-`). Quotes, brackets, `>`, `*`, `}` and dashes are.
_OPEN = r"(?<![\w/\\.$-])"
#: After a closing underscore: not a word character, so `…`, `*`, `]`, quotes and the end are.
_CLOSE = r"(?!\w)"
#: `_…_`: an underscore, a phrase that starts and ends with neither a blank nor an underscore,
#: and an underscore. `snake_case`, `_private`, `apply_end`, `__dunder__` and `/tmp/_x_/` don't
#: match.
EMPHASIS = re.compile(rf"{_OPEN}_(?=[^\s_])[^\n]*?(?<=[^\s_])_{_CLOSE}")
#: `__…__`, the same way.
BOLD = re.compile(rf"{_OPEN}__(?=[^\s_])[^\n]*?(?<=[^\s_])__{_CLOSE}")
#: A dunder name, which `BOLD` matches and Zulip would not be asked to show.
DUNDER = re.compile(r"__[a-z][a-z0-9_]*__")
#: The functions of `re` whose first argument is a pattern, not text.
_PATTERN_TAKERS = frozenset({"compile", "search", "match", "fullmatch", "sub", "subn", "split",
                             "findall", "finditer"})  # fmt: skip
#: Strings allowed to match, each with its reason. None so far.
ALLOWED: dict[str, str] = {}


def python_files() -> list[Path]:
    """Every Python file under `TREES`: `*.py`, and the extensionless ones whose shebang runs
    Python."""
    found = []
    for tree in TREES:
        for path in sorted((ROOT / tree).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix == ".py":
                found.append(path)
            elif not path.suffix:
                with path.open("rb") as f:
                    first = f.readline(200)
                if first.startswith(b"#!") and b"python" in first:
                    found.append(path)
    return found


def _skipped(tree: ast.AST) -> set[int]:
    """The ids of the docstrings, the parts of f-strings (read whole, with their f-string) and
    every string inside a pattern passed to `re`."""
    skip = set()
    nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, nodes) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
        elif isinstance(node, ast.JoinedStr):
            skip |= {id(v) for v in node.values}
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "re"
                and func.attr in _PATTERN_TAKERS
            ):
                skip |= {id(n) for n in ast.walk(node.args[0])}
    return skip


def _text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{x}" for v in node.values)
    return None


def _marked(text: str) -> bool:
    if EMPHASIS.search(text):
        return True
    return any(not DUNDER.fullmatch(m[0]) for m in BOLD.finditer(text))


def findings(source: str, name: str) -> list[str]:
    """`name:line: 'text'` for each string literal of `source` that holds `_…_` or `__…__`."""
    tree = ast.parse(source)
    skip = _skipped(tree)
    out = []
    for node in ast.walk(tree):
        text = _text(node)
        if text is None or id(node) in skip or text in ALLOWED:
            continue
        if _marked(text):
            out.append(f"{name}:{node.lineno}: {text[:100]!r}")
    return sorted(out)


def test_no_string_writes_underscore_emphasis():
    files = python_files()
    assert ROOT / "workshop/ami/files/bin/lean-project" in files  # Python without `.py`
    found = []
    for path in files:
        found += findings(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
    assert not found, "Zulip's italics are *…* and its bold **…** (#651):\n" + "\n".join(found)


PLANTED = '''
"""A docstring may say _this_ and __that__."""
import re
gone = links = got = whole = named = more = link = x = a = ""
n1 = f"_Cut to fit: {gone} left out{', and long lines cut' * x}. {link}_"
n2 = f"_Retired, so left out: {', '.join(named[:10])}{more}._"
n3 = f"{x}\\n_Cut to 40 lines; the attachment of the rest failed._"
n4 = f"{x}\\n_Cut to 40 lines: the whole of it is {link}._"
n5 = [f"_Too long to show here: {got}._", x]
n6 = f"_The last lines; {whole}._"
w1 = "a word in _italics_, then more"
w2 = 'say "_x_" here'
w3 = "[_x_]"
w4 = ">_x_"
w5 = "_x_…"
w6 = "_x_*"
w7 = f"{a}_x_"
w8 = "—_x_—"
b1 = "a __bold phrase__ here"
b2 = "__Bold__"
ok = ["snake_case", "__dunder__", "__main__", "_private", "[a-z_]", "apply_end", "_", "a _ b"]
ok2 = ["x_ y _z", f"{x}_{x}", "/tmp/_x_/y", "$_x_", "*Cut to fit: 2 lines left out.*"]
ok3 = re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+"), re.sub("_a_", "", x)
ok4 = "**bold**", "__init__.py", "a__b__c"
'''


def test_the_check_finds_the_old_notes_and_the_edges():
    """The seven notes as v0.2.10 wrote them, the edges a review found missed (quotes,
    brackets, `>`, `…`, `*`, an interpolation before, a dash), `__…__`, and what must pass."""
    found = findings(PLANTED, "p.py")
    names = [PLANTED.split("\n")[int(f.split(":")[1]) - 1].split(" =")[0] for f in found]
    assert sorted(names) == sorted(
        ["n1", "n2", "n3", "n4", "n5", "n6", *(f"w{i}" for i in range(1, 9)), "b1", "b2"]
    ), found
