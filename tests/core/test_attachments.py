import pytest

from zab.core.attachments import upload_paths

pytestmark = pytest.mark.integration


def test_only_same_org_uploads_are_selected_and_deduplicated():
    text = (
        "[pic](/user_uploads/1/ab/plot.png) [csv](https://zulip.example/user_uploads/1/ab/a.csv) "
        "[duplicate](/user_uploads/1/ab/plot.png) "
        "[external](https://evil.example/user_uploads/1/ab/x) "
        "[broken](https://[broken]/user_uploads/1/a/file.csv) "
        "[escape](/user_uploads/1/%2e%2e/auth.json)"
    )
    assert upload_paths(text, "https://zulip.example") == (
        "user_uploads/1/ab/plot.png",
        "user_uploads/1/ab/a.csv",
    )
