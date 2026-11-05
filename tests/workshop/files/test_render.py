"""The file server's rendered views (issue #256): Markdown like GitHub, highlighted sources."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

DIRECTORY = Path(__file__).parents[3] / "workshop" / "files"
SAMPLES = Path(__file__).parent / "samples"
spec = importlib.util.spec_from_file_location("files_render", DIRECTORY / "render.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)
BASE = "/thesis/" + "a" * 40


@pytest.mark.parametrize(
    ("path", "alias"),
    [
        ("README.md", "markdown"), ("notes.MARKDOWN", "markdown"), ("Proof.lean", "lean4"),
        ("x.py", "python"), ("x.sage", "python"), ("x.c", "c"), ("x.h", "c"), ("x.jl", "julia"),
        ("x.rs", "rust"), ("x.gp", "gp"), ("x.m", "magma"), ("x.mag", "magma"),
        ("x.magma", "magma"), ("x.g", "gap"), ("x.gd", "gap"), ("x.gi", "gap"),
        ("x.gap", "gap"), ("x.m2", "macaulay2"),
        ("x.cpp", "cpp"), ("x.sh", "bash"), ("x.tex", "tex"),
        ("x.bib", "bibtex"), ("x.toml", "toml"), ("x.yml", "yaml"), ("x.json", "json"),
        ("x.js", "javascript"), ("x.css", "css"), ("data.csv", None), ("LICENSE", None),
    ],
)  # fmt: skip
def test_the_extension_picks_the_language(path, alias):
    assert render.language(path) == alias


def page(path: str, text: str) -> str:
    out = render.render(path, text.encode(), base=BASE)
    assert out is not None
    return out.decode()


MD = """# Fermat's little theorem

If $p$ is prime, $$a^p \\equiv a \\pmod p$$ for every integer $a$.

$$
\\sum_{n \\ge 1} n^{-s} = \\prod_p (1 - p^{-s})^{-1}
$$

```math
\\zeta(2) = \\pi^2/6
```

- [x] statement
- [ ] proof

| p | a^(p-1) mod p |
|---|---|
| 5 | 1 |

~~wrong~~ See https://www.lmfdb.org and [the proof](Proof.lean#L42),
[root](/README.md), ![plot](fig.png) ![abs](/figs/a.png).

<script>alert(1)</script> <b onclick="x()">bold</b>

```lean
theorem t : 2 + 2 = 4 := by norm_num
```

```gp
forprime(p = 2, 10, print(p))  \\\\ primes
```
"""


def test_markdown_renders_like_github():
    html = page("dir/README.md", MD)
    assert '<h1 id="fermats-little-theorem">' in html
    assert '<span class="math inline">p</span>' in html
    assert '<span class="math display">a^p \\equiv a \\pmod p</span>' in html
    assert '<div class="math display">\n\\sum_{n \\ge 1}' in html.replace("\\n", "\n") or (
        '<div class="math display">' in html and "\\sum_{n \\ge 1}" in html
    )
    assert '<div class="math display">\\zeta(2) = \\pi^2/6\n</div>' in html
    assert 'class="task-list-item-checkbox" checked="checked" disabled="disabled"' in html
    assert "<table>" in html and "<th>p</th>" in html and "<s>wrong</s>" in html
    assert '<a href="https://www.lmfdb.org">https://www.lmfdb.org</a>' in html  # autolink
    assert '<a href="Proof.lean#L42">' in html  # relative: resolves inside the snapshot
    assert f'<a href="{BASE}/README.md">' in html  # absolute: the snapshot's root
    assert '<img src="fig.png"' in html and f'<img src="{BASE}/figs/a.png"' in html
    assert '<pre class="hl"><code>' in html and '<span class="kn">theorem</span>' in html
    assert '<span class="c1">\\\\ primes</span>' in html  # GP fenced code


def test_raw_html_in_markdown_is_escaped_never_run():
    html = page("README.md", MD)
    assert "<script>alert" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "onclick" not in html.replace("onclick=&quot;", "") or "&lt;b onclick" in html
    assert "<b onclick" not in html


def test_math_pages_load_the_vendored_katex_and_others_do_not():
    html = page("README.md", MD)
    assert '<script defer src="/_zab/static/katex/katex.min.js?v=' in html
    assert '<script defer src="/_zab/static/math.js?v=' in html
    assert "cdn" not in html.lower()
    plain = page("README.md", "# Title\n\nNo formulas, no dollars.\n")
    assert "katex" not in plain


def test_every_page_has_a_raw_link_and_an_escaped_title():
    html = page("dir/<x>.md", "hi\n")
    assert '<a class="raw" href="?raw=1">Raw</a>' in html
    assert "<title>&lt;x&gt;.md</title>" in html


def test_a_source_file_has_line_numbers_and_anchors():
    html = page("Proof.lean", "import Mathlib.Tactic\n\ntheorem t : 2 + 2 = 4 := by\n  norm_num\n")
    assert '<tr id="L1"><td class="ln"><a href="#L1">1</a></td>' in html
    assert '<tr id="L4">' in html and '<tr id="L5">' not in html
    assert '<tr id="L2"><td class="ln"><a href="#L2">2</a></td><td class="code"> </td></tr>' in html
    assert '<span class="kn">import</span>' in html or '<span class="k">import</span>' in html
    assert "katex" not in html


def test_the_bridge_keeps_line_anchors_for_exactly_the_files_rendered_with_line_numbers():
    """Issue #448: the bridge keeps `#L<n>` on a file-server link to exactly the files this
    server renders as numbered rows (`zab.render.links.SERVED_LINE_EXT`, which can't import
    this module: its libraries are no zab dependency). Each row's id is `L<n>` and there is
    no other line id, so a range has no anchor; Markdown pages have none at all."""
    from zab.render.links import SERVED_LINE_EXT

    sources = {e.removeprefix(".") for e, alias in render.LANGUAGES.items() if alias != "markdown"}
    assert sources == SERVED_LINE_EXT
    for ext in sorted(sources):
        ids = re.findall(r'\bid="([^"]*)"', page(f"x.{ext.upper()}", "a = 1\nb = 2\n"))
        assert ids == ["L1", "L2"], ext
    assert not re.findall(r'\bid="L', page("README.md", MD + "\n# L1\n"))


@pytest.mark.parametrize(("name", "alias"), [("sample.gp", "gp"), ("sample.m", "magma")])
def test_the_gp_and_magma_lexers_read_real_programs_without_errors(name, alias):
    from pygments.token import Error

    text = (SAMPLES / name).read_text()
    tokens = list(render.lexer(alias).get_tokens(text))
    assert not [value for kind, value in tokens if kind is Error]
    assert "".join(value for _, value in tokens) == text


def test_gap_and_macaulay2_highlighting():
    """Pygments' own GAP and Macaulay2 lexers (reviewer on #449); `m2` fences are Macaulay2,
    never Pygments' Modula-2."""
    html = page("check.g", "G := SymmetricGroup(4);;\nfor g in G do Print(Order(g)); od;\n")
    assert '<span class="k">for</span>' in html and '<tr id="L2">' in html
    html = page("res.m2", "R = QQ[x,y];\nI = ideal(x^2, y^3);\nres I\n")
    assert '<span class="nf">ideal</span>' in html and '<tr id="L3">' in html
    fenced = page("README.md", "```m2\nI = ideal(x^2, y^3)\n```\n")
    assert '<span class="nf">ideal</span>' in fenced


def test_gp_highlighting():
    html = page("x.gp", (SAMPLES / "sample.gp").read_text())
    assert '<span class="c1">\\\\ Count primes' in html  # \\ comment
    assert '<span class="cp">\\p 38</span>' in html  # metacommand
    assert '<span class="w">  </span><span class="cp">\\p 50</span>' in html  # indented
    assert '<span class="cp">?ellinit</span>' in html  # help
    assert '<span class="k">forprime</span>' in html and '<span class="k">my</span>' in html
    assert '<span class="s">' in html and '<span class="se">\\"</span>' in html
    assert '<span class="nf">fermat</span>' in html and '<span class="mf">1.5e-3</span>' in html


def test_magma_highlighting():
    html = page("x.m", (SAMPLES / "sample.m").read_text())
    assert '<span class="c1">// Class groups' in html
    for keyword in ("intrinsic", "return", "function", "elif", "mod", "div", "printf", "vprint"):
        assert f'<span class="k">{keyword}</span>' in html, keyword
    assert '<span class="nc">RngIntElt</span>' in html  # the type after ::
    assert '<span class="nf">ClassNumber</span>' in html
    assert '<span class="o">:=</span>' in html
    assert '<span class="o">&amp;+</span>' in html  # reduction
    assert '<span class="o">&amp;</span><span class="k">cat</span>' in html
    assert '<span class="n">E</span><span class="o">`</span><span class="n">Label' in html
    assert '<span class="bp">$$</span>' in html and "<span class=\"n\">'+'</span>" in html


def test_over_the_caps_or_not_utf8_it_stays_plain_text():
    assert render.render("big.py", b"x" * (render.MAX_BYTES + 1), base=BASE) is None
    big = b"x" * (render.MAX_MARKDOWN_BYTES + 1)
    assert render.render("big.md", big, base=BASE) is None
    assert render.render("big.py", big, base=BASE) is not None  # a source file's cap is 1 MB
    lines = b"\n" * (render.MAX_LINES + 1)
    assert render.render("long.py", lines, base=BASE) is None
    assert render.render("bad.lean", b"\xff\xfe theorem", base=BASE) is None
    assert render.render("data.csv", b"a,b\n", base=BASE) is None


def test_the_stylesheet_has_the_layout_and_the_highlight_colors():
    css = render.stylesheet().decode()
    assert "table.source tr:target" in css and ".hl .k" in css


def test_static_files_are_served_from_the_directory_only():
    assert b"katex" in render.static_file("katex/katex.min.js")
    assert render.static_file("files.css") == render.stylesheet()
    assert render.static_file("../render.py") is None
    assert render.static_file("katex/../../server.py") is None
    assert render.static_file("missing.js") is None
    assert render.asset("math.js").startswith("/_zab/static/math.js?v=")


def test_text_after_display_math_stays_and_dollar_amounts_are_not_math():
    html = render.markdown_html("$$a^p \\equiv a \\pmod p$$ (Fermat)\n", BASE)
    assert "(Fermat)" in html and '<span class="math display">a^p' in html
    assert render.markdown_html("It costs $5 and $10.\n", BASE) == "<p>It costs $5 and $10.</p>\n"
    assert '<span class="math inline">2^n</span>' in render.markdown_html("$2^n$\n", BASE)


def test_an_unknown_fence_language_is_escaped_as_it_is():
    html = render.markdown_html("```nosuchlanguage\n<b>x</b>\n```\n", BASE)
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>" not in html and 'class="hl"' not in html


TASKS_AND_HEADINGS = """# Notes
# Notes
## Notes-1
# Notes
# `code` and *emphasis* in a heading
Setext
======

- [ ] open
- [x] done
- [X] done too
  1. [ ] nested, ordered
  2. not a task
- [ ]no space, not a task

> - [x] in a quote

Text[^1]

[^1]: - [ ] a task in a footnote
"""


def plugins_and_ours():
    """The plugins render.py replaces, and render.py's rules, each with footnotes."""
    from markdown_it import MarkdownIt
    from mdit_py_plugins.anchors import anchors_plugin
    from mdit_py_plugins.footnote import footnote_plugin
    from mdit_py_plugins.tasklists import tasklists_plugin

    plugins = MarkdownIt("gfm-like").use(tasklists_plugin).use(footnote_plugin)
    plugins.use(anchors_plugin, max_level=6)
    ours = MarkdownIt("gfm-like")
    ours.core.ruler.after("inline", "github-tasklists", render._tasklists)
    ours.use(footnote_plugin).core.ruler.push("anchor", render._anchors)
    return plugins, ours


def test_task_lists_and_heading_ids_match_the_plugins_they_replace():
    """render.py has its own linear rules for these two plugins (review on #265): the
    output doesn't change, except for the marker case below."""
    plugins, ours = plugins_and_ours()
    html = ours.render(TASKS_AND_HEADINGS)
    assert html == plugins.render(TASKS_AND_HEADINGS)
    assert html.count("task-list-item-checkbox") == 6 and 'id="notes-2"' in html


@pytest.mark.parametrize("space", ["\t", "\n", "\r", "\v", "\f"])
def test_a_task_marker_before_other_ascii_whitespace_gets_its_checkbox(space):
    """The one difference from the plugin: it drops `[x]` before a tab or newline but draws no
    checkbox, as it builds one only after a space. GFM allows any whitespace there."""
    plugins, ours = plugins_and_ours()
    text = f"- [x]{space}done\n"
    html = ours.render(text)
    assert 'class="task-list-item-checkbox" checked="checked"' in html and "[x]" not in html
    plugin_html = plugins.render(text)
    assert "checkbox" not in plugin_html and "[x]" not in plugin_html  # the plugin's bug


@pytest.mark.parametrize("space", ["\u00a0", "\u2003", "\u3000"])
def test_a_task_marker_before_unicode_whitespace_stays_text(space):
    """GFM's whitespace is ASCII: `[ ]` before a no-break space is text, here as in the plugin."""
    plugins, ours = plugins_and_ours()
    text = f"- [ ]{space}open\n"
    assert "[ ]" in ours.render(text) and "checkbox" not in ours.render(text)
    assert ours.render(text) == plugins.render(text)


@pytest.mark.timing
@pytest.mark.parametrize("unit", ["- [ ] a\n", "# h\n"])
def test_long_task_lists_and_repeated_headings_are_cheap(unit, cpu_ratio_below):
    """The plugins' rules were quadratic: 10k tasks took 5 s, and 20k equal headings 14 s.

    A bound on how the cost grows, not on how long it takes (#707): the absolute bound of 2 s
    this test had was 3 to 8 times this machine's time, and one of ten times failed on a
    runner. 8,000 items against 2,000 four times (`cpu_ratio_below`, tests/conftest.py): 1.00
    to 1.11 times their CPU here and 0.96 to 1.13 on CI's runners, under Python 3.11 to 3.14;
    with the plugins' rules, 3.5 to 3.8 times. 8,000 items rather than the most that render
    keep a regression's failure under 30 s here."""

    def rendered(data: bytes) -> None:
        assert render.render("x.md", data, base=BASE) is not None

    cpu_ratio_below(rendered, (unit * 2_000).encode(), (unit * 8_000).encode(), 2, repeat=4)


def test_too_many_blocks_stay_plain_text():
    """A table holds about 1 KB of tokens per byte: a 0.8 MB one took 300 MB."""
    wide = "|" + "a|" * 1000 + "\n|" + "-|" * 1000 + "\n" + ("|" + "x|" * 1000 + "\n") * 25
    assert len(wide) < render.MAX_MARKDOWN_BYTES
    assert render.render("t.md", wide.encode(), base=BASE) is None
    assert "<table>" in page("t.md", "|a|b|\n|-|-|\n|1|2|\n")


#: Loads render.py in a fresh interpreter, as the server does, then renders every language at
#: once from many threads, and once more serially; prints the differences.
CONCURRENT = """
import importlib.util, json, sys, threading
spec = importlib.util.spec_from_file_location("render", sys.argv[1])
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)
fences = "".join(f"```{a}\\nx = 1\\n```\\n" for a in sorted(set(render.FENCE_ALIASES)))
docs = [("x.md", "# T\\n\\n- [ ] a www.example.com $x$\\n\\n" + fences)]
docs += [("x" + ext, "x = 1\\n") for ext in render.LANGUAGES if ext != ".markdown"]
docs *= 2
barrier = threading.Barrier(len(docs))
out = [None] * len(docs)

def work(i):
    barrier.wait()
    try:
        out[i] = render.render(docs[i][0], docs[i][1].encode(), base="/s").decode()
    except Exception as exc:
        out[i] = repr(exc)

threads = [threading.Thread(target=work, args=(i,)) for i in range(len(docs))]
for t in threads:
    t.start()
for t in threads:
    t.join()
expected = [render.render(p, t.encode(), base="/s").decode() for p, t in docs]
print(json.dumps([[p, o[:300]] for (p, _), o, e in zip(docs, out, expected) if o != e]))
"""


def test_concurrent_first_renders_in_a_fresh_process_all_succeed():
    """A restarted server's first burst: Pygments' lazy loader raised ImportError in some
    threads, and fences came out unhighlighted (review on #265)."""
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-c", CONCURRENT, str(DIRECTORY / "render.py")],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert json.loads(result.stdout) == []
