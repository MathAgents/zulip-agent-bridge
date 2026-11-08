#!/usr/bin/env python3
"""trust_fix_remote.py [--apply]: one VM's trust stores without a distrusted root (#816), and
Claude Code's own CA list on the system store (#882).

`trust_fix.py` runs it on a participant VM as root, read from stdin, with `TRUST_STORES` set to
the base64 of `workshop/ami/files/bin/trust_stores.py`, whose check it uses, and `APT_REPAIR` to
that of `workshop/admin/apt_repair.py`, which step 1 runs first (#947). Standard library
only: the VM's system Python runs it. Without `--apply` it changes nothing and says what it
would do. Each step acts only where the VM differs from the image, in this order, so that each
download goes through a store already fixed:

1. **the system store:** `ca-certificates` older than `CA_FLOOR`: `apt-get update`, then
   `install --only-upgrade ca-certificates`. First, the release's `apt_repair.py`, as a rollout
   runs it (`step_apt`, #947): an image whose Macaulay2 source is Launchpad's expired snapshot,
   the Oct 19 image's, fails every `apt-get update` until that source goes, and the rollout
   that removes it comes after the trust fix. Its line is the report's `apt source` step,
   `FAILED: …` when it exits non-zero; whatever it says, the update runs, and one that still
   fails stops step 1 as it always has. apt waits up to `APT_LOCK_WAIT_S` for its lock
   (unattended upgrades hold it after a boot) and is killed only after `APT_TIMEOUT_S`, well
   beyond, so that a lock freed late doesn't end with apt killed mid-install;
2. **uv,** through which `!rollout` installs the bridge: when `uv` or `uvx` is older than 0.13.4,
   whose built-in roots hold the root, both are replaced by deploy.sh's pin, downloaded with
   curl (the system store) and checked against its SHA-256;
3. **certifi** in `~/venv`, `/opt/certbot` and every uv tool's environment that has one
   (lean-lsp-mcp's, the bridge's, any other), older than uv.lock's release: that release, by
   its two hashes (`uv pip install --require-hashes`). A rollout's `uv tool install --force`
   re-resolves the bridge's dependencies anyway, certifi's newest among them, so it neither
   undoes nor trips on this; the bridge's environment is done here so that the check doesn't
   wait for a rollout;
4. **with Sage,** miniforge's `base` and `sage`, when their `ssl/` or a bundle that their
   certifi or pip's vendored certifi returns holds a distrusted root: `conda install -p` of
   build.sh's three floors (step 9), with conda trusting the system store. conda also
   updates openssl (its `aggressive_update_packages`), and its solver may move others. conda
   has no timeout here: a kill would reach sudo, not conda;
5. **Claude Code's own CA list** (#882): `CLAUDE_CODE_CERT_STORE=system` merged into the managed
   settings' `env`, as build.sh and the first boot write it, every other key kept, the file
   replaced whole. The requests that take that list then trust the system store alone, not
   also Claude Code's bundled roots; a request without it, such as a plain `fetch`, still gets
   those, G3 among them until a pin of 2.1.304, or of a later release byte-searched the same
   way (#859). The step changes only a file whose `CLAUDE_CODE_CERT_STORE` isn't `system`
   (unset, or another value), and creates one where there is none (`absent`, the image's other
   keys missing too). A file that isn't a JSON object, or whose `env` isn't one, is left as it
   is: `FAILED`. Nothing is downloaded, so nothing blocks it;
6. **the check:** build.sh's smoke test's stores (step 13), and uv's and uvx's versions; the
   managed settings' `CLAUDE_CODE_CERT_STORE`, and a certificate that agent reads from the
   system store, without which Claude Code falls back to its bundled roots.

A step whose download would go through a store that still holds a distrusted root is
`blocked`, and installs nothing. uv's and conda's go through the system store, so they are
blocked while it still holds the root (step 1 failed, say on apt's lock while unattended
upgrades run). certifi's goes through uv's own roots, so it is blocked while those do (step 2
failed). A rerun does them once the earlier step has worked. A step that ends `FAILED`, an
exception included, stops none of the others.

**Left:** the check reads more than the steps fix: all of `~/venv` and the uv tools, and a
uv-managed CPython. A distrusted root found there in anything but certifi's own bundle is
software an agent installed, which no step installs (`left_by_agents`): it is reported under
`left` and doesn't fail `ok`. The deployer decides (README.md §10). With `--apply
--upgrade-left-pips`, the pips among it are upgraded too, through uv once its roots are clean.

**One run at a time:** each run, a dry run included, holds `LOCK` (`flock`) for as long as it
lives. A dropped SSH doesn't end it, and every command that changes something runs with no
timeout of its own, since a timeout would kill sudo and leave the command running past the lock.
A run that can't take the lock does nothing and reports `running`. An `--apply` holds off idle
stop (`HOLD`) from inside its lock: written once the lock is taken, renewed before each step,
removed before the lock goes, by this process alone.

The last line printed is a JSON object: each step's outcome, the check's lines, `left`, and
`ok`, whether the check passed but for what is left.
"""

import base64
import fcntl
import hashlib
import json
import os
import platform
import pwd
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

AGENT = "agent"
HOME = Path("/home/agent")
BIN = HOME / ".local" / "bin"
UV_FLOOR = (0, 13, 4)  # GHSA-hq85-5cpj-mr62: the first uv without emSign ECC Root CA - G3
UV_VERSION = "0.13.7"  # deploy.sh's pin, with its SHA-256s (DECISIONS, Pins)
UV_SHA256 = {
    "x86_64": "c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83",
    "aarch64": "71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc",
}
UV_URL = "https://github.com/astral-sh/uv/releases/download/{0}/uv-{1}-unknown-linux-gnu.tar.gz"
CA_FLOOR = "20260816ubuntu0.24.04.1"  # USN-8944-1
APT_LOCK_WAIT_S = 300  # `DPkg::Lock::Timeout`
APT_TIMEOUT_S = 900  # the kill: the lock's wait, then the install's own time
CERTIFI = "2026.10.23"  # uv.lock's, with its wheel's and its sdist's hashes
CERTIFI_HASHES = (
    "sha256:e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1",
    "sha256:a9895e6e2758b371e0545e321a3a8fd1fcfd9829ada8824a936dfb16aaaf36cd",
)
SYSTEM_CERTS = Path("/etc/ssl/certs")
TOOLS = HOME / ".local" / "share" / "uv" / "tools"
PYTHONS = HOME / ".local" / "share" / "uv" / "python"  # a uv-managed CPython's pip, if any
ENVS = {"venv": (HOME / "venv", AGENT), "certbot": (Path("/opt/certbot"), "root")}
CONDA = HOME / "miniforge"
CONDA_ENVS = {"base": CONDA, "sage": CONDA / "envs" / "sage"}
CONDA_SPECS = ("ca-certificates>=2026.10.23", "certifi>=2026.10.23", "pip>=26.2.2")
SYSTEM_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
#: One run at a time on a VM (`take_lock`): root's, on /run, so gone at a reboot like its holder.
LOCK = "/run/zab-trust-fix.lock"
#: Idle stop's hold (`idle_stop.HOLD`, `admin_common.IDLE_HOLD_ON_CMD`): an epoch expiry.
HOLD = "/run/zab-idle-stop.hold"
HOLD_S = 3600
HOLDING = False  # this run, an --apply, holds it (`hold_on`)
WHERE_PY = (
    "import certifi; from pip._vendor import certifi as v; print(certifi.where()); print(v.where())"
)
VERSION_PY = "import importlib.metadata as m; print(m.version('certifi'))"
#: Claude Code's managed settings, and the key step 5 merges into their `env` (#882).
MANAGED = Path("/etc/claude-code/managed-settings.json")
CERT_STORE = "CLAUDE_CODE_CERT_STORE"
#: As agent: a certificate in the system bundle, and hashed ones beside it. The image's smoke
#: test runs the same (build.sh's `SYSTEM_STORE`).
AGENT_READS_STORE = (
    'grep -q -e "-----BEGIN CERTIFICATE-----" /etc/ssl/certs/ca-certificates.crt'
    ' && ls /etc/ssl/certs | grep -Eqx "[0-9a-f]{8}\\.[0-9]+"'
)
TRUST_STORES = ""  # trust_fix.py replaces this line
APT_REPAIR = ""  # trust_fix.py replaces this line


def run(
    argv: list[str],
    *,
    user: str = "root",
    env: dict[str, str] | None = None,
    timeout: int | None = 300,
) -> subprocess.CompletedProcess[str]:
    """ARGV as USER, from /, never through a shell; one that can't start or ends late: 124. A
    timeout kills only what was started: as USER, that is sudo, and the command runs on."""
    pre = [f"{k}={v}" for k, v in (env or {}).items()]
    if user != "root":
        argv = ["sudo", "-u", user, "-H", "env", *pre, *argv]
    elif pre:
        argv = ["env", *pre, *argv]
    try:
        return subprocess.run(  # noqa: S603 - an argument list
            argv, cwd="/", capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 124, "", f"{type(exc).__name__}: {exc}")


def killed(res: subprocess.CompletedProcess[str]) -> bool:
    return res.returncode == 124 and res.stderr.startswith("TimeoutExpired")


def tail(res: subprocess.CompletedProcess[str]) -> str:
    """The error to show: apt's first `E:` line, which names the cause (`Could not get lock …`),
    or else the last line."""
    lines = (res.stderr or res.stdout).strip().splitlines()
    first_e = next((line for line in lines if line.startswith("E: ")), "")
    return (first_e or (lines[-1] if lines else f"exit {res.returncode}"))[:200]


def numbers(text: str) -> tuple[int, ...]:
    """`0.13.2` or `uv 0.13.2 (…)` → (0, 13, 2); () for none."""
    word = next((w for w in text.split() if w[:1].isdigit()), "")
    return tuple(int(p) for p in word.split(".") if p.isdigit())


def dotted(version: tuple[int, ...]) -> str:
    return ".".join(map(str, version)) or "none"


def ca_version() -> str:
    return run(["dpkg-query", "-W", "-f", "${Version}", "ca-certificates"]).stdout.strip()


def ca_current(have: str) -> bool:
    return run(["dpkg", "--compare-versions", have, "ge", CA_FLOOR]).returncode == 0


def step_apt(apply: bool) -> str:
    """Before step 1's `apt-get update` (#947): the release's `apt_repair.py`, from a directory
    of root's own (0700), with the system Python, as a rollout runs it. Only with `--apply` and
    where step 1 will run apt (`ca-certificates` below CA_FLOOR); "" elsewhere, which the report
    leaves out. Its `apt: …` line; `FAILED: …` when it exits non-zero or is cut off, the
    source then kept, unless a rollout beside it removed it meanwhile."""
    have = ca_version()
    if not apply or not have or ca_current(have):
        return ""
    with tempfile.TemporaryDirectory(prefix="zab-trust-fix.") as tmp:
        script = Path(tmp, "apt_repair.py")
        script.write_bytes(base64.b64decode(APT_REPAIR))
        res = run([sys.executable, "-I", str(script)], timeout=APT_TIMEOUT_S)
    line = next((x for x in res.stdout.splitlines() if x.startswith("apt: ")), "")
    if res.returncode:
        return f"FAILED: {line or tail(res)}"
    return line or "apt: no line"


def step_system(apply: bool) -> str:
    have = ca_version()
    if not have:
        return "FAILED: ca-certificates is not installed"
    if ca_current(have):
        return f"{have}, current"
    if not apply:
        return f"{have}: would upgrade"
    wait = ["-o", f"DPkg::Lock::Timeout={APT_LOCK_WAIT_S}"]
    for name, argv in (
        ("update", ["apt-get", "-q", *wait, "update"]),
        ("install", ["apt-get", "-q", "-y", *wait, "install", "--only-upgrade", "ca-certificates"]),
    ):
        res = run(argv, env={"DEBIAN_FRONTEND": "noninteractive"}, timeout=APT_TIMEOUT_S)
        if killed(res) and name == "install":
            return (
                f"FAILED: apt-get install killed after {APT_TIMEOUT_S} s, maybe mid-install: run "
                "`sudo dpkg --configure -a` on the VM, then rerun"
            )
        if res.returncode:
            return f"FAILED: apt-get {name}: {tail(res)}"
    return f"{have} -> {ca_version()}"


def uv_version(name: str = "uv") -> tuple[int, ...]:
    return numbers(run([str(BIN / name), "--version"], user=AGENT).stdout)


def uv_shown() -> str:
    uv, uvx = uv_version(), uv_version("uvx")
    return dotted(uv) if uv == uvx else f"{dotted(uv)} (uvx {dotted(uvx)})"


def blocked(store: str) -> str:
    return f"blocked, nothing installed: {store} still holds a distrusted root"


def trusted(ts: dict, store: Path) -> bool:
    """STORE is there and holds no distrusted root."""
    return store.exists() and ts["check"]([store])[1]


def step_uv(apply: bool, *, system_ok: bool) -> str:
    """SYSTEM_OK: the system store, curl's, holds no distrusted root."""
    have, arch = uv_shown(), platform.machine()
    if min(uv_version(), uv_version("uvx")) >= UV_FLOOR:
        return f"{have}, current"
    if arch not in UV_SHA256:
        return f"FAILED: uv {have}, and no pinned uv for {arch}"
    if not apply:
        return f"{have}: would install {UV_VERSION}"
    if not system_ok:
        return blocked(str(SYSTEM_CERTS))
    agent = pwd.getpwnam(AGENT)
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp, "uv.tar.gz")
        url = UV_URL.format(UV_VERSION, arch)
        res = run(["curl", "-fsSL", "--proto", "=https", "-o", str(tarball), url], timeout=120)
        if res.returncode:
            return f"FAILED: uv {UV_VERSION}'s download: {tail(res)}"
        if hashlib.sha256(tarball.read_bytes()).hexdigest() != UV_SHA256[arch]:
            return f"FAILED: uv {UV_VERSION}: wrong SHA-256, nothing installed"
        with tarfile.open(tarball) as tar:
            for name in ("uv", "uvx"):
                member = tar.extractfile(f"uv-{arch}-unknown-linux-gnu/{name}")
                new = BIN / f".{name}.new"  # then renamed over the old one
                new.write_bytes(member.read() if member else b"")
                os.chown(new, agent.pw_uid, agent.pw_gid)
                new.chmod(0o755)
                new.replace(BIN / name)
    return f"{have} -> {uv_shown()}"


def certifi_version(python: Path, owner: str) -> str:
    return run([str(python), "-I", "-c", VERSION_PY], user=owner).stdout.strip()


def step_certifi(prefix: Path, owner: str, apply: bool, *, uv_ok: bool) -> str:
    """UV_OK: uv, through whose built-in roots certifi comes, is UV_FLOOR or newer."""
    python = prefix / "bin" / "python"
    if not python.exists():
        return "absent"
    have = certifi_version(python, owner)
    if not have:  # a tool that doesn't use certifi gets none
        return "no certifi"
    if numbers(have) >= numbers(CERTIFI):
        return f"{have}, current"
    if not apply:
        return f"{have}: would install {CERTIFI}"
    if not uv_ok:
        return blocked(f"uv {dotted(uv_version())}")
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o755)  # noqa: S103 - the owner reads the requirement
        req = Path(tmp, "certifi.txt")
        req.write_text(f"certifi=={CERTIFI} " + " ".join(f"--hash={h}" for h in CERTIFI_HASHES))
        req.chmod(0o644)
        argv = [str(BIN / "uv"), "pip", "install", "-q", "--python", str(python)]
        argv += ["--require-hashes", "--no-deps", "-r", str(req)]
        # no timeout: one would kill sudo and leave uv installing past this run's lock
        res = run(argv, user=owner, env={"UV_NO_CONFIG": "1"}, timeout=None)
    if res.returncode:
        return f"FAILED: {have} -> {CERTIFI}: {tail(res)}"
    return f"{have} -> {certifi_version(python, owner)}"


def conda_bundles(prefix: Path) -> list[Path] | None:
    """`ssl/`, and the bundles that the environment's certifi and pip's vendored certifi
    return; None when its Python can't say, or one of them isn't there."""
    res = run([str(prefix / "bin" / "python"), "-I", "-c", WHERE_PY], user=AGENT)
    paths = [prefix / "ssl", *(Path(line) for line in res.stdout.splitlines() if line)]
    return paths if res.returncode == 0 and all(p.exists() for p in paths) else None


def conda_present() -> dict[str, Path]:
    if not (CONDA / "bin" / "conda").exists():
        return {}
    return {env: p for env, p in CONDA_ENVS.items() if (p / "bin" / "python").exists()}


def step_conda(ts: dict, apply: bool, *, system_ok: bool) -> str:
    """SYSTEM_OK: the system store, which conda trusts here, holds no distrusted root."""
    envs = conda_present()
    if not envs:
        return "absent"
    out = []
    for env, prefix in envs.items():
        bundles = conda_bundles(prefix)
        if bundles is not None and ts["check"](bundles)[1]:
            out.append(f"{env} current")
        elif not apply:
            out.append(f"{env} would update ca-certificates, certifi, pip and openssl")
        elif not system_ok:
            out.append(f"{env} {blocked(str(SYSTEM_CERTS))}")
        else:  # by its prefix, the one the check reads, whatever an agent's envs_dirs says
            argv = [str(CONDA / "bin" / "conda"), "install", "-y", "-q", "-p", str(prefix)]
            env_vars = {"CONDA_SSL_VERIFY": SYSTEM_BUNDLE}
            # No timeout of its own: a kill would reach sudo, not conda, which would run on
            # beside a rerun's (review round 2 on #856). The driver's bounds the whole run.
            res = run([*argv, "-c", "conda-forge", *CONDA_SPECS], user=AGENT, env=env_vars,
                      timeout=None)  # fmt: skip
            out.append(f"{env} updated" if res.returncode == 0 else f"FAILED: {env}: {tail(res)}")
    return "; ".join(out)


def managed_settings() -> dict | None:
    """Claude Code's managed settings, None when there is no file. ValueError for a file that
    isn't a JSON object whose `env`, if there is one, is an object: step 5 leaves it alone."""
    try:
        text = MANAGED.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data = json.loads(text)  # a JSONDecodeError, or a UnicodeDecodeError, is a ValueError
    if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
        raise ValueError("not a JSON object with an object `env`")
    return data


def cert_store_shown(data: dict | None) -> str:
    """CLAUDE_CODE_CERT_STORE as a cell shows it: `absent` when there is no file (the image's
    other keys are missing too), `unset`, or its value."""
    if data is None:
        return "absent"
    have = data.get("env", {}).get(CERT_STORE)
    return "unset" if have is None else (have if isinstance(have, str) else json.dumps(have))[:40]


def step_claude(apply: bool) -> str:
    """Step 5 (#882): CLAUDE_CODE_CERT_STORE=system in the managed settings' `env`, every other
    key kept, the file replaced whole (0644, root's); a file that isn't valid is left as it is.
    The new file is written beside it under a fixed name, created afresh: a leftover there, a
    link included, is removed first and never followed."""
    try:
        data = managed_settings()
    except ValueError as exc:
        return f"FAILED: {MANAGED} left as it is: {exc}"
    have = cert_store_shown(data)
    if data is not None and data.get("env", {}).get(CERT_STORE) == "system":
        return "system, current"
    if not apply:
        return f"{have}: would set system"
    data = data or {}
    data.setdefault("env", {})[CERT_STORE] = "system"
    MANAGED.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    new = MANAGED.with_name(f"{MANAGED.name}.new")
    try:
        new.unlink()  # a killed run's, or anything else's: removed, not written through
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    with os.fdopen(os.open(new, flags, 0o644), "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fchmod(f.fileno(), 0o644)  # whatever the umask
        os.fsync(f.fileno())
    new.replace(MANAGED)  # whole, as Claude Code reads it
    return f"{have} -> {cert_store_shown(managed_settings())}"


def check_claude() -> tuple[str, bool]:
    """The image's smoke test's (#882): the managed settings set CLAUDE_CODE_CERT_STORE=system,
    and agent reads the system store, without which Claude Code falls back to its bundled
    roots. The check's line, and whether it passed."""
    try:
        data = managed_settings()
    except (OSError, ValueError) as exc:
        return f"FAILED: claude: {MANAGED}: {exc}", False
    if data is None:
        return f"FAILED: claude: no {MANAGED}", False
    if data.get("env", {}).get(CERT_STORE) != "system":
        return f"FAILED: claude: {CERT_STORE} is {cert_store_shown(data)} in {MANAGED}", False
    if run(["bash", "-c", AGENT_READS_STORE], user=AGENT).returncode:
        return f"FAILED: claude: agent reads no certificate from {SYSTEM_CERTS}", False
    return f"claude: {CERT_STORE}=system, and agent reads {SYSTEM_CERTS}", True


def left_by_agents(path: str) -> dict[str, str] | None:
    """What a DISTRUSTED file is when no step installs it, which is software an agent installed;
    None for a store a step owns: the system's (1), a `site-packages/certifi/cacert.pem` (3) and
    anything of conda's (4). In the image's own environments, certifi's bundle was the one file
    with the root (#815's DECISIONS row), so anything else holding it came later:
    - `python`: a uv-managed CPython, under PYTHONS;
    - `pip`: a pip in `~/venv`, `/opt/certbot` or a uv tool's environment, with that `env`;
    - `other`: any other bundle there, such as a package's own (grpcio's `roots.pem`)."""
    if path.startswith((f"{SYSTEM_CERTS}/", f"{CONDA}/")):
        return None
    if path.endswith("/site-packages/certifi/cacert.pem"):
        return None
    if path.startswith(f"{PYTHONS}/"):
        return {"kind": "python", "file": path}
    if "/pip/_vendor/certifi/" in path:
        return {"kind": "pip", "file": path, "env": path.split("/lib/python", 1)[0]}
    return {"kind": "other", "file": path}


def cpython_installed() -> bool:
    """A uv-managed CPython is there: a directory under PYTHONS, besides uv's own dot files (an
    uninstall leaves the directory with only those)."""
    return PYTHONS.is_dir() and any(p.is_dir() and p.name[:1] != "." for p in PYTHONS.iterdir())


def check(ts: dict) -> tuple[list[str], bool, list[dict[str, str]]]:
    """build.sh's smoke test's stores, uv's and uvx's versions, and Claude Code's CA list
    (`check_claude`): the report's lines, whether it passed but for what agents installed, and
    that (`left_by_agents`)."""
    paths = [SYSTEM_CERTS, ENVS["venv"][0], ENVS["certbot"][0], TOOLS]
    paths += [PYTHONS] if cpython_installed() else []
    lines, ok = [], True
    for env, prefix in conda_present().items():
        bundles = conda_bundles(prefix)
        if bundles is None:
            lines.append(f"FAILED: conda's {env}: its certifi or pip can't say where its bundle is")
            ok = False
        paths += bundles or []
    found, passed = ts["check"]([p for p in dict.fromkeys(paths) if p.exists()])
    failing = [x for x in found if x.lstrip().startswith(("DISTRUSTED", "NO BUNDLE"))]
    left = [y for x in failing if "DISTRUSTED" in x and (y := left_by_agents(x.split(": ", 1)[1]))]
    uv, uvx = uv_version(), uv_version("uvx")
    ok = ok and (passed or len(left) == len(failing)) and min(uv, uvx) >= UV_FLOOR
    claude, claude_ok = check_claude()
    return [*lines, *found, f"uv {dotted(uv)}, uvx {dotted(uvx)}", claude], ok and claude_ok, left


def guarded(fn: Callable[..., str], *args: object, **kwargs: object) -> str:
    """FN's outcome; an exception is that step's `FAILED`, so the report still comes back. The
    idle-stop hold is renewed first, so a long run keeps it."""
    try:
        hold_on()
        return fn(*args, **kwargs)
    except Exception as exc:  # one step's failure is the report's, not the run's
        return f"FAILED: {type(exc).__name__}: {exc}"


def checked(ts: dict) -> tuple[list[str], bool, list[dict[str, str]]]:
    try:
        return check(ts)
    except Exception as exc:  # the steps' outcomes still come back
        return [f"FAILED: the check: {type(exc).__name__}: {exc}"], False, []


def uv_current() -> bool:
    return holds(lambda: min(uv_version(), uv_version("uvx")) >= UV_FLOOR)


def step_left_pips(left: list[dict[str, str]], *, uv_ok: bool) -> str:
    """`--upgrade-left-pips`: each pip left in `~/venv` or a uv tool's environment upgraded to
    26.2.2 or newer, as agent, by uv: from PyPI, unhashed, so only through uv's roots once they
    lack the root (UV_OK). The paths are this VM's own walk's, in argument lists: none goes
    through a shell, and the deployer pastes none (review round 2 on #856)."""
    envs = sorted({Path(x["env"]) for x in left if x.get("kind") == "pip" and x.get("env")})
    if not envs:
        return "none"
    if not uv_ok:
        return blocked(f"uv {uv_shown()}")
    out = []
    for prefix in envs:
        python = prefix / "bin" / "python"
        if not (prefix == ENVS["venv"][0] or prefix.parent == TOOLS) or not python.exists():
            out.append(f"{prefix} not upgraded: not ~/venv or a uv tool's environment")
            continue
        argv = [str(BIN / "uv"), "pip", "install", "-q", "--python", str(python), "pip>=26.2.2"]
        res = run(argv, user=AGENT, env={"UV_NO_CONFIG": "1"}, timeout=None)  # as certifi's
        out.append(
            f"{prefix.name} upgraded"
            if res.returncode == 0
            else f"FAILED: {prefix.name}: {tail(res)}"
        )
    return "; ".join(out)


PIPS = "--upgrade-left-pips"


def take_lock() -> int | None:
    """LOCK, held exclusively for as long as this process lives, or None when another run holds
    it (Codex round 4 on #856). The kernel lets go of it when the process ends however it ends;
    this process waits for every command that changes something, so the lock covers them all.
    A dropped SSH doesn't end the process: it runs on, and holds the lock, until its end."""
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def main(argv: list[str]) -> int:
    if argv not in ([], ["--apply"], ["--apply", PIPS]):
        print(f"usage: trust_fix_remote.py [--apply [{PIPS}]]", file=sys.stderr)
        return 2
    apply = "--apply" in argv
    if (lock := take_lock()) is None:  # a dry run too: it would read stores being changed
        print(json.dumps({"apply": apply, "running": True, "steps": {}, "check": [], "ok": False}))
        return 0
    global HOLDING
    try:  # the hold lives inside the lock: written after it is taken, removed before it goes
        HOLDING = apply
        hold_on()
        report = repair(argv, apply)
    finally:
        if HOLDING:
            HOLDING = False
            hold_off()
    print(json.dumps(report))
    os.close(lock)  # as the process's end would, right after
    return 0


def repair(argv: list[str], apply: bool) -> dict[str, object]:
    ts = {"__name__": "trust_stores"}
    exec(compile(base64.b64decode(TRUST_STORES), "trust_stores.py", "exec"), ts)  # noqa: S102
    steps = run_steps(ts, apply)
    lines, ok, left = checked(ts)
    if PIPS in argv:
        steps["left pips"] = guarded(step_left_pips, left, uv_ok=uv_current())
        lines, ok, left = checked(ts)
    report = {"apply": apply, "steps": steps, "check": lines, "left": left, "ok": ok}
    return {**report, "uv_ok": uv_current()}  # uv_ok: a pip may come through it


def hold_on() -> None:
    """With `--apply`: idle stop's hold (`idle_stop.HOLD`), an expiry HOLD_S ahead, so that no
    tick of it powers the VM off while this run works; renewed before each step (`guarded`).
    Only this run's process writes and removes it, inside its lock (Codex round 5 on #856):
    another run of this script can't touch it, as a driver's clean-up after the lock could. A
    run killed leaves it, and idle stop ignores it from its expiry."""
    if not HOLDING:
        return
    new = f"{HOLD}.new"
    try:  # best effort: a run without its hold still runs, as on a VM without idle stop
        with open(new, "w", encoding="ascii") as f:
            f.write(f"{int(time.time()) + HOLD_S}\n")
        os.replace(new, HOLD)  # whole, as idle stop reads it
    except OSError:
        pass


def hold_off() -> None:
    try:
        os.unlink(HOLD)
    except OSError:  # gone already, or not ours to remove
        pass


def run_steps(ts: dict, apply: bool) -> dict[str, str]:
    """Each step's outcome, in order. Each download goes only through a store found clean after
    the step that fixes it (Codex round 1 on #856)."""
    apt = guarded(step_apt, apply)  # before step 1's apt-get update, where it runs (#947)
    steps = {"apt source": apt} if apt else {}
    steps["ca-certificates"] = guarded(step_system, apply)
    system_ok = holds(lambda: trusted(ts, SYSTEM_CERTS))
    steps["uv"] = guarded(step_uv, apply, system_ok=system_ok)
    uv_ok = holds(lambda: uv_version() >= UV_FLOOR)
    for name, (prefix, owner) in ENVS.items():
        steps[name] = guarded(step_certifi, prefix, owner, apply, uv_ok=uv_ok)
    steps["uv tools"] = guarded(step_tools, apply, uv_ok=uv_ok)
    steps["conda"] = guarded(step_conda, ts, apply, system_ok=system_ok)
    steps["claude store"] = guarded(step_claude, apply)  # downloads nothing: never blocked
    return steps


def holds(test: Callable[[], bool]) -> bool:
    """TEST's answer; False when it raises, so that what depends on it is blocked."""
    try:
        return bool(test())
    except Exception:  # an unreadable store is not a clean one
        return False


def step_tools(apply: bool, *, uv_ok: bool) -> str:
    out = [f"{p.name} {guarded(step_certifi, p, AGENT, apply, uv_ok=uv_ok)}" for p in tool_envs()]
    return "; ".join(out) or "absent"


def tool_envs() -> list[Path]:
    """Each uv tool's environment: lean-lsp-mcp's, the bridge's, and any an agent installed."""
    if not TOOLS.is_dir():
        return []
    return [p for p in sorted(TOOLS.iterdir()) if (p / "bin" / "python").exists()]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
