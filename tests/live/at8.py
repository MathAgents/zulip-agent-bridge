"""AT-8's channel, and why its live tests skip (#672, SPEC §15.3 and §17): one definition for
`test_claude_at` and `test_codex_m1`, which `test_at8_skipped` checks (#805)."""

PUBLIC = 639108  # zab-dev-public (AT-8): archived, never unarchived here (`AT8_SKIP`)
#: Why AT-8 never runs live.
AT8_SKIP = (
    "the dev organization's public channel, #zab-dev-public, is archived (Oct 26, 2026): "
    "a public channel is visible to every member of the organization. The offline tests cover "
    "the refusal with a fake public channel: tests/core/test_bridge.py::"
    "test_public_refusal_and_bad_options_do_not_create_session and tests/core/test_router.py::"
    "test_public_refusal_once_and_empty_topic; and the reading of Zulip's channel objects: "
    "tests/core/test_bridge.py::test_private_reads_zulips_channel_object"
)
