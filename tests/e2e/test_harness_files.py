"""The e2e harness's file-server helper, offline (issue #263)."""

from __future__ import annotations

import httpx
import pytest

from e2e.harness import files


async def test_file_server_links_raw_and_rendering():
    base = "https://files.example.org"
    answer = (
        f"See [notes]({base}/zab-test/abc/notes-1.md) and {base}/zab-test/abc/notes-1.md#L3, "
        "or https://evil.example/zab-test/abc/notes-1.md."
    )
    assert files.links(answer, base, ".md") == [f"{base}/zab-test/abc/notes-1.md"]
    assert files.links(answer, base, ".md", fragments=True) == [  # scenario 9's line link
        f"{base}/zab-test/abc/notes-1.md",
        f"{base}/zab-test/abc/notes-1.md#L3",
    ]
    assert files.raw_url(f"{base}/a.md?x=1#L3") == f"{base}/a.md?x=1&raw=1"
    with pytest.raises(ValueError):
        await files.fetch("https://evil.example/a.md", base)
    html = '<html><link href="katex.css"><pre><span class="k">def</span></pre></html>'

    def serve(request: httpx.Request) -> httpx.Response:
        """As #266's server: a page only for a browser's navigation, and never with ?raw=1."""
        navigating = request.headers.get("Sec-Fetch-Dest") in ("document", "iframe")
        vary = {"vary": "Sec-Fetch-Dest"}
        if request.url.params.get("raw") == "1" or not navigating:
            plain = {"content-type": "text/plain", **vary}
            return httpx.Response(200, text="# raw $x$", headers=plain)
        rendered = {"content-type": "text/html; charset=utf-8", **vary}
        return httpx.Response(200, text=html, headers=rendered)

    transport = httpx.MockTransport(serve)
    page = await files.fetch(f"{base}/a.md", base, transport=transport)
    assert page.html and files.renders_math(page) and files.highlights_code(page)
    raw = await files.fetch(files.raw_url(f"{base}/a.md"), base, transport=transport)
    assert raw.text == "# raw $x$" and not files.renders_math(raw)  # raw even when navigating
    bytes_ = await files.fetch(f"{base}/a.md", base, navigate=False, transport=transport)
    assert not bytes_.html  # a script's request gets the bytes
    # the deployed server, not the page, decides a skip (reviewer on #286)
    assert page.renderer and raw.renderer and bytes_.renderer
    old = httpx.MockTransport(
        lambda r: httpx.Response(200, text="# x", headers={"content-type": "text/plain"})
    )
    assert not (await files.fetch(f"{base}/a.md", base, transport=old)).renderer


@pytest.mark.parametrize("status,serves", [(404, True), (501, False), (502, False)])
async def test_a_file_server_with_share_links_answers_the_recheck(status, serves):
    """#710: a server before share links has no POST (501); one with them answers 404 for a
    share it doesn't hold."""
    seen = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        return httpx.Response(status)

    base = "https://files.example.org"
    assert await files.serves_shares(base, transport=httpx.MockTransport(answer)) is serves
    assert seen == [("POST", f"{base}/_zab/recheck/zab-e2e/{'0' * 40}")]


BASE = "https://files.example.org"
SHA = "0123456789abcdef0123456789abcdef01234567"
SHARE = f"{BASE}/zab-test/{SHA}/shared-1.md"
OWN = f"{BASE}/zab-test/{SHA}/my_notes*v2.md"  # a `_` and a `*` of the path's own


@pytest.mark.parametrize(
    "text,url",
    [
        (f"Here it is: **{SHARE}**", SHARE),  # #775: as the agent answered on the test VM
        (f"*{SHARE}*", SHARE),
        (f"__{SHARE}__", SHARE),
        (f"_{SHARE}_", SHARE),
        (f"The link {SHARE}** opens it.", SHARE),  # followed by the end of an emphasis
        (f"**The link: {SHARE}**.", SHARE),  # ... and a full stop
        (f"~~{SHARE}~~", SHARE),
        (f"`{SHARE}`", SHARE),
        (f"**{OWN}**", OWN),
        (f"_{OWN}_", OWN),
        (f"__{OWN}__, or *{OWN}*", OWN),
        (f"[{OWN}]({OWN})", OWN),
    ],
)
def test_a_link_in_emphasis_is_read_without_it(text, url):
    assert files.links(text, BASE, ".md") == [url]


def test_a_link_with_a_fragment_in_emphasis_keeps_the_fragment():
    assert files.links(f"**{OWN}#L3**", BASE, ".md", fragments=True) == [f"{OWN}#L3"]


def test_the_commit_of_a_file_server_link():
    assert files.commit_of(SHARE) == SHA
    assert files.commit_of(f"{BASE}/zab-test/{SHA}/") == SHA
    assert files.commit_of(f"{BASE}/zab-test/{SHA}") == SHA
    assert files.commit_of(f"{BASE}/{SHA}/shared-1.md") is None  # no repository
    assert files.commit_of(f"{BASE}/zab-test/{SHA[:12]}/shared-1.md") is None
    assert files.commit_of(f"{BASE}/zab-test/main/shared-1.md") is None


async def test_listed_shares_reads_what_zab_share_list_prints(monkeypatch):
    """The parser against `zab share --list`'s own code (`zab.tools.share.listing`), GitHub
    and git answered here: a file, a folder and a file, and a share made elsewhere."""
    from zab.tools import share

    other, elsewhere = "b" * 40, "c" * 40
    refs = {"20261103-120000-0a1b2c3d": SHA, "20261103-120500-4e5f6a7b": other, "x": elsewhere}
    made = {
        SHA: ("2026-11-03T12:00:00+00:00", ["my_notes*v2.md"]),
        other: ("2026-11-03T12:05:00+00:00", ["notes/", "a.md"]),
    }

    async def on_github(ws, top, remote):
        return refs

    async def describe(ws, top, sha):
        return made.get(sha, ("(made elsewhere)", []))

    monkeypatch.setattr(share, "on_github", on_github)
    monkeypatch.setattr(share, "describe", describe)
    text = await share.listing(None, "/repo", BASE, "zab-test", "origin")
    listed = files.listed_shares(text)
    assert listed == {SHA: "my_notes*v2.md", other: "notes/, a.md", elsewhere: "?"}, text
    refs.clear()
    none = await share.listing(None, "/repo", BASE, "zab-test", "origin")
    assert none == "This repository has no share links." and files.listed_shares(none) == {}
