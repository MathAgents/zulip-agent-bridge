# Codex 0.157.1 workshop settings

Verified on 2026-10-02 (issue #35; SPEC §8, §13.3). The workshop image writes these lines
into the agent's `~/.codex/config.toml` (`workshop/ami/build.sh`), and the first boot writes
them again where they are missing (`workshop/ami/README.md`, "First boot"):

```toml
# Root setting: put this before any [table] header.
check_for_update_on_startup = false

[mcp_servers.lmfdb]
url = "https://mcp.lmfdb.org/mcp"
```

The URL config selects streamable HTTP. This public server needs no token,
headers, OAuth login, or `type` key. Keep other existing config tables intact.

The release TUI checks for updates by default and can show an update prompt.
In the 0.157.1 source, `get_upgrade_version` returns immediately when
`check_for_update_on_startup` is false, before consulting its cached version or
scheduling the check. This setting controls the CLI's startup check and prompt;
it is not a package-manager version lock. Workshop provisioning must install
0.157.1 explicitly and avoid automatic package upgrades. By the deployment policy
(SPEC §8.3), upgrading Codex on the VMs requires a bridge release with a new validated
pin. zab does **not** enforce the daemon version (v0.2.15): RPC initialization
checks the home, and doctor only warns about the local CLI version. Do not rely
on a runtime rejection to enforce this policy. The maintainer may upgrade Claude Code
deliberately.

## Verification

Used the already-installed 0.157.1 release binary, not the current 0.159.0
launcher. Every live command used `CODEX_HOME=~/.codex-zabtest`; the app-server was
a dedicated stdio process created and closed for this check. No daemon under
`~/.codex` was started, stopped or contacted. Config was passed as `-c` overrides;
no home config or credential files were opened or modified by the test driver.

- `codex --version`: `codex-cli 0.157.1`.
- `codex mcp get lmfdb --json`: enabled, transport `streamable_http`, URL as above,
  no bearer-token environment variable or configured headers.
- `codex exec --json`: an actual `lmfdb/run_sql` call completed without error and
  returned the elliptic-curve label `11.a1`.
- Dedicated app-server: initialized, opened an ephemeral read-only thread, and
  `mcpServerStatus/list` reported LMFDB 1.28.1 connected with nine tools.
  `mcpServer/tool/call` invoked `count_rows` on `ec_curvedata` with
  `lmfdb_label = '11.a1'`; returned `count: 1`, `isError: false`.
- No Zulip messages or workshop infrastructure were involved. This verifies MCP
  configuration and tool use, not the full VM installation acceptance test.

## Sources

- [0.157.1 config schema](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/core/config.schema.json)
- [0.157.1 startup update check](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/tui/src/updates.rs)
- [OpenAI configuration reference](https://developers.openai.com/codex/config-reference/)
- [OpenAI MCP configuration](https://developers.openai.com/codex/mcp/)
