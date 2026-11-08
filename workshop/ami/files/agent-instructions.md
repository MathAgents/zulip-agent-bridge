# Workshop VM

You are working with a professional research mathematician at a workshop on AI agents in
mathematics. Be precise, separate what is proved from what is observed, cite the literature, and
skip the basics.

<!-- zab:tools -->

## Lean 4
- elan in ~/.elan, Lean v4.34.1. Mathlib v4.34.1 is prepared once for this whole VM, already
  built, in a shared read-only copy: Lean work here never downloads or builds Mathlib.
  - For scratch checks and experiments, use ~/lean/workspace: put the file there and run
    `lake env lean File.lean` from that directory (seconds). Don't create a Lean project for
    them.
  - Import narrowly (`import Mathlib.FieldTheory.Finite.Basic`, not `import Mathlib`): it loads
    faster.
  - When the formalization belongs in the repository, create the project with
    `lean-project DIR` (for example `lean-project lean` in this topic's worktree). It links the
    shared Mathlib, so the project takes megabytes and is ready at once. A Lean project already
    in the repository is usually linked when a topic starts. Before the first `lake` command in
    one, run `lean-project --repair DIR`: it links the project if it isn't, and does nothing if
    it is.
  - In such a project never run `lake new`, `lake update`, `lake cache get`,
    `lake exe cache get` or `lake clean`, and don't replace `.lake/packages` on your own: Mathlib
    is already there, and these either fail or stall. The lean4 plugin's advice to prime a cache
    or give each worktree its own `.lake/packages` doesn't apply on this VM; `lake build` and
    `lake env lean` are all you need.
  - Another Lean or Mathlib version needs a private copy, about 8 GB and several minutes:
    ask the participant first (`lean-project` says so when a project pins one). Check
    `df -h /` first; above 85% full, tell the participant. With their OK, `rm .lake/packages`
    (it removes only the link) and `lake update` give that project its own copy.
- Mind the memory. A Lean server is one `lake serve` and every process under it: its
  `lean --server` and one `lean --worker` per open file, 0.5 to 1.5 GB each with Mathlib.
  Claude's exit when its turn ends. Codex's stay running after the work stops, and zab stops
  each one that has been idle for 30 minutes, wherever it runs (`~/lean/workspace` included);
  the next Lean tool call starts a fresh one.
  - So don't stop Lean servers yourself: the topic's other agent, or another topic's, may be
    using one, and they look the same. Never stop `lean-lsp-mcp` (its thread's Lean tools need
    it) or a build (`lake build`, `lake env lean`).
  - Before heavy Lean work, check available memory (`free -g`); if it's low, tell the
    participant first. `!restart codex`, in a direct message to either bot, frees Codex's Lean
    servers at once, but interrupts a running Codex turn.
- MCP server `lean-lsp`: goals, diagnostics, hover, completions, local search. Its LeanSearch and
  Loogle tools are rate-limited, so try local search first.
- Plugin `lean4` (lean4-skills): Claude `/lean4:prove`, `/lean4:formalize`, ...; Codex `$lean4`.
  Its guardrails block `git reset --hard` and force pushes inside Lean projects.
- comparator checks that a solution proves exactly the challenge's statement, with only the
  permitted axioms. In a Lake project (Lean v4.34.1 only) with libraries `Challenge` (statements
  with `sorry`) and `Solution`, and a `config.json` such as
  `{"challenge_module": "Challenge", "solution_module": "Solution", "theorem_names": ["foo"],
  "permitted_axioms": ["propext", "Quot.sound", "Classical.choice"]}`, run `comparator-run`
  from the project root. Exit code 0 means accepted. Add `"enable_nanoda": true` for a second,
  independent kernel check.

## Other topics and earlier conversations
- When the participant refers to another topic or an earlier conversation, read it with
  `zab history` (see `zab history --help`) instead of guessing or relying on your own session
  logs: `zab history --list-topics --match TEXT` finds it, and
  `zab history --channel NAME --topic TOPIC` prints it, oldest first, with links.
- Never read `~/.config/zab/*.zuliprc` or call the Zulip API directly: those files are the
  bridge's credentials.

## During a turn: stay visible and responsive
- Until your final reply, all the participant sees in Zulip is the bridge's status message:
  your recent tool calls and your latest short messages.
- Say what you are doing. Before a step that may take more than about 10 seconds, write one
  short sentence: what you are about to do, and why. While you wait on a job or a sub-agent,
  write a note at least once a minute: never two waiting calls in a row without one.
- Answer mid-turn messages at once. The participant can write to you while you work; the
  message reaches you after your current tool call. Reply first, in one or two sentences:
  acknowledge it, answer the question ("what's the status?" gets the status), or say how the
  suggestion changes your plan. Then carry on. Never keep the answer to a mid-turn question
  for the final reply only, and if it still matters at the end, repeat it there: the status
  message's notes are gone once the turn ends.
- So never block for more than about 50 seconds in one call (next section): a longer call
  keeps the participant's message waiting until it ends. When a call can't be put in the
  background and may take longer (the first Lean tool call after the VM starts can take a
  minute or two), say so before you make it.

## Long-running commands
- Your turn ends when you send your final reply. Under zab, finishing a background job does
  not automatically start another turn or deliver a follow-up answer. Claude Code stops its
  background commands a few seconds after the turn ends. Never end a turn waiting for a job,
  and never promise to report back later.
- Run a command in the foreground only when it finishes within about 50 seconds. Start
  anything longer (a Lean build, a long computation, a search) so that it keeps running between
  your tool calls, and check it every 30 to 50 seconds, in calls of at most 50 seconds each, so
  that your notes come less than a minute apart:
  - Claude Code: start it in the background with its output in a log file, note its PID, and
    check it in bounded loops, for example
    `for i in $(seq 5); do kill -0 $PID 2>/dev/null || break; sleep 10; done; tail -n 5 job.log`
    (a bare `sleep` may be refused).
  - Codex: start it with `exec_command` and a short `yield_time_ms`, with its output also in a
    log file (for example `python3 -u run.py 2>&1 | tee job.log`), so that it keeps running in
    its session; check it with `write_stdin` (no input) and a `yield_time_ms` of at most 50000,
    which returns its new output. A job put in the background with `&` or `nohup` is stopped
    when the call that started it ends.
- After each check, before the next call, write a one-line progress note: the time elapsed and
  what is known so far. Never make two waiting calls in a row without a note between them, even
  when little has changed. Reply when the job is done. If it cannot finish in this turn, say so
  in your reply: what ran, where its log and partial results are, and that continuing needs a
  new request.

## Sub-agents
- For substantial work that needs thinking (a literature search, a proof attempt, an
  independent check of a result), you may delegate to a sub-agent that runs in the background,
  so that you stay free to give updates and answer the participant. A long computation is not
  such work: run it as a background command (previous section).
  - Claude Code: the Agent tool with `run_in_background: true`. You are told when it
    finishes, after your current tool call. Never run a long sub-agent in the foreground: you
    are blocked until it returns, and so is the participant's message.
  - Codex: `spawn_agent`, then `wait_agent` with a timeout of at most 50 seconds per call; a
    message from the participant ends the wait at once.
- Start one at a time, unless the participant asks for more. A sub-agent doesn't start
  sub-agents of its own: say so in its task.
- While it runs, keep checking on it as above, every 30 to 50 seconds until it is done. Write
  the note right after each check's result, before the next check: never two checks in a row
  without a note between them. A note is one line: how long it has run and what its log shows.
  Ask the sub-agent to write its progress to a log file of its own, and read the log's tail at
  each check (not Claude Code's output file for the sub-agent: that is its whole transcript).
  - Claude Code: check it with a bounded wait and its log's tail, for example
    `for i in $(seq 4); do sleep 10; done; tail -n 5 sub.log`. The message that starts a
    background sub-agent tells you not to report its results until you are told that it has
    finished. That is about its findings, not its progress: the notes are still due after every
    check, and a status question still gets an answer. Say what its log shows so far, and don't
    guess at what it will find.
- Don't end your turn while a sub-agent runs. Under zab, Claude's turn then lasts until the
  sub-agent is done, and the participant's messages wait for it; Codex's sub-agent finishes,
  but its answer never reaches the participant. If the turn must end before it is done (it
  can't finish, or the participant changes course), stop it first (Codex: `interrupt_agent`),
  and say in your reply what it had found. The participant's `!stop` ends your turn but not a
  Codex sub-agent: after a stopped turn, stop any sub-agent it left running.
- Sub-agents use up the participant's plan limits faster. Say so when you start one; `!usage`
  shows the plan's limits.

## Commits
- Your commits are made under your own name, for example `Claude · alice` with your bot's
  address: zab sets git's author and committer for you. Keep it that way: never pass
  `--author`, set `user.name`/`user.email`, or otherwise make your work look like the
  participant's, or theirs like yours. Rebasing or cherry-picking their commits keeps them as
  the author.

## Sharing results in Zulip
- Your replies are posted to Zulip. To share a file, save it inside the repository you are
  working in, then mention its relative path in backticks (`paper/main.pdf`) or as a markdown
  link. Files outside the repository are not linked.
- Images (PNG, JPEG, GIF) and PDFs are uploaded to Zulip and shown there. Make figures PNG (for
  example matplotlib `savefig("fig.png", dpi=150)`): Zulip offers an SVG file only as a
  download, so use SVG only inside HTML pages. Documents: compile to PDF and mention the .pdf.
- HTML pages (with their CSS, scripts and images) and other files open in the browser from
  the workshop's file server, in a snapshot of the repository as it was when you answered.
  Anyone who has one such link can open every file of that snapshot, not only the linked one.
- There, Markdown renders as on GitHub, with math, and source files (Python/Sage, Lean, PARI/GP,
  Magma, GAP, Macaulay2, Julia, C/C++, Rust, TeX, shell, JSON, YAML) are highlighted with line
  numbers: `src/check.gp:42`, or a link to `src/check.gp#L42`, opens at line 42. A Markdown
  README is fine for a directory of results: link the README itself (a directory's link shows
  only its file list). Results with tables, plots or many formulas still go in an HTML page,
  linked from the README.
- Never offer an answer's own link to the file server for sharing: it opens the whole
  snapshot. When the participant wants to share a file or a folder with someone, run
  `zab share PATH…` (see `zab share --help`) in the topic's checkout, and give the link it
  prints; a folder shares an HTML page with its images and scripts. Say that the link opens
  only what was shared, for anyone who has it, that it keeps working until it is revoked (after
  the VM ends, and even if the repository is deleted), and how to revoke it: ask an agent while
  the VM runs (`zab share --revoke NAME`), or the organizers after. `zab share --list` shows the
  links made so far.
- Present substantial results as a web page. When a result has tables, plots or many formulas,
  write a self-contained HTML file in the repository without being asked (MathJax or KaTeX from
  a CDN is fine), commit it and link it in your answer. Keep short answers in the reply itself.
- In your replies, write math as Zulip does: `$$...$$` for inline math, and a fenced code block
  with the language `math` for display math. In Markdown files, write it as GitHub does: `$...$`
  inline, and `$$...$$` for display.
- Write a time of day or a date and time, in your notes and your reply, as Zulip's
  `<time:2026-10-27T01:08:38Z>`, in UTC and not in backticks: each reader sees it in their own
  time zone. Never a bare clock time (`01:08:38`), and no epoch seconds in prose. Durations stay
  plain (`3 min 0 s`), and times inside quoted logs, command output or code stay exactly as
  they are.

## Remembering
- When the participant asks you to remember something across sessions, add it to the section
  "About me and standing instructions" at the end of `~/.codex/AGENTS.md` (this file; both
  agents read it at the start of every session). Don't use your own memory feature, and don't
  edit between the `zab:managed` markers: the organizers replace that part.
