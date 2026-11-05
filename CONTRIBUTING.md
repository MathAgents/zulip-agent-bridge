# Contributing to zab

zab is developed by Andrew V. Sutherland in a private repository, with coding agents doing much of the implementation and review. Each release is exported to the public repository as one commit, with its tag.

## References you will see

- **`#NNN`** in comments, commit messages, the changelog and the documents is an issue or pull request in the private development tracker. It is not an issue in this repository.
- **SPEC §N** and **DECISIONS Dn** point to [`docs/SPEC.md`](docs/SPEC.md) and [`docs/DECISIONS.md`](docs/DECISIONS.md), which are public.

## Issues and pull requests

- **Bugs and questions:** open an issue here. Include the zab version (`zab --version`), the backend (Codex or Claude Code) and its version, and what `zab doctor` prints, with tokens and addresses removed.
- **Security problems:** don't open a public issue. Report them privately through the repository's *Security and quality* tab (*Report a vulnerability*), as [`SECURITY.md`](SECURITY.md) describes.
- **Pull requests are welcome**, but they are not merged here. An accepted change is applied in the private repository, credited to you with a `Co-authored-by` line, and ships in the next release. Small, focused changes with tests are the easiest to take.
- **Only the maintainer merges a change that comes from a pull request here.** The maintainer reviews every such change and merges it into the private repository personally. No automated process or coding agent merges one, though agents may help to prepare and test it.
- By contributing, you agree that your contribution is licensed under the MIT license (see [`LICENSE`](LICENSE)).

## Development

```bash
uv sync                                     # .venv with the dev dependencies
uv run pytest -n auto -m 'not timing'       # the offline suite: no credentials, no network
uv run pytest -p no:xdist -m timing         # the CPU-bound tests, one at a time
uv run ruff check && uv run ruff format --check
```

Run them in a git clone of the repository: one offline test reads git's list of the files under `workshop/`, and fails in a tree without it, such as a release's source archive. Install [`age`](https://github.com/FiloSottile/age) (apt or brew) to run the backup tests; without it they skip.

- **Python 3.12 or newer**, with asyncio and type hints on public functions. Ruff must be clean.
- **Tests sit next to what they test** (`tests/<area>/`). Mark them `contract`, `integration`, `live` or `e2e` (SPEC §15.1), and `slow` (several seconds; a pull request's CI leaves them out) or `timing` (a CPU bound measured against a reference, run alone) where that applies; `pyproject.toml` describes each marker.
  - The live and end-to-end suites need real Zulip bots, test users, agent logins and, for end to end, a test VM ([SPEC §15.3](docs/SPEC.md#153-dev-environment)). They run only with `ZAB_LIVE` set, and never in parallel. The maintainer runs them before a release, on a setup that is not public; a pull request here needs only the offline suite.
  - The offline suite runs in parallel. A test must not share a file, port or environment variable with another.
- **No new runtime dependency** without a decision recorded in `docs/DECISIONS.md`.
- **Protocol facts come from the installed CLIs** (`codex app-server generate-json-schema`, `claude --help`) and from recorded fixtures, never from memory.

## Rules that are not negotiable

1. **No secrets in git:** no zuliprc files, API keys, tokens, `auth.json`, `.credentials.json`, rosters or SSH keys. Scrub recorded fixtures (`zab dev scrub`) and read the result before committing.
2. **zab never touches agent credential files or tokens.** It must not read, copy, print or modify `~/.codex/auth.json`, `~/.claude/.credentials.json` or the like (SPEC §1.2, §12). Tests of the upload denylist use decoy files in a temporary `HOME`.
3. **No shell strings.** Text from Zulip or agent output never reaches a shell: use argument lists (SPEC §10.6, §12.1).
4. **The bots obey only their allowlisted users** (DECISIONS D3). A change must not widen who can drive a bot.
