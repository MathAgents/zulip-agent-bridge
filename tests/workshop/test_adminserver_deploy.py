"""`workshop/adminserver/deploy.sh` (docs/ADMIN.md §1.6; issue #331), offline.

Each test builds a release repository in a temporary directory, with throwaway SSH keys that
sign its tags and a local package index whose wheels carry hashes, then runs deploy.sh as Drew
would. A fake `ssh` runs the remote commands here, a fake `sudo` drops nothing, and the server
half runs unprivileged under ZAB_DEPLOY_PREFIX, which roots every server path in the test's own
directory and skips the steps that need root (apt, systemd, chown) while logging them. No
network, server or AWS account is touched.

A bootstrapped server (`server`) is bootstrapped once per module and process, and each test
gets its own copy of that directory, at the path where it was bootstrapped (#473).
"""

import base64
import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "workshop" / "adminserver" / "deploy.sh"
LIST = ROOT / "workshop" / "adminserver" / "release-paths"
PRIVILEGED = ROOT / "workshop" / "adminserver" / "privileged-paths"


def listed(path: pathlib.Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line and not line.startswith("#")]


def covered(path: str, paths) -> bool:
    """`path` is one of `paths` or lies under one (deploy.sh's `covers`)."""
    return any(path == p or path.startswith(p + "/") for p in paths)


RELEASE_PATHS = listed(LIST)
#: Stand-ins the rig commits outside the release paths (#432): none may reach the server.
OUTSIDE = (
    "workshop/files/server.py",
    "workshop/waker/zab-waker.service",
    "workshop/ami/build.sh",
    "workshop/admin/join_server.py",
    "workshop/admin/README.md",
)
GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.org",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.org",
}


def call(argv, **kw) -> subprocess.CompletedProcess:
    """A fixed argument list, the tool by its full path."""
    tool = shutil.which(argv[0], path=kw.get("env", os.environ).get("PATH"))
    assert tool, argv[0]
    return subprocess.run([tool, *argv[1:]], **kw)  # noqa: S603


def _tools_missing() -> str:
    for tool in ("bash", "git", "ssh-keygen", "uv", "flock", "perl", "tar"):
        if not shutil.which(tool):
            return f"no {tool}"
    out = call(["bash", "-c", "echo $BASH_VERSION"], capture_output=True, text=True)
    major, minor = (int(x) for x in out.stdout.split(".")[:2])
    if (major, minor) < (4, 4):
        return "bash older than 4.4"
    git = call(["git", "--version"], capture_output=True, text=True).stdout
    if tuple(int(x) for x in re.findall(r"\d+", git)[:2]) < (2, 34):
        return "git older than 2.34 (SSH signatures)"
    return ""


pytestmark = pytest.mark.skipif(bool(_tools_missing()), reason=_tools_missing() or "ok")


# --- wheels and a local index ---------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_wheel(
    directory: pathlib.Path, name: str, files: dict[str, bytes], extra=None
) -> pathlib.Path:
    """A minimal valid wheel of `name` 1.0 with these files (and dist-info `extra` files)."""
    info = f"{name}-1.0.dist-info"
    content = dict(files)
    content[f"{info}/METADATA"] = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n".encode()
    content[f"{info}/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    for key, value in (extra or {}).items():
        content[f"{info}/{key}"] = value
    record = "".join(f"{p},sha256={_b64(d)},{len(d)}\n" for p, d in content.items())
    content[f"{info}/RECORD"] = (record + f"{info}/RECORD,,\n").encode()
    path = directory / f"{name}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for member, data in content.items():
            zf.writestr(member, data)
    return path


#: A PEP 517 backend, `hatchling.build` as the real one is named, that writes a one-file wheel
#: named after pyproject's version, listing the build directory's top level: it stands in for
#: hatchling, pinned by hash in the test repository's build-constraints.txt. Like hatchling, it
#: runs a custom build hook named in pyproject.toml's [tool.hatch] or a hatch.toml (#436).
FAKE_BACKEND = b"""
import base64, hashlib, pathlib, runpy, tomllib, zipfile

def get_requires_for_build_wheel(config_settings=None):
    return []

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    project = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
    hatch = project.get("tool", {}).get("hatch", {})
    if pathlib.Path("hatch.toml").exists():
        hatch = {**hatch, **tomllib.loads(pathlib.Path("hatch.toml").read_text())}
    hook = hatch.get("build", {}).get("hooks", {}).get("custom", {}).get("path")
    if hook:
        runpy.run_path(hook)
    version = project["project"]["version"]
    info = f"zulip_agent_bridge-{version}.dist-info"
    meta = f"Metadata-Version: 2.1\\nName: zulip-agent-bridge\\nVersion: {version}\\n"
    wheel = "Wheel-Version: 1.0\\nGenerator: fake\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"
    listing = "".join(f"{p.name}\\n" for p in sorted(pathlib.Path(".").iterdir()))
    files = {
        "zab_fake_wheel.txt": listing.encode(),
        f"{info}/METADATA": meta.encode(),
        f"{info}/WHEEL": wheel.encode(),
    }
    def b64(d):
        return base64.urlsafe_b64encode(hashlib.sha256(d).digest()).rstrip(b"=").decode()
    record = "".join(f"{p},sha256={b64(d)},{len(d)}\\n" for p, d in files.items())
    files[f"{info}/RECORD"] = (record + f"{info}/RECORD,,\\n").encode()
    name = f"zulip_agent_bridge-{version}-py3-none-any.whl"
    with zipfile.ZipFile(pathlib.Path(wheel_directory) / name, "w") as zf:
        for member, data in files.items():
            zf.writestr(member, data)
    return name
"""


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    """A PEP 503 index on disk: `fakecli` (with a console script), `fakebackend`, and stand-ins
    for `boto3` and `botocore`, enough for the release's `selfcheck` (`starter.sdk()`), and for
    `httpx`, which the bot's Zulip client and `zulip_provision.py` import (#341): importable,
    which is all the selfcheck's imports and the scripts' `--help` need."""
    root = tmp_path_factory.mktemp("index")
    wheels = root / "wheels"
    wheels.mkdir()
    built = {
        "fakecli": make_wheel(
            wheels,
            "fakecli",
            {"fakecli/__init__.py": b"def main():\n    print('fakecli')\n"},
            {"entry_points.txt": b"[console_scripts]\nfakecli = fakecli:main\n"},
        ),
        "fakebackend": make_wheel(
            wheels,
            "fakebackend",
            {"hatchling/__init__.py": b"", "hatchling/build.py": FAKE_BACKEND},
        ),
        "boto3": make_wheel(wheels, "boto3", {"boto3/__init__.py": b"STAND_IN = True\n"}),
        "botocore": make_wheel(
            wheels,
            "botocore",
            {"botocore/__init__.py": b"", "botocore/config.py": b"class Config:\n    pass\n"},
        ),
        "httpx": make_wheel(wheels, "httpx", {"httpx/__init__.py": b"STAND_IN = True\n"}),
    }
    simple = root / "simple"
    for project, wheel in built.items():
        (simple / project).mkdir(parents=True)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        (simple / project / "index.html").write_text(
            f'<a href="{wheel.as_uri()}#sha256={digest}">{wheel.name}</a>'
        )
    (simple / "index.html").write_text("".join(f'<a href="{p}/">{p}</a>' for p in built))
    backend = built["fakebackend"]
    return {
        "url": simple.as_uri(),
        "wheels": wheels,
        "backend_sha": hashlib.sha256(backend.read_bytes()).hexdigest(),
    }


# --- the rig ----------------------------------------------------------------------------------

ADMIN_TOML = """\
provider = "aws"
admins = [101]
deployers = [101]
destructive_approvals = 1
release = "{release}"
console_url = "https://admin.test.example"

[global]
scope = "zab-test"
regions = ["us-east-2"]
instance_types = ["m7a.2xlarge"]
tags = ["zab-role=test"]
zuliprc = "/etc/zab-admin/secrets/global.zuliprc"
user_id = 900
channels = [40]

[[workshop]]
name = "zab-test"
github_org = "MathAgents"
refserve_url = "https://files.example.org"
roster = "/var/lib/zab-admin/ws/zab-test/roster.csv"
out_dir = "/var/lib/zab-admin/ws/zab-test/out"
[workshop.bot]
zuliprc = "/etc/zab-admin/secrets/bots/zab-test.zuliprc"
user_id = 901
channels = [41]
[workshop.aws]
region = "us-east-2"
ami = "ami-0123456789abcdef0"
security_group = "sg-0123456789abcdef0"
instance_types = ["m7a.2xlarge"]
key_name = "zab-admin-zab-test"
admin_keys = ["/etc/zab-admin/break-glass/drew.pub"]
"""

FAKE_SSH = """#!/bin/sh
# ssh for the tests: drop the options and the host, run the command here
while [ $# -gt 0 ]; do
  case $1 in
    --) shift; break ;;
    -o|-i|-p|-l) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift
exec sh -c "$1"
"""


class Rig:
    def __init__(self, tmp: pathlib.Path, index: dict) -> None:
        self.tmp = tmp
        self.index = index
        self.repo = tmp / "repo"
        self.prefix = tmp / "server"
        self.etc = tmp / "etc"
        self.secrets = tmp / "secrets"
        for d in ("home", "tmp", "bin", "keys", "out", "cache"):
            (tmp / d).mkdir()
        (self.prefix / "tmp").mkdir(parents=True)  # the server's /tmp, for mktemp
        (tmp / "bin" / "ssh").write_text(FAKE_SSH)
        (tmp / "bin" / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
        for tool in ("ssh", "sudo"):
            (tmp / "bin" / tool).chmod(0o755)
        self.env = {
            "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}",
            "HOME": str(tmp / "home"),
            "TMPDIR": str(tmp / "tmp"),
            "LANG": "C.UTF-8",
            "ZAB_DEPLOY_SSH": str(tmp / "bin" / "ssh"),
            "ZAB_DEPLOY_PREFIX": str(self.prefix),
            "ZAB_DEPLOY_PYTHON": sys.executable,
            "UV_DEFAULT_INDEX": index["url"],
            "UV_OFFLINE": "1",
            "UV_CACHE_DIR": str(tmp / "cache"),
            **GIT_ENV,
        }
        self.keys = {name: self.keygen(name) for name in ("drew", "mallory")}
        self.write_etc(signers=["drew"])
        self.write_secrets()
        self.init_repo()

    # helpers

    def run(self, *argv, input=None, check=False, **kw):
        kw = {"capture_output": True, "text": True, "errors": "replace", **kw}
        return call([str(a) for a in argv], env=self.env, input=input, check=check, **kw)

    def git(self, *args, check=True):
        return self.run("git", "-C", str(self.repo), *args, check=check).stdout.strip()

    def keygen(self, name: str) -> pathlib.Path:
        key = self.tmp / "keys" / name
        self.run(
            "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key), check=True
        )
        return key

    def signers_line(self, name: str) -> str:
        pub = (self.keys[name].with_suffix(".pub")).read_text().strip()
        return f'{name}@example.org namespaces="git" {pub}\n'

    def write_etc(self, signers, release="final", etc=None):
        etc = etc or self.etc
        (etc / "break-glass").mkdir(parents=True, exist_ok=True)
        (etc / "admin.toml").write_text(ADMIN_TOML.format(release=release))
        (etc / "allowed_signers").write_text("".join(self.signers_line(s) for s in signers))
        (etc / "backup_recipients").write_text(self.keys["drew"].with_suffix(".pub").read_text())
        shutil.copy(self.keys["drew"].with_suffix(".pub"), etc / "break-glass" / "drew.pub")
        return etc

    def write_secrets(self):
        for rel in (
            "provisioner.zuliprc",
            "github-token",
            "global.zuliprc",
            "bots/zab-test.zuliprc",
        ):
            path = self.secrets / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"secret for {rel}\n")

    def init_repo(self):
        """The release this tree ships: exactly the paths of release-paths, copied from it,
        but pyproject, uv.lock and build-constraints.txt, which point at the local index."""
        repo = self.repo
        for rel in RELEASE_PATHS:
            if rel in ("pyproject.toml", "uv.lock", "build-constraints.txt"):
                continue
            source, target = ROOT / rel, repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy(source, target)
        (repo / "src/zab/bridge.py").write_text(
            "raise SystemExit('the bridge never runs on the server')\n"
        )
        for outside in OUTSIDE:  # #432: the files host's, the AMI build's, the laptop's
            (repo / outside).parent.mkdir(parents=True, exist_ok=True)
            (repo / outside).write_text(f"# {outside}: never on the server\n")
        (repo / "README.md").write_text("test release\n")
        (repo / "build-constraints.txt").write_text(
            f"fakebackend==1.0 \\\n    --hash=sha256:{self.index['backend_sha']}\n"
        )
        self.write_pyproject("0.3.0")
        self.run("git", "init", "-q", "-b", "main", str(repo), check=True)
        self.lock()
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "initial")

    def write_pyproject(self, version: str):
        (self.repo / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["fakebackend==1.0"]\n'
            'build-backend = "hatchling.build"\n\n'
            "[project]\n"
            'name = "zulip-agent-bridge"\n'
            f'version = "{version}"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = []\n\n"
            "[dependency-groups]\n"
            'admin = ["fakecli==1.0", "boto3==1.0", "botocore==1.0", "httpx==1.0"]\n'
        )

    def lock(self, **extra_env):
        env = dict(self.env, UV_NO_CONFIG="1", **extra_env)
        call(["uv", "lock", "-q"], cwd=self.repo, env=env, check=True)

    def release(self, version, signer="drew", *, edit=None, message=None):
        """Commit a release of `version` (pyproject agrees), then tag it: signed by `signer`,
        annotated but unsigned (None) or lightweight ("light")."""
        self.write_pyproject(version.removeprefix("v"))
        if edit:
            edit(self.repo)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message or f"release {version}")
        if signer == "light":
            self.git("tag", version)
        elif signer is None:
            self.git("tag", "-a", "-m", version, version)
        else:
            self.sign(version, signer=signer)

    def sign(self, version, commit="HEAD", signer="drew"):
        """`git tag -s` with an SSH key, as Drew signs releases (Q5)."""
        key = f"user.signingkey={self.keys[signer]}"
        self.git("-c", "gpg.format=ssh", "-c", key, "tag", "-s", "-m", version, version, commit)

    def deploy(self, version, *flags, etc=None, confirm=None):
        """deploy.sh as Drew runs it, typing `confirm` (the version) when asked."""
        where = ["--repo", self.repo, "--etc", etc or self.etc, "--out", self.tmp / "out"]
        if "--bootstrap" in flags:
            where += ["--secrets", self.secrets]
        argv = [*where, "--host", "admin.test", *flags, version]
        return self.run("bash", DEPLOY, *argv, input=f"{confirm or version}\n")

    # the server's state

    @property
    def opt(self) -> pathlib.Path:
        return self.prefix / "opt" / "zab-admin"

    def current(self) -> str | None:
        link = self.opt / "current"
        return os.readlink(link) if link.is_symlink() else None

    def releases(self) -> list[str]:
        path = self.opt / "releases"
        return sorted(p.name for p in path.iterdir()) if path.is_dir() else []

    def records(self) -> list[dict]:
        path = self.opt / "deploys.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def rig(tmp_path, index):
    return Rig(tmp_path, index)


@pytest.fixture(scope="module")
def bootstrapped(tmp_path_factory, index):
    """A rig whose server was bootstrapped with v0.3.0, and a copy of its directory kept aside.

    A bootstrap takes about 2.5 s, most of it the self-check; a copy of the directory takes
    0.02 s (#473). The rig lives at `base/rig` and its copy at `base/template`: `server` puts
    a fresh copy back at `base/rig` for each test. Each test so starts from the very files a
    bootstrap left, every absolute path in them (the venv's interpreter, the .pth, `current`,
    the records) still true, with their modes, symlinks and mtimes; hard links become copies.
    A module's tests run one at a time in a process (under xdist too), never two in `base/rig`.
    """
    base = tmp_path_factory.mktemp("bootstrapped")
    (base / "rig").mkdir()
    rig = Rig(base / "rig", index)
    rig.release("v0.3.0")
    result = rig.deploy("v0.3.0", "--bootstrap")
    assert result.returncode == 0, result.stderr
    template = base / "template"
    (base / "rig").rename(template)
    return rig, template


@pytest.fixture
def server(bootstrapped, request):
    """A rig whose server was bootstrapped with v0.3.0: a copy of `bootstrapped`'s, its own."""
    rig, template = bootstrapped
    name = re.sub(r"\W", "_", request.node.name)[:40]
    if rig.tmp.exists():  # a copy that failed part way, before its test
        rig.tmp.rename(tempfile.mkdtemp(prefix=f"{name}-before-", dir=rig.tmp.parent))
    shutil.copytree(template, rig.tmp, symlinks=True)
    yield copy.deepcopy(rig)
    # out of the next test's way, kept under the test's name as its tmp_path would be
    rig.tmp.rename(tempfile.mkdtemp(prefix=f"{name}-", dir=rig.tmp.parent))


@pytest.fixture
def released(rig):
    """A rig with v0.3.0 signed and no server: for the laptop half alone (`--review`, a refused
    `--wheel`), which needs no bootstrap, the slow part of `server`."""
    rig.release("v0.3.0")
    return rig


def refused(result, pattern: str):
    assert result.returncode != 0, result.stdout + result.stderr
    assert re.search(pattern, result.stderr), result.stderr


# --- a good release ---------------------------------------------------------------------------


def test_bootstrap_installs_the_release_at_its_final_path(server):
    rel = server.opt / "releases" / "v0.3.0"
    assert server.current() == str(rel)
    venv = rel / ".venv"
    # the .pth names the release's own src/, by its final path, and nothing else
    (pth,) = list(venv.glob("lib/python3*/site-packages/zab-release.pth"))
    assert pth.read_text() == f"{rel}/src\n"
    # every entry point's interpreter is the release's own venv, by its final path
    script = (venv / "bin" / "fakecli").read_text()
    interpreter = f"{rel}/.venv/bin/python"
    assert script.startswith(f"#!{interpreter}\n") or f"'''exec' '{interpreter}'" in script
    assert server.run(venv / "bin" / "fakecli", check=True).stdout == "fakecli\n"
    # every path of this test's directory that the venv names lies in the release: nothing
    # names the payload, the laptop's or the build's temporary directories
    named = re.compile(re.escape(str(server.tmp).encode()) + rb"[^\s'\"]*")
    for path in venv.rglob("*"):
        if path.is_file() and not path.is_symlink():
            for match in named.finditer(path.read_bytes()):
                assert match.group().startswith(str(rel).encode()), (path, match.group())
    # only the release paths: the bridge never reaches the server
    assert not (rel / "src" / "zab" / "bridge.py").exists()
    assert not (rel / "README.md").exists()
    assert (rel / "src" / "zab" / "admin" / "config.py").exists()
    # #432: nor the files host, the AMI build or the laptop's scripts; what the scripts copy does
    for outside in OUTSIDE:
        assert not (rel / outside).exists(), outside
    for inside in ("workshop/template", "workshop/ami/files/agent-instructions.md"):
        assert (rel / inside).exists(), inside
    assert not (rel / ".uv-cache").exists()
    # the release imports its own code, under -I
    probe = "import zab.admin.config as c; print(c.__file__)"
    out = server.run(venv / "bin" / "python", "-I", "-c", probe, check=True)
    assert out.stdout.strip() == f"{rel}/src/zab/admin/config.py"


def test_bootstrap_installs_the_servers_files(server):
    etc = server.prefix / "etc" / "zab-admin"
    assert (etc / "allowed_signers").read_text() == server.signers_line("drew")
    assert (etc / "break-glass" / "drew.pub").exists()
    for rel in ("provisioner.zuliprc", "github-token", "global.zuliprc", "bots/zab-test.zuliprc"):
        path = etc / "secrets" / rel
        assert path.read_text() == f"secret for {rel}\n"
        assert path.stat().st_mode & 0o777 == 0o640
    caddyfile = (server.prefix / "etc" / "caddy" / "Caddyfile").read_text()
    assert "admin.test.example {" in caddyfile and "<CONSOLE_HOST>" not in caddyfile
    units = server.prefix / "etc" / "systemd" / "system"
    source = ROOT / "workshop" / "adminserver"
    found = [*source.glob("*.service"), *source.glob("*.socket"), *source.glob("*.path")]
    assert "zab-admin-config.path" in {unit.name for unit in found}  # #418
    for unit in found:
        assert (units / unit.name).read_text() == unit.read_text()  # waker, starter: PR 21
    for drop_in in source.glob("*.service.d/*.conf"):
        assert (units / drop_in.parent.name / drop_in.name).read_text() == drop_in.read_text()
    for rule in ("imds.nft", "nftables-imds.nft", "nftables-stop-imds.nft"):
        assert (etc / rule).read_text() == (source / rule).read_text()
    fleet = server.prefix / "var" / "lib" / "zab-admin" / ".ssh" / "id_ed25519.pub"
    assert fleet.read_text().startswith("ssh-ed25519 ")
    (record,) = server.records()
    assert record["event"] == "bootstrap" and record["version"] == "v0.3.0"
    assert record["commit"] == server.git("rev-parse", "v0.3.0^{commit}")


# --- the root config helper's mounts (#437) ---------------------------------------------------

HELPER_UNIT = DEPLOY.parent / "zab-admin-config.service"
#: What each of the unit's settings makes of the path it names, and of what lies below until a
#: deeper one says otherwise (systemd.exec(5)). A tmpfs hides what was below it.
HELPER_KINDS = {
    "ReadWritePaths": "rw",
    "BindPaths": "rw",
    "ReadOnlyPaths": "ro",
    "BindReadOnlyPaths": "ro",
    "InaccessiblePaths": "hidden",
    "TemporaryFileSystem": "tmpfs",
}
#: What the root helper may write under /etc/zab-admin, and why; `/*`: what lies in that one.
HELPER_WRITES = {
    "/etc/zab-admin": "admin.toml is replaced by a rename, which needs the directory writable",
    "/etc/zab-admin/admin.toml": "replaced so: a read-only mount on it would fail the rename",
    "/etc/zab-admin/admin.toml.bak": "the old admin.toml, renamed over the one before",
    "/etc/zab-admin/secrets/bots": "the new bot's key, created there",
    "/etc/zab-admin/secrets/bots/*": "the other bots' keys: a unit names only paths that exist",
}


def helper_mounts() -> list[tuple[str, str, bool]]:
    """(path, kind, optional) for each path the helper's unit names."""
    found = []
    for line in HELPER_UNIT.read_text().splitlines():
        key, _, value = line.partition("=")
        for word in value.split() if key in HELPER_KINDS else []:
            path = word.split(":")[0].lstrip("-")  # TemporaryFileSystem's options; "-": optional
            found.append((path, HELPER_KINDS[key], word.startswith("-")))
    return found


def helper_access(path: str, mounts: list[tuple[str, str, bool]]) -> str:
    """rw, ro or hidden in the helper's namespace. ProtectSystem=strict makes / read-only; then
    the deepest path the unit names, at or above `path`, decides: a ReadWritePaths= inside a
    ReadOnlyPaths= is writable, a BindPaths= inside a TemporaryFileSystem= is back, and nothing
    may be nested inside an InaccessiblePaths= (systemd.exec(5), systemd 255)."""
    best, kind = "", "ro"
    for named, what, _ in mounts:
        if (path == named or path.startswith(named + "/")) and len(named) > len(best):
            best, kind = named, what
    if kind == "tmpfs":  # read-only (":ro") and empty: what was there is gone
        return "ro" if path == best else "hidden"
    return kind


def _installed(server) -> list[str]:
    etc = server.prefix / "etc" / "zab-admin"
    return [
        "/etc/zab-admin",
        *(f"/etc/zab-admin/{p.relative_to(etc)}" for p in sorted(etc.rglob("*"))),
    ]


def _allowed(path: str) -> bool:
    return path in HELPER_WRITES or f"{path.rsplit('/', 1)[0]}/*" in HELPER_WRITES


def test_the_config_helper_writes_only_what_it_must_under_etc(server):
    """#437: of what a bootstrap installs under /etc/zab-admin, and of what the units and the
    nftables rules there name, the root helper may write only HELPER_WRITES: the IMDS rule's
    files, the trust files and the break-glass keys are read-only, the other secrets hidden. A
    new file there is caught here until it is named in the unit or, with its reason, above.
    Each path the unit names there exists after a bootstrap, without "-": a missing one would
    be one the helper could create. Nothing is nested inside an inaccessible path."""
    mounts = helper_mounts()
    installed = _installed(server)
    loaded = sorted(DEPLOY.parent.glob("*.nft")) + sorted(DEPLOY.parent.glob("*.service"))
    loaded += sorted(DEPLOY.parent.glob("*.service.d/*.conf"))
    named = {m for f in loaded for m in re.findall(r"/etc/zab-admin/[\w./-]*\w", f.read_text())}
    assert {"/etc/zab-admin/imds.nft", "/etc/zab-admin/nftables-imds.nft"} <= named
    assert "/etc/zab-admin/nftables-stop-imds.nft" in installed
    access = {path: helper_access(path, mounts) for path in {*installed, *named}}
    writable = sorted(path for path, how in access.items() if how == "rw")
    assert [path for path in writable if not _allowed(path)] == [], "make these read-only"
    assert all(helper_access(p.replace("*", "x"), mounts) == "rw" for p in HELPER_WRITES)
    assert access["/etc/zab-admin/secrets/provisioner.zuliprc"] == "hidden"
    for path, _, optional in mounts:
        if path.startswith("/etc/zab-admin"):
            assert path in installed and not optional, path
        hidden = [p for p, k, _ in mounts if k == "hidden" and path.startswith(p + "/")]
        assert hidden == [], f"{path} is nested inside {hidden}"


def _user_manager() -> bool:
    """A user systemd manager that sets up mount namespaces (unprivileged user namespaces).
    Without them it skips ProtectSystem= and the like silently, so the probe is a tmpfs over a
    file's directory, which it must mount or fail (226): the file is gone inside."""
    if not (shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR")):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        marker = pathlib.Path(tmp) / "marker"
        marker.touch()
        argv = ["systemd-run", "--user", "--wait", "--quiet", "-p", f"TemporaryFileSystem={tmp}"]
        argv += ["--", "/usr/bin/test", "!", "-e", str(marker)]
        return call(argv, capture_output=True).returncode == 0


#: In the helper's namespace: each path's access; each mount's escapes (replacing, unlinking or
#: linking a file mount, moving a directory one), by errno; then the helper's own writes.
HELPER_PROBE = """
import errno, json, os, sys

root, mounted, paths = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3:]
NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL


def access(path):
    try:
        if os.path.isdir(path):
            os.close(os.open(path + "/.probe", NEW, 0o600))
            os.unlink(path + "/.probe")
        else:
            os.close(os.open(path, os.O_WRONLY | os.O_APPEND))
        return "rw"
    except OSError as exc:
        return "ro" if exc.errno == errno.EROFS else "hidden"


def tried(do):
    try:
        do()
        return "ok"
    except OSError as exc:
        return errno.errorcode[exc.errno]


etc, state = root + "/etc/zab-admin", root + "/var/lib/zab-admin-config"
os.close(os.open(etc + "/.fresh", NEW))
escapes = {}
for name in mounted:
    path = root + name
    if os.path.isdir(path):
        escapes[name] = {"rename": tried(lambda: os.rename(path, path + ".moved"))}
    else:
        escapes[name] = {
            "replace": tried(lambda: os.rename(etc + "/.fresh", path)),
            "unlink": tried(lambda: os.unlink(path)),
            "link": tried(lambda: os.link(path, etc + "/.linked")),
        }
os.unlink(etc + "/.fresh")
steps = [  # confighelper.apply's, then run's
    lambda: os.close(os.open(etc + "/secrets/bots/new-ws.zuliprc", NEW)),
    lambda: os.close(os.open(etc + "/admin.toml.new", NEW)),
    lambda: os.link(etc + "/admin.toml", etc + "/admin.toml.bak.new"),
    lambda: os.close(os.open(state + "/changes.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND)),
    lambda: os.replace(etc + "/admin.toml.bak.new", etc + "/admin.toml.bak"),
    lambda: os.replace(etc + "/admin.toml.new", etc + "/admin.toml"),
    lambda: os.close(os.open(state + "/result.json.new", NEW)),
    lambda: os.replace(state + "/result.json.new", state + "/result.json"),
]
found = {path: access(root + path) for path in paths}
done = []
for step in steps:
    try:
        done.append(step() or "ok")
    except OSError as exc:
        done.append(exc.strerror)
print(json.dumps({"access": found, "escapes": escapes, "steps": done}))
"""


def _rooted(word: str, prefix: str) -> str:
    """A path of the unit (`-/x`, `/x:ro`) under `prefix`."""
    return ("-" if word.startswith("-") else "") + prefix + word.removeprefix("-")


@pytest.mark.skipif(not _user_manager(), reason="no user systemd manager with namespaces")
def test_the_config_helpers_mounts_are_systemds(server):
    """The unit's mounts, rooted in the test's server, for a service of the user manager (which
    adds PrivateUsers=): each path's access is what `helper_access` says, the fleet key is out
    of sight, and every write of the helper's goes through. The design rests on the kernel's
    refusals in a writable /etc/zab-admin: a read-only file mount can't be replaced by a rename
    or unlinked (EBUSY) or hard-linked elsewhere (EXDEV), nor a directory one moved away
    (review of #438)."""
    prefix = str(server.prefix)
    argv = ["systemd-run", "--user", "--wait", "--pipe", "--quiet", "-p", "ProtectSystem=strict"]
    for line in HELPER_UNIT.read_text().splitlines():
        key, _, value = line.partition("=")
        if key in HELPER_KINDS:
            argv += ["-p", f"{key}={' '.join(_rooted(w, prefix) for w in value.split())}"]
    paths = [*_installed(server), "/var/lib/zab-admin-config", "/var/lib/zab-admin/.ssh/id_ed25519"]
    etc = "/etc/zab-admin"
    mounted = [path for path, _, _ in helper_mounts() if path.startswith(f"{etc}/")]
    argv += ["--", sys.executable, "-I", "-S", "-c", HELPER_PROBE, prefix, json.dumps(mounted)]
    done = call([*argv, *paths], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    pinned = {"replace": "EBUSY", "unlink": "EBUSY", "link": "EXDEV"}
    files = ("allowed_signers", "backup_recipients", "imds.nft", "nftables-imds.nft")
    assert report["escapes"] == {
        **{f"{etc}/{name}": pinned for name in (*files, "nftables-stop-imds.nft")},
        f"{etc}/break-glass": {"rename": "EBUSY"},
        f"{etc}/secrets": {"rename": "EBUSY"},
        f"{etc}/secrets/bots": {"rename": "EROFS"},  # in the read-only tmpfs
    }
    assert report["access"] == {path: helper_access(path, helper_mounts()) for path in paths}
    assert report["access"]["/var/lib/zab-admin/.ssh/id_ed25519"] == "hidden"
    assert report["access"]["/etc/zab-admin/imds.nft"] == "ro"
    assert report["steps"] == ["ok"] * 8


def test_bootstrap_logs_the_root_steps_it_skips_in_tests(rig):
    rig.release("v0.3.0")
    result = rig.deploy("v0.3.0", "--bootstrap")
    assert result.returncode == 0, result.stderr
    for step in (
        "apt-get",
        "systemd-sysusers",
        "enable --now zab-admin-imds.service",
        "import_key_pair.py --region us-east-2 --key-name zab-admin-zab-test",
        "--tag workshop=zab-test --tag zab-scope=zab-test --tag zab-role=test",  # #422
        "restart caddy.service",
    ):
        assert step in result.stderr, step
    calls = re.findall(r"skipped: systemctl (.+)", result.stderr)
    assert calls[-len(UNIT_STEPS) :] == UNIT_STEPS  # #348: the starter's socket, then the waker
    # #350's review: the IMDS unit is installed and loaded before it is enabled
    assert calls.index("daemon-reload") < calls.index("enable --now zab-admin-imds.service")


def test_a_failed_bootstrap_leaves_no_current_and_can_run_again(rig):
    """Codex round 1 on #338: every bootstrap step runs before `current` exists."""
    rig.release("v0.3.0")
    token = rig.secrets / "github-token"
    token.rename(rig.tmp / "github-token")
    refused(rig.deploy("v0.3.0", "--bootstrap"), "--secrets has no github-token")
    assert rig.current() is None
    assert rig.releases() == []
    assert not (rig.prefix / "etc/zab-admin/secrets/provisioner.zuliprc").exists()
    (rig.tmp / "github-token").rename(token)
    result = rig.deploy("v0.3.0", "--bootstrap")
    assert result.returncode == 0, result.stderr
    assert rig.current().endswith("v0.3.0")
    assert [r["event"] for r in rig.records()] == ["bootstrap"]


def test_a_failed_upgrade_keeps_the_running_release_and_can_run_again(server):
    """Codex round 4 on #338: the server's files are installed before `current` moves."""
    rule = pathlib.Path("workshop/adminserver/nftables-stop-imds.nft")
    server.release("v0.3.1", edit=lambda repo: (repo / rule).unlink())  # install_system fails
    refused(server.deploy("v0.3.1"), "nftables-stop-imds.nft")
    assert server.current().endswith("v0.3.0")
    assert server.releases() == ["v0.3.0"]
    assert [r["event"] for r in server.records()] == ["bootstrap"]
    server.release("v0.3.2", edit=lambda repo: shutil.copy(ROOT / rule, repo / rule))
    result = server.deploy("v0.3.2")
    assert result.returncode == 0, result.stderr
    assert server.current().endswith("v0.3.2")


def test_a_unit_that_fails_to_start_leaves_the_record_and_a_retry_command(server):
    """Codex round 5 on #338, as decided: a failed restart after a complete
    install is an operational fault. The record is written when `current` moves, and the run
    names the units that did not start and the command that restarts them."""
    stub = server.tmp / "bin" / "systemctl-stub"
    log = server.tmp / "systemctl.log"
    stub.write_text(f'#!/bin/sh\necho "$*" >> {log}\n[ "$*" != "restart caddy.service" ]\n')
    stub.chmod(0o755)
    server.env["ZAB_DEPLOY_SYSTEMCTL"] = str(stub)
    server.release("v0.3.1")
    result = server.deploy("v0.3.1")
    assert result.returncode != 0
    assert "these units did not start: caddy.service" in result.stderr
    assert "  sudo systemctl restart caddy.service\n" in result.stderr
    assert server.current().endswith("v0.3.1")
    record = server.records()[-1]
    assert record["event"] == "upgrade" and record["version"] == "v0.3.1"
    assert log.read_text().splitlines() == [
        "restart zab-admin-imds.service",
        "enable caddy.service",
        "restart caddy.service",
        *UNIT_STEPS,  # every unit is tried
    ]
    assert "these units did not start: caddy.service\n" in result.stderr


def test_units_that_fail_to_install_after_the_switch_leave_the_commands(server):
    """#350's review: after the switch and the record, units that fail to install are an
    operational fault, as a unit that fails to start is: the run names the commands that finish
    the job, since a rerun of the running version is refused."""
    dropins = server.prefix / "etc/systemd/system/caddy.service.d"
    shutil.rmtree(dropins)
    dropins.write_text("not a directory")  # install -D can't make it one
    server.release("v0.3.1")
    result = server.deploy("v0.3.1")
    assert result.returncode != 0
    assert "v0.3.1 is installed and" in result.stderr
    assert "but its units were not installed (above)." in result.stderr
    src = f"{server.prefix}/opt/zab-admin/releases/v0.3.1/workshop/adminserver"
    unit = f"{server.prefix}/etc/systemd/system"
    for line in (
        f"  sudo install -D -m 0644 {src}/zab-admin-bot.service {unit}/zab-admin-bot.service\n",
        f"  sudo install -D -m 0644 {src}/caddy.service.d/",
        "  sudo systemctl daemon-reload\n",
        "  sudo systemctl enable caddy.service zab-admin-starter.socket zab-archive-check.socket ",
        "  sudo systemctl restart zab-admin-imds.service caddy.service zab-admin-starter.socket ",
        "  sudo systemctl stop zab-admin-starter.service\n",
    ):
        assert line in result.stderr, line
    assert "stop zab-archive-check" not in result.stderr  # #374: per connection
    assert "skipped: systemctl restart" not in result.stderr.split("units were not installed")[1]
    assert server.current().endswith("v0.3.1")
    assert server.records()[-1]["version"] == "v0.3.1"
    refused(server.deploy("v0.3.1"), "already runs v0.3.1")  # why the note gives the commands


def test_a_unit_that_fails_to_install_stops_the_install(server):
    """The review of #354: a unit file that can't be installed returns at once, as a drop-in
    does; the units are never restarted half installed."""
    unit = server.prefix / "etc/systemd/system/zab-admin-bot.service"
    unit.unlink()
    unit.mkdir()
    unit.chmod(0o555)  # install -D copies into it, and can't
    try:
        server.release("v0.3.1")
        result = server.deploy("v0.3.1")
    finally:
        unit.chmod(0o755)
    assert result.returncode != 0
    assert "but its units were not installed (above)." in result.stderr
    assert "skipped: systemctl daemon-reload" not in result.stderr.split("current is v0.3.1")[1]


def test_relative_paths_are_the_callers(rig):
    """Codex round 2 on #338: --etc, --secrets, --out and --repo may be relative to where
    deploy.sh is run; git runs with -C REPO, so each is made absolute first."""
    rig.release("v0.3.0")
    where = ["--repo", "repo", "--etc", "etc", "--out", "out", "--host", "admin.test"]
    bootstrap = [*where, "--bootstrap", "--secrets", "secrets", "v0.3.0"]
    result = rig.run("bash", DEPLOY, *bootstrap, input="v0.3.0\n", cwd=rig.tmp)
    assert result.returncode == 0, result.stderr
    rig.release("v0.3.1")
    result = rig.run("bash", DEPLOY, *where, "v0.3.1", input="v0.3.1\n", cwd=rig.tmp)
    assert result.returncode == 0, result.stderr
    assert rig.current().endswith("v0.3.1")
    assert (rig.tmp / "out" / "zab-admin-v0.3.0..v0.3.1.diff").exists()


def test_an_upgrade_switches_current_and_keeps_the_old_release(server):
    server.release("v0.3.1")
    result = server.deploy("v0.3.1")
    assert result.returncode == 0, result.stderr
    assert server.current() == str(server.opt / "releases" / "v0.3.1")
    assert server.releases() == ["v0.3.0", "v0.3.1"]
    assert not (server.opt / "current.new").exists()
    for deny in ("cron.deny", "at.deny"):  # zab-build schedules nothing; listed once, not twice
        assert (server.prefix / "etc" / deny).read_text().splitlines().count("zab-build") == 1
    record = server.records()[-1]
    assert (
        record["event"] == "upgrade" and record["from"] == "v0.3.0" and record["base"] == "v0.3.0"
    )
    diff = server.tmp / "out" / "zab-admin-v0.3.0..v0.3.1.diff"
    assert record["diff_sha256"] == hashlib.sha256(diff.read_bytes()).hexdigest()


def test_the_confirmation_must_name_the_release(server):
    server.release("v0.3.1")
    refused(server.deploy("v0.3.1", confirm="yes"), "not confirmed")
    assert server.current().endswith("v0.3.0")


# --- the four checks --------------------------------------------------------------------------


def test_an_unsigned_tag_is_refused(released):
    released.release("v0.3.1", signer=None)
    refused(released.deploy("v0.3.1"), "not signed with an SSH key")
    assert released.releases() == []  # refused on the laptop


def test_a_lightweight_tag_is_refused(released):
    released.release("v0.3.1", signer="light")
    refused(released.deploy("v0.3.1"), "lightweight tag")


def test_a_wrong_signer_is_refused(released):
    released.release("v0.3.1", signer="mallory")
    refused(released.deploy("v0.3.1"), "not signed by a key in")
    assert released.releases() == []  # refused on the laptop


def test_a_ref_named_otherwise_than_its_tag_object_is_refused(released):
    """§1.6 check 2: refs/tags/v0.4.0 pushed to point at the signed v0.3.1 object."""
    released.release("v0.3.1")
    released.git("update-ref", "refs/tags/v0.4.0", released.git("rev-parse", "refs/tags/v0.3.1"))
    refused(released.deploy("v0.4.0"), "tag object named otherwise")
    assert released.releases() == []  # refused on the laptop


def test_a_release_candidate_is_refused_under_the_final_pattern(released):
    released.release("v0.3.1rc1")
    refused(
        released.deploy("v0.3.1rc1"), r'release candidate, and admin.toml has release = "final"'
    )


def test_a_release_candidate_is_taken_by_the_test_server(rig):
    rig.write_etc(signers=["drew"], release="rc")
    rig.release("v0.3.0rc1")
    result = rig.deploy("v0.3.0rc1", "--bootstrap")
    assert result.returncode == 0, result.stderr
    rig.release("v0.3.0")
    assert rig.deploy("v0.3.0").returncode == 0  # the final comes after its candidates
    assert rig.current().endswith("v0.3.0")


@pytest.mark.parametrize(
    ("line", "error"),
    [
        ('release = "beta"', 'release must be "final" or "rc", not "beta"'),
        ('release = "rc"\nrelease = "final"', r"release must be .*not \(twice\)"),
    ],
)
def test_a_release_kind_other_than_final_or_rc_is_refused(released, line, error):
    text = (released.etc / "admin.toml").read_text()
    (released.etc / "admin.toml").write_text(text.replace('release = "final"', line))
    released.release("v0.3.1")
    refused(released.deploy("v0.3.1"), error)


def test_a_branch_is_refused(released):
    released.release("v0.3.1", signer="light")
    released.git("tag", "-d", "v0.3.1")
    released.git("branch", "v0.3.1")  # a branch with a release's name, and no tag
    refused(released.deploy("v0.3.1"), r"v0\.3\.1: no such tag \(a branch is never deployed\)")
    refused(released.deploy("main"), "main: not a release name")
    assert released.releases() == []  # refused on the laptop


def test_a_downgrade_needs_the_flag(server):
    server.release("v0.3.1")
    assert server.deploy("v0.3.1").returncode == 0
    refused(server.deploy("v0.3.0"), r"older than v0\.3\.1.*--downgrade")
    assert server.current().endswith("v0.3.1")
    result = server.deploy("v0.3.0", "--downgrade")
    assert result.returncode == 0, result.stderr
    assert server.current().endswith("v0.3.0")
    assert server.records()[-1]["downgrade"] is True


def test_the_running_release_is_not_redeployed(server):
    refused(server.deploy("v0.3.0"), "already runs v0.3.0")


def test_a_tag_whose_pyproject_names_another_version_is_refused(released):
    released.release("v0.3.1")
    released.git("tag", "-d", "v0.3.1")
    released.sign("v0.3.2")  # the commit's pyproject says 0.3.1
    refused(released.deploy("v0.3.2"), "pyproject.toml has another version")


def test_the_server_checks_again_with_its_own_signers(server):
    """The laptop's allowed_signers trusts mallory; the server's, from the bootstrap, does not."""
    laptop = server.write_etc(signers=["drew", "mallory"], etc=server.tmp / "etc-laptop")
    server.release("v0.3.1", signer="mallory")
    result = server.deploy("v0.3.1", etc=laptop)
    refused(result, "not signed by a key in .*server/etc/zab-admin/allowed_signers")
    assert "The server's allowed_signers differs" in result.stderr
    assert server.releases() == ["v0.3.0"]


def server_half(rig, version, *flags, edit=None):
    """Run the server half directly, as sudo would, on a bundle of refs/tags/VERSION that the
    laptop half never checked, with the commit and release tree the laptop would send. `edit`
    may change the payload first; later flags override earlier ones."""
    payload = rig.tmp / f"payload-{version}"
    payload.mkdir()
    shutil.copy(DEPLOY, payload / "deploy.sh")
    rig.git("bundle", "create", "-q", str(payload / "release.bundle"), f"refs/tags/{version}")
    if edit:
        edit(payload)
    commit = rig.git("rev-parse", f"{version}^{{commit}}")
    listed = rig.git("show", f"{commit}:workshop/adminserver/release-paths").splitlines()
    paths = [line for line in listed if line and not line.startswith("#")]
    ls_tree = ["git", "-C", rig.repo, "ls-tree", "-r", "--full-tree", commit, "--", *paths]
    tree = rig.run(*ls_tree, check=True).stdout  # ASCII: modes, IDs and plain paths
    manifest = hashlib.sha256(tree.encode()).hexdigest()
    argv = ["--version", version, "--commit", commit, "--manifest", manifest, *flags]
    return rig.run("bash", payload / "deploy.sh", "--server", payload, *argv)


@pytest.mark.parametrize(
    ("case", "version", "reason"),
    [
        ("unsigned", "v0.3.1", "not signed with an SSH key"),
        ("lightweight", "v0.3.1", "lightweight tag"),
        ("wrong signer", "v0.3.1", "not signed by a key in"),
        ("renamed ref", "v0.4.0", "tag object named otherwise"),
        ("rc under final", "v0.3.1rc1", "release candidate"),
        ("downgrade", "v0.2.9", r"older than v0\.3\.0: --downgrade"),
    ],
)
def test_the_server_half_makes_every_check_itself(server, case, version, reason):
    signer = {"unsigned": None, "lightweight": "light", "wrong signer": "mallory"}.get(case, "drew")
    server.release("v0.3.1" if case == "renamed ref" else version, signer=signer)
    if case == "renamed ref":
        server.git("update-ref", "refs/tags/v0.4.0", server.git("rev-parse", "refs/tags/v0.3.1"))
    refused(server_half(server, version), reason)
    assert server.current().endswith("v0.3.0") and server.releases() == ["v0.3.0"]


def test_the_server_half_refuses_another_copy_of_itself(server):
    """Review of #338, finding 7: the payload's deploy.sh must be the tag's, byte for byte."""
    server.release("v0.3.1")

    def one_byte(payload):
        with (payload / "deploy.sh").open("a") as f:
            f.write("#")

    refused(server_half(server, "v0.3.1", edit=one_byte), r"this deploy.sh is not v0\.3\.1's")
    assert server.current().endswith("v0.3.0") and server.releases() == ["v0.3.0"]


def test_the_server_half_refuses_another_commit_than_the_laptops(server):
    """Review of #338, finding 7: both halves must name the same commit."""
    server.release("v0.3.1")
    other = server.git("rev-parse", "v0.3.0^{commit}")
    refused(server_half(server, "v0.3.1", "--commit", other), r"v0\.3\.1 is [0-9a-f]+ here")
    assert server.current().endswith("v0.3.0") and server.releases() == ["v0.3.0"]


@pytest.mark.parametrize("good", [True, False])
def test_the_server_half_places_a_wheel_only_with_its_sha256(server, good):
    """Review of #338, findings 2 and 7: the wheel's SHA-256 is checked on root's copy, and
    zab-admin, whose directory it is, places it."""
    server.release("v0.3.1")
    name = "zulip_agent_bridge-0.3.1-py3-none-any.whl"
    data = b"a wheel, as far as this test goes"
    digest = hashlib.sha256(data).hexdigest() if good else "0" * 64
    result = server_half(
        server,
        "v0.3.1",
        "--wheel",
        name,
        "--wheel-sha256",
        digest,
        edit=lambda payload: (payload / name).write_bytes(data),
    )
    placed = server.prefix / "var/lib/zab-admin/releases/wheels" / name
    if good:
        assert result.returncode == 0, result.stderr
        assert placed.read_bytes() == data
        assert placed.stat().st_mode & 0o777 == 0o640
        assert not placed.with_name(name + ".new").exists()
    else:
        refused(result, "wrong SHA-256")
        assert not placed.exists()


def test_a_bundle_with_malformed_objects_is_refused(server):
    """Re-check of #338: fetch.fsckObjects doesn't apply to a bundle (git 2.43), so the server
    runs `git fsck --strict` on what it fetched. A signed release whose commit carries a tree with
    duplicate entries and a `..` entry, outside the release paths, is refused."""
    repo = ["git", "-C", str(server.repo)]
    blob = server.run(*repo, "hash-object", "-w", "--stdin", input="x\n", check=True).stdout.strip()
    raw = bytes.fromhex(blob)
    entries = b"".join(b"100644 " + name + b"\0" + raw for name in (b"..", b"a", b"a"))
    literally = [*repo, "hash-object", "-t", "tree", "-w", "--literally", "--stdin"]
    bad = call(literally, input=entries, env=server.env, capture_output=True, check=True)
    server.write_pyproject("0.3.1")
    server.git("add", "-A")
    server.git("commit", "-q", "-m", "release v0.3.1")
    listing = server.git("ls-tree", "HEAD") + f"\n040000 tree {bad.stdout.decode().strip()}\tbad\n"
    top = server.run(*repo, "mktree", input=listing, check=True).stdout.strip()
    commit = server.git("commit-tree", top, "-p", "HEAD", "-m", "a malformed tree")
    server.sign("v0.3.1", commit)
    refused(server.deploy("v0.3.1"), r"v0\.3\.1: the bundle holds malformed objects")
    assert server.current().endswith("v0.3.0") and server.releases() == ["v0.3.0"]


def test_a_payload_with_a_link_is_refused(server):
    """Review of #338, hardening a: root copies the payload before reading it, and takes only
    plain files and directories."""
    server.release("v0.3.1")
    result = server_half(
        server, "v0.3.1", edit=lambda payload: (payload / "extra").symlink_to("/etc/passwd")
    )
    refused(result, "the payload holds something other than files")


def test_a_dangling_current_counts_as_bootstrapped(rig):
    """Review of #338, finding 5: --bootstrap with a dangling `current` is refused before it
    replaces any trust anchor (allowed_signers, admin.toml)."""
    rig.release("v0.3.0")
    (rig.prefix / "opt/zab-admin").mkdir(parents=True)
    (rig.prefix / "opt/zab-admin/current").symlink_to("/nonexistent/releases/v0.2.9")

    def mallory(payload):
        rig.write_etc(signers=["mallory"], etc=payload / "etc")

    refused(server_half(rig, "v0.3.0", "--bootstrap", edit=mallory), "already bootstrapped")
    assert not (rig.prefix / "etc/zab-admin").exists()


#: SERVICES' steps after the IMDS rule and Caddy (#348): the starter's socket, its service stopped
#: so that the next request runs the new release; the archive check's socket (#374), whose
#: instances each end with their connection: nothing to stop; the config helper's path unit
#: (#418), whose oneshot runs only on a request; the bot; the collector and the console (#489);
#: then the waker (after the bot, PR 9).
UNIT_STEPS = [
    "enable zab-admin-starter.socket",
    "restart zab-admin-starter.socket",
    "stop zab-admin-starter.service",
    "enable zab-archive-check.socket",
    "restart zab-archive-check.socket",
    "enable zab-admin-config.path",
    "restart zab-admin-config.path",
    "enable zab-admin-bot.service",
    "restart zab-admin-bot.service",
    "enable zab-admin-collector.service",
    "restart zab-admin-collector.service",
    "enable zab-console.service",
    "restart zab-console.service",
    "enable zab-waker.service",
    "restart zab-waker.service",
]


def systemctl_log(rig) -> pathlib.Path:
    """A systemctl stub that logs every call and succeeds."""
    stub, log = rig.tmp / "bin" / "systemctl-log", rig.tmp / "systemctl.log"
    stub.write_text(f'#!/bin/sh\necho "$*" >> {log}\n')
    stub.chmod(0o755)
    rig.env["ZAB_DEPLOY_SYSTEMCTL"] = str(stub)
    return log


def test_the_units_are_enabled_at_bootstrap_and_restarted_on_upgrade(rig):
    """#348: every deploy enables and restarts the starter's socket and the waker, in order."""
    log = systemctl_log(rig)
    rig.release("v0.3.0")
    assert rig.deploy("v0.3.0", "--bootstrap").returncode == 0
    steps = ["restart zab-admin-imds.service", "enable caddy.service", "restart caddy.service"]
    assert log.read_text().splitlines() == [*steps, *UNIT_STEPS]
    log.unlink()
    rig.release("v0.3.1")
    result = rig.deploy("v0.3.1")
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [*steps, *UNIT_STEPS]


def test_an_aborted_deploy_touches_no_unit(server):
    """A failed self-check, the waker's included, leaves every unit on the running release."""
    log = systemctl_log(server)

    def break_the_waker(repo):
        (repo / "src/zab/waker/starter_client.py").write_text("raise SystemExit(3)\n")

    server.release("v0.3.1", edit=break_the_waker)
    refused(server.deploy("v0.3.1"), r"v0\.3\.1: the waker's modules did not import")
    assert server.current().endswith("v0.3.0") and server.releases() == ["v0.3.0"]
    assert not log.exists()  # nothing enabled, restarted or stopped


def test_a_deploy_aborted_after_the_build_keeps_the_running_units(server):
    """#348: the new release's units are installed and loaded only after the switch. A deploy
    that fails later than the build, here at the Caddyfile (a release without one), leaves the
    running release's unit files byte-identical, with no daemon-reload and no restart; the next
    good deploy installs the new ones."""
    log = systemctl_log(server)
    units = server.prefix / "etc/systemd/system"
    before = {p: p.read_bytes() for p in units.rglob("*") if p.is_file()}
    waker = pathlib.Path("workshop/adminserver/zab-waker.service")

    def change_a_unit(repo):
        with (repo / waker).open("a") as f:
            f.write("# a change of v0.3.1's\n")

    def and_lose_the_caddyfile(repo):
        change_a_unit(repo)
        (repo / "workshop/adminserver/Caddyfile").unlink()

    server.release("v0.3.1", edit=and_lose_the_caddyfile)
    result = server.deploy("v0.3.1")
    refused(result, "Caddyfile")
    assert server.current().endswith("v0.3.0") and server.releases() == ["v0.3.0"]
    assert {p: p.read_bytes() for p in units.rglob("*") if p.is_file()} == before
    assert "daemon-reload" not in result.stderr and not log.exists()
    server.release(
        "v0.3.2",
        edit=lambda repo: shutil.copy(
            ROOT / "workshop/adminserver/Caddyfile", repo / "workshop/adminserver/Caddyfile"
        ),
    )
    result = server.deploy("v0.3.2")
    assert result.returncode == 0, result.stderr
    assert (units / waker.name).read_text().endswith("# a change of v0.3.1's\n")
    assert result.stderr.index("daemon-reload") > result.stderr.index("current is v0.3.2")


def test_a_list_without_a_waker_module_fails_the_self_check(server):
    """The waker's closure must be in release-paths: a tree without one of its modules fails
    the self-check, before the switch."""

    def drop_the_store(repo):  # the waker's alone: the bot doesn't import it
        listed = (repo / "workshop/adminserver/release-paths").read_text().splitlines()
        listed.remove("src/zab/core/store.py")
        (repo / "workshop/adminserver/release-paths").write_text("\n".join(listed) + "\n")

    server.release("v0.3.1", edit=drop_the_store)
    refused(server.deploy("v0.3.1"), "the waker's modules did not import")
    assert server.current().endswith("v0.3.0")


def test_a_list_without_a_script_the_bot_runs_fails_the_self_check(server):
    """#432: the scripts' `--help`, which the self-check runs, stop a release whose narrowed list
    lost one of them, or a module they import. (A file they only copy, such as the template, is
    the static test's to catch: test_the_release_paths_hold_what_the_server_runs….)"""

    def drop(repo):
        path = repo / LIST.relative_to(ROOT)
        path.write_text(path.read_text().replace("workshop/aws/aws_common.py\n", ""))

    server.release("v0.3.1", edit=drop)
    refused(server.deploy("v0.3.1"), r"selfcheck failed")
    assert server.current().endswith("v0.3.0")


def test_a_list_without_the_server_files_is_refused(server):
    """Review of #338, finding 4: release-paths must cover workshop/adminserver, so deploy.sh,
    which root runs, is always in the reviewed diff."""

    def drop_deploy_sh(repo):
        listed = ["pyproject.toml", "uv.lock", "build-constraints.txt", "src/zab/__init__.py"]
        # each file but deploy.sh, and but the archive check's template unit (#374), whose `@`
        # release-paths takes in no line of its own: it ships with `workshop`
        listed += ["src/zab/admin"] + [
            f"workshop/adminserver/{p.name}"
            for p in sorted((repo / "workshop/adminserver").iterdir())
            if p.is_file() and p.name != "deploy.sh" and "@" not in p.name
        ]
        listed += [
            "workshop/adminserver/caddy.service.d",
            "workshop/adminserver/nftables.service.d",
        ]
        (repo / "workshop/adminserver/release-paths").write_text("\n".join(listed) + "\n")
        with (repo / "workshop/adminserver/deploy.sh").open("a") as f:
            f.write("# a change the reviewed diff would not show\n")

    server.release("v0.3.1", edit=drop_deploy_sh)
    refused(server.deploy("v0.3.1"), "must list workshop/adminserver")
    assert server.releases() == ["v0.3.0"]


def test_a_login_banner_never_reaches_rm(server):
    """Review of #338, finding 6: what `mktemp` answers is checked before the cleanup trap can
    use it; a banner such as `Welcome * to …` is refused, and no `rm` is sent."""
    log = server.tmp / "remote.log"
    chatty = server.tmp / "bin" / "ssh-banner"
    chatty.write_text(
        FAKE_SSH.replace(
            'exec sh -c "$1"',
            f'echo "$1" >> {log}\n'
            "case $1 in *mktemp*) echo 'Welcome * to the admin server' ;; esac\n"
            'exec sh -c "$1"',
        )
    )
    chatty.chmod(0o755)
    server.env["ZAB_DEPLOY_SSH"] = str(chatty)
    refused(server.deploy("v0.3.0", "--wheel"), "unexpected answer to mktemp")
    assert not [line for line in log.read_text().splitlines() if line.startswith("rm ")]


def test_the_server_half_installs_a_good_payload(server):
    server.release("v0.3.1")
    result = server_half(server, "v0.3.1")
    assert result.returncode == 0, result.stderr
    assert server.current().endswith("v0.3.1")
    server.release("v0.3.2")
    refused(server_half(server, "v0.3.2", "--manifest", "0" * 64), "release tree differs")


# --- what is installed ------------------------------------------------------------------------


def test_a_release_whose_archive_check_fails_is_never_switched_to(server):
    """#374: the build runs the archive check's own selfcheck, as its unit runs it."""
    log = systemctl_log(server)

    def break_the_check(repo):
        path = repo / "workshop/admin/archive_check.py"
        text = path.read_text()
        path.write_text(text.replace('("work/same.md", b"alpha")]', '("work/same.md", b"beta")]'))

    server.release("v0.3.1", edit=break_the_check)
    result = server.deploy("v0.3.1")
    refused(result, r"v0\.3\.1: the archive check's selfcheck failed")
    assert "archive_check: selfcheck FAILED" in result.stdout + result.stderr
    assert server.current().endswith("v0.3.0") and server.releases() == ["v0.3.0"]
    assert not log.exists()


def test_a_failed_self_check_changes_nothing(server):
    def break_config(repo):
        (repo / "src/zab/admin/config.py").write_text("raise SystemExit(3)\n")

    server.release("v0.3.1", edit=break_config)
    refused(server.deploy("v0.3.1"), r"v0\.3\.1: selfcheck failed")
    assert server.current().endswith("v0.3.0")
    assert server.releases() == ["v0.3.0"]


def test_the_self_check_refuses_a_bad_admin_toml_before_the_switch(server):
    """Final check of #338: the release's own `python -m zab.admin selfcheck` always runs, here
    on the server's admin.toml, which the strict loader refuses. The deploy aborts before the
    switch: the running release, its files and the record are untouched."""
    toml = server.prefix / "etc/zab-admin/admin.toml"
    toml.write_text(
        toml.read_text().replace("destructive_approvals = 1", "destructive_approvals = 0")
    )
    units = server.prefix / "etc/systemd/system"
    before = {p.name: p.read_text() for p in units.iterdir() if p.is_file()}
    server.release("v0.3.1")
    result = server.deploy("v0.3.1")
    refused(result, r"v0\.3\.1: selfcheck failed")
    assert "admin.toml refused: " in result.stdout + result.stderr
    assert server.current().endswith("v0.3.0")
    assert server.releases() == ["v0.3.0"]
    assert [r["event"] for r in server.records()] == ["bootstrap"]
    assert {p.name: p.read_text() for p in units.iterdir() if p.is_file()} == before


def test_a_deny_file_without_a_final_newline_keeps_its_last_name(rig):
    """Final check of #338, finding 2: zab-build is added on a line of its own."""
    deny = rig.prefix / "etc/cron.deny"
    deny.parent.mkdir(parents=True)
    deny.write_text("alice")  # no final newline
    rig.release("v0.3.0")
    assert rig.deploy("v0.3.0", "--bootstrap").returncode == 0
    assert deny.read_text() == "alice\nzab-build\n"
    assert (rig.prefix / "etc/at.deny").read_text() == "zab-build\n"


def test_the_self_check_runs_the_release_as_shipped(server):
    """The rig ships exactly release-paths, so the real `selfcheck` ran at the bootstrap: the
    strict loader, `from zab.admin import audit, starter`, and `starter.sdk()` (boto3)."""
    py = server.opt / "current/.venv/bin/python"
    out = server.run(
        py,
        "-I",
        "-m",
        "zab.admin",
        "selfcheck",
        "--config",
        server.prefix / "etc/zab-admin/admin.toml",
        check=True,
    )
    assert out.stdout.strip() == "selfcheck: OK"


def test_a_lock_entry_without_a_hash_is_not_installed(server, index):
    """V9: `uv pip install --require-hashes` refuses what `uv sync` would install unhashed. A
    find-links wheel is locked with its path and no hash."""

    def unhashed_lock(repo):
        server.lock(UV_DEFAULT_INDEX="", UV_NO_INDEX="1", UV_FIND_LINKS=str(index["wheels"]))
        assert (
            "hash" not in (repo / "uv.lock").read_text().split('name = "fakecli"')[1].split("[[")[0]
        )

    server.release("v0.3.1", edit=unhashed_lock)
    refused(server.deploy("v0.3.1"), "the admin group's wheels did not install")
    assert server.current().endswith("v0.3.0")
    assert server.releases() == ["v0.3.0"]


def test_gitattributes_do_not_change_what_is_installed(server):
    """The server's info/attributes turns export-subst off: the blob is installed as committed."""

    def subst(repo):
        (repo / ".gitattributes").write_text("workshop/adminserver/stamp.txt export-subst\n")
        (repo / "workshop/adminserver/stamp.txt").write_text("$Format:%H$\n")

    server.release("v0.3.1", edit=subst)
    result = server.deploy("v0.3.1")
    assert result.returncode == 0, result.stderr
    installed = server.opt / "releases/v0.3.1/workshop/adminserver/stamp.txt"
    assert installed.read_text() == "$Format:%H$\n"


def test_a_bootstrap_installs_the_units_as_committed(rig):
    """Review of #338, finding 1: a root .gitattributes, outside the reviewed paths, marks a unit
    export-subst, and the commit message body carries a [Service] line. Root installs and starts
    the server's files only as their blobs, checked before anything is installed."""
    unit = pathlib.Path("workshop/adminserver/zab-admin-imds.service")

    def subst(repo):
        (repo / ".gitattributes").write_text(f"{unit} export-subst\n")
        (repo / unit).write_text((repo / unit).read_text() + "# build: $Format:%B$\n")

    body = "ExecStartPost=/bin/sh -c 'echo from-the-commit-body'"
    rig.release("v0.3.0", edit=subst, message=f"release v0.3.0\n\n{body}")
    result = rig.deploy("v0.3.0", "--bootstrap")
    assert result.returncode == 0, result.stderr
    installed = (rig.prefix / "etc/systemd/system" / unit.name).read_text()
    assert installed == rig.git("show", f"v0.3.0:{unit}") + "\n"
    assert "$Format:%B$" in installed and "ExecStartPost" not in installed


def test_an_upgrade_checks_the_tree_before_anything_runs_in_it(server):
    """The release tree is checked as soon as zab-build has unpacked it, before uv or the
    release's Python run there; the check after the build is the backstop."""
    server.release(
        "v0.3.1", edit=lambda repo: (repo / "workshop/adminserver/passwd").symlink_to("/etc/passwd")
    )
    refused(
        server.deploy("v0.3.1"),
        r"(?s)the unpacked tree differs .*not a file: workshop/adminserver/passwd",
    )
    assert server.current().endswith("v0.3.0") and server.releases() == ["v0.3.0"]


def test_a_release_that_is_not_plain_files_is_refused_before_anything_is_installed(rig):
    """The tree is checked right after it is unpacked: a link stops the bootstrap before root
    installs a user, a directory or a unit."""

    def link(repo):
        (repo / "workshop/adminserver/passwd").symlink_to("/etc/passwd")

    rig.release("v0.3.0", edit=link)
    refused(rig.deploy("v0.3.0", "--bootstrap"), r"not a file: workshop/adminserver/passwd")
    assert not (rig.prefix / "etc/sysusers.d").exists()
    assert not (rig.prefix / "etc/systemd/system").exists()
    assert rig.current() is None


# --- the reviewed diff ------------------------------------------------------------------------


def test_the_reviewed_diff_shows_the_release_paths_only(server):
    hidden = (
        "\n# a reviewed change\u202e\n"
        "ok = False\n"
        "ok\u3164 = True\n"  # U+3164 HANGUL FILLER: a valid identifier character, invisible
        "x\ufe0f = '\u00a0\u034f\u180e\U000e0001'  # \u00a7 \u2192 \u00e9 \u4e2d\u6587\n"
    )

    def change(repo):
        (repo / "src/zab/admin/config.py").write_text(
            (repo / "src/zab/admin/config.py").read_text() + hidden
        )
        (repo / "src/zab/bridge.py").write_text("# an unreviewed bridge change\n")

    server.release("v0.3.1", edit=change)
    result = server.deploy("v0.3.1")
    assert result.returncode == 0, result.stderr
    assert "+# a reviewed change<U+202E>" in result.stdout  # shown, not obeyed, by the terminal
    assert "+ok = False\n+ok<U+3164> = True\n" in result.stdout
    assert (
        "+x<U+FE0F> = '<U+00A0><U+034F><U+180E><U+E0001>'  # \u00a7 \u2192 \u00e9 \u4e2d\u6587"
        in (result.stdout)
    )
    assert "bridge.py" not in result.stdout
    diff = (server.tmp / "out" / "zab-admin-v0.3.0..v0.3.1.diff").read_text()
    assert "\u202e" in diff  # the file is the raw diff, which the SHA-256 covers


def test_a_path_new_to_the_list_is_shown_whole(server):
    """A module joins the release: its unchanged file is shown in full, not as no change.
    (zab.zulip, the example once, is in the release since #341: the bot's Zulip client.)"""
    (server.repo / "src/zab/telemetry").mkdir()
    (server.repo / "src/zab/telemetry/client.py").write_text("SITE = 'x'\n")
    server.git("add", "-A")
    server.git("commit", "-q", "-m", "a module outside the release")
    server.release("v0.3.1")
    result = server.deploy("v0.3.1")
    assert "client.py" not in result.stdout

    def add_to_list(repo):
        with (repo / "workshop/adminserver/release-paths").open("a") as f:
            f.write("src/zab/telemetry/client.py\n")

    server.release("v0.3.2", edit=add_to_list)
    result = server.deploy("v0.3.2")
    assert result.returncode == 0, result.stderr
    assert "+++ b/src/zab/telemetry/client.py\n@@ -0,0 +1 @@\n+SITE = 'x'" in result.stdout
    assert (server.opt / "releases/v0.3.2/src/zab/telemetry/client.py").exists()


def test_review_before_signing_matches_the_deploy(server):
    """The review ends with the commands to sign, verify and push the commit's own version, in
    the clone it read (#468). Pasted as printed, in a directory that holds no clone, they make
    the tag the deploy takes, with the SHA-256 the review printed."""
    server.release("v0.3.1", signer="light")  # not signed yet: Drew reviews, then signs
    commit = server.git("rev-parse", "v0.3.1^{commit}")
    server.git("tag", "-d", "v0.3.1")
    origin = server.tmp / "origin.git"
    server.run("git", "init", "-q", "--bare", origin, check=True)
    server.git("remote", "add", "origin", str(origin))
    clone = server.tmp / "my zab"  # a path the shell must be given quoted
    clone.symlink_to(server.repo)
    where = ["--repo", clone, "--etc", server.etc, "--out", server.tmp / "out"]
    review = server.run("bash", DEPLOY, *where, "--review", commit, "--since", "v0.3.0")
    assert review.returncode == 0, review.stderr
    reviewed = re.search(r"SHA-256 ([0-9a-f]{64})", review.stderr).group(1)
    git = "git -C " + str(clone).replace(" ", "\\ ")
    printed = [
        f'{git} tag -s v0.3.1 -m "zab v0.3.1" {commit}',
        f"{git} tag -v v0.3.1",
        f"{git} push origin v0.3.1",
    ]
    tail = "Sign it once reviewed, then verify and push it:\n" + "".join(
        f"  {line}\n" for line in printed
    )
    assert review.stderr.endswith(f"\n{tail}"), review.stderr
    signing = {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "gpg.format",
        "GIT_CONFIG_VALUE_0": "ssh",
        "GIT_CONFIG_KEY_1": "user.signingkey",
        "GIT_CONFIG_VALUE_1": str(server.keys["drew"]),
        "GIT_CONFIG_KEY_2": "gpg.ssh.allowedSignersFile",
        "GIT_CONFIG_VALUE_2": str(server.etc / "allowed_signers"),
    }
    for line in printed:  # as Drew pastes them, in ~/zab-admin (the review's --out)
        done = call(
            ["bash", "-c", line],
            env={**server.env, **signing},
            cwd=server.tmp / "out",
            input="",
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, (line, done.stderr)
    assert server.run("git", "-C", origin, "rev-parse", "v0.3.1^{commit}").stdout.strip() == commit
    tag = server.git("cat-file", "tag", "v0.3.1")
    assert "\n\nzab v0.3.1\n-----BEGIN SSH SIGNATURE-----\n" in tag
    result = server.deploy("v0.3.1")
    assert result.returncode == 0, result.stderr
    assert server.records()[-1]["diff_sha256"] == reviewed


def review(rig, version, since="v0.3.0", edit=None):
    """`deploy.sh --review` of a release commit not yet signed, since the signed `since`. Returns
    the run, the commit, and the raw diff's path."""
    rig.release(version, signer="light", edit=edit)
    commit = rig.git("rev-parse", f"{version}^{{commit}}")
    rig.git("tag", "-d", version)
    where = ["--repo", rig.repo, "--etc", rig.etc, "--out", rig.tmp / "out"]
    done = rig.run("bash", DEPLOY, *where, "--review", commit, "--since", since)
    return done, commit, rig.tmp / "out" / f"zab-admin-{since}..{commit[:12]}.diff"


def summary(stdout: str) -> dict[str, str]:
    """The review's line of each file, by path: `P M     +1     -0  text    PATH` gives
    `P M +1 -0 text`, and `  M ...` gives `M ...`."""
    found = {}
    for line in stdout.split("\n\n")[1].splitlines():  # the block after the legend
        status, added, removed, kind, path = line[2:].split(maxsplit=4)
        found[path] = " ".join([*line[0].split(), status, added, removed, kind])
    return found


@pytest.mark.parametrize(
    "edit",
    [
        lambda text: text.replace('version = "0.3.1"\n', ""),
        lambda text: text.replace('"0.3.1"', '"0.3.1.dev0"'),  # no release name
        lambda text: text + '\n[tool.other]\nversion = "0.3.2"\n',  # a deploy takes either
    ],
    ids=["none", "not-a-release", "two"],
)
def test_the_review_prints_no_tag_command_without_one_version(released, edit):
    """#468: the tag is the version of the commit's pyproject.toml; with none that a deploy
    would take, or two, the review says so instead of printing a command."""

    def change(repo):
        path = repo / "pyproject.toml"
        path.write_text(edit(path.read_text()))

    done, commit, _ = review(released, "v0.3.1", edit=change)
    assert done.returncode == 0, done.stderr
    no = f'No tag command: {commit}\'s pyproject.toml has no single version = "X.Y.Z" line\n'
    assert done.stderr.endswith(f"\n{no}"), done.stderr
    assert "tag -s" not in done.stderr


def test_the_review_prints_no_tag_command_for_a_tag_that_exists(released):
    """#468: the clone already has a tag of the commit's version: no command."""
    released.release("v0.3.1", signer="light")
    commit = released.git("rev-parse", "v0.3.1^{commit}")
    where = ["--repo", released.repo, "--etc", released.etc, "--out", released.tmp / "out"]
    done = released.run("bash", DEPLOY, *where, "--review", commit, "--since", "v0.3.0")
    assert done.returncode == 0, done.stderr
    no = f"No tag command: {released.repo} already has a tag v0.3.1\n"
    assert done.stderr.endswith(f"\n{no}"), done.stderr
    assert "tag -s" not in done.stderr


def test_the_review_lists_every_file_then_shows_the_privileged_ones_first(released):
    """#432, D31: one line per file, P for privileged, and uv.lock's packages; then the
    privileged files' diff, then the rest's. A privileged binary is shown, sanitized; the raw
    diff, whose SHA-256 the deploy records, holds every file."""

    def change(repo):
        with (repo / "workshop/adminserver/deploy.sh").open("a") as f:
            f.write("# a privileged change\n")
        with (repo / "src/zab/admin/audit.py").open("a") as f:
            f.write("# an unprivileged change\n")
        (repo / "workshop/adminserver/blob.bin").write_bytes(b"\x00SHOWN-BYTES\n")

    done, commit, raw = review(released, "v0.3.1", edit=change)
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert out.startswith(f"zab admin release review: v0.3.0..{commit[:12]} ({commit})\n")
    lines = summary(out)
    assert lines["workshop/adminserver/deploy.sh"] == "P M +1 -0 text"
    assert lines["src/zab/admin/audit.py"] == "M +1 -0 text"
    assert lines["workshop/adminserver/blob.bin"] == "P A - - binary"
    assert lines["pyproject.toml"] == "P M +1 -1 text"  # the version
    assert "uv.lock" not in lines and "\nuv.lock, " not in out  # unchanged: not shown
    privileged, rest = out.split("\n==== The rest ")
    assert "==== The privileged files (3): read every line ====" in privileged
    assert "==== Newly privileged since v0.3.0 (0): read them whole once ====" in privileged
    assert "+# a privileged change" in privileged and "an unprivileged change" not in privileged
    assert "+^@SHOWN-BYTES" in privileged  # privileged: always shown, sanitized
    assert "+# an unprivileged change" in rest
    sha = re.search(r"SHA-256 ([0-9a-f]{64}); release tree", done.stderr).group(1)
    assert sha == hashlib.sha256(raw.read_bytes()).hexdigest()
    shown = raw.with_suffix(".review")
    assert shown.read_text() == out  # the review, as shown
    assert f"The review, as shown, is {shown}: 4 files, 3 privileged\n" in done.stderr
    for line in (
        "M pyproject.toml",
        "M workshop/adminserver/deploy.sh",
        "A workshop/adminserver/blob.bin",
    ):
        assert f"\n    P {line}\n" in done.stderr, line


@pytest.mark.parametrize(
    ("name", "content", "reason"),
    [
        ("src/zab/admin/blob.bin", b"\x00code\n", "blob.bin: a binary file outside"),
        ("src/zab/admin/__pycache__/config.cpython-312.pyc", b"x", "not allowed in a release"),
        ("src/zab/admin/stale.pyc", b"x", "not allowed in a release path"),
        ("src/zab/admin/fast.so", b"x", "not allowed in a release path"),
        ("src/zab/admin/start.pth", b"import os\n", "not allowed in a release path"),
        ("src/zab/admin/café.py", b"", "a file name of other characters"),
        ("src/zab/admin/a b.py", b"", "a file name of other characters"),
        ("src/zab/admin/x\u202eyp.py", b"", "a file name of other characters"),
    ],
)
def test_the_review_refuses_what_it_could_not_show(released, name, content, reason):
    """#436: a binary outside privileged-paths could be code that the review only lists (a pyc
    shadows its source under -I -B); a name could hide from the terminal. Both are refused."""

    def add(repo):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(content)

    done, _, _ = review(released, "v0.3.1", edit=add)
    refused(done, re.escape(reason))


def test_a_deleted_binary_is_listed_not_shown(released):
    """A binary that leaves the release, from a base that held one, is listed and not shown; the
    review still accounts for every section of the diff."""
    old = "src/zab/admin/old.bin"
    released.release("v0.3.1", edit=lambda repo: (repo / old).write_bytes(b"\x00old"))
    done, _, raw = review(released, "v0.3.2", "v0.3.1", lambda repo: (repo / old).unlink())
    assert done.returncode == 0, done.stderr
    assert summary(done.stdout)[old] == "D - - binary"
    assert f"Binary, not shown: {old} (blob " in done.stdout
    assert "\x00old" in raw.read_text(errors="replace")  # the hashed diff holds it


def test_a_type_change_is_one_file_and_two_sections(released):
    """git shows a type change as a deletion and an addition: the review lists it once, and
    accounts for both sections of the reviewed diff."""
    released.release("v0.3.1", edit=lambda repo: (repo / "src/zab/admin/link").symlink_to("x"))

    def to_file(repo):
        (repo / "src/zab/admin/link").unlink()
        (repo / "src/zab/admin/link").write_text("text\n")

    done, _, raw = review(released, "v0.3.2", since="v0.3.1", edit=to_file)
    assert done.returncode == 0, done.stderr
    assert summary(done.stdout)["src/zab/admin/link"] == "T +1 -1 text"
    assert raw.read_text().count("diff --git a/src/zab/admin/link ") == 2
    assert done.stdout.count("diff --git a/src/zab/admin/link ") == 2


def test_a_file_that_joins_the_privileged_set_is_shown_whole_once(released):
    """#436: privileged at the commit and not at the base: shown whole once, changed or not, and
    named after the SHA-256, which still covers the diff alone. A base without the list (v0.2.4)
    has workshop/adminserver alone."""
    plist = PRIVILEGED.relative_to(ROOT)
    released.release("v0.3.1", edit=lambda repo: (repo / plist).unlink())

    def restore(repo):
        shutil.copy(PRIVILEGED, repo / plist)
        with (repo / "src/zab/admin/checks.py").open("a") as f:
            f.write("# a change\n")

    done, _, raw = review(released, "v0.3.2", since="v0.3.1", edit=restore)
    assert done.returncode == 0, done.stderr
    lines = summary(done.stdout)
    assert lines["src/zab/admin/checks.py"] == "P M +1 -0 text"
    assert lines[str(plist)].startswith("P A ")  # new: shown whole in the diff
    newly = done.stdout.split("==== Newly privileged since v0.3.1 (")[1].split("\n==== The rest")[0]
    for path in ("config.py", "checks.py", "confighelper.py"):
        assert f"+++ b/src/zab/admin/{path}\n@@ -0,0 " in newly, path  # unchanged ones too
    assert f"+++ b/{plist}" not in newly  # new: in the diff already
    # read as data (D32): uv.lock by package, build-constraints.txt by pin, never their hashes
    assert "+++ b/uv.lock" not in newly and "+++ b/build-constraints.txt" not in newly
    pins = "\nbuild-constraints.txt, every pin (name, versions); not its hashes, which the tests"
    assert pins in newly and "\n  fakebackend (new) -> 1.0\n" in newly
    assert "\nuv.lock, every package (name, versions, source); not its hashes" in newly
    assert '\n  "fakecli" (new) -> "1.0"  ' in newly and "sha256:" not in newly.split(pins)[1]
    assert "  Newly privileged, shown whole once: " in done.stderr
    assert "uv.lock" in done.stderr.split("Newly privileged")[1]
    assert raw.read_text().count("+++ b/src/zab/admin/config.py") == 0  # display only


def test_a_path_that_leaves_the_privileged_set_is_still_marked_once(released):
    """#436: P is the union of the base's set and the commit's, so a change in the release that
    drops a path from privileged-paths is still read as privileged."""
    plist = PRIVILEGED.relative_to(ROOT)

    def drop(repo):
        text = (repo / plist).read_text().replace("src/zab/admin/confighelper.py\n", "")
        (repo / plist).write_text(text)
        with (repo / "src/zab/admin/confighelper.py").open("a") as f:
            f.write("# a change\n")

    done, _, _ = review(released, "v0.3.1", edit=drop)
    assert done.returncode == 0, done.stderr
    assert summary(done.stdout)["src/zab/admin/confighelper.py"] == "P M +1 -0 text"


def test_a_commit_without_the_privileged_list_after_one_with_it_is_refused(released):
    done, _, _ = review(
        released, "v0.3.1", edit=lambda repo: (repo / PRIVILEGED.relative_to(ROOT)).unlink()
    )
    refused(done, "privileged-paths: the base has one")


def test_the_review_shows_uv_lock_and_the_pins_as_data(released):
    """#436, D32, display only: a changed uv.lock or build-constraints.txt is shown in the
    privileged section by package and by pin, never by its hash lines: each changed one, its
    versions (and source), with a hash changed under an unchanged version flagged, and the rig's
    own index, which is not PyPI. The raw diff, in the .diff file, holds the hashes."""

    def rehash(repo):
        lock = (repo / "uv.lock").read_text()
        block = lock.split('name = "fakecli"')[1].split("[[package]]")[0]
        digest = re.search(r"sha256:([0-9a-f]{64})", block).group(1)
        (repo / "uv.lock").write_text(lock.replace(digest, "0" * 64))
        pins = (repo / "build-constraints.txt").read_text()
        (repo / "build-constraints.txt").write_text(
            re.sub("sha256:[0-9a-f]{64}", "sha256:" + "1" * 64, pins)
        )

    done, _, raw = review(released, "v0.3.1", edit=rehash)
    assert done.returncode == 0, done.stderr
    lines = summary(done.stdout)
    assert lines["uv.lock"].startswith("P M ") and lines["build-constraints.txt"].startswith("P M ")
    privileged = done.stdout.split("==== The privileged files")[1].split("==== Newly")[0]
    assert "+++ b/uv.lock" not in privileged and "+++ b/build-constraints.txt" not in privileged
    assert "sha256:" not in privileged
    assert "\nuv.lock, each package changed (name, versions, source); not its hashes" in privileged
    head = '  "fakecli" "1.0" -> "1.0"  '
    (line,) = [ln for ln in privileged.splitlines() if ln.startswith(head)]
    assert "** SAME VERSION, OTHER FILES" in line and "** NOT FROM PyPI" in line
    assert (
        "\nbuild-constraints.txt, each pin changed (name, versions); not its hashes" in privileged
    )
    assert "\n  fakebackend 1.0 -> 1.0  ** SAME VERSION, OTHER HASHES\n" in privileged
    assert privileged.count("(its raw diff is in the .diff file, which the SHA-256 covers)") == 2
    text = raw.read_text()
    assert "+++ b/uv.lock" in text and "+++ b/build-constraints.txt" in text  # the hashed diff


@pytest.mark.parametrize(
    "where",
    [
        "a second hash on the hash line",
        "a hash on the pin's own line",
        "another requirement",
        "a second line for one name",
    ],
)
def test_the_pins_summary_reads_every_hash_and_line(released, where):
    """Codex on 43350d1: build-constraints.txt's summary takes every --hash, on the pin's line
    and after it, several on one line included, and shows a requirement it can't read as a pin
    as written; review round 2: and every line of one name, in any case. So no change to what
    the build accepts goes unshown."""
    extra = "sha256:" + "2" * 64

    def edit(repo):
        text = (repo / "build-constraints.txt").read_text()
        if where == "a second hash on the hash line":
            text = re.sub(r"(--hash=sha256:[0-9a-f]{64})", rf"\1 --hash={extra}", text, count=1)
        elif where == "a hash on the pin's own line":
            text = text.replace("fakebackend==1.0 \\", f"fakebackend==1.0 --hash={extra} \\", 1)
        elif where == "a second line for one name":  # the old pin, after a changed one
            text = text.replace("fakebackend==1.0", "fakebackend==1.1", 1) + text.replace(
                "fakebackend==1.0", "FakeBackend==1.0", 1
            )
        else:
            text += 'other[cli]==2.0 ; python_version < "3.12"\n'
        assert text != (repo / "build-constraints.txt").read_text()
        (repo / "build-constraints.txt").write_text(text)

    done, _, _ = review(released, "v0.3.1", edit=edit)
    assert done.returncode == 0, done.stderr
    privileged = done.stdout.split("==== The privileged files")[1].split("==== Newly")[0]
    if where == "another requirement":
        line = '\n  other[cli]==2.0 ; python_version < "3.12" (new) -> (as written)\n'
        assert line in privileged
    elif where == "a second line for one name":
        assert "\n  fakebackend 1.0 -> 1.0, 1.1\n" in privileged
    else:
        assert "\n  fakebackend 1.0 -> 1.0  ** SAME VERSION, OTHER HASHES\n" in privileged
    assert "sha256:" not in privileged


def test_the_lock_summary_shows_dependencies_markers_and_lock_fields(released):
    """Codex on fbb4304: with uv.lock's raw diff out of the review, every other change must show
    there too. A dependency added to a package and a lock-level field (requires-python) appear,
    each by field, without a hash; a dependency removed appears as removed."""

    dependency = 'dependencies = [{ name = "httpx", marker = "sys_platform == \'linux\'" }]\n'

    def edit(repo):
        lock = (repo / "uv.lock").read_text()
        lock, n = re.subn(
            r'(name = "fakecli"\nversion = "1\.0"\nsource = [^\n]*\n)',
            lambda m: m.group(1) + dependency,
            lock,
            count=1,
        )
        assert n == 1
        lock, n = re.subn(r'(?m)^requires-python = "[^"]*"$', 'requires-python = ">=3.12"', lock)
        assert n == 1
        (repo / "uv.lock").write_text(lock)

    done, commit, _ = review(released, "v0.3.1", edit=edit)
    assert done.returncode == 0, done.stderr
    shown = done.stdout.split("\nuv.lock, each package changed")[1].split("\n\n")[0]
    assert '\n  (the lock) "requires-python": ">=3.11" -> ">=3.12"\n' in shown
    assert (
        '\n      "1.0" "dependencies": +{"marker": "sys_platform == \'linux\'", "name": "httpx"}'
        in shown
    )
    assert "sha256:" not in shown
    released.sign("v0.3.1", commit)  # the base of the next review

    def drop(repo):
        lock = (repo / "uv.lock").read_text()
        assert dependency in lock
        (repo / "uv.lock").write_text(lock.replace(dependency, ""))

    done, _, _ = review(released, "v0.3.2", "v0.3.1", drop)
    assert done.returncode == 0, done.stderr
    assert (
        '\n      "1.0" "dependencies": -{"marker": "sys_platform == \'linux\'", "name": "httpx"}'
        in (done.stdout)
    )


def test_the_lock_summary_shows_fields_across_a_version_change(released):
    """Review round 2 on #436: the project's entry changes version at every release, and its
    `admin` group is what the server installs; with one entry on each side, its fields are
    compared whatever the versions."""

    def bump(repo):
        lock = (repo / "uv.lock").read_text()
        blocks = lock.split("[[package]]\n")
        (i,) = [i for i, b in enumerate(blocks) if 'name = "zulip-agent-bridge"\n' in b]
        block, n = re.subn(r'(?m)^version = "0\.3\.0"$', 'version = "0.3.1"', blocks[i])
        block, m = re.subn(
            r"(?m)^admin = \[\n", 'admin = [\n    { name = "extra-pkg" },\n', block, count=1
        )
        assert (n, m) == (1, 1)
        blocks[i] = block
        (repo / "uv.lock").write_text("[[package]]\n".join(blocks))

    done, _, _ = review(released, "v0.3.1", edit=bump)
    assert done.returncode == 0, done.stderr
    shown = done.stdout.split("\nuv.lock, each package changed")[1].split("\n\n")[0]
    assert '\n  "zulip-agent-bridge" "0.3.0" -> "0.3.1"  "editable ."\n' in shown
    assert re.search(r'\n      "0\.3\.1" "[\w.-]*admin": \+\{"name": "extra-pkg"\}\n', shown), shown


@pytest.mark.parametrize("failure", ["no tomllib", "an entry without a name"])
def test_a_summary_that_fails_shows_the_raw_diff(released, failure):
    """Review round 2 on #436: fail closed. When a summary can't be made (macOS's python3 3.9
    has no tomllib; a lock it can't read), the file's raw diff takes its place in the privileged
    section, counted as shown, with the error on stderr, and the review still accounts for
    every section."""
    if failure == "no tomllib":  # every python3 on the PATH fails as 3.9 would
        fake = released.tmp / "bin" / "python3"
        fake.write_text(
            "#!/bin/sh\necho \"ModuleNotFoundError: No module named 'tomllib'\" >&2\nexit 1\n"
        )
        fake.chmod(0o755)

    def edit(repo):
        lock = (repo / "uv.lock").read_text()
        if failure == "an entry without a name":
            lock = lock.replace('name = "fakecli"\n', "", 1)
        else:
            lock = lock.replace('requires-python = ">=3.11"', 'requires-python = ">=3.12"', 1)
        (repo / "uv.lock").write_text(lock)
        with (repo / "build-constraints.txt").open("a") as f:
            f.write("# a change\n")

    done, _, _ = review(released, "v0.3.1", edit=edit)
    assert done.returncode == 0, done.stderr
    privileged = done.stdout.split("==== The privileged files")[1].split("==== Newly")[0]
    note = "uv.lock: no summary (python3 failed; its error went to stderr): its raw diff instead."
    assert f"\n{note}\n" in privileged and "+++ b/uv.lock\n" in privileged
    if failure == "no tomllib":
        assert "No module named 'tomllib'" in done.stderr
        assert "+++ b/build-constraints.txt\n" in privileged  # the same for the pins
    else:
        assert "KeyError" in done.stderr
        assert "\nbuild-constraints.txt, each pin changed" in privileged  # its own summary
    assert "(none shown" not in done.stdout


def test_the_lock_summary_reads_every_version_of_a_package(released):
    """Codex on d3e70ce: a lock may hold two versions of one package (for two markers). The
    summary keeps each: one whose files change is flagged, and both versions are listed."""

    def second(repo):  # fakecli 0.9 beside 1.0
        lock = (repo / "uv.lock").read_text()
        (entry,) = [b for b in lock.split("[[package]]\n") if 'name = "fakecli"\n' in b]
        block = "[[package]]\n" + entry.replace('version = "1.0"', 'version = "0.9"')
        (repo / "uv.lock").write_text(lock.rstrip("\n") + "\n\n" + block.rstrip("\n") + "\n")

    released.release("v0.3.1", edit=second)

    def rehash_the_first(repo):  # 1.0's, which a summary keyed by name alone would drop
        lock = (repo / "uv.lock").read_text()
        blocks = lock.split("[[package]]\n")
        i = next(i for i, b in enumerate(blocks) if 'name = "fakecli"\n' in b)
        digest = re.search(r"sha256:([0-9a-f]{64})", blocks[i]).group(1)
        blocks[i] = blocks[i].replace(digest, "1" * 64)
        (repo / "uv.lock").write_text("[[package]]\n".join(blocks))

    done, _, _ = review(released, "v0.3.2", "v0.3.1", rehash_the_first)
    assert done.returncode == 0, done.stderr
    (line,) = [ln for ln in done.stdout.splitlines() if ln.startswith('  "fakecli" ')]
    assert line.startswith('  "fakecli" "0.9", "1.0" -> "0.9", "1.0"  ')
    assert "** SAME VERSION, OTHER FILES" in line


@pytest.mark.parametrize("where", ["name", "version", "source", "field"])
def test_no_string_in_the_lock_makes_a_line_of_its_own(released, where):
    """Review round 3 of #436 (#435): the lock summary prints every name, version, source and
    field key from uv.lock as JSON, so a TOML `\\n` in one can't add a line to the review that
    looks like the summary's own. (uv refuses such a lock, so nothing would install it.)"""
    forged = "  forged 1.0 -> 1.0  https://pypi.org/simple"
    entry = r'(name = "fakecli)("\nversion = "1\.0)("\nsource = \{ registry = "[^"\n]*)(" \}\n)'

    def edit(repo):
        lock = (repo / "uv.lock").read_text()
        group = {"name": 1, "version": 2, "source": 3, "field": 4}[where]

        def forge(m):
            parts = list(m.groups())
            if where == "field":  # a key of its own in fakecli's entry
                parts[3] += f'"x\\n{forged}" = 1\n'
            else:
                parts[group - 1] += f"\\n{forged}"
            return "".join(parts)

        lock, n = re.subn(entry, forge, lock, count=1)
        assert n == 1, lock
        (repo / "uv.lock").write_text(lock)

    done, _, _ = review(released, "v0.3.1", edit=edit)
    assert done.returncode == 0, done.stderr
    shown = done.stdout.split("\nuv.lock, each package changed")[1].split("\n\n")[0]
    assert f"\\n{forged}" in shown  # escaped, on the line it belongs to
    assert not [ln for ln in done.stdout.splitlines() if ln.startswith(forged)], shown


def test_the_reviewed_diff_is_the_release_paths_diff(released):
    """The SHA-256 covers what it did before #432: the release paths' diff-tree since the base, in
    git's order, when the list keeps its shape."""

    def change(repo):
        with (repo / "workshop/admin/status.py").open("a") as f:
            f.write("# a change\n")

    done, commit, raw = review(released, "v0.3.1", edit=change)
    assert done.returncode == 0, done.stderr
    opts = ["-r", "-p", "-a", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames"]
    opts += ["--full-index", "--diff-algorithm=myers", "--indent-heuristic"]
    paths = listed(LIST)
    expected = released.run(
        "git", "-C", released.repo, "diff-tree", *opts, "v0.3.0", commit, "--", *paths, check=True
    )
    assert raw.read_text() == expected.stdout


def test_a_path_under_one_the_list_held_is_diffed_not_shown_whole(released):
    """#432: v0.2.4 listed `workshop`; v0.2.5 names its files. A file under a path the base
    listed is shown as its diff since the base, not whole as a path new to the list."""

    def widen(repo):
        kept = [p for p in listed(repo / LIST.relative_to(ROOT)) if not p.startswith("workshop")]
        (repo / LIST.relative_to(ROOT)).write_text("\n".join([*kept, "workshop"]) + "\n")

    released.release("v0.3.1", edit=widen)

    def narrow(repo):
        shutil.copy(LIST, repo / LIST.relative_to(ROOT))
        with (repo / "workshop/admin/status.py").open("a") as f:
            f.write("# a change\n")

    done, _, raw = review(released, "v0.3.2", since="v0.3.1", edit=narrow)
    assert done.returncode == 0, done.stderr
    lines = summary(done.stdout)
    assert lines["workshop/admin/status.py"] == "M +1 -0 text"
    assert lines["workshop/adminserver/release-paths"].startswith("P M ")
    assert "workshop/admin/push_config.py" not in lines  # unchanged: not shown
    assert "workshop/files/released.py" not in lines  # no longer a release path
    assert "new file mode" not in raw.read_text()


@pytest.mark.parametrize(
    ("paths", "reason"),
    [
        (["src/zab/admin/confighelper.py"], "privileged-paths must list workshop/adminserver"),
        (["workshop/adminserver", "workshop/ami/build.sh"], "build.sh is not a release path"),
    ],
)
def test_the_privileged_list_covers_the_server_files_and_only_release_paths(
    released, paths, reason
):
    def edit(repo):
        (repo / PRIVILEGED.relative_to(ROOT)).write_text("\n".join(paths) + "\n")

    done, _, _ = review(released, "v0.3.1", edit=edit)
    refused(done, reason)


# --- fleet wheels -----------------------------------------------------------------------------


def test_a_fleet_wheel_is_built_with_the_pinned_backend_and_placed(server):
    # a relative --repo, as from a checkout's parent (Codex round 2 on #436: check_tree runs git
    # from another directory)
    where = ["--repo", "repo", "--etc", server.etc, "--out", server.tmp / "out"]
    argv = ["bash", DEPLOY, *where, "--host", "admin.test", "--wheel", "v0.3.0"]
    result = server.run(*argv, input="v0.3.0\n", cwd=server.tmp)
    assert result.returncode == 0, result.stderr
    wheel = (
        server.prefix
        / "var/lib/zab-admin/releases/wheels/zulip_agent_bridge-0.3.0-py3-none-any.whl"
    )
    # #436: built from what the wheel needs alone (the rig has no LICENSE or .gitignore)
    assert " uv build -q --wheel --no-sources " in DEPLOY.read_text()  # no [tool.uv.sources]
    built_in = zipfile.ZipFile(wheel).read("zab_fake_wheel.txt").decode().split()
    assert built_in == ["README.md", "build-constraints.txt", "pyproject.toml", "src"]
    record = server.records()[-1]
    assert record["event"] == "wheel"
    assert record["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert server.current().endswith("v0.3.0")  # the server's own release is untouched


def test_a_backend_that_fails_its_hash_is_refused(released):
    def bad_hash(repo):
        text = (repo / "build-constraints.txt").read_text()
        flip = lambda m: "sha256:" + ("1" if m.group(1) == "0" else "0")  # noqa: E731
        bad = re.sub("sha256:(.)", flip, text)
        assert bad != text
        (repo / "build-constraints.txt").write_text(bad)

    released.release("v0.3.1", edit=bad_hash)
    refused(released.deploy("v0.3.1", "--wheel"), "did not build with the pinned backend")
    assert not (released.prefix / "var/lib/zab-admin").exists()  # refused on the laptop


HOOK_CONFIG = {
    "hatch.toml": ("hatch.toml", '[build.hooks.custom]\npath = "docs/hook.py"\n'),
    "pyproject hook": (
        "pyproject.toml",
        '\n[tool.hatch.build.hooks.custom]\npath = "src/zab/hook.py"\n',
    ),
    "backend-path": ("pyproject.toml", None),
    "dynamic": ("pyproject.toml", None),
    # review round 2: [tool.uv.sources] would build the backend from the tree (uv build also
    # gets --no-sources)
    "uv sources": ("pyproject.toml", '\n[tool.uv.sources]\nhatchling = { path = "src/zab" }\n'),
}


@pytest.mark.parametrize("case", sorted(HOOK_CONFIG))
def test_a_fleet_wheel_runs_no_code_of_the_tree(released, case):
    """#436: the build's configuration may run code of the tree on the laptop (a hatch.toml or a
    hook in pyproject.toml, which the stand-in backend obeys as hatchling does; an in-tree
    backend; a dynamic version or metadata). Without deploy.sh's checks the hook runs (the
    control); with them, --wheel is refused and it doesn't."""
    marker = released.tmp / "HOOK-RAN"
    hook = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n"
    name, extra = HOOK_CONFIG[case]

    def configure(repo):
        for path in ("docs/hook.py", "src/zab/hook.py"):
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(hook)
        text = (repo / name).read_text() if (repo / name).exists() else ""
        if case == "backend-path":
            extra_text = text.replace("[build-system]\n", '[build-system]\nbackend-path = ["."]\n')
        elif case == "dynamic":
            extra_text = text.replace("[project]\n", '[project]\ndynamic = ["readme"]\n')
        else:
            extra_text = text + extra
        (repo / name).write_text(extra_text)

    released.release("v0.3.1", edit=configure)
    if case in ("hatch.toml", "pyproject hook"):  # the control: the same build, unguarded
        tree, tar = released.tmp / "control", released.tmp / "control.tar"
        tree.mkdir()
        released.git("archive", "-o", str(tar), "v0.3.1")
        released.run("tar", "-xf", tar, "-C", tree, check=True)
        build = ["uv", "build", "-q", "--wheel", "--build-constraints", "build-constraints.txt"]
        released.run(*build, "--require-hashes", "-o", released.tmp / "control-dist", cwd=tree)
        assert marker.read_text() == "ran", "the stand-in backend should obey the hook"
        marker.unlink()
    result = released.deploy("v0.3.1", "--wheel")
    refused(result, "hatch.toml, which hatchling would obey|configures the build beyond")
    assert not marker.exists()
    assert not (released.prefix / "var/lib/zab-admin").exists()  # refused before the server


def test_a_fleet_wheel_builds_the_blobs_not_what_gitattributes_make_of_them(released):
    """Codex on 40b75d8: `git archive` obeys the tree's .gitattributes, so export-subst could turn
    a comment of the checked pyproject.toml into a hook. The unpacked build tree must be the
    commit's blobs (check_tree, as on the server), or --wheel is refused."""
    marker = released.tmp / "HOOK-RAN"
    comment = '# $Format:%x0a[tool.hatch.build.hooks.custom]%x0apath = "src/zab/hook.py"$\n'

    def subst(repo):
        (repo / ".gitattributes").write_text("pyproject.toml export-subst\n")
        (repo / "pyproject.toml").write_text((repo / "pyproject.toml").read_text() + comment)
        (repo / "src/zab/hook.py").write_text(
            f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n"
        )

    released.release("v0.3.1", edit=subst)
    archived = released.tmp / "archived"
    archived.mkdir()
    released.git("archive", "-o", str(archived / "a.tar"), "v0.3.1", "--", "pyproject.toml")
    released.run("tar", "-xf", archived / "a.tar", "-C", archived, check=True)
    assert "\n[tool.hatch.build.hooks.custom]\n" in (archived / "pyproject.toml").read_text()
    refused(released.deploy("v0.3.1", "--wheel"), "the wheel build's tree differs from v0.3.1's")
    assert not marker.exists()


def test_a_fleet_wheel_still_needs_a_signed_tag(released):
    released.release("v0.3.1", signer="mallory")
    refused(released.deploy("v0.3.1", "--wheel"), "not signed by a key in")


# --- consistency ------------------------------------------------------------------------------


def _closure_files(start: list[pathlib.Path]) -> set[str]:
    """The source files of the import closure of `start` (statically: every import statement,
    in functions too), each with its parent packages."""
    import ast

    src = ROOT / "src"
    todo = list(start)
    seen: set[pathlib.Path] = set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        parts = list(path.relative_to(src).with_suffix("").parts)
        package = parts[:-1]  # a module's package; for __init__.py, its own
        for i in range(1, len(package) + 1):
            todo.append(src.joinpath(*package[:i], "__init__.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = package[: len(package) - node.level + 1] if node.level else []
                module = [*base, *(node.module.split(".") if node.module else [])]
                names = [".".join(module)] + [".".join([*module, a.name]) for a in node.names]
            for name in names:
                bits = name.split(".")
                if bits[0] != "zab":
                    continue
                for i in range(1, len(bits) + 1):
                    for cand in (
                        src.joinpath(*bits[:i]).with_suffix(".py"),
                        src.joinpath(*bits[:i], "__init__.py"),
                    ):
                        if cand.is_file():
                            todo.append(cand)
    return {str(p.relative_to(ROOT)) for p in seen}


def test_the_release_paths_cover_the_admin_import_closure():
    """What the server runs is `python -m zab.admin …` (deploy.sh's selfcheck and every unit) and
    the scripts of workshop/: the closure of zab.admin.__main__, with all its subcommands, and of
    every other zab.admin module, lies in release-paths, whatever it reaches (zab.render now;
    zab.waker, say, if the starter ever imported it)."""
    main = ROOT / "src/zab/admin/__main__.py"
    from_main = _closure_files([main])
    # the subcommands' modules are reached, through `from zab.admin import audit, starter`
    for module in ("audit", "starter", "config", "scrub"):
        assert f"src/zab/admin/{module}.py" in from_main, module
    everything = from_main | _closure_files(sorted((ROOT / "src/zab/admin").rglob("*.py")))
    for file in sorted(everything):
        assert any(file == p or file.startswith(p + "/") for p in RELEASE_PATHS), (
            f"{file}: add it to {LIST.name}"
        )


def test_the_release_paths_cover_the_wakers_import_closure():
    """#348: zab-waker.service runs `python -I -m zab.waker run`, and the `waker` console script
    is the same `main`: their closure, both modes, lies in release-paths."""
    unit = (ROOT / "workshop/adminserver/zab-waker.service").read_text()
    assert re.search(r"^ExecStart=\S+/python -I -m zab\.waker run ", unit, re.M)
    project = (ROOT / "pyproject.toml").read_text()
    assert 'waker = "zab.waker.__main__:main"' in project
    closure = _closure_files([ROOT / "src/zab/waker/__main__.py"])
    for module in ("service", "starter_client", "queues"):
        assert f"src/zab/waker/{module}.py" in closure, module
    assert "src/zab/zulip/client.py" in closure and "src/zab/zulip/poster.py" in closure
    for file in sorted(closure):
        assert any(file == p or file.startswith(p + "/") for p in RELEASE_PATHS), (
            f"{file}: add it to {LIST.name}"
        )
    assert len(RELEASE_PATHS) == len(set(RELEASE_PATHS)), "a path is listed twice"
    # `waker selfcheck`, which the deploy runs, imports exactly the closure's modules
    import ast

    tree = ast.parse((ROOT / "src/zab/waker/__main__.py").read_text())
    (check,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "selfcheck"]
    imported = {a.name for n in ast.walk(check) if isinstance(n, ast.Import) for a in n.names}
    modules = {
        file.removeprefix("src/").removesuffix(".py").replace("/", ".")
        for file in closure
        if not file.endswith("__init__.py")
    }
    assert imported == modules


#: Where the workshop scripts import each other from: their own directory, and workshop/aws,
#: which admin_common.py puts on sys.path.
SCRIPT_DIRS = (ROOT / "workshop" / "admin", ROOT / "workshop" / "aws")
#: Paths a script names beside itself that the server never uses (#432), and why.
NOT_ON_THE_SERVER = {
    ("workshop/admin/admin_common.py", "workshop/aws"): "sys.path: its modules are followed",
    ("workshop/aws/grow_disk.py", "workshop/admin"): "sys.path: its modules are followed",
    ("workshop/aws/spare_idle_stop.py", "workshop/admin"): "sys.path: its modules are followed",
    ("workshop/aws/adopt_spare.py", "workshop/admin"): "sys.path: its modules are followed",
    ("workshop/admin/push_waker.py", "workshop/waker"): "the files host's mode, not --config-dir",
}


def _entry_points() -> set[str]:
    """The workshop/ paths the server runs: the bot's scripts (zab.admin.checks.SCRIPTS, which
    test_admin_bot keeps equal to the steps of actions.py and newworkshop.py), what the units and
    deploy.sh run or install from the release, and the runbooks' commands on the server."""
    from zab.admin.checks import SCRIPTS

    found = {f"workshop/{script}" for script in SCRIPTS}
    tail = r"(workshop/[\w./@-]*[\w@-])"
    for unit in (ROOT / "workshop" / "adminserver").glob("zab-*"):
        found |= set(re.findall("/opt/zab-admin/current/" + tail, unit.read_text()))
    found |= set(re.findall(r"\$\{?(?:REL|rel)\}?/" + tail, DEPLOY.read_text()))
    for doc in [*(ROOT / "docs").glob("*.md"), *(ROOT / "workshop").glob("*/README.md")]:
        found |= set(re.findall("/opt/zab-admin/current/" + tail, doc.read_text()))
    return found


#: `__file__` uses that name no file of the tree, and why each is safe on the server (#436).
UNRESOLVED = {
    ("workshop/admin/push_config.py", "CHECKOUT"): "`git -C` for --tag; the bot passes --wheel",
    ("workshop/admin/idle_stop.py", "os.path.abspath(__file__)"): "the script runs itself",
}


def _beside(script: pathlib.Path) -> tuple[set[pathlib.Path], set[tuple[str, str]]]:
    """The paths `script` names beside itself: `Path(__file__).resolve().parent / "x"` and the
    like (`.parents[N]`, a chain of `/`), directly or through a module-level name; and the
    UNRESOLVED entries it met. Any other use of `__file__` fails (#436): `os.path`, `.joinpath`,
    an f-string, a name in a function, a directory used but as `sys.path.insert(0, str(it))`."""
    import ast

    tree = ast.parse(script.read_text(encoding="utf-8"))
    rel = str(script.relative_to(ROOT)) if script.is_relative_to(ROOT) else script.name
    names: dict[str, pathlib.Path] = {}

    def value(node):
        match node:
            case ast.Call(func=ast.Name(id="Path") | ast.Attribute(attr="Path"), args=[arg]):
                return script if isinstance(arg, ast.Name) and arg.id == "__file__" else None
            case ast.Call(func=ast.Attribute(attr="resolve", value=inner), args=[]):
                return value(inner)
            case ast.Attribute(attr="parent", value=inner):
                return (v := value(inner)) and v.parent
            case ast.Subscript(value=ast.Attribute(attr="parents", value=inner), slice=index):
                k = index.value if isinstance(index, ast.Constant) else None
                return v.parents[k] if (v := value(inner)) and isinstance(k, int) else None
            case ast.BinOp(op=ast.Div(), left=left, right=ast.Constant(value=str() as part)):
                return (v := value(left)) and v / part
            case ast.Name(id=name):
                return names.get(name)
        return None

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign | ast.AnnAssign) and stmt.value is not None:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and value(stmt.value):
                names[targets[0].id] = value(stmt.value)
    nodes = list(ast.walk(tree))
    lefts = {id(n.left) for n in nodes if isinstance(n, ast.BinOp)}
    found = (value(n) for n in nodes if isinstance(n, ast.BinOp) and id(n) not in lefts)
    parent = {id(child): n for n in nodes for child in ast.iter_child_nodes(n)}

    def up(node):  # the next larger expression, past `.resolve` and `.parents` to their use
        p = parent.get(id(node))
        q = parent.get(id(p))
        if isinstance(p, ast.Attribute) and p.attr in ("resolve", "parents") and q is not None:
            return q if getattr(q, "func", None) is p or getattr(q, "value", None) is p else p
        return p

    met: set[tuple[str, str]] = set()

    def unresolved(what: str) -> None:
        assert (rel, what) in UNRESOLVED, f"{rel}: {what}: a use of __file__ the walk can't follow"
        met.add((rel, what))

    for node in nodes:
        if not (isinstance(node, ast.Name) and node.id == "__file__"):
            continue
        expr = parent[id(node)]
        if value(expr) is None:
            unresolved(ast.unparse(expr))
            continue
        while (bigger := up(expr)) is not None and value(bigger) is not None:
            expr = bigger
        user = parent.get(id(expr))
        if isinstance(expr, ast.BinOp):
            continue  # a file it names: returned
        if isinstance(user, ast.Assign | ast.AnnAssign) and user in tree.body:
            name = (user.targets[0] if isinstance(user, ast.Assign) else user.target).id
            for use in nodes:
                if isinstance(use, ast.Name) and use.id == name and isinstance(use.ctx, ast.Load):
                    bin_op = parent.get(id(use))
                    if not (isinstance(bin_op, ast.BinOp) and bin_op.left is use and value(bin_op)):
                        unresolved(name)
            continue
        caller = parent.get(id(user))
        if (
            value(expr) == script.parent
            and isinstance(user, ast.Call)
            and ast.unparse(user.func) == "str"
            and isinstance(caller, ast.Call)
            and ast.unparse(caller.func) == "sys.path.insert"
        ):
            continue  # the script's own directory, for its siblings
        unresolved(ast.unparse(expr))
    return {path for path in found if path}, met


def _server_workshop_files() -> tuple[dict[str, str], set[tuple[str, str]]]:
    """Each workshop/ path the server runs, reads or installs, with why: the entry points, and
    from each script its sibling imports and the files it names beside itself that exist. Also
    the exceptions of NOT_ON_THE_SERVER and UNRESOLVED the walk met."""
    import ast
    import collections

    listing = ["git", "-C", str(ROOT), "ls-files", "-z", "workshop"]
    tracked = call(listing, capture_output=True, check=True).stdout.decode().split("\0")
    todo = collections.deque((path, "an entry point") for path in sorted(_entry_points()))
    seen: dict[str, str] = {}
    skipped: set[tuple[str, str]] = set()
    while todo:
        rel, why = todo.popleft()  # the entry points first: each is named as one
        if rel in seen:
            continue
        seen[rel] = why
        path = ROOT / rel
        if path.suffix != ".py" or not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                modules = [node.module]
            else:
                continue
            for top in (module.split(".")[0] for module in modules):
                siblings = [d / f"{top}.py" for d in SCRIPT_DIRS if (d / f"{top}.py").is_file()]
                if siblings:
                    todo.append((str(siblings[0].relative_to(ROOT)), f"imported by {rel}"))
        targets, met = _beside(path)
        skipped |= met
        for target in targets:
            name = str(target.relative_to(ROOT))
            off = [(s, p) for s, p in NOT_ON_THE_SERVER if s == rel and covered(name, [p])]
            if off:
                skipped.update(off)
            elif any(covered(t, [name]) for t in tracked):  # not `out/`, which they write
                todo.append((name, f"named by {rel}"))
    return seen, skipped


@pytest.mark.parametrize(
    "line",
    [
        "X = os.path.join(os.path.dirname(__file__), 'x')",
        "X = pathlib.Path(__file__).resolve().parent.joinpath('x')",
        "X = pathlib.Path(__file__).with_name('x')",
        "X = f'{pathlib.Path(__file__).parent}/x'",
        "def f():\n    here = pathlib.Path(__file__).parent\n    return here / 'x'",
        "HERE = pathlib.Path(__file__).parent\nX = str(HERE) + '/x'",
    ],
)
def test_the_walk_fails_on_a_use_of_file_it_cannot_follow(tmp_path, line):
    """#436: the walk follows `Path(__file__)…/"x"` alone; any other use of `__file__` fails it,
    so that a file a script names another way can't be missed."""
    script = tmp_path / "s.py"
    script.write_text(f"import os, pathlib, sys\n{line}\n")
    with pytest.raises(AssertionError, match="a use of __file__ the walk can't follow"):
        _beside(script)
    good = "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))\n"
    good += "HERE = pathlib.Path(__file__).resolve().parent\nX = HERE / 'x'\n"
    good += "Y = pathlib.Path(__file__).resolve().parents[1] / 'y' / 'z'\n"
    script.write_text(f"import os, pathlib, sys\n{good}")
    assert _beside(script) == ({tmp_path / "x", tmp_path.parent / "y" / "z"}, set())


def test_the_release_paths_hold_what_the_server_runs_and_nothing_else():
    """#432: release-paths names the workshop/ files the server runs or installs, each of them,
    and no other. A script the bot gains, an import between scripts, a file a script copies to the
    VMs: each must join the list, and a path nothing uses must leave it."""
    used, skipped = _server_workshop_files()
    assert skipped == {*NOT_ON_THE_SERVER, *UNRESOLVED}, "an exception that no longer applies"
    for rel, why in [
        ("workshop/adminserver", "an entry point"),  # deploy.sh's install_files
        ("workshop/admin/archive_check.py", "an entry point"),  # its unit's ExecStart
        ("workshop/aws/import_key_pair.py", "an entry point"),  # deploy.sh's fleet_key
        ("workshop/admin/stage_server.py", "an entry point"),  # ADMIN §8.4: its run on the server
        ("workshop/aws/aws_common.py", "imported by"),
        ("workshop/template", "named by workshop/admin/provision_repos.py"),
        ("workshop/ami/files/agent-instructions.md", "named by workshop/admin/push_config.py"),
        ("workshop/ami/files/bin/lean-project", "named by workshop/admin/push_config.py"),
        ("workshop/admin/export.sh", "named by workshop/admin/collect_exports.py"),
        ("workshop/admin/final_sync_remote.py", "named by workshop/admin/final_sync.py"),
    ]:
        assert used.get(rel, "").startswith(why), (rel, used.get(rel))
    names = [p for p in RELEASE_PATHS if p.startswith("workshop/")]
    assert "workshop" not in RELEASE_PATHS
    for rel, why in sorted(used.items()):
        assert covered(rel, names), f"{rel} ({why}): add it to {LIST.name}"
    for name in names:
        assert any(covered(rel, [name]) for rel in used), f"{name}: listed, but used by nothing"


def test_the_privileged_paths():
    """D31, D32: the privileged set covers workshop/adminserver (deploy.sh, everything root
    installs, render_policy.py, both lists), the root helper's code, and what decides the code
    that runs on the laptop (`--wheel`'s build) and in the venv; each is a release path. Every
    module the helper runs as root (HELPER_IMPORTS, which test_admin_imports pins by running the
    unit's command, #438) is privileged, but the two packages' __init__.py."""
    from admin.test_admin_imports import HELPER_IMPORTS

    privileged = listed(PRIVILEGED)
    assert privileged == [
        "workshop/adminserver",
        "src/zab/admin/confighelper.py",
        "src/zab/admin/config.py",
        "src/zab/admin/checks.py",
        "pyproject.toml",
        "build-constraints.txt",
        "uv.lock",
    ]
    for path in privileged:
        assert covered(path, RELEASE_PATHS), path
    unit = (ROOT / "workshop/adminserver/zab-admin-config.service").read_text()
    (start,) = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert start.startswith('ExecStart=/usr/bin/python3 -I -B -S -c "')
    assert "from zab.admin.confighelper import main" in start
    for module in sorted(HELPER_IMPORTS - {"zab", "zab.admin"}):
        path = "src/" + module.replace(".", "/") + ".py"
        assert covered(path, privileged), f"{path}: root runs it in the helper (D32)"
    import ast

    for package in ("src/zab/__init__.py", "src/zab/admin/__init__.py"):  # constants alone
        body = ast.parse((ROOT / package).read_text()).body
        for node in body:
            assert isinstance(node, ast.Expr | ast.Assign), (package, ast.unparse(node))
            assert isinstance(node.value, ast.Constant), (package, ast.unparse(node))


def _admin_group_closure() -> set[str]:
    """The distributions `uv export --only-group admin` installs: the group's closure in
    uv.lock, names normalized."""
    import tomllib

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {p["name"]: p for p in lock["package"]}
    project = packages["zulip-agent-bridge"]
    todo = [d["name"] for d in project["dev-dependencies"]["admin"]]
    seen: set[str] = set()
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo += [d["name"] for d in packages[name].get("dependencies", [])]
    return {name.replace("_", "-").lower() for name in seen}


#: An import name that differs from its distribution's.
DISTRIBUTION = {"dateutil": "python-dateutil", "yaml": "pyyaml"}


def test_the_admin_group_provides_every_import_the_server_runs():
    """Codex round 3 on #338: the venv holds only the admin group, so every third-party module
    that a release script or zab.admin imports must be in its closure (httpx, for one)."""
    import ast

    files = [*(ROOT / "src" / "zab" / "admin").rglob("*.py")]
    files += [ROOT / f for f in _closure_files([ROOT / "src/zab/waker/__main__.py"])]  # #348
    for sub in ("admin", "aws", "adminserver"):
        files += (ROOT / "workshop" / sub).glob("*.py")
    siblings = {
        p.stem
        for sub in ("admin", "aws", "adminserver")
        for p in (ROOT / "workshop" / sub).glob("*.py")
    }
    wanted: dict[str, str] = {}
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [node.module]
            else:
                continue
            for top in (name.split(".")[0] for name in names):
                if (
                    top in sys.stdlib_module_names
                    or top in siblings
                    or top in ("__future__", "zab")
                ):
                    continue
                wanted.setdefault(DISTRIBUTION.get(top, top), str(path.relative_to(ROOT)))
    assert "boto3" in wanted and "httpx" in wanted
    missing = {name: where for name, where in wanted.items() if name not in _admin_group_closure()}
    assert not missing, f"add to pyproject's admin group, then uv lock: {missing}"


def _lock_packages(text: str) -> dict[tuple[str, str], dict]:
    """Every entry of the lock, by name and version: a lock may hold several versions of one
    package (Codex on d3e70ce), and each is checked."""
    import tomllib

    found: dict[tuple[str, str], dict] = {}
    for package in tomllib.loads(text).get("package", []):
        key = (package["name"], package.get("version", "?"))
        assert key not in found, f"{key}: twice in the lock"
        found[key] = package
    return found


def _lock_files(package: dict) -> list[tuple[str, str]]:
    found = [package.get("sdist"), *package.get("wheels", [])]
    return sorted((f.get("url", ""), f.get("hash", "")) for f in found if f)


def test_the_lock_takes_every_package_from_pypi():
    """#436, uv.lock's mechanical checks, so that a reader of its review checks packages, not
    hashes: every package comes from PyPI, but the project itself (`editable = "."`), and every
    file from files.pythonhosted.org, with a SHA-256."""
    packages = _lock_packages((ROOT / "uv.lock").read_text())
    for (name, _), package in packages.items():
        source = package["source"]
        if name == "zulip-agent-bridge":
            assert source == {"editable": "."}
            continue
        assert source == {"registry": "https://pypi.org/simple"}, name
        files = _lock_files(package)
        assert files, name
        for url, digest in files:
            assert url.startswith("https://files.pythonhosted.org/"), (name, url)
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), (name, url)


def _lock_floor(text: str) -> tuple[int, int]:
    """The lowest Python of the lock's `requires-python`, which zab writes as `>=3.N`."""
    import tomllib

    bound = re.fullmatch(r">=3\.(\d+)", tomllib.loads(text)["requires-python"])
    assert bound, "the lock's requires-python is not >=3.N"
    return (3, int(bound[1]))


def _only_below(url: str, floor: tuple[int, int]) -> bool:
    """A wheel that no Python from `floor` on can install, which `uv lock` drops when the bound
    rises to `floor` (#774: `cp311` and `pp311` wheels at `>=3.12`): each of its Python tags is a
    CPython or PyPy below `floor`, and its ABI isn't `abi3`, which later CPythons take too."""
    name = url.rsplit("/", 1)[-1]
    if not name.endswith(".whl"):
        return False
    pythons, abis = (part.split(".") for part in name[: -len(".whl")].split("-")[-3:-1])
    found = [re.fullmatch(r"(?:cp|pp)3(\d+)", tag) for tag in pythons]
    return "abi3" not in abis and all(m and (3, int(m[1])) < floor for m in found)


def _other_files(then: str, text: str) -> list[str]:
    """Each package of the lock `then` whose files differ in the lock `text` at the same
    version, with what differs. The one change allowed: a file gone that no Python of
    `text`'s `requires-python` can install, as when the bound rises (#774)."""
    old, new, floor = _lock_packages(then), _lock_packages(text), _lock_floor(text)
    found = []
    for key in sorted(old.keys() & new.keys()):  # the same name and version in both
        was, now = set(_lock_files(old[key])), set(_lock_files(new[key]))
        gone = sorted(url for url, _ in was - now if not _only_below(url, floor))
        if not now <= was or gone:
            found.append(f"{key}: {sorted(now - was) or gone}")
    return found


def test_no_lock_file_changed_without_its_version_since_the_last_release():
    """#436: a package whose files or hashes changed keeps no version it had in the last release
    tag this checkout holds (CI's shallow checkout holds none: then the review's flag stands),
    but for the wheels a higher `requires-python` drops (#774)."""
    found = call(
        ["git", "-C", str(ROOT), "describe", "--tags", "--abbrev=0", "--match", "v*", "HEAD"],
        capture_output=True,
        text=True,
    )
    if found.returncode != 0:
        pytest.skip("no release tag in this checkout")
    tag = found.stdout.strip()
    then = call(["git", "-C", str(ROOT), "show", f"{tag}:uv.lock"], capture_output=True, text=True)
    changed = _other_files(then.stdout, (ROOT / "uv.lock").read_text())
    assert not changed, f"other files since {tag}: {changed}"


def test_a_lock_file_changed_at_the_same_version_is_found():
    """The rule above on made-up locks: a hash changed, a file added, and a wheel gone that
    3.12 installs are each found; the cp311 wheel that `>=3.12` drops is not."""

    def lock(bound: str, *wheels: tuple[str, str]) -> str:
        at = "https://files.pythonhosted.org/packages/ab/cd"
        rows = "".join(f'  {{ url = "{at}/{n}", hash = "sha256:{h * 64}" }},\n' for n, h in wheels)
        return (
            f'version = 1\nrequires-python = "{bound}"\n\n[[package]]\nname = "x"\n'
            f'version = "1.0"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
            f"wheels = [\n{rows}]\n"
        )

    old, new = ("x-1.0-cp311-cp311-win32.whl", "a"), ("x-1.0-cp312-cp312-win32.whl", "b")
    then = lock(">=3.11", old, new)
    assert _other_files(then, lock(">=3.12", new)) == []
    assert _other_files(then, lock(">=3.11", new)) != []  # the bound didn't rise
    assert _other_files(then, lock(">=3.12", (new[0], "c"))) != []  # another hash
    assert _other_files(then, lock(">=3.12", old)) != []  # 3.12's wheel gone
    assert _other_files(then, lock(">=3.12", new, ("x-1.0-py3-none-any.whl", "d"))) != []


@pytest.mark.parametrize(
    "name,below",
    [
        ("rpds_py-2026.6.3-cp311-cp311-win32.whl", True),
        ("websockets-17.1-pp311-pypy311_pp73-macosx_10_15_x86_64.whl", True),
        ("x-1.0-1-cp310.cp311-cp311-manylinux_2_17_x86_64.whl", True),  # a build tag, two
        ("websockets-17.1-cp312-cp312-macosx_11_0_arm64.whl", False),
        ("x-1.0-cp311-abi3-manylinux_2_17_x86_64.whl", False),  # 3.12 installs it too
        ("x-1.0-cp311.cp312-cp311.cp312-win32.whl", False),
        ("x-1.0-py3-none-any.whl", False),
        ("x-1.0.tar.gz", False),
    ],
)
def test_only_a_wheel_below_the_bound_may_go_at_the_same_version(name, below):
    url = f"https://files.pythonhosted.org/packages/ab/cd/{name}"
    assert _only_below(url, (3, 12)) is below
    assert _lock_floor('version = 1\nrequires-python = ">=3.12"\n') == (3, 12)


def test_the_release_patterns_match_the_loader():
    from zab.admin.config import RELEASE_PATTERNS

    text = DEPLOY.read_text()
    for kind, var in (("final", "FINAL_RE"), ("rc", "RC_RE")):
        shell = RELEASE_PATTERNS[kind].replace(r"\d", "[0-9]").replace("(?:", "(")
        assert f"{var}='{shell}'" in text


def test_the_build_constraints_pin_hatchling_with_hashes():
    text = (ROOT / "build-constraints.txt").read_text()
    pins = re.findall(
        r"^([A-Za-z0-9_.-]+)==(\S+) \\\n((?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", text, re.M
    )
    names = {name for name, _, _ in pins}
    assert "hatchling" in names
    requirement_lines = [
        line for line in text.splitlines() if line and not line.startswith((" ", "#"))
    ]
    assert len(requirement_lines) == len(pins)  # every requirement is pinned and hashed


def test_the_pinned_backend_meets_the_build_requirements():
    """`deploy.sh --wheel` builds with build-constraints.txt's pins, so each of pyproject.toml's
    `build-system.requires` must hold for its pin: a floor raised past the pin fails here, in CI,
    not on the deployer's machine (review of #594)."""
    import tomllib

    from packaging.requirements import Requirement  # pytest's own dependency
    from packaging.utils import canonicalize_name

    text = (ROOT / "build-constraints.txt").read_text()
    pins = {canonicalize_name(n): v for n, v in re.findall(r"^([\w.-]+)==(\S+)", text, re.M)}
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text())["build-system"]["requires"]
    assert requires
    for requirement in map(Requirement, requires):
        pin = pins.get(canonicalize_name(requirement.name))
        assert pin is not None and requirement.specifier.contains(pin), (str(requirement), pin)
