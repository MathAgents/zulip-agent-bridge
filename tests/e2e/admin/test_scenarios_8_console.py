"""A11, the console (docs/ADMIN.md §7, §4; issue #489), on the rig.

The rig's admin.toml names `console_url = "https://admin-e2e.localhost"` (`harness/admin_toml`):
Caddy treats a `.localhost` name as internal, with a certificate of its own CA, and the server
resolves it to itself. The scenario asks the console on `127.0.0.1:8090` over SSH, with `curl`
(Q4: no DNS record for a test console), sends the session cookie itself, as a browser would to
the console's host, and reads the server's files as root. It runs before A9, which ends the
fake participants' VMs (by file name: `8_console` before `8_the_end`).

It needs A2's roster and A4's VMs (`requires`): every run's setup empties the rosters.

1. `zab-tester` (a global admin) sends `!console` in `#zab-dev-organizers`: the bot answers
   there that it sent the link by DM, and the DM has the link, for the console's host. A `GET
   /login`, as Zulip's previewer would make it, consumes nothing. The link signs in once: a
   snapshot written during the scenario, from a successful EC2 read, has each roster row with a
   VM as a participant VM with its instance and a state, and the page of `zab-admin-e2e` links
   each. Each VM whose last read there has the disk's sizes (#552) shows them in its own row
   and on its page, `<used> of <size> GB (<n>%)`, as its snapshot values give them (the page
   and the snapshot it was rendered from). The link's second use is refused; `POST /` is 405;
   the bot records `console_login`.
2. The bots restarted (fresh answer windows, §2.1): `zab-tester2`'s `!console` by DM to
   `Workshop admin · e2e`, whose workshop they don't organize, is refused (`🚫`), with no token
   made. Through `Workshop admin · e2e2`, which they organize, their link signs them in to
   `zab-admin-e2e2` alone: `zab-admin-e2e`'s page answers 404.
3. As `zab-console`, `secrets/` and a key in it can't be listed ("Permission denied"), while the
   snapshot can be read; the console's files have their modes; both units run.
4. Through Caddy: while 40 clients trickle `POST /login` bodies to it, a fresh `GET /login`
   answers 200 (Caddy reads a whole body before it passes the request on: the review of #496,
   R2-1); the sign-in page answers through Caddy at all.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from typing import Any

import pytest

from e2e.admin.conftest import requires
from e2e.admin.harness.chat import READ_S
from e2e.admin.harness.rig import E2E, E2E2
from e2e.admin.test_scenarios_2_bot import last_seq, records, restart, talk
from e2e.harness.zulip_user import dm_narrow
from zab.admin import pages

CONSOLE = "http://127.0.0.1:8090"
HOST = "admin-e2e.localhost"  # the rig's console_url (harness/admin_toml.py)
LINK = re.compile(rf"https://{re.escape(HOST)}/login#([0-9a-f]{{64}})")
TOKENS = "/var/lib/zab-admin/console/tokens"
SNAPSHOT = "/var/lib/zab-admin/console/snapshot.json"
#: Run on the server as `python3 -I -c HELD HOST`: 40 TLS clients trickle a POST to Caddy, then
#: one fresh GET; prints its status line (the review of #496, R2-1).
HELD = """
import socket, ssl, sys, time
host = sys.argv[1]
ctx = ssl.create_default_context()
ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
def tls():
    raw = socket.create_connection(("127.0.0.1", 443), timeout=20)
    return ctx.wrap_socket(raw, server_hostname=host)
held = []
for _ in range(40):
    sock = tls()
    sock.sendall(b"POST /login HTTP/1.1\\r\\nHost: " + host.encode() + b"\\r\\n"
                 b"Content-Length: 4096\\r\\n\\r\\nt")
    held.append(sock)
time.sleep(2)
sock = tls()
sock.sendall(b"GET /login HTTP/1.1\\r\\nHost: " + host.encode()
             + b"\\r\\nConnection: close\\r\\n\\r\\n")
print(sock.recv(200).decode("latin-1").split("\\r\\n", 1)[0])
"""


def ask(server: Any, method: str, path: str, *, cookie: str = "", data: str | None = None):
    """`curl` on the server against the console: (status, headers, body)."""
    argv = ["curl", "-sS", "-m", "20", "-D", "-", "-X", method, f"{CONSOLE}{path}"]
    argv += ["-H", f"Cookie: {cookie}"] if cookie else []
    argv += ["--data-binary", "@-"] if data is not None else []
    done = server.run(argv, input=data, check=False, timeout=60)
    assert done.ok, f"curl {method} {path}: {done.stderr.strip()[-200:]}"
    head, _, body = done.stdout.replace("\r\n", "\n").partition("\n\n")
    status = int(head.split("\n", 1)[0].split()[1])
    headers = {}
    for line in head.split("\n")[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return status, headers, body


def sign_in(server: Any, token: str) -> str:
    status, headers, _ = ask(server, "POST", "/login", data=f"token={token}")
    assert status == 303 and headers.get("location") == "/", (status, headers)
    cookie = headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    return cookie.split(";", 1)[0]


async def link_by_dm(user: Any, bot: int, after: int) -> str:
    dm = await user.wait_reply(dm_narrow(bot), after_id=after, senders=[bot], within=READ_S,
                               pattern=r"^Your console sign-in link")  # fmt: skip
    found = LINK.search(dm["content"])
    assert found, f"no link for {HOST} in the DM: {dm['content'][:200]}"
    return found[1]


async def logged(server: Any, after: int, user_id: int) -> dict[str, Any]:
    """The `console_login` record of `user_id` after `after`: the bot's tick writes it."""
    for _ in range(24):
        for record in records(server, after):
            if record["event"] == "console_login" and record["requester"]["id"] == user_id:
                return record
        await asyncio.sleep(5)
    raise AssertionError(f"no console_login record for {user_id}")


def vms(server: Any) -> list[str]:
    """The slugs of zab-admin-e2e's roster rows with a VM (an `ip`)."""
    text = server.read(f"/var/lib/zab-admin/ws/{E2E.name}/roster.csv")
    return [r["slug"] for r in csv.DictReader(io.StringIO(text)) if (r.get("ip") or "").strip()]


def disk_labels(snapshot: dict[str, Any]) -> dict[str, str]:
    """`<used> of <size> GB (<n>%)` (#552) for each of zab-admin-e2e's VMs whose last status read
    in `snapshot` has the disk's sizes (status.py's `disk`: a `!status` since the release was
    deployed), from its bytes, in binary GB as `pages.disk_gb` gives them."""
    out = {}
    for vm in snapshot["workshops"][E2E.name]["vms"]:
        status = vm.get("status") or {}
        space, percent = pages.disk_space(status.get("disk")), status.get("disk_percent")
        if vm.get("state") != "no VM" and space is not None and type(percent) is int:
            used, size = pages.disk_gb(space[1]), pages.disk_gb(space[0])
            out[vm["slug"]] = f"{used} of {size} GB ({percent}%)"
    return out


def row_of(page: str, slug: str) -> str:
    """The workshop page's table row that links the VM's page; "" without one."""
    link = re.escape(f'href="/w/{E2E.name}/vm/{slug}"')
    found = re.search(rf"<tr>[^\n]*{link}[^\n]*</tr>", page)
    return found[0] if found else ""


def pages_and_snapshot(server: Any, cookie: str, slugs: list[str]):
    """The workshop's page, each VM's page, and the snapshot they were rendered from: the
    console reads `snapshot.json` at each request, so one written between the reads before and
    after them (every minute) means another try."""
    for _ in range(5):
        before = json.loads(server.read(SNAPSHOT))["at"]
        status, headers, page = ask(server, "GET", f"/w/{E2E.name}/", cookie=cookie)
        own = {slug: ask(server, "GET", f"/w/{E2E.name}/vm/{slug}", cookie=cookie)[2]
               for slug in slugs}  # fmt: skip
        snapshot = json.loads(server.read(SNAPSHOT))
        if snapshot["at"] == before:
            return status, headers, page, own, snapshot
    pytest.fail("the snapshot changed during each of 5 reads of the pages")


def shown(server: Any, expected: list[str], since: float) -> str | None:
    """Why the snapshot doesn't show `expected` as zab-admin-e2e's VMs yet; None when it does:
    written after `since`, from an EC2 read that didn't fail, each slug a participant row with a
    state other than "no VM" and an instance."""
    snapshot = json.loads(server.read(SNAPSHOT))
    ws = snapshot["workshops"][E2E.name]
    if snapshot["at"] <= since:
        return f"the snapshot is from {snapshot['written_at']}, before the scenario"
    if ws["ec2"]["error"] is not None:
        return f"its EC2 read failed: {ws['ec2']['error']}"
    rows = {vm["slug"]: vm for vm in ws["vms"] if vm["kind"] == "participant"}
    for slug in expected:
        vm = rows.get(slug, {})
        if vm.get("state") in (None, "no VM") or not vm.get("instance"):
            return f"{slug}: {vm.get('state')!r}, instance {vm.get('instance')!r}"
    return None


@pytest.mark.admin_e2e
@pytest.mark.admin_scenario("A11")
@requires("roster", "launched")
async def test_a11_console(scenario, tester, tester2, rigged, server, nonce):
    ids = rigged.ids()
    e2e_bot, e2e2_bot = ids.bots[E2E.bot][0], ids.bots[E2E2.bot][0]
    seq = last_seq(server)
    since = float(server.run(["date", "+%s"]).stdout.strip())  # the server's clock
    # 1. zab-tester, a global admin: the link by DM, one sign-in
    organizers = talk(tester, rigged, E2E.name, f"A11 console {nonce}")
    sent = await organizers.say("!console")
    await organizers.reply(sent, r"^sent you a sign-in link by DM")
    token = await link_by_dm(tester, e2e_bot, sent)
    assert ask(server, "GET", "/login")[0] == 200  # a previewer's fetch consumes nothing
    cookie = sign_in(server, token)
    expected, why = vms(server), "not read"
    assert expected, "no roster row of zab-admin-e2e has a VM: A4 first"
    for _ in range(18):  # the collector writes a snapshot a minute
        why = shown(server, expected, since)
        if why is None:
            break
        await asyncio.sleep(10)
    assert why is None, why
    status, headers, page, own, snapshot = pages_and_snapshot(server, cookie, expected)
    assert status == 200, status
    assert all(f"/w/{E2E.name}/vm/{slug}" in page for slug in expected), expected
    assert headers["content-security-policy"].startswith("default-src 'none';")
    scenario.notes.append(f"the console's page of {E2E.name} shows its VMs: {expected}")
    # #552: each VM read with the disk's sizes (A1's, A6's `!status`) shows its own, in its row
    # of the fleet and on its page, as its snapshot values give them
    labels = disk_labels(snapshot)
    for slug, label in labels.items():
        shown_label = f'<span class="num">{label}</span>'
        assert shown_label in row_of(page, slug), f"{slug}'s row: {row_of(page, slug)[:500]}"
        assert shown_label in own.get(slug, ""), f"{slug}'s page has no {label}"
    scenario.notes.append(f"disks in GB (#552): {labels or 'no read with the sizes yet'}")
    again = ask(server, "POST", "/login", data=f"token={token}")
    assert again[0] == 403 and "used already" in again[2], again[0]
    assert ask(server, "POST", "/", cookie=cookie)[0] == 405
    record = await logged(server, seq, ids.tester)
    assert record["workshop"] == E2E.name and record["bot"] == e2e_bot
    # 2. zab-tester2: refused by e2e's bot; signed in to e2e2 alone through e2e2's bot
    await restart(server, talk(tester, rigged, "global", f"A11 console {nonce}"))
    seq = last_seq(server)
    before = server.run(["sudo", "ls", TOKENS]).stdout.split()
    sent = await tester2.send_dm(e2e_bot, "!console")
    await tester2.wait_reply(dm_narrow(e2e_bot), after_id=sent, senders=[e2e_bot],
                             within=READ_S, pattern=r"^🚫 .* is not an organizer here")  # fmt: skip
    after = server.run(["sudo", "ls", TOKENS]).stdout.split()
    assert set(after) <= set(before), after  # no token made (the bot may delete stale ones)
    sent = await tester2.send_dm(e2e2_bot, "!console")
    second = sign_in(server, await link_by_dm(tester2, e2e2_bot, sent))
    index = ask(server, "GET", "/", cookie=second)[2]
    assert f"/w/{E2E2.name}/" in index and f"/w/{E2E.name}/" not in index
    assert ask(server, "GET", f"/w/{E2E.name}/", cookie=second)[0] == 404
    assert ask(server, "GET", f"/w/{E2E2.name}/", cookie=second)[0] == 200
    record = await logged(server, seq, ids.tester2)
    assert record["workshop"] == E2E2.name and record["bot"] == e2e2_bot
    # 3. what zab-console can't read; the files and units
    for path in ("/etc/zab-admin/secrets", "/etc/zab-admin/secrets/github-token"):
        denied = server.run(["sudo", "-u", "zab-console", "ls", "--", path], check=False)
        assert not denied.ok and "Permission denied" in denied.stderr, (path, denied.stderr)
    read = server.run(["sudo", "-u", "zab-console", "cat", "--", SNAPSHOT], check=False)
    assert read.ok and read.stdout.startswith("{"), "zab-console can't read the snapshot"
    for path, want in (
        ("/var/lib/zab-admin/console/snapshot.json", "zab-admin:zab-console 640"),
        ("/var/lib/zab-admin/console/audit-view.jsonl", "zab-admin:zab-console 640"),
        ("/var/lib/zab-console/secret", "zab-console:zab-console 600"),
    ):
        got = server.run(["sudo", "stat", "-c", "%U:%G %a", "--", path]).stdout.strip()
        assert got == want, (path, got)
    units = ["zab-console.service", "zab-admin-collector.service"]
    active = server.run(["systemctl", "is-active", *units], check=False).stdout.split()
    assert active == ["active", "active"], active
    # 4. Through Caddy: a fresh request answers while trickled POSTs are held (R2-1)
    caddy = server.run(["curl", "-sk", "-m", "20", "-o", "/dev/null", "-w", "%{http_code}",
                        "--resolve", f"{HOST}:443:127.0.0.1", f"https://{HOST}/login"],
                       check=False)  # fmt: skip
    assert caddy.stdout.strip() == "200", f"GET https://{HOST}/login: {caddy.stdout.strip()}"
    held = server.run(["python3", "-I", "-c", HELD, HOST], check=False, timeout=120)
    assert held.ok and held.stdout.strip().endswith("200 OK"), (held.stdout, held.stderr[-300:])
    scenario.notes.append(f"through Caddy, with 40 POSTs held, a fresh GET: {held.stdout.strip()}")
