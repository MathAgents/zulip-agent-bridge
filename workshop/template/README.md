# {{repo}}

Your workshop repository. It is not public:
you (once you accept its invitation) and the workshop's organizers can read it. Files your agents
link in their answers are snapshots that anyone with the link can read.

- Your agents work in a checkout of it on your workshop VM, `~/work/{{name}}`, and commit and push
  here as they go. Each Zulip topic works on its own branch, in its own worktree; ask an agent to
  merge a topic's work into `main` to share it with other topics.
- `AGENTS.md` holds the conventions both agents follow (`CLAUDE.md` points Claude Code at it).
  Edit it to suit your project.
- `HANDOVER.md` says where the work stands; `!handover` in a Zulip topic asks the agent to update
  it, commit and push.
