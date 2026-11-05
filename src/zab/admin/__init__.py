"""The workshop admin server: the approval bot's grammar and configuration, its audit log and
scrubbing (docs/ADMIN.md).

This package runs on a server that holds the fleet's keys, and only a release Drew signed runs
there (§1.6). Keep its import closure small: it imports the standard
library and only the `zab` modules that `tests/admin/test_admin_imports.py` allows, never the
bridge.
"""
