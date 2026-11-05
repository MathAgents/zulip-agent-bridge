"""Fixtures of the admin suite (issue #356) and its report, as #263's conftest does for its own.

Scenarios are test functions marked `admin_e2e` and `admin_scenario("A10")`; one scenario may
have several parts. `ZAB_ADMIN_E2E_SCENARIOS` selects them (`run.py --scenarios`);
`ZAB_ADMIN_E2E_REPORT_JSON` names the rows' file; `ZAB_ADMIN_E2E_STATE` is the setup's
`Rigged`. Nothing opens until a scenario asks, and only under `ZAB_LIVE=admin-e2e` (the root
conftest skips `admin_e2e` tests otherwise). Pytest runs the files in name order, which is the
scenarios' order of dependence (`test_scenarios_1…` first).
"""

from __future__ import annotations

import functools
import inspect
import os
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from e2e.admin.harness import rig as rigs
from e2e.admin.harness.setup import Rigged
from e2e.conftest import noted, record
from e2e.harness.recorder import Recorder

_RECORDER = Recorder(
    titles={n: title for n, (_, title, _) in rigs.SCENARIOS.items()},
    labels={n: label for n, (label, _, _) in rigs.SCENARIOS.items()},
)


def _number(item: pytest.Item) -> int | None:
    marker = item.get_closest_marker("admin_scenario")
    return rigs.LABELS[str(marker.args[0]).lower()] if marker else None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    spec = os.environ.get("ZAB_ADMIN_E2E_SCENARIOS")
    parts: dict[int, list[str]] = {}
    for item in items:
        if (number := _number(item)) is not None:
            parts.setdefault(number, []).append(item.name)
    if spec:
        wanted = set(rigs.select(spec))
        drop = [i for i in items if (n := _number(i)) is not None and n not in wanted]
        if drop:
            config.hook.pytest_deselected(items=drop)
            items[:] = [i for i in items if i not in drop]
    for number in {n for item in items if (n := _number(item)) is not None}:
        _RECORDER.expect(number, parts[number])


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    rep = yield
    number = _number(item)
    if number is not None and item.get_closest_marker("admin_e2e"):
        record(_RECORDER, number, item.name, rep)
    return rep


def pytest_sessionfinish(session: pytest.Session) -> None:
    target = os.environ.get("ZAB_ADMIN_E2E_REPORT_JSON")
    if target and _RECORDER.rows:
        _RECORDER.report().write(Path(target))


# -------------------------------------------------------------------------------- fixtures

#: The scenario running now: the session's `server` notes its connect failures in its row.
_RUNNING: list[int] = []


@pytest.fixture(autouse=True)
def _running(request: pytest.FixtureRequest) -> Iterator[None]:
    number = _number(request.node)
    _RUNNING[:] = [] if number is None else [number]
    yield
    _RUNNING.clear()


def note_ssh(line: str) -> None:
    """A connect failure of the server's SSH: in the running scenario's notes (`noted`)."""
    noted(_RECORDER, _RUNNING[0] if _RUNNING else None)(line)


#: What each scenario established, for the ones after it (`needs`).
DONE: dict[str, Any] = {}


def needs(*what: str) -> None:
    """Skip, naming it, unless an earlier scenario established each of `what` (`DONE`)."""
    missing = [w for w in what if w not in DONE]
    if missing:
        pytest.skip(f"needs {', '.join(missing)} from an earlier scenario, not established")


def requires(*what: str) -> Callable[[Any], Any]:
    """A scenario that `needs(*what)` before anything else; `what` is kept on it as `requires`,
    so the offline tests can walk the chain of prerequisites."""

    def wrap(fn: Any) -> Any:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def run(*args: Any, **kwargs: Any) -> Any:
                needs(*what)
                return await fn(*args, **kwargs)
        else:

            @functools.wraps(fn)
            def run(*args: Any, **kwargs: Any) -> Any:
                needs(*what)
                return fn(*args, **kwargs)

        run.requires = what  # type: ignore[attr-defined]
        return run

    return wrap


@pytest.fixture
def scenario(request: pytest.FixtureRequest):
    number = _number(request.node)
    assert number is not None, "an admin e2e test needs @pytest.mark.admin_scenario(label)"
    return _RECORDER.row(number)


@pytest.fixture(scope="session")
def admin_rig() -> rigs.AdminRig:
    return rigs.load()


@pytest.fixture(scope="session")
def rigged() -> Rigged:
    path = os.environ.get("ZAB_ADMIN_E2E_STATE")
    if not path or not Path(path).is_file():
        pytest.fail("no ZAB_ADMIN_E2E_STATE: run the suite with tests/e2e/admin/run.py")
    return Rigged.read(Path(path))


@pytest.fixture(scope="session")
def server(rigged: Rigged, admin_rig: rigs.AdminRig):
    from e2e.admin.harness.server import Server

    return Server(rigged.ip, admin_rig, log=note_ssh)


@pytest.fixture(scope="session")
def aws(admin_rig: rigs.AdminRig):
    """The rig's AWS view, to check what the bots did: reads only (`ReadOnly` refuses any
    change before it is sent)."""
    import boto3

    from e2e.admin.harness.aws_rig import AwsRig
    from e2e.admin.harness.gate import Aws, ReadOnly

    ec2 = boto3.client("ec2", region_name=admin_rig.region)
    return AwsRig(Aws(ReadOnly(), ec2, boto3.client("iam")), admin_rig)


async def _user(path: Path, rigged: Rigged) -> AsyncIterator:
    """A test user who may post only in the rig's admin channels and DM only its admin bots
    (the dev org's `zab-dev*` channels only)."""
    from e2e.harness.zulip_user import TestUser

    ids = rigged.ids()
    user = TestUser.from_zuliprc(path)
    try:
        user.restrict(channels=ids.channels.values(), bots=[uid for uid, _ in ids.bots.values()])
        yield user
    finally:
        await user.close()


@pytest.fixture
async def tester(admin_rig: rigs.AdminRig, rigged: Rigged) -> AsyncIterator:
    """`zab-tester`: global admin and deployer."""
    async for user in _user(admin_rig.tester_zuliprc, rigged):
        yield user


@pytest.fixture
async def tester2(admin_rig: rigs.AdminRig, rigged: Rigged) -> AsyncIterator:
    """`zab-tester2`: organizer of zab-admin-e2e2 only; a stranger to the e2e bot."""
    async for user in _user(admin_rig.tester2_zuliprc, rigged):
        yield user


@pytest.fixture
def nonce() -> str:
    return f"{time.strftime('%H%M%S')}-{os.urandom(3).hex()}"
