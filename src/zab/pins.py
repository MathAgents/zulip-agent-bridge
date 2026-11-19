"""The agent CLIs this release runs, and the workshops' default models (SPEC §5.3, §8.3;
docs/DECISIONS.md, Pins).

A release moves these, and a rollout applies them: `workshop/admin/push_config.py` reads this
file from the release it installs (never imported there, by `ast`: plain string literals only)
and brings each VM's Codex and Claude Code to them (`workshop/admin/agent_clis.py`). `zab
doctor` warns about another version, `workshop/ami/build_ami.py` builds images with them, and
`workshop/ami/build.sh`'s defaults are checked to agree (tests/workshop/test_agent_clis.py).
"""

#: `codex-package-x86_64-unknown-linux-musl.tar.gz` of the GitHub release `rust-v<version>`
CODEX_VERSION = "0.169.1"
CODEX_PACKAGE_SHA256 = "738df5b3182e2b858a48143f0c2fff288d8dd200330dadfe4c5c9f3eac7e2244"
#: The native build for linux-x64, its SHA-256 from that version's `manifest.json`
CLAUDE_VERSION = "2.1.311"
CLAUDE_SHA256 = "86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada"

#: The workshops' default models, at effort high (Drew, Nov 18, #1074): `push_config.py` writes
#: them into the config of the VMs it installs this release on (`release_models`), and only
#: then, since a release without them names none. A new session whose agent can't use its model
#: starts on the agent's own default (`zab.core.creation`, SPEC §7.9). Codex 0.169.1's catalog
#: has `gpt-6.2-astra`, 0.157.1's doesn't; Claude's is the full name, since Claude Code 2.1.311
#: resolves `fable` to `claude-fable-5-1` (DECISIONS, 2026-11-18)
CODEX_MODEL = "gpt-6.2-astra"
CLAUDE_MODEL = "claude-fable-5-2"
