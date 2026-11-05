# Conventions for mathematical computing

These instructions are read by both agents (Codex reads this file; Claude Code reads it through
`CLAUDE.md`). Edit them to suit your project.

- **Verify claims numerically.** Before stating a result, check it on examples and say what was
  checked (ranges, counts, the computation used). Separate what is proved from what is observed.
- **Prefer PARI/GP**, through `cypari2` in Python or `gp` directly, for number-theoretic
  computation; Sage and Magma are available too, where the image has them (`!tools` lists what
  is installed). Use exact arithmetic unless floating point is clearly enough, and say which.
- **Record experiments and their parameters**: scripts in the repository rather than one-off
  commands, with the inputs, bounds and random seeds that produced each result, so that every
  table and claim can be reproduced.
- **Commit small and often**, with messages that say what was learned, not only what changed.
  Push after each meaningful step.
- **Keep `HANDOVER.md` current**: the goal, the current state, the key decisions and why, the open
  problems and next steps, and the exact commands that reproduce the main results.

## Topics, worktrees and branches

- Each Zulip topic runs in its own git worktree, `<main checkout>/.zab/worktrees/<topic>-<6 hex>`,
  on its own branch `zab/<topic>-<6 hex>`, created from what the main checkout (`~/work/<repo>`)
  had checked out, normally `main`, when the topic started. Both agents of a topic share it.
- Commit on that branch and push it (`git push -u origin HEAD`).
- To make finished work visible to other and future topics: `git merge main` in your worktree
  and resolve any conflict there, then `git -C <main checkout> merge --ff-only <your branch>` and
  `git -C <main checkout> push origin main`. New worktrees start from the main checkout's `main`.
- To pick up work another topic merged: `git merge main`.
- Other topics' worktrees are under `<main checkout>/.zab/worktrees/`: read files there when the
  user refers to work from another topic, but never edit them. Gitignored files (build outputs,
  caches, `.lake/`, generated data) don't carry over to a new worktree: regenerate them, or read
  them from the other worktree.
