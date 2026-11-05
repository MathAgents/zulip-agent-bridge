"""Pygments lexers for PARI/GP and Magma, which Pygments doesn't ship (issue #256).

Written for zab's file server from the languages' own documentation: GP's control
statements and comment syntax (the PARI/GP user's guide, "Programming in GP"), Magma's
reserved words (the Magma handbook, "Reserved words"). No grammar or keyword list was copied
from an editor mode or a Jupyter kernel, so no third-party license applies (DECISIONS).
"""

from __future__ import annotations

from typing import ClassVar

from pygments.lexer import RegexLexer, bygroups, words
from pygments.token import (
    Comment,
    Keyword,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Whitespace,
)

#: GP's control statements and scoping (they read like functions, so they're keywords only
#: by convention; highlighting them helps a reader find a loop).
GP_KEYWORDS = (
    "if", "while", "until", "for", "forcomposite", "fordiv", "fordivfactored", "forell",
    "forfactored", "forpart", "forperm", "forprime", "forprimestep", "forqfvec",
    "forsquarefree", "forstep", "forsubgroup", "forsubset", "forvec", "parfor", "parforeach",
    "parforprime", "parforprimestep", "parforvec", "break", "next", "return", "my", "local",
    "iferr", "error", "export", "exportall", "unexport", "unexportall", "breakpoint",
)  # fmt: skip

#: Magma's reserved words (the handbook's list): none of them can name a variable.
MAGMA_KEYWORDS = (
    "_", "adj", "and", "assert", "assert2", "assert3", "assigned", "break", "by", "case", "cat",
    "catch", "clear", "cmpeq", "cmpne", "continue", "declare", "default", "delete", "diff",
    "div", "do", "elif", "else", "end", "eq", "error", "eval", "exists", "exit", "false", "for",
    "forall", "forward", "fprintf", "freeze", "function", "ge", "gt", "if", "iload", "import",
    "in", "intrinsic", "is", "join", "le", "load", "local", "lt", "meet", "mod", "ne", "not",
    "notadj", "notin", "notsubset", "or", "print", "printf", "procedure", "quit", "random",
    "read", "readi", "repeat", "require", "requirege", "requirerange", "restore", "return",
    "save", "sdiff", "select", "subset", "then", "time", "to", "true", "try", "until",
    "vprint", "vprintf", "vtime", "when", "where", "while", "xor",
)  # fmt: skip

IDENT = r"[A-Za-z_][A-Za-z0-9_]*"


class GPLexer(RegexLexer):
    """PARI/GP scripts: `\\\\` and `/* */` comments, `\\p`-style metacommands at the start of a
    line, strings, integers and reals, and `name(` calls."""

    name = "PARI/GP"
    aliases: ClassVar[list[str]] = ["gp", "pari", "pari-gp"]
    filenames: ClassVar[list[str]] = ["*.gp"]

    tokens: ClassVar[dict] = {
        "root": [
            # a metacommand (`\\p 38`) or help (`?ellinit`) starts a line, maybe indented, so
            # whitespace stops at each newline to keep `^` matching
            (r"^([^\S\n]*)(\\[a-zA-Z]+.*?)$", bygroups(Whitespace, Comment.Preproc)),
            (r"^([^\S\n]*)(\?.*?)$", bygroups(Whitespace, Comment.Preproc)),
            (r"\s*\n|[^\S\n]+", Whitespace),
            (r"\\\\.*?$", Comment.Single),
            (r"/\*", Comment.Multiline, "comment"),
            (r'"', String, "string"),
            (words(GP_KEYWORDS, suffix=r"\b"), Keyword),
            (r"(" + IDENT + r")(\s*)(?=\()", bygroups(Name.Function, Whitespace)),
            (IDENT + r"'*", Name),
            (r"0x[0-9a-fA-F]+", Number.Hex),
            (r"0b[01]+", Number.Bin),
            (r"(\d+\.\d*|\.\d+)([eE][+-]?\d+)?|\d+[eE][+-]?\d+", Number.Float),
            (r"\d+", Number.Integer),
            (r"->|==|!=|<=|>=|&&|\|\||\+\+|--|[-+*/%\\^=<>!~#'&|]=?", Operator),
            (r"[()\[\]{},;:.]", Punctuation),
        ],
        "comment": [
            (r"[^*]+", Comment.Multiline),
            (r"\*/", Comment.Multiline, "#pop"),
            (r"\*", Comment.Multiline),
        ],
        "string": [
            (r"\\.", String.Escape),
            (r'[^"\\]+', String),
            (r'"', String, "#pop"),
        ],
    }


class MagmaLexer(RegexLexer):
    """Magma: `//` and `/* */` comments, reserved words, strings, `:=` and `->`, `name(`
    calls, and the type after `::` in intrinsic signatures."""

    name = "Magma"
    aliases: ClassVar[list[str]] = ["magma"]
    filenames: ClassVar[list[str]] = ["*.m", "*.mag", "*.magma"]

    tokens: ClassVar[dict] = {
        "root": [
            (r"\s+", Whitespace),
            (r"//.*?$", Comment.Single),
            (r"/\*", Comment.Multiline, "comment"),
            (r'"', String, "string"),
            (r"'[^'\n]*'", Name),  # a quoted name, such as the operator '+'
            (r"\$\$|\$\d*", Name.Builtin.Pseudo),  # the function being defined; past results
            (r"(::)(\s*)(" + IDENT + r")", bygroups(Punctuation, Whitespace, Name.Class)),
            (words(MAGMA_KEYWORDS, suffix=r"\b"), Keyword),
            (r"(" + IDENT + r")(\s*)(?=\()", bygroups(Name.Function, Whitespace)),
            (IDENT, Name),
            (r"(\d+\.\d*|\.\d+)([eE][+-]?\d+)?|\d+[eE][+-]?\d+", Number.Float),
            (r"\d+", Number.Integer),
            # `&` reduces (`&+[…]`, `&cat`); a backtick reads an attribute (`E`Name`)
            (r":=|->|@@|@|\^|[-+*/=<>~#|!&`]", Operator),
            (r"[()\[\]{},;:.<>]", Punctuation),
        ],
        "comment": [
            (r"[^*]+", Comment.Multiline),
            (r"\*/", Comment.Multiline, "#pop"),
            (r"\*", Comment.Multiline),
        ],
        "string": [
            (r"\\.", String.Escape),
            (r'[^"\\]+', String),
            (r'"', String, "#pop"),
        ],
    }
