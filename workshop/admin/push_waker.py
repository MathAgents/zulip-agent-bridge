#!/usr/bin/env python3
"""Opt in selected roster VMs for the waker; dry-run unless --apply (issue #207; PR 21).

On the files host as root (direct mode), or on the admin server with --config-dir, as zab-admin.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import fcntl
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import tomllib
from collections import Counter
from pathlib import Path

from admin_common import load_roster, select_rows, slug_of

ROOT = Path("/etc/zab-waker")
ASSETS = Path(__file__).resolve().parents[1] / "waker"
#: The VMs' `workshop` tag (`zab.waker.service.WORKSHOP`; this script may run before zab is
#: installed). `--workshop zab-test` for the test waker (issue #263).
WORKSHOP = "nt-agents-2026"
_WORKSHOP_RE = re.compile(r"[A-Za-z0-9._:/=+@-]{1,256}")
#: VMs in another account (#207): the service assumes a role there through this drop-in's
#: AWS_CONFIG_FILE and AWS_PROFILE (botocore's AssumeRoleProvider, `credential_source`).
DROPIN = Path("/etc/systemd/system/zab-waker.service.d/assume-role.conf")
PROFILE = "zab-waker"
_NAME = r"[A-Za-z0-9_+=,.@-]"
_ROLE_ARN_RE = re.compile(rf"arn:aws[a-z-]*:iam::\d{{12}}:role/(?:{_NAME}+/)*{_NAME}{{1,64}}")


def aws_config(role_arn: str) -> str:
    """The profile with which the service starts VMs in another account: botocore assumes
    `role_arn` with the files host's instance-profile credentials, and renews it."""
    if not _ROLE_ARN_RE.fullmatch(role_arn):
        raise ValueError(f"malformed role ARN {role_arn!r}")
    return (
        "# push_waker.py --assume-role (#207): start the VMs as this role in their account\n"
        f"[profile {PROFILE}]\n"
        f"role_arn = {role_arn}\n"
        "credential_source = Ec2InstanceMetadata\n"
        "role_session_name = zab-waker\n"
    )


def dropin() -> str:
    return (
        f"[Service]\nEnvironment=AWS_CONFIG_FILE={ROOT / 'aws-config'}\n"
        f"Environment=AWS_PROFILE={PROFILE}\n"
    )


def role_config(role_arn: str | None, remove: bool) -> str | None:
    """The aws-config to install: a new role, none (`--no-assume-role`), or the installed one,
    which a rerun without either flag keeps."""
    if remove:
        return None
    if role_arn is not None:
        return aws_config(role_arn)
    path = ROOT / "aws-config"
    return path.read_text() if path.exists() else None


def identity(boto3, region: str) -> str:
    """The caller's ARN (sts:GetCallerIdentity needs no permission and returns no secret)."""
    try:
        return boto3.client("sts", region_name=region).get_caller_identity()["Arn"]
    except Exception as exc:  # botocore: no credentials, or the AssumeRole refused
        raise ValueError(f"no usable AWS identity: {exc}") from exc


def same_role(arn: str, role_arn: str) -> bool:
    session = re.fullmatch(r"arn:[a-z-]+:sts::(\d{12}):assumed-role/([^/]+)/.+", arn)
    role = re.fullmatch(r"arn:[a-z-]+:iam::(\d{12}):role/(?:.*/)?([^/]+)", role_arn)
    return bool(session and role and session.groups() == role.groups())


def aws_error(exc: Exception) -> str:
    """botocore's error: a ClientError's code, or the class (NoCredentialsError, …), whatever an
    odd exception's `response` holds (#348; `zab.waker.service.error_code`, which this script,
    run before zab is installed, can't import)."""
    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, dict) else None
    code = error.get("Code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code else type(exc).__name__


def use_profile(role: str | None, scratch: str) -> None:
    """Make this process's boto3 see what the service sees: no AWS_* setting but systemd's,
    no ~/.aws (ProtectHome=true), and `role`'s profile if any. Otherwise the operator's
    exported keys, profile or credential files would win (Codex rounds 1 and 2 on #298)."""
    for name in [n for n in os.environ if n.startswith("AWS_")]:
        del os.environ[name]
    os.environ.update(AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull)
    if role is not None:
        Path(scratch, "aws-config").write_text(role)
        os.environ.update(AWS_CONFIG_FILE=f"{scratch}/aws-config", AWS_PROFILE=PROFILE)


def role_arn_of(text: str, profile: str | None = PROFILE) -> str:
    """The profile's role_arn, read raw as botocore reads it. The config `aws_config` writes
    holds no secret, but an AWS config may hold keys, and `--check` reads the one the unit names:
    a parse error is said by its type, never its text, which quotes the line (#523)."""
    profiles = configparser.ConfigParser(interpolation=None)
    try:
        profiles.read_string(text)
    except configparser.Error as exc:
        why = type(exc).__name__
    else:
        return profiles.get(f"profile {profile}", "role_arn", fallback="(no role_arn)")
    raise ValueError(f"the AWS config doesn't parse: {why}") from None


def verify(who: str, role: str | None) -> None:
    """Apply nothing unless the identity is the role asked for."""
    if role is not None and not same_role(who, arn := role_arn_of(role)):
        raise ValueError(f"the AWS identity {who} is not the role {arn}")


def install_role(role: str | None) -> None:
    """The profile and the drop-in, or neither; the caller reloads systemd and restarts."""
    if any(p.is_symlink() for p in (ROOT, ROOT / "aws-config.new", DROPIN)):
        raise ValueError("refusing symlink destination")
    if role is None:  # the instance's own role starts the VMs
        (ROOT / "aws-config").unlink(missing_ok=True)
        DROPIN.unlink(missing_ok=True)
    else:  # an ARN and settings, no secret; root-only like the rest of ROOT
        new = ROOT / "aws-config.new"
        new.write_text(role)
        new.chmod(0o600)
        os.chown(new, 0, 0)
        new.replace(ROOT / "aws-config")
        DROPIN.parent.mkdir(exist_ok=True)
        DROPIN.write_text(dropin())


def role_only(role_arn: str | None, apply: bool) -> int:
    """`--assume-role ARN` or `--no-assume-role` alone, on a deployed waker: only the profile
    and the drop-in change (no roster, keys or wheel needed), and a running service restarts;
    a stopped one stays stopped, as after the README's undo (review on #298)."""
    role = role_config(role_arn, role_arn is None)
    mode = "APPLY" if apply else "DRY RUN"
    if role is None:  # removing needs no AWS, so it works after the host's profile is gone
        print(f"{mode}: remove {ROOT / 'aws-config'} and {DROPIN}")
    else:
        path = ROOT / "config.toml"
        if not path.exists():
            raise ValueError(f"no waker deployed ({path}): run a full deployment")
        config = tomllib.loads(path.read_text())
        default = config.get("region", "us-west-2")
        region = next((b["region"] for b in config.get("bots", []) if "region" in b), default)
        with tempfile.TemporaryDirectory(prefix="zab-waker-") as scratch:
            use_profile(role, scratch)
            import boto3  # only this operator command needs the SDK

            who = identity(boto3, region)  # proves the AssumeRole before anything changes
        verify(who, role)
        print(f"{mode}: the AWS profile only; AWS identity: {who}\n{role}")
    if apply:
        if os.geteuid() != 0:
            raise ValueError("--apply must run as root on the files host")
        if ROOT.exists() and ROOT.stat().st_uid != 0:
            raise ValueError("waker directories must be root-owned and not symlinks")
        os.umask(0o077)
        install_role(role)
        subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True)
        subprocess.run(["/usr/bin/systemctl", "try-restart", "zab-waker.service"], check=True)
    return 0


#: The unit's AWS_* settings whose values `--check` prints; others only by name (review on #298).
SHOWN = ("AWS_CONFIG_FILE", "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION")


def check() -> int:
    """`--check`, read-only: the identity that systemd's environment for zab-waker.service
    yields, the workshop VMs it sees, and whether it may start each opted-in one."""
    show = ["/usr/bin/systemctl", "show", "--property=Environment", "--value", "zab-waker.service"]
    shown = subprocess.run(show, capture_output=True, text=True, check=True).stdout.split()  # noqa: S603
    env = dict(v.split("=", 1) for v in shown if v.startswith("AWS_") and "=" in v)
    use_profile(None, "")
    os.environ.update(env)
    listed = [f"{k}={v}" if k in SHOWN else k for k, v in env.items()]  # never a key's value
    print("service environment:", " ".join(listed) or "none")
    role = None
    if "AWS_CONFIG_FILE" in env:
        profile = Path(env["AWS_CONFIG_FILE"])
        text = profile.read_text() if profile.is_file() else ""
        role = role_arn_of(text, env.get("AWS_PROFILE"))
    print(f"role to assume: {role or 'none (the instance profile starts the VMs)'}")
    path = ROOT / "config.toml"
    config = tomllib.loads(path.read_text()) if path.exists() else {}
    workshop, default = config.get("workshop", WORKSHOP), config.get("region", "us-west-2")
    bots = {b["instance_id"]: b.get("region", default) for b in config.get("bots", [])}
    regions = sorted(set(bots.values())) or [default]
    import boto3  # only the waker's virtualenv has the SDK

    try:
        arn = identity(boto3, regions[0])
        ok = role is None or same_role(arn, role)
        print(f"AWS identity: {arn}" + ("" if ok else f" (MISMATCH: not {role})"))
    except ValueError as exc:  # no credentials, or the AssumeRole refused
        ok = False
        print(f"AWS identity: none: {exc}")
    for region in regions:  # botocore's errors are reported, never raised (review on #298)
        try:
            found = boto3.client("ec2", region_name=region).describe_instances(
                Filters=[{"Name": "tag:workshop", "Values": [workshop]}]
            )
        except Exception as exc:  # ClientError, NoCredentialsError, BotoCoreError
            ok = False
            print(f"{region}: cannot list VMs: {aws_error(exc)}")
            continue
        states = Counter(i["State"]["Name"] for r in found["Reservations"] for i in r["Instances"])
        print(f"{region}: sees {dict(states)} VMs tagged workshop={workshop}")
    for instance, region in bots.items():  # DryRunOperation: IAM allows it, nothing starts
        try:
            ec2 = boto3.client("ec2", region_name=region)
            ec2.start_instances(InstanceIds=[instance], DryRun=True)
            code = "no DryRunOperation"
        except Exception as exc:  # ClientError, NoCredentialsError, BotoCoreError
            code = aws_error(exc)
        allowed = code == "DryRunOperation"
        ok = ok and allowed
        print(f"start {instance} ({region}):", "allowed (dry run)" if allowed else f"NO: {code}")
    if not ok and role is None:
        print("hint: no role is configured, so the instance profile is used; for VMs in another")
        print("account, run push_waker.py --assume-role ARN --apply (README, step 3)")
    elif not ok:
        print("hint: check the role's policy (iam-policy.json) and its trust (README, step 2)")
    print("check:", "OK" if ok else "FAILED")
    return 0 if ok else 2


def render(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        lines.append("[[bots]]")
        lines.extend(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in row.items())
    return "\n".join(lines) + "\n"


def find_vm(client, scope: list[dict], row: dict, slug: str) -> dict:
    """The one VM that `scope`'s tag filters select at the roster row's public IP."""
    ip = str(ipaddress.ip_address(row.get("ip", "")))
    response = client.describe_instances(
        Filters=[
            *scope,
            {"Name": "network-interface.association.public-ip", "Values": [ip]},
            {
                "Name": "instance-state-name",
                "Values": ["running", "pending", "stopped", "stopping"],
            },
        ]
    )
    instances = [i for r in response["Reservations"] for i in r["Instances"]]
    if len(instances) != 1:
        raise ValueError(
            f"{slug}: expected exactly one workshop VM at the roster IP, found {len(instances)}"
        )
    if not re.fullmatch(r"i-(?:[a-f0-9]{8}|[a-f0-9]{17})", instances[0]["InstanceId"]):
        raise ValueError("invalid instance ID")
    return instances[0]


def allowlist(row: dict, slug: str) -> tuple[list[int], list[str]]:
    uid = (row.get("zulip_user_id") or "").strip()
    email = (row.get("email") or "").strip()
    if not uid and not email:
        raise ValueError(f"{slug}: participant allowlist required")
    if uid and (not uid.isdecimal() or int(uid) <= 0):
        raise ValueError(f"{slug}: invalid Zulip user ID")
    return ([int(uid)] if uid else []), ([email] if email else [])


def key_file(keys: Path, slug: str, kind: str) -> Path:
    source = keys / slug / f"{kind}.zuliprc"
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"{slug}: missing regular {kind}.zuliprc")
    return source


def plan(
    rows: list[dict],
    keys: Path,
    organizers: int,
    clients: dict,
    existing: dict,
    workshop: str = WORKSHOP,
) -> tuple[str, list[tuple[Path, Path]]]:
    if not _WORKSHOP_RE.fullmatch(workshop):
        raise ValueError(f"invalid workshop tag value {workshop!r}")
    if existing.get("bots") and existing.get("workshop", WORKSHOP) != workshop:
        # one waker serves one workshop: the retained opt-ins are another workshop's VMs
        raise ValueError(
            f"the installed config is for workshop {existing.get('workshop', WORKSHOP)!r}, "
            f"not {workshop!r}"
        )
    bots = {
        b["name"]: {"region": existing.get("region", "us-west-2"), **b}
        for b in existing.get("bots", [])
    }
    copies = []
    for row in rows:
        slug, region = slug_of(row), row.get("region") or "us-west-2"
        scope = [{"Name": "tag:workshop", "Values": [workshop]}]
        instance = find_vm(clients[region], scope, row, slug)["InstanceId"]
        uids, emails = allowlist(row, slug)
        for kind in ("codex", "claude"):
            source = key_file(keys, slug, kind)
            dest = ROOT / slug / source.name
            copies.append((source, dest))
            name = f"{kind}-{slug}"
            bots[name] = dict(
                name=name,
                instance_id=instance,
                region=region,
                zuliprc=str(dest),
                allow_user_ids=uids,
                allow_emails=emails,
            )
    head = (
        f"organizers_channel = {organizers}\n"
        f"workshop = {json.dumps(workshop)}\n"
        'state_dir = "/var/lib/zab-waker"\n'
    )
    return head + render(list(bots.values())), copies


def install(
    config: str, copies: list[tuple[Path, Path]], wheel: Path, role: str | None = None
) -> None:
    if os.geteuid() != 0:
        raise ValueError("--apply must run as root on the files host")
    os.umask(0o077)
    for directory in {ROOT, *(dest.parent for _, dest in copies)}:
        if directory.exists() and (directory.is_symlink() or directory.stat().st_uid != 0):
            raise ValueError("waker directories must be root-owned and not symlinks")
    news = [ROOT / "config.toml.new", ROOT / "aws-config.new", DROPIN]
    for path in [*news, *(dest for _, dest in copies)]:
        if path.is_symlink():
            raise ValueError("refusing symlink destination")
    ROOT.mkdir(mode=0o700, exist_ok=True)
    ROOT.chmod(0o700)
    for source, dest in copies:
        dest.parent.mkdir(mode=0o700, exist_ok=True)
        dest.parent.chmod(0o700)
        shutil.copyfile(source, dest)
        dest.chmod(0o600)
        os.chown(dest, 0, 0)
    temporary = ROOT / "config.toml.new"
    temporary.write_text(config)
    temporary.chmod(0o600)
    os.chown(temporary, 0, 0)
    subprocess.run(["/usr/bin/python3", "-m", "venv", "/opt/zab-waker"], check=True)
    subprocess.run(  # noqa: S603 - fixed argv, operator-selected wheel
        [
            "/opt/zab-waker/bin/python",
            "-m",
            "pip",
            "install",
            str(wheel.resolve()),
            "-r",
            str(ASSETS / "requirements.txt"),
        ],
        check=True,
    )
    subprocess.run(  # noqa: S603 - operator-selected local wheel
        [
            "/opt/zab-waker/bin/python",
            "-m",
            "pip",
            "install",
            "--force-reinstall",
            "--no-deps",
            str(wheel.resolve()),
        ],
        check=True,
    )
    subprocess.run(["/opt/zab-waker/bin/waker", "status", "--config", str(temporary)], check=True)  # noqa: S603
    subprocess.run(
        ["/opt/zab-waker/bin/python", "-c", "import zab.waker.service; import boto3"], check=True
    )
    temporary.replace(ROOT / "config.toml")
    shutil.copyfile(ASSETS / "zab-waker.service", "/etc/systemd/system/zab-waker.service")
    install_role(role)
    subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True)
    subprocess.run(["/usr/bin/systemctl", "enable", "zab-waker.service"], check=True)
    subprocess.run(["/usr/bin/systemctl", "restart", "zab-waker.service"], check=True)


#: The admin server (docs/ADMIN.md §1.2, §1.3; PR 21): `--config-dir` writes the waker's config
#: and the opted-in bots' zuliprcs into its directory, for zab-waker's group. The waker there
#: holds no AWS credential, asks zab-admin-starter for starts, and restarts itself when its
#: config changes: no wheel, no root, no systemd.
STARTER = "/run/zab-admin-starter/start.sock"
STATE = "/var/lib/zab-waker"
STATUS = "/var/lib/zab-waker/status/waker.json"
_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")  # admin.toml's workshop names
_TAG_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,127})=([A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,255})"
)


def scope_of(workshop: str, tags: list[str] | None) -> list[tuple[str, str]]:
    """admin.toml's scope tags of the workshop: `workshop=<name>` among them, each key once."""
    if not _NAME_RE.fullmatch(workshop):
        raise ValueError(f"invalid workshop name {workshop!r}")
    scope = [m.groups() for m in map(_TAG_RE.fullmatch, tags or [f"workshop={workshop}"]) if m]
    keys = [key.lower() for key, _ in scope]
    if len(scope) != len(tags or [1]) or len(set(keys)) != len(keys):
        raise ValueError("--tag takes KEY=VALUE, each key once")
    if ("workshop", workshop) not in scope:
        raise ValueError(f"the scope must include workshop={workshop}")
    return scope


def plan_shared(
    rows: list[dict],
    keys: Path,
    organizers: int,
    client,
    existing: dict,
    workshop: str,
    scope: list[tuple[str, str]],
    region: str,
    config_dir: Path,
) -> tuple[str, list[tuple[Path, Path]]]:
    """The admin server's config: this workshop's opt-ins added or updated, its existing ones
    and every other workshop's kept. Only the workshop's participant VMs qualify, as for the
    starter: its scope tags, a non-blank `participant` tag, no `zab-admin` tag."""
    workshops = {w["name"]: w for w in existing.get("workshop", [])}
    bots = {b["name"]: b for b in workshops.get(workshop, {}).get("bots", [])}
    copies = []
    for row in rows:
        slug = slug_of(row)
        if (row.get("region") or region) != region:
            raise ValueError(f"{slug}: the roster's region is not the workshop's, {region}")
        filters = [{"Name": f"tag:{key}", "Values": [value]} for key, value in scope]
        vm = find_vm(client, filters, row, slug)
        tags = {t["Key"]: t["Value"] for t in vm.get("Tags", [])}
        if not (tags.get("participant") or "").strip():
            raise ValueError(f"{slug}: the VM at the roster IP has no participant tag")
        if any(key.lower() == "zab-admin" for key in tags):
            raise ValueError(f"{slug}: the VM at the roster IP is tagged zab-admin")
        uids, emails = allowlist(row, slug)
        for kind in ("codex", "claude"):
            source = key_file(keys, slug, kind)
            dest = config_dir / workshop / slug / source.name
            copies.append((source, dest))
            name = f"{kind}-{slug}"
            bots[name] = dict(
                name=name,
                instance_id=vm["InstanceId"],
                zuliprc=str(dest),
                allow_user_ids=uids,
                allow_emails=emails,
            )
    workshops[workshop] = dict(
        name=workshop, organizers_channel=organizers, bots=list(bots.values())
    )
    check_shared(list(workshops.values()), workshop)
    return render_shared(list(workshops.values())), copies


def check_shared(workshops: list[dict], workshop: str) -> None:
    """The waker's loader (`zab.waker.__main__.load_fleets`) refuses a whole config, and so
    stops every workshop's waker, when a bot name is in two workshops or an instance in two.
    This makes the same two checks for `workshop` against the others at the dry run, so that
    the plan fails and nothing is written. Its errors name only `workshop`'s slugs, since the
    plan goes to that workshop's organizers channel (§2.1). It also refuses an alerts channel
    that another workshop's waker posts to. `tests/waker/test_deployment.py` keeps it in step
    with the loader (review of #362)."""
    mine = next(w for w in workshops if w["name"] == workshop)
    others = [w for w in workshops if w["name"] != workshop]
    names = {bot.get("name") for w in others for bot in w.get("bots", [])}
    instances = {bot.get("instance_id") for w in others for bot in w.get("bots", [])}
    if mine["organizers_channel"] in {w.get("organizers_channel") for w in others}:
        raise ValueError("the alerts channel is another workshop's waker's: refused")
    for bot in mine["bots"]:
        if bot["name"] in names or bot["instance_id"] in instances:
            slug = bot["name"].split("-", 1)[1]
            raise ValueError(f"{slug}: opted in under another workshop on this server: refused")


def remove_shared(
    existing: dict, workshop: str, slugs: list[str], config_dir: Path
) -> tuple[str, list[Path], list[str]]:
    """`--remove` (`!waker off`): the config without these participants' bots, a workshop left
    without any dropped, their zuliprcs to delete once it is written, and the slugs that had
    no bot there."""
    workshops = {w["name"]: w for w in existing.get("workshop", [])}
    names = {f"{kind}-{slug}" for slug in slugs for kind in ("codex", "claude")}
    present = {b["name"] for b in workshops.get(workshop, {}).get("bots", [])}
    absent = [s for s in slugs if not {f"codex-{s}", f"claude-{s}"} & present]
    if entry := workshops.get(workshop):
        entry["bots"] = [b for b in entry["bots"] if b["name"] not in names]
        if not entry["bots"]:
            del workshops[workshop]
    keys = [config_dir / workshop / s / f"{k}.zuliprc" for s in slugs for k in ("codex", "claude")]
    return render_shared(list(workshops.values())), keys, absent


def render_shared(workshops: list[dict]) -> str:
    lines = [
        "# push_waker.py --config-dir: the admin server's waker (docs/ADMIN.md §1.2)",
        f"starter = {json.dumps(STARTER)}",
        f"state_dir = {json.dumps(STATE)}",
        f"status_file = {json.dumps(STATUS)}",
    ]
    for entry in workshops:
        lines += ["", "[[workshop]]", f"name = {json.dumps(entry['name'])}"]
        lines.append(f"organizers_channel = {int(entry['organizers_channel'])}")
        for bot in entry["bots"]:
            lines.append("[[workshop.bots]]")
            lines.extend(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in bot.items())
    return "\n".join(lines) + "\n"


def write_shared(path: Path, data: bytes, gid: int) -> None:
    """Replace `path` whole with a 0640 file of the setgid directory's group `gid`."""
    if path.is_symlink():
        raise ValueError(f"refusing symlink destination {path}")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".new-")
    try:
        with os.fdopen(fd, "wb") as file:
            os.fchmod(file.fileno(), 0o640)
            if os.fstat(file.fileno()).st_gid != gid:
                raise ValueError(f"{path.parent}: new files don't take the waker's group")
            file.write(data)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def install_shared(
    config: str, copies: list[tuple[Path, Path]], config_dir: Path, stale: list[Path] = ()
) -> None:
    """The admin server's layout (§1.3): `config_dir` is tmpfiles.conf's setgid
    zab-admin:zab-waker 2750, so what is made in it takes the waker's group; directories 0750,
    files 0640. The config goes after the new zuliprcs and before the removal of the `stale`
    ones: the waker never reads a config that names a missing zuliprc."""
    info = config_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or not info.st_mode & stat.S_ISGID:
        raise ValueError(f"{config_dir}: expected the setgid directory of tmpfiles.conf")
    os.umask(0o027)
    for source, dest in copies:
        for directory in (dest.parent.parent, dest.parent):
            if directory.is_symlink():
                raise ValueError(f"refusing symlink destination {directory}")
            directory.mkdir(mode=0o750, exist_ok=True)
            if directory.stat().st_gid != info.st_gid:
                raise ValueError(f"{directory}: not of the waker's group")
        write_shared(dest, source.read_bytes(), info.st_gid)
    write_shared(config_dir / "config.toml", config.encode(), info.st_gid)
    for key in stale:
        key.unlink(missing_ok=True)
        with contextlib.suppress(OSError):  # the participant's directory, once empty
            key.parent.rmdir()


#: Seconds a run waits for another run's lock on the waker's config before it refuses (#348).
LOCK_WAIT = 60.0


@contextlib.contextmanager
def locked(directory: Path):
    """One writer of the waker's config at a time. Workshops apply concurrently (ADMIN §2.4),
    and each run reads, rewrites and prunes the one config they share: without this, one run
    could restore bots whose zuliprcs another just deleted, and the waker would refuse the
    config for every workshop (Codex round 2 on #343).

    The lock is `.lock` in the directory, 0600 and the writer's own: a file zab-waker can't
    open. An flock needs only a read-only fd, and the waker reads the directory through its
    group, so a lock on anything it can read would let a compromised waker hold every
    `!waker on`/`off` (final review of #343, #348). It is taken with a non-blocking retry for
    `LOCK_WAIT` seconds, then the run is refused; it goes with the process, however it ends."""
    path = directory / ".lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError(f"{path}: expected a regular file of this user, mode 0600")
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError(
                        f"another push_waker.py run has held {path} for {LOCK_WAIT:.0f} s: "
                        "nothing changed; try again once it is done"
                    ) from None
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


def own_entry(config: str, workshop: str) -> str:
    """The config as written, with only this workshop's entry: what a run prints. The admin
    bot posts it in this workshop's organizers channel, which must not see another workshop's
    participants (docs/ADMIN.md §2.1)."""
    entries = [w for w in tomllib.loads(config).get("workshop", []) if w["name"] == workshop]
    return render_shared(entries)


def shared(args: argparse.Namespace) -> int:
    """`--config-dir`: run as zab-admin on the admin server, with the instance role (§1.2).
    With --apply, from reading the config to the last deletion, under `locked`."""
    rows = select_rows(load_roster(args.roster), args.only)
    config_dir = Path(os.path.abspath(args.config_dir))
    if args.apply and os.geteuid() == 0:  # root's files and lock would shut zab-admin out
        raise ValueError("--config-dir --apply runs as zab-admin (sudo -u zab-admin), not root")
    with locked(config_dir) if args.apply else contextlib.nullcontext():
        return update_shared(args, rows, config_dir)


def update_shared(args: argparse.Namespace, rows: list[dict], config_dir: Path) -> int:
    path = config_dir / "config.toml"
    if args.remove:  # no AWS call: the VMs may be gone already
        if not _NAME_RE.fullmatch(args.workshop):
            raise ValueError(f"invalid workshop name {args.workshop!r}")
        existing = tomllib.loads(path.read_text()) if path.exists() else {}
        slugs = [slug_of(row) for row in rows]
        config, stale, absent = remove_shared(existing, args.workshop, slugs, config_dir)
        mode = "APPLY" if args.apply else "DRY RUN"
        shown = own_entry(config, args.workshop)
        print(f"{mode}: remove {', '.join(slugs)} from {args.workshop}'s waker\n{shown}")
        for slug in absent:  # a wrong --workshop changes nothing: say so (review on #343)
            print(f"warning: {slug} has no bot in {args.workshop}'s waker config")
        if args.apply:
            install_shared(config, [], config_dir, stale)
        return 0
    scope = scope_of(args.workshop, args.tag)
    with tempfile.TemporaryDirectory(prefix="zab-waker-") as scratch:
        use_profile(None, scratch)  # the server's role, never an operator's keys
        import boto3  # the release's venv has the SDK

        who = identity(boto3, args.region)
        existing = tomllib.loads(path.read_text()) if path.exists() else {}
        client = boto3.client("ec2", region_name=args.region)
        config, copies = plan_shared(
            rows,
            args.keys,
            args.organizers_channel,
            client,
            existing,
            args.workshop,
            scope,
            args.region,
            config_dir,
        )
    print(
        f"{'APPLY' if args.apply else 'DRY RUN'}: {len(rows)} selected VMs of {args.workshop}, "
        f"{len(copies)} bot files into {config_dir}; existing opt-ins retained"
    )
    print(f"AWS identity: {who}")
    print(own_entry(config, args.workshop))  # configuration only, never zuliprc contents
    if args.apply:
        install_shared(config, copies, config_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roster")
    parser.add_argument("--only", action="append", metavar="SLUG")
    parser.add_argument("--keys", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--organizers-channel", type=int)
    parser.add_argument(
        "--workshop",
        help=f"the VMs' workshop tag value, by default {WORKSHOP} (the test waker: zab-test, "
        "issue #263); with --config-dir, required: the workshop's name in admin.toml",
    )
    role_flags = parser.add_mutually_exclusive_group()
    role_flags.add_argument(
        "--assume-role",
        metavar="ARN",
        help="VMs in another account: the service assumes this role there "
        f"({ROOT}/aws-config and {DROPIN.name}); reruns keep it. Without the deployment "
        "options, on a deployed waker, only this changes (also --no-assume-role)",
    )
    role_flags.add_argument(
        "--no-assume-role", action="store_true", help="remove both: the instance's role starts VMs"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument(
        "--check",
        action="store_true",
        help="read-only, alone: the service's AWS identity and whether it may start each "
        "opted-in VM (EC2 dry run)",
    )
    admin = parser.add_argument_group(
        "the admin server (docs/ADMIN.md §1.2)",
        "--config-dir /var/lib/zab-admin/waker, as zab-admin: write the waker's config and the "
        "selected bots' zuliprcs there for zab-waker's group, with --workshop (admin.toml's "
        "name), --region and --tag from admin.toml. No --wheel: the waker runs from the release "
        "and restarts itself when its config changes; the starter starts the VMs.",
    )
    admin.add_argument("--config-dir", type=Path)
    admin.add_argument("--region", help="the workshop's region")
    admin.add_argument(
        "--tag", action="append", metavar="KEY=VALUE", help="a scope tag (default workshop=NAME)"
    )
    admin.add_argument(
        "--remove", action="store_true", help="opt the --only participants out (no AWS call)"
    )
    args = parser.parse_args(argv)
    needed = ("roster", "only", "keys", "wheel", "organizers_channel")
    if args.config_dir:
        needed = ("roster", "only", "keys", "organizers_channel", "region", "workshop")
        if args.remove:
            needed = ("roster", "only", "workshop")
        if args.wheel or args.assume_role or args.no_assume_role or args.check:
            parser.error("--config-dir takes no --wheel, --assume-role or --check")
    elif args.tag or args.region or args.remove:
        parser.error("--tag, --region and --remove go with --config-dir")
    missing = [f"--{n.replace('_', '-')}" for n in needed if getattr(args, n) is None]
    alone = len(missing) == len(needed) and (args.assume_role or args.no_assume_role)
    others = len(missing) < len(needed) or (args.workshop or WORKSHOP) != WORKSHOP
    if args.check and (others or args.assume_role or args.no_assume_role):
        parser.error("--check takes no other options: it checks what is installed")
    if missing and not (args.check or alone):
        parser.error(f"the following arguments are required: {', '.join(missing)}")
    args.workshop = args.workshop or WORKSHOP  # only direct mode has a default
    try:
        if args.config_dir:
            if not args.remove and args.organizers_channel <= 0:
                raise ValueError("positive organizers channel required")
            return shared(args)
        if args.check:
            return check()
        if alone:
            return role_only(args.assume_role, args.apply)
        if args.organizers_channel <= 0 or not args.wheel.is_file():
            raise ValueError("positive organizers channel and existing wheel required")
        rows = select_rows(load_roster(args.roster), args.only)
        role = role_config(args.assume_role, args.no_assume_role)
        regions = sorted({row.get("region") or "us-west-2" for row in rows})
        with tempfile.TemporaryDirectory(prefix="zab-waker-") as scratch:
            use_profile(role, scratch)  # discover as the service will run
            import boto3  # only this operator command needs the SDK

            who = identity(boto3, regions[0])
            verify(who, role)
            clients = {r: boto3.client("ec2", region_name=r) for r in regions}
            path = ROOT / "config.toml"
            existing = tomllib.loads(path.read_text()) if path.exists() else {}
            config, copies = plan(
                rows, args.keys, args.organizers_channel, clients, existing, args.workshop
            )
        print(
            f"{'APPLY' if args.apply else 'DRY RUN'}: {len(rows)} selected VMs, "
            f"{len(copies)} bot files; existing opt-ins retained"
        )
        print(f"AWS identity: {who}")
        print(role or "assume-role: none (the instance profile starts the VMs)")
        print(config)  # configuration only, never zuliprc contents
        if args.apply:
            install(config, copies, args.wheel, role)
        return 0
    except (ValueError, OSError) as exc:
        print(f"waker deployment refused: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
