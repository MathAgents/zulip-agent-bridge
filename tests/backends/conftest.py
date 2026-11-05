"""The fake Claude account and workspace the backend tests run against (SPEC §15.2)."""

from __future__ import annotations

import pytest

from backends.claude_backend_helpers import FIXTURES
from zab.backends import claude as cb


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake account directory and a workspace; the fake's env comes from os.environ."""
    account = tmp_path / "account"
    account.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_FIXTURE", str(FIXTURES / "simple.jsonl"))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.delenv("ZAB_FAKE_CLAUDE_HANG", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-decoy-must-be-scrubbed")
    monkeypatch.setattr(cb, "MODEL_CHECK_DIR", str(tmp_path / "model-check"))  # not the real ~
    return {"account": account, "work": work, "log": tmp_path / "calls.jsonl"}
