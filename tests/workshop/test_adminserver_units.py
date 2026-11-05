"""The admin server's users, units, IMDS rule and Caddyfile (docs/ADMIN.md §1.2, §1.3, §4.3;
issue #324). The checks that need systemd-analyze, systemd-sysusers, a user systemd manager,
nft, unshare or shellcheck skip where the tool is missing."""

import functools
import io
import os
import pathlib
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import uuid

import archive_check
import collect_exports
import pytest

from .checker import instance_slice

SERVER = pathlib.Path(__file__).resolve().parents[2] / "workshop" / "adminserver"
UNITS = sorted(SERVER.glob("*.service"))
SOCKETS = sorted(SERVER.glob("*.socket"))
PATHS = sorted(SERVER.glob("*.path"))
CADDY = SERVER / "caddy.service.d" / "zab-admin.conf"
NFTABLES = SERVER / "nftables.service.d" / "zab-admin-imds.conf"
PYTHON = "/opt/zab-admin/current/.venv/bin/python"
LOAD = "/usr/sbin/nft -f /etc/zab-admin/imds.nft"
#: nftables.service's start/reload and stop: each flush and the rule in one transaction
FLUSH_AND_LOAD = "/usr/sbin/nft -f /etc/zab-admin/nftables-imds.nft"
FLUSH_STOP = "/usr/sbin/nft -f /etc/zab-admin/nftables-stop-imds.nft"
IMDS = "169.254.169.254 fd00:ec2::254"
#: §1.2: every unit's hardening. The IMDS loader and Caddy keep one capability each.
HARDENING = {
    "ProtectSystem": "strict",
    "ProtectHome": "true",
    "PrivateTmp": "true",
    "NoNewPrivileges": "true",
    "UMask": "0077",
}
CAPABILITIES = {
    "zab-admin-imds.service": "CAP_NET_ADMIN",
    "zab-admin.conf": "CAP_NET_BIND_SERVICE",
    # #418: read and delete the request in zab-admin's 0700 directory, give the key its group
    "zab-admin-config.service": "CAP_CHOWN CAP_DAC_OVERRIDE",
}
IMDS_USERS = {"root", "zab-admin", "zab-starter"}


def settings(path: pathlib.Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            found.setdefault(key, []).append(value)
    return found


def run(*argv: str, **options) -> subprocess.CompletedProcess:
    """A fixed argument list, the tool by its full path."""
    tool = shutil.which(argv[0])
    assert tool, argv[0]
    return subprocess.run([tool, *argv[1:]], capture_output=True, text=True, **options)  # noqa: S603


def users() -> set[str]:
    lines = (SERVER / "sysusers.conf").read_text().splitlines()
    return {line.split()[1] for line in lines if line.startswith("u ")}


def test_the_units_are_the_ones_of_section_1_2():
    assert [u.name for u in UNITS] == [
        "zab-admin-bot.service",
        "zab-admin-collector.service",
        "zab-admin-config.service",
        "zab-admin-imds.service",
        "zab-admin-starter.service",
        "zab-archive-check@.service",
        "zab-console.service",
        "zab-waker.service",
    ]
    assert [s.name for s in SOCKETS] == ["zab-admin-starter.socket", "zab-archive-check.socket"]
    assert [p.name for p in PATHS] == ["zab-admin-config.path"]


#: The config helper's command (#437): the system interpreter deploy.sh builds the venv from,
#: never the venv's, which zab-build makes while third-party code runs (review of #438); without
#: site processing, so no `.pth` line runs as root; the release's src/ the only path beyond the
#: stdlib.
HELPER_PYTHON = "/usr/bin/python3"
HELPER_LAUNCH = (
    "import os, sys; sys.path.append(os.path.realpath('/opt/zab-admin/current/src')); "
    "from zab.admin.confighelper import main; sys.exit(main())"
)


def test_the_config_helper_starts_on_a_request_and_writes_only_its_paths():
    """#418, §2.9: the bot's request starts the root helper, which writes admin.toml, the key,
    the request's directory (to delete it) and its own state, and has no network. #437: the
    files root loads from /etc/zab-admin are read-only to it, the other secrets and the bot's
    state hidden (the deploy tests check these sets against what a bootstrap installs)."""
    watch = settings(SERVER / "zab-admin-config.path")
    assert watch["PathExists"] == ["/var/lib/zab-admin-config/requests/request.json"]
    assert watch["Unit"] == ["zab-admin-config.service"]
    helper = settings(SERVER / "zab-admin-config.service")
    assert helper["Type"] == ["oneshot"]
    assert helper["ExecStart"] == [f'{HELPER_PYTHON} -I -B -S -c "{HELPER_LAUNCH}"']
    deploy = (SERVER / "deploy.sh").read_text()
    assert re.search(rf"^  UV=\$OPT/uv/uv PYTHON={HELPER_PYTHON}$", deploy, re.M)  # the venvs'
    # no path the bot could swap (review of #421): requests/ lies in root's state directory
    assert helper["ReadWritePaths"] == ["/etc/zab-admin /var/lib/zab-admin-config"]
    assert "StateDirectory" not in helper  # tmpfiles.conf makes it, and requests/ inside
    read_only = [path for line in helper["ReadOnlyPaths"] for path in line.split()]
    etc = "/etc/zab-admin"
    assert read_only == [f"{etc}/{name}" for name in (
        "allowed_signers", "backup_recipients", "break-glass",  # the trust files
        "imds.nft", "nftables-imds.nft", "nftables-stop-imds.nft",  # what nftables loads
    )]  # fmt: skip
    assert helper["TemporaryFileSystem"] == [f"{etc}/secrets:ro"]  # every secret, hidden
    assert helper["BindPaths"] == [f"{etc}/secrets/bots"]  # but the bots' keys: the new one
    assert helper["InaccessiblePaths"] == ["-/var/lib/zab-admin"]  # the fleet key, ws/, …
    bot = settings(SERVER / "zab-admin-bot.service")
    assert bot["ReadWritePaths"] == ["/var/lib/zab-admin -/var/lib/zab-admin-config/requests"]
    assert helper["PrivateNetwork"] == ["true"] and helper["IPAddressDeny"] == ["any"]
    assert helper["RestrictAddressFamilies"] == ["AF_UNIX"]
    assert "Restart" not in helper  # a request's run, then nothing: it restarts no unit


@pytest.mark.parametrize("path", [*UNITS, CADDY], ids=lambda p: p.name)
def test_every_unit_is_hardened(path):
    found = settings(path)
    for key, value in HARDENING.items():
        assert found.get(key) == [value], key
    assert found["CapabilityBoundingSet"] == [CAPABILITIES.get(path.name, "")]
    assert "AmbientCapabilities" not in found or found["AmbientCapabilities"][-1] in (
        "",
        CAPABILITIES[path.name],
    )


def test_who_runs_what_and_who_reaches_imds():
    """§1.2: only root, zab-admin and zab-starter reach IMDS. The console, Caddy and the waker
    can't, each by the rule and by its own IP filter; the archive check has no network at all,
    and a user of its own per instance (#374)."""
    run_as = {u.name: settings(u).get("User", ["root"])[0] for u in UNITS}
    assert run_as == {
        "zab-admin-bot.service": "zab-admin",
        "zab-admin-collector.service": "zab-admin",
        "zab-admin-config.service": "root",
        "zab-admin-imds.service": "root",
        "zab-admin-starter.service": "zab-starter",
        "zab-archive-check@.service": "zab-ac-%i",
        "zab-console.service": "zab-console",
        "zab-waker.service": "zab-waker",
    }
    dynamic = {u.name for u in UNITS if settings(u).get("DynamicUser") == ["yes"]}
    assert dynamic == {"zab-archive-check@.service"}
    assert {run_as[u.name] for u in UNITS if u.name not in dynamic} - {"root"} <= users()
    rule = (SERVER / "imds.nft").read_text()
    sets = re.findall(r"daddr (\S+) meta skuid != \{ ([^}]*) \} counter reject", rule)
    assert [a for a, _ in sets] == IMDS.split()
    assert all({u.strip(' "') for u in s.split(",")} == IMDS_USERS for _, s in sets)
    console = settings(SERVER / "zab-console.service")
    assert (console["IPAddressDeny"], console["IPAddressAllow"]) == (["any"], ["localhost"])
    assert settings(SERVER / "zab-waker.service")["IPAddressDeny"] == [IMDS]
    caddy = settings(CADDY)
    assert caddy["IPAddressDeny"] == [IMDS]
    assert caddy["Requires"] == caddy["After"] == ["zab-admin-imds.service"]
    loader = settings(SERVER / "zab-admin-imds.service")
    assert loader["ExecStart"] == [LOAD] and "ExecStop" not in loader  # stopping keeps it
    assert "PartOf" not in loader  # nftables.service's drop-in reloads the rule instead
    assert settings(NFTABLES) == {
        "ExecStart": ["", FLUSH_AND_LOAD],
        "ExecReload": ["", FLUSH_AND_LOAD],
        "ExecStop": ["", FLUSH_STOP],
    }
    assert nft_lines("nftables-imds.nft") == [
        'include "/etc/nftables.conf"',
        'include "/etc/zab-admin/imds.nft"',
    ]
    assert nft_lines("nftables-stop-imds.nft") == [
        "flush ruleset",
        'include "/etc/zab-admin/imds.nft"',
    ]


def nft_lines(name: str) -> list[str]:
    lines = (SERVER / name).read_text().splitlines()
    return [line for line in lines if line and not line.startswith("#")]


def test_the_bot_stops_its_own_scripts():
    """PR 10: systemd's SIGTERM goes to the bot alone, which stops each running script's process
    group and records the apply as interrupted; SIGKILL still reaches whatever is left."""
    bot = settings(SERVER / "zab-admin-bot.service")
    assert bot["KillMode"] == ["mixed"] and bot["Restart"] == ["always"]
    assert int(bot["TimeoutStopSec"][0]) >= 30  # the runner's stop takes ~10 s


def test_each_process_sees_only_its_directory():
    for name, own in (("zab-console", "console"), ("zab-waker", "waker")):
        found = settings(SERVER / f"{name}.service")
        assert found["TemporaryFileSystem"] == ["/var/lib/zab-admin:ro"]
        assert found["BindReadOnlyPaths"] == [f"/var/lib/zab-admin/{own}"]
        assert found["InaccessiblePaths"] == ["-/etc/zab-admin/secrets"]
    console = settings(SERVER / "zab-console.service")
    assert console["BindPaths"] == ["/var/lib/zab-admin/console/tokens"]
    assert "ReadWritePaths" not in console
    starter = settings(SERVER / "zab-admin-starter.service")
    assert starter["StateDirectory"] == ["zab-starter"]
    assert starter["InaccessiblePaths"] == ["-/var/lib/zab-admin -/etc/zab-admin/secrets"]
    assert not {"ReadWritePaths", "BindPaths", "BindReadOnlyPaths"} & set(starter)
    assert settings(SERVER / "zab-admin-collector.service")["ReadWritePaths"] == [
        "/var/lib/zab-admin/console /var/lib/zab-admin/collector"
    ]


def test_the_starter_is_no_key_holders_user():
    """A process may read another of its uid's files through /proc/<pid>/root, whatever its own
    mounts hide (review on #329), so the starter, which takes the waker's input, must not share
    a uid with any process that holds a key, nor with the waker or the console."""
    holders = {"zab-admin-bot.service", "zab-admin-collector.service"}
    run_as = {u.name: settings(u)["User"][0] for u in UNITS if "User" in settings(u)}
    starter = run_as.pop("zab-admin-starter.service")
    assert starter == "zab-starter" and starter not in run_as.values()
    assert {run_as[name] for name in holders} == {"zab-admin"}
    assert settings(SERVER / "zab-admin-starter.service")["Group"] == ["zab-starter"]
    assert "zab-starter" in users()


def test_the_waker_writes_only_its_state_and_reaches_the_socket():
    """connect() needs no writable mount, so the waker gets none outside its own state."""
    waker = settings(SERVER / "zab-waker.service")
    assert not {"ReadWritePaths", "BindPaths"} & set(waker)
    socket = settings(SERVER / "zab-admin-starter.socket")
    assert socket["ListenStream"] == ["/run/zab-admin-starter/start.sock"]
    assert (socket["SocketUser"], socket["SocketGroup"], socket["SocketMode"]) == (
        ["root"],
        ["zab-waker"],
        ["0660"],
    )
    assert socket["DirectoryMode"] == ["0755"]  # root's: the waker can create nothing there
    assert settings(SERVER / "zab-admin-starter.service")["Requires"] == [
        "zab-admin-starter.socket"
    ]


def test_the_caddyfile_proxies_to_the_console():
    caddyfile = (SERVER / "Caddyfile").read_text()
    assert "admin off" in caddyfile and "<CONSOLE_HOST> {" in caddyfile
    # Caddy, not the console, holds a slow client (the review of #496, R2-1): the whole body
    # read before the request is passed on (Caddy 2.6's `buffer_requests`; 2.6.2 refuses the
    # newer `request_buffers`), and 10 s for its headers and its body
    proxy = re.search(r"^\treverse_proxy 127\.0\.0\.1:8090 \{\n\t\tbuffer_requests\n\t\}$",
                      caddyfile, re.M)  # fmt: skip
    assert proxy and "request_buffers" not in caddyfile
    timeouts = re.search(r"^\tservers \{\n\t\ttimeouts \{\n\t\t\tread_header 10s\n"
                         r"\t\t\tread_body 10s\n\t\t\}\n\t\}$", caddyfile, re.M)  # fmt: skip
    assert timeouts and "max_size 16KB" in caddyfile


def test_tmpfiles_owners_exist_and_reader_directories_are_setgid():
    entries = [
        line.split()
        for line in (SERVER / "tmpfiles.conf").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    known = users() | {"root", "-"}
    assert all(e[3] in known and e[4] in known for e in entries)
    modes = {e[1]: (e[0], e[2], e[3], e[4]) for e in entries if e[0] in "df"}
    assert modes["/var/lib/zab-admin"] == ("d", "0711", "zab-admin", "zab-admin")
    assert modes["/var/lib/zab-admin/console"] == ("d", "2750", "zab-admin", "zab-console")
    assert modes["/var/lib/zab-admin/console/tokens"] == ("d", "2770", "zab-admin", "zab-console")
    # the collector's own state (#489): the bot writes its !status reads there too
    assert modes["/var/lib/zab-admin/collector"] == ("d", "0700", "zab-admin", "zab-admin")
    assert modes["/var/lib/zab-admin/waker"] == ("d", "2750", "zab-admin", "zab-waker")
    # the waker's status file for the collector (PR 21): its state traversable, status/ setgid
    # zab-admin, the parent listed first and as the unit makes it, so systemd never re-chowns it
    assert modes["/var/lib/zab-waker"] == ("d", "0711", "zab-waker", "zab-waker")
    assert modes["/var/lib/zab-waker/status"] == ("d", "2750", "zab-waker", "zab-admin")
    paths = [e[1] for e in entries]
    assert paths.index("/var/lib/zab-waker") < paths.index("/var/lib/zab-waker/status")
    waker = settings(SERVER / "zab-waker.service")
    assert (waker["StateDirectory"], waker["StateDirectoryMode"]) == (["zab-waker"], ["0711"])
    assert " run --wait-for-config --config " in waker["ExecStart"][0]  # a fresh server idles
    # the starter's start log (#359), the same way: the bots' group reads it, and only the
    # starter writes it; the waker's user is in no group but its own (sysusers.conf)
    assert modes["/var/lib/zab-starter"] == ("d", "0711", "zab-starter", "zab-starter")
    assert modes["/var/lib/zab-starter/log"] == ("d", "2750", "zab-starter", "zab-admin")
    assert paths.index("/var/lib/zab-starter") < paths.index("/var/lib/zab-starter/log")
    starter = settings(SERVER / "zab-admin-starter.service")
    assert (starter["StateDirectory"], starter["StateDirectoryMode"]) == (["zab-starter"], ["0711"])
    lines = (SERVER / "sysusers.conf").read_text().splitlines()
    assert not [line for line in lines if line.split()[:1] in (["m"], ["g"])]
    assert modes["/etc/zab-admin/secrets"] == ("d", "0750", "root", "zab-admin")
    assert modes["/etc/zab-admin/secrets/bots"] == ("d", "0750", "root", "zab-admin")
    # roster_import.py --retired fails closed on a missing file (#317); `f` never truncates
    assert modes["/var/lib/zab-admin/retired-slugs.txt"] == ("f", "0600", "zab-admin", "zab-admin")
    # !new-workshop's request: the bot's alone, in the root helper's state (#418, #421)
    assert modes["/var/lib/zab-admin-config"] == ("d", "0755", "root", "root")
    assert modes["/var/lib/zab-admin-config/requests"] == ("d", "0700", "zab-admin", "zab-admin")
    assert paths.index("/var/lib/zab-admin-config") < paths.index(
        "/var/lib/zab-admin-config/requests"
    )
    assert ["h", "/var/lib/zab-admin/audit.jsonl", "-", "-", "-", "-", "+a"] in entries


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="no systemd-analyze")
def test_systemd_analyze_verify(tmp_path):
    """Each unit, with the drop-ins on stand-ins for Caddy's and nftables' units, verifies
    without a warning. Executables that exist only on the server become ones that exist here."""
    true = shutil.which("true")
    for unit in [*UNITS, *SOCKETS, *PATHS]:
        text = unit.read_text().replace(PYTHON, sys.executable)
        (tmp_path / unit.name).write_text(text.replace("/usr/sbin/nft", true))
    for drop_in in (CADDY, NFTABLES):
        stand_in = drop_in.parent.name.removesuffix(".d")
        (tmp_path / stand_in).write_text(
            f"[Unit]\nDescription=stand-in\n[Service]\nType=oneshot\nExecStart={true}\n"
        )
        (tmp_path / drop_in.parent.name).mkdir()
        text = drop_in.read_text().replace("/usr/sbin/nft", true)
        (tmp_path / drop_in.parent.name / drop_in.name).write_text(text)
    units = sorted(str(p) for p in tmp_path.iterdir() if p.is_file())
    result = run("systemd-analyze", "verify", "--man=no", *units)
    ours = [line for line in result.stderr.splitlines() if str(tmp_path) in line]
    assert result.returncode == 0 and not ours, result.stderr


def _user_manager() -> bool:
    if not (shutil.which("systemctl") and os.environ.get("XDG_RUNTIME_DIR")):
        return False
    return run("systemctl", "--user", "show-environment").returncode == 0


@pytest.mark.skipif(not _user_manager(), reason="no user systemd manager")
def test_nftables_start_reload_restart_and_stop_run_only_ours(tmp_path):
    """The drop-in on a user unit shaped like Ubuntu's nftables.service, whose own commands each
    flush alone (they write `flush`). Ours flush and load in one (`flush+imds`), or, to stop,
    keep the rule alone (`imds`). Every action must run ours and none of the stock commands."""
    state = tmp_path / "ruleset"
    write = lambda text: f"/bin/sh -c 'echo {text} >> {state}'"  # noqa: E731
    name = f"zab-test-{uuid.uuid4().hex[:12]}-nftables.service"
    units = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd" / "user"
    unit, drop_ins = units / name, units / f"{name}.d"
    units.mkdir(parents=True, exist_ok=True)
    flush = write("flush")
    unit.write_text(
        "[Unit]\nDescription=zab test: nftables.service's shape\n[Service]\nType=oneshot\n"
        f"RemainAfterExit=yes\nExecStart={flush}\nExecReload={flush}\nExecStop={flush}\n"
    )
    drop_ins.mkdir()
    ours = NFTABLES.read_text().replace(FLUSH_AND_LOAD, write("flush+imds"))
    (drop_ins / NFTABLES.name).write_text(ours.replace(FLUSH_STOP, write("imds")))
    try:
        run("systemctl", "--user", "daemon-reload", check=True)
        for action, want in (
            ("start", ["flush+imds"]),
            ("reload", ["flush+imds"]),
            ("restart", ["imds", "flush+imds"]),
            ("stop", ["imds"]),
        ):
            state.write_text("")
            run("systemctl", "--user", action, name, check=True, timeout=30)
            assert state.read_text().split() == want, action
    finally:
        run("systemctl", "--user", "stop", name)
        shutil.rmtree(drop_ins, ignore_errors=True)
        unit.unlink(missing_ok=True)
        run("systemctl", "--user", "daemon-reload")


@pytest.mark.skipif(not shutil.which("systemd-sysusers"), reason="no systemd-sysusers")
def test_sysusers_and_tmpfiles_parse(tmp_path, monkeypatch):
    (tmp_path / "etc").mkdir()
    run("systemd-sysusers", f"--root={tmp_path}", str(SERVER / "sysusers.conf"), check=True)
    assert {line.split(":")[0] for line in (tmp_path / "etc/passwd").read_text().splitlines()} == (
        users()
    )
    result = run(
        "systemd-tmpfiles", f"--root={tmp_path}", "--create", str(SERVER / "tmpfiles.conf")
    )
    # Unprivileged, only the chown and the chattr fail; any parse error or unknown user shows.
    if os.geteuid() != 0:
        errors = [line for line in result.stderr.splitlines() if line]
        assert errors and all("Operation not permitted" in line for line in errors), errors
    else:
        assert result.returncode == 0, result.stderr
    assert (tmp_path / "var/lib/zab-admin/console/tokens").is_dir()
    assert (tmp_path / "var/lib/zab-admin/retired-slugs.txt").read_text() == ""
    assert (tmp_path / "var/lib/zab-waker/status").is_dir()
    assert (tmp_path / "var/lib/zab-starter/log").is_dir()
    # the waker's bootstrap config: starter mode with no workshop, which the waker idles on
    from zab.waker import __main__ as waker

    config = tmp_path / "var/lib/zab-admin/waker/config.toml"
    info = config.stat()
    monkeypatch.setattr(waker.pwd, "getpwnam", lambda _: type("U", (), {"pw_uid": info.st_uid}))
    monkeypatch.setattr(waker.grp, "getgrnam", lambda _: type("G", (), {"gr_gid": info.st_gid}))
    loaded, entries = waker.load(config)
    assert (loaded["starter"], loaded["fleets"], entries) == (
        "/run/zab-admin-starter/start.sock",
        [],
        [],
    )
    assert loaded["status_file"] == "/var/lib/zab-waker/status/waker.json"


NFT_PROBE = """
import socket, subprocess, sys
rule, ip = sys.argv[1], sys.argv[2]
run = lambda *a: subprocess.run(a, check=True)
run("ip", "link", "set", "lo", "up")
run("ip", "addr", "add", "169.254.169.254/32", "dev", "lo")
run("ip", "-6", "addr", "add", "fd00:ec2::254/128", "dev", "lo", "nodad")
server = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET)
server.bind((ip, 80)); server.listen()
run("nft", "-f", rule)
try:
    socket.create_connection((ip, 80), timeout=5).close()
    print("open")
except ConnectionRefusedError:
    print("refused")
"""


def _nft_usable() -> bool:
    if not (shutil.which("nft") and shutil.which("unshare") and shutil.which("ip")):
        return False
    return run("unshare", "-rn", "nft", "list", "ruleset").returncode == 0


@pytest.mark.skipif(not _nft_usable(), reason="no nft, or no unprivileged network namespace")
@pytest.mark.parametrize("ip", IMDS.split())
@pytest.mark.parametrize(("allowed", "want"), [("root", "open"), ("bin", "refused")])
def test_the_imds_rule_refuses_other_users(tmp_path, ip, allowed, want):
    """In a private network namespace, where this test is root: the rule as shipped, with
    zab-admin swapped for a user that exists here, lets root through, and refuses it once root
    is swapped out of the set too."""
    rule = (
        (SERVER / "imds.nft")
        .read_text()
        .replace('"zab-admin"', '"nobody"')
        .replace('"zab-starter"', '"daemon"')
    )
    (tmp_path / "imds.nft").write_text(rule.replace('"root"', f'"{allowed}"'))
    rule_file = str(tmp_path / "imds.nft")
    result = run("unshare", "-rn", sys.executable, "-c", NFT_PROBE, rule_file, ip, timeout=30)
    assert result.stdout.strip() == want, result.stderr


TRANSACTIONS = """
import pathlib, subprocess, sys
d = pathlib.Path(sys.argv[1])
nft = lambda f: subprocess.run(["nft", "-f", str(d / f)], capture_output=True).returncode
tables = lambda: subprocess.run(["nft", "list", "tables"], capture_output=True, text=True).stdout
for step, f in (("boot", "imds.nft"), ("start", "start.nft"), ("reload", "start.nft"),
                ("stop", "stop.nft"), ("start", "start.nft"), ("broken", "start.nft")):
    if step == "broken":
        (d / "nftables.conf").write_text((d / "nftables.conf").read_text() + "garbage {")
    print(step, nft(f), " ".join(tables().split()))
"""


@pytest.mark.skipif(not _nft_usable(), reason="no nft, or no unprivileged network namespace")
def test_each_nftables_transaction_keeps_the_rule(tmp_path):
    """The two files nftables.service now loads, with their includes pointed here, and a stand-in
    for /etc/nftables.conf that flushes and adds its own table: start and reload keep both
    tables, stop keeps ours alone, and a broken nftables.conf is refused whole, ours kept."""
    rule = (
        (SERVER / "imds.nft")
        .read_text()
        .replace('"zab-admin"', '"nobody"')
        .replace('"zab-starter"', '"daemon"')
    )
    (tmp_path / "imds.nft").write_text(rule)
    (tmp_path / "nftables.conf").write_text(
        "flush ruleset\ntable inet filter {\n"
        " chain input { type filter hook input priority 0; }\n}\n"
    )
    for name, out in (("nftables-imds.nft", "start.nft"), ("nftables-stop-imds.nft", "stop.nft")):
        text = (
            (SERVER / name)
            .read_text()
            .replace("/etc/nftables.conf", str(tmp_path / "nftables.conf"))
        )
        (tmp_path / out).write_text(
            text.replace("/etc/zab-admin/imds.nft", str(tmp_path / "imds.nft"))
        )
    result = run("unshare", "-rn", sys.executable, "-c", TRANSACTIONS, str(tmp_path), timeout=30)
    both, ours = "table inet filter table inet zab_imds", "table inet zab_imds"
    assert result.stdout.splitlines() == [
        f"boot 0 {ours}",
        f"start 0 {both}",
        f"reload 0 {both}",
        f"stop 0 {ours}",
        f"start 0 {both}",
        f"broken 1 {both}",
    ], result.stderr


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="no shellcheck")
@pytest.mark.parametrize("script", sorted(SERVER.rglob("*.sh")), ids=lambda p: p.name)
def test_shellcheck(script):
    result = run("shellcheck", str(script))
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------- #374: the archive check's sandbox

CHECK, CHECK_SOCKET = SERVER / "zab-archive-check@.service", SERVER / "zab-archive-check.socket"
CHECKER = "/opt/zab-admin/current/workshop/admin/archive_check.py"


def test_the_archive_check_is_a_sandbox_per_connection():
    """#374: an instance per connection, as a user of its own, with no network, no access to
    the server's keys or state, nothing writable, and archive_check.py's own limits."""
    socket = settings(CHECK_SOCKET)
    assert socket["ListenStream"] == [collect_exports.CHECKER_SOCKET]
    assert (socket["SocketUser"], socket["SocketGroup"], socket["SocketMode"]) == (
        ["root"],
        ["zab-admin"],
        ["0660"],
    )
    assert socket["DirectoryMode"] == ["0755"] and socket["Accept"] == ["yes"]
    unit = settings(CHECK)
    assert unit["ExecStart"] == [f"{PYTHON} -I -S -B {CHECKER} serve --peer zab-admin"]
    assert (unit["DynamicUser"], unit["User"]) == (["yes"], ["zab-ac-%i"])
    assert unit["PrivateNetwork"] == ["true"] and unit["IPAddressDeny"] == ["any"]
    assert unit["RestrictAddressFamilies"] == ["none"]
    hidden = {"-/etc/zab-admin", "-/var/lib/zab-admin", "-/dev/shm", "-/run/dbus"}
    hidden |= {"-/run/systemd/resolve", "-/run/systemd/userdb", "-/run/zab-admin-starter"}
    assert hidden <= set(" ".join(unit["InaccessiblePaths"]).split())
    assert unit["ReadOnlyPaths"] == ["/tmp /var/tmp"]  # noqa: S108 - PrivateTmp's, read-only
    writable = {"ReadWritePaths", "BindPaths", "StateDirectory", "RuntimeDirectory"}
    assert not (writable | {"CacheDirectory", "LogsDirectory"}) & set(unit)
    assert unit["LimitAS"] == unit["MemoryMax"] == [f"{archive_check.MEMORY >> 20}M"]
    assert unit["LimitCPU"] == [str(archive_check.CPU_SECONDS)]
    assert unit["LimitNOFILE"] == [str(archive_check.OPEN_FILES)]
    assert unit["LimitFSIZE"] == unit["LimitCORE"] == ["0"]
    assert unit["CPUQuota"] == ["100%"]
    assert float(unit["RuntimeMaxSec"][0]) > collect_exports.CHECK_TIMEOUT
    filters = unit["SystemCallFilter"]
    assert filters[0] == "@system-service" and all(f.startswith("~") for f in filters[1:])
    denied = set(" ".join(filters[1:]).replace("~", "").split())
    assert {"@privileged", "@process", "@debug", "socket", "connect", "process_vm_readv"} <= denied
    assert {"@aio", "userfaultfd"} <= denied  # io_uring, past the socket ban (review of #384)
    socket = settings(CHECK_SOCKET)
    assert socket["MaxConnections"] == [str(collect_exports.MAX_CHECKS)]
    bot = settings(SERVER / "zab-admin-bot.service")
    assert "zab-archive-check.socket" in bot["Wants"][0].split()
    assert "zab-archive-check.socket" in bot["After"][0].split()


def _offline_security() -> bool:
    if not shutil.which("systemd-analyze"):
        return False
    return "--offline" in run("systemd-analyze", "--help").stdout


@pytest.mark.skipif(not _offline_security(), reason="no systemd-analyze security --offline")
def test_the_archive_checks_exposure_is_at_most_one(tmp_path):
    """`systemd-analyze security`, offline, on an instance: 0.4, "SAFE", on systemd 255. What
    remains is PrivateUsers= (it would show zab-admin to SO_PEERCRED as nobody), the host's root
    directory and the RTC device. At most 1.0, so that a lost setting shows."""
    (tmp_path / CHECK.name).write_text(CHECK.read_text().replace(PYTHON, sys.executable))
    (tmp_path / CHECK_SOCKET.name).write_text(CHECK_SOCKET.read_text())
    instance = str(tmp_path / "zab-archive-check@0-1234-998.service")
    args = ("security", "--offline=true", "--threshold=10", "--no-pager", instance)
    result = run("systemd-analyze", *args)
    exposure = re.search(r"Overall exposure level for \S+: (\d+\.\d+)", result.stdout)
    assert exposure and float(exposure[1]) <= 1.0, result.stdout[-3000:] + result.stderr
    assert result.returncode == 0, result.stderr


#: An instance in the checker's place, under the unit's sandbox: it answers, as its refusal,
#: what each attempt got (an errno's name, or "allowed").
PROBE = """
import errno, os, socket
def tried(call):
    try:
        call()
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
    return "allowed"
def fork():
    if os.fork() == 0:
        os._exit(0)
def write(path):
    with open(path, "wb") as file:
        file.write(b"x")
def io_uring():  # io_uring_setup (425 on x86_64 and aarch64): sockets without socket()
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(425, 1, ctypes.create_string_buffer(120)) < 0:
        raise OSError(ctypes.get_errno(), "io_uring_setup")
seen = [
    tried(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)),
    tried(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM)),
    tried(fork),
    tried(lambda: write("/tmp/zab-probe")),
    tried(lambda: write("/var/tmp/zab-probe")),
    tried(lambda: write("/dev/shm/zab-probe")),
    tried(io_uring),
    tried(lambda: os.stat("/run/dbus/system_bus_socket")),
]
text = " ".join(seen).encode()
os.write(3, b"R" + len(text).to_bytes(4, "big") + text)
"""


def user_mode_service() -> list[str]:
    """The [Service] settings of zab-archive-check@.service as a user manager can run them: no
    DynamicUser or User (PrivateUsers instead, which a user manager needs for the namespaces),
    and ProtectHome=read-only, so that a checkout under /home is reachable."""
    lines = CHECK.read_text().split("[Service]\n", 1)[1].splitlines()
    kept = [
        line.replace("ProtectHome=true", "ProtectHome=read-only")
        for line in lines
        if line and not line.startswith(("#", "DynamicUser=", "User=", "ExecStart="))
    ]
    return [*kept, "PrivateUsers=yes"]


#: What the unit asks of the kernel that a user manager may not give: namespaces, mounts and
#: filters, each on /bin/true. A fixed list, never the unit's own settings, so that a unit that
#: runs nothing fails the test, not skips it (the review of #384).
FEATURES = [
    *("PrivateTmp=yes", "PrivateDevices=yes", "PrivateNetwork=yes", "PrivateIPC=yes"),
    *("ProtectSystem=strict", "ProtectHome=read-only", "ProtectProc=invisible", "ProcSubset=pid"),
    *("ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectKernelLogs=yes"),
    *("ProtectControlGroups=yes", "ProtectClock=yes", "ProtectHostname=yes"),
    *("RestrictNamespaces=yes", "LockPersonality=yes", "MemoryDenyWriteExecute=yes"),
    *("RestrictRealtime=yes", "SystemCallArchitectures=native", "SystemCallFilter=@system-service"),
    *("InaccessiblePaths=-/run", "ReadOnlyPaths=/tmp", "BindReadOnlyPaths=/usr"),
]


def _true_runs(*cases: tuple[str, ...]) -> list[bool]:
    """Whether this user manager runs /bin/true with PrivateUsers and each case's settings, a
    unit for each: all loaded by one `daemon-reload`, which takes up to a second (#473)."""
    units = pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd" / "user"
    units.mkdir(parents=True, exist_ok=True)
    names = [f"zab-test-{uuid.uuid4().hex[:12]}-precheck.service" for _ in cases]
    for name, settings in zip(names, cases, strict=True):
        lines = ["[Service]", "Type=oneshot", "ExecStart=/bin/true", "PrivateUsers=yes", *settings]
        (units / name).write_text("\n".join(lines) + "\n")
    try:
        run("systemctl", "--user", "daemon-reload", check=True)
        return [run("systemctl", "--user", "start", n, timeout=60).returncode == 0 for n in names]
    finally:
        for name in names:
            run("systemctl", "--user", "reset-failed", name)
            (units / name).unlink(missing_ok=True)
        run("systemctl", "--user", "daemon-reload")


@functools.cache
def _no_sandbox_here() -> str:
    """Why this user manager can't build the unit's sandbox at all, or "": then the checker's
    test under the unit proves nothing, and is skipped with the features it lacks. (GitHub's
    Ubuntu 24.04 runners restrict unprivileged user namespaces.)"""
    if not _user_manager():
        return "no user systemd manager"
    if _true_runs(tuple(FEATURES)) == [True]:
        return ""
    if _true_runs(()) == [False]:
        return "this user manager can't make a user namespace (PrivateUsers=yes)"
    ran = _true_runs(*((feature,) for feature in FEATURES))
    lacking = [feature for feature, ok in zip(FEATURES, ran, strict=True) if not ok]
    return "this user manager can't build: " + (" ".join(lacking) or "all of them at once")


@pytest.mark.parametrize("what", ["re-pack", "probe"])
def test_the_archive_check_runs_in_its_sandbox(tmp_path, what):
    """The units as shipped, under this user's systemd manager, but for what only PID 1 can do:
    no DynamicUser or socket owner (PrivateUsers instead, which a user manager needs for the
    namespaces), and this checkout, under /home, readable (ProtectHome=read-only). A re-pack
    runs under the system call filter, RestrictAddressFamilies=none, MemoryDenyWriteExecute
    and the limits; a probe in the checker's place makes no socket, no process, no file."""
    if why := _no_sandbox_here():
        pytest.skip(why)
    name = f"zab-test-{uuid.uuid4().hex[:12]}-check"
    runtime = pathlib.Path(os.environ["XDG_RUNTIME_DIR"])
    units, here = runtime / "systemd" / "user", runtime / name
    units.mkdir(parents=True, exist_ok=True)
    here.mkdir()
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "probe.py").write_text(PROBE)
    me = pwd.getpwuid(os.getuid()).pw_name
    python = os.path.realpath(sys.executable)
    command = f"{python} -I -S -B {archive_check.__file__} serve --peer {me}"
    if what == "probe":
        command = f"{python} -I -S -B {probe / 'probe.py'}"
    # this checkout and interpreter may be under /tmp, which PrivateTmp hides, or /run: bound
    # read-only into the unit, in the tests only, so that they run from anywhere
    shown = [os.path.realpath(sys.base_prefix), str(pathlib.Path(archive_check.__file__).parent)]
    bind = "BindReadOnlyPaths=" + " ".join([*shown, str(probe)])
    unit = CHECK.read_text().split("[Service]\n", 1)[0].replace("zab-archive-check", name)
    service = "\n".join(
        [unit + "[Service]", f"ExecStart={command}", *user_mode_service(), bind, ""]
    )
    socket = CHECK_SOCKET.read_text().replace(collect_exports.CHECKER_SOCKET, str(here / "s"))
    socket = "\n".join(
        line for line in socket.splitlines() if not line.startswith(("SocketUser", "SocketGroup"))
    )
    (units / f"{name}@.service").write_text(service)
    (units / f"{name}.socket").write_text(socket.replace("SocketMode=0660", "SocketMode=0600"))
    source = tmp_path / "x.tar.gz"
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for member, body in (("work/a.md", b"alpha"), ("work/noise", os.urandom(3 << 20))):
            info = tarfile.TarInfo(member)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    source.write_bytes(data.getvalue())
    try:
        run("systemctl", "--user", "daemon-reload", check=True)
        run("systemctl", "--user", "start", f"{name}.socket", check=True)
        with source.open("rb") as archive, (tmp_path / "new.tar.gz").open("wb") as dest:
            got = collect_exports.repack_file(archive, dest, checker=str(here / "s"))
        journal = run("journalctl", "--user", "--no-pager", "-o", "cat", "-u", f"{name}@*").stdout
    finally:
        run("systemctl", "--user", "stop", f"{name}.socket", instance_slice(name))
        (units / f"{name}@.service").unlink(missing_ok=True)
        (units / f"{name}.socket").unlink(missing_ok=True)
        run("systemctl", "--user", "daemon-reload")
        shutil.rmtree(here, ignore_errors=True)
    want = ("EPERM EPERM EPERM EROFS EROFS EACCES EPERM EACCES: deleted", "")
    if got != (want if what == "probe" else ("", "")):
        pytest.fail(f"{got}\nthe instance's journal:\n{journal}")
    if what == "probe":
        return
    with tarfile.open(tmp_path / "new.tar.gz") as tar:
        assert tar.getnames() == ["work/a.md", "work/noise"]


def test_the_archive_check_never_hides_run_or_run_systemd_whole():
    """A19 on the rig (#392): with InaccessiblePaths=-/run, every instance exited
    226/NAMESPACE under PID 1 (systemd 255.4, arm64), and so did -/run/systemd: PID 1 needs
    /run/systemd to set the unit up. A user manager runs such a unit regardless, so the
    sandbox test can't see it; this one can. Only paths below /run/systemd may be hidden."""
    unit = settings(CHECK)
    paths = {p.lstrip("-+") for p in " ".join(unit["InaccessiblePaths"]).split()}
    paths |= {
        p.split(":")[0].lstrip("-+") for p in " ".join(unit.get("TemporaryFileSystem", [])).split()
    }
    for whole in ("/", "/run", "/run/", "/run/systemd", "/run/systemd/"):
        assert whole not in paths, whole
    assert {"/run/dbus", "/run/systemd/resolve"} <= paths  # the sockets still hidden
