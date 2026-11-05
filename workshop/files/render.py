"""Rendered views for files.mathagents.org (issue #256): Markdown as GitHub shows it, and
source files highlighted, with line numbers and `#L42` anchors.

Rendering is server-side (DECISIONS): markdown-it-py with mdit-py-plugins (GFM tables, task
lists, strikethrough, autolinks, footnotes, heading anchors, `$…$`/`$$…$$` and ```` ```math ````
math) and Pygments, with `lexers.py` for PARI/GP and Magma. Math is left as TeX in
`.math` elements and typeset in the browser by KaTeX, vendored under `/_zab/static/katex/`,
so a page never depends on a CDN. Raw HTML in Markdown is escaped, never passed through:
the strictest sanitizer. Pages are served with the same CSP sandbox as agent HTML.

`render(path, data, base=…)` returns the page's bytes, or None when the file isn't one of
ours or is over the caps: the server then serves it as plain text, as before. `?raw=1`
always returns the exact bytes (server.py).

Loading this module warms it up (`warm()`): every library, lexer and rule a render can use
is loaded before the server takes requests. Pygments loads lazily, and its loader isn't
thread-safe: concurrent first renders failed with ImportError (review on #265).
"""

from __future__ import annotations

import functools
import hashlib
import html
import posixpath
import re
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.anchors.index import slugify
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.footnote import footnote_plugin
from pygments import highlight
from pygments.formatters.html import HtmlFormatter  # never `pygments.formatters`: see above
from pygments.lexers import find_lexer_class_by_name
from pygments.util import ClassNotFound

STATIC = "/_zab/static"
MAX_BYTES = 1_000_000
MAX_LINES = 20_000
#: Markdown costs far more than highlighting: up to about 5 µs a byte, and a table holds
#: about 1 KB of tokens per byte. Over these caps a Markdown file stays plain text, so a
#: render stays under about 1 s and 100 MB (review on #265; the PR records the timings).
MAX_MARKDOWN_BYTES = 200_000
MAX_MARKDOWN_BLOCKS = 60_000  # block-level tokens: a table cell is 3, a list item 5
HERE = Path(__file__).resolve().parent

#: File extension → Pygments lexer alias ("markdown" for Markdown). `.m` is Magma here, never
#: MATLAB or Objective-C, and `.sage` is read with the Python grammar. The bridge links lines
#: of exactly these files but Markdown (`zab.render.links.SERVED_LINE_EXT`, issue #448).
LANGUAGES: dict[str, str] = {
    ".md": "markdown", ".markdown": "markdown",
    ".lean": "lean4", ".py": "python", ".sage": "python", ".c": "c", ".h": "c",
    ".jl": "julia", ".rs": "rust", ".gp": "gp", ".m": "magma", ".mag": "magma",
    ".magma": "magma", ".g": "gap", ".gd": "gap", ".gi": "gap", ".gap": "gap",
    ".m2": "macaulay2",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".sh": "bash", ".bash": "bash", ".tex": "tex", ".sty": "tex", ".cls": "tex",
    ".bib": "bibtex", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".js": "javascript", ".mjs": "javascript", ".css": "css",
}  # fmt: skip
#: Fenced-code info strings that name a language differently. `m2` is Macaulay2 here, never
#: Pygments' Modula-2.
FENCE_ALIASES = {
    "lean": "lean4", "sage": "python", "sagemath": "python", "pari": "gp", "pari-gp": "gp",
    "gp": "gp", "magma": "magma", "sh": "bash", "shell": "bash", "console": "bash",
    "m2": "macaulay2",
}  # fmt: skip


def language(path: str) -> str | None:
    return LANGUAGES.get(posixpath.splitext(path)[1].lower())


@functools.cache
def _custom() -> object:
    """lexers.py, loaded from this directory (the service runs server.py as a script)."""
    spec = spec_from_file_location("zab_files_lexers", HERE / "lexers.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=256)
def _lexer_class(alias: str) -> type | None:
    """Pygments looks an alias up linearly, and an unknown one in every installed package's
    metadata: about 140 µs, once per fenced block. Fence names are the document's, hence
    the bound."""
    if alias in ("gp", "pari", "pari-gp"):
        return _custom().GPLexer
    if alias == "magma":
        return _custom().MagmaLexer
    try:
        return find_lexer_class_by_name(alias)
    except ClassNotFound:
        return None


def lexer(alias: str):
    cls = _lexer_class(alias)
    if cls is None:
        raise ClassNotFound(alias)
    return cls(stripnl=False, ensurenl=True)


def highlight_lines(text: str, alias: str) -> list[str]:
    """One HTML fragment per source line (Pygments closes its spans at each line end)."""
    out = highlight(text, lexer(alias), HtmlFormatter(nowrap=True))
    return out[:-1].split("\n") if out.endswith("\n") else out.split("\n")


# ----------------------------------------------------------------------------- Markdown


def _fence_highlight(code: str, lang: str, _attrs: str) -> str:
    alias = FENCE_ALIASES.get(lang.lower(), lang.lower()) if lang else ""
    if not alias or _lexer_class(alias) is None:
        return ""  # an unknown language: markdown-it escapes the code as it is
    # a whole <pre>, which markdown-it uses as it is, so that Pygments' `.hl` colors apply
    return '<pre class="hl"><code>' + "\n".join(highlight_lines(code, alias)) + "\n</code></pre>\n"


class TooLarge(Exception):
    """A Markdown file with too many blocks to render cheaply: it stays plain text."""


def _cap_blocks(state) -> None:
    """Before the inline pass, which multiplies the tokens: a 19k-row table took 300 MB."""
    if len(state.tokens) > MAX_MARKDOWN_BLOCKS:
        raise TooLarge


_TASK = re.compile(r"\[([ xX])][ \t\n\v\f\r]+")
_BOX = '<input class="task-list-item-checkbox"{} disabled="disabled" type="checkbox">'


def _tasklists(state) -> None:
    """GitHub task lists, rendered as by mdit-py-plugins' `tasklists_plugin`, in one pass: the
    plugin searches back for each item's list, which took 17.6 s for 19k items. One case
    differs: when a tab, newline, CR, VT or FF follows `[ ]` or `[x]`, not a space, the plugin
    drops the marker and draws no checkbox (it builds one only after a space). This rule draws
    it, as GFM does: a task marker may be followed by any whitespace."""
    tokens = state.tokens
    last_at_level: dict[int, Token] = {}
    for i, token in enumerate(tokens):
        if (
            token.type == "inline"
            and i >= 2
            and tokens[i - 1].type == "paragraph_open"
            and tokens[i - 2].type == "list_item_open"
            and (match := _TASK.match(token.content))
        ):
            box = Token("html_inline", "", 0)
            box.content = _BOX.format("" if match[1] == " " else ' checked="checked"')
            token.children.insert(0, box)
            token.children[1].content = token.children[1].content[3:]
            token.content = token.content[3:]
            tokens[i - 2].attrSet("class", "task-list-item")
            last_at_level[tokens[i - 2].level - 1].attrSet("class", "contains-task-list")
        last_at_level[token.level] = token


def _anchors(state) -> None:
    """GitHub-style heading ids, the same as mdit-py-plugins' `anchors_plugin` sets them, but
    remembering each slug's next `-n`: the plugin counts from 1 every time, which took 14 s
    for 20k headings with the same title."""
    taken: set[str] = set()
    tried: dict[str, int] = {}
    tokens = state.tokens
    for i, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        children = tokens[i + 1].children or []
        slug = slugify("".join(c.content for c in children if c.type in ("text", "code_inline")))
        uniq, n = slug, tried.get(slug, 1)
        while uniq in taken:
            uniq, n = f"{slug}-{n}", n + 1
        tried[slug] = n
        taken.add(uniq)
        token.attrSet("id", uniq)


@functools.cache
def _markdown():
    md = MarkdownIt("gfm-like", {"html": False, "highlight": _fence_highlight})
    # no labels: they swallowed "(Fermat)" after `$$…$$`; no digits: "$5 and $10" is money
    md.use(dollarmath_plugin, double_inline=True, allow_labels=False, allow_digits=False)
    md.core.ruler.after("block", "zab_cap", _cap_blocks)
    md.core.ruler.after("inline", "github-tasklists", _tasklists)
    md.use(footnote_plugin)
    md.core.ruler.push("anchor", _anchors)

    def math(cls: str, tag: str):
        def rule(self, tokens, idx, options, env) -> str:
            return f'<{tag} class="math {cls}">{html.escape(tokens[idx].content)}</{tag}>' + (
                "\n" if tag == "div" else ""
            )

        return rule

    md.add_render_rule("math_inline", math("inline", "span"))
    md.add_render_rule("math_inline_double", math("display", "span"))
    md.add_render_rule("math_block", math("display", "div"))
    fence = md.renderer.rules["fence"]

    def fence_or_math(self, tokens, idx, options, env) -> str:
        if tokens[idx].info.strip().lower() == "math":
            return f'<div class="math display">{html.escape(tokens[idx].content)}</div>\n'
        return fence(tokens, idx, options, env)

    md.add_render_rule("fence", fence_or_math)
    return md


def _rebase(tokens, base: str) -> None:
    """`/x` in a link or image means the snapshot's root, as on GitHub; relative links
    already resolve inside the snapshot, since the page's URL is the file's."""
    for token in tokens:
        for attr in ("href", "src"):
            value = token.attrGet(attr)
            if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
                token.attrSet(attr, base + value)
        if token.children:
            _rebase(token.children, base)


def markdown_html(text: str, base: str) -> str:
    md = _markdown()
    env: dict = {}
    tokens = md.parse(text, env)
    _rebase(tokens, base)
    return md.renderer.render(tokens, md.options, env)


# ----------------------------------------------------------------------------- pages


def source_html(text: str, alias: str) -> str:
    rows = "".join(
        f'<tr id="L{n}"><td class="ln"><a href="#L{n}">{n}</a></td>'
        f'<td class="code">{line or " "}</td></tr>\n'
        for n, line in enumerate(highlight_lines(text, alias), 1)
    )
    return f'<table class="source hl"><tbody>\n{rows}</tbody></table>'


def static_file(name: str) -> bytes | None:
    """A file served under STATIC: files.css is generated, the rest read from static/."""
    if name == "files.css":
        return stylesheet()
    path = (HERE / "static" / name).resolve()
    if not path.is_relative_to(HERE / "static") or not path.is_file():
        return None
    return path.read_bytes()


@functools.cache
def asset(name: str) -> str:
    """The URL of a static file, tagged with its content hash, so pages that reference it can
    be cached for a year without going stale when it changes."""
    data = static_file(name) or b""
    return f"{STATIC}/{name}?v={hashlib.sha256(data).hexdigest()[:12]}"


def page(path: str, body: str, *, kind: str, math: bool) -> str:
    name = posixpath.basename(path) or path
    katex = (
        f'<link rel="stylesheet" href="{asset("katex/katex.min.css")}">\n'
        f'<script defer src="{asset("katex/katex.min.js")}"></script>\n'
        f'<script defer src="{asset("math.js")}"></script>\n'
        if math
        else ""
    )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(name)}</title>\n"
        f'<link rel="stylesheet" href="{asset("files.css")}">\n{katex}</head>\n'
        f'<body><header><span class="path">{html.escape(path)}</span>'
        f'<a class="raw" href="?raw=1">Raw</a></header>\n'
        f'<main class="{kind}">\n{body}\n</main></body></html>\n'
    )


def render(path: str, data: bytes, *, base: str) -> bytes | None:
    """The rendered page for a Markdown or source file, or None (plain text, as before).
    None is always for good (the caps, not UTF-8); an exception may be transient."""
    alias = language(path)
    limit = MAX_MARKDOWN_BYTES if alias == "markdown" else MAX_BYTES
    if alias is None or len(data) > limit or data.count(b"\n") > MAX_LINES:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if alias == "markdown":
        try:
            body = markdown_html(text, base)
        except TooLarge:
            return None
        return page(path, body, kind="markdown", math='class="math ' in body).encode()
    return page(path, source_html(text, alias), kind="source", math=False).encode()


@functools.cache
def stylesheet() -> bytes:
    """files.css: the page layout, then Pygments' colors for `.hl`."""
    own = (HERE / "static" / "files.css").read_text()
    return (own + "\n" + HtmlFormatter(style="default").get_style_defs(".hl")).encode()


#: Touches every Markdown rule and plugin once, with a fenced block in each language.
_WARM = """# Warm *up*

- [x] task [link](a.md) www.example.com https://example.com x@example.com ~~s~~ `c`[^1]

| a | b |
|---|---|
| $x$ | $$y$$ |

$$
z
$$

```math
w
```

> quote &amp; <b>

[^1]: note
"""


def warm() -> None:
    """Load everything a render can use, once (see the module's docstring)."""
    stylesheet()
    for name in ("files.css", "katex/katex.min.css", "katex/katex.min.js", "math.js"):
        asset(name)
    aliases = sorted({*LANGUAGES.values(), *FENCE_ALIASES} - {"markdown"})
    fences = "".join(f"\n\n```{alias}\nx = 1\n```" for alias in aliases)
    for alias in aliases:
        source_html("x = 1\n", FENCE_ALIASES.get(alias, alias))
    markdown_html(_WARM + fences, "/")


warm()
