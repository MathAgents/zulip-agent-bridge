# zab — Zulip Agent Bridge: specification

Status: describes v0.2.15.

This document is the source of truth for the bridge. Section numbers are stable: cite them in commits and pull requests ("SPEC §7.4"). `#NNN` is an issue or pull request of the private repository where zab is developed (CONTRIBUTING.md; D37). The words *must*, *should* and *may* are used in the RFC 2119 sense. *Phase 1* is the bridge as first built, for a workshop in October 2026, with the amendments since folded in; §18 lists what is deferred, and §16 the history.

Items marked **VERIFY** are protocol or server details that were taken from documentation and have not been confirmed against the versions in use. Check each one against the installed CLI or the Zulip server before relying on it, and record the result in `docs/DECISIONS.md`. A settled item gives its result here instead, and cites its row of the verification log there.

**Scope.** This document specifies the bridge (`zab run`), the sentinel (§13.5), the waker (§13.4) and the file links to turn snapshots (§10.7, §11.3). The rest of a workshop deployment has documents of its own: the admin server, its bots, console and starter in `docs/ADMIN.md`; the files host in `workshop/files/README.md`; the waker's installation in `workshop/waker/README.md`.

---

## 0. Summary

zab lets a person drive coding agents (OpenAI Codex and Claude Code) running on their own machines from Zulip:

- a Zulip **channel** is a project (a git repository);
- a **topic** is one agent session;
- in each topic the bot posts a **session card**, one **status message** per turn (edited as work proceeds), and the agent's **final answer** as an ordinary message.

zab runs on the agent's machine and accepts no connections. It talks to Zulip over HTTPS and, where file links are on (§10.7), pushes turn snapshots to GitHub with git over SSH. For personal use there is no server to run: Zulip is the only shared component. That is what makes it replicable: each person runs their own bridge on their own machines, and all their transcripts land in one searchable place.

zab has two kinds of users:

1. **People on their own machines** (personal use): a laptop, a workstation or a server, with one bot per agent per machine, each owned by its person (`zab init`, §5.3; §3's diagram).
2. **Workshops:** each participant has a cloud VM that runs both agents, which the organizers provision and run from an admin server (§0.1; `docs/ORGANIZERS.md` is their guide).

### 0.1 Deployments

A personal deployment is the one machine of §3. A workshop adds shared servers around its participants' VMs:

```
                         ┌──────────────── Zulip org ────────────────┐
                         │ participants' channels and DMs;           │
                         │ the organizers' and admins' channels      │
                         └─────▲─────────────────────────────▲───────┘
                         HTTPS │                       HTTPS │
┌──────────────────────────────┴──┐      ┌───────────────────┴───────────────────────┐
│ participant VM, one each        │ SSH  │ admin server (docs/ADMIN.md)              │
│  zab: one bot per agent         │◄─────┤  admin bots: plan → approval → apply      │
│  zab sentinel (§13.5)           │      │  collector → console (read-only web)      │
│  codex app-server, claude -p    │ EC2  │  waker (§13.4) → starter: StartInstances  │
│  idle stop: a timer, powers off │◄─────┤  EC2 API: launch, stop, start, retire     │
└──────────────┬──────────────────┘      └───────────────────┬───────────────────────┘
 git over SSH: │ turn snapshots (§10.7)                      │ repositories,
               ▼                                             ▼ invitations
┌────────────────────────────────────────────────────────────────────────────────────┐
│ GitHub <org>: the participants' repositories, and their refs/zab/turns/*           │
└──────────────────────────────────────────────▲─────────────────────────────────────┘
                              fetch, read-only │ token
┌──────────────────────────────────────────────┴─────────────────────────────────────┐
│ files host (workshop/files/README.md): https://files.<domain>/<repo>/<sha>/<path>  │
└────────────────────────────────────────────────────────────────────────────────────┘
```

- **The participant VM** runs the bridge with two bots, the participant's `Codex · <slug>` and `Claude · <slug>`; the sentinel, which answers `!vm` and `!restart` while the bridge is down (§13.5); the Codex daemon (§8.1); and idle stop, which powers the VM off when nobody has used it for a while (`workshop/admin/README.md` §8).
- **The files host** serves the denylist-filtered turn snapshots that the VMs push, so that a link in an answer opens for anyone who has it (§10.7).
- **The admin server** runs one admin bot per workshop and a global one, which change the fleet only after a person approves a dry-run plan; the collector and the read-only console; and the waker, which starts a stopped VM when its owner writes to one of its bots, through the starter, which holds the right to start it that the waker itself lacks (§13.4).

The MathAgents workshops, for example, run all of these. Personal use needs none of them, and a VM's waker is opt-in (`!waker on`, ORGANIZERS §4.2).

## 1. Goals and non-goals

### 1.1 Goals

- **G1 Control.** Start, continue, steer and stop Codex and Claude Code sessions from Zulip (web, desktop, mobile), one session per topic.
- **G2 Readable record.** Final answers are normal messages; tool activity is collapsed into one edited status message per turn; math renders; file references become working links; images and PDFs show inline.
- **G3 Durability.** Sessions survive bridge restarts, topic renames and moves, and loss of the Zulip event queue.
- **G4 Safe defaults.** Only a bot's owner can drive it; sessions run only in private channels; agent output cannot ping everyone and has known secret formats redacted.
- **G5 Replicable.** One Python package, one config file, `zab init` and `zab doctor`; installable on a fresh Ubuntu VM in under ten minutes; runs in the foreground on macOS.
- **G6 Codex co-presence.** A Codex thread can be driven from Zulip and from `codex` in a terminal at the same time.

### 1.2 Non-goals for Phase 1

- Driving an agent as anyone but its allowed users (§2, "owner"). Several agents, and several people with their agents, may share a topic (§4.5: shared topics, peer context, multi-party channels); what the others write reaches an agent only as quoted context.
- Token-by-token streaming into Zulip.
- A web UI for driving the agents (D1). The files host serves turn snapshots read-only (§10.7), and the admin server's console only shows a fleet (ADMIN §4).
- Live co-presence with an interactive Claude Code terminal session.
- Handling credentials of any kind. The bridge never reads, copies, stores or relays agent credentials.

Phase 1b (after the workshop, M4 in §16) adds backfill of historical sessions, automatic mirroring of terminal-started Codex threads, macOS service install, and moving a Claude session between accounts.

## 2. Glossary

| Term | Meaning |
|---|---|
| **bridge** | One running `zab` process on one machine, belonging to one person. |
| **owner** | Short for the bot's *allowed users*: `allow_user_ids` if it is non-empty, otherwise the bot's Zulip owner (`bot_owner_id`). The allowlist **replaces** the Zulip owner; it does not add to it (§5.2). Reactions and commands count from any allowed user. Notifications mention the allowed user who sent the message that triggered the turn, never `bot_owner_id` as such. |
| **active session** | A session in state `idle`, `running` or `awaiting_approval`. Recovery (§7.8) applies only to active sessions. Reconciliation fetches (§7.8.2) are further limited to sessions with a running turn or with activity in the last 7 days; any other session is refreshed when its next message arrives. Activity is `sessions.updated_at`, which moves only when the session's stored state changes (a turn, a setting); reopening an unchanged session at startup does not move it (issue #443). |
| **bot** | A Zulip *generic bot* used by exactly one bridge, bound to exactly one backend. Naming: `Codex · laptop`, `Claude · alice`. |
| **backend** | An adapter for one agent CLI installation: `codex` (app-server) or `claude` (headless CLI). |
| **native id** | The agent's own session identifier: a Codex thread id, or a Claude session UUID. |
| **session** | A binding between one topic and one native session, stored by the bridge. |
| **card** | The bot's first message in a session topic, an anchor posted once and never edited (Appendix B.1); `!session` shows the session's details. Its *location* defines the topic the session lives in (§4.4). |
| **turn** | One request/response cycle of the agent. |
| **status message** | The single bot message per turn that is edited to show progress, then finalized. |
| **workspace** | The directory a session runs in: the project checkout, or a git worktree of it (§10). |
| **project** | A local git checkout that a channel maps to (§5.4). |

## 3. Architecture

```
 Zulip Cloud org                           agent machine (a laptop, a server, a workshop VM)
┌────────────────────┐  HTTPS, outbound  ┌───────────────────────────────────────────────────────┐
│ channel = project  │◄─────────────────►│ zab (one asyncio process, runs as the agent's user)    │
│ topic   = session  │  event queue      │  ├─ ZulipClient + Poster, one per bot                   │
│ bots: Codex·<m>,   │  (long poll),     │  ├─ Router → SessionActor (one per session)             │
│       Claude·<m>   │  REST calls       │  ├─ Store (SQLite)                                       │
└────────────────────┘                   │  ├─ Renderer (math, links, uploads, redaction)          │
                                         │  ├─ CodexBackend ── unix socket ──► codex app-server     │
                                         │  │                                  (daemon) ◄── codex TUI│
                                         │  └─ ClaudeBackend ── one process per turn ──► claude -p  │
                                         └───────────────────────────────────────────────────────┘
```

### 3.1 Process model

A single asyncio process contains:

- one **listener** task per bot, which long-polls the Zulip event queue;
- one **poster** per bot, which serializes outbound Zulip calls, applies rate limits and coalesces edits (§4.10);
- one **event pump** task per backend, which reads backend events and dispatches them by native id;
- one **SessionActor** per active session: a task with an inbox queue that serializes everything happening to that session (user messages, commands, reactions, backend events).

Nothing on the backend side (for example, the Codex socket reader) may ever wait on a Zulip call. Backend readers hand events to actors through queues.

### 3.2 Module layout

The main modules:

```
src/zab/
  cli.py, commands/      entry point and the `zab` commands: init, doctor, run, status, service (§13)
  config.py              TOML loading and validation (§5)
  core/types.py          the contract between the core and the backends (D17)
  core/store.py          SQLite (§6)
  core/bridge.py         one listener per bot, startup and wiring (§7.1)
  core/router.py         routing rules (§4.5)
  core/addressing.py     how a message addresses bots, for the router and the waker (§4.5, #713)
  core/command_text.py   commands as people type them: `!Login`, `` `!login` `` (§4.6, #709)
  core/session.py        SessionActor and the turn lifecycle (§7)
  core/creation.py, attach.py, fork.py
                         new sessions, `!attach` and `!fork` (§7.2, §8.7, §9.8, §7.11)
  core/catchup.py, recovery.py, detached.py
                         startup replay and recovery (§7.8)
  core/presentation.py, commands.py, session_details.py, settings.py, usage.py, vm.py, software.py
                         the card, the status message and the commands' replies (§4.6, §4.9)
  core/peer_context.py   peer and multi-party context (§4.5)
  core/login.py          DM `!login` (§4.6)
  core/watchdog.py       listener liveness for systemd (§13.3)
  core/lean_reaper.py    stops the idle Lean servers Codex threads hold (§7.12)
  core/logging.py, metrics.py
                         logs and metrics (§14)
  zulip/client.py        REST and event queue over httpx (§4.10)
  zulip/poster.py, budget.py
                         rate limiting, priorities, edit coalescing (§4.10)
  backends/codex*.py     the Codex backend (§8)
  backends/claude*.py    the Claude backend (§9)
  workspace/             WorkspaceManager, GitContext, snapshots, diffs, the upload denylist (§10, §11.4)
  render/                code protection, math, links, mentions, redaction, splitting, diffs, bars (§11)
  tools/                 `zab setup-repo`, `zab preflight`, `zab history`, `zab share`, `zab sentinel` (§5.6, §10.7, §13)
  dev/                   `zab dev`: claude-turn, record-claude, record-codex, scrub (§13.1, §15.2)
  testing/               fake Zulip, fake Codex app-server, fake `claude` executable (§15.2)
  waker/                 the waker, a separate `waker` program, never imported by the bridge (§13.4)
  admin/                 the admin server: its bots, starter, collector and console (docs/ADMIN.md)
workshop/                image build, launch and teardown, provisioning, the files host (workshop/*/README.md)
```

### 3.3 Technology

- Python ≥ 3.12 with `asyncio`.
- Runtime dependencies: `httpx` (Zulip REST) and `websockets` (Codex socket). The standard library supplies `tomllib`, `sqlite3`, `dataclasses` and `logging`.
- Development dependencies: `pytest`, `pytest-asyncio` and `ruff`.
- Any new runtime dependency needs an entry in `docs/DECISIONS.md`.
- Packaging with `hatchling`; console script `zab`.

## 4. The Zulip model

### 4.1 Organization

The organization is a single Zulip Cloud org on the Standard plan (academic sponsorship). The admin checklist is BOOTSTRAP §5.7. Settings the bridge depends on:

- **Message editing** allowed, with no time limit on editing content. Status messages are edited throughout turns that can last hours. If an edit is refused, the bridge must fall back to posting a new status message.
- **Members may create generic bots.**
- **Members may move messages between topics**, so owners can reorganize sessions after the fact.
- **Topics are required.** "Default *general chat* topic configuration for channels" is set so that *general chat* is not allowed. Topic = session, and the empty topic has no name to bind to. The bridge must cope anyway (§4.5 step 7).

### 4.2 Bots

- Each bot is a Zulip **generic bot**, owned by the person it serves. Its `zuliprc` holds its credentials.
- A bot belongs to exactly one bridge and one backend. One bridge may run several bots: a workshop VM runs `Codex · alice` and `Claude · alice`.
- Two running bridges must never share a bot. `zab run` must refuse to start if another process holds the per-bot lock file (§6.1).
- Mentioning a bot selects the agent. That keeps addressing obvious for novices and extends naturally to multi-agent topics in Phase 2.

### 4.3 Channels and projects

- Before moving a topic, subscribe its bot(s) to the destination channel, including a public channel; otherwise the bridge cannot receive its events or reliably follow the session card's location.
- A channel maps to a project checkout on the bridge's machine (§5.4). Machine-specific paths live in the bridge config, never in Zulip.
- The channel description should contain a line `repo: <owner>/<name>` for humans and for auto-clone (§5.4).
- Sessions run only in **private** channels. In a public or web-public channel the bridge replies once, "sessions are disabled in public channels", and does nothing else, unless `allow_public_channels = true`.
- To publish a finished session, move its topic into a public channel. When `allow_public_channels = false`, the bridge treats a session whose card now sits in a public channel as read-only and does not act on it. With `true`, the session stays live; owner-only triggering and multi-party mention rules still apply. The workshop VMs' configuration permits public channels (`push_config.py`, §5.3); `zab init --profile workshop` does not.

### 4.4 Topics and sessions

- A session is bound to its **card message id**. The session's current channel and topic are the card's current location.
- The store caches that location and updates it from `update_message` events (§4.10). After any event-queue loss, the bridge must refresh the location of each active session's card with `GET /messages/{card_id}` before its catch-up. On startup it confirms a card before anything depends on its location (issue #443):
  - before the catch-up replay, the cards of sessions that can post before a message reaches them (a turn to recover, held or pending input, a pending approval);
  - before routing a message in a topic, the cards of the sessions stored at that topic; if none is left there, it searches the topic, once, for this bot's messages (`GET /messages`, narrow of channel, topic and sender), and confirms any card found, moved there while the bridge was down;
  - before a session posts in its topic, its own card (a turn the backend reports for a session nothing else has reached, say);
  - in the background housekeeping after the replay (§7.1), the cards of recent sessions (§2 "active session"). Older ones are confirmed when a message reaches them, or when they post.
- A confirmation moves the session with its card, detaches it if the card is inaccessible (400, 403, 404), and applies the channel's privacy (§4.5). A live `update_message` for the card handled while it was being fetched is newer than the fetch: the fetch is dropped and repeated.
- The search costs one request for a topic without this bot's messages; a topic with a long history of them (after `!detach`, say) pages through it, once per process. If Zulip refuses the search outright, every unconfirmed card is confirmed then and there, about a request per session: the old start-up cost, in one message's handling. It needs a refused channel and topic narrow for a channel the message came from, so it is not expected.
- Consequences:
  - Renaming a topic, resolving it (Zulip adds the `✔ ` prefix), or moving it with "move all messages" keeps the session attached.
  - Moving only part of a topic leaves the session with whichever part contains the card. The other part is an ordinary topic without a session.
- A topic may hold at most one session per bot. A topic containing cards from two different bots is **shared** (§4.5).
- The card begins with the literal marker line `**zab session**`, so other bridges can recognize it.
- For Codex, the topic name (without the `✔ ` prefix) is also the thread name (`thread/name/set`, §8.4).

### 4.5 Routing rules

For each `message` event received by bot **B** with sender **S**, channel **C**, topic **T** and content **M**, the router applies these rules in order:

1. **Ignore** the message if S is B, if S is any bot, or if S is not in B's allowlist (§5.2).
2. **Direct message** to B: handle DM control commands only (§4.6), then stop.
3. **Public channel** with `allow_public_channels = false`: if B was mentioned, reply once per topic with the refusal (§4.3), then stop.
4. Normalize: let **M′** be M with its leading list of bot mentions, completed or typed ("Mention detection", below), and the whitespace after them removed. M′ is a **command** if it starts with `!`, and an **aside** if it starts with `//`. Nothing about a topic's earlier messages decides who gets M (#713): before it, each bot kept the topic's last addressees (`topic_addressing:<bot>:<stream>:<topic>`, which nothing reads now).
5. Look up B's session **X** whose card is currently in (C, T). Detached sessions are excluded.
6. If X exists:
   1. An aside is ignored; it stays in the transcript for humans.
   2. If M mentions no bot, it goes to each of the VM's bots, whatever earlier messages in T addressed ("A message without a mention", below): deliver it to X. This also applies to commands such as `!stop`. In a multi-party channel, it goes to none.
   3. If M mentions another bot and does not mention B, ignore M.
   4. A command is a topic command for X (§4.6).
   5. Otherwise deliver M′ to X (§7.3).
7. If no session exists and B is mentioned, or M mentions no bot and goes to B ("A message without a mention", below):
   1. If T is the empty topic (*general chat*), reply once asking for a named topic, and stop.
   2. If M′ is `!help`, `!attach` with or without an ID, `!vm` or `!tools`, handle it without creating a session (§4.6, §13.5). `!fork` gets the answer that a mention with a prompt starts a session (§7.11).
   3. Any other command is refused, "No session here: write a prompt to start one; !help lists the commands.", except `!new`. Without a mention, only where no other bot of the VM has a session in T, and from one bot (§4.6).
   4. For `!new [opts] <prompt>` or a plain prompt: resolve the project for C (§5.4) and create a session (§7.2). If no project resolves, reply with the fix ("add this channel to `[[projects]]` in your zab config") and stop.
8. Otherwise ignore M.

Before dropping an unauthorized **one-to-one DM**, the bridge may send a human sender one plain owner/bot-finding hint per bot per hour, persisted across restarts. Resolve the owner from the configured allowlist. Never reply to bots or unresolved identities, never in channels or group DMs, never echo/store/log the incoming text, and never start a login or session (issue #35).

Mention detection:

- B counts as mentioned if the message event's `flags` contains `"mentioned"` and M contains `@**<B's full name>**` or `@**<B's full name>|<B's user id>**`, a **completed mention**: Zulip uses the second form when two users share a name. B also counts as mentioned if M contains a **typed mention** of B's kind (below). Wildcard and group mentions do not count (**VERIFY** which flags Zulip sets for them). Silent mentions (`@_**Name**`) do not count.
- **A typed mention (#713)** is `@claude` or `@codex` typed out, without Zulip's typeahead, which leaves it plain text and sets no `mentioned` flag. It is a mention of the sender's bot of that kind: of B when B's backend kind is that one, so B doesn't require the flag for it; and, for B, of each other bot of the VM (the bridges of B's process) of that kind whose allowlist has the sender. Case doesn't matter (`@Claude`, `@CODEX`). The `@` follows the start of M or a character that isn't a letter, digit, `_`, `.`, `@` or `*`, and the name is followed by the end of M or a character that isn't a letter, digit or `_`: `@claude,`, `@claude:` and `@claude's` count, as do `@claude-code` and `@claude/@codex`; `@claudette` and `x@claude.ai` don't, nor does a silent mention, nor an `@` in a link (`https://medium.com/@claude/…`, `github.com/@codex`: one with a scheme, or a domain name and a path). It counts anywhere in M, as a completed mention does, and wherever one does: the routing rules, the message's audience, peer context, commands and asides. A typed mention of a kind of which the VM has no bot that allows the sender still names a bot, so a bot of the other kind stays out (rule 6.3). In a channel two VMs share, each bot still answers only its own allowlist, so Alice's `@claude` reaches Alice's Claude; a sender on the allowlists of two bots of one kind there reaches both. Other typed names (`@gpt`) are not mentions. The waker reads typed mentions too (§13.4), and so do DM commands (§4.6).
- **Where a mention counts** (`zab.core.addressing.visible_text`): outside code (inline code and code fences), not escaped (`\@claude`), and not quoted: a ```` ```quote ```` fence, with its body, and a blockquote, from a line that starts with `>` to the next blank line, as Zulip shows it. Quoted text is someone's earlier words, and Zulip silences the mentions in it, so the quoted bot never gets the flag for one; the other bots read a quote the same way since #713, completed mentions included. Before, a quoted `@**Codex · …**` made Claude ignore a message that Codex ignored too, without the flag. A spoiler's body counts.
- The leading address list takes a typed mention when whitespace, `,`, `:` or the end follows it, so `@claude !stop` is a command and `@codex // note` an aside; `@claude's idea` keeps its words. A typed mention later in M stays in the prompt as written.
- With `bridge.multi_party=true` (default), correct a leading mention of a foreign
  same-kind bot for an allowlisted owner: when B is absent from the visible mentions,
  a bot in the leading address list named `Claude · …` / `Codex · …` with B's kind
  counts as mentioning B. Strip the address list and route M
  normally. Configured bridge peers are never foreign; `bot_owner_id` is not used.
  References later in the prose, code/escaped/silent mentions, other-kind bots and
  non-workshop names are not corrected. The switch also disables this correction
  in single-human channels. Mentioning foreign Claude and Codex addresses both of
  the owner's agents; every bot still rejects non-owner triggers.
- Bot names should be unique in the org; the workshop names them by the participant's slug (`workshop/admin/README.md` §1).
- Before forwarding M′ to the agent, remove any remaining completed mention of B from it, except in a quote, which arrives as it was, together with a leading `!new …` options line (§4.6).

**A message without a mention (#687, #713).** In a single-human channel (below), an allowed
user's message that mentions no bot, completed or typed, goes to each of the VM's bots (the
bridges of B's process) that is subscribed to the channel (the membership read for the
multi-party rule, below) and whose allowlist has the sender, whatever earlier messages in the
topic addressed: each takes it as if M mentioned them all, B's peers being the others. So a mention
talks to the agents it names, and a message that names none to all of them; no hidden state
decides. A message that mentions another bot, of the VM or not (another participant's, an
integration's), is that bot's (rule 6.3). A bot with a session in the topic delivers M to it
(rule 6), with the co-addressed note and peer context; one without starts a session, as a new
topic's first message does (rule 7), its commands included (§4.6 lists what each command does
then). That holds wherever the topic came from: a `!fork`'s topic (§7.11), one a card moved
into or out of, one where another bot attached a session. After `!detach` in the topic, that
bot waits for a mention there (§4.6); the others don't. A bot without a session in the topic
whose agent is known to be signed out stays out, silently, and the others leave it out of
their peers, unless none of the VM's bots that M goes to is signed in: then each answers, with
its sign-in hint. A bot with a session there is not asked: its turn says what it needs. Known
means the backend's answer (`signed_in`): Codex asks its daemon (`account/read`, no token
refresh), waiting for its own daemon to connect within 15 seconds of the bridge's start (a
wake, a restart) until a check ends unknown, and otherwise, as for another bot's, 1 second;
Claude has the last `claude auth status` (from `!vm`, `!status` or `!login`), a "not signed
in" for 10 minutes, and never runs a new one, so a Claude not signed in may answer with its
sign-in message. Unknown, or no answer in time, counts as signed in. The checks run only for a
message without a mention, and only for the bots that would start a session; each bridge
reuses an answer, unknown included, for `SIGNED_IN_CACHE_S` (5 seconds), so that a catch-up's
messages wait at most once for a Codex daemon that is reconnecting, and drops it when that
bot's `!login` ends, so that a new sign-in counts at once. In a multi-party channel, or with
unknown membership, M goes to no bot. With `bridge.multi_party=false`, M goes to the bots that
have a session in the topic and are subscribed to the channel, and starts none: a new session
needs a mention. The channel's membership is read for that too (`channel_members`, the cache
of the multi-party rule below), switch or not, and read again after Zulip drops the event
queue, whose subscription events went with it.

Shared topics: the `shared` flag may remain for display, but never gates routing. A mention of both bots addresses both, and so does a message without a mention; a mention of one addresses that one alone, and the next message without a mention both again. Session creation order does not change this rule. Before delivering a co-addressed prompt, prepend a short note naming the other agents and telling this agent to answer it itself. Keep topic commands and `!new` options intact. Bot messages never select recipients or start turns.

Idempotency: every message id a bot processes is recorded in the store for that bot (§6.2), and a bot never processes a message id twice.

**Peer visibility (R3).** On a user prompt addressed to multiple agents, by mentions
or without one, prepend bounded topic context that this agent
missed since its previous accepted input (a turn or steer). Include output from
the other addressed bots and allowed-user messages whose recorded audience was
only those peers. Exclude bridge cards and status/approval messages. Label the
preamble as untrusted quoted reference material, not instructions; the current
user request follows it. Bot messages themselves never start turns.

Fetch at most the 100 topic messages immediately before the newest triggering
user message. Limit the preamble to 12,000 characters and each excerpt to 2,000
characters, shrinking further for JSON escaping. Prefer recent excerpts, link
each original and mark truncation/omission. The cutoff excludes answers to the
current request: a later “compare your answers” prompt can see prior answers.
Persist the cursor only after backend acceptance, atomically with clearing held
input. If the optional history fetch fails, retain the cursor and deliver the
user's prompt with a short unavailable-context notice and topic link.
Consumed or explicitly dropped inputs release their `addressed_peers` keys and
addressed-to-self audience snapshots. On cursor advancement, retain the newest
100 consumed unaddressed snapshots per bot/channel/topic, sufficient for the
100-message history query; never prune newer snapshots. Defer audience pruning
while that bot has any held or pending input, whose older anchor may still need
history. Legacy audience snapshots without a topic are conservatively retained.

**Multi-party channels (workshop amendment).** `bridge.multi_party` defaults to true.
A channel with more than one active human subscriber requires an explicit owner
mention for every prompt, steer and topic command: a message without a mention goes
to no bot there (rule 6.2), and a typed mention is one. Unmentioned owner chat becomes context; bots and other people
never trigger turns. Asides remain ignored. Cache `GET /streams/{id}/members`
subscriber IDs with the users list, refresh on subscription peer_add/peer_remove,
and expire after three minutes. Unknown membership requires mentions as a
conservative fallback. Single-human channels retain R3, and a message without a mention goes to
all of the VM's bots (above); `multi_party=false` restores the preceding session prompt and
context, and a message without a mention goes only to the bots with a session in the topic.

In multi-party channels, every owner prompt or steer uses the R3 fetch, window,
cursor and backend-acceptance rules, regardless of co-addressing. Include all
not-yet-received discussion, excluding the bot's own output, triggering inputs,
asides and CONTROL-matching bridge lines. Label the owner `(your user)`, other
humans `(another participant)`, configured bridge peers `(your user's other agent)`,
and foreign bots `(another participant's agent)`. The preamble identifies the
owner, calls all history context rather than instructions, and restricts the
agent to that owner's request. The session prompt repeats this boundary.

The preamble is capped at 40,000 characters, each message at 8,000 (including the
inner JSON sizing cap). On truncation or omission, save the complete selection
from the fetched 100-message window as Markdown under
`.zab/inbox/topic-context-<bridge-input-id>.md`, and name it in the preamble.
The input UUID is assigned before backend submission, including steers; no native
turn ID exists yet for a new turn. `.zab/` remains git-excluded. On a transcript
write failure, send an unavailable notice and preserve the cursor for retry.

### 4.6 Commands

Commands are recognized at the start of M′ (§4.5 step 4), that is, after an optional leading mention of the bot, in a message from an allowed sender. Unknown commands get a one-line reply pointing to `!help`.

Commands are read as people type them (#709, `zab.core.command_text.as_command`): the verb in any case (`!Login`, from a phone keyboard), and a message that is one code span on one line holding a command (`` `!login` ``, which Zulip's paste of the rendered "Start here" gives) is that command; a fenced block never is. What follows the verb is kept as typed. Any other message, a prompt that starts with a code span included, is unchanged. In a topic they are read after the leading address list (§4.5 step 4), typed mentions included, and before the command rules of §4.5, so that an unmentioned `!Help` is the VM's `!help`. The same holds in a one-to-one DM, after its leading mention of the bot (below).

Topic commands (in a session topic, or on the first message of a new one):

| Command | Effect |
|---|---|
| `!help` | List the commands. |
| `!new [opts]` | Only on the mention that creates a session. Options: `--model M`, `--effort E`, `--approval P`, `--account A` (Claude), `--worktree` or `--shared`, `--subdir PATH` (project-relative working directory). |
| `!session` | Show backend/machine, native id, workspace/branch, resolved settings, state, turns/tokens and the SSH resume command. |
| `!status` | Reply with the current state: idle, running and elapsed time, or pending approval. A session in `error`, or waiting for its backend, is reopened first (§7.8.1). |
| `!stop` | Interrupt the running turn (§7.7). Queued messages are kept. |
| `!model [M]`, `!effort [E]` | Show the resolved value (label agent defaults), or validate and set it for subsequent turns (§7.9). |
| `!approve`, `!approve-session`, `!deny` | Answer the oldest pending approval (§7.6). |
| `!diff` | Post the workspace diff since the session started (§10.5). |
| `!file PATH` | Upload a file from the workspace, subject to the denylist and size limits (§11.4). |
| `!handover` | Run the canned handover turn (Appendix A.2). |
| `!fork [--name NAME] prompt` | Run the prompt in parallel, in a new topic `<topic> ⑂ NAME` (or `⑂ 1`, `⑂ 2`, …), in a copy of this session through its last completed turn, on a worktree branched from this topic's files as they stand (§7.11). Only in a topic with a session of this bot. Each bot that gets one `!fork` message forks into one topic and worktree, the first's (#712). |
| `!btw question` | Ask this topic's agent a side question about the conversation: answered without tools, in a reply marked `💬 btw`, while a running turn goes on; the question and its answer never enter the conversation (§7.13). Only in a topic with a session of this bot. |
| `!attach [ID]` | With no ID, list recent native sessions for this project. With an ID, bind that session to this topic (§8.7, §9.8). Only valid in a topic without a session of this bot; it must mention the bot (§4.5 step 7). A native session that is attached elsewhere must be detached there first. |
| `!detach` | Unbind the session from the topic and mark it `detached`. The native session is left intact and can be attached elsewhere. Afterwards the topic has no session of the bot, and mentioning the bot starts a new one; a message without a mention doesn't reach it there (§4.5). |
| `!queue [clear]` | Show, or drop, messages held while a turn runs (§7.5). |
| `!tools` | Post the VM's software from its list `/etc/zab/tools.json` (schema 1, #475): the tools grouped by `area`, each with its version and `call`, in one message (summaries dropped first, then the last tools, with a line saying how many and where the list is). Answered by the bridge, never an agent turn, in a topic with or without a session and in a DM. A missing or unreadable list is one plain sentence. |

**Commands without a mention (#713).** In a single-human channel a topic command that names
no bot goes to each of the VM's bots (§4.5), and each does what it does when named, with these
exceptions, so that a bot the command doesn't concern stays quiet (`Route.quiet`,
`Delivery.quiet`):
- `!help`, `!vm` and `!tools`, whose answer is the VM's, are answered by one bot, the first by
  full name among those the command reaches (Claude's on the workshop VMs).
- A bot without a session in the topic gives rule 7's refusal ("No session here", or
  `!fork`'s answer) only where no other bot of the VM has a session there, and then only the
  first by name; `!attach` and `!new` are its own, and `!login`'s pointer to a DM comes from
  each.
- `!stop` stops each running turn; an idle bot says nothing.
- `!model M` and `!effort E` set the value on each bot whose backend accepts it; one that
  refuses it says nothing, since the value is the other agent's. So does one whose backend
  can't verify the value, which a named command accepts (Codex without its model list,
  `model/list`, `zab.core.reason.verified_only`): it changes nothing. A value neither knows
  gets no answer: name the agent to see its list (`@claude !model …`). Without a value, each
  shows its own.
- `!new …` where the bot has a session says nothing; a bot without one starts a session.
- `!handover` where both agents have a session in the topic is refused, once, by the first bot
  by name: both would write `HANDOVER.md` in the topic's one worktree (§10.1), so the reply
  asks to name one (`@claude !handover`). Where one agent has a session, it runs it.

The rest answer from each bot, about its own session: `!status`, `!session`, `!usage`, `!diff`,
`!queue` and `!queue clear`, `!approve`, `!approve-session` and `!deny` (each answers its own
oldest pending request, and says nothing without one, as when named), `!file PATH` (from each
workspace; a refusal names the reason, a missing file included), `!detach` (each detaches,
and its reply says that a mention now starts its new session in the topic), `!attach` (each
lists its own sessions, or tries the ID, and the one whose backend doesn't know it says so:
the bridge can't tell a native ID's backend without asking it), `!fork` (each forks its own
session, all into one fork topic and one worktree, §7.11) and unknown commands. A mention
keeps a command to the bots it names, and every bot answers it as before.

Topic and DM `!status` append “sign in via DM: !login” only when the backend
explicitly reports no signed-in account. An unavailable or failed health check
is not evidence of being signed out.

`!usage` is a topic command showing account limit rows with ten-cell coloured
emoji bars with whole-percent labels, context occupancy, and cumulative session
token breakdowns (per model for Claude). Bar colour, by the whole percent shown:
green below 70%, yellow below 80%, orange below 90%, red otherwise; round filled
cells to the nearest tenth of that percent, halves up, with at least one filled
cell for any nonzero utilization. `!vm` uses the same bar function
(`zab.render.bars`, issue #204). Use Zulip `<time:…>` reset and observation
timestamps. Claude cache hit = cache read / (input + cache read
+ cache write). Codex columns are Input / Cached / Output / Reasoning; Claude's
are Input / Output / Cache read / Cache write. Unknown fields are omitted with
an explanation, not replaced with zeros. Do not display credits or subscription
list-price cost. End every table with a blank line before any following paragraph.
The asynchronous `Backend.usage` returns
`Usage` without starting a turn: Codex fetches account limits on demand, Claude
uses its last observed turn. Session and limit timestamps are separate: "This
session · as of <time>" dates the session's figures, and "Account limits · as of
<time>", a line of its own above the limits table, the time the limits were
reported (for Claude, when the backend stored the turn's `rate_limit_event`).
A window whose reset time is at or before now has reset since it was reported,
so its row shows no bar and no percentage: "unknown until the next Claude turn"
(Codex: "unknown; try !usage again"), and "reset at <time>" (issue #647).

Direct-message commands to a bot: `!help` (command list), `!status` (bridge and backend health), `!vm` and `!tools` (as in a topic), `!login [account]` (subscription sign-in, described below), and `!restart`, `!restart codex`. Where `zab-sentinel.service` runs (§13.3), `zab sentinel` answers `!vm` and `!restart` in a one-to-one DM, also while the bridge is down or deaf, and the bridge stays silent on them (issue #204; §13.5). A DM command may follow a leading mention of the bot, typed or completed. The bridge's DM route, its sign-in (`!login`, and `!login cancel` during one) and `zab sentinel` drop a typed mention of the bot's own kind first (`zab.core.addressing.without_typed_address`, #713), then read the rest as people type commands (`as_command`, above), after a completed mention of the bot (`@**Bot name**`, #709). The bridge and the sentinel read a DM with the same kind and mention, the router's, so that both agree on which of them answers. `@claude !login` to Claude's bot is `!login`, and so are `@**Claude · alice** !Login`, ``@claude `!login` `` and `@claude @**Claude · alice** !login`; `@codex !login` to it is no command, and neither is a completed mention followed by a typed one, `@**Claude · alice** @claude !vm`: the typed mention is dropped only first. `zab sentinel`'s `!restart` reads its one word in any case too: `!Restart Codex` is `!restart codex`. A pasted code during a sign-in comes without a leading typed mention: `@claude CODE` passes on `CODE`.

`!login [account]` in a one-to-one DM starts the CLI's own login flow for an
allowlisted user (D24); `account` is an optional Claude account alias. One login
per bot, with a ten-minute deadline and `!login cancel`. While pending, that
owner's non-command DMs go only to the backend's ephemeral `receive(timeout_s)`
callback, never to routing, persistence or logging. Commands beginning with `!`
(other than `!login cancel`) receive "A login is in progress: paste the code here,
or send !login cancel." and are never passed to the CLI. The bridge replies ✅ connected or ❌,
then refreshes backend health. The CLI alone handles credential files/tokens.
A backend that is not connected yet (Codex's daemon right after a deploy; Claude's binary not
found) raises `BackendUnavailable` before sending anything. The bridge then says "⏳ Codex isn't
connected yet…" once, retries every 2 s for up to 60 s, and starts the sign-in as soon as it
connects; if it never does, it replies "⏳ … still isn't connected, so the sign-in didn't start.
Send !login again in a minute.", never ❌. DMs sent during that wait are discarded, and the
exception's text is never posted or logged. `BackendUnavailable` after something was sent is a ❌.
Codex relays the device verification URL/code and the instruction to turn on device code
sign-in for Codex in ChatGPT's settings, as ChatGPT names it (under "Security and login",
"Enable device code sign-in for Codex", as of Nov 1, 2026), and says that nothing goes back
into the DM: the code is typed at the link (#709). It then waits for
`account/login/completed`; timeout/cancellation calls `account/login/cancel`. Claude's reply
asks for the code its page shows, pasted back into the DM.
A topic `!login` receives "Send !login to me in a direct message." An AUTH turn
failure receives one line: "This agent isn't connected to your Claude/ChatGPT
subscription yet: send me !login in a direct message." Help and the one-line
status reply mention DM sign-in; raw authentication errors are not posted.

### 4.7 Reactions

Reactions are matched on `emoji_code` with `reaction_type == "unicode_emoji"`; names vary between emoji sets.

Reactions on bot messages from any allowed user (§2 "owner"); reactions from anyone else are ignored:

| Message | Emoji (code) | Effect |
|---|---|---|
| status message | 🛑 (`1f6d1`) | Stop the turn |
| approval message | 👍 (`1f44d`) | Accept |
| approval message | 🔁 (`1f501`) | Accept for the rest of the session |
| approval message | 👎 (`1f44e`) | Decline |

The bot's acknowledgements on the owner's messages:

| Emoji (code) | Meaning |
|---|---|
| 👀 (`1f440`) | Received, and a turn started |
| ↪️ (`21aa`) | Injected into the running turn (steer) |
| ✅ (`2705`) | Beside ↪️: the agent has taken the message in, at its next step (§7.3; issue #654) |
| ⏳ (`23f3`) | Held for the next turn (§7.5) |
| ⚠️ (`26a0`) | Could not be processed; see the bot's reply |

The add-reaction API needs `emoji_name`. Look up the names for these codes in the server's emoji data once at startup: the bridge reads the `code_to_names` of the `server_emoji_data_url` that `register` returns (**VERIFY** the endpoint and field names).

### 4.8 Notifications

- Edits never notify, which is why progress goes into an edited status message.
- The final answer is a new message. The owner follows the topic automatically after posting in it, so Zulip's followed-topic notification settings apply.
- The bridge @-mentions the owner, with a normal (not silent) mention, when any of the cases below applies. The user mentioned is the allowed user who sent the message that triggered the turn; for an approval, the sender of the turn's last message.
  - an approval is needed;
  - a turn fails;
  - a turn finishes after running at least `mention_after_s` seconds (default 120; on a workshop's VMs, the workshop's, from `admin.toml`: ADMIN §2.1, #711);
  - a side answer (§7.13) took at least 60 s, whatever `mention_after_s` says: the mention is in its first part.
- While a turn runs, the bridge may send a topic typing indicator (`POST /typing`, renewed every 10 s).

### 4.9 Message formats

Exact templates for the card, status, approval and final messages are in Appendix B. The shapes:

- **Card.** One friendly anchor line, posted once and never edited. Preserve the `**zab session**` marker (D7). `!session` posts current session details separately.
- **Status message.** Posted when a turn starts. While running, it shows the state, when the turn started (a Zulip time, `<time:…>`), the last eight actions, plan progress and the agent's last three notes (its interim texts), newest last, in one quote, a paragraph each. Nothing in it changes only because time passes: no elapsed time, and no running step's time (issue #754). Each note is redacted (§11.6), then put on one line and cut after a word to at most 500 characters, with `…` (issue #654), never inside such a Zulip time as §11.1 defines (issue #673). When the turn ends, it becomes a one-line summary with the turn's duration, followed by the action log in a spoiler. The log is capped at the last 50 actions and truncated so that the whole message fits in `max_message_length − 200`.
- **Status edits (issue #754; D41).** Zulip keeps every earlier version of an edited message, and Zulip Cloud's guidance is to edit a message only when something changes in an important way, about once a minute at most. The status message is edited when the turn's model becomes known, when an approval is requested or answered, when the turn is stopping, when the backend is reconnecting or recovers, and with the summary when the turn ends. Its action list, plan and notes are refreshed only when they changed. From one edit gap (60 s) after the turn starts, the bridge looks every 30 s, and the first look that finds them changed edits the message once, the early view: most turns end within five minutes, and one early view of what the agent does costs one edit, even when the agent is slow to report its first action. An edit of the model, effort or state alone is not the early view. After it, a refresh comes at the first look that finds a change at least `status_edit_interval_s` after the last (default 300, at least 60: a lower value is a configuration error). Every edit of a message, whatever its path, goes through one place (`ZulipClient.edit`), which bounds each message:
  - two edits of a message are at least 60 s apart. A final edit may come sooner: the turn's summary (or the restart notice in its place, §7.8.1), and the answer on an approval request. Each is its message's last edit, so it adds one edit at most when it lands, and holding a summary back would show a finished turn as running. A summary that fails is tried again at a look at most once a gap (`redeliver`, `!status` and a reconnect retry it too), each try counted, since it may have landed;
  - a message gets at most 20 edits, a final one included. The others stop at 19, so that the final edit always has its slot; the 19th says that updates are paused, that `!status` shows the turn's progress, and that the summary comes when the turn ends. A summary whose edit can't be made, its slot spent by a failed one that may have landed, is posted as a new status message;
  - an edit that comes within its gap is held, one per message, merged with any later one (the newest content wins), and sent when the gap has passed, unless a final edit comes first; one past the cap is dropped. Nothing queues edits without limit.

  The counts are kept in memory, for the 4096 messages edited last: a restart begins them again. So a bridge that restarts in a loop (systemd restarts it 5 s after a failure) may edit a surviving turn's status message once per restart, as recovery refreshes it; persisting the counts is a follow-up.
- **Approval message.** What is being requested (the command and working directory, or the files and a diff in a spoiler), the reason, and the reaction legend. When answered, it is edited to show who decided what; if answered elsewhere, it shows "resolved elsewhere".
- **Final answer.** The rendered agent text (§11), in one or more parts.
- **Diff message.** If the turn changed files, one more message follows the final answer: one visible summary line (with the diff attached when it is too large to inline) and one spoiler with the file list and, when it fits, the patch (§11.8). It is always a separate message, never appended to the answer.
- **Bridge notes (issue #677).** What the bridge says on its own behalf, or passes on from the program the agent runs in, starts with one marker, so that a newcomer never takes it for the agent's words: `⚠️ **Bridge** (not <Agent>): ` for warnings and errors, `🔧 **Bridge** (not <Agent>): ` for information, where `<Agent>` is the bot's agent, Claude or Codex (Appendix B.4). It marks backend `Notice`s (§8.5, §9.4), each by its level; the ⚠️ lines posted into a topic: a failed session operation, a session that needs recovery (§7.3), answer delivery pending, an answer that could not be formatted, a missing diff, a rejected attachment, a failed or refused `!fork` (§7.11), a failed, refused or timed-out `!btw` (§7.13), a failed session creation and an expired message (§7.2, §7.8.1), and the one-time replies to a plain mention in a public channel or an empty topic (§4.3, §4.5); and the bridge's paragraphs inside an answer: a failed turn's error (§7.9), the sign-in guidance, a stop without confirmation, a stale turn that recovery ends (§7.8.1), a lost steer and a stopped background job. The card, the status and ⏳ waiting lines, the approval and diff messages, the `@user turn …` ping, the `⑂` lines, the `💬 btw` answers and replies to `!` commands keep their own forms, as do the sentinel's DMs. Text from elsewhere, such as a notice's, is redacted (§11.6), its NUL characters dropped first, before a marker or prefix goes in front of it, since the zuliprc rule matches whole lines. Peer context (§4.5) skips a message that starts with the marker, as it skips those.

### 4.10 Zulip API usage

Client:

- `zulip/client.py` is a thin async wrapper over `httpx`, using HTTP Basic authentication with the credentials from the zuliprc (`[api] email`, `key`, `site`).
- Send `User-Agent: zab/<version>`.
- Read `max_message_length`, `max_topic_length` and `max_file_upload_size_mib` from the `register` response and never hard-code them. The test organization's (Zulip Cloud, feature level 511) were 10000, 60 and 10 MiB (DECISIONS, 2026-09-27, "Registration limits"); another organization's may differ.

Event queue:

- `POST /register` with `event_types: ["message", "reaction", "update_message", "subscription", "stream", "realm_user"]`, `fetch_event_types: ["realm", "subscription"]` (without `realm` the response lacks the limits above: DECISIONS, 2026-09-27), `apply_markdown: false` (so message content arrives as raw markdown), `client_gravatar: true` and `include_subscribers: true` (§4.5, "Multi-party channels"). A `stream` or `subscription` event forgets the cached privacy of the channels it names (all of them if it names none). **VERIFY** that a channel made public or private sends the bot a `stream` event (`op: update`, `property: invite_only`) on Zulip Cloud; the cache expiry of two minutes bounds the staleness meanwhile.
- Then long-poll `GET /events` with `queue_id` and `last_event_id`. Persist `last_event_id` for each bot (§6.2).
- On `BAD_EVENT_QUEUE_ID`, register again, then reconcile (§7.8.2).
- After a failed poll, poll once with `dont_block: true`, then retry undelivered answers (§7.8.2).

Endpoints used:

| Purpose | Endpoint |
|---|---|
| Identity | `GET /users/me`; `GET /users/{id}` (for `bot_owner_id`) |
| Channel privacy | `GET /streams/{id}` (`invite_only`, `is_web_public`), cached until a `stream` or `subscription` event about the channel, a new event queue, or two minutes |
| Send | `POST /messages` (`type: "stream"`, `to: <channel id>`, `topic`, `content`) |
| Edit | `PATCH /messages/{id}` with `content` |
| Fetch | `GET /messages/{id}`; `GET /messages` with a narrow of channel and topic, anchored after the last processed id, for reconciliation; with a narrow of channel, topic and `sender` (the bot's user id), from `anchor: "oldest"`, to find a moved card (§4.4). Zulip Cloud accepts a numeric `sender` operand: the waker's narrow uses one, and the server answered it live (DECISIONS, 2026-10-27, the live check of §13.4.2). A 400 still makes the bridge filter the topic's messages itself |
| Reactions | `POST /messages/{id}/reactions` |
| Upload | `POST /user_uploads`, which returns `url` (older servers return `uri`, **VERIFY**) |
| Download an attachment | `GET /user_uploads/{realm_id_str}/{filename}`, which returns a temporary URL to fetch |
| Typing | `POST /typing` with `type: "stream"`, `stream_id`, `topic`, `op` |

`update_message` events: when an event's `message_ids` include a known card id, update that session's cached location from `new_stream_id` and `subject` (the new topic). On feature level 511 a move carries `stream_id`, `new_stream_id` and the affected `message_ids` (DECISIONS, "M1 move verification", 2026-09-28).

Rate limits:

- The default limit is 200 requests per minute per user, and each bot is a separate user.
- The API client shares one rolling 60-second budget per bot (default 150 requests, including event polls and uploads) with the poster, honours `X-RateLimit-Remaining` and `X-RateLimit-Reset`, and on HTTP 429 sleeps until the reset time.
- Status edits are coalesced: only the latest content for each message id is sent. Each message's edits are bounded, at least 60 s apart and at most 20 (§4.9, "Status edits").
- Priorities, highest first: final answers, approvals, acknowledgements, status edits, housekeeping.
- Housekeeping (§7.1: confirming cards, reconciling topics) also leaves a reserve of the window to everything else: the poster sends a housekeeping request only while more than a third of the budget (50 of 150) is free in the window, checked when it would be sent, and only when no other request is waiting, so that housekeeping never makes a reply wait for the budget (issue #443).

Splitting: a message longer than `max_message_length` is split as described in §11.7. Topic names are truncated to `max_topic_length`.

## 5. Configuration

### 5.1 Files and directories

| What | Default location | Override |
|---|---|---|
| Config | `~/.config/zab/config.toml` | `--config`, or `ZAB_CONFIG` |
| Bot credentials | `~/.config/zab/<bot>.zuliprc` | per bot, in the config |
| State | `~/.local/state/zab/` (database, logs, lock files) | `bridge.state_dir` |

- Credential files must have mode `0600`. `zab doctor` fails if they don't.
- `config/zab.example.toml` is the reference example and must be kept in sync with this section.

### 5.2 Schema

```toml
[bridge]
machine = "laptop"               # shown on cards; default: short hostname
state_dir = "~/.local/state/zab"
projects_root = "~/src"          # where auto-cloned projects go (§5.4)
default_project = ""             # path used for channels without a [[projects]] entry (§5.4)
auto_clone = false               # clone `repo:` from the channel description (§5.4)
allow_public_channels = false
multi_party = true                 # owner-only mentions and shared context in multi-human channels
mention_after_s = 120            # §4.8
status_edit_interval_s = 300     # §4.9: at least 60
max_concurrent_turns = 3         # per backend; extra turns wait with ⏳ (§7.10)
default_workspace = "shared"     # "shared" | "worktree" (§10.1)
service_path_prepend = []        # directories put first on the services' PATH (§13.1)
service_env = {}                 # extra environment for the services, e.g. { VIRTUAL_ENV = "~/venv" }
worktree_post_create = []        # argv run in each new topic worktree: no shell, 60 s, failures logged

[[bots]]
name = "codex"                   # local handle
zuliprc = "~/.config/zab/codex.zuliprc"
backend = "codex"                # key into [backends]
allow_user_ids = []              # combined with resolved allow_emails
allow_emails = []                # case-insensitive; both lists empty = bot owner

[[bots]]
name = "claude"
zuliprc = "~/.config/zab/claude.zuliprc"
backend = "claude"

[backends.codex]
kind = "codex"
binary = ""                      # empty = `codex` on PATH; zab init writes the absolute path
connect = "daemon"               # "daemon" (unix socket) | "spawn" (stdio child) (§8.1)
socket = ""                      # empty = $CODEX_HOME/app-server-control/app-server-control.sock
codex_home = ""                  # empty = inherit (~/.codex)
answer_external_approvals = false  # §8.6
git_identity = "auto"            # who the agent commits as: "auto" | "Name <email>" | "off" (§5.5)
defaults = { model = "", effort = "", approval = "never", sandbox = "workspace-write" }

[backends.claude]
kind = "claude"
binary = "claude"
accounts = { main = "~/.claude" }  # name → CLAUDE_CONFIG_DIR
default_account = "main"
defaults = { model = "", approval = "bypassPermissions" }  # approval = --permission-mode
allowed_tools = []               # passed as --allowedTools when non-empty; absent = §9.5's list
disallowed_tools = []
steer = true                     # messages sent during a turn steer it; false holds them (§9.7)
claudeai_connectors = false      # the signed-in account's claude.ai connectors in bridge turns
git_identity = "auto"            # as for Codex (§5.5)

[[projects]]
channel = "g2-torsion"           # channel name or numeric id
path = "~/src/Genus2Torsion"
workspace = "worktree"           # optional per-project override
subdir = ""                      # optional default working directory inside the project
backend_defaults = { claude = { account = "main" } }

[render]
auto_upload_ext = ["png", "jpg", "jpeg", "gif", "svg", "pdf"]
max_upload_mb = 10               # also capped by the server limit
diff_inline_max_lines = 300
# text files a final answer references, uploaded as <name>.<ext>.txt (§11.3)
auto_upload_text_ext = ["lean", "tex", "bib", "sty", "py", "sage", "gp", "m", "g", "mag", "txt", "md", "json", "csv", "toml", "yaml", "yml", "sh", "c", "h", "cpp", "rs", "jl", "log", "out"]
max_text_upload_kb = 512
max_text_uploads = 10            # per answer; 0 = off
refserve_url = ""                # the file server's base URL; empty = off (§10.7)
refserve_owner = "MathAgents"    # the GitHub owner whose repositories it serves (§10.7)

[lean]
reap_idle_minutes = 30           # stop Codex's idle Lean servers after this long; 0 = off (§7.12)
```

Rules:

- Empty strings mean "use the agent's own default". The bridge passes only settings that are non-empty.
- Unknown keys are a fatal error that names the key and its file position.
- Relative paths are resolved against the config file's directory, and `~` is expanded.
- `allow_user_ids` and `allow_emails` form one explicit allowlist, applying to topics,
  reactions and DMs. Only when both lists are empty does the bot owner apply. A
  configured email that has not joined or is hidden authorizes nobody, with no
  fallback to the owner. IDs can be used when a user keeps their email private.
- Resolve active human users at startup and after `realm_user` events. Match
  case-insensitively against visible `delivery_email`, otherwise `email`. Refresh
  existing actors too; loss of authorization cancels a pending login. Failed user
  refreshes discard email authorizations; explicit IDs remain configured.
- Before inviting participants, an organization administrator sets **Organization
  settings → Default user settings → Privacy settings → Who can access user's email
  address → Admins, moderators and members** (`email_address_visibility=2`; *Everyone*,
  `1`, works too). A participant's bots are members, so they can then read the
  participant's address, and the provisioning account, a Moderator, binds each row by it
  (ADMIN.md Q6). The trade-off: participants can see each other's addresses. Existing
  users keep their own setting. One who restricts it to admins and moderators is allowed
  by ID once provisioning has bound them and a rollout has written the ID; one who
  restricts it further (admins only, or nobody) can't be bound, and needs at least
  **Personal settings → Account & privacy → Who can access your email address → Admins
  and moderators**. These are
  [Zulip's default and per-user visibility controls](https://zulip.com/help/restrict-visibility-of-email-addresses).
  A hidden address appears as null `delivery_email` and a synthetic API `email`,
  so a configured real address cannot resolve ([API](https://zulip.com/api/get-users)).

### 5.3 Profiles

`zab init --profile personal|workshop` writes a starting config. `personal` is the default, and
`drew`, its name before issue #593, is an alias of it: the same config, byte for byte. The
profile's name is not written to the config, so a config written under either name loads the same.

- **personal**: shared workspaces by default and worktree mode per project. Codex runs in daemon mode with `approval = "never"` and the `workspace-write` sandbox. Claude uses `bypassPermissions` and one account, `main`, which is the CLI's default directory `~/.claude` (§9.2). `zab init` asks for no account: it writes that `[backends.claude]` table when it adds a Claude bot (`--backend claude`) and the config has no such table yet, and otherwise leaves that table as it is, accounts, `default_account`, `binary` and `defaults` included. Other accounts are added to `accounts` by editing the config (§5.2).
- **workshop**: the shared workspace; `default_project` set to the participant's repo; Codex with `approval = "never"` and `sandbox = "danger-full-access"` (the VM is disposable, and agents need `sudo`); Claude with `bypassPermissions`; `max_concurrent_turns = 2`; `allow_user_ids` set to the participant.
  The workshop VMs themselves get their config from `workshop/admin/push_config.py`, which differs: `default_workspace = "worktree"`, a worktree per topic (issue #36: in one shared checkout the agents overwrote each other's files and the turn diff mixed topics; `!new --shared` opts out); `max_concurrent_turns = 4`; `mention_after_s`, the workshop's (`--mention-after-s`, from `admin.toml`; #711); `allow_public_channels = true`, since participants may make their sandboxes public; `service_path_prepend` and `service_env` with the agent's venv and elan; `worktree_post_create` running `lean-project --repair-all` (which `--lean-shared` installs) to link committed Lean projects to the shared Mathlib; `effort = "high"` for both agents; Claude's `steer` and `bridge.multi_party`, written out (`--no-claude-steer`, `--no-multi-party`); and the `[render] refserve_url` of the deployment's file server, with `refserve_owner`, the workshop's `github_org` (`--refserve-owner`; #693).

### 5.4 Resolving a channel to a project

1. If a `[[projects]]` entry matches the channel by id or by name, use its path.
2. Else, if `bridge.default_project` is set, use it. The workshop profile sets it.
3. Else, if the channel description has a `repo: owner/name` line and `bridge.auto_clone = true`, clone into `<projects_root>/<name>` with the machine's existing git credentials, then use that path. Auto-clone is off by default.
4. Else, do not resolve; the router replies with the fix (§4.5 rule 7.4).

The path must exist and be a git repository. Otherwise reply with the problem and do not create a session.

### 5.5 Commit identity (issue #214)

`SessionSpec.git_identity` (a `GitIdentity(name, email)`, or None) is who the agent's commits are
made as. A backend sets `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME` and
`GIT_COMMITTER_EMAIL` to it in every process the agent's tools run in, for every turn of the
session: Claude in the turn's environment (`turn_env`, §9.1), Codex through
the thread's `shell_environment_policy.set` (verified on 0.157.1: DECISIONS, 2026-10-11, "Codex
commit identity"). With
None it sets none of them and removes inherited ones, so the user's git config applies.

The core resolves it once per bot from `[backends.<kind>] git_identity`: `"auto"` (the default)
is the bot's Zulip full name and e-mail, for example `Claude · alice` and the bot's address, which
receives no mail, so no GitHub account can claim the commits; `"Name <email>"` is used as given;
`"off"` is None. It goes on every `SessionSpec` the core builds (create, open, fork), so a
session reopened after a restart keeps it. git keeps the original author when an agent rebases,
cherry-picks, amends or applies (`git am`) someone else's commits, and records the agent only as
committer; merges, reverts and new commits are the agent's (DECISIONS, 2026-10-11).

### 5.6 The session's bot, `ZAB_BOT` (issue #248)

`SessionSpec.bot` is the name (`[[bots]] name`) of the bot the session belongs to. A backend sets
`ZAB_BOT` to it in every process the agent's tools run in: Claude in the turn's environment
(`turn_env`, §9.1), Codex through `shell_environment_policy.set`, next to the commit identity
(§5.5). With `""` it sets nothing and removes an inherited `ZAB_BOT`. Like the commit identity,
the core passes it on every `SessionSpec` it builds (create, attach, reopen, recovery and fork):
the bot whose session it is.

One exception: a Codex thread that its app-server had already loaded keeps the environment it
was loaded with, so a thread loaded before the upgrade that added `ZAB_BOT` runs without it, and
one reopened for another bot keeps the old value, until the app-server restarts. The backend
resumes such a thread rather than refusing it, unlike an unknown commit identity (DECISIONS,
2026-10-13).

It is the default bot of `zab history`, the read-only way for an agent to read another topic
without the zuliprc (§12.2): `zab history --list-topics [--match TEXT]` and `zab history
--channel C --topic T`. It is a default, not a permission: `--bot NAME` overrides it, and without
it `zab history` takes the config's only bot or asks for `--bot`.

## 6. State

### 6.1 Store

- SQLite at `<state_dir>/zab.db` in WAL mode. Schema versions are tracked with `PRAGMA user_version`, and each migration is a numbered function.
- Each bot holds an exclusive lock file `<state_dir>/<bot>.lock` (`fcntl.flock`) for its whole lifetime (§4.2).

### 6.2 Schema

```sql
CREATE TABLE sessions (
  id              TEXT PRIMARY KEY,      -- zab session id (uuid4)
  bot             TEXT NOT NULL,
  backend         TEXT NOT NULL,
  native_id       TEXT NOT NULL,
  card_message_id INTEGER NOT NULL UNIQUE,
  stream_id       INTEGER NOT NULL,      -- cached card location (§4.4)
  topic           TEXT NOT NULL,
  workspace       TEXT NOT NULL,         -- the session's cwd (absolute); inside repo_root
  workspace_mode  TEXT NOT NULL,         -- shared | worktree
  repo_root       TEXT NOT NULL,         -- absolute path (§10.1)
  branch          TEXT,
  start_snapshot  TEXT,                  -- JSON-encoded Snapshot at session start (§10.5)
  settings_json   TEXT NOT NULL,
  state           TEXT NOT NULL,         -- idle | running | awaiting_approval | detached | readonly | error
  shared          INTEGER NOT NULL DEFAULT 0,
  last_sender_id  INTEGER,               -- allowed user to mention (§4.8)
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL,
  parent_session_id TEXT                 -- the session a `!fork` copied (§7.11); NULL otherwise
);
-- a native session can be bound to at most one topic at a time (§4.6 !attach / !detach)
CREATE UNIQUE INDEX sessions_native ON sessions(backend, native_id) WHERE state != 'detached';
-- a topic holds at most one session per bot (§4.4)
CREATE UNIQUE INDEX sessions_topic ON sessions(bot, stream_id, topic) WHERE state != 'detached';
CREATE TABLE turns (
  id                INTEGER PRIMARY KEY,
  session_id        TEXT NOT NULL REFERENCES sessions(id),
  native_turn_id    TEXT,
  trigger_msg_ids   TEXT NOT NULL,       -- JSON list of Zulip message ids
  state             TEXT NOT NULL,       -- running | completed | interrupted | failed | superseded
  status_message_id INTEGER,
  final_message_ids TEXT,                -- JSON list; NULL until posted
  start_snapshot    TEXT,                -- JSON-encoded Snapshot for the turn diff (§10.5)
  pgid              INTEGER,             -- Claude only: process group of the running turn (§9.6)
  sender_id         INTEGER,             -- allowed user who triggered the turn (§4.8)
  started_at        REAL NOT NULL,
  ended_at          REAL,
  UNIQUE (session_id, native_turn_id)
);
CREATE TABLE approvals (
  request_id  TEXT PRIMARY KEY,          -- backend request id
  session_id  TEXT NOT NULL REFERENCES sessions(id),
  message_id  INTEGER,
  kind        TEXT NOT NULL,
  state       TEXT NOT NULL,             -- pending | accepted | accepted_for_session | declined | cancelled | resolved_elsewhere
  created_at  REAL NOT NULL
);
CREATE TABLE inbound (
  message_id  INTEGER NOT NULL,          -- Zulip message id
  bot         TEXT NOT NULL,
  session_id  TEXT,
  disposition TEXT NOT NULL,             -- ignored: claimed, nothing taken in (a command, a
                                         --   message for another bot); idle stop skips it
                                         -- turn: a first prompt (!new, a mention, a !fork)
                                         -- delivered: to a session (a turn, a steer, held)
                                         -- attach: !attach ID | side_question: !btw (§7.13)
  handled_at  REAL NOT NULL,
  PRIMARY KEY (message_id, bot)          -- each bot of a bridge sees the message (D40)
);
CREATE TABLE held (
  message_id  INTEGER NOT NULL,
  session_id  TEXT NOT NULL REFERENCES sessions(id),
  sender      TEXT NOT NULL,
  content     TEXT NOT NULL,
  received_at REAL NOT NULL,
  PRIMARY KEY (message_id, session_id)
);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- last_event_id:<bot>, upload cache, …
```

`parent_session_id` came after the first release (#202). A store that opens a database without it adds it (`ALTER TABLE … ADD COLUMN`, under `BEGIN IMMEDIATE`) and keeps `user_version` 1: a VM keeps its database across a rollout, and an older zab, after a rollback, still opens it, reading and writing rows by column name.

### 6.3 Invariants

- **Inbound.** Record the message id in `inbound`, for the bot, before acting on it. Never act on a message id that is already there for that bot.
- **Final answers** are posted at least once. Set `final_message_ids` immediately after a successful post. A crash between the post and the database write can duplicate one final answer; that is the only tolerated duplicate.
- **Held messages** survive restarts (§7.5).

## 7. Core behavior

The session card is an immutable anchor. Use `!session` for changing details;
turn/state/settings updates never edit or replace the anchor (Appendix B.1).

### 7.1 Startup

1. Load and validate the config, open the store, and take the bot locks.
2. Start the backends.
   - `Backend.start()` must not raise because the agent is unavailable (for example, the Codex daemon is not running). It returns, emits `BackendStatus(up=False, message=…)`, and retries internally with backoff (§7.8.3).
   - While a backend is down, its methods raise `BackendUnavailable`, and its bots reply "backend unavailable: <message>" to new messages.
   - When it recovers, it emits `BackendStatus(up=True)`, and the core reattaches that backend's active sessions (§7.8.1).
3. Reattach the active sessions of every backend that is up (§7.8.1).
4. For each bot:
   1. `GET /users/me` to learn its identity.
   2. Resolve the allowlist (§2 "owner").
   3. Look up emoji names (§4.7).
   4. Register an event queue.
   5. Catch up (§7.8.1): create an actor for every session without a request, confirm the cards of sessions with work in flight (§4.4), recover pending creations, and replay the combined feed.
   6. Start the listener.
   7. Housekeeping, in the background (issue #443): confirm the cards of recent sessions (§4.4), then reconcile their topics (§7.8.2), through the budget's housekeeping lane (§4.10).

The message that woke the VM is answered in step 5, without waiting on step 7, whose cost
grows with the number of sessions: on the participant test VM (Oct 18) the claude bot's 86
sessions took about 260 requests against its 150 a minute, and the waking DM waited about
80 s for its answer instead of about 20 s.

### 7.2 Creating a session

1. Parse any `!new` options.
2. Resolve the project (§5.4), the workspace mode, and the settings (built-in defaults < backend defaults < project `backend_defaults` < `!new` options).
3. `WorkspaceManager.prepare(...)` (§10.1): creates the worktree if the mode is `worktree`, makes sure `.zab/` is excluded, and returns `repo_root`, `cwd`, `branch` and `base` for the card. Then `WorkspaceManager.snapshot(...)`, stored as the session's `start_snapshot` (§10.5).
4. `backend.create_session(spec)` with `spec.name` = topic name and `spec.context_prompt` = Appendix A.1.
5. Post the card, then insert the session row with the card's message id and location.
6. Deliver the triggering message as the first turn (§7.3).

If any step fails, reply with ⚠️ and the error, clean up (remove a worktree that was just created), and create no session row. A step that fails because Zulip does not answer (a transport error or a transient Zulip error, at the card) is cleaned up the same way, without a reply: the triggering message stays pending, and the session is created from step 1 once Zulip answers again, before any later message is handled (§7.8.2). At startup, Zulip failing this recovery fails the start, as a failed replay does. A pending first prompt is never executed once it is older than the catch-up's two-hour limit (§7.8.1), whether its retries keep failing for a reason of their own or never ran: it gets one ⚠️ reply that links to it, and is dropped (without the reply if Zulip refuses it with a 4xx). One whose sender is unknown, because the user list could not be fetched (a failed refresh empties it, to fail closed), is kept until the sender can be checked; one whose sender a fetched list doesn't include is dropped (issue #534).

### 7.3 Delivering a message to a session

Each delivery is a `UserInput`:

- the text, with the mention and options removed;
- the attachments, downloaded into the workspace inbox (§10.3), with their paths appended to the text as `[attached: .zab/inbox/…]`;
- a fresh `client_id` (uuid4).

What happens depends on the session state:

| State | Action |
|---|---|
| `idle` | Start a turn (§7.4). React 👀. |
| `running`, and the backend can steer | `backend.steer(...)`. React ↪️. If steering is rejected (for example, the turn just ended or cannot be steered), fall back to holding the message. When the backend reports that the agent took it in, an `ItemCompleted(USER_MESSAGE)` with the input's `client_id`, react ✅ as well (issue #654): Codex's `userMessage` item (§8.5), Claude's echo (§9.4). Both come at the agent's next step, after the tool call that runs. |
| `running`, and the backend cannot steer | Hold the message (§7.5). React ⏳. |
| `awaiting_approval` | Hold the message, and reply once with a reminder that an approval is pending. |
| `readonly` or `detached` | Ignore it. |
| `error` | Hold the message. Reply once that the session needs recovery and that `!status` retries it. A ⏳ notice already under held messages, such as one caught up at startup before the reopen failed, is edited to say so. |

### 7.4 Turn lifecycle

1. Before starting the turn, take `WorkspaceManager.snapshot(...)` and store it as the turn's `start_snapshot` (§10.5).
2. `turn_id = backend.start_turn(session, input)`. Insert the turn row, post the status message ("⏳ Working…"), and set the session state to `running`.
3. On `ItemStarted` and `ItemCompleted`, update the action log. The status message is re-rendered on a timer (§4.9), not on every event.
4. On `PlanUpdated`, render the steps as ☐, ◐ or ☑ in the status message.
5. On `ApprovalRequested`, post the approval message (§7.6) and set the state to `awaiting_approval`.
6. On `TurnCompleted`:
   1. Finalize the status message (Appendix B.2).
   2. Compute the turn diff: `WorkspaceManager.diff(ws, turn start_snapshot)` (§10.5).
   3. Post the final answer (`Renderer.render_agent_text`), then, if files changed, the diff message (`Renderer.render_diff`, §11.8).
   4. Record `final_message_ids` and set the session state to `idle`.
   5. Mention the owner if §4.8 applies.
   6. If messages are held, start the next turn with them (§7.5).
7. If the final text is empty, post "(no final message)" and the diff, if any.

A turn completed with status `interrupted` or `failed` follows the same steps, but the status line starts with ⏹ or ❌, and failures render the `ErrorInfo` as described in §7.9.

### 7.5 Held messages

Messages that arrive while a turn runs and cannot be steered are stored in `held`. When the turn ends, they are delivered as a single turn whose text is:

```
[N messages arrived while you were working; the newest takes precedence]
HH:MM <sender>: <text>
…
[end of held messages]
```

- `!stop` does not drop held messages; `!queue clear` does.
- Held messages survive restarts.
- If a delivery fails, the messages stay held, and delivery is retried when the next message arrives or on `!status`.

### 7.6 Approvals

Approvals come only from backends that declare `capabilities.approvals` (Codex in Phase 1).

1. Post the approval message, mentioning the owner, and insert an `approvals` row.
2. A decision can come from:
   - the owner's reaction on the approval message (§4.7);
   - `!approve`, `!approve-session` or `!deny` in the topic, which answer the oldest pending request;
   - a backend `ApprovalResolved` event, meaning someone else answered, for example from the TUI.
3. On a decision, call `backend.respond(request_id, decision)`, edit the approval message, and return the session state to `running`.
4. If the backend lists the decisions it allows, offer only those, and reject any other reaction with a short reply.
5. There is no approval timeout in Phase 1. `!stop` cancels pending approvals and interrupts the turn.

### 7.7 Stopping

- `!stop`, or 🛑 on the status message, calls `backend.interrupt(session)`.
- The status message immediately shows "⏹ stopping…".
- If no `TurnCompleted` arrives within 30 s, the bridge marks the turn `interrupted` itself, posts a warning, and sets the session state to `error`. A later `!status` probes the backend and clears the error once the backend reports the session idle.

### 7.8 Recovery

**7.8.1 Bridge restart.** For each session in the store whose state is not `detached`:

1. `backend.open_session(native_id, spec)`.
2. For turns left `running`:
   - **Codex** (the daemon kept running): `SessionInfo.active_turn_id` and `SessionInfo.last_turn` describe the thread.
     - If the stored running turn equals `last_turn.turn_id`, it has completed. Post its final answer unless `final_message_ids` is already set, and finalize the status message with `last_turn.status` and `last_turn.error`.
     - If it equals `active_turn_id`, it is still running; resume tracking it.
     - If it is neither, mark it `interrupted` with a warning.
     - A running turn the store does not know about (for example, one started in the TUI) is tracked like any external turn (§8.6).
   - **Claude** (per-turn processes):
     - If the turn's `pgid` is still alive and its leader's command line contains `claude`, kill the group: SIGTERM, then SIGKILL after 5 s. Such a group survives when the bridge crashed or was killed, because each turn runs in its own session (§9.1).
     - Then mark the turn `interrupted`, and edit its status message to "⏹ interrupted by bridge restart; send a message to continue".

On startup, capture the bot's highest handled message ID before the replay (after an
event-queue loss, before topic reconciliation, which then precedes the replay).
With no inbound history or saved cursor, initialize at the newest message and skip
replay. Expiry notices obey normal addressing: only input for this bot receives one.
Page through its combined feed after that ID (100 messages
per fetch through the poster), preserving message flags. This also recovers new topics,
sessions idle for more than seven days, and DMs. Only requests from the last two hours
are executed, including in existing session topics. Older authorized messages receive
one notice per topic or one-to-one DM: "This message arrived while your VM was off;
send it again if it's still needed." Normal authorization still applies. A durable
ID-only catch-up cursor suppresses overlap with queued live events, including login
DMs whose contents must never be persisted. With no prior inbound ID or cursor, scan
from the beginning using the same age limit. Event-queue recovery (§7.8.2) runs the
same topic reconciliation and catch-up.

At startup, topic reconciliation (§7.8.2 steps 3 and 4) is housekeeping: it runs after
the replay and the start of the listener (§7.1), from the boundary captured before the
replay, the catch-up cursor and each session's newest inbound message. What it finds is
measured against that cursor, not the one the replay advanced, and claims keep it from
handling a message twice. It handles nothing newer than the last message the replay read:
those are the listener's, and a claim ahead of the listener would raise the next catch-up's
anchor (the newest claim) past DMs and new topics not yet read (e2e scenario 4 on #447:
an event queue lost during the housekeeping). The boundary stays in the store (`catchup_reconcile:<bot>`)
until reconciliation finishes, so a restart in between reconciles from it again. After
an event-queue loss, recovery stops the startup housekeeping if it is still running and
reconciles inline, before its replay, from the older of the two boundaries.

Backend events queued while the bridge was starting can arrive after the reopened
session's summary has already settled their turn. A late event of a settled turn is
dropped; it is never a new terminal turn (e2e scenario 2).

Each session calls `open_session` itself, as one step of its serialized actor, and
reconciles the result in that same step: at startup, on `BackendStatus(up=True)`
(§7.8.3), when its card becomes visible again, on `!status`, and after `!attach` has
posted the card. A turn the session began before
that step, such as one for a new topic caught up during startup, is in the snapshot;
no snapshot older than the turn ever judges it (e2e scenario 14). `!attach` also
reads the session once before it has an actor: a binding that new has no messages to
start a turn with, and the snapshot in the actor then settles what changed meanwhile.

The sessions of all the bots of one backend reopen in one round, one `open_session` at
a time. On `BackendUnavailable` the round stops, and the rest wait for the next
`BackendStatus(up=True)` as backend-down, not in `error`. A backend that is up but
refuses one session raises `SessionRefused` (Codex: a thread its app-server had
already loaded, with an unverified commit identity, §5.5): only that session goes to
`error`, and the round continues. `!status` reopens a session in `error`, or a
backend-down one if its backend answers, and shows a refusal's reason (issue #306).

**Detached sessions.** Reattaching a live turn atomically creates the replacement turn row and
marks prior running copies `superseded`, with an end timestamp for the old binding. Recovery
queries backend state for detached sessions too: terminal summaries settle matching rows;
turns absent from the backend become interrupted, following the existing Claude orphan checks.
A backend error leaves the row running. Live completion events also settle detached rows
without actors. No messages are posted to detached topics and no new turn is started; a later
terminal turn is not adopted into a detached binding.

**7.8.2 Event-queue loss or reconnect.** After re-registering:

1. Refresh the card locations of active sessions (§4.4), so that topics renamed or moved during the gap are queried under their new names.
2. Recover the pending creations (§7.2): create the sessions whose first prompt is still pending, before any later message is handled. If Zulip interrupts one again, this step is retried after the backoff below, without step 1 again.
3. For each active session within the reconciliation window (§2), fetch the messages in its topic after the highest `inbound.message_id` seen for that session (`GET /messages` with anchor, `num_after = 100`, and a narrow of channel and topic). These fetches go through the poster's rate limiter.
4. Process messages that are not yet in `inbound`, in order.
5. Fetch the reactions on pending approval messages and on the status messages of running turns, and apply any decisions not yet applied.

Steps 3 and 4, and a catch-up of the combined feed after it, work as at startup (§7.8.1),
with the same two-hour limit and notices. The catch-up starts after the newest message
the listener handled, so a new topic or DM sent while the queue was gone is answered,
and nothing handled live (an owner's login DMs, which are never claimed) is handled
twice. Zulip drops a queue nobody polled for about ten minutes, so a longer outage
always ends here (end-to-end scenario 4).

**Zulip unreachable.** On a transport error or a 5xx, the listener retries every second.
After a failure, its next poll does not block (`dont_block`), so it notices at once when
Zulip answers again. Then each answer that could not be posted is retried, as `!status`
would. A turn whose stop was never confirmed is the exception: it still waits for
`!status` (§7.7). A session whose creation Zulip interrupted (§7.2) is created then too,
before the events of that poll are handled, so that a later message in its topic does not
start the session in its place. If Zulip interrupts it again, the poll's events wait.
Zulip answering polls while it fails the creation (a 5xx on the card, or a transport
error during the creation) is no new outage
(issue #534): the creation is retried, then those events, after 1 s, doubling to 60 s,
and nothing else is retried or refreshed meanwhile. Once it succeeds, the answers Zulip
refused meanwhile are retried once. Its message was already
claimed, so neither the retried poll nor the catch-up reads it again (e2e scenario 4 on
c114d64f, issue #532). A failed status refresh is logged and retried at the next tick; it
does not put the session in `error`. The listener logs the outage's start, the return of
Zulip, re-registration and the catch-up at INFO or WARNING, without message contents.

**7.8.3 Backend disconnect.** The backend owns reconnection. For example, when the Codex socket closes:

1. The backend emits `BackendStatus(up=False)`, then reconnects with exponential backoff: 1 s doubling to a 60 s cap, with jitter.
2. On reconnect it emits `BackendStatus(up=True)`. The core then calls `open_session` for every active session of that backend and reconciles as in §7.8.1.
3. Throughout the outage, the status messages of running turns show "backend reconnecting".

### 7.9 Settings and errors

**Settings.** `!model` and `!effort` call `backend.update_settings` before storing
anything. The backend validates without mutating its settings or the caller's
object on refusal, raising `InvalidSetting(field, value, valid, message="")`.
The core posts an escaped one-line explanation with valid choices (for example,
“Unknown model `banana`. Valid: …”), leaves the session state and stored settings
unchanged, and continues accepting turns. Successful updates return accepted
overrides, applied to the next turn; `None` remains the agent-default sentinel.
Changing the model clears an inherited effort the new model cannot support, with
a note in the reply. An explicitly invalid `!effort` is rejected instead.
At session creation, an unsupported effort inherited from backend or project
configuration is cleared once, with a warning, so the model default can run.
An explicit `!new --effort` remains a refusal; other setting errors are unchanged.

For display, the synchronous `backend.effective_settings(session_id)` reports
known resolved values without I/O, or `None` when unknown. This is separate from
persisted overrides. No-argument commands and `!session` show a known resolved
default as e.g. `claude-opus-… (default)`; use bare `default` only when unknown.
An explicit override has no default suffix. Core tolerates an absent display
hook during rollout; never treat the agent's self-report of effort as evidence.

Codex validates against all pages of `model/list` with `includeHidden=true`,
cached per daemon connection, including each model's supported reasoning efforts.
A failed catalog lookup logs a warning and accepts updates as before; it must
not block commands. Hidden models remain valid when named explicitly.

**Errors.** Failures are rendered from `ErrorInfo.kind`:

| Kind | Message |
|---|---|
| `usage_limit` | "Usage limit reached" plus the reset time if known; suggest `!model` with a cheaper model, or waiting |
| `auth` | "Not logged in on <machine>"; give the exact login command (`codex login --device-auth`, or run `claude` and use `/login`) |
| `context_window` | Suggest `!handover` and a new topic |
| `network`, `backend_unavailable` | Retry advice |
| `sandbox`, `other` | The message, truncated to 1000 characters |

### 7.10 Concurrency

- At most `max_concurrent_turns` turns run at once per backend.
- An extra turn is held with ⏳, and its status message reads "waiting for a free slot".
- A per-session lock ensures a session never runs two turns at once.

### 7.11 Forking a session (`!fork`, issue #202)

`!fork [--name NAME] prompt` runs a side task in parallel without steering the topic's running turn: a fork is a separate session, in a separate topic and worktree, whose conversation is a copy of this one.

- **Who and where.** The usual command rules (§4.5, §4.6): an allowed sender, and a mention in a multi-party channel. In a DM the bot answers that `!fork` works in a topic with a session; in a topic where it has no session, that a prompt starts one. The options are `--name NAME` or `--name=NAME`, and `--`; anything else starts the prompt (a Markdown list, `-1 …`). No prompt, a session in `error`, and a backend without `capabilities.fork` are refused with one ⚠️ line, as is a session with no completed turn yet (the backend's reason, §8.8).
- **A session still reopening.** A `!fork` for a session not reopened yet (the message that woke the VM, replayed before the backend is up; or one sent while the backend is down) waits: it is kept in the store (`pending_forks:<bot>`), ⏳ goes on the message, and it runs once the session has reopened: after the reopen of that backend's sessions (`Runtime.finish_recovery`), and after any other reopen (a card back in a private channel, an actor restored, `!status`). It runs once: its row's transaction, or its failure, removes it, and the waiting forks run one at a time. One whose session is still closed waits for its reopen; one whose sender can't be checked (a failed user refresh) waits too. One whose sender a refresh no longer allows is dropped, and so is one whose channel Zulip refuses (4xx), with a ⚠️ in its topic where that can still be posted. When it runs, it joins another bot's fork of the same message (below) if that fork's session is still in its topic and its worktree still exists; its agent then starts from the fork's files as they are when it runs, which may hold the other agent's first turn. Otherwise it makes its own.
- **The topic.** `<topic> ⑂ NAME`, or `<topic> ⑂ n` with the smallest n ≥ 1 that no session of any bot has had in this channel, compared without case as Zulip does; a resolved topic's "✔ " is dropped. It fits `max_topic_length`: the label takes at most half, then the parent's part is shortened with "…". A `--name` whose topic is taken is refused. When several of the VM's bots get one `!fork` message (no mention in a single-human channel, or a mention of each, §4.5), they fork into one topic, the first's (#712, "One message, one fork topic", below).
- **Creation**, in the bot's message handler as for `!new`, one fork at a time per channel, holding the fork topic's creation lock (the one `!new` takes there) until step 5, and the bot's own creation lock with it (#712): a session another bot starts in that topic meanwhile waits, then joins the fork's worktree; a message to this bot there, which can come while a fork that waited runs beside the bot's listener, waits too, then reaches the fork; and a fork whose topic a session took first is refused.
  1. `WorkspaceManager.fork_base(<parent workspace>, "<parent session id>/<!fork message id>")`: the parent's files as they stand, uncommitted changes included, on a commit whose parent is the parent's HEAD (§8.8, §10.1).
  2. `prepare(<project>, "worktree", <fork topic>, <parent's subdir>, base_commit=…)`, where the project is the checkout the parent's worktree belongs to: the fork's worktree goes beside the parent's, even when the parent is in `shared` mode; then the start snapshot (§10.5).
  3. `backend.fork_session(<parent>, spec)`: `spec.settings` are the parent's persisted settings (model, effort, approval, sandbox, account) without its native bookkeeping, laid by the backend over the parent's effective ones (§8.8); a fresh context prompt (Appendix A.1), commit identity and bot.
  4. The card in the fork topic, `**zab session** · ⑂ Forked from #channel > topic, …`, linking back.
  5. In one transaction: the session row with `parent_session_id`, the `!fork` message's prompt as its pending first delivery, and the message's `inbound` record bound to it. The fork's actor delivers the prompt like any first message (§7.3): it waits for a turn slot (§7.10), and the 👀 or ⏳ goes on the `!fork` message.
  6. `⑂ forked to #channel > <fork topic>` in the parent topic (best effort; peer context leaves it out).

  A failure before step 5 removes the new worktree and posts one ⚠️ line in the parent topic; the `!fork` message is not retried, so the user sends it again.
- **One message, one fork topic (#712).** The bots of one bridge process create their forks one at a time (the channel's fork lock). The first to fork a `!fork` message makes its topic and worktree as above, and step 5's transaction also records its session as that message's fork (`fork_of_message:<message id>`). Each later bot that forks that message joins it, while that session is live (not detached or read-only) in the channel, its worktree still exists, and the joining bot has no session in its topic: holding that topic's creation lock, and its own as above, it forks its own session into that topic, in that worktree (`topic_worktree`, §10.1, with its own parent's subdirectory), the way a session another bot starts in a fork topic joins it. It takes no `fork_base` and prepares nothing, and a failure leaves the worktree, which is the first's. With `--name` it joins the named topic too. Its card says that it works in the files and on the branch of the topic's other agent; the parent topic gets one `⑂ forked to` line, the first's. Each fork's row has its own bot's session as its parent, so each fork's first answer is reported to its own parent (Report back): two lines when both agents answer. Otherwise the later bot makes a topic of its own, as above, and a `--name` taken by another message's fork, or by any other session, is refused. A bot that can't fork (no completed turn, a session in `error`, no `capabilities.fork`) posts its one ⚠️ line and the other forks alone; when the first fails before its row exists, the next one makes the topic. A mention forks only the bots it names. Bots of different bridge processes share no store, and each forks alone.
- **Afterwards** the fork topic is an ordinary topic: follow-ups, `!stop`, `!diff` (since the fork started; for a fork that joined another bot's, since it joined, its start snapshot, so a joiner that waited leaves out the other agent's first turn), `!model` and `!effort` (for the fork only), `!handover`. The other bot, mentioned there, works in the fork's worktree (§10.1), even where new topics default to `shared`. So does it after a follow-up without a mention in a single-human channel, which goes to both bots there as everywhere (§4.5, #713): after a fork of one agent, that follow-up brings the other agent into the fork topic, in a new session, without the parent conversation (it gets the topic's context since the fork, as peer context). An unmentioned `!fork` in a topic with both bots' sessions forks both, into one topic (above). The fork's work comes back by merging its branch into the parent's.
- **Report back.** When the fork's first turn has ended and its answer is posted, once per fork:
  - if a bridge restart ends a Claude fork's first turn, no answer is posted, so the fork's next turn is the one reported; a Codex turn usually survives the restart and is reported when it ends, with its answer if it ended meanwhile, and one the app-server lost is reported as interrupted, with zab's warning "stored turn no longer active" as its answer;
  - the parent topic gets one line: ⑂, the fork topic as code, "answered" (or the turn's status), and a link to the answer, posted at a confirmed card location (§4.4);
  - the parent's next input (a turn or a steer) carries that answer in an untrusted-context block beside the peer context (§4.5): one JSON line per fork (its topic, branch, status, link and answer), then `[End of fork answers]`. It is passed on once, when the input carrying it is accepted; a rejected start or steer keeps it.
  - Sizes: a fork's line is at most 8000 characters, encoded. The answers share a budget of 16000 characters of lines: an answer cut to fit is marked `"truncated": true`, and its link leads to the rest; each fork past the budget gets a short line with its topic, branch, status and link only (about 200 characters), so the block exceeds 16000 by that much per such fork. U+2028, U+2029 and U+0085 are escaped, here and in peer context's lines, as JSON escapes a newline.
  - A detached or read-only parent gets neither. Peer context leaves both fork lines out.
- **The parent is only read:** its session, its transcript or thread, its worktree and branch, and its running turn, which completes as it would have.

### 7.12 Idle Lean servers (issue #249)

A Lean tool call through `lean-lsp-mcp` starts a **Lean server**: one `lake serve`, its
`lean --server`, and one `lean --worker` per open file. With Mathlib an idle one holds about
1.3 GB of anonymous memory; the rest of its RSS is the mapped `.olean` files, shared and
reclaimable (measured on the test VM: DECISIONS, 2026-10-25). Claude's servers exit with its turn. The Codex
app-server starts one `lean-lsp-mcp` per loaded thread and keeps it while the thread stays
loaded, with its servers, so they pile up during a long stretch of work. The bridge stops them:

- **Where.** One task per bridge process (`Runtime`), as the bridge's user, every 60 s, when
  `[lean] reap_idle_minutes` is above 0 (default 30), a Codex backend is configured and `/proc`
  exists. It reads `/proc/<pid>/stat` (parent, start time, CPU time), `cmdline`, `status` (the
  owner, `RssAnon`) and `cwd`.
- **What.** A tree: a `lake serve` (argv[0]'s basename `lake`, then `serve`; the toolchain runs
  by absolute path) whose parents reach a `codex app-server` through a `lean-lsp-mcp`, with no
  Claude process among them, and every process under it. In any working directory: the topic
  worktrees, `~/lean/workspace` and the rest. Each thread has its own `lean-lsp-mcp`, so a tree
  belongs to exactly one thread.
- **Never.** `lean-lsp-mcp` itself (its thread's Lean tools need it), a `lake build` or
  `lake env lean` (neither is a `lake serve`), anything under Claude, another user's process,
  and a tree with any process under its `lake serve` other than `lean --server` and
  `lean --worker`, which is logged once and left alone whole.
- **Idle.** No process joined or left the tree, and at every pass of the last
  `reap_idle_minutes` their summed CPU time (utime, stime and the children's) grew by less than
  an allowance: `IDLE_TICKS_PER_PROCESS`, 4 clock ticks (0.04 s) a minute for each process of
  the tree, at least `IDLE_TICKS_MIN`, 10 (0.1 s), pro rata. A pass that gained nothing is never
  busy. A busy pass starts the window again, and so does a bridge restart. Idle trees are not
  still: on the test VM they gained about 1 tick a minute per process, a 3-process tree about 1
  and a 5-process tree about 3.5, so a rule of no tick at all reaped none (live checks of #635,
  Oct 25); the allowance is about 4 times that. A first `lean_diagnostic_messages` on a file
  cost about 2,000 ticks, one more `example` re-checked in an open file about 2. So the
  lightest calls look idle, and their server can be stopped while a thread still uses it: its
  next call starts a fresh one, which re-elaborates the open file (10 to 20 s for a small
  file).
- **Stopping.** The tree is read again just before: if a process joined or left, or it gained
  any CPU time since the pass's scan, tens of milliseconds earlier, it is busy and kept.
  Each process then gets SIGTERM, the `lake serve` first, and after 5 s whatever is left gets
  SIGKILL. Before each signal its PID is pinned with a pidfd, and its start time and argv are
  read again: a PID that names another process by then gets nothing, and the signal goes
  through the pidfd. Without a pidfd (no file descriptor left, a kernel before 5.3) nothing is
  sent, and a warning is logged: a plain `kill` after the check could reach the PID's next
  owner. A Python built without `os.pidfd_open` (uv's standalone builds) makes the system
  calls through libc. Stopping only `lake serve` would leave the rest running, orphaned, with
  the memory.
- **Afterwards.** The thread's `lean-lsp-mcp` stays, and its next Lean tool call starts a fresh
  server, and the call succeeds (verified: DECISIONS, 2026-10-25): about 10 s for `import
  Mathlib` once the boot's read of Mathlib is done.
- **The log.** One line per tree stopped, `event="lean_reaped"`: the time idle and the CPU
  time used in it, the working
  directory, the PIDs signalled by kind (`lake serve 4142, lean --server 4191, lean --worker
  4199`), the anonymous memory freed, and how many needed SIGKILL. Not the RSS: it is mostly
  the shared `.olean` mappings, which a sum over processes counts once per process.

`!restart codex` (§4.6) still frees every server at once, at the cost of running Codex turns.

### 7.13 Side questions (`!btw`, issue #716)

`!btw question` asks the topic's agent something about the conversation, as Claude Code's
interactive `/btw` does: answered from what the conversation already holds, without tools, while a
running turn goes on, and kept out of every history.

- **Who and where.** The usual command rules (§4.5, §4.6): an allowed sender, the agents the
  message addresses, and a mention in a multi-party channel. Each addressed agent with a session
  in the topic answers about its own conversation; an unmentioned `!btw` reaches all of the VM's
  bots, and one without a session there stays quiet, as for any command. In a DM (a typed
  address, `@claude !btw`, too) the bot answers that `!btw` works in a topic with a session; in
  a topic where no bot has one, one bot answers that a prompt starts one (the reply of rule 7,
  from its lead bot when unmentioned). Refused with one ⚠️ line (§4.9): no question, a backend
  without `capabilities.side_question`, a session in `error`, and a session with no completed
  turn yet (the backend's reason). A session still reopening (the message that woke the VM,
  replayed before its backend is up) is waited for, up to 120 s, then refused like one in
  `error`.
- **Beside the session, never through it.** It runs as a task of the bot's bridge, not in the
  session's actor: the bot handles its other messages meanwhile, `!stop` among them. It takes no
  turn slot (§7.10), creates no session, turn, branch or snapshot, and sends nothing to a running
  turn. One side question per session at a time: a second waits for the first, with ⏳ on its
  message. 👀 goes on the message when its answer starts. The backend has 120 s; then it stops what
  it started, and the bridge posts one ⚠️ line, as for any failure. A bridge restart drops a side
  question in flight; it is not replayed. Its message is taken in (`inbound` disposition
  `side_question`, its time when the answer starts), so idle stop (§0.1; `zab status`, §13.1)
  counts it as activity and the VM doesn't power off in the middle of a side answer.
- **What it sees and can do.** `Backend.side_question(native_id, question, timeout_s)` answers from
  a throwaway copy of the conversation: at least through the last ended turn, and the running
  turn's progress where the backend can include it safely. Nothing it does enters the session's
  history, its settings or the workspace. No tools where the backend can drop them, else the
  narrowest sandbox, and never an approval. Codex: §8.10; Claude: §9.10.
- **The answer** is posted in the topic, rendered as a final answer is (§11: links, uploads, math,
  splitting), but with no turn behind it: no snapshot (§10.7) and no turn files (§11.3). Every part
  starts with `💬 [btw](<link to the !btw message>):`; one taking at least 60 s mentions its asker
  there (`💬 [btw](…) @**Name|id**:`), whatever `mention_after_s` says (§4.8), and a faster one never
  does: a side question can't run longer than its 120 s, the setting's default. It is never a card.
- **Out of every history.** Peer context (§4.5) and `zab history` (§5.6) leave out every side
  question, whoever asked it and however addressed (after its leading mentions, typed `@claude`
  and `@codex` included) or typed (`!Btw`, `` `!btw …` ``, any code span that `as_command`
  reads as one, §4.6), and every part of an answer (its marker). A side question is read as
  the routers read a command (`peer_context.side_message`, for both): its leading addresses
  off, the organization's bots' completed mentions (`bot_address`, from `GET users`) and typed
  `@claude`/`@codex`, then `btw.is_btw(as_command(…))`. A person's mention is no address, so
  `@**Bob** !btw …` is a prompt, as every bridge runs it. That is what the owner's bridge runs
  as a side question, and another participant's too, to their own bots or unaddressed, which no
  bridge of the VM runs (nor, unaddressed, any bridge: a channel with other people needs a
  mention). Where the bots aren't known (`zab history` when `GET users` fails), any completed
  mention counts as an address. A side answer is no turn, so a fork's report (§7.11) never
  carries one.
- **Earlier side questions** are not replayed: each is answered alone, unlike `/btw`, which shows
  the newest 20. Turning a side answer into a fork (`/btw`'s `f`) is `!fork`'s job.

## 8. Codex backend

The reference is the app-server README for the pinned Codex version, together with its generated schema (§8.3). Every method and notification name below comes from that README. Treat the parameter details as **VERIFY**.

### 8.1 Deployment modes

**`connect = "daemon"` (default).**

- A long-lived `codex app-server --listen unix://` runs as a systemd user service (§13.3), using the default socket `$CODEX_HOME/app-server-control/app-server-control.sock`. zab connects to it.
- A `codex` TUI started on the same machine attaches to a running daemon on the default socket; `codex --remote unix://` forces that.
- Two benefits:
  1. Threads and running turns survive bridge restarts.
  2. The same thread can be driven from Zulip and from a terminal (G6).
- If the daemon is not running at startup, the backend is marked down (§7.1). The bridge must not start its own daemon; the service manager owns it.

**`connect = "spawn"`.**

- zab spawns `codex app-server` itself as a child process (stdio, newline-delimited JSON) and owns it.
- There is no co-presence, and running turns die with the bridge.
- It exists for machines where running a daemon is impractical, and as a fallback if the socket transport misbehaves.

Only one app-server process may own a `CODEX_HOME` at a time, because paginated threads allow a single writer process. `zab doctor` must warn when it finds more than one app-server process for the same user.

### 8.2 Connection

**Daemon mode.**

- Use a websocket over the unix socket: `websockets.asyncio.client.unix_connect(path, "ws://localhost/")`.
- Set `max_size=None`. The library's 1 MiB default is far too small: resume responses can exceed 50 MB.
- Enable ping and pong with a 20 s interval.

**Spawn mode.** One JSON object per line on stdin and stdout.

**Handshake.**

```json
{"id": 0, "method": "initialize", "params": {
  "clientInfo": {"name": "zab", "title": "Zulip Agent Bridge", "version": "<zab version>"},
  "capabilities": {"experimentalApi": true,
    "optOutNotificationMethods": ["item/agentMessage/delta", "item/reasoning/summaryTextDelta",
      "item/reasoning/summaryPartAdded", "item/reasoning/textDelta", "item/commandExecution/outputDelta",
      "item/plan/delta", "item/fileChange/patchUpdated", "command/exec/outputDelta"]}}}
```

Then send the `{"method": "initialized"}` notification. Record the `codexHome` returned by `initialize`, and log the user agent.

**Reader.**

- One reader task demultiplexes three kinds of message:
  1. responses, matched by `id` to pending futures;
  2. notifications, which go to the event pump;
  3. server-initiated requests (those that have both `id` and `method`), which go to the approval handler.
- The reader must never block.
- Error `-32001` ("Server overloaded; retry later.") is retried with jittered exponential backoff, at most 5 attempts.

The opt-out list keeps traffic small. The bridge does not stream text, and the final agent message arrives complete in `item/completed`.

### 8.3 Version pinning

- The pinned Codex CLI version is recorded in `docs/DECISIONS.md` and checked by `zab doctor`: a different version produces a warning, not a failure.
- For each pinned version, commit the output of `codex app-server generate-json-schema --out docs/protocol/codex-<version>/`.
- Contract tests replay recorded JSON-RPC fixtures (§15.2) and validate them against that schema.
- Codex on the workshop VMs is upgraded only with a bridge release whose pin is the new version, with its schema and fixtures (`docs/testing/codex-workshop-settings.md`).

### 8.4 Backend operations

| Contract method (§ types.py) | App-server calls |
|---|---|
| `create_session(spec)` | `thread/start` with `cwd`, `config` (the identity policy of §5.5), `serviceName: "zab"` and `developerInstructions`, the context prompt (Appendix A.1; the field is in the 0.157.1 schema: DECISIONS, 2026-09-27), plus non-empty `model`, `approvalPolicy`, `sandbox` (the legacy shorthand; switch to the experimental `permissions` profile only after verifying it). Then `thread/name/set` with `{threadId, name}`. |
| `open_session(id, spec)` | `thread/resume` with `{threadId, excludeTurns: true}` and non-empty `developerInstructions` from the spec. Codex 0.157.1 does not reliably apply new resume instructions to existing history: also prefix the first accepted start/steer input after opening with `<zab-context>…</zab-context>` (§A.1), retaining the prefix across rejected submissions. Then `thread/turns/list` with `{threadId, limit: 2, sortDirection: "desc", itemsView: "summary"}`. From these, fill `SessionInfo.active_turn_id` (a turn still in progress) and `SessionInfo.last_turn` (the latest completed turn: id, status, final text, error) for reconciliation (§7.8.1). The event stream runs meanwhile: a `turn/completed` it handled during the call, for a turn the list shows running or not at all, is taken as newer than the list, and that turn is the `last_turn`, with its answer (issue #306). A completion handled before the call never overrides the list. A turn missing from the list because another client ran two newer ones within the same call is not told apart; reconciliation matches turns by id, so no answer goes to another turn. |
| `list_sessions(cwd)` | `thread/list` with `{cwd, limit, sortKey: "updated_at", sourceKinds: <all kinds>}`. Without `sourceKinds` the server returns only interactive sessions. The kinds are the 0.157.1 schema's: `cli`, `vscode`, `exec`, `appServer`, the `subAgent…` kinds and `unknown` (DECISIONS, 2026-09-27). |
| `start_turn(id, input)` | `turn/start` with `{threadId, clientUserMessageId: input.client_id, input: [{type: "text", text}, …{type: "localImage", path} for image attachments]}`, plus any pending setting overrides (`model`, `effort`). Returns `turn.id`. |
| `steer(id, turn, input)` | `turn/steer` with `{threadId, expectedTurnId: turn, clientUserMessageId, input}`. The server rejects this when no turn is active or the turn cannot be steered; return that as an error. |
| `interrupt(id)` | `turn/interrupt` with `{threadId, turnId}` for the active turn. The turn then ends with `turn/completed`, status `interrupted`. |
| `respond(request_id, decision)` | A JSON-RPC response to the server request: `{"id": <request id>, "result": {"decision": "accept" \| "acceptForSession" \| "decline" \| "cancel"}}`. |
| `update_settings(id, s)` | Keep the settings and send them as overrides on the next `turn/start`, where they persist for later turns. `thread/settings/update` (experimental) may be used instead once verified. |
| `rename(id, name)` | `thread/name/set`. |
| `fork_session(id, spec)` | §8.8: `thread/turns/list` `{threadId, limit: 2, sortDirection: "desc", itemsView: "notLoaded"}` for the newest turn not `inProgress`, then `thread/fork` with `{threadId, lastTurnId: <that turn>, cwd: spec.cwd, excludeTurns: true, developerInstructions, config: <the identity policy, §5.5>}` and the effective `model`, `approvalPolicy`, `sandbox`; then `thread/name/set` on the child. Its turns carry its settings, `effort` included, as the parent's turns carry theirs. |
| `side_question(id, question, timeout_s)` | §8.10: `thread/turns/list` for the boundary, `thread/items/list` for a running turn's progress, `config/read` for the MCP servers, plugins and apps to turn off, then `thread/fork` with `ephemeral: true`, `mcpServerStatus/list` for its thread (every server `disabled`, else refused) and one `turn/start` on it, read-only with approval `never`, then `turn/interrupt` if it still runs and `thread/unsubscribe`. |
| `workspace(cwd)` | `LocalWorkspace(cwd)` in Phase 1, since zab and Codex run as the same user. See §12.3 for the cross-user variant. |
| `doctor()` | Check that the socket connects and `initialize` succeeds; `codex --version` matches the pin (a warning otherwise, §8.3); the account is signed in (`account/read` with `refreshToken: false`, which reports the account: DECISIONS, 2026-09-27); there is exactly one app-server process. |

### 8.5 Event mapping

| App-server message | Contract event |
|---|---|
| `turn/started` | `TurnStarted(turn.id)` |
| `item/started`, `item/completed` | `ItemStarted` / `ItemCompleted` with an `ItemInfo` (below) |
| `turn/plan/updated` | `PlanUpdated(steps)` |
| `thread/tokenUsage/updated` | `UsageUpdated`, carrying the thread's cumulative total. The per-turn figure for `TurnCompleted.usage` is the difference between the totals at the turn's end and at its start. |
| `turn/diff/updated` | Keep the latest diff per turn; attach it to `TurnCompleted.native_diff` |
| `turn/completed` | `TurnCompleted`: `status` from `turn.status`; `error` from `turn.error` (below); `final_text` = text of the last `agentMessage` item completed in this turn. Do not read `turn.items`: the README says it is currently always empty, so track items yourself. |
| `error` | Remember it for the turn's `TurnCompleted`. If the error is not tied to a turn, emit an `error` `Notice` that says Codex reported it (Appendix B.4). |
| `item/commandExecution/requestApproval` (server request) | `ApprovalRequested(kind="command", summary=command, detail=cwd + reason, decisions from availableDecisions)` |
| `item/fileChange/requestApproval` (server request) | `ApprovalRequested(kind="file_change", detail=` the diffs of the pending `fileChange` item from its `item/started``)` |
| `serverRequest/resolved` | `ApprovalResolved(request_id)` |
| `thread/name/updated` | `SessionChanged(title=…)` |
| `thread/status/changed`, `thread/closed` | Internal bookkeeping (§8.6) |
| any other server request (`item/tool/requestUserInput`, `attestation/generate`, MCP elicitation, …) | Respond with a JSON-RPC error "not supported by zab" and emit an `info` `Notice` that says so in plain words (Appendix B.4): nothing failed for the user. Relaying `requestUserInput` (posting the questions, with the owner's next message as the answer) is not built. |

`ItemInfo` construction:

| Item type | `ItemInfo` |
|---|---|
| `userMessage` | `USER_MESSAGE`; `text` joined from the text parts; `client_id` from `clientId`. A steer's item comes when Codex takes the input in, not with the `turn/steer` response: in the `steer` fixture (0.157.1), 2.9 s after the response, as the running command completed (DECISIONS, 2026-10-25) |
| `agentMessage` | `AGENT_MESSAGE`; `text` |
| `reasoning` | `REASONING`; `text` = the summary parts joined; `title` = the first bold heading or first line |
| `commandExecution` | `COMMAND`; `title` = the command, shortened to 120 characters; `exit_code`; `duration_ms`; `status` (`declined` maps to `DECLINED`) |
| `fileChange` | `FILE_CHANGE`; `title` = the changed paths; `detail` = the concatenated diffs |
| `mcpToolCall` | `TOOL_CALL`; `title` = `server.tool` |
| `collabToolCall` | `SUBAGENT` |
| `webSearch` | `WEB_SEARCH`; `title` = the query |
| `contextCompaction` | `COMPACTION` |
| `plan` | `PLAN` |
| anything else | `OTHER`, with the type name as `title` |

`codexErrorInfo` → `ErrorKind`:

| Codex value | `ErrorKind` |
|---|---|
| `usageLimitExceeded`, `sessionBudgetExceeded`, `rateLimitExceeded` | `USAGE_LIMIT` |
| `contextWindowExceeded` | `CONTEXT_WINDOW` |
| `unauthorized` | `AUTH` |
| `httpConnection…`, `responseStream…`, `responseTooMany…` | `NETWORK` |
| `sandboxError` | `SANDBOX` |
| anything else | `OTHER` |

The values are the 0.157.1 schema's, in camelCase; an object variant is read by its one key (DECISIONS, 2026-09-27, "Approval and error shapes").

### 8.6 Co-presence with the Codex TUI

- A `userMessage` item whose `clientId` is missing or unknown did not come from zab. Emit it as an `ItemCompleted(USER_MESSAGE)` with no `client_id`; the core mirrors it into the topic as `💻 *via terminal:*` followed by the quoted text.
- A turn started from the TUI still emits `turn/started` to zab, because zab is subscribed to the thread. zab handles it like its own turns: status message, then final answer.
- Both the TUI and zab, subscribed to one thread, received its approval request on 0.157.1 (DECISIONS, "M1 live verification", 2026-09-27). The rules:
  - zab answers only requests for turns it started, unless `answer_external_approvals = true`.
  - If the TUI answers first, `serverRequest/resolved` arrives and the approval message is edited to "resolved elsewhere".
- If `thread/closed` arrives for a thread with no active turn, the next delivery calls `open_session` again first.

### 8.7 Attaching existing threads

- `!attach` with no ID lists the ten most recently updated threads for the project's `cwd`: id, name, preview and updated time.
- `!attach <id>`:
  1. `open_session`;
  2. post the card;
  3. post a recap: the thread's last user message and last final answer, from `thread/turns/list` with `itemsView: "summary"`;
  4. set the session state to `idle`, or to `running` if a turn is active.
- In daemon mode this lets a session begun at the terminal continue from Zulip, and vice versa.

### 8.8 Session fork contract (issue #202)

`Capabilities.fork` defaults to false. An enabled backend implements
`fork_session(native_id, spec) -> SessionInfo`, creating an independent child at
its last completed native turn boundary without changing the parent. A turn that is no longer running has ended, whether it completed or a bridge restart or `!stop` interrupted it, so a fork copies it like any other; only a turn still running is left out (#227; DECISIONS, 2026-10-11, "The fork boundary"). The child
uses `spec.cwd`, inherited effective settings and fresh bridge context; core starts
its first prompt separately through the normal turn limit. A backend unable to
exclude an active partial turn refuses safely. No completed boundary also refuses.

For Codex 0.157.1, use `thread/fork` with `lastTurnId`, `cwd` and explicit settings;
native forks do not reliably inherit model, sandbox or effort (DECISIONS verification).
The Codex backend takes as boundary the parent's newest turn that is not `inProgress`
(`thread/turns/list`, §8.4); a running turn is never one (`lastTurnId` "cannot be in progress"),
and the fork itself freezes the boundary. The child's settings are the parent's effective ones
(the model and effort it resolved, its approval and sandbox) with the spec's explicit values on
top: model, approval and sandbox go on `thread/fork`, and all of them, effort included
(`ThreadForkParams` has none), on each of the child's `turn/start`, as the parent's turns send
theirs. An approval or sandbox the parent runs with without having set it (from its start or
resume, or a later `thread/settings/updated`) goes on `thread/fork` only, where the server
builds the whole policy as for `thread/start`. The child also gets the bridge context as developer instructions and, as a
resumed thread does, on its first input (§8.4), and the identity policy of §5.5 in `config`. The
parent is only read: a closed parent is reopened as its own next turn would reopen it, and
nothing else is sent for it.
The workspace contract adds `prepare(..., base_commit=None)`: a specified commit
is used only for a new worktree, with ordinary discard-on-failure behavior. A local
fork snapshot must retain parent ancestry for merge-back; published snapshots stay
parentless. `WorkspaceManager.fork_base(ws, name)` builds it: the parent workspace selected as
for §10.7, committed with the parent's HEAD as parent under `refs/zab/forks/<name>` (never
pushed), keeping HEAD's version of any tracked file a snapshot would drop, so a merge-back never
deletes one; `prepare(..., base_commit=<that sha>)` then branches the fork's worktree from it. The `!fork` command (§7.11) drives this contract.

### 8.9 Pitfalls checklist

- Set websocket `max_size=None`. Resume with `excludeTurns: true`.
- Opt out of delta notifications, and never block the reader.
- The daemon's environment must not contain `OPENAI_API_KEY` or `CODEX_ACCESS_TOKEN`: an access token in the environment silently overrides the ChatGPT login. Give the daemon an explicit `PATH` in its unit file.
- `thread/start` with a `cwd` and a writable sandbox marks the project trusted in the user's `config.toml`. That is expected.
- Experimental methods (`thread/turns/list`, `thread/settings/update`, `excludeTurns`) need `experimentalApi: true` and may change between versions. Pin the version.
- Never call `thread/shellCommand` or `process/spawn`: both run unsandboxed with full access. `command/exec` is not used in Phase 1 either. §12.3 may reintroduce it later, restricted to a fixed allowlist of git argument vectors.

### 8.10 Side questions (`!btw`, issue #716)

`capabilities.side_question` is on. A side question (§7.13) is an ephemeral fork and one turn on
it, read-only, with approval `never`:

1. The boundary as for a fork (§8.8): the newest turn of `thread/turns/list` not `inProgress`;
   none refuses ("There is no completed turn to ask about yet."). A closed parent is reopened as
   its own next turn would reopen it.
2. A running turn can't be copied (`lastTurnId` "cannot be in progress"), so what it has done so
   far is told in words: `thread/items/list` for that turn (newest 50 items, `desc`), each a line
   (the user's messages and the agent's whole, a command or tool by its title, reasoning left
   out), the newest kept within 6000 characters, appended to the frame. Unreadable, it is left out.
3. `thread/fork` with `threadId`, `lastTurnId`, `ephemeral: true` ("should not be materialized on
   disk"), `excludeTurns`, the parent's `cwd`, `approvalPolicy: "never"`, `sandbox: "read-only"`,
   the parent's resolved model, the bridge context plus the frame (`SIDE_QUESTION_FRAME`) as
   `developerInstructions`, and a `config` that turns off what runs outside the sandbox
   (`codex-rs/core/config.schema.json` at rust-v0.157.1): `mcp_servers.<id>.enabled = false`,
   `plugins.<name>.enabled = false` and `apps.<id>.enabled = false` for every MCP server, plugin
   and app entry of the cwd's effective config (`config/read`), `apps._default.enabled = false`
   (connectors), `features.apps = false` and `web_search = "disabled"`. The feature flag drops
   the built-in `codex_apps` MCP server, which 0.157.1 registers for a ChatGPT sign-in while
   `features.apps` is on (its default), whatever `apps._default` says. A name with a `.`, which
   a dotted key can't address, refuses the side question. These tables are no typed field of
   `config/read`'s answer, so the bridge logs what it found (`side_question_config`, names
   only), and then checks.

   **Fail closed:** `mcpServerStatus/list` with the fork's `threadId` (`detail:
   "toolsAndAuthOnly"`). Any server whose `runtimeStatus` isn't `disabled` (connected,
   starting, not started, failed, unknown), or a listing that can't be read, refuses the side
   question before its turn, with one ⚠️ line naming the servers; the fork is unsubscribed. The
   states are logged (`side_question_mcp`).
4. `turn/start` on the fork: the question, with the frame in `<zab-context>` (§8.4's first-input
   fallback); `approvalPolicy: "never"`, `sandboxPolicy: {"type": "readOnly", "networkAccess":
   false}`, the parent's model and effort.
5. The answer is the turn's last agent message once it completes. A failed or interrupted turn is
   one plain line: the sign-in hint for `AUTH`, otherwise the kind and the error's first line,
   redacted.
6. Then `turn/interrupt` if the turn still runs, and `thread/unsubscribe`, each best effort
   within 10 s of its own; the fork is forgotten, its stragglers (`thread/closed`) dropped.
   `thread/fork` and `turn/start` wait for their answer without the RPC's 120 s deadline, until
   a disconnect: should the side question give up first (its timeout, a shutdown), a fork or
   turn the server makes anyway is stopped and unsubscribed when its answer comes.

**What can still run.** Codex can't drop its built-in tools as Claude does. With that config,
what the fork's turn can reach is the shell and file-change tools, inside the read-only sandbox
without network: a command may read the workspace, but writes nothing and reaches nothing. MCP
tools, which run outside the sandbox, are gone with their servers, as are the plugins' tools
and hooks, the apps and web search; a fork whose MCP servers aren't all `disabled` for its
thread is refused before its turn (step 3), so none runs a turn with one, whatever `config/read`
reported. Hooks of the user's own `config.toml` still run, as for every turn. On top of that:

- **The fork's messages never reach the event stream** or the parent's bookkeeping. They go to
  the side question, routed by `threadId`.
- **An approval it asks for anyway is declined:** `decline` for a command or a file change, and
  JSON-RPC -32601 for any other request, also after the answer. It is never posted.
- **An item other than a user or agent message, a hook's prompt, reasoning, a plan or a
  compaction stops it.** A command, file change, tool call, web search or sub-agent ends the
  side question with `turn/interrupt` and the ⚠️ line "Codex reached for a tool … so the bridge
  stopped it". The item may have run by then, read-only.

**Verified** on the test VM with Codex 0.157.1 (DECISIONS, 2026-11-02, participant run
`20261102T044623Z-058db488`):
- the overrides are accepted. `config/read` reported the MCP servers, the fork's listing showed
  `lean-lsp` and `lmfdb` `disabled` and no `codex_apps`, and no `lean-lsp-mcp` was started for
  it;
- asked to run a command, write a file, run Lean code that writes one, or query the LMFDB, the
  turn answered each time without a tool, and nothing was written. So the tool stop wasn't
  exercised live: whether a read-only command runs before its interrupt lands stays
  unobserved (the sandbox keeps it read-only either way);
- the fork left no rollout, and the topic's next turn saw none of the questions;
- `thread/items/list` reads a running turn: asked mid-turn, the side answer named the `sleep`
  that the running turn was running.

## 9. Claude backend

The backend drives the unmodified `claude` binary through its documented headless mode. It must not use the Agent SDK, must not read or write credential files, and must not intermediate sign-in (see `docs/DECISIONS.md` D5).

### 9.1 Invocation

Each turn runs as one process:

```
env: CLAUDE_CONFIG_DIR=<account dir> (left unset for the default `~/.claude`, see §9.2);
     removed: the API-key, token and provider variables (ANTHROPIC_API_KEY,
     ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_BASE_URL, CLAUDE_CODE_USE_BEDROCK,
     …), CLAUDE_CODE_EFFORT_LEVEL and the nested-session markers;
     set: the commit identity and ZAB_BOT (§5.5, §5.6), and
     ENABLE_CLAUDEAI_MCP_SERVERS=false unless `claudeai_connectors = true`
cwd: <workspace>
claude -p --output-format stream-json --verbose --permission-prompts none
       (--session-id <uuid> | --resume <uuid>
        | --resume <cut copy> --fork-session --session-id <uuid>)      a fork's first turn, §9.3
       [--input-format stream-json --replay-user-messages]              steer = true, §9.7
       [--permission-mode <mode>] [--model <model>] [--effort <level>]
       [--allowedTools <…>] [--disallowedTools <…>]
       --append-system-prompt "<Appendix A.1>"
       [--settings '{"attribution":{"commit":""}}']                  with a commit identity, §5.5
stdin: the turn's text (UTF-8), then EOF; with steering, one stream-json line per message (§9.7)
```

- **Start the process in its own session and process group** (`start_new_session=True`), so that stop and shutdown can kill the whole tree.
- **Scrub the API-key variables, always.** In `-p` mode an `ANTHROPIC_API_KEY` in the environment takes precedence over the subscription login. Phase 1 has no API-key mode for Claude.
- **Settings (§7.9).** `--effort` takes `low`, `medium`, `high`, `xhigh` or `max`. The CLI silently ignores any other value, and `CLAUDE_CODE_EFFORT_LEVEL` overrides the flag, so the backend validates the level and scrubs that variable (DECISIONS, 2026-10-04).
  - `--model` takes one of the CLI's aliases or a full `claude-…` name.
  - Before a new model is stored, the backend tries it with one short turn: `claude -p --no-session-persistence --tools "" [--model M]`, in a fixed empty directory, on the session's account. A missing model (404, "There's an issue with the selected model") is refused. Otherwise the check's `system/init` gives the resolved name and `per_turn_effort_active`, which is false for models that take no effort, such as haiku.
  - Each turn's `system/init` repeats `TurnStarted` with the resolved model (Appendix B.2 amendment). A turn that still fails on its model says so and points to `!model`.
- **stdout** is newline-delimited JSON. Parse each line independently; log non-JSON lines and otherwise ignore them.
- **stderr**: keep the last 100 lines per turn for classifying errors (§9.4) and for the log.
- Checked on the pinned version (DECISIONS, 2026-09-27):
  - `--output-format stream-json` requires `--verbose` in `-p` mode.
  - `--session-id` accepts a UUID the caller chooses.
  - `--resume` reuses the session id unless `--fork-session` is given.
  - The permission modes are `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk` and `plan`.
  - `--effort <level>` exists, with the levels above.

### 9.2 Accounts

- `accounts` maps a name to a `CLAUDE_CONFIG_DIR`. A session records its account at creation.
- For the CLI's default directory, `~/.claude`, the backend does **not** set `CLAUDE_CONFIG_DIR`: with the variable set, the CLI keeps its global config in `<dir>/.claude.json` rather than `~/.claude.json`, so bridge sessions would not see the MCP servers, settings and onboarding state of the interactive `claude` on the same machine (the workshop VMs). Any other directory is passed as before.
- Session transcripts live under that account's directory, so a session can only be resumed with the same account in Phase 1. Moving a session between accounts is M4 (§18).
- The card shows the terminal resume command: `cd <workspace> && CLAUDE_CONFIG_DIR=<dir> claude --resume <uuid>` (without the variable for the default directory). It carries a warning not to resume the session in a terminal while zab is using it; the two copies of the context would diverge.

### 9.3 Session ids

- For the first turn, zab chooses a UUID and passes `--session-id`. Later turns pass `--resume <uuid>`.
- The backend reads `session_id` from the `system/init` event and from the `result` event. If it differs from the stored id, emit `SessionChanged(session_id=<old>, new_session_id=<new>)`; the core updates the store.
- Never pass `--fork-session`, except for a fork's first turn (`!fork`, #202, §8.8): `--resume <cut copy> --fork-session --session-id <child>`. The copy holds the parent's transcript up to the size it had when its running turn started (all of it when idle), with the parent worktree's paths pointing at the child's, under a fresh id derived from the child's. It is deleted once the child has its own transcript. The parent's transcript is never written.

### 9.4 Event mapping

| stream-json event | Contract event |
|---|---|
| `system` with subtype `init` | `TurnStarted(turn_id)`. The turn id is generated in `start_turn()` before the process starts, as `<session uuid>:<uuid4 hex>`, so it stays unique across bridge restarts. Also record the model and permission mode. |
| `assistant`, `text` content block | `ItemCompleted(AGENT_MESSAGE, text)`: commentary during the turn |
| `assistant`, `tool_use` block | `ItemStarted`, with the kind chosen from the tool name (below) |
| `user`, `tool_result` block | `ItemCompleted` for the matching `tool_use_id`; status `FAILED` if `is_error` is true |
| `user` with `isReplay`, the echo of a steer's uuid (§9.7) | `ItemCompleted(USER_MESSAGE)` with the steer's `client_id`: the model took it in (issue #654). Once per steer: a `result` that lists the steer in `user_message_uuids` without its echo reports it too. The prompt's echo is no event |
| `assistant`, `tool_use` of `TodoWrite` (older versions), `TaskCreate` or `TaskUpdate` (2.1.283) | `PlanUpdated`, from the todo list (`content`, `status`) or the tasks (`subject`, `status`; DECISIONS, 2026-09-27) |
| `result` | `TurnCompleted`. `final_text = result`. Status is `COMPLETED` unless `is_error` is true or the subtype starts with `error`, in which case it is `FAILED`. The turn's usage and `total_cost_usd` go into `TurnCompleted.usage`, which is per turn. The backend also emits `UsageUpdated` with a running total kept in memory; the core falls back to summing per-turn usage after a restart. A non-empty `permission_denials` produces one warning `Notice` for all of the result's denials, in plain words (Appendix B.4, issue #677): Claude Code, the program Claude runs in, blocked them under its safety rules; they needed approval, which can't be given from Zulip, so they did not run; Claude was told and can carry on. A line follows for each denial, in a code span: a `Bash` call's command, its start as in item titles, or else `Tool(title)`. When the denied call's `tool_result` (the same `tool_use_id`, `is_error`) holds `What required approval: `, Claude Code's reason comes under it: the first two lines after that, NULs dropped and redacted, on one line, cut after a word at 300 characters and then, if it was cut, back to the end of its last whole sentence when it has one. A result that lists a denial a previous result of the process reported does not report it again. |
| process exits without a `result` | `TurnCompleted(FAILED)`, with the error classified from stderr: login or 401 → `AUTH`; "usage limit" or "rate limit" → `USAGE_LIMIT`, with the reset time parsed if present; otherwise `OTHER` |

**OAuth refresh race.** The access token may have expired while another `claude` process holds the OAuth refresh lock, or exited holding it. The CLI then fails the turn with an API error that it calls transient (`error: "server_error"`, "Failed to refresh OAuth token: another Claude Code process is refreshing it or exited mid-refresh…"). The account is still signed in, so the error is `BACKEND_UNAVAILABLE` with retry advice, never `AUTH`. The CLI reports the loss only when the lock went untouched for 7.5 s: its holder is gone, and the lock stays until it is stale, 60 s after its last touch (a live holder's new token is picked up instead; DECISIONS, 2026-10-18). A turn that fails this way before any item or plan step runs again in a new process, at most twice, resuming the session: 2–5 s later, then no sooner than 54 s after the first loss, when that lock is stale for sure. The core sees one turn (DECISIONS, 2026-10-14). If every try loses, it posts the error's one plain sentence, without the kind word.

**The bridge's own `auth status`.** The doctor (§13.1; `!status` and `!vm` ask it) runs `claude auth status`, which may refresh the token too. It never runs for an account while one of that account's turns is starting, from the turn's start to the model's first answer, retries included: the doctor gives the last answer, with its age, instead. A turn waits for a running `auth status` of its account, at most 15 s, before its process starts. `auth status` runs to its end even when the doctor's caller stops waiting. On its own 30 s limit it gets SIGTERM, and SIGKILL 10 s later: on SIGTERM the CLI runs its own shutdown, waits up to 2 s for a refresh it holds, and exits within a failsafe of at least 5 s, and the lock library's exit handler removes the lock at that exit. A SIGKILL mid-refresh leaves the lock behind for a minute (#450).

Tool name → `ItemKind`:

| Tool | Kind | `title` |
|---|---|---|
| `Bash` | `COMMAND` | the command |
| `Edit`, `MultiEdit`, `Write`, `NotebookEdit` | `FILE_CHANGE` | the file path |
| `Read`, `Grep`, `Glob`, `LS` | `TOOL_CALL` | `Read <path>`, `Grep <pattern>`, and so on |
| `WebSearch`, `WebFetch` | `WEB_SEARCH` | query or URL |
| `Task`, `Agent` | `SUBAGENT` | description |
| `mcp__<server>__<tool>` | `TOOL_CALL` | `<server>.<tool>` |
| anything else | `OTHER` | the tool name |

The event shapes were checked against fixtures recorded with the pinned version (§15.2; DECISIONS, 2026-09-27, "Event shapes").

### 9.5 Permissions

- `capabilities.approvals = false` in Phase 1: headless mode cannot ask. The permission mode is chosen per backend, project or session through the `approval` setting. The backend always passes `--permission-prompts none`, which makes the CLI deny anything that would have prompted instead of hanging the turn.
- On dedicated machines (workshop VMs, a machine kept for the agents), use `bypassPermissions`.
- Elsewhere, use `dontAsk` together with `allowed_tools`, whose default for this mode is `["Read", "Glob", "Grep", "Edit", "Write"]`: reads and edits are allowed, and every other tool call that would have prompted is denied. (Commands the CLI classifies as safe, such as `echo` or `ls`, never prompt and still run.) Denials surface as a `Notice` after the turn, and the owner can widen `allowed_tools` in the config. Allowing an interpreter (`python`, `gp`, `bash`, or the `Bash` tool as a whole) amounts to allowing everything, so on a personal machine the real protection is a separate user account, not the allowlist. `acceptEdits` restricts nothing in headless mode on Claude Code 2.1.283 (`docs/DECISIONS.md`).
- Interactive approvals through a permission-prompt MCP tool are Phase 2.

### 9.6 Stopping and shutdown

`interrupt`:

1. Send SIGINT to the process group.
2. Wait 5 s, then send SIGTERM.
3. Wait another 5 s, then send SIGKILL.
4. Emit `TurnCompleted(INTERRUPTED)`.

The transcript up to that point stays in the session file, and the next turn resumes from it. On bridge shutdown, the running Claude process groups are terminated the same way.

The process group id of a running turn is reported to the core, which stores it in `turns.pgid`. After a crash, the core uses it to kill orphaned groups (§7.8.1). The backend reports it through `TurnStarted.pgid`.

### 9.7 Follow-up messages

With `[backends.claude] steer = true`, the default (D25; the key was added off by default in the amendment of Fri Oct 9, issue #36), a turn takes its prompt as `--input-format stream-json` and keeps stdin open, `capabilities.steer` is true, and a message that arrives during the turn is written to stdin (↪️). stdin closes once the CLI has taken in every message written (`--replay-user-messages`); later messages are held (§7.5). The texts of all the turn's results are posted in order (DECISIONS, 2026-10-09).

With `steer = false`, `capabilities.steer = false` and a turn runs as it did before steering: the core holds messages that arrive during a turn and coalesces them (§7.5).

### 9.8 Attaching existing sessions

- `list_sessions(cwd)` reads the transcripts `<config_dir>/projects/*/*.jsonl` of every configured account, newest first, and keeps those whose recorded cwd is this one. The CLI names a project directory after its cwd, but lossily: every character other than an ASCII letter or digit becomes `-`, and a long path is cut and given a hash suffix that can't be reproduced (DECISIONS, 2026-10-02, "Encoding of the cwd … corrected"). So the backend finds a session's transcript by its id (`projects/*/<uuid>.jsonl`), and uses the encoded name only as a first guess.
- For each session it shows the id, the first user prompt (80 characters) and the modification time.
- `!attach <uuid>` binds the session and posts a recap: the last user prompt and the last assistant text, read from the JSONL file.

### 9.9 Pitfalls checklist

- Scrub the API-key environment variables (§9.1).
- `-p` mode runs hooks and MCP servers from the user's settings, as usual. Interactive-only tools (questions to the user, plan approval) are unavailable.
- A login can expire while unattended. Map "Login expired" or HTTP 401 to `AUTH`. The fix is to run `claude` on the machine and `/login`; tokens from `claude setup-token` are an alternative for headless machines.
- Kill process groups, not just the PID, so that no orphaned tool processes are left behind.

### 9.10 Side questions (`!btw`, issue #716)

`capabilities.side_question` is on. A side question (§7.13) is one process, in the session's
workspace, on its account:

```
env: as a turn's (§9.1), without the commit identity and ZAB_BOT, and with
     ENABLE_CLAUDEAI_MCP_SERVERS=false whatever `claudeai_connectors` says
claude -p --output-format stream-json --verbose --permission-prompts none
       --resume <session> --fork-session --no-session-persistence
       --tools "" --strict-mcp-config --settings '{"disableAllHooks":true}'
       [--model <model>] [--effort <level>]                            the session's
stdin: the frame (`SIDE_QUESTION_FRAME`), then the question
```

- **What it sees.** The session's transcript as it is, resumed and never saved: every ended turn,
  and the running turn's progress, its tool call in flight reading as never returned. The
  transcript and the running turn are untouched, and a half-written last line is skipped. A fork
  whose first turn hasn't written its own transcript is asked from the cut copy it starts from
  (§9.3). A session whose first turn still runs has no ended turn: refused. (DECISIONS,
  2026-11-01.)
- **What it can do.** Nothing but answer. `--tools ""` removes the built-in tools but keeps the
  tools of MCP servers, which a side question would then call; `--strict-mcp-config` without
  `--mcp-config` loads none. `disableAllHooks` keeps the user's hooks from running for a process
  that is no session of its own.
- **The frame goes in the prompt.** On resume the CLI sends the conversation's recorded system
  prompt (`--system-prompt-snapshot`), and an `--append-system-prompt` never reaches the model.
- **The process** waits for a running `auth status` of its account and counts as a turn starting
  until the model answers (§9.4, #307). A timeout or a bridge shutdown stops it the §9.6 way.
- **The answer** is the last non-empty `result`: resuming a session whose turn runs a background
  command adds a `result` for that command's notification. A failed `result` is one plain line:
  the sign-in hint for `AUTH`, the retry advice for a lost refresh race, otherwise the kind and
  the CLI's first line, redacted.

## 10. Workspaces and git

`workspace/` implements the `WorkspaceManager` protocol (`core/types.py`), and the core calls it for everything in this section. Each `Workspace` has two paths:

- `root`: the repository root, or the worktree root. It is the boundary for every path check (§11.3, §11.4).
- `cwd`: where the agent runs, which is `root` plus `subdir`.

### 10.1 Workspace modes

`WorkspaceManager.prepare(project, mode, slug, subdir)` returns a `PreparedWorkspace` (`repo_root`, `cwd`, `branch`, `base`). Here `project` is `backend.workspace(project_path, project_path)`. It also ensures the `.zab/` exclusion (§10.2).

- **`shared`.** The workspace is the project path, plus `subdir` if one is set.
- **`worktree`.**
  - Command: `git worktree add -b zab/<slug>-<6 hex> <project>/.zab/worktrees/<slug>-<6 hex> HEAD`.
  - `<slug>` is the topic name in lower case, reduced to `[a-z0-9-]` and at most 40 characters.
  - The session's workspace is that worktree, plus `subdir` if one is set.
  - Worktrees are never deleted automatically in Phase 1. `!detach` leaves them in place, and the card shows the cleanup command.
  - A fork (§7.11) branches its worktree from `base_commit`, the commit `fork_base` built from the parent's files, instead of `HEAD` (`PreparedWorkspace.base` reads `fork@<short sha>`).
- If another session in the same shared workspace is `running`, the bridge warns in the new session's first status message. It still proceeds.

### 10.2 The `.zab/` directory

Each project and worktree has a `.zab/` directory holding `inbox/` and `worktrees/`. The bridge makes sure `.zab/` is listed in `.git/info/exclude`, which keeps it out of `git status` without changing any committed file.

### 10.3 Inbox

- An attachment on an owner message is downloaded to `<workspace>/.zab/inbox/<message id>-<sanitized name>`.
  - It uses the temporary URL from `GET /user_uploads/{realm_id_str}/{filename}`.
  - The size limit is `max_upload_mb`.
  - Name sanitizing keeps only `[A-Za-z0-9._-]`, at most 100 characters.
- The core downloads the bytes (it holds the Zulip client). It then calls `WorkspaceManager.save_attachment(ws, message_id, filename, data, mime_type)`, which sanitizes the name, writes the file under `<root>/.zab/inbox/` and returns an `Attachment`.
- The files are passed to the backend as `Attachment`s (§7.3). Codex additionally receives images as `localImage` items.

### 10.4 GitContext

`workspace/git.py` computes the following, cached for each turn:

| Field | How |
|---|---|
| `repo_root` | `git rev-parse --show-toplevel` |
| `head_sha` | `git rev-parse HEAD` |
| `branch` | `git symbolic-ref --short -q HEAD` |
| `upstream` | `git rev-parse --abbrev-ref @{u}` (may be missing) |
| `pushed` | true if `git merge-base --is-ancestor HEAD @{u}` succeeds |
| `web_url` | from `git remote get-url origin`, for GitHub only |

`web_url` accepts `https://github.com/o/r(.git)`, `git@github.com:o/r(.git)` and SSH host aliases of the form `github.com-<alias>` (the deploy-key setup in §13.1 creates these), and normalizes them to `https://github.com/o/r`. Other hosts yield `None` in Phase 1.

### 10.5 Diffs

- **Snapshot.** At session start and at each turn start, `WorkspaceManager.snapshot(ws)` returns a `Snapshot`, which the core stores JSON-encoded:
  - `ref`: the output of `git stash create` (a commit capturing uncommitted tracked changes, made without touching the working tree or the index), or `HEAD` if that prints nothing;
  - `untracked`: the output of `git ls-files --others --exclude-standard`.
- **Turn diff.** `WorkspaceManager.diff(ws, snapshot)` returns a `DiffResult`:
  - the counts, the per-file list (`--name-status` and `--numstat`) and the patch, from `git diff <ref>` (working tree against the snapshot);
  - `new_untracked`: untracked files that are not in the snapshot.

  If the workspace is not a git repository it returns `None`, and the core falls back to the backend's `native_diff` (Codex) or to "no diff available". An empty diff means no diff message is posted.
- **`!diff`.** The same computation against the session's `start_snapshot`.
- **Rendering.** `Renderer.render_diff` (§11.8).

### 10.6 Rules for git calls

- Always pass argument lists, never a shell string.
- Always pass `-c core.quotepath=off` and put `--` before paths.
- Use a 10 s timeout, and set `GIT_TERMINAL_PROMPT=0` and `GIT_OPTIONAL_LOCKS=0`.
- The bridge never fetches, pulls, pushes or commits. Handovers commit through the agent (Appendix A.2).
- The repository is the agent's, so nothing it configures may run as the bridge (a sandbox escape where the agent is sandboxed): every call passes `-c core.fsmonitor=false`, `-c core.hooksPath=/dev/null`, `--attr-source=<the empty tree>` (git 2.41 or newer, the bridge's minimum; the empty tree's id follows the repository's object format, SHA-1 or SHA-256; no `.gitattributes` of the checkout can name a filter or diff driver) and `-c diff.ignoreSubmodules=dirty` (never descend into a submodule's repository); diffs add `--no-textconv`; inherited `GIT_*` variables are dropped except the author and committer identity.
- A workspace is pinned to its repository (`--git-dir` and `--work-tree` recorded when it is prepared or first used) and the pins are persisted in `<state_dir>/workspace-pins.json`, so that neither a nested `.git` nor a replaced worktree `.git` file, before or after a bridge restart, redirects the bridge. What remains: `$GIT_DIR/info/attributes` is read regardless of `--attr-source`.

### 10.7 Turn snapshots for the file server

Behind a per-machine switch (`[render] refserve_url`, the central file server's base URL; empty =
off), the final answer and its diff can link files to the turn's snapshot (issue #36). The
snapshot is **lazy**: `RenderContext.snapshot` is a provider that a message awaits
only when it would link at least one file (a resolved reference in the answer, or a name in the
diff's file lists). A turn that links no file creates no snapshot and pushes nothing; the Zulip
conversation is its record. The core memoizes the provider per turn, so the answer and its diff
share one snapshot and one push.

On first use the core calls `WorkspaceManager.publish_snapshot(ws, "<session id>/<turn row>",
github_owner=[render] refserve_owner)`. The snapshot is a commit of the repository as it stands:
tracked changes plus untracked files (respecting `.gitignore`), minus every denylisted path
(§11.4), even a tracked one, minus changed or untracked files with more than one hard link, and
minus untracked files over a per-file cap (20 MB) and a total budget (50 MB, what the push can
carry within its timeout). It is built with a
temporary index (`GIT_INDEX_FILE`) that starts from HEAD's tree, `write-tree` and `commit-tree`
**without a parent** (a push must not carry the checkout's unpublished history, whose commits may
hold denylisted files), and kept under `refs/zab/turns/<session id>/<turn row>`, created only if
absent. It is then pushed to `origin` as that same ref
(`git push origin refs/zab/turns/<s>/<t>:refs/zab/turns/<s>/<t>`, with the deploy key alias that
`zab setup-repo` configured, §10.6 hardening, a 120 s timeout, its wall time logged at INFO), only when `origin` is a GitHub
repository of `[render] refserve_owner`, the account or organization whose repositories the
server serves, its `ZAB_FILES_ORG` (`workshop/files/README.md` §3). `push_config.py` writes it
from the workshop's `github_org` (`--refserve-owner`, #693). Its default, `MathAgents`, the
example deployment's organization, is for configs written before that. The branch, the index and the working tree are
never touched, and no branch is pushed. Once per name: a retried completion reuses the ref's
commit (and pushes it again, which is idempotent).

The core turns the result into `PublishedSnapshot(sha, url)` with
`url = {refserve_url}/{repo}/{sha}`, where `repo` is the name of `origin`'s repository (the
server's configuration fixes the owner). The links of §11.3 and the diff's new-file list then
point there, ahead of GitHub permalinks: anyone with a link can open the server's copy, while a
GitHub link needs the reader's account to have been invited to the repository (issue #36). The
server (`workshop/files/`) fetches `refs/zab/turns/*` from GitHub with a
read-only token and serves only full shas that are tips of those refs, so the denylist-filtered
snapshots are the only content it exposes; anyone with a link can read that file. It removes
its copy of a repository that was deleted or transferred within about an hour, telling that
apart from a failure, an outage or a bad token (`workshop/files/README.md`, *Operations and
bounds*; #714); a browser that has opened a file keeps it. Links are
permanent once posted, so the URL scheme does not change. When there is no GitHub origin of
that owner, when the push fails, or on any other failure, the provider returns None and
rendering is exactly as without the file server.

**Share links** (#710). An answer's link opens its whole snapshot, so sharing one file through it
shares the repository as it was. `zab share PATH…`, which an agent runs when the participant
asks for a link to give others, makes a commit of only those files and folders, as they stand in
the working tree, with no parent and a random 256-bit nonce in its message (so that no one who
knows the files can compute the link), under `refs/zab/shares/<name>`, and pushes it as a turn
snapshot is pushed. A folder holds what a snapshot would of it; a path named that a snapshot
would leave out (the §11.4 denylist, the caps, a symbolic link, a submodule, anything outside the
repository) is refused, and nothing is pushed. Its link is the snapshot's form,
`{refserve_url}/{repo}/{sha}/` and the path for one file or folder. `zab share` opens the link
once, and withdraws the share if the server doesn't serve it. The server keeps its own copy of
each share, apart from its mirrors, so that a share outlives its VM and its repository, until
its ref is deleted from the repository it came from (`zab share --revoke`, which asks the server
to check it at once, or a deployer: `workshop/files/README.md`, *Share links*).

## 11. Rendering agent text

`render/` implements the `Renderer` protocol in `core/types.py`:

- `render_agent_text(text, ctx)` turns a final answer into Zulip message parts that are ready to post.
- `render_inline(text)` handles one line of **prose**: commentary, or a reasoning heading. It applies steps 1, 3 and 5 below, with no links, uploads or splitting. The core wraps command lines and paths in code spans itself and never passes them through `render_inline`, so `$VAR` in a shell command is never treated as math.
- `render_diff(diff, ctx)` builds the diff message (§11.8).

The steps of `render_agent_text`, in order:

1. Redact secrets (§11.6).
2. Protect code: replace fenced blocks (```` ``` ```` and `~~~`, including longer fences) and inline code spans with placeholders.
3. Convert math (§11.2).
4. Rewrite links and collect uploads (§11.3, §11.4). This step looks inside the protected inline code spans, but not inside fenced blocks.
5. Neutralize mentions (§11.5).
6. Restore the code.
7. Split into parts (§11.7).

Every step is a pure function except step 4, which is async because it uses the Workspace and the upload callback. Each needs table-driven unit tests.

### 11.1 Zulip markdown notes

Zulip renders CommonMark-like markdown with GFM tables, fenced code with a language tag, spoilers (```` ```spoiler Title ````) and math. In the reply, headings deeper than `###` are downgraded to bold text. A time written `<time:2026-10-27T01:08:38Z>` shows in each reader's own time zone; the agents' instructions ask for it, in UTC (issue #673). The bridge takes `<time:` up to the next `>` on the same line, at most 100 characters after `<time:`, as one such time.

### 11.2 Math

Zulip writes inline math as `$$…$$` and display math as a ```` ```math ```` fenced block. Conversions, applied outside code:

| Input | Output |
|---|---|
| A paragraph consisting only of `$$ … $$`, whether on one line or several | ```` ```math ```` block |
| `\[ … \]` | ```` ```math ```` block |
| `\begin{align*}`, `align`, `equation`, `gather` or `multline` environments outside any delimiters | ```` ```math ```` block, with the environment kept |
| Inline `\( … \)` | `$$…$$` |
| Inline `$…$` | `$$…$$`, following pandoc's `tex_math_dollars` rules: the opening `$` is followed immediately by a non-space character, the closing `$` is preceded immediately by a non-space character and not followed by a digit, and the math contains no blank line |
| Existing inline `$$…$$` | Left alone |

The test table must include:

- `$100 and $200` stays unchanged;
- `costs $5.` stays unchanged;
- `$x$-adic` becomes `$$x$$-adic`;
- `\mathbb{Q}` and `\overline{\mathbb{Q}}` survive byte for byte;
- dollars inside code are never touched.

### 11.3 Links

**Candidates:**

- markdown links `[label](target)` whose target is not a URL, anchor, `mailto:` or `/user_uploads/…`;
- inline code spans that look like a path, optionally with a line suffix;
- bare tokens of the form `path:line` or `path:line-line` whose path contains `/` or has an extension.

Nothing inside such a Zulip time (§11.1) is a bare or link candidate.

Codex tends to write markdown links with absolute paths; Claude tends to write `` `src/foo.py:120` ``. Both must work.

**Resolution:**

1. Strip a `:L`, `:L-M` or `#Lx-Ly` line suffix.
2. Resolve a relative path against `Workspace.cwd` first, then against `Workspace.root`; accept an absolute path as is. The bridge context (Appendix A.1) asks for repository-relative paths, but agents also write cwd-relative ones.
3. Canonicalize with `Workspace.realpath` (resolving symlinks), and reject anything outside `Workspace.root`.
4. Check that it exists.
5. In a turn's final answer, prefer the files the turn touched (`RenderContext.turn_files`: the files it created or modified, from its diff against the start snapshot, §10.5). This applies when a relative path resolves to a file the turn did not touch, or to nothing. Then the one touched file whose repository-relative path is the written path, or ends with `/` and the written path, wins. An answer that lists files it just created writes them relative to where it created them, and the root may hold a tracked file with the same name (issue #36, a bare `README.md:1`).
   - If several touched files match a path that would resolve to an untouched file, the reference is ambiguous: show it as code, linked nowhere.
   - Without turn information (commentary, outside a turn), steps 1 to 4 decide alone.

Batch the git queries for all candidates in a message: tracked (`git ls-files -z -- <paths>`), modified (`git diff --name-only HEAD -- <paths>`), and directories.

The rows are checked in order, and the first match wins:

| Case | Rendering |
|---|---|
| Does not exist | Leave the text unchanged |
| Extension in `auto_upload_ext` (images, PDF), accepted by `check_upload` | Upload it (§11.4) and link the upload, even if it is committed; image uploads preview inline |
| In the turn's snapshot (§10.7: the file server is on and the snapshot was pushed) | `[label]({snapshot url}/<path>)`, directories with a trailing `/`; for every other file type, committed and unchanged ones included. A source file the server renders with line numbers (`zab.render.links.SERVED_LINE_EXT`, the server's languages but Markdown) keeps the line as `#L<first>`, the only line anchor its pages have, so a range opens at its first line; other files get no anchor. The label keeps the line as written (issue #448) |
| Tracked, unmodified, `pushed`, `web_url` known (no snapshot) | `[label](https://github.com/o/r/blob/<head_sha>/<path>#L10-L20)`; directories use `tree/` |
| In a turn's final answer: extension in `[render].auto_upload_text_ext` (`lean`, `tex`, `py`, `md`, `json`, `csv`, …; not HTML), valid UTF-8 without NUL bytes, at most `max_text_upload_kb`, accepted by `check_upload`, and fewer than `max_text_uploads` distinct text files uploaded for this answer so far | Upload a copy, with secrets redacted as in the answer (§11.6), as `<name>.<ext>.txt` (a `.txt` file keeps its name; Zulip shows `text/plain` in the browser but offers `.lean` or `.md` as downloads) and link the upload, labelled with the path. A snapshot at posting time, like images (issue #36) |
| Anything else that exists | Code span `` `path:line` `` followed by ` *(local)*` |

Permalinks use the SHA, never a branch name, so links stay correct as the branch moves.

### 11.4 Uploads

- `RenderContext.upload(Upload)` is supplied by the core. It calls `POST /user_uploads` and returns the URL. It also deduplicates per session by (path, sha256), with a cache in `kv`, so the renderer does not need the store.
- Every candidate first goes through `WorkspaceManager.check_upload(ws, path, explicit=…)`, which applies the root check, the denylist and the size limit and returns an `Upload`, or raises `UploadRefused(reason)`. `explicit=True` is for `!file`, which allows any extension.
- **Automatic uploads** cover files with extensions in `render.auto_upload_ext`. The size limit is the smaller of `max_upload_mb` and the server limit.
- **`!file PATH`** uploads any file type under the same limit.
- The **denylist** is checked for both, after resolving symlinks. The path must be inside the workspace root. Refuse:
  - anything under `.git/` or `.zab/worktrees/`;
  - the names or patterns `.env*`, `*.pem`, `*.key`, `id_*`, `*.zuliprc`, `zuliprc`, `.netrc`, `auth.json`, `.credentials.json`, `.git-credentials`, `.pypirc`, `.npmrc`, `*.p12`, `*.pfx`;
  - anything under `.ssh/`, `.codex/`, `.claude/`, `.config/zab/` or `.aws/`.

  Directories match anywhere in the path and names match the file's own name, both without regard to case (`zab.workspace.denylist`).
- The same denylist applies to **share links** (§10.7, #710): `zab share` refuses a denylisted
  path, leaves denylisted files out of a folder, and the file server applies it again.
- The same denylist applies to **turn diffs** (§10.5, §11.8): a changed file it refuses is excluded from the patch and the counts, and listed in the stat as `<path> | changed (contents hidden)`; a name whose exclusion cannot be expressed hides the whole patch.

### 11.5 Mentions

- Wildcard mentions (`@**all**`, `@**everyone**`, `@**channel**`, `@**stream**`, `@**topic**`) and group mentions (`@*group*`) become inline code.
- Every other `@**Name**` becomes a silent mention, `@_**Name**`.
- The bridge's own notification mentions are added by the core after rendering, so they are not affected.

### 11.6 Secret redaction

Replace matches with `[redacted <kind>]`:

| Kind | Pattern |
|---|---|
| OpenAI key | `sk-(proj-)?[A-Za-z0-9_-]{20,}` |
| Anthropic key | `sk-ant-[A-Za-z0-9_-]{20,}` |
| GitHub token | `gh[pousr]_[A-Za-z0-9]{36,}` and `github_pat_[A-Za-z0-9_]{50,}` |
| AWS access key | `(AKIA\|ASIA)[0-9A-Z]{16}` (long-term and temporary keys) |
| JWT | `eyJ…` `.` `eyJ…` `.` `…`: three base64url segments, the first two starting with `eyJ`, as a whole token |
| Private key | `-----BEGIN [A-Z ]*PRIVATE KEY-----` through the matching `END` line |
| PGP private key | `-----BEGIN PGP PRIVATE KEY BLOCK-----` through the `END` line |
| Zulip API key | a line `key = [A-Za-z0-9]{32}` (blanks around `=` optional, one leading diff or list prefix character kept; a line ends wherever `str.splitlines` ends one, a lone `\r` included), the value of `ZULIP_API_KEY=…` (quoted or not), and the key in `curl -u user:key` / `--user user:key` |

A key prefix (`sk-`, `sk-ant-`, `gh…_`, `AKIA`/`ASIA`) matches only at the start of a token, never inside a longer hyphenated or alphanumeric one: a URL slug such as `…-the-unit-disk-to-…` or `…/task-scheduling-…` is not a key. Redaction is a safety net, not a guarantee. It also applies to status-message titles.

### 11.7 Splitting

- If the text fits in `max_message_length − 200`, post one part.
- Otherwise split at blank lines outside code fences. If a fence must be split, close it at the end of the part and reopen it with the same info string. Parts are labeled `(1/3)` and so on.
- If there would be more than 3 parts, upload the full rendered text as `answer-turn<N>.md` and post the first part plus a link.

### 11.8 Diffs and spoilers

- The diff is always its own message, posted after the final answer (§4.9).
- **One visible line**, then **one collapsed spoiler** (issue #36, from a rehearsal): most readers read only the line, and a long turn must not post a long message after every answer.

  `````
  Changes: 7 files (+1210 -3), 5 new
  ````spoiler Details
  - [`genus2/curves.py`](https://files.<domain>/<repo>/<sha>/genus2/curves.py) new, +800 -0
  - `old/notes.txt` deleted, +0 -3
  - `plots/fig1.png` new, binary
  - `.env` changed (contents hidden)
  - `README.md` +2 -1
  ```diff
  …
  ```
  ````
  `````

- The line counts every file the turn touched, committed or not, with the total insertions and deletions and the number of new files. When the patch is uploaded it ends with `: [turn<N>.diff](…)`; when it cannot be, with "(diff too large to inline)" or "(diff too large to upload: …)". The core's "Changes may also include work from…" note (several bots in one folder) follows the message, outside the spoiler.
- **The file list** replaces the `--stat` block and the old "New files" list. It comes from `DiffResult.files` (`DiffFile`: path, status, counts, binary, hidden), which the workspace fills from `git diff --name-status`/`--numstat` against the start ref, then the new untracked files as `new`, counted by reading them (regular files only, size-capped).
  - Each line is the path, linked to its copy in the turn's snapshot when there is one (§10.7; hidden and deleted files never), then `new`/`deleted` where it applies, then `+a -d` (`binary` instead for binary files; nothing when the count is unknown).
  - Hidden (denylisted, §11.4) files read `changed (contents hidden)` (`new, contents hidden` for an untracked one), without counts or link.
  - The list is clipped with "… (n more)" to fit.
- **The patch** follows the list inside the spoiler only if it is at most `diff_inline_max_lines` lines **and** the whole message, fences included, fits in `max_message_length − 200`. Otherwise it is uploaded as `turn<N>.diff` (linked from the visible line) and the spoiler holds only the list.
- Every fence is longer than any backtick run it contains (the patch, the file names' code spans), so nothing inside can close it. Everything inline is redacted (§11.6) and has its mentions disarmed (§11.5). An empty diff posts nothing. `!diff` uses the same message.

## 12. Security model

### 12.1 Threats and mitigations

| Threat | Mitigation |
|---|---|
| Another org member drives someone's agent | Allowlist = the bot's owner (§4.5 rule 1); sessions only in private channels (§4.3) |
| Another bot drives an agent, or two bridges answer each other | Bot senders are always ignored; shared-topic rule (§4.5) |
| Agent output pings everyone, or leaks secrets | Mention neutralizing (§11.5); redaction (§11.6); upload denylist (§11.4) |
| Path traversal through agent-supplied paths | Canonicalize and check the root; symlinks resolved before the check (§11.3, §11.4) |
| Shell injection | Argument lists only; no Zulip-supplied string is ever passed to a shell; no command that runs arbitrary input |
| Unsandboxed app-server APIs | `thread/shellCommand`, `process/spawn` and `command/exec` are never used (§8.9) |
| Credentials | The bridge never reads agent credential files. API-key variables are scrubbed for Claude (§9.1) and must be absent from the Codex daemon's environment (§8.9). |

### 12.2 Known residual risk in Phase 1

zab runs as the same Unix user as the agent, so a prompt-injected agent can read the bot's zuliprc. With it, the agent can read every channel the bot is subscribed to. It can also post wherever the bot's **owner** can post, not only where the bot is subscribed:

- Zulip lets a bot send to any channel its owner can send to (`access_stream_for_send_message`: "Bots can send to any stream their owner can"). That includes the owner's private channels.
- Zulip accepts `widget_content`, such as buttons, from any sender.
- Both were verified live on Zulip Cloud: a participant's bot that was not subscribed to a private channel posted there, because its owner was (DECISIONS, the V2 row of the verification log; ADMIN §1.5).

For a person's own bots (D3), the owner is that person, so the key reaches no further than they do. For bots that one account owns for many people, it reaches every channel of that account.

On the admin server's fleets that account is `zab-provisioner`, which owns every participant bot. It is never subscribed to `#zab-admins` or to an organizers channel, but it is subscribed to every workshop's `#<ws>-fleet-alerts` channel and to its participants' channel where it has one (BOOTSTRAP §5.4; the waker posts failed wakes in the alerts channel with the participant's bot). So a prompt-injected agent on any workshop's VM can post in those private channels of every workshop on the server.

Mitigations:

- one bot per backend per machine;
- bots subscribed only to their owner's project channels;
- keys regenerated when a machine is retired;
- on workshop VMs, keys that die with the VM (`workshop/admin/README.md` §7.3);
- on the admin server's fleets, no bot is ever obeyed: the admin bots never obey a bot, and warn about any message from a sender they don't recognize (ADMIN §1.5, §2.1); nothing takes commands in an alerts channel.

This is documented, not solved, in Phase 1.

### 12.3 Cross-user isolation (deferred, §18.1)

On a machine where the agent runs under its own account without sudo, real isolation is possible:

- zab runs as another user and reaches the daemon socket, either with group permissions or with `sudo -u <agent user> codex app-server proxy --sock <path>` (the proxy speaks the websocket upgrade over stdio).
- File and git operations go through the app-server's `fs/*` methods, and git through a fixed argv allowlist.
- The contract's `Backend.workspace()` indirection exists so this can be added without touching the core.

## 13. CLI and packaging

### 13.1 Commands

| Command | Behavior |
|---|---|
| `zab init [--profile P] [--bot NAME --zuliprc FILE --backend B]…` | Writes the config and copies each zuliprc into place with mode 0600. Validates that each bot works (`GET /users/me`) and is a bot, then shows its owner. Prints the next steps. Idempotent: re-running adds or updates entries. |
| `zab doctor [--json]` | Checks: config valid; credential files 0600; each bot authenticates; the owner resolves; each project path is a git repo; each backend's `doctor()` passes (logins, versions, single daemon); `git` present; clock skew under 5 s; root and project filesystems report used percentage and free GiB, with a non-fatal `disk` warning at 85% used; where `zab.service` is installed, `<bot>:sentinel` for each bot (issue #588): `zab-sentinel.service` installed and active, and the bot's mark (§13.5) from this boot, by a live process, as this bot and rewritten within 3 minutes, else a non-fatal warning whose detail starts with its state: `not installed`, `down`, `no mark` or `stale`, in which DM `!restart` is unavailable and the bridge answers DM `!vm` itself, or `unknown` when `systemctl --user` gives no answer (the user's systemd out of reach). Exit code 0 means all good. |
| `zab run` | Foreground bridge. Logs go to stderr and the log file. SIGTERM triggers a graceful shutdown (§9.6). |
| `zab status [--json]` | Reads the store: sessions, their state and topic links, running turns, and (`--json`) `last_activity`, the newest message taken in for the agents or turn start or end, as idle stop counts it. Works while `zab run` is running. |
| `zab sentinel [--config PATH]` | The DM `!vm` and `!restart` of issue #204, run by `zab-sentinel.service` (§13.3). |
| `zab service install \| uninstall \| status` | Linux only. Writes and enables the systemd user units in §13.3 and runs `loginctl enable-linger $USER`, printing the sudo command if that needs it. The user manager does not inherit the login shell's `PATH`, so the command bakes `Environment=PATH=…` into both units: configured prepends, then non-standard directories of the `codex`, `claude`, `uv`, `git` and `gp` it finds with `shutil.which`, then `~/.local/bin:/usr/local/bin:/usr/bin:/bin` in that order. Finding a tool in a standard directory does not promote that directory. `zab init` likewise records absolute paths for the backend binaries in the config. `zab doctor --service-env` re-runs the checks with the unit's `PATH`. When run through `sudo -u`, the caller must provide `XDG_RUNTIME_DIR=/run/user/<uid>` (`workshop/admin/README.md` §2, step 4). |
| `zab dev <subcommand>` | Developer tools, not for users: `claude-turn`, `record-claude`, `scrub` and `record-codex`. `cli.py` dispatches `zab dev *` and `zab setup-repo` / `zab preflight` to modules in `zab/dev/` and `zab/tools/`. |
| `zab setup-repo OWNER/REPO [--path DIR]` | Deploy-key flow for machines without GitHub credentials (the workshop VMs). Details below. |
| `zab preflight [--json]` | Workshop readiness checks (§13.1). Details below. |
| `zab history (--list-topics [--match TEXT] \| --channel C --topic T) [--limit N] [--before ID] [--bot NAME]` | An agent's read-only view of another topic, as the session's bot (§5.6, §12.2). |
| `zab share PATH… \| --list \| --revoke LINK\|NAME` | An agent's share link: some files or folders alone, served by the file server until revoked (§10.7, #710). |
| `zab version` | Prints the version. |
| `waker run \| status \| selfcheck [--config PATH]` | The waker (§13.4): the package's second console script, run by its service on the waker's host. |

`zab setup-repo` steps:

1. Generate `~/.ssh/zab_<repo>` (ed25519, no passphrase).
2. Add a `Host github.com-<repo>` block to `~/.ssh/config`.
3. Print the public key and the URL `https://github.com/OWNER/REPO/settings/keys/new`, with the instruction to tick "Allow write access".
4. Wait for Enter.
5. Clone to `--path` (default `~/work/REPO`), or set the remote of an existing checkout.
6. Verify write access by pushing a branch `zab-preflight-<host>` and then deleting it.

`zab preflight` checks:

- `codex login status`;
- a one-line `codex exec` smoke prompt;
- a one-line `claude -p` smoke prompt on the cheapest model;
- each smoke prompt makes the agent run one shell command, `cat` of a file holding a fresh nonce, and passes only if the reply contains the nonce. An agent that answers but cannot run tools fails (AMI v1's Codex lacked `codex-code-mode-host`, issue #36);
- the git push check;
- math tools present (`gp --version-short` and others);
- free disk space of at least 20 GB.

It writes `~/.local/state/zab/preflight.json`. The smoke prompts use a few tokens; the reason for making real calls is that status commands can report "logged in" with a stale token.

### 13.2 Installation

- From a release tag: `uv tool install git+https://github.com/MathAgents/zulip-agent-bridge@vX.Y.Z` (README, "Install"). `pipx` also works.
- Python ≥ 3.12 is required. `uv` provides it on machines that lack it.
- Releases are tagged `v0.x.y`. The workshop image is built from a tag, and a rollout installs a release's wheel on the VMs (`workshop/admin/README.md` §2).

### 13.3 systemd user units (Linux)

`~/.config/systemd/user/codex-app-server.service`:

```ini
[Unit]
Description=Codex app-server daemon (for zab)
[Service]
ExecStart=%h/.local/bin/codex app-server --listen unix://
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
LimitNOFILE=65536
Restart=on-failure
RestartSec=5
[Install]
WantedBy=default.target
```

`~/.config/systemd/user/zab.service`:

```ini
[Unit]
Description=zab Zulip agent bridge
After=codex-app-server.service
Wants=codex-app-server.service
[Service]
Type=notify
NotifyAccess=main
TimeoutStartSec=1800
WatchdogSec=300
ExecStart=%h/.local/bin/zab run
# zab service install writes the real PATH list (§13.1)
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=5
KillMode=mixed
TimeoutStopSec=30
[Install]
WantedBy=default.target
```

- Adjust the `codex` path to wherever the install put it. `zab service install` detects it with `shutil.which`.
- The Codex unit is written only if a Codex backend in daemon mode is configured.
- **The Codex daemon's open files** (issue #664). Each Codex thread the daemon has loaded keeps a `lean-lsp-mcp` child and its pipes (#632), so the default soft limit of 1024 is reached: on the test VM, 109 children held 967 files a minute after boot. Its unit sets `LimitNOFILE=65536`, or the user manager's `DefaultLimitNOFILE` where that is lower (1048576 on the AMI). systemd would clamp a higher value to the manager's hard limit itself; the cap is for the reinstall below: a daemon started from a unit without the line has that hard limit, and an unprivileged `prlimit` can't raise a hard limit. The installing process's own limit binds neither, and is not read. The bridge's and the sentinel's units keep the default: the bridge held 15 to 20 files on the test VM, and Claude's processes, its children, end with their turn.
- **A reinstall applies it without a restart.** When an active Codex daemon's unit changed in its `LimitNOFILE=` line alone, as on the first install of this unit over one without it, `zab service install` doesn't restart the daemon, which would end its running Codex turns: it runs `prlimit --pid=<MainPID> --nofile=<n>:<n>` on it, and the daemon's next start reads the limit from the unit. If `prlimit` fails, it prints that the limit applies at the next start (a boot, or `!restart codex`), and `push_config.py` puts that line in the VM's row as a note. Any other change to the unit restarts the daemon, as before.
- Each listener touches `<state_dir>/heartbeat-<bot>` after advancing its event cursor
  (sentinel progress). Separately, in-memory liveness advances on every listen-loop
  pass and before each handler, including errors. Known poster-queue and rate-limit
  waits renew liveness; arbitrary handler awaits do not. A Zulip outage or 429 storm
  therefore does not trigger a restart. A hung poster queue is outside this detector.
- The unit uses `Type=notify`: `READY=1` is sent only after all bridges start. Startup
  has a 30-minute timeout. Thereafter pings every 75 seconds require every listener
  to be live within 150 seconds; a hung listener causes restart within approximately
  450 seconds. **One hung bot restarts the process**, interrupting other bot turns:
  this trades that disruption for automatic recovery from silent listener hangs.
  Previous-process files cannot renew liveness. Outside systemd only files update.
- End-to-end scenarios 3 and 4 (`tests/e2e/test_scenarios_slow.py`) check both sides on a
  test VM, and pass: a bridge frozen whole with SIGSTOP during a turn is restarted within that
  bound, and a 12-minute Zulip block during a Claude turn causes no restart. That one hung
  listener stops the pings while the others stay live is tested offline
  (`tests/core/test_watchdog.py`).
- `zab doctor --service-env` reports systemd's `NRestarts` as `service:restarts`, warning
  on a nonzero count or unavailable manager; it never resets that counter. The journal
  retains the reasons for individual restarts.
- `~/.config/systemd/user/zab-sentinel.service` (issue #204) runs `zab sentinel --config …`
  with the bridge's `PATH` and environment, `Restart=always`, `RestartSec=10`,
  `WantedBy=default.target` (lingering starts it at boot, a waker's start included), and no
  `After`/`Wants`/`BindsTo` on `zab.service`, so that it runs while the bridge doesn't.
  `zab service install` writes it with the others, restarts it on every install (it reads the
  configuration and its code only at start, and keeps no state), and `uninstall` removes it.
  `push_config.py` removes a unit zab wrote before the installed release's own `service install`
  or `uninstall`, so that a rollout of an older release, or its uninstall, leaves none running.
- Codex 0.157.1 has its own `app-server daemon start`, which manages a daemon by PID file and advertises no foreground flag, so the unit keeps `codex app-server --listen unix://` (DECISIONS, 2026-09-27, "Daemon subcommand").
- On macOS in Phase 1, run `codex app-server --listen unix://` and `zab run` in terminals, or under `screen`. launchd support is deferred (§18.1).

### 13.4 The waker (opted-in VMs; issue #207)

The waker starts a stopped workshop VM when its owner writes to one of its bots, so that idle
stop can power VMs off without making their owners wait for an organizer. It is a separate
program, `waker run|status|selfcheck` (`zab.waker`, the package's second console script), which the
bridge never imports. It reads each opted-in VM's bots' messages through queues of its own
(messages only), with the bots' keys, and starts only the VMs its config lists, after
refreshing the bridge's owner authorization rules (§5.2) and the VM's EC2 state. Its config
chooses one of two modes:

- **Starter mode**, on the admin server (ADMIN §1.2): the config, which the admin bots write
  (`!waker on|off`), names the starter's socket and has one `[[workshop]]` table per workshop,
  with its opted-in bots and the channel for failed wakes (`organizers_channel`, which the admin
  bots set to the workshop's `#<ws>-fleet-alerts`). The waker runs as its own user, without an AWS
  credential. It asks `zab-admin-starter` for its VMs' states and starts, and the starter starts
  only a VM of that workshop's fleet: one with the workshop's tags and a `participant` tag, in
  its region. Its status file, every 30 s, gives each workshop's `ready_at`: when its waker last
  served, once it had run a minute. The admin bots report a waker whose `ready_at` is 5 minutes
  old, in the workshop's `#<ws>-fleet-alerts`, `!status` and the console (ADMIN §1.8, #717).
- **Direct mode**, for a deployment without an admin server: the config and the bots' zuliprcs
  are root's, 0600, under `/etc/zab-waker/`, and the waker starts VMs with boto3 and its host's
  role (`workshop/waker/README.md`). One waker serves one workshop: the config's `workshop` key
  is the VMs' `workshop` tag value, and only instances carrying that tag are started. The key
  defaults to `nt-agents-2026`, the example deployment's first fleet, so always set it
  (`push_waker.py --workshop`, whose default is the same).

In both modes the waker keeps one durable one-attempt-per-minute cooldown per VM, which its
bots share. `InsufficientInstanceCapacity` retries after 60 and 120 seconds; the third failure
notifies the owner and the organizers. Successful start requests receive a channel ⏰ reaction
or a short owner DM: on each waiting owner message in a channel, a ⏰ from each bot it mentions,
completed or typed, else from each of the VM's bots (§4.5 rule 6.2, #713), and one DM per wake
for the owner's DMs (§13.4.2). No incoming message text is persisted or logged.

#### 13.4.1 Wakes

The waker checks its 30-second state cache before owner resolution. A wake waits
up to five minutes for `stopping` to become `stopped`. EC2 discovery uses tag and
instance-ID filters so a missing VM does not prevent other VMs from waking.

To cover a shutdown between state polls, the waker keeps, per VM and in memory
only, each message it sees until the next successful poll: its ID, sender ID and
type, and which of the VM's bots saw it, each with whether the message names that bot: its
queue's `mentioned` flag (a completed mention), or a typed mention of the bot's kind
(§4.5, #713), which Zulip doesn't flag. The waker reads that as the message arrives,
outside code and quotes as the bridge does, and takes a bot's kind from its name's
prefix, `claude-…` or `codex-…`, as `push_waker` names the bots. Never its
text. At most 100 are kept per VM; past that, the oldest message of the sender
with the most is dropped, so another sender cannot push out the owner's. Both
bots' queues see a channel message; their copies merge into one entry, so a
message counts once. If the VM then shows stopped/stopping, the waker rechecks
owner authorization and wakes it without requiring a second message. A poll acts
only on the messages seen before it; one that arrives during its pass waits for
the next poll.

#### 13.4.2 Acknowledgments

Wake decisions are per VM (one wake task, one cooldown); acknowledgments are per
message. A message wakes the VM when any of the VM's bots that saw it allows its
sender, including a copy that arrives during another bot's owner check. Every
owner message that waited for a wake is acknowledged once the start request
succeeds: those seen while the VM showed stopped or stopping, those kept while
the cache still said otherwise, and those that arrive while the wake is in
flight. A channel message gets a ⏰ from each bot that will answer it, as each
bot's bridge adds its 👀, in configured order, if that bot allows its sender:
- a message that mentions bots, completed or typed: each of them (#679, #713);
- a message without a mention: each of the VM's bots, as the bridge sends it to
  each (§4.5, "A message without a mention", #713), whatever its topic's earlier
  messages addressed. The waker reads no topic history: #683's lookup went with
  the rule it served. In a multi-party channel too, where the bridge needs a
  mention: the ⏰ says that the VM is starting, even where no bot will answer;
  likewise for a bot whose agent is not signed in, which stays
  out, and for one whose session in the topic was detached: the waker sees none
  of them.

A DM gets the one notice below, from the first bot, in configured order, that saw
it and allows its sender (`push_waker` configures Codex first). The waker cannot
see sessions, so an unmentioned owner message still wakes the VM. None of the bots
allows the sender: no ⏰. The waker sees the bots of this VM only: a message that
mentions only another participant's bot is one without a mention to it.

The start request does not wait for the second bot's copy of a message, but the
⏰s do: up to 5 seconds, until every bot of the VM has delivered each waiting
channel message, so the order in which the queues deliver does not pick the bots.
All the owner DMs of one wake share one "⏳ Starting your VM" notice, from the bot
that received the first. A message first seen after the start request gets no ⏰; the
bridge's 👀 answers it. If no start request is made (the VM is up, the cooldown,
the owner check, a capacity failure), the waiting messages are dropped. The last
1,024 acknowledged messages are remembered, so a copy that arrives after its ⏰s
adds none, from either bot: no bot adds a second ⏰ to a message. The waker logs
`httpx` and `httpcore` at WARNING only.

#### 13.4.3 Catch-up (#403)

A queue sees only what is sent after it is registered, and the waker registers its queues when
it starts: after each config change (`!waker on|off`), a deploy or a reboot, and when Zulip
drops a queue. After each registration it fetches what it missed and handles those messages as live ones:
- **From where.** Per bot, the waker stores its mark in its store: the newest
  message up to which it handled every one it saw. A message that still waits
  (for its wake, or for the next poll) holds the mark back, so a wake cut short
  by an exit is caught up again. At each start the waker forgets the marks of the
  bots its config doesn't list. A bot without a mark (just opted in, opted in
  again, or its mark damaged) starts from its config's write, 30 seconds earlier
  for the clocks; so does a bot whose mark is past the newest message it has
  received, as the registration says (Zulip's `max_message_id`).
- **Only what is new** (#717). No fetch at all when the registration's newest
  message is the mark. Else from the newest message back, 10 a request, to the
  mark, or to the first message sent before the floor (5 minutes ago, or the
  time without a usable mark), the newest 100 at most. Of each message only what
  the waker reads is kept, not its text: the kinds its typed mentions name
  (`@claude`, `@codex`, #713) are read from the text before it goes. Each
  request's answer goes before the next; then they are handled oldest first. Never the messages of the VM's
  own bots, which a negated `sender` narrow leaves out (if Zulip refuses it, the
  waker leaves them out itself): they wake nothing (the owner check refuses a
  bot), and Zulip returns each message with its whole edit history, every earlier
  version and its HTML, which for a status message edited every 10 seconds of a
  turn (before #754) is most of what a fetch weighs.
- **Bounds.** None sent more than 5 minutes before; a message both fetched and
  queued counts once. At most 8 bots of the process, every workshop's, register
  and catch up at once, their deliveries included, each turn for at most 30
  seconds, its registration and catch-up together: a registration still going
  then is tried again, a catch-up counts as a failed try. A start for a
  5-minute-old message stays in the wake monitor's 10-minute window, after two
  capacity retries too, unless the wake first waits for a VM still stopping (up
  to 5 minutes more): that start alerts as unmatched.
- **Retries.** A fetch that fails is tried again between the queue's polls, 4
  times in all, 5, 10 and 20 seconds apart at least. Until it succeeds the bot's
  stored mark stays where the catch-up started, so a restart fetches again; after
  the fourth failure it is given up, and what it would have fetched is missed.
- **Never for a stale message.** Per VM, the waker keeps the last time it saw it
  pending or running, or asked for its start, in its states snapshot. A message
  sent more than 31 seconds before that time (Zulip's whole second and 30 for
  the clocks) wakes nothing: the VM was up after it, so its bridge has it. A live
  message is never that old, and waits for the next poll as before. A message
  sent after that limit and before the stop (the margin, then one 30-second poll,
  or while the waker was down, at most the 5 minutes) can't be told from one
  sent after the stop, and wakes the VM. A sighting or a start request stored
  with a time in the future (a clock set back since) counts as none.

### 13.5 VM commands and the sentinel (issue #204)

`!vm` works in topics (normal addressing) and owner DMs, even without a session.
It answers with the host name in bold, then one line per resource, measured as
for `!usage` below: the §4.6 bar first, so the bars line up and the text after
them may wrap on a phone, then how much is used and what of:
"🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜ 1.4 of 8 CPUs", "… 30% of 32 GB memory", "… 76% of
97 GB disk". CPU use is stated in CPUs, never as a percentage (issue
#441): one decimal, halves up, and plural ("1.0 CPUs"), while a count
agrees with its number ("0.5 of 1 CPU"); only a VM whose CPU count can't be read
shows the share, "18% of CPU". Memory and disk show the whole percent. The memory size is the larger of MemTotal rounded up and the size
sold, read from the online memory blocks (`lsmem`'s total) as the next multiple
of 2 GiB (of 4 GiB above 16) when that is less than 1 GiB more, else the blocks
to the nearest GB. The blocks count only when MemTotal is at least three
quarters of them and `/proc/meminfo` is procfs's own: lxcfs mounts its own file
there, giving a container its limit while `/sys` lists the host's blocks. So the
m7a.2xlarge (MemTotal 30.65 GiB, 31.375 GiB of blocks) says 32, and a container
says its own limit. On bare metal with 64 GiB or more, a partly filled 2 GiB
block counts whole and the label can say a GB or two too many: a known limit,
outside the workshop's VMs, for which `/sys/firmware/memmap`'s "System RAM"
total is the better input. The disk size is the root filesystem's, to the
nearest GB. A configured project on another filesystem adds a line labelled with
its path. A value that can't be read shows "unavailable" instead of a bar. "GB"
means binary gigabytes. A blank line follows; then one status line, in this
order: running turns ("no turns running", "1 turn running"), uptime in words
("up 2 hours"), the agents' sign-in, merged when they agree ("Codex and Claude
signed in", otherwise "Codex signed in · Claude not signed in"), and
"zab <version>". The sign-in checks run alongside the collection. Channel
replies never show process command lines; owner DMs add the processes using the
most memory or CPU ("Using the most: `sage` 3.2 GB memory, 2.1 CPUs · …", where
`top` would say 210%). `!usage` ends with one VM line,
`VM: 0.4 of 8 CPUs · memory 10% · disk 41%`, even when backend usage is
unavailable: the busy share of all CPUs over a `/proc/stat` sample, times their
count (the 1-minute load average, capped at the count, where that can't be
read), memory in use as (MemTotal − MemAvailable) / MemTotal, and the
root filesystem as `df` counts it, used / (used + available). `!vm` samples the
CPU for one second; `!usage` for a quarter second, taken while the backend
reports its usage, so it adds at most that to the reply. There is no swap figure
and no raw load average. Collection runs in a worker thread; `!usage` and
channel reports do not scan processes.

**`zab sentinel`** (`zab.tools.sentinel`, unit in §13.3) answers the owner's
`!vm`, `!restart` and `!restart codex` in a one-to-one DM to either bot, by the
bridge's owner rule (`allow_emails`/`allow_user_ids`, else the bot's owner), and
nothing else. For each bot it reads the bridge's zuliprc and registers its own
queue for DMs only. Its `!vm` has the same lines and processes, then one status
line: the bridge's state ("bridge running, last Zulip event 40 seconds ago",
"bridge running, but no Zulip event processed for 7 minutes: send !restart"
after 3 minutes without one, "bridge not running (failed): send !restart",
"bridge starting"), uptime and version; it has neither turns nor sign-in, which
a topic's `!vm` and DM `!status` give. `!restart` replies "🔄 Restarting your
bridge: running turns will be interrupted.", runs `systemctl --user
reset-failed` and `restart --no-block zab.service`, and confirms ("✅ Your bridge
is back …") once the heartbeat file moves after systemd shows a new main
process. Within 2 minutes without that it says the bridge is "starting again
but hasn't processed a Zulip event yet" when a new main process is up, and
otherwise that the restart failed. `!restart codex` restarts the
installed `codex-app-server*.service` units (which also ends idle Lean servers
under the daemon at once, #249; the bridge stops each one itself after
`[lean] reap_idle_minutes`, §7.12) and confirms with `is-active`. One restart a minute
across both bots; fixed argument lists only (CONTRIBUTING.md, rule 3); each command
logged with the sender's ID, never the text. While the organizers' hold
`/run/zab-idle-stop.hold` is unexpired (a rollout or a final sync, which may stop
the bridge on purpose: `--lean-reclaim`, `--restart-codex`), both restarts are
refused ("⏸ Nothing was restarted: your organizers are working on this VM …",
which for `!restart codex` says that they may be restarting Codex themselves)
and `!vm` doesn't advise one. A command it can't carry out (a `systemctl` that
can't be started counts as exit 126) gets a short "⚠️ … failed (…)" reply, and a
`!vm` report that Zulip refuses for good "⚠️ Zulip refused the VM report (…)". It
reads only the heartbeat files and the hold, and writes only its marks: idle stop
never counts it (`workshop/admin/README.md` §8).

The bridge leaves a one-to-one DM `!vm` or `!restart` to the sentinel when the
sentinel answers it: its mark `<state_dir>/sentinel-<bot>` (JSON: its process
ID, the boot ID, the bot's Zulip ID, when its queue was registered, and since
when it allows each sender) is from this boot, by a live process listening as
this bot, rewritten by a poll within 3 minutes, and both its queue and its
allowing the sender date from at least 5 seconds before the message's Zulip
timestamp. The sentinel answers by the same
rule, so a message the bridge leaves to it is one it answers. It rewrites the mark after every
poll and every reading of the users (at least every 10 minutes; at most once a
minute on a command from a sender it doesn't allow yet), removes it when it
starts and when Zulip drops its queue, and retries a failed poll on the same
queue. A reply Zulip
doesn't take for a transient reason (429, 5xx, no answer) is tried again for
about a minute, then its DM is handled again at the next poll; a restart's own
messages never stop it or run it twice.
Otherwise (no sentinel, one down or deaf, a DM the bridge's catch-up replays
from before the sentinel started, as after a wake, a sender allowed since the
sentinel last read the users) the bridge answers `!vm`
itself, and a `!restart` with "Nothing was restarted: …". So a DM gets a reply,
at worst two, unless the sentinel hangs or dies with it while its mark is still
fresh (up to 3 minutes), or Zulip drops the sentinel's queue before it fetches
the DM. In a group DM, and in a channel, `!vm` stays the bridge's and
`!restart` is no command, as before.

`zab doctor` reports each bot's sentinel by the same mark, the sender's rule
apart, as `<bot>:sentinel`: a warning, never a failure, when the unit isn't
installed or active or the mark isn't one the bridge would trust (§13.1, issue
#588). The organizers see it in `status.py`'s `sentinel` column, and so in
`!status` and the console, from the doctor's read: it reads only the unit's
state and the marks, nothing idle stop counts.

## 14. Observability

- **Logs.**
  - JSON lines in `<state_dir>/logs/zab.log`: rotating, 10 MB × 5 files.
  - Human-readable output on stderr.
  - Each record carries: `ts`, `level`, `bot`, `session`, `turn`, `event` and `msg`.
  - Message contents and agent text are logged only at DEBUG level, and redacted (§11.6).
- **Metrics.** Counters are written to the log every 10 minutes:
  - turns started, completed and failed, per backend;
  - Zulip requests and 429 responses;
  - reconnects;
  - the p50 and p95 latency from a message's arrival to its 👀 reaction.
- Interval records have `event="metrics"`, `bot`, `backend`, `window_s` and a
  `metrics` object; counters reset after each emission. A final partial interval
  is emitted on shutdown. Sum counters across bot records to group by backend.
  `turns_completed` means successful native completion; interruptions have a
  separate `turns_interrupted` counter. Delivery retries do not count twice.
  `reconnects` counts backend down→up transitions and successful lost-queue replacements.
- Each successful 👀 also logs `event="ack"`, `message_id` and `latency_s`, measured
  from the monotonic time its event batch arrives to successful reaction posting.
  This includes bridge processing/queue time, not Zulip delivery before the poll
  returns. `latency_e2e_s` measures from the Zulip message's integer-second
  `timestamp` to successful posting using wall-clock time, including pre-poll
  budget stalls. Missing/invalid/future timestamps produce null and increment
  `ack_e2e_unavailable`; clock-skew checks must pass before using this measurement.
  Replayed existing reactions produce no new sample. Unknown arrival
  times count as `ack_untracked`; bounded arrival eviction as `arrival_dropped`.
  p50/p95 use nearest rank over the last at most 10,000 samples in the interval;
  `ack_count`, `ack_samples` and `ack_samples_dropped` expose sample truncation.
  `ack_e2e_p50_s`/`ack_e2e_p95_s` accompany the local `ack_p50_s`/`ack_p95_s`;
  `ack_e2e_samples`/`ack_e2e_samples_dropped` report their separate sample cap.
- HTTP 429s log `event="zulip_429"` and numeric `retry_after`, without response
  bodies, URLs, credentials or message contents. All attempted API calls,
  including failed requests, retries, uploads and event polls, count once against
  the shared budget and `zulip_requests`. Signed CDN downloads are not API calls.
  An in-flight long poll never holds the outbound worker or the request budget.
- For the rehearsal gate, combine retained end-to-end `ack` samples across bots, **not**
  averages of their p95 values. See [the measurement recipe](testing/telemetry.md).
- **Health.** `zab status`, and the DM command `!status` (§4.6). The workshop admin tooling gathers `zab status --json` over SSH (`workshop/admin/README.md` §3).

## 15. Testing

### 15.1 Test layers

| Layer | Runs where | Credentials | Command |
|---|---|---|---|
| Unit (rendering, routing, store, config, parsing) | anywhere, in CI | none | `uv run pytest` |
| Contract (backend adapters against recorded fixtures) | anywhere, in CI | none | `uv run pytest -m contract` |
| Integration (core + fake Zulip + fake backends) | anywhere, in CI | none | `uv run pytest -m integration` |
| Live backend (real CLI, no Zulip) | a development machine with the CLI | its agent login | `ZAB_LIVE=codex` or `ZAB_LIVE=claude` `uv run pytest -m live` |
| Live end to end (real Zulip dev channel) | either machine | a dev bot in the `zab-dev` channel only | `ZAB_LIVE_ZULIP=1 …` |
| End to end (`e2e`: a wheel deployed to a test VM, driven as a test user; `tests/e2e/`) | the machine that operates the test VM | the test users' and the test participant's bots, the test infrastructure's AWS access | `uv run --with boto3 python tests/e2e/run.py` (sets `ZAB_LIVE=e2e`) |
| Admin end to end (`admin_e2e`: a test admin server; ADMIN §7) | the same | the same, and the test admin server's | `ZAB_LIVE=admin-e2e uv run --with boto3 python tests/e2e/admin/run.py` |

Offline, `pytest` skips every `live`, `e2e` and `admin_e2e` test. The live and end-to-end suites never run in parallel (`-n`): their scenarios share one VM and run in order. Tests post only in the `zab-dev*` channels and the test participant's channels (§15.3), and never as a real person's account.

### 15.2 Fixtures and fakes

- **Codex.** `zab dev record-codex` connects to the real daemon, runs a scripted scenario, and writes the JSON-RPC transcript to `tests/fixtures/codex/<version>/<scenario>.jsonl`. It uses a scratch repo under `/tmp`.
  - Scenarios: a simple turn; a command with an approval; a steer; an interrupt; resume after a disconnect.
  - `testing/fake_codex.py` replays such transcripts over a socket, or over stdio. Its `FakeAppServer` is a scripted app-server over the daemon's socket, keeping threads and turns in memory (`thread/fork` included, #202), so the real backend and the core run against it offline; a test checks its wire against the pinned schemas, with the requests' params closed so that an unknown field fails.
- **Claude.** `zab dev record-claude` does the same with the real `claude -p` and writes `tests/fixtures/claude/<version>/<scenario>.jsonl`.
  - Scenarios: a simple turn; a tool use; an error; a permission denial.
  - `testing/fake_claude.py` is an executable script that replays a fixture chosen by an environment variable. Tests point `backends.claude.binary` at it.
- **Zulip.** `testing/fake_zulip.py` is an in-process ASGI or `httpx.MockTransport` fake implementing the endpoints in §4.10.
  - It supports event queues, 429 injection and `BAD_EVENT_QUEUE_ID` injection.
  - It keeps its messages in memory so tests can assert on the transcript.

Fixtures must be scrubbed of tokens and personal paths before they are committed. `zab dev scrub` does this; review its output.

### 15.3 Dev environment

Live acceptance tests need human senders, several bots and several channels. The maintainer sets up the following in the Zulip organization the tests use:

| What | Details |
|---|---|
| Two human test users, `zab-tester` and `zab-tester2` | Ordinary member accounts, not a real person's. `zab-tester` is in the `allow_user_ids` of every dev bot; `zab-tester2` is not (AT-7). Each development machine gets `zab-tester`'s API key in `~/.config/zab/tester.zuliprc`, and `zab-tester2`'s in `tester2.zuliprc`. A test driver in `tests/live/` uses these accounts to post and react like a person. |
| Dev bots | Per development machine: one for its backend (`Codex · dev-<machine>`, `Claude · dev-<machine>`), one spare for AT-7's "another bot", and 5 soak bots on one machine for AT-23 (fake backends). Their allowlists are set explicitly. |
| Channels | `#zab-dev` (private; testers and dev bots); `#zab-dev-2` (private; same members, for AT-9's move); `#zab-dev-public` (public, for AT-8). |
| A test VM | A workshop VM of a test participant, with its own bots, channels and repository, launched from the workshop image and tagged as test infrastructure. The end-to-end suite deploys the wheel under test to it and posts as the test users. AT-21 needs a fresh Ubuntu 24.04 VM with both CLIs signed in. |

Credential files for test users and dev bots live only on the development machines and the test VMs, never in git.

## 16. History

*This section is history, kept as a record.* The bridge was first built for a workshop for 35 number theorists on October 9–10, 2026, by the maintainer, with coding agents doing much of the implementation and review, on this plan. The acceptance tests (AT-n) are in §17. The releases since are in CHANGELOG.md, and §18 lists what is still deferred.

| Milestone | Due | Contents | Done when |
|---|---|---|---|
| **M0** | Mon Sep 28 | Package skeleton and the `cli.py` dispatcher, including the `zab dev` and tools hooks (§13.1). Then config (§5), store (§6), Zulip client and poster (§4.10), fake Zulip, `zab init`, `zab doctor` (partial), CI. | Unit tests pass. `zab doctor` authenticates a dev bot. |
| **M0** | Mon Sep 28 | Any `contract:` PR needed after a close reading of `types.py`; Claude driver `zab dev claude-turn`; `zab dev record-claude` and `zab dev scrub`; scrubbed fixtures; `render/markdown.py` (§11.2, §11.5–§11.7) with table tests; `workspace/local.py`; the `workspace/git.py` snapshot, diff and GitContext (§10.4–§10.6) | Fixtures committed. Rendering and git unit tests pass. |
| **M1a** | Tue Sep 29 | `render/links.py` (§11.3, §11.4); `render_diff` (§11.8); a complete `WorkspaceManager`: `prepare` (with worktrees), `save_attachment`, `check_upload` | Unit tests against a temporary git repository and a fake upload callback cover the logic of AT-12, AT-13, AT-19 and AT-22 |
| **M1** | Wed Sep 30 | Router, SessionActor and turn lifecycle, including held messages (§7.3, §7.5); card, status and approval messages (§7); Codex backend (§8), including approvals, steering, interrupt, restart recovery and co-presence; the renderer and `WorkspaceManager` wired in | AT-1 to AT-3, AT-5 to AT-10, AT-12 to AT-14 on a Codex development machine, against the `zab-dev*` channels (§15.3) |
| **M1b** | Wed Sep 30 | Workshop tools: `zab setup-repo` and `zab preflight` (§13.1). `workshop/ami/` build script and `workshop/aws/launch.py`, `teardown.py`, which the maintainer runs. Tag `v0.0.2`: the tools work, the bridge does not yet. | The image is built from `v0.0.2` on Thu Oct 1 |
| **M2** | Sat Oct 3 | Claude backend integrated (§9); `zab service install` (§13.1, §13.3), workshop profile (§5.3), attachments (§10.3). Tag `v0.1.0rc1`. | AT-4, AT-11, AT-15, AT-16 and AT-19 on the development machines; AT-21 on a fresh VM (§15.3) |
| **M3** | Mon Oct 5 (dry run with colleagues; go/no-go) | Hardening: reconciliation (§7.8.2), rate limits (§4.10), `!handover`, `!attach`, `!diff`, `!file`, `!queue`, redaction; soak test; admin scripts (`workshop/admin/README.md`). | All of AT-1 to AT-23. The dry run passes the go/no-go criteria. |
| Freeze | Tue Oct 6 | Tag `v0.1.0`. The rollout (`workshop/admin/README.md` §2) upgrades every VM to it; the image is not rebuilt. | — |
| Rollout | Wed Oct 7 | Bots, channels, configs, upgrade to `v0.1.0`, services, run by the maintainer with the admin scripts (`workshop/admin/README.md`) | `zab doctor` green on all VMs |
| Workshop | Fri Oct 9 – Sat Oct 10 | | |
| **M4** | from Oct 12 | Phase 1b (§18.1) | — |

Between Oct 6 and Oct 10, only fixes for bugs found in the dry run or on the VMs were allowed, each in its own pull request, each approved by the maintainer.

## 17. Acceptance tests

Unless stated otherwise, run them in a private channel mapped to a scratch repo, with the owner's account.

| ID | Test | Expected result |
|---|---|---|
| **AT-1** | New session: in a new topic, post `@**Codex · laptop** list the files and summarize the README`. | Within 5 s: 👀 reaction, card, status message. Final answer posted; the card is never edited. `codex resume <id>` in a terminal shows the same conversation. |
| **AT-2** | Follow-up: a second message in the same topic, without a mention. Then, in a new topic of a single-human channel, a first message without a mention, and its follow-up. Then, in another new topic, a typed `@codex` (plain text, not Zulip's typeahead), and a follow-up without a mention. | It continues the same native session of the agent that was mentioned, and the VM's other signed-in agent answers it too, in a new session (#713). Each of the VM's signed-in agents answers the new topic's message and its follow-up (#687). Only Codex answers the typed mention; each answers the follow-up after it (#713). |
| **AT-3** | Steer (Codex): a message sent while a turn runs. | ↪️ reaction; the thread history shows it as a user message inside the running turn. |
| **AT-4** | Hold (Claude with `steer = false`, §9.7): two messages sent while a turn runs. | Both get ⏳; they are delivered as one coalesced turn (§7.5) after the running turn completes. |
| **AT-5** | Stop: `!stop`, or 🛑 on the status message. | The turn ends within 15 s; the status message shows ⏹. |
| **AT-6** | Approvals (Codex, approval policy `on-request`): trigger a command approval. | Approval message mentions the owner. 👍 runs the command; 👎 declines it. Answering in the TUI edits the message to "resolved elsewhere". |
| **AT-7** | Non-owners: messages from another human and from another bot in the topic. | Ignored: no reaction, no delivery. |
| **AT-8** | Public channel: mention the bot in a public channel. | One refusal message; no session created. |
| **AT-9** | Rename and move: rename the topic, resolve it, move it to another private channel, then post there. | The session continues; the Codex thread name follows the rename. |
| **AT-10** | Restart (Codex): kill `zab` during a turn and restart it. | The final answer is posted exactly once; the status message is finalized. |
| **AT-11** | Restart (Claude): kill `zab` during a turn and restart it. | The status message reads "interrupted by bridge restart". The next message resumes the same session id. |
| **AT-12** | Links: answers that reference a tracked, pushed file with a line number; an unpushed text file; an unpushed file of another type; a PNG plot. Then again with the file server on (§10.7). | GitHub permalink at the pushed SHA with a line anchor; the text file uploaded as `<name>.<ext>.txt` (§11.3); a code span with *(local)*; uploaded image that previews inline. With the file server, the pushed and unpushed files link to the turn's snapshot. |
| **AT-13** | Math: an answer containing `$x^2$`, `\(a\)`, a multi-line `$$` block, `\[ \]`, `$100 and $200`, and a code block containing `$`. | Correct Zulip math for the first four; the prose dollars and the code block unchanged. |
| **AT-14** | Mentions: the agent is asked to write `@**all**`. | Nobody is notified; the text appears as code. |
| **AT-15** | Long answers: one of about 15 000 characters with a code block that straddles a split point, and one of about 45 000 characters (more than 4 × (`max_message_length` − 200)). | The first is posted as (1/2) and (2/2), with the fence closed and reopened. The second is posted as its first part plus an uploaded `.md`. |
| **AT-16** | Attachments: upload a PNG and a CSV with a message. | Both files are saved in `.zab/inbox/`; the agent can describe the image and read the CSV. |
| **AT-17** | Rate limits: the fake Zulip returns 429 during a busy turn. | Backoff, no crash; status edits coalesce; the final answer is still delivered. |
| **AT-18** | Event queue loss: inject `BAD_EVENT_QUEUE_ID` and send two messages during the gap. | Re-register; each message is processed exactly once. |
| **AT-19** | Worktrees: two concurrent sessions in worktree mode in one project. | Separate branches and directories; no interference. |
| **AT-20** | Handover: run `!handover`. | `HANDOVER.md` updated, committed and pushed; the answer includes the commit hash and a permalink. |
| **AT-21** | Fresh install: on a fresh Ubuntu 24.04 VM (§15.3), with both CLIs logged in, follow the README using the current tag. A person runs this. | `zab doctor` (including `--service-env`) is all green within 10 minutes, and AT-1 passes with the service running. |
| **AT-22** | Secrets, run only against fake Zulip and a temporary `HOME` containing **decoy** files (`.env`, `.codex/auth.json` with fake contents): `!file .env`, `!file ~/.codex/auth.json`, and an answer containing a fake `sk-ant-…` key. | Both uploads refused; the key is redacted. Never run this with real credentials present (CONTRIBUTING.md, rule 2). |
| **AT-23** | Soak: 5 bridges × 3 concurrent sessions × 30 min against the real `zab-dev` channel, driven by fake backends. | No lost or duplicated messages, no unhandled exceptions, stable memory, no 429 failures that go unrecovered. |
| **AT-24** | Two humans and their bots share a private channel. One person posts; the other mentions their own bot to use the discussion, then posts unmentioned chat. Repeat with `multi_party=false` and a single-human channel. | Only owner mentions trigger in multi-party mode, a typed `@claude` or `@codex` included (#713); prior discussion has role labels, controls/asides/own output are absent, clipped context has a full local transcript, and accepted input advances the cursor. In the single-human channel a message without a mention reaches each of the VM's bots; with `multi_party=false`, only the bots with a session in its topic (§4.5). It needs two people, live. |

## 18. Deferred work

### 18.1 Phase 1b (M4, after the workshop)

- **Backfill.** Import historical sessions as resolved topics in per-project archive channels:
  - Codex rollouts from `~/.codex/sessions/**/rollout-*.jsonl`; Claude transcripts from `<config_dir>/projects/*/*.jsonl`, for all accounts.
  - One message per turn, containing the prompt, the final answer and a spoiler with the tool summary, with the original timestamps in the text.
  - Rate-limited to fit within the Zulip limits. Resumable.
- **Automatic mirroring** of terminal-started Codex threads into their project's channel (config flag).
- **Account moves.** `!account NAME` moves a Claude session to another account by copying its transcript, then resumes it there.
- **Platform.** launchd service install for macOS; cross-user isolation where the agent has an account of its own (§12.3).

### 18.2 Phase 2

- **Native catch-up in shared topics.** Several agents and several people can share a topic, with explicit addressing, attribution and loop prevention, and each agent gets what it missed as quoted context in its prompt (§4.5). Left: passing that context natively (for Codex, `thread/inject_items`).
- **Interactive Claude approvals** through a permission-prompt MCP tool that talks to the bridge over a local socket.
- **Live Claude co-presence** through Claude Code channels, once they leave research preview.
- **Topic operations.** Splitting a topic in place. (Forking a session into a new topic, `!fork`, is implemented: §7.11.)
- (**A viewer** for unpushed files is built: the file server, §10.7, for anyone with a link. A large diff is uploaded, §11.8.)

---

## Appendix A. Canned prompts

### A.1 Bridge context

Passed as `SessionSpec.context_prompt`. For Claude it goes in `--append-system-prompt`. For Codex it goes in `developerInstructions`, and, after a reopen or in a fork, also into a `<zab-context>` prefix on the first input (§8.4, §8.8).

```
This session is mirrored to a Zulip topic by zab. Your final message is posted as the
answer; make it self-contained. When you mention files in your answer, give their paths relative to
the repository root (for example fermat_little/README.md:1), not relative to a subdirectory.
Use $$...$$ for inline math and fenced math blocks for display math. Save images/PDFs inside
the repository and mention their paths. Attached files are under .zab/inbox/. Never print secrets.
Follow the repository's instructions about committing and pushing. Other agents may share this
topic; a message that mentions you is addressed to you, even when it also mentions another agent.
```

With `bridge.multi_party` on (the default), one more line follows (§4.5, "Multi-party channels"):

```
Other participants and their agents may be in this channel; act only for your user, and do not answer questions addressed to others unless your user asks.
```

The text is `zab.core.creation.CONTEXT`. The bridge passes it, with that line, to every session it creates, reopens, attaches or forks. On the workshop VMs the agents' own instructions (`~/.codex/AGENTS.md`, which `~/.claude/CLAUDE.md` imports) say more, and are installed by the rollout (`workshop/admin/README.md` §2.2).

### A.2 Handover (`!handover`)

```
Update HANDOVER.md at the repository root so that someone new (a person or a fresh agent session) could
continue this work without this conversation. Include: the goal; the current state (what works, what does
not); the key decisions and why; the open problems and next steps; the exact commands to reproduce the main
results; and pointers to the relevant files. Keep it under about 150 lines. Then commit HANDOVER.md together
with any other finished work and push. End your reply with a short summary, the full commit hash
(`git rev-parse HEAD`) and the path `HANDOVER.md`.
```

The last sentence names the hash and the path explicitly: with "reply with a short summary and the commit hash", haiku left the hash out in three of four live runs (2026-10-03), and the path in backticks is what the renderer turns into the permalink (§11.3).

## Appendix B. Message templates

### B.1 Card and `!session`

The card is posted once and never edited, including on settings/state changes or
detach. Keep `card_message_id` as the anchor for topic moves/renames/deletion (D7).
The marker is unchanged, so existing and new cards remain recognizable to the
router, allowlist bypass, and peer-context filter.

```
**zab session** · 🤖 Session started. Write in this topic to continue (mention me if others are in the channel); !help lists commands, !session shows details.
```

A fork's card (§7.11) links back to the topic it was forked from:

```
**zab session** · ⑂ Forked from #**channel>topic**, with its conversation through its last completed turn and its files, on a branch of its own. Write in this topic to continue (mention me if others are in the channel); !help lists commands, !session shows details.
```

The card of a fork that joins another bot's fork of the same message (§7.11, #712):

```
**zab session** · ⑂ Forked from #**channel>topic**, with its conversation through its last completed turn, in the files and on the branch that this topic's other agent works on. Write in this topic to continue (mention me if others are in the channel); !help lists commands, !session shows details.
```

`!session` posts backend/machine and native id; workspace, mode and branch;
current model/effort from effective_settings where available, approvals, sandbox
and account; state, turn count and known session tokens (unknown if unreported).
The last line is explicitly for SSH users and uses the backend's resume command,
including Claude account/CLAUDE_CONFIG_DIR and concurrent-use guidance. Details
are never mistaken for an anchoring card. Detach cleanup advice is a new reply.

### B.2 Status message

While running:

```
⏳ **Working** · started <time:2026-11-02T14:03:00+00:00> · 3 actions · model default · effort default
- ✅ `pytest -q` (exit 0, 12 s)
- ✅ edited `src/parser.py`
- ⏳ `python scripts/search.py --bound 1e6`
Plan: ☑ reproduce · ◐ fix parser · ☐ add test
> The parser drops the last field; fixing it, then a test.
>
> Now running the search up to 10^6, about 5 minutes; I will check it every minute.
```

The quote holds the agent's last three notes, newest last, a paragraph each, so that markup a cut left open in one cannot reach the next (§4.9).

When finished:

````
✅ **Done** · 3m 02s · 9 actions · 3 files changed (+40 −12) · 85k tokens
```spoiler Actions
1. ✅ `rg -n parse_header` …
…
```
````

Other first-line variants: `⏹ **Stopped**`, `❌ **Failed**: <kind>`, `⏸ **Waiting for approval**`, `⚠️ **Error**` (the session failed during a running turn) and `**Backend reconnecting**` (§7.8.3). A turn that has ended but whose answer is not yet posted shows how it ended, never the session's `error`.

The status message's first line, alongside the turn's start (running) or duration
(finished) and action count, shows that turn's model and `effort <value>` on both
running and final statuses.
`TurnStarted` optionally carries `model`, `effort`, and `effort_supported`;
backends may repeat it for the same turn when initialization resolves the values.
Persist a per-turn snapshot. Later `!model`/`!effort` changes affect `!session` and the
next turn, never a previous turn's status. Unknown model/effort display as
`model default`/`effort default`; `effort_supported=False` omits effort entirely.
For Claude, model and effort support come from that turn's `system/init`; effort
comes from the actual CLI argument, not the model's prose. For Codex, use the
turn's reported model/effort and resolved thread values where needed.

### B.2a Diff message

Posted after the final answer (§11.8): one visible line, then one spoiler.

`````
Changes: 3 files (+40 −12), 1 new: [turn4.diff](/user_uploads/…/turn4.diff)
```spoiler Details
- [`proof/Solution.lean`](https://files.<domain>/<repo>/<sha>/proof/Solution.lean) new, +30 -0
- `src/foo.py` +10 -12
- `.env` changed (contents hidden)
```
`````

With a small patch there is no upload link, and the patch follows the list inside the spoiler, in a ```` ```diff ```` block.

### B.3 Approval message

````
@**<sender of the turn's last message>** approval needed: run a command
```
sudo apt-get install -y pari-gp
```
in `~/work/sandbox` · reason: install PARI/GP
React 👍 approve · 🔁 approve for this session · 👎 decline (or reply !approve / !deny)
````

### B.4 Bridge notes

The marker of §4.9 (issue #677), then the note. Claude Code's denial of `cd … && echo a; rm -f runs/*` in `bypassPermissions` (§9.4):

````
⚠️ **Bridge** (not Claude): Claude Code, the program Claude runs in, blocked this command under its safety rules. It needed approval, which can't be given from Zulip, so it did not run. Claude was told and can carry on.
- ` cd /home/agent/work/demo/search && echo a; rm -f runs/* `
  Claude Code's reason: ` Dangerous rm operation detected: '/home/agent/work/demo/runs/*' This command changes directories before the removal, so the relative glob target cannot be statically resolved. This requires explicit approval and cannot be auto-allowed by permission rules. `
````

Several denials of one result say "blocked these 2 commands" (or "actions", when one of them is not a `Bash` call) and "They … so they did not run", with a line each. A denial whose `tool_result` gives no reason, such as one in `dontAsk` mode, has no reason line.

The other notices and a ⚠️ line:

```
⚠️ **Bridge** (not Claude): Claude Code, the program Claude runs in, is using an API key (source: ` ANTHROPIC_API_KEY `) instead of a Claude account sign-in.
⚠️ **Bridge** (not Claude): Claude Code, the program Claude runs in, used a third-party provider for this turn: ` bedrock `.
🔧 **Bridge** (not Codex): Codex asked for something the bridge can't do from Zulip (` item/tool/requestUserInput `), so the bridge declined it. Codex was told and can carry on.
⚠️ **Bridge** (not Codex): Codex reported an error outside a turn: <message>
⚠️ **Bridge** (not Claude): This session needs recovery; use !status to retry.
```

## Appendix C. References

- Codex app-server README (pinned version): `codex-rs/app-server/README.md` in `openai/codex`
- Codex authentication, including device-code login: https://developers.openai.com/codex/auth
- Claude Code headless mode and CLI reference: https://code.claude.com/docs (pages "headless" and "cli-reference")
- Claude Code authentication: https://code.claude.com/docs/en/authentication
- Claude Code legal and compliance (the credential rules): https://code.claude.com/docs/en/legal-and-compliance
- Zulip REST API: https://zulip.com/api/ (pages "Register an event queue", "Get events", "Send a message", "HTTP headers" for rate limits)
- Zulip markdown, including math and spoilers: https://zulip.com/help/format-your-message-using-markdown
- Prior art: `tijszwinkels/mattermost-agent-bridge` (channel per session, held-message coalescing), `RuslanAtakyan/tg-agent-bridge` (topic per session), `kcosr/codex-threads` (shared app-server daemon)
