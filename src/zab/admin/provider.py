"""A workshop's VMs from EC2, for the plans' target sets (docs/ADMIN.md §2.4, §6 item 2).

The start of §6's `Provider`: `targets` only, which the runner hashes for the verbs that act on
VMs (`actions.TARGETS`), at plan time and again at apply. It only reads: DescribeInstances in
the workshop's one region, through the instance role (§5). A VM is the workshop's as the starter
decides it (`starter.fleet_vm`): every scope tag, a non-blank `participant` tag, no `zab-admin`
tag, and the workshop's region.

The scripts find a target's VM in two ways: `launch.py`, `protect.py` and `adopt_spare.py` by its
`participant` tag; `power.py` (and `status.py`) by its address, the roster row's `ip` or a
spare's in `spares.json`, where a VM handed over by hand carries a spare's tag (Codex on #354).
So a target's material is every VM found either way: a new VM at the address, or one tagged
anew, changes the hash. `!retire` also reads `tagged`, the VMs tagged with the name exactly, which
`protect.py` and `teardown.py` select, and refuses a row for which the two differ (#559).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zab.admin import starter
from zab.admin.actions import Fleet
from zab.admin.config import Workshop
from zab.admin.fleet import MAX_FILE, scope_label

#: Every state but `terminated`: the scripts select those too.
STATES = ("pending", "running", "stopping", "stopped", "shutting-down")
_clients: dict[str, starter.Ec2] = {}
_lock = threading.Lock()


def ec2(region: str) -> starter.Ec2:
    """One EC2 client per region, made once: the runner calls `targets` in threads."""
    with _lock:
        if region not in _clients:
            boto3, config = starter.sdk()
            settings = config(
                connect_timeout=10, read_timeout=20, retries={"total_max_attempts": 2}
            )
            client = boto3.client("ec2", region_name=region, config=settings)
            _clients[region] = starter.Ec2(client)
        return _clients[region]


def _ips(vm: Mapping[str, Any]) -> set[str]:
    """Its public addresses, an Elastic IP's while it is stopped too (`aws_common.instance_ips`)."""
    ips = {vm.get("PublicIpAddress") or ""}
    for nic in vm.get("NetworkInterfaces") or []:
        ips.add((nic.get("Association") or {}).get("PublicIp") or "")
    return {ip for ip in ips if isinstance(ip, str)} - {""}


def spare_addresses(ws: Workshop) -> dict[str, str]:
    """Each spare's address in the workshop's `spares.json`, as `power.py` reads it."""
    path = Path(ws.out_dir) / "spares.json"
    if not path.exists():
        return {}
    with path.open("rb") as f:
        data = json.loads(f.read(MAX_FILE + 1).decode("utf-8-sig"))
    prefix = f"{scope_label(ws)}/{ws.aws.region}/"
    out = {}
    for key, entry in data.items():
        if key.startswith(prefix) and isinstance(entry, dict) and isinstance(entry.get("ip"), str):
            name = entry.get("name") or key[len(prefix) :]
            out[str(name)] = entry["ip"]
    return out


def _fleet_vms(fleet: Fleet, client: starter.Ec2 | None) -> list[Mapping[str, Any]]:
    """The workshop's VMs, as the starter decides them (module doc)."""
    ws = fleet.workshop
    filters = [{"Name": f"tag:{key}", "Values": [value]} for key, value in ws.tags]
    filters += [
        {"Name": "tag-key", "Values": ["participant"]},
        {"Name": "instance-state-name", "Values": list(STATES)},
    ]
    listed = (client or ec2(ws.aws.region)).describe(filters)
    return [vm for vm in listed if starter.fleet_vm(vm, ws)]


def tagged(
    fleet: Fleet, names: tuple[str, ...], client: starter.Ec2 | None = None
) -> dict[str, str]:
    """Each of `names` → the IDs of the workshop's VMs tagged `participant=<name>` exactly, as
    `protect.py --only` and `teardown.py --only` select them, sorted and comma-joined; a name
    with none is left out (#559)."""
    found = _fleet_vms(fleet, client)
    out = {}
    for name in names:
        ids = {str(vm.get("InstanceId")) for vm in found if _tag(vm) == name}
        if ids:
            out[name] = ",".join(sorted(ids))
    return out


def addressed(
    fleet: Fleet, names: tuple[str, ...], client: starter.Ec2 | None = None
) -> dict[str, str]:
    """Each of `names` with a roster address → the IDs of the workshop's VMs at it, as
    `power.py` and `push_config.py` reach them; a name with none is left out. `!retire`'s
    plan flags a row whose one VM is elsewhere (review of #564)."""
    found = _fleet_vms(fleet, client)
    where = {row.slug: row.ip for row in fleet.rows if row.ip}
    out = {}
    for name in names:
        ids = {str(vm.get("InstanceId")) for vm in found if where.get(name) in _ips(vm)}
        if ids:
            out[name] = ",".join(sorted(ids))
    return out


def targets(
    fleet: Fleet, names: tuple[str, ...], client: starter.Ec2 | None = None
) -> dict[str, str]:
    """Each of `names` (the plan's targets) → the IDs of the workshop's VMs that a script could
    act on for it, sorted and comma-joined: tagged with its name (in any case: `launch.py`'s
    legacy tags), or at its address. A target with none is left out."""
    found = _fleet_vms(fleet, client)
    where = {row.slug: row.ip for row in fleet.rows if row.ip} | spare_addresses(fleet.workshop)
    out = {}
    for name in names:
        ids = {
            str(vm.get("InstanceId"))
            for vm in found
            if _tag(vm).lower() == name or where.get(name) in _ips(vm)
        }
        if ids:
            out[name] = ",".join(sorted(ids))
    return out


def _tag(vm: Mapping[str, Any]) -> str:
    tags = {t.get("Key"): t.get("Value") for t in vm["Tags"] if isinstance(t, dict)}
    return str(tags.get("participant", "")).strip()
