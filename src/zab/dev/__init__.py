"""`zab dev` developer tools (SPEC §13.1, §15.2), not for users.

Each subcommand is a module `zab.dev.<name>` exposing `main(argv: list[str]) -> int`; the CLI's
dispatcher calls it with the arguments after the subcommand. The modules: `claude_turn`,
`record_claude`, `scrub` and `record_codex`.
"""
