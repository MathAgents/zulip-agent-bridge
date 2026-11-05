import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message

from zab.core.git_identity import resolve
from zab.core.recovery import session_spec

pytestmark = pytest.mark.integration


def test_identity_auto_custom_off_and_missing_email():
    assert (
        resolve("auto", "Codex · alice", "codex-alice-bot@example.zulipchat.com").name
        == "Codex · alice"
    )
    assert resolve("Agent <bot@example.test>").email == "bot@example.test"
    assert resolve("off") is None
    assert resolve("auto", "Codex", "") is None
    with pytest.raises(ValueError):
        resolve("Agent\nInjected <bot@example.test>")


async def test_identity_reaches_create_recovery_and_status(bridge):
    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    assert backend.create_session.call_args.args[0].git_identity == b.git_identity
    assert session_spec(b.actors["native"]).git_identity == b.git_identity
    await user_message(b, fake, "!status")
    assert any(
        "git: Test Bot <test-bot@example.zulipchat.com>" in m["content"]
        for m in fake.messages.values()
    )


async def test_missing_auto_email_warns_without_aborting_bridge(bridge, caplog):
    b, fake, backend = bridge
    original = b.client.request

    async def missing_email(method, path, **kwargs):
        result = await original(method, path, **kwargs)
        if path == "users/me":
            result.pop("email", None)
        return result

    b.client.request = missing_email
    await b.initialize()
    assert b.git_identity is None
    assert "using user git config" in caplog.text
    await user_message(b, fake, "@**Test Bot** begin")
    assert backend.create_session.call_args.args[0].git_identity is None
    await user_message(b, fake, "!status")
    assert any("git: off (user git config)" in m["content"] for m in fake.messages.values())


async def test_recovery_persists_applied_identity(bridge):
    import json
    from dataclasses import replace

    from zab.core import types as t
    from zab.core.recovery import recover

    b, fake, backend = bridge
    await user_message(b, fake, "@**Test Bot** begin")
    actor = b.actors["native"]
    marker = {"name": b.git_identity.name, "email": b.git_identity.email}
    info = replace(
        backend.create_session.return_value,
        active_turn_id="turn1",
        settings=t.SessionSettings(extra={"applied_git_identity": marker}),
    )
    await recover(actor, info)
    stored = b.store.rows("sessions", id=actor.row["id"])[0]
    assert json.loads(stored["settings_json"])["extra"]["applied_git_identity"] == marker
    assert session_spec(actor).settings.extra["applied_git_identity"] == marker
