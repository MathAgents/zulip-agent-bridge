"""Build the workshop AMI: one builder, `build.sh`, the image, terminate; `--smoke` (README.md).

    uv run --with boto3 python workshop/ami/build_ami.py --profile workshop --region us-west-2 \\
        --name nt-agents-2026-v1 --zab-wheel dist/zulip_agent_bridge-0.0.2-py3-none-any.whl \\
        --magma-dir /opt/magma --magma-passfile <licence file> \\
        [--magma-licence-setup <the deployer's licence setup script>] --with-sage \\
        [--ssh-public-key ~/.ssh/id_ed25519.pub] [--smoke] [--dry-run]
    uv run --with boto3 python workshop/ami/build_ami.py --profile workshop --smoke-only ami-…

It creates exactly what an AMI build may create (issue #36, D21): one builder instance running
`build.sh`, the image, one smoke-test instance; nothing else outlasts the build. Every resource
is tagged `workshop=nt-agents-2026`, or with the `--tag`s (a test build:
`--tag zab-role=test --tag workshop=zab-test`). The temporary security group admits SSH from
this machine's egress address only, the temporary key pair is this machine's public key, and both
are deleted at the end; the builder and the smoke instance are terminated also when a step fails.
The Magma files are copied to the builder for the build and are never committed (`.gitignore`).
Standalone script (boto3 is not a zab dependency); tests drive it with fakes for EC2 and SSH.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "aws"))
from aws_common import WARM_VOLUME, ZAB_REPO_URL  # noqa: E402 - the path is set up above

BUILD_SH = HERE / "build.sh"
AMI_FILES = HERE / "files"  # instructions, smoke sources, comparator-run (build.sh AMI_FILES)
WORKSHOP_TAG = ("workshop", "nt-agents-2026")


def scope_tags(opts: argparse.Namespace) -> dict[str, str]:
    """The tags every resource carries: `--tag KEY=VALUE` (repeatable) or the workshop default.
    Test builds pass `--tag zab-role=test --tag workshop=zab-test`: test infrastructure only."""
    tags = dict(getattr(opts, "tag", None) or [WORKSHOP_TAG])
    return tags


def parse_tag(spec: str) -> tuple[str, str]:
    key, sep, value = spec.partition("=")
    if not sep or not key or not value:
        raise argparse.ArgumentTypeError(f"{spec!r} is not KEY=VALUE")
    return key, value


UBUNTU_OWNER = "099720109477"  # Canonical
UBUNTU_NAME = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
PASSFILE_NAMES = {"magmapassfile", "magmapassfile_cloud", "oldmagmapassfile", "magmapassfile.txt"}
#: Shared by ssh and scp, so only `-o` options (scp has no `-a`; codex round 4 on PR #56).
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
    "-o", "ForwardAgent=no",
    "-o", "ServerAliveInterval=30",  # the build is long and mostly silent
    "-o", "ServerAliveCountMax=10",
]  # fmt: skip
#: The last command on the builder before the image is taken: the copied files, the licence, and
#: both authorized_keys files cloud-init wrote for this machine's key pair (root's forced-command
#: copy included), so that no VM accepts the building machine's key for `ubuntu`; the pending
#: safety shutdown is cancelled first. Checked, so a failure prevents imaging (PM review on #56).
FINAL_CLEANUP = (
    "sudo shutdown -c >/dev/null 2>&1 || true; "
    "rm -f magmapassfile magma.tar.gz magma-licence-setup build.sh ami-files.tar.gz ./*.whl"
    " ~/.ssh/authorized_keys"
    " && sudo rm -f /root/.ssh/authorized_keys"
    " && test ! -e magmapassfile && test ! -e magma-licence-setup"
    " && test ! -e ~/.ssh/authorized_keys"
    " && sudo test ! -e /root/.ssh/authorized_keys"
)
SAFETY_SHUTDOWN_MIN = {"zab-ami-builder": 240, "zab-ami-smoke": 90}
#: How long to wait for the image: v1 (about 25 GB) took minutes, v2 (TeX, Lean with Mathlib,
#: about 45 GB) was still pending after the old 30 minutes, so the build stopped short of its
#: smoke test while AWS went on creating the image (Sat Oct 3). 75 minutes, well inside the
#: builder's safety shutdown.
IMAGE_WAIT = {"Delay": 30, "MaxAttempts": 150}


class Interrupted(Exception):
    """SIGTERM or SIGHUP: raised into `main`'s handler so that cleanup runs (PM review on #56)."""


#: Magma, when the image has it (`$MAGMA`): the deployer's licence setup, where the image has one,
#: ran at this boot, and Magma runs a computation, which is what shows its licence accepted,
#: however the host meets it (README.md, "Magma's licence"). Run before and after the reboot.
MAGMA_SMOKE = r"""if [ "$MAGMA" = 1 ]; then
  if [ -e /etc/systemd/system/magma-licence.service ]; then
    check systemctl is-active magma-licence.service
  fi
  four=$(echo "print 2+2;" | timeout 120 magma -b 2>/dev/null | tr -d "[:space:]")
  if [ "$four" = 4 ]; then echo "ok   magma -b runs print 2+2"
  else echo "FAIL magma -b does not print 4 for print 2+2: is its licence accepted here?"; fail=1
  fi
fi
"""

#: The smoke test's checks (README.md) on a fresh instance from the image, run as ubuntu.
#: `$MAGMA`/`$SAGE` say what the image was built with; `$BUILDER_MACHINE_ID`/`$BUILDER_HOST_FP`
#: are the builder's values, which the instance must not share (cloud-init regenerates both).
SMOKE_SCRIPT = r"""
set -u
# From a directory every user can read, as build.sh does: ssh lands in /home/ubuntu (0750), and
# commands run as agent from there fail where a tool looks at its working directory. GAP.jl's
# initialisation stats a relative path, so `using Oscar` died with EACCES on the v3 smoke VM.
cd /
fail=0
check() { if "$@" >/dev/null 2>&1; then echo "ok   $*"; else echo "FAIL $*"; fail=1; fi; }
as_agent() { sudo -u agent -H env HOME=/home/agent PATH=/home/agent/.local/bin:$PATH "$@"; }
check bash -c "test \"\$(gp --version-short)\" = '$PARI_VERSION'"
check bash -c 'test "$(command -v gp)" = /usr/local/bin/gp'
GP_PLAIN='out=$(echo "matid(60)" | gp -q 2>/dev/null); n=$(printf "%s\n" "$out" | wc -l)
[ "$n" -ge 60 ] && ! printf "%s" "$out" | grep -q "\[+++\]" &&
! printf "%s" "$out" | grep -q "$(printf "\033")"'
check bash -c "$GP_PLAIN"          # without -f: no gprc colouring or `[+++]` cut-off (PM, #60)
check as_agent bash -lc "$GP_PLAIN"
check bash -c 'test "$(gp -q -f <<<"print(ellinit(\"11a1\").a6)" 2>/dev/null)" = -20'
check bash -c 'test "$(gp -q -f <<<"print(type(polgalois(x^8-2)))" 2>/dev/null)" = t_VEC'
sea='print(type(ellsea(ellinit([1,0,0,-1,0],nextprime(10^30)))))'
check bash -c "test \"\$(gp -q -f <<<'$sea' 2>/dev/null)\" = t_INT"
check bash -c 'test "$(gp -q -f <<<"print(type(galoisgetpol(10,1)))" 2>/dev/null)" = t_VEC'
check bash -c 'test "$(gp -q -f <<<"print(type(nflist(\"A5\",[1,2000])))" 2>/dev/null)" = t_VEC'
check bash -c 'test "$(gap -q <<<"Print(Size(SmallGroup(8,3)), \"\\n\");" 2>/dev/null)" = 8'
check as_agent bash -c "codex --version | grep -q '$CODEX_VERSION'"
check as_agent bash -c "claude --version | grep -q '$CLAUDE_VERSION'"
check as_agent zab --version
check as_agent /home/agent/venv/bin/python -c "import cypari2"
check sudo -u agent sudo -n true
check as_agent grep -q '^check_for_update_on_startup = false' /home/agent/.codex/config.toml
check as_agent grep -q '^\[mcp_servers\.lmfdb\]' /home/agent/.codex/config.toml
check as_agent bash -c "claude doctor </dev/null 2>&1 | grep -q 'Auto-updates: disabled'"
check as_agent bash -c "claude mcp get lmfdb 2>/dev/null | grep -q 'mcp.lmfdb.org'"
check bash -c 'grep -Eq "^[0-9a-f]{32}$" /etc/machine-id'
check bash -c 'test "$(cat /etc/machine-id)" != "$BUILDER_MACHINE_ID"'
hostfp=$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d" " -f2)
check test "$hostfp" != "$BUILDER_HOST_FP"
@MAGMA_SMOKE@
if [ "$SAGE" = 1 ]; then
  rank='print(EllipticCurve("11a1").rank())'
  check as_agent bash -c "test \"\$(sage -c '$rank' 2>/dev/null)\" = 0"
fi
# AMI v2 (PM's list on #36, item 7): code-mode host, Lean tools, LaTeX, comparator.
as_lean() {
  as_agent env PATH=/home/agent/.elan/bin:/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin "$@"
}
host='h=$(dirname "$(readlink -f ~/.local/bin/codex)")/codex-code-mode-host'
check as_agent bash -c "$host"' && test -x "$h" && "$h" --help'
check as_agent grep -q '^\[mcp_servers\.lean-lsp\]' /home/agent/.codex/config.toml
check as_agent bash -c "claude mcp get lean-lsp 2>/dev/null | grep -q 'Status: .*Connected'"
lean4='.[] | select(.id == "lean4@lean4-skills" and .enabled)'
check as_agent bash -c "claude plugin list --json </dev/null 2>/dev/null | jq -e '$lean4'"
if as_agent bash -c 'timeout 60 codex plugin list </dev/null 2>&1 | grep -q lean4@lean4-skills'
then echo "ok   codex plugin lean4@lean4-skills"
else echo "WARN codex plugin lean4@lean4-skills (best effort, see build.sh)"
fi
check as_agent bash -lc 'command -v lean && command -v lake && command -v comparator-run'
check as_agent bash -ic 'command -v lean && command -v lake' </dev/null
check as_agent test -s /home/agent/.claude/CLAUDE.md
check as_agent test -s /home/agent/.codex/AGENTS.md
d=/usr/local/share/workshop/smoke
tex="t=\$(mktemp -d) && cp $d/smoke.tex $d/smoke.bib \$t && cd \$t && timeout 300 latexmk -pdf"
tex="$tex -interaction=nonstopmode -halt-on-error smoke.tex && test -s smoke.pdf"
check as_agent bash -c "$tex && grep -q Serre smoke.bbl"
# The first Mathlib import on a fresh volume reads ~5 GB of oleans that EBS fetches lazily from
# the snapshot: timed, with a generous limit (seconds on a warm volume).
# Lean's exit status counts, not only the #eval line (a theorem error still prints 1024).
lean="out=\$(cd ~/lean/workspace && timeout 900 lake env lean $d/Smoke.lean)"
lean="$lean && grep -qx 1024 <<<\"\$out\""
start=$SECONDS
check as_lean bash -c "$lean"
echo "time first Mathlib import: $((SECONDS - start)) s"
start=$SECONDS
check as_lean bash -c "$lean"
echo "time second Mathlib import: $((SECONDS - start)) s"
cmp="t=\$(mktemp -d) && cp -r $d/comparator/. \$t && cd \$t"
check as_lean bash -c "$cmp && timeout 300 comparator-run config.json"
# AMI v3: Julia and Oscar. The first `using Oscar` reads the depot, which EBS fetches lazily;
# it must load from the precompiled cache ("Precompiling" would mean a rebuild): timed twice.
# On failure the output's tail is shown. Before the timed loads Julia says whether it takes
# Oscar's cache (a read, no compiling); the first timed load runs with JULIA_DEBUG=loading, and
# on failure its "Rejecting cache file" reasons are shown too. No load runs before the checks,
# so a stale cache cannot be rebuilt unseen (codex review on #140).
check bash -c 'julia --version | grep -q "^julia version 1\."'
jlid='Base.PkgId(Base.UUID("f1435218-dba5-11e9-1e4d-f1a5fab5fc13"), "Oscar")'
pre=$(as_agent julia -e "println(Base.isprecompiled($jlid))" 2>&1 | tail -n1)
echo "Oscar precompiled before loading: $pre"
oscar='using Oscar; Qx, x = polynomial_ring(QQ, :x); K, a = number_field(x^2 + 5)'
oscar="$oscar; println(order(class_group(K)[1]))"
oscar_check() {  # $1: an environment assignment for this run (JULIA_DEBUG=loading), or none
  local start=$SECONDS out rc
  out=$(as_agent env ${1:+"$1"} timeout 900 julia -e "$oscar" 2>&1); rc=$?
  out=$(tr -d '\r' <<<"$out")
  if [ "$rc" = 0 ] && [ "$(tail -n1 <<<"$out")" = 2 ] && ! grep -q Precompiling <<<"$out"; then
    echo "ok   using Oscar, class group of Q(sqrt(-5)) has order 2 ($((SECONDS - start)) s)"
  else
    echo "FAIL using Oscar (exit $rc, $((SECONDS - start)) s)"
    grep -E 'Rejecting|Precompiling' <<<"$out" | grep -v '/opt/julia-' | head -n 8 \
      | sed 's/^/     | /'
    grep -v -E '^\s*(@|\[[0-9]+\])|Debug:|^│|^└' <<<"$out" | tail -n 8 | sed 's/^/     | /'
    fail=1
  fi
}
oscar_check JULIA_DEBUG=loading
oscar_check
# Issue #474 (review on #481): HomotopyContinuation and Oscar's extension for it load from the
# cache too, checked before the tools check below, which would recompile a rejected cache unseen.
hc='using Oscar, HomotopyContinuation'
hc="$hc; println(Base.get_extension(Oscar, :HomotopyContinuationExt) !== nothing)"
out=$(as_agent timeout 900 julia -e "$hc" 2>&1); rc=$?
out=$(tr -d '\r' <<<"$out")
if [ "$rc" = 0 ] && [ "$(tail -n1 <<<"$out")" = true ] && ! grep -q Precompiling <<<"$out"; then
  echo "ok   using Oscar, HomotopyContinuation from the cache, with Oscar's extension"
else
  echo "FAIL using Oscar, HomotopyContinuation (exit $rc)"
  tail -n 8 <<<"$out" | sed 's/^/     | /'
  fail=1
fi
check bash -c '/opt/certbot/bin/certbot --help all | grep -q -- --ip-address'
check bash -c 'jq -e ".disableClaudeAiConnectors == true" /etc/claude-code/managed-settings.json'
# Issue #474: every tool of /etc/zab/tools.json again, from the image's tools.toml: each one's
# small computation, as agent with the services' PATH, and the version the build recorded. One
# line per tool; the Julia ones load Oscar, about half a minute each.
have=""
if [ "$MAGMA" = 1 ]; then have=magma; fi
if [ "$SAGE" = 1 ]; then have="$have${have:+,}sage"; fi
svc=/home/agent/venv/bin:/home/agent/.elan/bin:/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin
w=/usr/local/share/workshop
start=$SECONDS
if as_agent env -i HOME=/home/agent USER=agent LOGNAME=agent SHELL=/bin/bash LANG=C.UTF-8 \
  PATH="$svc" /usr/bin/python3 $w/bin/tools_manifest.py check --source $w/tools.toml \
  --manifest /etc/zab/tools.json --have "$have" 2>&1
then echo "ok   tools manifest: every tool as recorded ($((SECONDS - start)) s)"
else echo "FAIL tools manifest ($((SECONDS - start)) s)"; fail=1
fi
check bash -c 'test "$(stat -c %a /etc/zab/tools.json)" = 644'
echo "julia depot $(sudo du -sh /home/agent/.julia | cut -f1)"
abi=$(python3 -c 'import ctypes; print(ctypes.CDLL(None).syscall(444, None, 0, 1))')
echo "kernel $(uname -r), Landlock ABI $abi, disk used $(df -h --output=used / | tail -n1)"
echo "SMOKE-RESULT $fail"
""".replace("@MAGMA_SMOKE@\n", MAGMA_SMOKE)

#: After `sudo reboot`: Magma's licence must hold again on its own.
REBOOT_SCRIPT = r"""
set -u
fail=0
check() { if "$@" >/dev/null 2>&1; then echo "ok   $*"; else echo "FAIL $*"; fail=1; fi; }
check bash -c 'test "$(uptime -s)" != "$BOOT_BEFORE"'
@MAGMA_SMOKE@
echo "SMOKE-RESULT $fail"
""".replace("@MAGMA_SMOKE@\n", MAGMA_SMOKE)


# ------------------------------------------------------------------------------------ helpers


def tag_list(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def egress_ip(urlopen: Callable[..., Any] = urllib.request.urlopen) -> str:
    """This machine's public address, for the builder's SSH rule: SSH from one place."""
    with urlopen("https://checkip.amazonaws.com", timeout=20) as resp:
        ip = resp.read().decode().strip()
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
        raise RuntimeError(f"unexpected answer from checkip: {ip!r}")
    return ip


def validate_magma_dir(magma_dir: str) -> Path:
    """The resolved Magma tree (a symlink would otherwise tar as one link), checked before any
    AWS call: magma.exe, the `magma` driver, libs/ and package/ must be there."""
    root = Path(magma_dir).expanduser().resolve()
    for needed in ("magma.exe", "magma", "libs", "package"):
        if not (root / needed).exists():
            raise ValueError(f"{magma_dir} is not a Magma installation ({needed} missing)")
    return root


def magma_tarball(magma_dir: str, dest: str) -> str:
    """Tar the Magma tree without any licence file (never commit or copy those by accident)."""
    root = validate_magma_dir(magma_dir)

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        name = Path(info.name).name
        return None if name in PASSFILE_NAMES or name.startswith("magmapassfile") else info

    with tarfile.open(dest, "w:gz", compresslevel=1) as tar:
        tar.add(str(root), arcname="magma", filter=keep)
    return dest


def files_tarball(dest: str) -> str:
    """Tar workshop/ami/files (members relative to it), for build.sh's AMI_FILES."""
    with tarfile.open(dest, "w:gz") as tar:
        for path in sorted(AMI_FILES.rglob("*")):
            if path.is_file():
                tar.add(str(path), arcname=str(path.relative_to(AMI_FILES)))
    return dest


def console_fingerprints(text: str) -> set[str]:
    """`SHA256:…` host-key fingerprints cloud-init prints to the console at first boot."""
    block = re.search(
        r"BEGIN SSH HOST KEY FINGERPRINTS-----(.*?)-----END SSH HOST KEY FINGERPRINTS", text, re.S
    )
    return set(re.findall(r"SHA256:[A-Za-z0-9+/=]+", block.group(1))) if block else set()


def instance_params(
    opts: argparse.Namespace, image: str, sg: str, key: str, name: str
) -> dict[str, Any]:
    """The builder's (and the smoke instance's) `run_instances` parameters, the fleet's shape
    (workshop/aws/README.md, "The VMs"). The smoke instance runs on the fleet's type (D23), the
    builder on `--instance-type`."""
    tags = {"Name": name, **scope_tags(opts), "role": "ami-build"}
    smoke = name == "zab-ami-smoke"
    params: dict[str, Any] = {
        "ImageId": image,
        "InstanceType": opts.smoke_instance_type if smoke else opts.instance_type,
        "KeyName": key,
        "SecurityGroupIds": [sg],
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {"VolumeSize": 100, "VolumeType": "gp3", "DeleteOnTermination": True},
            }
        ],
        "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled"},
        # a `shutdown -h +N` safety net on the instance then terminates it, so a build that lost
        # its driver cannot leave a builder (with the licence on it) running for days
        "InstanceInitiatedShutdownBehavior": "terminate",
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": tag_list(tags)},
            {"ResourceType": "volume", "Tags": tag_list(tags)},
        ],
    }
    if getattr(opts, "subnet_id", None):  # a subnet of the chosen VPC (codex round 2 on PR #56)
        params["SubnetId"] = opts.subnet_id
    return params


def build_knobs(opts: argparse.Namespace, remote: dict[str, str]) -> dict[str, str]:
    """Environment for `build.sh` on the builder; `remote` maps a knob to the copied file."""
    knobs = {
        "CODEX_VERSION": opts.codex_version,
        "CLAUDE_VERSION": opts.claude_version,
        "ZAB_TAG": opts.zab_tag,
        "ZAB_REPO": getattr(opts, "zab_repo", None) or ZAB_REPO_URL,  # read without a wheel
        "PARI_VERSION": opts.pari_version,
        "IMAGE_NAME": opts.name,  # /etc/zab/tools.json's "image" (issue #474)
    }
    knobs.update(remote)
    if opts.with_sage:
        knobs["WITH_SAGE"] = "1"
        knobs["SAGE_VERSION"] = opts.sage_version
    return knobs


def env_command(env: dict[str, str], command: str) -> str:
    """`env K=V … <command>`, every value shell-quoted (no shell strings from data)."""
    pairs = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(env.items()))
    return f"env {pairs} {command}"


def heredoc(env: dict[str, str], script: str) -> str:
    """Feed `script` to a remote `bash -s` through a quoted heredoc, with `env` set."""
    return env_command(env, "bash -s") + " <<'ZAB_EOF'\n" + script + "\nZAB_EOF"


# -------------------------------------------------------------------------------------- EC2


class Ec2:
    """The few EC2 calls the build makes. Tests substitute a fake with the same methods."""

    def __init__(self, region: str, profile: str | None) -> None:
        import boto3  # lazy: only the real client needs it

        self._session = boto3.Session(profile_name=profile, region_name=region)
        self._ec2 = self._session.client("ec2")

    def identity(self) -> tuple[str, str]:
        """(account id, caller ARN) from STS, checked against --account-id before any create."""
        who = self._session.client("sts").get_caller_identity()
        return str(who["Account"]), str(who["Arn"])

    def image_name_taken(self, name: str) -> bool:
        images = self._ec2.describe_images(
            Owners=["self"], Filters=[{"Name": "name", "Values": [name]}]
        )["Images"]
        return bool(images)

    def latest_ubuntu_image(self) -> str:
        images = self._ec2.describe_images(
            Owners=[UBUNTU_OWNER],
            Filters=[
                {"Name": "name", "Values": [UBUNTU_NAME]},
                {"Name": "architecture", "Values": ["x86_64"]},
                {"Name": "state", "Values": ["available"]},
            ],
        )["Images"]
        if not images:
            raise RuntimeError("no Ubuntu 24.04 amd64 image found")
        return max(images, key=lambda im: im["CreationDate"])["ImageId"]

    def default_vpc(self) -> str:
        vpcs = self._ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            raise RuntimeError("no default VPC; pass --vpc-id or --subnet-id")
        return vpcs[0]["VpcId"]

    def subnet(self, subnet_id: str) -> dict[str, Any]:
        subnets = self._ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"]
        if not subnets:
            raise RuntimeError(f"subnet {subnet_id} not found")
        return subnets[0]

    def public_subnet(self, vpc_id: str) -> str:
        """A subnet of `vpc_id` that assigns public addresses (the builder must answer SSH)."""
        subnets = self._ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])[
            "Subnets"
        ]
        public = [sn for sn in subnets if sn.get("MapPublicIpOnLaunch")]
        if not public:
            raise RuntimeError(f"no subnet of {vpc_id} assigns public IPs; pass --subnet-id")
        public.sort(key=lambda sn: (not sn.get("DefaultForAz", False), sn["SubnetId"]))
        return str(public[0]["SubnetId"])

    def create_security_group(self, name: str, vpc: str, tags: dict[str, str]) -> str:
        """The group alone; `authorize_ssh` adds the rule, so that the caller can register the
        group for cleanup in between (codex round 3 on PR #56)."""
        return str(
            self._ec2.create_security_group(
                GroupName=name,
                Description="zab AMI builder: SSH from the building machine only",
                VpcId=vpc,
                TagSpecifications=[{"ResourceType": "security-group", "Tags": tag_list(tags)}],
            )["GroupId"]
        )

    def authorize_ssh(self, sg: str, cidr: str) -> None:
        self._ec2.authorize_security_group_ingress(
            GroupId=sg,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": cidr, "Description": "AMI builder"}],
                }
            ],
        )

    def delete_security_group(self, sg: str) -> None:
        self._ec2.delete_security_group(GroupId=sg)

    def import_key_pair(self, name: str, public_key: bytes, tags: dict[str, str]) -> str:
        self._ec2.import_key_pair(
            KeyName=name,
            PublicKeyMaterial=public_key,
            TagSpecifications=[{"ResourceType": "key-pair", "Tags": tag_list(tags)}],
        )
        return name

    def delete_key_pair(self, name: str) -> None:
        self._ec2.delete_key_pair(KeyName=name)

    def run_instance(self, **params: Any) -> str:
        res = self._ec2.run_instances(MinCount=1, MaxCount=1, **params)
        return res["Instances"][0]["InstanceId"]

    def wait_running(self, instance_id: str) -> None:
        self._ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

    def public_ip(self, instance_id: str) -> str:
        res = self._ec2.describe_instances(InstanceIds=[instance_id])
        return str(res["Reservations"][0]["Instances"][0].get("PublicIpAddress", ""))

    def console_output(self, instance_id: str) -> str:
        return str(self._ec2.get_console_output(InstanceId=instance_id).get("Output") or "")

    def create_image(
        self, instance_id: str, name: str, description: str, tags: dict[str, str]
    ) -> str:
        res = self._ec2.create_image(
            InstanceId=instance_id,
            Name=name,
            Description=description,
            TagSpecifications=[
                {"ResourceType": "image", "Tags": tag_list(tags)},
                {"ResourceType": "snapshot", "Tags": tag_list(tags)},
            ],
        )
        return res["ImageId"]

    def wait_image(self, image_id: str) -> None:
        waiter = self._ec2.get_waiter("image_available")
        waiter.wait(ImageIds=[image_id], WaiterConfig=IMAGE_WAIT)

    def terminate(self, instance_ids: list[str]) -> None:
        self._ec2.terminate_instances(InstanceIds=instance_ids)

    def wait_terminated(self, instance_ids: list[str]) -> None:
        self._ec2.get_waiter("instance_terminated").wait(InstanceIds=instance_ids)


# -------------------------------------------------------------------------------------- SSH


class Ssh:
    """ssh/scp as argv lists against a dedicated known_hosts (no `~/.ssh` state).

    The host key is pinned before the first connection: `pin_host_key` scans it, stores it, and
    returns its fingerprint for the console check; every ssh/scp then runs with
    `StrictHostKeyChecking=yes`, so the key that was verified is the only one ever trusted
    (codex round 3 on PR #56).
    """

    def __init__(self, key_file: str | None, known_hosts: str, log: Callable[[str], None]) -> None:
        self.known_hosts = known_hosts
        self.log = log
        self.identity = ["-i", key_file] if key_file else []

    def _opts(self) -> list[str]:
        return [
            *SSH_OPTS,
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            "StrictHostKeyChecking=yes",
            *self.identity,
        ]

    def _argv(self, ip: str, command: str) -> list[str]:
        return ["ssh", *self._opts(), f"ubuntu@{ip}", command]

    @staticmethod
    def wait_port(ip: str, timeout_s: float = 300.0) -> bool:
        """True once TCP port 22 accepts a connection."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((ip, 22), 5.0):
                    return True
            except OSError:
                time.sleep(5.0)
        return False

    def pin_host_key(self, ip: str) -> str:
        """Scan the host's ed25519 key into known_hosts; returns its `SHA256:…` fingerprint."""
        scan = subprocess.run(  # noqa: S603
            ["ssh-keyscan", "-t", "ed25519", "-T", "15", ip],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = [line for line in scan.stdout.splitlines() if line and not line.startswith("#")]
        if not lines:
            raise RuntimeError(f"no ed25519 host key scanned from {ip}")
        keyed = lines[0] + "\n"
        fp = subprocess.run(
            ["ssh-keygen", "-lf", "-"],  # noqa: S607
            input=keyed,
            capture_output=True,
            text=True,
            timeout=30,
        )
        m = re.search(r"SHA256:[A-Za-z0-9+/=]+", fp.stdout)
        if not m:
            raise RuntimeError(f"could not fingerprint the scanned key of {ip}")
        with open(self.known_hosts, "a", encoding="utf-8") as f:
            f.write(keyed)
        return m.group(0)

    def wait_ready(self, ip: str, timeout_s: float = 300.0) -> bool:
        """True once a (strictly checked) ssh session runs `true`."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            res = self.run(ip, "true", timeout_s=30, check=False)
            if res.returncode == 0:
                return True
            time.sleep(5.0)
        return False

    def run(
        self, ip: str, command: str, *, timeout_s: float = 3600.0, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        argv = self._argv(ip, command)
        res = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL
        )
        if check and res.returncode != 0:
            raise RuntimeError(
                f"ssh {command[:60]!r} failed ({res.returncode}): {res.stderr[-500:]}"
            )
        return res

    def stream(self, ip: str, command: str, timeout_s: float = 7200.0) -> int:
        """Run with the output streamed to our log (the long build), under a real deadline.

        The output is pumped by a thread, so the deadline holds even while the remote side is
        silent (reading the pipe inline would block past it; codex round 1 on PR #56). On expiry
        the ssh process is killed and reaped, and `TimeoutExpired` propagates so that cleanup runs.
        """
        proc = subprocess.Popen(  # noqa: S603
            self._argv(ip, command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",  # a stray byte in the build output must not stall the pump
        )
        assert proc.stdout is not None
        stdout = proc.stdout

        def pump() -> None:
            for line in stdout:
                self.log("    " + line.rstrip("\n"))

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            return proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        finally:
            reader.join(timeout=5)

    def _scp_argv(self, ip: str, local: str, remote: str) -> list[str]:
        return ["scp", "-q", *self._opts(), local, f"ubuntu@{ip}:{remote}"]

    def copy(self, ip: str, local: str, remote: str) -> None:
        subprocess.run(  # noqa: S603
            self._scp_argv(ip, local, remote), check=True, timeout=3600, stdin=subprocess.DEVNULL
        )


# ------------------------------------------------------------------------------------- build


@dataclass
class Resources:
    """What was created, so that `cleanup` can undo it in any state."""

    security_group: str = ""
    key_pair: str = ""
    instances: list[str] = field(default_factory=list)


def cleanup(ec2: Any, res: Resources, log: Callable[[str], None]) -> None:
    """Terminate instances, then delete the key pair and the group (retried: the group is busy
    until the instances are gone). Never raises past a failed step: the rest is still tried."""
    if res.instances:
        log(f"terminating {', '.join(res.instances)}")
        try:
            ec2.terminate(res.instances)
            ec2.wait_terminated(res.instances)
        except Exception as exc:
            log(f"WARNING terminate failed: {type(exc).__name__}: {exc}")
        res.instances = []
    if res.key_pair:
        try:
            ec2.delete_key_pair(res.key_pair)
        except Exception as exc:
            log(f"WARNING delete key pair {res.key_pair}: {type(exc).__name__}: {exc}")
        res.key_pair = ""
    if res.security_group:
        for attempt in range(12):
            try:
                ec2.delete_security_group(res.security_group)
                break
            except Exception as exc:
                if attempt == 11:
                    log(
                        f"WARNING delete {res.security_group} yourself: {type(exc).__name__}: {exc}"
                    )
                else:
                    time.sleep(10)
        res.security_group = ""


def verify_host_key(
    ec2: Any, instance_id: str, pinned: str, log: Callable[[str], None], tries: int = 20
) -> None:
    """Compare the pinned host key's fingerprint with the ones cloud-init printed to the instance
    console; the console lags by minutes, so wait for it, and warn (first contact only) if it
    never shows. A mismatch raises before any ssh session is opened."""
    for _ in range(tries):
        printed = console_fingerprints(ec2.console_output(instance_id))
        if printed:
            if pinned not in printed:
                raise RuntimeError(
                    f"pinned host key {pinned} is not in the console output {printed}"
                )
            log(f"host key {pinned} matches the console output")
            return
        time.sleep(15)
    log(f"WARNING console output never showed fingerprints; trusting {pinned} on first contact")


def prepare(ec2: Any, opts: argparse.Namespace, res: Resources, log: Callable[[str], None]) -> None:
    """The temporary security group and key pair."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tags = {**scope_tags(opts), "role": "ami-build"}
    cidr = f"{opts.my_ip or egress_ip()}/32"
    # The group and the instance must share a VPC, and the subnet must hand out a public address
    # (codex round 2 on PR #56): --subnet-id fixes both; --vpc-id picks a public subnet of that
    # VPC; neither means EC2's default VPC and its default subnet.
    if opts.subnet_id:
        subnet = ec2.subnet(opts.subnet_id)
        if not subnet.get("MapPublicIpOnLaunch"):
            raise RuntimeError(f"{opts.subnet_id} does not assign public IPs; SSH needs one")
        vpc = str(subnet["VpcId"])
        if opts.vpc_id and opts.vpc_id != vpc:
            raise RuntimeError(f"{opts.subnet_id} is in {vpc}, not in {opts.vpc_id}")
    elif opts.vpc_id:
        vpc = opts.vpc_id
        opts.subnet_id = ec2.public_subnet(vpc)
    else:
        vpc = ec2.default_vpc()
    res.security_group = ec2.create_security_group(f"zab-ami-builder-{stamp}", vpc, tags)
    ec2.authorize_ssh(res.security_group, cidr)  # registered above first: cleanup finds it
    log(f"security group {res.security_group} in {vpc}: SSH from {cidr} only")
    if opts.subnet_id:
        log(f"subnet {opts.subnet_id}")
    public_key = Path(opts.ssh_public_key).expanduser().read_bytes()
    res.key_pair = ec2.import_key_pair(f"zab-ami-builder-{stamp}", public_key, tags)
    log(f"key pair {res.key_pair} from {opts.ssh_public_key}")


def launch(
    ec2: Any,
    ssh: Ssh,
    opts: argparse.Namespace,
    res: Resources,
    image: str,
    name: str,
    log: Callable[[str], None],
) -> tuple[str, str, str]:
    """One instance from `image`, registered for cleanup; returns (id, ip, host fingerprint)
    once its host key is pinned and verified and a strict ssh session works."""
    params = instance_params(opts, image, res.security_group, res.key_pair, name)
    instance = ec2.run_instance(**params)
    res.instances.append(instance)
    log(f"{name}: {instance} launched; waiting")
    ec2.wait_running(instance)
    ip = ec2.public_ip(instance)
    if not ip or not ssh.wait_port(ip):
        raise RuntimeError(f"{instance} ({ip or 'no public ip'}) never answered on port 22")
    pinned = ssh.pin_host_key(ip)
    verify_host_key(ec2, instance, pinned, log)
    if not ssh.wait_ready(ip):
        raise RuntimeError(f"{instance} ({ip}) accepts no ssh session with the pinned key")
    minutes = SAFETY_SHUTDOWN_MIN.get(name, 120)
    ssh.run(ip, f"sudo shutdown -h +{minutes}")  # terminates the instance (see instance_params)
    log(f"{name}: safety shutdown in {minutes} min unless cancelled")
    return instance, ip, pinned


def build(
    ec2: Any, ssh: Ssh, opts: argparse.Namespace, res: Resources, log: Callable[[str], None]
) -> tuple[str, dict[str, str]]:
    """Run build.sh on a fresh builder and create the image. Returns (ami id, builder identity)."""
    if ec2.image_name_taken(opts.name):
        raise RuntimeError(f"an image named {opts.name!r} already exists; pass another --name")
    base = opts.base_ami or ec2.latest_ubuntu_image()
    log(f"base image {base}")
    instance, ip, pinned = launch(ec2, ssh, opts, res, base, "zab-ami-builder", log)
    identity = {
        "BUILDER_MACHINE_ID": ssh.run(ip, "cat /etc/machine-id").stdout.strip(),
        "BUILDER_HOST_FP": pinned,
    }
    remote: dict[str, str] = {}
    ssh.copy(ip, str(BUILD_SH), "build.sh")
    with tempfile.TemporaryDirectory() as tmp:
        ssh.copy(ip, files_tarball(os.path.join(tmp, "ami-files.tar.gz")), "ami-files.tar.gz")
    remote["AMI_FILES"] = "/home/ubuntu/ami-files.tar.gz"
    if opts.zab_wheel:
        ssh.copy(ip, opts.zab_wheel, Path(opts.zab_wheel).name)
        remote["ZAB_WHEEL"] = f"/home/ubuntu/{Path(opts.zab_wheel).name}"
    if opts.magma_dir:
        with tempfile.TemporaryDirectory() as tmp:
            tarball = magma_tarball(opts.magma_dir, os.path.join(tmp, "magma.tar.gz"))
            size_mib = os.path.getsize(tarball) // 2**20
            log(f"copying the Magma tree ({size_mib} MiB, no licence files)")
            ssh.copy(ip, tarball, "magma.tar.gz")
        ssh.copy(ip, opts.magma_passfile, "magmapassfile")
        ssh.run(ip, "chmod 0600 magmapassfile")
        remote.update(
            MAGMA_TARBALL="/home/ubuntu/magma.tar.gz", MAGMA_PASSFILE="/home/ubuntu/magmapassfile"
        )
        if opts.magma_licence_setup:  # passed through; main() read only its #!
            ssh.copy(ip, opts.magma_licence_setup, "magma-licence-setup")
            ssh.run(ip, "chmod 0700 magma-licence-setup")
            remote["MAGMA_LICENCE_SETUP"] = "/home/ubuntu/magma-licence-setup"
    log("running build.sh (this takes a while; PARI, conda and Magma are the slow parts)")
    code = ssh.stream(ip, env_command(build_knobs(opts, remote), "bash build.sh"))
    if code != 0:
        raise RuntimeError(f"build.sh exited {code}; the builder {instance} is being terminated")
    ssh.run(ip, FINAL_CLEANUP)  # checked: no image from a builder that still holds the files
    log("builder cleaned: copied files, licence and both authorized_keys removed")
    log(f"creating image {opts.name}")
    ami = ec2.create_image(
        instance,
        opts.name,
        f"{opts.name} workshop AMI (zab {opts.zab_tag}, PARI {opts.pari_version})",
        {**scope_tags(opts), "Name": opts.name},
    )
    print(f"AMI {ami} (creating)", flush=True)  # known at once, even if the wait is cut short
    ec2.wait_image(ami)
    log(f"image {ami} available")
    return ami, identity


def smoke(
    ec2: Any,
    ssh: Ssh,
    opts: argparse.Namespace,
    res: Resources,
    ami: str,
    identity: dict[str, str],
    log: Callable[[str], None],
) -> bool:
    """One instance from the AMI: the smoke-test checks, a reboot, the Magma checks again."""
    _instance, ip, _pinned = launch(ec2, ssh, opts, res, ami, "zab-ami-smoke", log)
    env = {
        "PARI_VERSION": opts.pari_version,
        "CODEX_VERSION": opts.codex_version,
        "CLAUDE_VERSION": opts.claude_version,
        "MAGMA": "1" if opts.magma_dir or opts.assume_magma else "0",
        "SAGE": "1" if opts.with_sage or opts.assume_sage else "0",
        **identity,
    }
    if opts.smoke_warm:  # what a fleet VM's first boot does, here in the foreground and timed
        warm = f'start=$SECONDS; {WARM_VOLUME}; echo "time warm-up: $((SECONDS - start)) s"'
        log(ssh.run(ip, "sudo bash -c " + shlex.quote(warm), check=False).stdout.rstrip())
    out = ssh.run(ip, heredoc(env, SMOKE_SCRIPT), check=False)
    log(out.stdout.rstrip())
    ok = "SMOKE-RESULT 0" in out.stdout
    boot_before = ssh.run(ip, "uptime -s").stdout.strip()
    log("rebooting the smoke instance")
    ssh.run(ip, "sudo systemctl reboot", check=False, timeout_s=60)
    time.sleep(20)
    if not ssh.wait_ready(ip):
        log("FAIL the smoke instance did not come back after the reboot")
        return False
    ssh.run(ip, "sudo shutdown -h +60", check=False)  # the reboot dropped the first safety net
    env["BOOT_BEFORE"] = boot_before
    out = ssh.run(ip, heredoc(env, REBOOT_SCRIPT), check=False)
    log(out.stdout.rstrip())
    return ok and "SMOKE-RESULT 0" in out.stdout


def keep_running(res: Resources, log: Callable[[str], None]) -> None:
    """`--keep`: leave the current instance(s) for inspection; the group and key pair stay too,
    since the instance cannot be reached without them (terminate, then delete them yourself)."""
    if not res.instances:
        return
    log(
        f"--keep: leaving {res.instances} running with {res.security_group} and {res.key_pair} "
        "(terminate the instance, then delete the group and key pair yourself)"
    )
    res.instances = []
    res.security_group = ""
    res.key_pair = ""


def plan(opts: argparse.Namespace, log: Callable[[str], None]) -> None:
    """What a run would do, without touching AWS (`--dry-run`)."""
    base = opts.base_ami or "the latest Ubuntu 24.04 amd64 image"
    log(f"would check that profile {opts.profile!r} is account {opts.account_id} in {opts.region}")
    log(f"would create a security group (SSH from {opts.my_ip or '<egress ip>'}/32)")
    log(f"would import {opts.ssh_public_key} as a temporary key pair")
    if opts.smoke_only:  # no builder, no image: one instance from the given AMI
        log(
            f"would launch one {opts.smoke_instance_type} instance from {opts.smoke_only}, run the"
            " smoke-test checks, reboot, check again, terminate it, delete the group"
        )
        return
    where = opts.subnet_id or (
        f"a public subnet of {opts.vpc_id}" if opts.vpc_id else "the default VPC"
    )
    log(f"would launch one {opts.instance_type} builder from {base} in {opts.region}, {where}")
    files = [
        "build.sh",
        "ami-files.tar.gz",
        *([Path(opts.zab_wheel).name] if opts.zab_wheel else []),
    ]
    remote: dict[str, str] = {"AMI_FILES": "/home/ubuntu/ami-files.tar.gz"}
    if opts.zab_wheel:
        remote["ZAB_WHEEL"] = f"/home/ubuntu/{Path(opts.zab_wheel).name}"
    if opts.magma_dir:
        files += ["magma.tar.gz (licence files excluded)", "magmapassfile"]
        remote.update(
            MAGMA_TARBALL="/home/ubuntu/magma.tar.gz", MAGMA_PASSFILE="/home/ubuntu/magmapassfile"
        )
        if opts.magma_licence_setup:
            files.append("magma-licence-setup")
            remote["MAGMA_LICENCE_SETUP"] = "/home/ubuntu/magma-licence-setup"
        else:
            log("no --magma-licence-setup: Magma will run only if its licence file alone suffices")
    log("would copy " + ", ".join(files))
    log("would run " + env_command(build_knobs(opts, remote), "bash build.sh"))
    log(f"would create image {opts.name!r}, wait for it, terminate the builder, delete the group")
    if opts.smoke:
        log(
            f"would launch one {opts.smoke_instance_type} instance from the image, run the"
            " smoke-test checks, reboot, check again"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build (and smoke-test) the workshop AMI.")
    parser.add_argument("--profile", default="workshop", help="AWS profile (the workshop account)")
    parser.add_argument(
        "--account-id", required=True, help="the workshop AWS account; checked against STS first"
    )
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--name", default=f"nt-agents-2026-{time.strftime('%Y%m%d')}")
    parser.add_argument("--instance-type", default="m7a.xlarge", help="the builder's type")
    parser.add_argument(
        "--smoke-instance-type",
        default="m7a.2xlarge",
        help="the smoke instance's type: the fleet's (DECISIONS D23)",
    )
    parser.add_argument("--base-ami", help="builder image (default: latest Ubuntu 24.04 amd64)")
    parser.add_argument("--vpc-id", help="default: the default VPC (a public subnet of it is used)")
    parser.add_argument("--subnet-id", help="a subnet that assigns public IPs; implies its VPC")
    parser.add_argument("--my-ip", help="default: asked from checkip.amazonaws.com")
    parser.add_argument("--ssh-public-key", default="~/.ssh/id_ed25519.pub")
    parser.add_argument(
        "--ssh-key-file", help="private key for ssh -i (default: the agent/default)"
    )
    parser.add_argument("--codex-version", default="0.157.1")
    parser.add_argument("--claude-version", default="2.1.283")
    parser.add_argument(
        "--zab-tag", help="the release to install; required without --zab-wheel (else its version)"
    )
    parser.add_argument(
        "--zab-repo",
        default=os.environ.get("ZAB_REPO") or ZAB_REPO_URL,
        help="where --zab-tag installs from, an https:// or ssh:// URL (ZAB_REPO, else public)",
    )
    parser.add_argument(
        "--zab-wheel", help="wheel to install instead of the git tag (private repo)"
    )
    parser.add_argument("--pari-version", default="2.17.4")
    parser.add_argument("--magma-dir", help="a Magma installation tree, e.g. /opt/magma")
    parser.add_argument("--magma-passfile", help="Magma's licence file (magmapassfile)")
    parser.add_argument(
        "--magma-licence-setup",
        metavar="SCRIPT",
        help="optional: the deployer's script for any host-specific setup Magma's licence needs;"
        ' run as root at every boot (README.md, "Magma\'s licence")',
    )
    parser.add_argument("--with-sage", action="store_true")
    parser.add_argument("--sage-version", default="10.9")
    parser.add_argument("--smoke", action="store_true", help="also smoke-test the new image")
    parser.add_argument("--smoke-only", metavar="AMI", help="only smoke-test this image")
    parser.add_argument(
        "--smoke-warm",
        action="store_true",
        help="run the fleet's first-boot volume warm-up (timed) before the smoke checks",
    )
    parser.add_argument(
        "--assume-magma", action="store_true", help="--smoke-only: the image has Magma"
    )
    parser.add_argument(
        "--assume-sage", action="store_true", help="--smoke-only: the image has Sage"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the builder/smoke instance on failure"
    )
    parser.add_argument(
        "--tag",
        action="append",
        type=parse_tag,
        metavar="KEY=VALUE",
        help="scope tag for every resource, repeatable (default workshop=nt-agents-2026)",
    )
    parser.add_argument("--dry-run", action="store_true")
    opts = parser.parse_args(argv)

    def log(msg: str) -> None:
        print(msg, flush=True)  # a detached run's log must show progress as it happens

    if not re.fullmatch(r"\d{12}", opts.account_id):
        print("--account-id must be the 12-digit account number", file=sys.stderr)
        return 2
    if not opts.zab_wheel and not opts.zab_tag and not opts.smoke_only:
        print("--zab-tag is required without --zab-wheel: the release to install", file=sys.stderr)
        return 2
    if opts.zab_wheel and not opts.zab_tag:  # the wheel's own version
        found = re.match(r"zulip_agent_bridge-([^-]+)-", Path(opts.zab_wheel).name)
        opts.zab_tag = f"v{found.group(1)}" if found else ""
    if opts.zab_wheel:
        version = opts.zab_tag.lstrip("v")
        if f"-{version}-" not in Path(opts.zab_wheel).name:
            print(
                f"{opts.zab_wheel} is not a {opts.zab_tag} wheel; build it from the tag",
                file=sys.stderr,
            )
            return 2
        if not Path(opts.zab_wheel).is_file():
            print(f"{opts.zab_wheel}: no such wheel", file=sys.stderr)
            return 2
    if bool(opts.magma_dir) != bool(opts.magma_passfile):
        print("--magma-dir and --magma-passfile go together", file=sys.stderr)
        return 2
    if opts.magma_licence_setup and not opts.magma_dir:
        print("--magma-licence-setup needs --magma-dir and --magma-passfile", file=sys.stderr)
        return 2
    if opts.magma_dir:
        for p in (opts.magma_passfile, opts.magma_licence_setup):
            if p and not Path(p).is_file():
                print(f"{p}: no such file", file=sys.stderr)
                return 2
        if opts.magma_licence_setup:  # systemd runs it directly: it needs an interpreter line
            with open(opts.magma_licence_setup, "rb") as script:
                if script.read(2) != b"#!":
                    print(f"{opts.magma_licence_setup}: no #! line to run it by", file=sys.stderr)
                    return 2
        try:
            opts.magma_dir = str(validate_magma_dir(opts.magma_dir))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
    if not Path(opts.ssh_public_key).expanduser().is_file():
        print(f"{opts.ssh_public_key}: no such public key", file=sys.stderr)
        return 2
    if opts.dry_run:
        plan(opts, log)
        return 0
    ec2 = Ec2(opts.region, opts.profile)
    account, arn = ec2.identity()
    if account != opts.account_id:
        print(
            f"the {opts.profile} profile is account {account}, not {opts.account_id}; stopping",
            file=sys.stderr,
        )
        return 2
    log(f"account {account}, caller {arn}, region {opts.region}")
    fd, known_hosts = tempfile.mkstemp(prefix="zab-ami-known-hosts.")
    os.close(fd)
    ssh = Ssh(opts.ssh_key_file, known_hosts, log)
    res = Resources()
    ok = True

    def interrupted(signum: int, _frame: Any) -> None:
        raise Interrupted(signal.Signals(signum).name)

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        prepare(ec2, opts, res, log)
        if opts.smoke_only:
            ami, identity = opts.smoke_only, {"BUILDER_MACHINE_ID": "", "BUILDER_HOST_FP": ""}
        else:
            ami, identity = build(ec2, ssh, opts, res, log)
            print(f"AMI {ami}", flush=True)
            if res.instances:
                ec2.terminate(res.instances)
                ec2.wait_terminated(res.instances)
                res.instances = []
        if opts.smoke or opts.smoke_only:
            ok = smoke(ec2, ssh, opts, res, ami, identity, log)
            print("smoke test " + ("passed" if ok else "FAILED"))
            if not ok and opts.keep:
                keep_running(res, log)  # a failed check is what --keep is for (codex round 6)
    except Exception as exc:
        log(f"ERROR {type(exc).__name__}: {exc}")
        ok = False
        if opts.keep:
            keep_running(res, log)
    finally:
        # a second SIGTERM/SIGHUP must not cut the cleanup short (PM note on #56)
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        cleanup(ec2, res, log)
        os.unlink(known_hosts)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
