"""The deployer's opt-in check of a files host (README §4): a synthetic turn snapshot pushed to a
private scratch repository of the host's GitHub organization, then read back through the host.
Uses the deployer's configured git push credentials."""

import argparse
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

#: One file per language the renderer must handle (issue #256), checked as rendered pages.
RENDERED = {
    "README.md": "# Selftest\n\nIf $p$ is prime, $$a^p \\equiv a \\pmod p$$.\n",
    "Proof.lean": "theorem two : 1 + 1 = 2 := rfl\n",
    "probe.py": "def f(n):\n    return n + 1\n",
    "probe.sage": "K.<a> = NumberField(x^2 + 23)\n",
    "probe.c": "int main(void) { return 0; }\n",
    "probe.jl": "f(n) = n + 1\n",
    "probe.rs": "fn main() {}\n",
    "probe.gp": "\\\\ comment\nforprime(p = 2, 10, print(p))\n",
    "probe.m": "// comment\nK<w> := QuadraticField(-23);\n",
}
#: Plain HTTP only to this machine: an SSH tunnel to the server staged beside the running one
#: before an update's switch (README, "Updating a running server"; issue #442).
LOOPBACK = re.compile(r"http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}/?")
#: A GitHub organization's name, as the server checks its own (`server.ORG_NAME`, #693).
ORG_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", re.ASCII)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="a private scratch repository of --org, by its name alone")
    parser.add_argument(
        "--org",
        required=True,
        help="the GitHub organization the server serves: its ZAB_FILES_ORG (README §3)",
    )
    parser.add_argument(
        "--url",
        required=True,
        help="the server, https://<host>; http://127.0.0.1:PORT for a tunnel to a staged one",
    )
    parser.add_argument("--push-url", help="HTTPS or SSH push URL for that repository")
    args = parser.parse_args()
    if not ORG_NAME.fullmatch(args.org):
        parser.error("--org must be a GitHub organization's name")
    if not re.fullmatch(r"(?!\.{1,2}$)[a-zA-Z0-9_.-]{1,100}", args.repo):
        parser.error("repo must be a repository's name, without its organization")
    if not (args.url.startswith("https://") or LOOPBACK.fullmatch(args.url)):
        parser.error("--url must use HTTPS, or be http://127.0.0.1:PORT (a tunnel)")
    full = f"{args.org}/{args.repo}"
    remote = args.push_url or f"https://github.com/{full}.git"
    if remote.lower() not in (
        f"https://github.com/{full.lower()}.git",
        f"git@github.com:{full.lower()}.git",
        f"ssh://git@github.com/{full.lower()}.git",
    ):
        parser.error(f"--push-url must target {full} on GitHub")
    # Probe anonymously: public repositories must never pass this token check.
    # A 404 can also mean absent; the authenticated git push below then fails.
    # No gh login or API credential is needed on the admin machine.
    try:
        with urlopen(f"https://api.github.com/repos/{full}", timeout=40):
            parser.error(
                "selftest requires a private scratch repository to exercise the server token"
            )
    except HTTPError as exc:
        if exc.code != 404:
            parser.error(f"anonymous repository visibility check failed (HTTP {exc.code})")
    args.repo = args.repo.lower()
    with tempfile.TemporaryDirectory(prefix="zab-files-selftest-") as tmp:
        root = Path(tmp)

        def git(*argv: str) -> str:
            return (
                subprocess.check_output(  # noqa: S603 — argv only
                    [
                        "/usr/bin/git",
                        "-C",
                        tmp,
                        "-c",
                        "user.name=Zab selftest",
                        "-c",
                        "user.email=selftest@example.invalid",
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "core.hooksPath=/dev/null",
                        *argv,
                    ],
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
                .decode()
                .strip()
            )

        git("init", "--quiet", "--initial-branch=main", "--template=")
        (root / "probe.txt").write_text("branch only\n")
        git("add", "probe.txt")
        git("commit", "--quiet", "-m", "Selftest branch commit")
        branch = git("rev-parse", "HEAD")
        marker = f"zab-files selftest {time.time_ns()}\n"
        (root / "probe.txt").write_text(marker)
        # Decoy only. Verify the central denylist even for tracked files.
        (root / "auth.json").write_text("synthetic denylist decoy\n")
        for name, text in RENDERED.items():
            (root / name).write_text(text)
        git("add", ".")
        git("commit", "--quiet", "-m", "Selftest turn snapshot")
        snapshot = git("rev-parse", "HEAD")
        ref = f"refs/zab/turns/selftest/{time.time_ns()}"
        git("push", remote, f"HEAD:{ref}")
        base = f"{args.url.rstrip('/')}/{args.repo}"

        def get(path: str, page: bool = False) -> tuple[int, bytes]:
            # a browser navigating sends Sec-Fetch-Dest: document and gets the rendered view
            headers = {"Sec-Fetch-Dest": "document"} if page else {}
            try:
                request = Request(f"{base}/{path}", headers=headers)  # noqa: S310
                with urlopen(request, timeout=40) as response:  # noqa: S310
                    return response.status, response.read()
            except HTTPError as exc:
                return exc.code, exc.read()

        for _attempt in range(12):
            status, body = get(f"{snapshot}/probe.txt")
            if status == 200:
                break
            time.sleep(6)
        if (status, body) != (200, marker.encode()):
            raise SystemExit(
                f"FAIL: snapshot content unavailable or incorrect (does the server serve "
                f"{args.org}? `serving the repositories of …` in its journal; README §3)"
            )
        for path in (
            f"{branch}/probe.txt",
            f"{snapshot}/auth.json",
            f"{snapshot}/%2e%2e/probe.txt",
        ):
            if get(path)[0] != 404:
                raise SystemExit("FAIL: branch SHA or denied path was served")
        for name, text in RENDERED.items():
            status, page = get(f"{snapshot}/{name}", page=True)
            expect = b'class="math inline"' if name.endswith(".md") else b'<tr id="L1">'
            if status != 200 or expect not in page:
                raise SystemExit(f"FAIL: {name} was not rendered")
            if get(f"{snapshot}/{name}?raw=1", page=True) != (200, text.encode()):
                raise SystemExit(f"FAIL: {name}?raw=1 did not return its bytes")
        site = args.url.rstrip("/")
        with urlopen(f"{site}/_zab/static/files.css", timeout=40) as response:  # noqa: S310
            if response.status != 200 or b".hl" not in response.read():
                raise SystemExit("FAIL: the rendered pages' stylesheet is missing")
        print("PASS: snapshot file served; branch SHA, tracked auth.json and traversal return 404")
        print(
            f"PASS: {len(RENDERED)} files rendered (Markdown with math, 8 languages); ?raw=1 exact"
        )
        print(f"Retained synthetic test ref: {ref} in {args.org}/{args.repo}")


if __name__ == "__main__":
    main()
