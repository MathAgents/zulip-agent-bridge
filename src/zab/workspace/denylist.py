"""The upload denylist (SPEC §11.4), shared by the workspace manager, diffs and the renderer.

The lists extend the SPEC §11.4 table with the entries from the PM review (issue #36):
``zuliprc`` without an extension, ``.git-credentials``, ``.pypirc``, ``.npmrc``, ``.aws/``,
``*.p12`` and ``*.pfx``. Matching is case-insensitive for both directories and names, so
``.ENV`` and ``Auth.JSON`` are refused too. This module imports nothing from the rest of the
package, so ``workspace/git.py`` can hide denylisted paths from turn diffs without a cycle.
"""

from __future__ import annotations

import fnmatch
import os

#: Directories whose contents are never uploaded, as path-component sequences matched
#: anywhere in the repository-relative path (lower case).
UPLOAD_DENY_DIRS: tuple[tuple[str, ...], ...] = (
    (".git",),
    (".zab", "worktrees"),
    (".ssh",),
    (".codex",),
    (".claude",),
    (".config", "zab"),
    (".aws",),
)

#: File-name globs that are never uploaded, matched against the lower-cased base name.
UPLOAD_DENY_NAMES: tuple[str, ...] = (
    ".env*",
    "*.pem",
    "*.key",
    "id_*",
    "*.zuliprc",
    "zuliprc",
    ".netrc",
    "auth.json",
    ".credentials.json",
    ".git-credentials",
    ".pypirc",
    ".npmrc",
    "*.p12",
    "*.pfx",
)


def upload_denied_reason(rel_path: str) -> str | None:
    """The denylist rule that refuses ``rel_path`` (repository-relative), or ``None``.

    Directory rules match any run of path components; name rules match the base name;
    both ignore case. The reason names the rule, never the file's contents (§11.4).
    """
    parts = tuple(p.lower() for p in rel_path.replace(os.sep, "/").split("/") if p and p != ".")
    for seq in UPLOAD_DENY_DIRS:
        n = len(seq)
        for i in range(len(parts) - n + 1):
            if parts[i : i + n] == seq:
                return f"files under {'/'.join(seq)}/ are never uploaded"
    if parts:
        for pat in UPLOAD_DENY_NAMES:
            if fnmatch.fnmatchcase(parts[-1], pat):
                return f"file names matching {pat} are never uploaded"
    return None


__all__ = ["UPLOAD_DENY_DIRS", "UPLOAD_DENY_NAMES", "upload_denied_reason"]
