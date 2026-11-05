"""The `workshop/aws/`, `admin/`, `ami/` and `adminserver/` scripts are standalone (not in the
package): import them by path."""

import pathlib
import sys

import pytest

WORKSHOP = pathlib.Path(__file__).resolve().parents[2] / "workshop"
for sub in ("aws", "admin", "ami", "adminserver"):
    path = str(WORKSHOP / sub)
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture(autouse=True)
def no_github_token(monkeypatch):
    """No test here reaches the real GitHub: `final_sync.main` (and so the ending scripts'
    tests) checks repositories only with a token from the environment (#714)."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.fixture
def checker(monkeypatch):
    """The re-pack's socket, served as systemd serves `zab-archive-check.socket` (`checker.py`),
    and `collect_exports.py` pointed at it."""
    import collect_exports

    from .checker import Checker

    served = Checker()
    monkeypatch.setattr(collect_exports, "CHECKER_SOCKET", served.path)
    yield served
    served.close()
