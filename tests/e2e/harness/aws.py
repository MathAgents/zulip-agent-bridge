"""EC2 for the rig, through boto3, acting only on test infrastructure (`zab-role=test`).

`TestEc2` refuses to exist for a scope without `zab-role=test`, and it re-reads an instance's
tags before every stop, start or reboot: an instance that lacks `zab-role=test` or any of the
rig's scope tags raises `NotTestInfrastructure`, and nothing is sent to AWS for it. The fleet,
in another account, is out of reach anyway; Drew's other instances in this one are what the
check protects. boto3 is imported lazily (`uv run --with boto3`), as in `workshop/aws/`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from e2e.harness.config import TEST_ROLE

LIVE_STATES = ("pending", "running", "stopping", "stopped")


class NotTestInfrastructure(RuntimeError):
    """The harness was about to act on something that is not tagged as test infrastructure."""


def tags_of(resource: Mapping[str, Any]) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in resource.get("Tags", [])}


class TestEc2:
    __test__ = False  # not a pytest test class

    def __init__(
        self,
        region: str,
        scope: Mapping[str, str],
        *,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.scope = dict(scope)
        if self.scope.get(TEST_ROLE[0]) != TEST_ROLE[1]:
            raise NotTestInfrastructure(f"the scope {self.scope} lacks zab-role=test")
        if client is None:
            import boto3  # lazy: `uv run --with boto3`

            client = boto3.client("ec2", region_name=region)
        self.client, self.sleep, self.clock = client, sleep, clock

    def guard(self, instance: Mapping[str, Any]) -> Mapping[str, Any]:
        """The instance, if it carries every scope tag; else NotTestInfrastructure."""
        have = tags_of(instance)
        wrong = {k: have.get(k) for k, v in self.scope.items() if have.get(k) != v}
        if wrong:
            raise NotTestInfrastructure(
                f"{instance.get('InstanceId', '?')} is not test infrastructure: {wrong}"
            )
        return instance

    def find(self, tags: Mapping[str, str]) -> dict[str, Any]:
        """The one live instance carrying the scope and `tags` (the test VM by `participant`)."""
        wanted = {**self.scope, **tags}
        filters = [{"Name": f"tag:{k}", "Values": [v]} for k, v in wanted.items()]
        filters.append({"Name": "instance-state-name", "Values": list(LIVE_STATES)})
        pages = self.client.get_paginator("describe_instances").paginate(Filters=filters)
        found = [
            i
            for page in pages
            for r in page["Reservations"]
            for i in r["Instances"]
            if all(tags_of(i).get(k) == v for k, v in wanted.items())
        ]
        if len(found) != 1:
            ids = ", ".join(i["InstanceId"] for i in found) or "none"
            raise LookupError(f"expected one live instance tagged {wanted}, found {ids}")
        return dict(self.guard(found[0]))

    def describe(self, instance_id: str) -> dict[str, Any]:
        """The instance as EC2 has it now, checked by `guard`."""
        res = self.client.describe_instances(InstanceIds=[instance_id])
        instances = [i for r in res["Reservations"] for i in r["Instances"]]
        if len(instances) != 1:
            raise LookupError(f"{instance_id}: not found")
        if instances[0].get("InstanceId") != instance_id:  # never vouch for another instance
            raise NotTestInfrastructure(
                f"asked for {instance_id}, EC2 answered {instances[0].get('InstanceId')}"
            )
        return dict(self.guard(instances[0]))

    def state(self, instance_id: str) -> str:
        return str(self.describe(instance_id)["State"]["Name"])

    def public_ip(self, instance_id: str) -> str:
        return str(self.describe(instance_id).get("PublicIpAddress") or "")

    def stop(self, instance_id: str) -> None:
        self.describe(instance_id)  # guard first: nothing is sent for anything else
        self.client.stop_instances(InstanceIds=[instance_id])

    def start(self, instance_id: str) -> None:
        self.describe(instance_id)
        self.client.start_instances(InstanceIds=[instance_id])

    def reboot(self, instance_id: str) -> None:
        self.describe(instance_id)
        self.client.reboot_instances(InstanceIds=[instance_id])

    def wait_state(
        self, instance_id: str, target: str, *, timeout: float = 600, every: float = 10
    ) -> float:
        """Seconds until the instance is in `target`; TimeoutError after `timeout`."""
        start = self.clock()
        while True:
            state = self.state(instance_id)
            if state == target:
                return self.clock() - start
            if self.clock() - start >= timeout:
                raise TimeoutError(f"{instance_id} still {state} after {timeout:.0f} s")
            self.sleep(every)

    def wait_while(
        self, instance_id: str, state: str, *, timeout: float = 600, every: float = 10
    ) -> str:
        """The instance's first state read other than `state`; TimeoutError after `timeout`.
        For a transition that may end in more than one state: a VM `stopping` may read
        `pending` next, its `stopped` missed by the polls, when the test waker starts it again
        (#305)."""
        start = self.clock()
        while True:
            now = self.state(instance_id)
            if now != state:
                return now
            if self.clock() - start >= timeout:
                raise TimeoutError(f"{instance_id} still {state} after {timeout:.0f} s")
            self.sleep(every)
