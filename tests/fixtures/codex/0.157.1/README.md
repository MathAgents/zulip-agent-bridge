Recorded on the Codex development machine with Codex 0.157.1 and the isolated `~/.codex-zabtest`
daemon, using a scratch repository containing only a README. Each JSONL row
is metadata or `{direction, t, message}`; `t` is elapsed seconds.

Scenarios: `simple`, `tool_use`, `approval` (decline), `steer`, `interrupt`,
and `resume` (second turn in the simple thread). The recorder also resumes
the thread and fetches summary history after each turn. Use
`zab dev record-codex --help`; explicit `ZAB_LIVE` and test home are required.
Raw captures remain in `/tmp`, not git. Each committed capture was processed
by `zab dev scrub --home /home/gpt --cwd /tmp/zab-codex-live-repo`, then
inspected. Home/cwd/host strings are placeholders; UUIDs are test-session IDs.

`uv run pytest tests/backends/test_codex_fixtures.py` validates every wire
record against the pinned generated schemas and replays the mapper offline.
