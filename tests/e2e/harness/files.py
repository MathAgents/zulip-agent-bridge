"""Fetches from the central file server (scenario 9, issue #256): rendered pages and `?raw=1`.

Only URLs on the rig's file server are fetched (`ZAB_E2E_FILES_URL`): a link taken from an
agent's answer is checked against it first, so the suite never follows a link elsewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

#: A URL ends at a space, a quote or a backtick, none of which a URL holds unencoded (RFC 3986),
#: or at what closes a Markdown link or an autolink.
_LINK_RE = r"https://[^\s)\]>\"'`]+"
#: What can follow a link without being part of it, as GitHub's autolinks have it: punctuation,
#: and the end of Markdown emphasis wrapped around it (`**…**`, `_…_`, `~~…~~`; #775). Inside
#: the path these stay.
_TRAILING = ".,;:!?*_~"


@dataclass(frozen=True)
class Page:
    status: int
    content_type: str
    text: str
    vary: str = ""  # the `Vary` header: `Sec-Fetch-Dest` once #256's rendering is deployed

    @property
    def html(self) -> bool:
        return self.content_type == "text/html"

    @property
    def renderer(self) -> bool:
        """The server has #256's rendering (PR #266 varies every answer on `Sec-Fetch-Dest`),
        whatever this page came out as: what decides a skip, not the rendering's success."""
        return "sec-fetch-dest" in self.vary.lower()


def links(text: str, base: str, suffix: str = "", *, fragments: bool = False) -> list[str]:
    """The file-server links in `text` (an agent's answer) whose path ends in `suffix`, each
    once; without their `#L…` fragments unless `fragments`."""
    out = []
    for url in re.findall(_LINK_RE, text):
        u = urlsplit(url.rstrip(_TRAILING))
        url = urlunsplit((u.scheme, u.netloc, u.path, u.query, u.fragment if fragments else ""))
        if within(url, base) and u.path.endswith(suffix):
            out.append(url)
    return list(dict.fromkeys(out))


#: The commit in a file-server link, `<base>/<repo>/<sha>/…`: a share's or a turn snapshot's.
_COMMIT = re.compile(r"/[^/]+/([0-9a-f]{40})(?:/|$)")


def commit_of(url: str) -> str | None:
    """The commit that the file-server link `url` opens, or None."""
    found = _COMMIT.match(urlsplit(url).path)
    return found[1] if found else None


def listed_shares(text: str) -> dict[str, str]:
    """What `zab share --list` printed (#710, `zab.tools.share.listing`), a line `<name> ·
    <date> · <paths> · <link>` per share: each share's commit → its paths, as listed."""
    out = {}
    for line in text.splitlines():
        parts = line.split(" · ")
        if len(parts) >= 4 and (sha := commit_of(parts[-1])):
            out[sha] = " · ".join(parts[2:-1])
    return out


def within(url: str, base: str) -> bool:
    u, b = urlsplit(url), urlsplit(base)
    return u.scheme == "https" and u.netloc == b.netloc and not u.username and not u.password


def raw_url(url: str) -> str:
    """`url` with `raw=1` added to its query (and its fragment dropped)."""
    u = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(u.query) if k != "raw"] + [("raw", "1")]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(query), ""))


#: What a browser sends when it navigates to a page: the file server renders a file only for
#: such a request and gives anything else the bytes (#256, PR #266; codex on #286).
NAVIGATE = {"Sec-Fetch-Dest": "document"}


async def fetch(
    url: str,
    base: str,
    *,
    navigate: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Page:
    """GET `url`, as a browser navigating to it unless `navigate` is false."""
    if not within(url, base):
        raise ValueError(f"not on the file server {base}: {url[:100]}")
    async with httpx.AsyncClient(transport=transport, timeout=60) as http:
        # no redirects followed: they could lead elsewhere
        response = await http.get(url, headers=NAVIGATE if navigate else None)
    kind = response.headers.get("content-type", "").split(";", 1)[0].strip()
    return Page(response.status_code, kind, response.text, response.headers.get("vary", ""))


async def serves_shares(base: str, *, transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """Whether the file server has #710's share links: its recheck answers 404 for a share it
    doesn't hold, where a server before #710 has no POST at all (501)."""
    async with httpx.AsyncClient(transport=transport, timeout=30) as http:
        response = await http.post(f"{base}/_zab/recheck/zab-e2e/{'0' * 40}")
    return response.status_code == 404


def renders_math(page: Page) -> bool:
    """A rendered Markdown page with math: HTML with KaTeX or MathJax (#256 requirement 1)."""
    text = page.text.lower()
    return page.html and ("katex" in text or "mathjax" in text)


def highlights_code(page: Page) -> bool:
    """Highlighted code: a `<pre>` with token spans (Pygments, highlight.js or Shiki)."""
    return page.html and bool(
        re.search(r"<pre[^>]*>.*?<span class=\"?[a-z]", page.text, re.S | re.I)
    )
