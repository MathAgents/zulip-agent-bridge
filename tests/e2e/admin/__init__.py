"""The admin end-to-end suite (docs/ADMIN.md §7; issue #356), on the rig of #263.

`run.py` builds or refreshes the rig (the test admin server, its role, the test workshops' bots
and channels), deploys the commit under test to the server with `deploy.sh`, runs the selected
scenarios, and always ends by terminating the fake participants and stopping the server.
`harness/` holds its parts. The scenarios are marked `admin_e2e` and run only with
`ZAB_LIVE=admin-e2e`; the harness's own tests run offline, with fakes, in every `uv run pytest`.
"""
