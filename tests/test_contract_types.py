"""Smoke tests for the shared contract (src/zab/core/types.py)."""

import dataclasses
import inspect

import pytest

from zab.core import types as t

pytestmark = pytest.mark.contract


def test_events_are_keyword_only_and_frozen():
    ev = t.TurnCompleted(session_id="s", turn_id="u", status=t.TurnStatus.COMPLETED)
    assert ev.final_text is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.status = t.TurnStatus.FAILED  # type: ignore[misc]
    with pytest.raises(TypeError):
        t.TurnStarted("s", "u")  # type: ignore[misc]  # positional arguments are not allowed


def test_approval_defaults():
    ev = t.ApprovalRequested(
        session_id="s", turn_id="u", request_id="r", kind="command", summary="ls"
    )
    assert t.ApprovalDecision.DECLINE in ev.decisions


def test_enums_are_strings():
    assert t.ItemKind.COMMAND == "command"
    assert t.ErrorKind("usage_limit") is t.ErrorKind.USAGE_LIMIT


def test_mutable_defaults_are_independent():
    a, b = t.SessionSettings(), t.SessionSettings()
    a.extra["x"] = 1
    assert b.extra == {}


def test_published_snapshot_and_render_context_default():
    """rc6 refserve (issue #36, Drew Mon 04:01): a snapshot is optional and lazy; without a
    provider, rendering is unchanged."""
    snap = t.PublishedSnapshot(sha="a" * 40, url="https://files.mathagents.org/alice/" + "a" * 40)
    ref = t.SnapshotRef(sha="a" * 40, repo="alice")
    for frozen in (snap, ref):
        with pytest.raises(dataclasses.FrozenInstanceError):
            frozen.sha = "b"  # type: ignore[misc]
    field = next(f for f in dataclasses.fields(t.RenderContext) if f.name == "snapshot")
    assert field.default is None
    assert "Awaitable" in str(field.type)  # a provider, awaited on first use
    signature = inspect.signature(t.WorkspaceManager.publish_snapshot)
    assert signature.parameters["github_owner"].kind is inspect.Parameter.KEYWORD_ONLY


async def test_the_local_manager_takes_the_protocols_arguments(tmp_path):
    """codex on #132: the concrete manager accepts the contract's call (and, before the
    snapshot PR, answers None, which the contract allows)."""
    from zab.workspace.local import LocalWorkspace
    from zab.workspace.manager import LocalWorkspaceManager

    impl = inspect.signature(LocalWorkspaceManager.publish_snapshot)
    assert impl.parameters["github_owner"].kind is inspect.Parameter.KEYWORD_ONLY
    ws = LocalWorkspace(str(tmp_path))
    result = await LocalWorkspaceManager().publish_snapshot(
        ws, "d41a70c0-ee01-40fe-8692-a0491855d7ac/1", github_owner="MathAgents"
    )
    assert result is None or isinstance(result, t.SnapshotRef)


def test_git_identity_is_optional_and_off_by_default():
    spec = t.SessionSpec(cwd="/w", name="topic")
    assert spec.git_identity is None
    who = t.GitIdentity("Claude · alice", "claude-alice-bot@example.zulipchat.com")
    assert t.SessionSpec(cwd="/w", name="topic", git_identity=who).git_identity == who
    with pytest.raises(AttributeError):
        who.name = "someone else"  # frozen


def test_fork_is_opt_in_and_old_workspace_calls_still_bind():
    from zab.backends.claude import ClaudeBackend

    assert ClaudeBackend.capabilities.fork  # verified on 2.1.283 (#202), implemented
    assert not t.Capabilities(steer=True, approvals=True, live_attach=True, rename=True).fork
    signature = inspect.signature(t.WorkspaceManager.prepare)
    signature.bind(None, None, "shared", "topic", "subdir")
    base = signature.parameters["base_commit"]
    assert base.default is None and base.kind is inspect.Parameter.KEYWORD_ONLY


async def test_side_questions_are_opt_in():
    """`!btw` (#716): a backend answers side questions only once it says so."""
    from zab.backends.claude import ClaudeBackend

    assert ClaudeBackend.capabilities.side_question  # verified on 2.1.283 and 2.1.316
    plain = t.Capabilities(steer=True, approvals=True, live_attach=True, rename=True)
    assert not plain.side_question
    with pytest.raises(NotImplementedError, match="does not answer side questions"):
        await t.Backend.side_question(None, "native", "what?", timeout_s=1)


async def test_stopping_subagents_with_no_turn_is_opt_in():
    """`!stop` with no turn running (#847): a backend stops sub-agents ended turns left
    running only once it says so; Claude's have no such sub-agents (its turn holds them)."""
    from zab.backends.claude import ClaudeBackend
    from zab.backends.codex import CodexBackend

    assert not ClaudeBackend.capabilities.stop_subagents
    assert "stop_subagents" in CodexBackend.__dict__
    plain = t.Capabilities(steer=True, approvals=True, live_attach=True, rename=True)
    assert not plain.stop_subagents
    with pytest.raises(NotImplementedError, match="no sub-agents to stop"):
        await t.Backend.stop_subagents(None, "native")


async def test_unimplemented_contract_fork_refuses_without_starting_work():
    with pytest.raises(NotImplementedError, match="does not support"):
        await t.Backend.fork_session(None, "parent", t.SessionSpec(cwd="/decoy", name="child"))


def test_bot_is_optional_and_empty_by_default():
    """Issue #248: the bot's name, exported as ZAB_BOT for `zab history`."""
    assert t.SessionSpec(cwd="/w", name="topic").bot == ""
    assert t.SessionSpec(cwd="/w", name="topic", bot="claude-alice").bot == "claude-alice"


def test_session_is_optional_and_empty_by_default():
    """Issue #655: the zab session's id, exported as ZAB_SESSION for `zab job`."""
    assert t.SessionSpec(cwd="/w", name="topic").session == ""
    assert t.SessionSpec(cwd="/w", name="topic", session="s-1").session == "s-1"
