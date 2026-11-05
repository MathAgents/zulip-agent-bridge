"""End-to-end suite against the test VM (issue #263).

`harness/` holds the helpers (configuration, SSH, guarded AWS, the test users on Zulip, GitHub,
the file server, the report); `run.py` builds and deploys the wheel and runs the scenarios.
The scenarios are marked `e2e` and run only with `ZAB_LIVE=e2e`; the helpers' own tests run
offline, with fakes, in every `uv run pytest`.
"""
