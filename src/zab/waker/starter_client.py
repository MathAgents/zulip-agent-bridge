"""The waker's EC2 on the admin server: requests to zab-admin-starter (docs/ADMIN.md §1.2).

There the waker holds no AWS credential and no IMDS route (#329): it sends the starter, over its
Unix socket, the requests that `zab.admin.starter` documents. This client has the two methods
`Waker` calls on boto3's client, so `Waker` is the same in both modes, and it imports no AWS SDK.
"""

from __future__ import annotations

import json
import socket

#: The starter's socket (zab-admin-starter.socket).
SOCKET = "/run/zab-admin-starter/start.sock"
#: Bytes in one reply: about 30 per VM in a describe.
LIMIT = 1024 * 1024


class StarterError(Exception):
    """The starter refused or failed a request. `response` has botocore's shape, so `Waker`
    reads an EC2 error code (InsufficientInstanceCapacity) as it does from boto3."""

    def __init__(self, error: str, code: str | None = None) -> None:
        super().__init__(error)
        self.response = {"Error": {"Code": code or error}}


class StarterClient:
    def __init__(self, path: str, workshops: dict[str, str], timeout: float = 60.0) -> None:
        """`workshops`: each opted-in instance's workshop, whose fleet the starter checks it
        against."""
        self.path, self.workshops, self.timeout = path, workshops, timeout

    def request(self, payload: dict) -> dict:
        """The starter's answer to `payload`. It answers a peer it refuses (`forbidden`) without
        reading the request, and closes: before this sends, or after (#387). Either way its
        answer is read and reported, as for any other."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            sock.connect(self.path)
            try:
                sock.sendall(json.dumps(payload).encode() + b"\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # it closed before reading: its answer says why
            data = b""
            while not data.endswith(b"\n") and len(data) <= LIMIT:
                try:
                    chunk = sock.recv(65536)
                except ConnectionResetError:
                    chunk = b""  # it closed with the request unread: what came is its answer
                if not chunk:
                    break
                data += chunk
        try:
            reply = json.loads(data)
        except ValueError:
            reply = None
        if not isinstance(reply, dict):
            raise StarterError("no-reply")
        if reply.get("ok") is not True:
            error, code = reply.get("error"), reply.get("code")
            raise StarterError(str(error), code if isinstance(code, str) else None)
        return reply

    def describe_instances(self, *, Filters: list) -> dict:
        """`Waker.refresh`'s call: the instances of its workshop's tag filter that the starter
        finds in that workshop's fleet, each with that tag."""
        values = {f["Name"]: f["Values"] for f in Filters}
        (workshop,), ids = values["tag:workshop"], list(values["instance-id"])
        states = self.request({"op": "describe", "workshop": workshop, "instances": ids})["states"]
        if not isinstance(states, dict):
            raise StarterError("bad-reply")
        tag = {"Key": "workshop", "Value": workshop}
        instances = [
            {"InstanceId": i, "State": {"Name": states[i]}, "Tags": [tag]}
            for i in ids
            if isinstance(states.get(i), str)
        ]
        return {"Reservations": [{"Instances": instances}]}

    def start_instances(self, *, InstanceIds: list[str]) -> dict:
        if len(InstanceIds) != 1:
            raise ValueError("start one opted-in instance at a time")
        (instance,) = InstanceIds
        self.request({"op": "start", "workshop": self.workshops[instance], "instance": instance})
        return {}

    def close(self) -> None:
        pass
