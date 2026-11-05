from unittest.mock import Mock

import pytest

from zab.backends import claude
from zab.config import BackendConfig
from zab.core.backends import make_backend

pytestmark = pytest.mark.contract


def test_claude_factory_converts_common_config(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr(claude, "ClaudeBackend", constructor, raising=False)
    config = BackendConfig(
        kind="claude",
        accounts={"team": "/isolated"},
        default_account="team",
        defaults={"approval": "dontAsk"},
    )
    assert make_backend("claude-team", config) is constructor.return_value
    name, mapped = constructor.call_args.args
    assert name == "claude-team" and isinstance(mapped, claude.ClaudeConfig)
    assert mapped.config_dir(None) == "/isolated"
    assert mapped.defaults.approval == "dontAsk"
