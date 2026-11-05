"""The rig, made or refreshed, and the commit under test deployed (docs/ADMIN.md §7; #356).

`Setup.run` goes through every step in order, each idempotent, so a second run reuses what is
there and a run that died part-way is completed by the next:

1. the rig's keys (`release.ensure_keys`) and Zulip: the admin bots, their channels, the
   zuliprcs (`zulip_rig`); the provisioner's key and the GitHub token, staged as secrets;
2. AWS: the groups, the test AMI's scope tag, admin.toml (`admin_toml`), the role's policy as
   the commit under test's `render_policy.py` renders it, the role and instance profile, the
   server (launched once, started, its host key pinned), SSH from it to the fake participants;
   any fake participant an aborted run left is terminated;
3. the server: a new signed rc of the commit under test (`release`), deployed with that
   commit's `deploy.sh`: `--bootstrap` the first time (after deleting an earlier server's key
   pairs), an upgrade after that, with the server's key pairs tagged with the rig's scope
   (#422) and the rig's admin.toml, allowed_signers and backup_recipients installed first if
   the server's differ; then the test workshops' rosters
   and `out/` emptied (each run starts from no roster: A2), and the release's fleet wheel placed
   with `deploy.sh --wheel` (A6's rollout).

In a dry run (`Gate(dry_run=True)`) every lookup is made and every change only printed; the
server is not started, so its steps are printed as the commands deploy.sh would run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from e2e.admin.harness import admin_toml
from e2e.admin.harness.aws_rig import AwsRig
from e2e.admin.harness.gate import Gate
from e2e.admin.harness.release import Releases, ensure_keys, next_version, write_ssh
from e2e.admin.harness.rig import E2E, E2E2, SERVER_GROUP, SERVER_TAGS, AdminRig
from e2e.admin.harness.server import Server
from e2e.admin.harness.zulip_rig import ZulipIds, ZulipRig, private_dir, stage_secrets
from e2e.harness.secret_files import label_of, read_secret

ETC_FILES = ("admin.toml", "allowed_signers", "backup_recipients")
#: The secrets a later setup installs when they change: Q9's, placeholders until Drew's.
SYNCED = ("github-token", "provisioner.zuliprc")


@dataclass
class Rigged:
    """What the scenarios need from the setup (`run.py` passes it as JSON): IDs and names,
    never a secret."""

    commit: str  # the commit under test
    release: str  # the test release of it the server runs (`v0.2.4rc1`)
    release_commit: str  # that release's version commit
    server: str = ""  # instance ID
    ip: str = ""
    ami: str = ""
    groups: dict[str, str] = field(default_factory=dict)  # workshop → its participants' group
    zulip: dict[str, Any] = field(default_factory=dict)  # `ZulipIds`, as a dict
    github_token: bool = False  # Drew's (Q9), or a placeholder
    provisioner: bool = False  # zab-tester's key, once Drew agrees (Q9), or a placeholder
    wheel: str = ""
    deploys: list[str] = field(default_factory=list)  # one line per deploy.sh run
    started: float = 0.0  # when the setup began (epoch): what this run posted is newer

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Rigged:
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def ids(self) -> ZulipIds:
        return ZulipIds(**self.zulip)


class Setup:
    def __init__(
        self,
        rig: AdminRig,
        gate: Gate,
        aws: AwsRig,
        releases: Releases,
        *,
        zulip_clients: Any,  # () -> (zab-tester's client, zab-tester2's)
        server: Any = Server,  # (ip, rig) -> Server
        out: Path,
    ) -> None:
        self.rig, self.gate, self.aws, self.releases = rig, gate, aws, releases
        self.zulip_clients, self.make_server, self.out = zulip_clients, server, out

    async def _zulip(self) -> tuple[ZulipIds, frozenset[str]]:
        tester, tester2 = self.zulip_clients()
        try:
            zrig = ZulipRig(self.gate, tester, tester2, self.rig)
            return await zrig.ensure(), zrig.tester_emails
        finally:
            await tester.close()
            await tester2.close()

    def run(self, commit: str) -> Rigged:
        rig, gate, aws = self.rig, self.gate, self.aws
        began = time.time()
        if not gate.dry_run:
            for directory in (rig.dir, rig.keys, rig.secrets):
                private_dir(directory)
        gate.note("keys and Zulip")
        ensure_keys(gate, rig, self.releases.run)
        ids, tester_emails = asyncio.run(self._zulip())
        real_token, real_provisioner = stage_secrets(gate, rig, ids.site, tester_emails)
        gate.note("AWS")
        vpc = aws.vpc()
        server_tags = {k: v for k, v in SERVER_TAGS.items() if k != "Name"}
        server_group = aws.ensure_group(SERVER_GROUP, server_tags, vpc, aws.ssh_sources(vpc))
        groups = {ws.name: aws.ensure_group(ws.group, ws.tags, vpc) for ws in (E2E, E2E2)}
        aws.fleet_group(vpc)  # the region set up, for A16's !new-workshop (#418)
        image = aws.test_image()
        toml = admin_toml.render(ids, region=rig.region, ami=image["ImageId"], groups=groups)
        self._write_etc(toml)
        aws.ensure_role(self._policies(commit, toml))
        inst = aws.server()
        fresh = inst is None
        if inst is None:
            if not gate.dry_run:  # an earlier server's: never this one's
                rig.host_key.unlink(missing_ok=True)
            inst = aws.launch_server(server_group)
        ip = aws.start(inst)
        rigged = Rigged(commit, "", "", inst["InstanceId"], ip, image["ImageId"], groups)
        rigged.zulip, rigged.github_token = asdict(ids), real_token
        rigged.provisioner = real_provisioner
        rigged.started = began
        aws.pin_host_key(inst, ip)
        if not gate.dry_run:
            write_ssh(rig, ip)
        aws.allow_server(vpc, ip if not gate.dry_run else "<server address>")
        aws.terminate_participants()  # what an aborted run left
        gate.note("the server")
        self._deploy(rigged, fresh)
        return rigged

    def _write_etc(self, toml: str) -> None:
        rig = self.rig
        if self.gate.dry_run:
            self.gate.note(f"would write {rig.etc}/admin.toml:\n{toml}")
            return
        (rig.etc / "break-glass").mkdir(mode=0o700, parents=True, exist_ok=True)
        (rig.etc / "admin.toml").write_text(toml, encoding="utf-8")
        signing = (rig.keys / "signing.pub").read_text(encoding="utf-8")
        (rig.etc / "allowed_signers").write_text(admin_toml.allowed_signers(signing), "utf-8")
        backup = (rig.keys / "backup.pub").read_text(encoding="utf-8")
        (rig.etc / "backup_recipients").write_text(backup, encoding="utf-8")
        deployer = rig.ssh_key.with_name(rig.ssh_key.name + ".pub").read_text(encoding="utf-8")
        (rig.etc / "break-glass" / Path(admin_toml.BREAK_GLASS).name).write_text(deployer, "utf-8")

    def _policies(self, commit: str, toml: str) -> list[dict[str, Any]]:
        """The role's policy for the test admin.toml, from the commit under test's renderer
        (§5: `render_policy.py`), split as IAM needs it, and kept in the run's directory. It
        reads no bot ID, so a dry run's placeholders render the same policy."""
        script = self.releases.extract(commit, "workshop/adminserver/render_policy.py",
                                       self.out / "render_policy.py")  # fmt: skip
        config = self.out / "admin.toml"
        config.write_text(toml, encoding="utf-8")
        argv = [sys.executable, "-I", str(script), "--config", str(config)]
        done = self.releases.run([*argv, "--out-dir", str(self.out / "policy")])
        if not done.ok:
            raise RuntimeError(f"render_policy.py: {done.stderr.strip()[:300]}")
        docs = sorted((self.out / "policy").glob("zab-admin-*.json"), key=lambda p: p.stem)
        self.gate.note(f"the role's policy: {', '.join(str(p) for p in docs)}")
        return [json.loads(p.read_text(encoding="utf-8")) for p in docs]

    def _deploy(self, rigged: Rigged, fresh: bool) -> None:
        gate, rel = self.gate, self.releases
        commit = rigged.commit
        script = rel.extract(commit, "workshop/adminserver/deploy.sh", self.out / "deploy.sh")
        if gate.dry_run:
            if fresh:
                self.aws.drop_key_pairs()
            else:
                self.aws.scope_key_pairs()
            how = "--bootstrap" if fresh else "(upgrade, or --bootstrap if none runs)"
            gate.write(f"local deploy.sh {how} <next rc of {commit[:12]}>, then --wheel")
            gate.write("ssh: stop the bot, empty the test workshops' rosters and out/, start it")
            return
        server = self.make_server(rigged.ip, self.rig)
        for _ in range(60):
            if server.reachable():
                break
            self.aws.sleep(10)
        else:
            raise TimeoutError(f"no SSH to the server at {rigged.ip} after 10 minutes")
        running = server.running()
        version = next_version(rel.base_version(commit), running)
        rel.ensure_repo()
        if running and not rel.has_tag(running):
            rel.release(commit, running)  # the diff's base: deploy.sh verifies it
        rigged.release, rigged.release_commit = version, rel.release(commit, version)
        log = self.out / "deploy.log"
        if running is None:
            self.aws.drop_key_pairs()
            done = rel.deploy(script, version, "--bootstrap", out=self.out, log=log)
        else:
            self.aws.scope_key_pairs()  # imported by an earlier bootstrap, maybe unscoped
            self._sync_etc(server)
            done = rel.deploy(script, version, out=self.out, log=log)
        self._sync_secrets(server)
        rigged.deploys.append(f"{version}: {'ok' if done.ok else done.why()}")
        if not done.ok:
            raise RuntimeError(f"deploy.sh {version}: {done.why()} (log: {log})")
        server.bot("stop")
        server.reset_workshops()
        server.bot("start")
        wheel = rel.deploy(script, version, "--wheel", out=self.out, log=log)
        rigged.deploys.append(f"{version} --wheel: {'ok' if wheel.ok else wheel.why()}")
        if not wheel.ok:
            raise RuntimeError(f"deploy.sh --wheel {version}: {wheel.why()} (log: {log})")
        rigged.wheel = f"zulip_agent_bridge-{version[1:]}-py3-none-any.whl"

    def _sync_etc(self, server: Server) -> None:
        """The rig's trust files on the server, where they differ (a new AMI, a new key): the
        bootstrap installs them, and deploy.sh only shows a difference after that."""
        for name in ETC_FILES:
            mine = (self.rig.etc / name).read_text(encoding="utf-8")
            if server.read(f"/etc/zab-admin/{name}") != mine and self.gate.write(
                f"ssh: install /etc/zab-admin/{name} from {self.rig.etc}"
            ):
                server.install_etc(name, mine)

    def _sync_secrets(self, server: Server) -> None:
        """The GitHub token and the provisioner's key, once Drew provides them (Q9): the
        bootstrap installed the placeholders, and only the bootstrap installs secrets.
        Compared by SHA-256 only; the bot restarts with the reset that follows. Each is read
        through `read_secret` (#519), and its text is held by no frame of this one."""
        for name in SYNCED:
            path = self.rig.secrets / name
            label = label_of(path, self.rig.dir)
            if server.secret_sha256(name) != read_secret(label, _sha256, path):
                if self.gate.write(f"ssh: install /etc/zab-admin/secrets/{name}"):
                    server.install_secret(name, read_secret(label, _utf8, path))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utf8(path: Path) -> str:
    return path.read_bytes().decode("utf-8")
