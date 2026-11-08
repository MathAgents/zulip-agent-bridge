# Changelog

## 0.2.19 — 2026-11-07

What is new since 0.2.18: the admin server's in-band upgrade, `!upgrade-server`, for the releases
after this one (#569), and `upgrade.py`, which reviews, signs, verifies and pushes a release on the
deployer's machine (#772); the automated month-end of the kept VMs (#570, #883); `!unmentioned
last` (#878); answers at once after a Zulip outage (#964); 10 turns of each agent at once on the
workshop VMs (#919), and their `apt-get update` repaired by the rollout and the trust fix (#947);
releases from the public repository (#958); Codex sessions reopened without loading their threads
(#632); sub-agents across a turn's end and `!stop` (#660, #847, #858), and the agents' notes while
one works (#668); a Codex identity record that vouches only within its app-server's run (#830);
trust stores without emSign ECC Root CA - G3, with a fix for VMs launched from an older image, and
Claude Code on the system store (#671, #816, #882); the edit bounds kept across restarts (#757,
#806); redaction and regular-expression memory (#666, #821, #833, #836); each `!retire` step on the
audit log as it ends (#837, #855); the export check's hard links and ambiguous archive headers
(#819, #888); two options of `deploy.sh` and a base signed by a retired key (#772, #587); the
workshop scripts' required scope and region (#808); images built from pinned releases of
Macaulay2's packages and PARI's data (#945); and documentation. Each change cites its issue and
then its pull request.

**Upgrade notes.**

- **This release is the last one deployed by hand** with `deploy.sh` from a deployer's machine,
  as before (#569). Its deploy installs and starts `!upgrade-server`'s two root helpers' path
  units (`zab-admin-upgrade-plan.path`, `zab-admin-upgrade.path`) and their state directory
  (`/var/lib/zab-admin-upgrade`); `admin.toml` needs no new key for the deploy. Then, once, over
  SSH (BOOTSTRAP §13.4): make the server's deploy key, add its public half to the release
  repository as a read-only deploy key, and set `release_repo` in `admin.toml`, on the server and
  in your copy, only after this release is deployed, since 0.2.18's loader refuses the key.
  Until then `!upgrade-server` is refused. From the next release on, a deployer reviews with
  the running release's `deploy.sh --review`, and sends
  `!upgrade-server vX.Y.Z sha256=<the review's SHA-256>` to the global admin bot once the
  release's signed tag is in `release_repo`. For the public repository's owner, `release_repo`
  is that repository, `MathAgents/zulip-agent-bridge`, whose published tags the server fetches
  (#958). The running release's tag there must name the commit the server's `deploys.jsonl`
  records, so a server running a release deployed from another repository's commit first takes
  one hand deploy of a published release: BOOTSTRAP §13.4, *Onto the public repository's
  commits*, with a check that ties the published commit to the one reviewed. For anyone else,
  `release_repo` is a private repository of their own holding the release tags (§13.4 makes
  it). BOOTSTRAP §13.4 also gives the commands for what still needs `deploy.sh`.
- **Roll the release out with `restart-codex`:** `!rollout vX.Y.Z all restart-codex`, or
  `workshop/admin/push_config.py … --restart-codex` (#830). A VM where a Codex turn was running
  keeps its daemon, and its row's note says `codex daemon NOT restarted: N Codex turn(s) running;
  rerun with --restart-codex when idle`: send the same line with that VM's slug in place of `all`,
  a minute or more after its Codex turns have ended. That rerun may report the same note, since
  the bridge's store counts such a turn as running until its topic reopens: the daemon's restart
  is then no longer needed. Two other notes, `codex daemon NOT restarted: zab status unreadable;
  rerun later` and `codex daemon NOT restarted: the bridge didn't stop (…)`, and `codex daemon
  restart failed: …`, call for the same rerun, a few minutes later. Without the restart, each
  Codex topic the old bridge had loaded holds its next message under "This session needs
  recovery; use !status to retry."
  until the app-server unloads its thread, a minute after the old bridge stopped, or after the
  topic's turn ended if one was running then; a `!status` there, or the bridge's next restart,
  then reopens it. Claude topics are not affected. The rollout also writes
  `max_concurrent_turns = 10` (#919), and repairs `apt-get update` on VMs from the Oct 19
  image (#947): their rows note `apt: removed …; held …`. A row's `apt: kept …` names a VM whose
  Macaulay2 source the rollout left alone, since it names other sources too, isn't a regular
  file, or is too large to be a sources file: look at that file and fix it by hand (`workshop/admin/README.md` §2.2).
- **The admin server.** Before deploying, check that workshops sharing a `refserve_url` share
  their `github_org`: the loader now refuses a file where they don't (#771), and the deploy's
  self-check stops there. The first deploy of this release replaces the server's uv with 0.13.7
  (#816). The deployer's machine needs `python3` 3.12 or newer (#781).
- **Trust stores** (#671, #816, #882). The admin server's venv gets certifi 2026.10.23 at its
  deploy. A rollout installs the bridge with the newest certifi, unhashed, through the VM's own
  uv. A VM launched from an image built before this release keeps the certifi of its other
  environments, which holds the root, and, on an image built before 2026-10-20, a uv older than
  0.13.4, which holds it too: `workshop/admin/trust_fix.py` (its README's §10) fixes them, and
  sets `CLAUDE_CODE_CERT_STORE=system` in Claude Code's managed settings (#882), a dry run
  first, then `--apply`, once per workshop. Run it after the admin server's deploy and before
  the rollout, so that the rollout's install goes through the fixed uv. On a VM of the Oct 19
  image whose `ca-certificates` is older, it first repairs that image's expired apt source, which
  would fail its `apt-get update` (#947). Each machine's system
  store needs `ca-certificates` 20260816ubuntu0.24.04.1 or newer, which unattended upgrades
  install. A waker installed by `workshop/admin/push_waker.py` gets certifi 2026.10.23 or newer
  at its next `--apply`. An image built with this release's `build.sh` holds none of these.
- **The audit log** (#837, #855): each step of a `!retire`'s row, and of a month-end run's, is a
  `retire_step` record, written as the step ends. A tool of your own that reads `audit.jsonl`
  sees two new events: `retire_step`, and `month_end` for the schedule's own events (#570; ADMIN
  §2.8).
- **Codex sessions reopen lazily** (#632): after the upgrade, and after each boot or wake, a Codex
  topic's thread loads at that topic's next message. Nothing to do.
- **Hand runs of the workshop scripts** need `--tag` (the workshop's scope) and, where they take
  one, `--region`; `build_ami.py` needs `--name` to build (#808). The admin bot passes them
  already.

**Changes since 0.2.18**

**The bridge, on the participants' VMs and your own machines**

- **After a Zulip outage longer than ten minutes, the answers come at once** (#964, PR #965;
  SPEC §7.8.2, §4.4). Zulip drops an event queue nobody polled for about ten minutes. The
  bridge's recovery used to confirm every session's card and reconcile every recent topic before
  anything else: a request or two per session against the bot's 150 a minute. Only then did it
  retry the answers Zulip had refused and read the messages sent meanwhile. Now it confirms only
  the cards of sessions with work in flight, then catches up the gap, retries the answers, and
  leaves the rest to the background housekeeping, as start-up has done since #443. On the test
  VM, end-to-end scenario 4's answer after the block had grown with the sessions, from 3 s on
  Oct 19 to 547 s on Nov 6, and missed its 600 s bound twice at v0.2.19's candidate; with the
  fix it came 6.3 s after the block. A follow-up for the Codex backend's shutdown in spawn mode
  is #967.
- **`!unmentioned last`: a message without a mention can stay with the last agent named** (#878,
  PRs #879, #881, #897; SPEC §4.5, "The rule `last`"; §4.6). A participant sends `!unmentioned
  last` to either bot in a direct message, and a message that names no bot then goes to the
  agent or agents that the topic's last prompt with a mention named, completed or typed, and to
  both where none has been named yet, or where none of those can take it; a command with a
  mention, such as `@codex !usage`, changes nothing, except `!new`, with or without a prompt,
  which records the agents it names as a prompt does, and a `!detach`, which takes the agents
  it detaches out of the record. `!unmentioned all`, the default, is #713's rule. One setting
  for all of the VM's agents, in every channel, kept across a restart, a stop and a wake;
  `bridge.unmentioned` in `config.toml` gives the default. An unmentioned `!stop` under `last`
  stops the named agents' turns only. The topic's record of who was named follows a moved
  session card, and a `!fork` starts its topic's record with the agents that forked, so a
  follow-up there stays with them. `!unmentioned` alone says which rule is in force, a DM
  `!status` shows it, and a topic's `!session` says where a message without a mention goes
  there. Multi-party channels and `bridge.multi_party=false` keep their rules; with the latter,
  `!unmentioned` says that the setting has no effect. The waker is unchanged: under `last` both
  bots still react ⏰ to an unmentioned message that wakes a VM, and the agent the record leaves
  out then stays silent. The old per-bot record (`topic_addressing:…`, unread since #713) is not
  used: the new one is the VM's, `topic_addressees:<stream>:<topic>`, written under both rules.
- **One answer to `!help`, `!vm` or `!tools` without a mention next to a `!detach`** (#899, PR
  #909; SPEC §4.6). They came from both agents, or from neither, when a `!detach` of one agent
  came just after them, or just before them, and one bot read both messages before the other, as
  at a wake. Now the first by name of the agents that the channel and their allowlists let them
  reach answers (under `!unmentioned last`, of those the topic's last mention named), whatever
  the agents' sessions in the topic, a `!detach` there or their sign-ins: an agent that waits
  for a mention after `!detach` still answers them. A topic's `!unmentioned` too. Who gets a
  message without a mention is unchanged. `!stop` with no turn running, and "No session here",
  still depend on the topic's sessions, and can still come from both agents, or from neither,
  next to a `!detach`.
- **Workshop VMs run 10 turns of each agent at once, not 4** (#919, PR #921; SPEC §7.10,
  ORGANIZERS §4.1): an agent's eleventh turn waits with ⏳, where its fifth did. It takes effect
  at the next `!rollout`, which writes `max_concurrent_turns = 10`. The schema's default (3), which
  `zab init`'s default profile leaves in force, and `zab init --profile workshop`'s 2 are
  unchanged.
- **The agents note progress while a sub-agent works** (#668, PR #922; the agents' instructions,
  *Sub-agents*). While a background sub-agent runs, the instructions tell each agent to keep
  reading the sub-agent's log every 30 to 50 seconds until it is done, and to write a one-line
  note right after each check's result, before the next check, never two checks in a row
  without one: how long it has run and what its log shows. The rule is the instructions', not
  the bridge's (0.2.11's *Known limits*). Claude Code's message on starting a background sub-agent
  says to report nothing of its results before it finishes, and with v0.2.11's text Claude
  sometimes chained log checks with no note between them, up to 130 seconds without one
  (0.2.11's *Known limits*). The instructions now say that this warning is about the
  sub-agent's findings, not its progress: the notes are still due, a status question still
  gets an answer from the log, and only a guess at what it will find is left out. Codex gets
  the same text. The VMs get it at their next `!rollout`.
  - *Measured for Claude on the test VM*, in one run; Codex wasn't measured with this text
    (end-to-end scenario 17's new part: three topics at once, each
    with Claude waiting on a background sub-agent doing thinking work, from the session
    transcripts; DECISIONS, verification log, 2026-11-07): with this text, the longest gaps
    between notes were 42.8, 43.2 and 42.1 seconds, with no two checks in a row without a note.
    Before the sentence "Write the note right after each check's result, before the next
    check" was added, they were 43, 88 and 127 seconds, two of the three topics running two or
    three checks back to back. The participant sees the notes in the status message, whose
    notes are refreshed about every 5 minutes (#754).
  - The new part requires every gap to be at most 65 seconds, a target rather than a bound in
    a release candidate's run (`run.py --require rc`).
- **Each agent's tool processes get `ZAB_SESSION`** (#655, PR #927): the zab session's id, next
  to `ZAB_BOT` (SPEC §5.6), for the background jobs #655 will record; nothing reads it yet, and
  like `ZAB_BOT` it grants nothing. A contract change, backward compatible: `SessionSpec.session`,
  empty by default, which unsets an inherited one.

- **A Codex identity record vouches only within its app-server's run** (#830, PR #854; SPEC §5.5,
  §7.3, §8.6, §8.7). The record of the git identity the bridge applied to a Codex thread now
  names the app-server's run: the boot, the process and its start time. After a boot, or a
  restart of the daemon alone, a thread that a terminal's client loaded before the bridge reopened
  it is refused rather than trusted on the earlier run's record, as is `!attach` of a detached
  session whose thread another client loaded since. A session in error whose turn is still running
  answers a message with "⚠️ **Bridge** (not Codex): This session needs recovery; use !status to
  retry.", not with ⏳ alone.
- **Codex sessions reopen without loading their threads** (#632, PR #818; SPEC §5.5, §7.8.1,
  §8.6). At its start the bridge resumes only the threads the app-server holds loaded; the others
  open from a snapshot of their turns and load at their first turn, `!fork`, `!btw`, `!model` or
  `!effort`. On a test VM after a boot, the `lean-lsp-mcp` processes went from 491 (25.9 GB) to
  none, and memory used from 30.5 GB to under 1 GB.
- **Sub-agents across a turn's end** (#660).
  - **Claude** (PR #814; SPEC §9.6, §9.7): while a background sub-agent runs, the bridge keeps
    Claude's input open after a result, so a message sent then gets ↪️ and a loop of its own,
    not ⏳ until the sub-agent ends; it closes at the latest 10 minutes after the last result. A
    turn `!stop`ped after one of its results ends ⏹, interrupted, not completed.
  - **Codex, a turn that ends with a sub-agent running** (PR #822; SPEC §8.5) says so: "⚠️
    **Bridge** (not Codex): A sub-agent was still running when this turn ended. Its answer won't
    be posted here: ask Codex about it in a new message, or send !stop to stop it."
  - **`!stop` stops a Codex thread's sub-agents** (PR #825; SPEC §8.4), at any depth up to 8,
    then the thread's own turn. A sub-agent attached as a session of its own in another topic is
    left to that topic's `!stop`.
  - **`!stop` with no turn running** (#847, PR #851; SPEC §4.6, §7.7, §9.6) no longer answers
    "Unknown command": Codex stops the sub-agents that ended turns left running ("No turn is
    running; stopped N sub-agent(s) that an earlier turn left running."), and both agents say
    when nothing runs. A contract change: `Capabilities.stop_subagents`, `Backend.stop_subagents`.
  - **Codex's sub-agents are found among the daemon's loaded threads** (#858, PR #861; SPEC
    §7.7), since `thread/list` lists none on Codex 0.157.1: so a `!stop` finds one left running
    across a bridge restart. The bridge logs each look at the loaded threads as
    `subagent_loaded`.
- **Each message's edit count and last edit are kept across restarts** (#757, PR #795; SPEC §4.9,
  §6.2), in `zab.db`, so a bridge that restarts again and again can't refresh a status message
  every few seconds.
- **A repeated summary waits for its gap, and `zab.service` restarts back off** (#806, PR #813;
  SPEC §4.9, §6.2, §13.3). A summary goes at once only the first time for its message; a repeat,
  as at each start of a bridge that crashes delivering it, waits the 60 s gap. The unit restarts
  after 5 s, then longer, up to 120 s, with no start limit; meanwhile `!vm` says "bridge
  starting", and `!restart` starts it at once. A rollout's `zab service install` installs the
  unit; on your own machine, run `zab service install` again.
- **Redaction** (#666, PR #801; #821, PR #828; SPEC §11.6). A key's match runs to the end of its
  token, so two keys glued together are one placeholder; a JWT glued behind a key takes every
  dot-joined segment after it; Claude's error, sign-in and title texts are stripped before they
  are redacted.
- **Bounded memory in regular expressions** (#833, PR #834; #836, PR #857): the patterns for math,
  code spans, the console's sentinel, regions and paths are possessive, with the same matches,
  so a 1 MB input no longer takes up to 147 MB. A test reads every pattern compiled at import
  under `src/zab`, `workshop/admin` and `workshop/adminserver`.

**The admin server and its bots**

- **`!upgrade-server vX.Y.Z sha256=<SHA-256>`, the admin server's in-band upgrade** (#569, PRs
  #889, #893, #894, #898, #901, #902; ADMIN §1.6, §2.2; BOOTSTRAP §13.4): requested and approved
  by one deployer on the global admin bot, as a server plan holding every workshop's lock, from
  the release after this one. A compromised bot can have only a correctly signed release of the
  `release` pattern installed, newer than the running one: the in-band upgrade only goes
  forward, and a rollback is `deploy.sh --downgrade` from a deployer's machine (`downgrade` is
  refused, with that pointer). The `sha256=` check makes a deployer's request install only the
  diff they reviewed, but it is no secret (a refused plan shows it), so it binds an honest
  request, not a compromised bot:
  - **the plan helper** (`zab.admin.upgradehelper`, root, sandboxed: it reads only the deploy
    key of `secrets/`, and no IMDS) fetches exactly the release's tag and the running one from
    `release_repo` with the server's own read-only deploy key and GitHub's pinned host keys,
    makes `deploy.sh`'s checks again, checks the running tag against `deploys.jsonl`, and has the
    running release's `deploy.sh --review` the release since it. The plan is refused unless the
    review's SHA-256 is the deployer's `sha256=`. It shows both tags (object, commit, signer and
    key fingerprint), the SHA-256, the privileged diff, whether `render_policy.py` changed, and
    the release's CHANGELOG section. A tag the server has taken is never moved to another commit.
  - **the apply helper** holds the request to the plan helper's record, checks again on the
    server's own copies of the tags, and runs the release's own `deploy.sh` with a bundle of its
    tag: `--build-wheel`, which builds the release's fleet wheel as `zab-build` and places it for
    `!rollout`, then `--server … --in-band P-n`, which installs it as an SSH deploy does and
    records the plan in `deploys.jsonl`. The result shows the fleet wheel's SHA-256. The bots
    post the result and then restart themselves into the new release.
  - **`admin.toml`'s `release_repo`** (`git@github.com:OWNER/REPO.git`), optional: unset, the
    in-band upgrade is off.
  - **End-to-end scenario A10b** runs it on the admin rig, with A10's refusals in-band.
- **`new_workshop.py` puts its request in place without ever giving it two names**
  (`renameat2(RENAME_NOREPLACE)`, #569, PR #898), as `upgrade_server.py` does.
- **The automated month-end** (#570: PRs #817, #841, #829, #835, #839; ADMIN §11.5, ORGANIZERS
  §5.7). A kept VM ends at its date by itself, once a schedule is approved.
  - **`!keep-until DATE|none SEL`** (PR #817) sets a row's end in the roster's new `keep_until`
    column: `2026-11-30`, which ends at 15:00 UTC, `2026-11-30T18:00` in UTC, or `none`. `!roster
    import` takes the column, and `!roster show` lists it as `kept until`. Its plan says how its
    rows end, from the schedule as the plan finds it (#868, PR #871; ORGANIZERS §5.2, §5.7): with
    no row scheduled, that nothing ends on its own; the rows the schedule holds at that end
    already, with their warnings; a row in the month-end run applying, as ending now; a row in a
    run cut short, which a later look resumes unless this plan applies first and drops it out
    (`!month-end cancel` stops either); and the rows the schedule won't end then, which only a new
    `!month-end schedule` or a `!retire` ends.
  - **`!month-end schedule`** (PR #841), destructive, with the phrase `month-end <n>`, covers
    every row in service with a `keep_until`; `!month-end show` lists the schedule, and
    `!month-end cancel SEL` takes rows out at once, from an approver of the workshop.
  - **The warnings** (PR #829): the workshop's bot sends each participant a DM 7 days before the
    end, or at once when the schedule is approved later, and a reminder a day before, unless the
    notice went within 36 hours of the end; each a new message, never sent twice. A row with no
    Zulip ID, or whose DM Zulip refused, still ends: the schedule's topic names it, for the
    organizers to tell. A row whose `keep_until`, Zulip ID or bots changed since the approval
    drops out.
  - **The runs** (PR #835): at its end, each row runs `!retire`'s steps, as a plan of the bot's
    own in the schedule's topic, each teardown at least 5 minutes after its row's protection
    came off. A run that a restart interrupts resumes at the bot's next look, each row at its
    first unfinished step. A notice cut short by the bot's downtime names an end a day after
    the notice, and the row runs then (#840, PR #852).
  - **End-to-end scenario A17** (PRs #839, #863) runs it on the admin rig.
- **A month-end warning that Zulip refused stops its row** (#883, PRs #908, #912; ADMIN §11.5,
  ORGANIZERS §5.7). A notice or a reminder refused at every try (3, a minute apart) no longer
  counts as given: its row stops before any step, its VM in service, and the schedule's topic
  says so, with `!retire <row>`, or `!keep-until`, then `!month-end schedule`, which tries it
  again (a reminder only until the end: still refused then, it keeps its row stopped). A row
  with no Zulip ID still ends at its date without a warning: the schedule's plan now names
  every such row in its first note, which says first that approving it ends them.
- **Each step of a `!retire` is on the audit log as it ends** (#837, PR #849; #855, PR #860;
  ADMIN §2.4, §2.8). A `retire_step` record per row and step, from which the ledger marks the row
  uninstalled, synced or retired in place, so that a hard stop loses none of it; a month-end run
  writes them too. After a restart, a cut `!retire` says what it did to each row and what to
  send.
- **A stopped `!retire` row's advice keeps the plan's words** (#820, PR #827; ORGANIZERS §5.3,
  §5.6): `export` until its archive is in, `unsynced` when the plan had it, and the way back into
  service first when its participant must change what the export refused.
- **Roster plans apply one at a time on the server** (#646, PR #793; ADMIN §2.4), so that two
  workshops approving at once can't both take one free slug: the second waits, then goes stale.
- **`!status` keeps room for the lines under its table** (#767, PR #787): an answer at Zulip's
  limit keeps its footer.
- **`admin.toml`: one organization per files host** (#771, PR #789; ADMIN §2.1). Workshops that
  share a `refserve_url` must share their `github_org`, compared ignoring case.
- **The server's uv is 0.13.7** (#816, PR #853; DECISIONS *Pins*). uv 0.11.24's built-in roots
  hold emSign ECC Root CA - G3. The server's uv installs only hash-checked wheels, so what it
  installed was never at stake; VERIFY V9 holds with 0.13.7. `deploy.sh` refuses a binary whose
  version isn't the pin, and `--wheel` notes a laptop uv other than 0.13.7.

- **The export check matches hard links by their exact name** (#819, PR #887; ORGANIZERS §5.6).
  A tree with names that differ only in case (`a.md`, `A.md`), one of them with a second name (a
  hard link), was refused when the export listed the second name first and `a.md` before
  `A.md`: `a.md` was then the hard link, and `A.md` lay at its name regardless of case. The
  export lists the VM's directory order, so a retry was refused again (#802). Every order now
  passes, re-packed as before. A member at or under an earlier symbolic link's name in another
  case is still refused.
- **The export check refuses a name or link target that tar readers read two ways** (#888, PR
  #892). A crafted archive can give a member's name or a link's target twice, as a GNU long name
  or long link record and a PAX attribute, or in two headers of one kind, which GNU tar and
  Python's `tarfile` choose between differently; or a header's `prefix` field, which `tarfile`
  joins to the name and GNU tar, without POSIX's magic, doesn't. Such a member is now refused,
  with its name. GNU tar, `tarfile` and libarchive (bsdtar) write none, except GNU tar's
  incremental archives (`--listed-incremental`), whose names `tarfile` misreads: those are
  refused too. `export.sh` doesn't make them. If a Python update ever hides the headers from the
  check, every archive is refused, and `deploy.sh`'s self-check of the archive check fails.

**Trust stores** (#671, PR #815; #816, PRs #853, #856; SPEC §12.1)

- **certifi 2026.10.23 wherever zab pins it:** `uv.lock`, the image's `venv-requirements.txt` and
  `certbot-requirements.txt`, lean-lsp-mcp's environment, and a floor in
  `workshop/waker/requirements.txt`. An offline test requires every requirements, constraints,
  lock, `.in` and `pyproject.toml` file that names certifi to pin that release, or bound it from
  below there.
- **The image's trust stores are checked** (`workshop/ami/README.md`, *Smoke test*):
  `files/bin/trust_stores.py` fails on a bundle that holds a distrusted root. `build.sh` runs it
  on `/etc/ssl/certs` after the system packages, and its smoke test on that and on the certifi of
  every Python environment it creates, on a uv-managed CPython's, if one is installed, and, with
  Sage, on the bundles that conda's certifi and pip's vendored certifi return in each of
  miniforge's environments. With Sage, miniforge's environments get `ca-certificates` and
  certifi 2026.10.23 or newer, and pip 26.2.2 or newer. The smoke test fails a uv older than
  0.13.4.
- **`workshop/admin/trust_fix.py`, new** (PR #856; `workshop/admin/README.md` §10): a VM's
  trust stores brought to a new image's, over SSH with the host key pinned, a dry run unless
  `--apply`. It changes only what is older: `ca-certificates`, uv (to `deploy.sh`'s pin),
  certifi in `~/venv`, `/opt/certbot` and each uv tool's environment, by `uv.lock`'s hashes,
  conda's two environments with Sage (where conda also updates openssl, and its solver may move
  other packages of `sage`), then the image's check. A step that would download through
  a store still holding the root is blocked. Software an agent installed that holds the root is
  reported as left. One run at a time on a VM, and each `--apply` holds off its idle stop.
- **The trust fix repairs the apt source first** (#947, PR #957; `workshop/admin/README.md`
  §10). Where its `--apply` will run `apt-get update` (`ca-certificates` older than the fix's
  floor), it first runs the release's `apt_repair.py` as a rollout does, sent inside its remote
  half, from a directory of root's own: on a VM of the Oct 19 image, the expired Macaulay2
  source would otherwise fail the update, and the rollout that removes it comes after the trust
  fix. Its line comes under the table, but for `apt: nothing to repair`; a failed repair asks for
  a rerun. Whatever it says, the update runs, and one that still fails stops that step as before.
  The look under the table covers the repair's processes too.
- **Claude Code's own CA list on the system store** (#882, PR #885; `workshop/ami/README.md`,
  `workshop/admin/README.md` §10). The image's managed settings, written by `build.sh` and the
  first boot, set `CLAUDE_CODE_CERT_STORE=system`, every other key of the file kept. The
  requests that take Claude Code's own CA list then trust the system store alone, not also
  Claude Code's bundled roots. On 2.1.283 those requests were seen to include the messages API
  and its settings fetches. A request without that list, such as a plain `fetch`, still gets
  the bundled roots (*Known limits*). For VMs already running, `trust_fix.py` has a fifth step,
  `claude store`, which merges the key into the file where it isn't `system`, and leaves a file
  that isn't a JSON object as it is; where there is no file, `--apply` writes one holding that
  key alone (`absent -> system`), and the run prints the command that puts the image's file,
  with its other keys, in its place. The smoke tests and `trust_fix.py`'s
  check require the key, and a certificate that `agent` reads from the system store: with the
  store empty or unreadable, Claude Code falls back to its bundled roots.
- **A guard on CA variables** (#882, PR #885). Both smoke tests fail, naming the variable, when
  `SSL_CERT_FILE` or `CODEX_CA_CERTIFICATE` is set for `agent`: in its login shell, its user
  manager's environment, which `zab.service` inherits, or `zab.service`'s own `Environment=`
  (`files/bin/ca_env.py`). Either variable adds roots to the system store's, and Codex 0.157.1
  then also trusts reqwest's built-in roots, emSign ECC Root CA - G3 among them (#859).

**Deploying**

- **`deploy.sh --expect-diff-sha256 SHA` and `--with-wheel`** (#772, PR #798; ADMIN §1.6,
  `workshop/adminserver/README.md`): a deploy refuses, before its prompt, a diff whose SHA-256
  isn't the one you reviewed, and doesn't page it again; `--with-wheel` builds the fleet wheel
  before the prompt and places it after the deploy, under one confirmation. Every option that
  takes a value refuses an empty one.
- **`--base-signers FILE`, after a deployer key is replaced** (#587, PR #810, PR #823; BOOTSTRAP
  §17): the base may be signed by a retired key, never the release deployed, and must then be
  the release the server runs, at the commit its deploy record names.
- **The deployer's machine needs `python3` 3.12 or newer** (#781, PR #797; BOOTSTRAP §1), the
  package's own floor.
- **`deploy.sh --build-wheel`** (#569, PR #901; DECISIONS D26, amended): the server builds a
  release's fleet wheel itself, for `!upgrade-server`'s apply, as `zab-build`, which holds no key,
  after the IMDS rule's check, with the hash-pinned backend of a deployer's `--wheel`. Root
  places only its own copy, checked to be a wheel of the release: its name, version and tag, and
  the zip's integrity. A laptop's `--wheel` still places one built elsewhere.
- **`workshop/adminserver/upgrade.py`: a release reviewed, signed, verified and pushed** (#772,
  PRs #884, #886, #907, #914, #929; the adminserver README). It reaches no server. The deploy
  upgrades the server from the copy it makes, `~/zab-vN`. `!upgrade-server`, from the next
  release on, never reads that copy: it fetches the release's tags from `release_repo`, so the
  tag `upgrade.py` pushes to origin serves it where origin is `release_repo`. Run on the deployer's machine, from the signed copy
  of the release the server runs, with `python3 -I`: `--setup` once per machine, then
  `upgrade.py vN --commit C --review-sha256 SHA`. It refuses any other copy, with the lines that
  make the right one; fetches origin's tags beside the clone's own, never over them; reviews C
  with that copy's `deploy.sh` and compares the review's SHA-256 with the release message's, on
  every run (paged until vN is signed); signs vN through an agent that holds the dedicated key
  alone, once you type the version; checks the tag itself, never git's exit status (git 2.43
  exits 0 after a failed signature); pushes it with your terminal's own agent, unless origin has
  another tag of C, such as the public repository's, whose signer it names (BOOTSTRAP §9): one
  with no fingerprint to check, with no SSH signature or one that doesn't check, it calls an
  anomaly to investigate in a repository you tag yourself (#895, #910); and makes `~/zab-vN`. A
  vN of another commit, origin's or the clone's, stops it before the review. Each phase checks
  first whether it is done, so a rerun goes on. It uses its own agent once that accepts
  connections, and its fetch starts no git maintenance, which from git 2.54 on prunes worktrees
  in the background and could make its `git worktree add` of `~/zab-vN` fail (#920).
  It signs and pushes the tag in the repository the release was made in; with `release_repo` the
  public repository, the tag the server fetches is the one the release's publication signs, which
  `upgrade.py` doesn't make yet (#960).
- **Releases from the public repository** (#958, PR #961; BOOTSTRAP §13.4, ADMIN §1.6).
  `release_repo` may be the public repository, `MathAgents/zulip-agent-bridge`, for its owner's
  server, with a read-only deploy key there; nothing is pushed for the server. BOOTSTRAP §13.4's
  *Onto the public repository's commits* deploys a published release by hand on a server whose
  running release came from another repository's commit: the review where the running release's
  tag is, a deploy clone of the public repository with that tag and the reviewed commit fetched
  in, a check that the published commit names the reviewed one as its source and is its tree
  with files removed and none the server installs or builds from, then
  `deploy.sh --expect-diff-sha256 <the review's SHA-256> --with-wheel`. The reviewed diff covers
  the release paths alone, never commits, so the same SHA-256 holds across the two histories;
  the check covers the fleet wheel's other inputs. `deploy.sh`, the upgrade helper and the loader
  are unchanged, and `release_repo` unset still turns the in-band upgrade off.
- **`ssh-rule.sh --group-name NAME`** (#772, PR #880; BOOTSTRAP §3.1) finds the admin server's
  group by another name than `zab-admin`, still by its `zab-admin` tag key. After an interrupt
  or a dropped connection, `deploy.sh`'s note on the fleet wheel says to let a deploy that may
  still be running end, and gives the `--wheel` command; the adminserver README lists the four
  outcomes.

**Workshop scripts**

- **These scripts no longer default to the first deployment** (#808, PR #831): `launch.py`,
  `teardown.py`, `protect.py`, `power.py`, `grow_disk.py`, `spare_idle_stop.py`, `adopt_spare.py`,
  `import_key_pair.py`, `status.py`, `roster_import.py` and `roster_sync.py` require `--tag`, and
  all but `status.py` `--region` too; `build_ami.py` requires `--tag`, `--region` and, to build,
  `--name`. A roster row with a VM and an empty `region` cell still reads as `us-west-2`.
  `push_waker.py` keeps its defaults, `nt-agents-2026` and `us-west-2`, which the admin bot
  overrides with `--workshop` and `--region`.
- **A rollout repairs `apt-get update` on VMs from the Oct 19 image** (#947, PRs #949, #956;
  `workshop/admin/README.md` §2.2). That image left `/etc/apt/sources.list.d/macaulay2.sources`
  on Launchpad's snapshot of the Macaulay2 PPA, which Launchpad no longer serves, so every
  `apt-get update` there exits 100 and an `apt-get update && apt-get install …` fails with it
  (#945). Each release rollout now runs `apt_repair.py` as root. It reads that file as apt
  does, and where its sources are the expired snapshot and nothing else, it holds the installed
  macaulay2, macaulay2-common, gfan, msolve and libmsolve3 (`apt-mark hold`), then removes the
  file; their packages stay as they are. A hold that fails, or a `dpkg-query` that can't read
  dpkg's database, keeps the file for the next rollout and is a failure in the row. A file
  naming the snapshot beside other sources, a link or another kind of file is left alone and
  noted (`apt: kept …`); any other file, or none, is left quietly. The trust fix runs the same
  repair before its own `apt-get update` (PR #957, under *Trust stores*). A new image carries no
  such source (*Building an image*, below).
- **Building an image: Macaulay2's packages and PARI's data from pinned releases** (#945, PRs
  #946, #950, #951, #952; `workshop/ami/README.md`, *Macaulay2's packages* and *PARI's data
  packages*). Launchpad stops serving a snapshot of the Macaulay2 PPA about 16 days after the PPA
  publishes again (DECISIONS, verification log, 2026-11-07), so `build.sh`'s pin of Oct 18 failed
  every build from Nov 7; PR #946 re-pinned it to `20261106T000000Z` in the interim. A new image
  installs macaulay2 and macaulay2-common 1.26.06+ds-5 (packaging changes only, against the
  Oct 18 image's -2), and gfan and msolve as before, from the GitHub release
  `deps/macaulay2-1.26.06+ds-5`, every file checked by its SHA-256
  (`workshop/ami/files/apt/macaulay2-debs.sha256`); it holds them, keeps their source packages
  in `/usr/src/macaulay2-ppa`, and keeps no apt source for the PPA (PR #950).
  `workshop/ami/m2_debs.py capture` takes a new set from Launchpad into such a release (PR #951).
  PARI's five data packages, which PARI's site keeps under no versioned name, come from the
  release `deps/pari-data-20261107`, with PARI's signatures, and `build.sh` downloads from PARI's
  site only one that is missing or doesn't match its pin (PR #952). `build_ami.py` fetches both
  with the deployer's `gh` before anything is created in AWS, from the repository its checkout's
  `origin` names (`--deps-release`, `--deps-dir`, `--pari-data-release`, `--pari-data-dir`): check
  `gh auth status` first. The public repository holds no such release yet, so a build from a
  public checkout stops at that fetch, before anything is made in AWS: name a repository that
  holds them, `--deps-release OWNER/REPO@deps/macaulay2-1.26.06+ds-5` and
  `--pari-data-release OWNER/REPO@deps/pari-data-20261107`, or pass their files with
  `--deps-dir` and `--pari-data-dir` (`workshop/ami/README.md`).
- **Building an image: PARI's fallback and the host key** (#945, PR #948; #953, PR #954).
  `build.sh` fetches a PARI/GP release that a newer one has replaced from `pub/pari/OLD/<major.minor>/`, where PARI keeps it; the
  fallback looked in `unix/OLD/`, which doesn't exist, so a build would have failed once the
  pinned release left `unix/`. `build_ami.py` scans a fresh instance's host key every 5 seconds
  for up to 2 minutes, since its port 22 can accept before its first boot serves the key, and
  says how long it tried when it gives up.
- `provision_repos.py` reports an invitation's transport failure in its row, and a launch's
  first boot no longer adds a second `/swapfile` line to `/etc/fstab` where the first is separated
  by a tab (#799, PR #812).

**Docs**

- A bot's owner can reactivate it, and ADMIN §3.3 gives a retired row's way back (#762, PR #790;
  #799, PR #812). ORGANIZERS §5.6 explains the archive check's refusal of case twins (#802, PR
  #809). The files host's update keeps its AWS profile apart from the share probe's directory,
  and ORGANIZERS §3.4 says what a failed invitation does (#768, PR #794). Six of the docs audit's
  last findings (#691, PR #807), and the rest of it (#808, PR #831): README says which part of
  each release's notes concerns your own machines.
- `build-constraints.txt`'s regeneration command names Python 3.12 (#799, PR #848); no pin
  moves.
- BOOTSTRAP §13.4: the in-band upgrade's deploy key, `release_repo`, each release's commands,
  and when `deploy.sh` is still the way (#569, PR #902).
- SPEC and ADMIN describe v0.2.19; ADMIN lists the month-end and `!upgrade-server` as built.

**Tests and CI**

- End-to-end scenario 17, sub-agents across a turn's end (#660, PR #838), with parts for `!stop`
  with no turn (PR #851) and after a bridge restart (PR #861).
- The end-to-end runners wait for steady SSH before deploying, and rerun a connection that fails
  (#690, PR #800; #804, PR #826).
- The IMDS rule's nftables tests run on CI (#785, PR #796); the live AT-8 tests skip, their
  channel archived (#672, PR #792); flaky waits made robust (#782, PR #791; #745, PR #788; #805,
  PR #811; #843, PR #844); CI's full run gets 25 minutes on `main` and 20 on a pull request
  (#845, PR #846).
- The end-to-end harness names a Codex turn that OpenAI refused with a 429, and runs that part
  once more (#842, PR #866); the admin tests read the audit log under its lock (#874, PR #875).
- CI bounds and retries its apt, pip and uv steps (#900, PR #903); test children and fakes wait
  for a signal in 10 ms sleeps, never one of 30 s (#890, PR #905; #910, PR #913); the
  end-to-end waits tell a rig-side Zulip failure from the bridge's (#877, PR #906); probes of
  background jobs as transient user units (#655, PRs #925, #931, #937).
- The admin end-to-end suite models releases from the public repository (#958, PR #961): A10
  reviews its good rc in the rig's repository and deploys its exported commit from a deploy
  clone, and A10b's tags are exported commits with signed tags; offline, the hand deploy across
  the two histories, the bundle the server gets (the public history alone) and BOOTSTRAP's steps
  themselves are tested.

**Known limits**

- **The agent CLIs' trust stores** (#816, #859; DECISIONS, verification log, 2026-11-06).
  Claude Code 2.1.283, the image's pin, trusts its bundled roots, among them emSign ECC
  Root CA - G3, as well as the system store, for the API connections checked; 2.1.304 is its
  first release without G3. Codex 0.157.1, as the image configures it, trusts the system store
  alone on the API connections checked, though G3 is among its built-in roots, which
  `SSL_CERT_FILE` or `CODEX_CA_CERTIFICATE` would bring in; and, from its source, not run, its MCP
  HTTP client retries with them after a TLS protocol-version alert, which isn't authenticated.
  0.164.0 is its first release without G3. Since #882, where the managed settings set
  `CLAUDE_CODE_CERT_STORE=system` (an image built since, or after `trust_fix.py --apply`) and
  the system store isn't empty, Claude Code's requests that take its own CA list trust the
  system store alone; a request without that list, such as a plain `fetch`, still gets the
  bundled roots.
- **`trust_fix.py` reaches only roster rows:** a spare launched from an older image gets the fix
  after its `!adopt-spare`, whose own rollout still went through the spare's old uv.
- **Lazy reopen** (#632): a terminal's Codex turn on a thread the bridge hasn't loaded since its
  start reaches no topic, and a topic whose thread a terminal loaded first is refused until that
  terminal quits (`!status` says what to do).
- **The identity record within one run** (#830): if the bridge is away for longer than the
  app-server's unload delay, a minute, and another client loads the thread meanwhile, the record
  still vouches for it. Codex 0.157.1 reports neither a load's time nor its client.
- **Codex sub-agents** (#660): an interrupted sub-agent's commands keep running, and the answer
  of a sub-agent that ends after its parent's turn isn't posted (#655). The agents' instructions
  still say that `!stop` doesn't end a Codex sub-agent (#668).
- **The month-end:** the schedule makes no archive (`!retire <row> export` does), and it lives in
  the server's `state.db`, which the backups leave out: a rebuilt server has no schedule, and the
  organizers approve one again.

**Corrections to earlier notes**

- **0.2.10, retired rows** (#762: #790, #799). The notes said a retired row's bots stay
  deactivated "since only a Zulip organization administrator can reactivate them". A bot's owner
  can too, and the bots a `!retire` deactivates are the provisioner's, whose key is on the admin
  server: a deployer reactivates them there, after clearing the row's `host` and `ip` and before
  its `!launch` or `!adopt-spare` (ADMIN §3.3). No command of the admin bot reactivates a bot. A
  reactivated bot keeps its key, so every copy of that key works again, the server's backups'
  included; `zulip_provision.py --no-rebind --regenerate-keys --only <slug>` replaces it when
  that isn't wanted (ADMIN §3.3).

## 0.2.18 — 2026-11-04

0.2.18 is the first release published in the public repository, `MathAgents/zulip-agent-bridge`.
What is new since 0.2.17: the GitHub organization as a setting of the files host and the bridge
(#693), Python 3.12 or newer (#774), the CHANGELOG as release notes (#773), signed release tags
with the maintainer's key in SECURITY.md (#776), the last of the docs audit's findings (#691), and
end-to-end scenario 9's share-link checks (#775). For a deployment under `MathAgents`, what the VMs
run changes only at a VM's next `!rollout`: the agents' instructions (their math notation, and the
file server named without its host), and `config.toml`'s `refserve_owner`, written at the
bridge's own default.

**Upgrade notes.** Python 3.12 or newer is required. The files host's service needs its
organization before it restarts on this release: `ZAB_FILES_ORG=<org>` in
`/etc/zab-files/zab-files.env`, owned by root, mode 0644, which `workshop/files/README.md`'s
*Updating a running server* writes in its step 3. Without it, the service exits with status 78 and
stays down. Before the first `!rollout` or `!adopt-spare` of this release, check that each
workshop's `github_org` in `admin.toml` is the organization its file server serves: both now write
it to the VM's `config.toml`.

**Changes since 0.2.17**

- **The GitHub organization is a setting of the files host and the bridge** (#693;
  `workshop/files/README.md`, SPEC §10.7, ADMIN §2.1). The files host fetched snapshots only from
  `MathAgents`, and the rollout never wrote the bridge's `[render] refserve_owner`, so a
  deployment under another organization had its file links silently off.
  - **The files host** has no built-in organization: `--org`, or `ZAB_FILES_ORG` from
    `/etc/zab-files/zab-files.env`, names it, and every GitHub access uses it: the snapshots'
    fetches, the share store, the hourly gone and share checks, and the recheck. Without a valid
    name, `server.py` logs what to set and exits with status 78 (`EX_CONFIG`), which its unit
    doesn't restart; at its start it logs `serving the repositories of <org>`. With no
    organization, nothing is ever taken for gone or revoked. `selftest.py` requires `--org` and
    `--url`; `--url` no longer defaults to one host.
  - **The rollout:** `push_config.py --refserve-owner ORG` writes `[render] refserve_owner` beside
    `refserve_url`, `MathAgents` by default, and the admin bot passes each workshop's `github_org`
    on `!rollout` and `!adopt-spare`.
  - **The agents' instructions** name "the workshop's file server" rather than a host.
- **Python 3.12 or newer** (#774). `requires-python` is `>=3.12`, and CI tests 3.12, 3.13 and
  3.14. The workshop image and the admin server are Ubuntu 24.04, with its Python 3.12. On 3.11,
  `asyncio.wait_for` can lose a cancellation, so that a `zab run` stopped at the wrong moment
  could hang at its shutdown (#498): that case is gone with 3.11. `uv.lock` drops the 3.11
  wheels of two dependencies, at the same versions.
- **The CHANGELOG is release notes** (#773): each release's section holds its summary, the
  upgrade notes any deployer needs, and its changes.
- **Signed release tags** (#776; SECURITY.md, *Verifying a release*). The public repository's
  release tags are signed with the maintainer's SSH key. SECURITY.md publishes its public half,
  its fingerprint and the `allowed_signers` line, with the commands that verify a tag and what
  each outcome prints. `git tag -v` checks the signature, not the tag's name: check that its
  `tag` line is the version you asked for. BOOTSTRAP §9 points a deployer who takes the
  maintainer's review as their own to it; one who reviews a release themselves still signs their
  own tag.
- **Docs** (#691). README opens with a diagram of personal use, zab and its bots on your own
  machine, before the workshop's. NEW-WORKSHOP loses its last references to one deployment and
  links every reference. CONTRIBUTING says that the offline suite needs a git clone and that the
  maintainer runs the live and end-to-end suites. SECURITY links the private reporting form, says
  what to do without one, and which uses of a files host's links are vulnerabilities; a browser
  that has opened a share link keeps its copy after the link is revoked.
  - **The agents' math notation** (`workshop/ami/files/agent-instructions.md`, installed by each
    rollout): in replies, math as Zulip writes it, `$$...$$` inline and a `math` code block for
    display; in Markdown files, as GitHub does, `$...$` inline and `$$...$$` for display.
- **End-to-end scenario 9** (#775) reads a share link wrapped in Markdown emphasis, and confirms
  the share with `zab share --list` on the VM, not the turn's status line, which shortens
  commands.

## 0.2.17 — 2026-11-03

0.2.17 includes 0.2.16, a hotfix release with #754 alone (its section, below). What is new
since 0.2.16: the waker's start-up memory (#717), the spares' own idle stop (#715, #607),
addressing (a typed `@claude` is a mention, and a message without one goes to both agents: #713),
one fork topic for both agents (#712), "Start here", a workshop's own gift step and commands as
people type them (`!Login`, `` `!login` ``: #709), `mention_after_s` (#711), participants as their
repository's admins and the file server honoring a deletion (#714), share links (#710), `!btw`
(#716), `!rollout`'s defaults (#704), nothing to change as a success (#708), the SSH rule in one
command (#674), moving bots with an administrator's key (#758), and a status table of 50 lines
(#764).

**Upgrade notes.** The role's policy is unchanged, and no server unit is new. Deploy the release to
the admin server before adding any of `admin.toml`'s new optional keys (`gifts_step`,
`mention_after_s`, `spare_idle_stop`; ADMIN §2.1): an older loader refuses the file, and its bots
exit at their start. Update the files host (`workshop/files/README.md`, *Updating a running
server*) before rolling the release out to the VMs, since `zab share` needs it. After the
rollout, one `!provision repos invites-only all` per workshop makes each participant their
repository's admin (#714), and one `!start spares` gives the spares a workshop already has their
own idle stop (#715).

**Changes since 0.2.16**

- **The admin bot's status table shows 50 lines, not 40** (#764; ADMIN §2.4, ORGANIZERS §2.2,
  §3.6). `!status all` on a workshop of 41 VMs showed 39 rows and attached the other 2, though
  a workshop is planned at about 40 participants. The table, its header included, is now cut at
  50 lines, in `!status`'s answer and in a plan's status read: a workshop of up to 49 VMs shows
  whole, at about 80 characters a row well inside Zulip's 10,000. A plan's dry run, a script's
  output and an apply's result are still cut at 40 lines.
- **Operators: moving bots needs an organization administrator's key** (#758; ADMIN §8.4, V15).
  Since Nov 2, Zulip Cloud refuses to change a bot's owner for any key but an organization
  administrator's or organization owner's, the bot's own owner's included: `400
  UNAUTHORIZED_PRINCIPAL`, "Must be an organization administrator". Zulip's open-source code
  still lets the bot's owner do it, so the change is Zulip Cloud's own. `move_bots.py` now reads
  the caller's role: a key that isn't an administrator's is said so, and its dry run fails when a
  bot would move; with `--apply`, a row Zulip refuses fails with that cause, not a bare Zulip
  error. Run it with an administrator's or organization owner's key. The admin end-to-end
  scenario A18 no longer moves bots, since the rig's test users are a Moderator and a Member: it
  starts from the bots the move would leave, the test provisioner's, runs the move's dry run, and
  names the `--apply` as needing an administrator's key.
- **Status messages, every edit bounded** (#754): shipped in 0.2.16, below, and not new here.
  0.2.17's code for it is 0.2.16's, line for line.
- **The file server honors a deletion or a transfer** (#714). A request for a snapshot it has
  already mirrored never asks GitHub, so every snapshot fetched before a participant deleted or
  transferred their repository stayed readable for good. Now, every hour, the server looks up
  each mirrored repository with the GitHub API, and removes the mirror of one that is gone,
  within about 65 minutes; its links then answer 404. Gone is a 404, or a redirect to another
  owner, confirmed by a second 404 after the token has read another private repository of the
  organization, so a failure, an outage or an expired or revoked token never removes one, and a
  check never removes more than 3 mirrors, or a quarter of them, as gone at once: it names those
  it keeps, for a deployer to check. A repository re-created
  under the same name has another GitHub ID, so the old one's mirror goes too. A deployer removes
  one mirror at once with `server.py purge <repo>` (`workshop/files/README.md`, *Operations and
  bounds*). A copy a browser has already opened can't be recalled.
  - **Upgrade:** the files host's update (`workshop/files/README.md`, *Updating a running
    server*), with no unit, Caddyfile or token change: the token already reads the repositories'
    metadata. The first check fetches every mirror once.
- **A typed `@claude` or `@codex` is a mention** (#713). In a channel, `@claude` or `@codex` typed
  out, without picking the bot from Zulip's typeahead, is a mention of the sender's own Claude or
  Codex, as `@**Claude · alice**` is: in any case, anywhere in the message, and wherever a mention
  counts, commands and asides included (`@claude !stop`, `@codex // a note`). Not in code, escaped
  (`\@claude`), part of a word (`@claudette`, `x@claude.ai`), in a link
  (`https://medium.com/@claude/…`) or in a quote (SPEC §4.5, "Mention detection").
  - **Quotes.** A mention in a quote, a ```` ```quote ```` block or a `>` line, no longer counts,
    a completed one included. Zulip silences those, so the quoted bot never had Zulip's mention;
    the other bot used to count it, and ignored the message as one for the quoted bot, which
    ignored it too. A quoted mention of the bot stays in the prompt as it was.
  - **DMs.** A DM command may follow a typed mention of the bot's own kind: `@claude !login` to
    Claude's bot is `!login`, for the bridge, its sign-in and `zab sentinel` (SPEC §4.6).
  - **The waker** gives a typed mention its bot's ⏰, as a completed mention (SPEC §13.4.1). It
    takes a bot's kind from its configured name, `claude-<slug>` or `codex-<slug>`, as
    `push_waker` writes them.
  - **Upgrade.** The bridge reads typed mentions on a VM from its next `!rollout`. The waker reads
    them once the admin server runs this release; until then, it gives a message with a typed
    mention the ⏰s of one without a mention. "Start here" drops "and pick the one with your name"
    for workshops provisioned after this release.
- **A message without a mention goes to both agents** (#713). In a channel where the participant
  is the only person, a message that names no bot, by a completed or a typed mention, goes to each
  of the VM's bots, whatever the topic's earlier messages addressed: name a bot and only that one
  answers, name none and both do (SPEC §4.5 rule 6.2, "A message without a mention"). Before, a
  follow-up went to the bots that the topic last addressed, which nothing showed.
  - **A new session.** An agent without a session in the topic starts one, with the co-addressed
    note and the topic's messages since as context. In a fork's topic too: an unmentioned
    follow-up brings the other agent in, without the parent conversation (SPEC §7.11); an
    unmentioned `!fork` forks both, into one topic (#712, below).
  - **Commands** without a mention go to both, and an agent that a command doesn't concern says
    nothing: `!stop` with no turn running, `!model` or `!effort` with a value its backend
    refuses or, for Codex without its model list, can't check (a named command still accepts
    it), a command for a session it doesn't have where the other agent has one, `!new` where it
    has one. `!help`, `!vm` and `!tools` are answered once, by the first bot by name (Claude).
    `!handover` with both agents in the topic is refused once, asking to name one, since both
    would write `HANDOVER.md` in the topic's one worktree. The others answer from each agent
    (SPEC §4.6, "Commands without a mention"). The help's first line says so; rule 7's refusal
    reads "No session here: write a prompt to start one; !help lists the commands.", and
    `!fork`'s "No session here to fork: write a prompt to start one, then !fork it; …";
    `!detach`'s reply adds "Mention me to start a new session in this topic."
  - **Unchanged:** a mention keeps a message to the bots it names; a channel with other people
    needs a mention for every prompt; asides, DMs, bots and other senders; a Codex not signed in
    stays out, silently; after `!detach`, that bot waits for a mention in the topic. With the
    kill switch (`multi_party = false`), a message without a mention goes to the agents that have
    a session in its topic, and starts none.
  - **The addressee record** (`topic_addressing:<bot>:<stream>:<topic>` in each VM's store) is
    no longer written or read; the rows a VM has stay, unread. So is the search for another
    bot's moved card that a new topic's first message made (#687). A bridge reuses an agent's
    sign-in answer for 5 seconds, so that a catch-up's messages without a mention wait at most
    once for a Codex daemon that is reconnecting, and forgets it when that agent's `!login`
    ends.
  - **The waker** gives a message without a mention a ⏰ from each bot that allows its sender,
    and no longer reads its topic's history (#683's lookup, up to 12 requests per wake, went;
    SPEC §13.4.2).
  - **Docs:** the quickstart ("Type @claude or @codex to talk to one agent; a message without
    either goes to both."), ORGANIZERS §3.8 and the waker's section, SPEC §4.5, §4.6, §7.11,
    §13.4, AT-2 and AT-24.
  - **Upgrade.** The bridge's rule reaches a VM at its next `!rollout`. The waker's reaches the
    fleet with the admin server's upgrade to the release, which changes no unit, config or
    `release-paths` line for it. Until both run it, a message's ⏰s may follow the old rule; the
    wake is the same.
- **One `!fork`, one fork topic** (#712). A `!fork` that both agents get (no mention, where the
  participant is the only person in the channel, or a mention of each) forks both into one topic,
  `<topic> ⑂ 1` or `--name`'s, in one worktree. Before, each agent forked into a topic of its own,
  with its own copy of the files, and with `--name` the second was refused (Harvard, Oct 31).
  - **How.** The first bot to fork makes the topic and the worktree, as before, and records its
    fork of the message in the store (`fork_of_message:<message id>`). The other finds it there and
    forks its own session into that topic and worktree, as a session another bot starts in a fork
    topic does; its card says it works in the other agent's files and branch. The parent topic
    gets one `⑂ forked to` line, and each agent's first answer there is reported to that agent's
    session in the parent topic, as before: two lines when both answer (SPEC §7.11).
  - **When one can't fork** (no completed turn yet, a session in error), it says why in one ⚠️ line
    and the other forks alone, as before. A `!fork` that waited for its session to reopen joins
    the other agent's fork when it runs, if that is still in its topic and its worktree exists, and
    its agent starts from the files as they are then, which may hold the other agent's first turn;
    its `!diff` starts there.
  - **Unchanged:** a mention forks only the agent it names (`@claude !fork …`); a `--name` that
    another `!fork`'s topic, or any other session, holds is refused. Fork topics made before the
    release stay as they are.
  - **Docs:** SPEC §4.6, §7.11 and Appendix B.1; the quickstart's `!fork` row.
  - **Upgrade.** A VM's next `!rollout` of the release; the waker is unchanged.

- **`!btw <question>`: a side question about the conversation** (#716), as Claude Code's `/btw`.
  The agent answers from what the topic's conversation already holds, without tools, in a reply
  marked `💬 btw` that links the question; a running turn goes on, and the question and its
  answer never enter the conversation, peer context or a fork's answer (SPEC §7.13).
  - **Claude** resumes a copy of the session that is never saved, with no tools, no MCP servers
    and no hooks: it sees the running turn's progress too (SPEC §9.10).
  - **Codex** answers on an ephemeral fork of the thread through its last ended turn, in one
    read-only turn with approval `never`, told what a running turn has done so far. The fork
    runs without MCP servers, plugins, apps or web search, which work outside Codex's sandbox,
    and is refused unless Codex reports each MCP server off for it; an approval it asks for is
    declined, and a tool it reaches for stops it (SPEC §8.10).
  - One at a time per session (a second waits, with ⏳), no turn slot, 120 s at most; a refusal,
    a timeout or a failure is one ⚠️ line. A side answer taking a minute or more mentions its
    asker, whatever `mention_after_s` says. Each is answered alone: earlier side questions
    aren't replayed, unlike `/btw`'s.
  - `zab history` leaves side questions and their answers out too, and idle stop counts a side
    question as activity.
  - `!help` and the quickstart's command table list it.
- **The spares stop themselves** (#715): at Harvard's workshop, keeping the spares running cost
  money for nothing. ORGANIZERS §3.7.
  - **Their own idle stop.** A spare gets neither the waker nor a row's idle stop, so until now
    it ran until it was adopted, stopped or torn down. Each spare now gets
    `idle_stop.py --spare`, after admin.toml's new `spare_idle_stop` minutes, per workshop: 10
    without it, 10 to 240 (ADMIN §2.1). The rules are idle stop's own; `--spare` only doesn't
    count a missing zab database, which a spare without a bridge has, as a reason to stay up,
    and writes its verdicts to `/var/log/zab-idle-stop-spare.log`.
  - **When it is installed:** by a new step, `workshop/aws/spare_idle_stop.py`, at the end of
    `!launch … spares=N` and of every `!start` of spares (`!start spares`, `!start zab-spare-N`,
    `!start all spares`), each spare named. It skips a spare that is stopped, adopted or handed
    over, waits up to 5 minutes for the others' SSH, pins each one's host key to the
    fingerprints its console printed (`spares.json`), checks on the VM that it is still the
    spare, and installs the timer. The plans' note says so.
  - **The first boot's volume warm-up** now holds every VM up while it can still end (its
    marker `/var/lib/zab-warm.done` missing, and its unit or cloud-init's last stage running):
    a stop would cut it off for good. The verdict says `the first boot's volume warm-up is
    running`. So a spare launched stops itself after its warm-up and its load have settled.
  - **A hand-over replaces the spare's timer.** `push_config.py`'s idle-stop probe reads a
    spare's timer first (`---ZAB-IDLE-STOP-SPARE`): `--idle-stop-default` replaces it with the
    row's where the row's waker is on, and removes it elsewhere, with a note; `--idle-stop N`
    replaces it, as before; an `--adopt-spare` run with neither removes it.
  - **The spares a workshop already has** were launched without it: `!start spares` once, after
    the upgrade, gives each running or stopped one its idle stop (a stopped one is started), and
    each then stops within about 10 minutes. Until then they run as before.
  - **`spare_idle_stop` in admin.toml** only once the server runs this release: an older
    server's loader refuses a key it doesn't know, and refuses the whole file. Without the key,
    a workshop's spares get 10 minutes.
  - **A `!launch … spares=N` that fails** stops before the spares' step: once the cause is
    fixed, send `!start spares`, which gives them their idle stop. The step runs right after
    `launch.py`, before `scan_hosts.py`, so that a failure of the latter doesn't leave them
    without it.
  - **Launch day** no longer needs `!stop all spares`: `!stop all` (ORGANIZERS, "At a glance").
  - **`!adopt-spare` starts a stopped spare itself**, so no spare needs to be kept running for
    a late joiner. Its dry run says `zab-spare-1: stopped, started for the adoption`, and its
    first step, `adopt_spare.py`, starts the spare and waits until it runs and its port 22
    answers, 300 s at most, before the retag. A late joiner's adoption takes about a minute
    longer. A spare `stopping` or `pending` is still refused, now with the wait it needs
    (`refusing: zab-spare-1 is stopping: request again in a minute or two, once it has stopped
    (this plan then starts it)`); 0.2.16's `is stopped, not running: start it first with
    !start …` is gone. The row's idle stop replaces the spare's in the chain's first
    `push_config.py`, under its hold.
  - **The first morning** starts the rows alone: `!start all`, not `!start all spares`
    (ORGANIZERS §3.8).
  - **Admin scenario A20** (#715): a spare launched stops by itself after its warm-up, within
    90 minutes, `Client.InstanceInitiatedShutdown`; the late joiner's `!adopt-spare` of that
    stopped spare starts and adopts it; the adopted VM's idle-stop unit is the fleet's
    (`--minutes 240`, no `--spare`), and the spare's log shows the warm-up's hold before its
    power-off. Its part "a stopped spare is refused at plan time" is gone, with `zab-test-e`.
  - **The cost line names the spares:** `cost (estimate): … · 5 of 44 running · spares: 3 of 3
    running`. Harvard's `8 of 47 running` was 5 of `!status`'s VMs and 3 spares. The collector's
    snapshot counts them apart (`running`, `vms`; `spares`, `spares_running`), and the console
    agrees: the workshop page's cost line, the index's `Running` column (`5 of 44 · spares 3 of
    3`) and a `Spares` line in the fleet totals.
- **`spares.json` forgets what is gone** (#607, with #611's points). `teardown.py --spares-file`,
  which `!teardown` and `!retire` now pass, drops every entry of the scope and region at an
  address it released, written atomically: a spare torn down is no longer offered by `!start
  spares` or `!adopt-spare`, nor taken for a later spare at its recycled address. `launch.py`
  drops another spare's entry at an address it records for a new one, and keeps an entry's own
  fields for the same instance, the `adopted_by` mark of a hand-over by hand included
  (ORGANIZERS §7.2). Admin scenario A9 checks that its spare's teardown removes it from
  `spares.json`. New tests fail if `teardown.py --roster`'s or `adopt_spare.py`'s stale-ip guard
  goes.
  - **A workshop's existing `spares.json`** may hold torn-down spares from before: those whose
    `ip` no instance has. The first `!launch … spares=N` that records a new spare at such an
    address drops the stale one; any other stays until a deployer removes its key
    (`<ws>/<region>/zab-spare-N`, one whose `instance_id` EC2 no longer has) from the workshop's
    `out/spares.json` on the server, as `zab-admin`, after a backup. Meanwhile `!start spares`
    fails on it (`FAILED: no live instance … has it`), and gives no spare its idle stop: name the
    live spares instead, `!start zab-spare-1 zab-spare-2`.
- **The waker's start-up stays small** (#717). On Nov 1, after a config change, each start of
  the admin server's waker was OOM-killed at its `MemoryMax=512M`, for 90 minutes, with about
  118 opted-in bots after two workshop days. Measured with the new load rig
  (`tests/waker/load_rig.py`, 150 bots of 100 messages of 10,000 characters): 439 MB before, and
  874 MB with the bridge's status messages and their edit histories; 57 to 66 MB after, on
  Python 3.11 to 3.14.
  - **The catch-up fetches only what is new:** nothing when Zulip's registration says a bot's
    mark is its newest message; else from the newest back, 10 messages a request, to the mark or
    to 5 minutes ago, the newest 100 at most. Of each message it keeps only what the waker reads,
    and lets each answer go before the next request.
  - **Never the VM's own bots' messages,** which wake nothing: Zulip returns each with its whole
    edit history, every 10-second version of a status message and its HTML. If Zulip refuses
    the narrow, the waker leaves them out itself.
  - **At most 8 bots at a time** register and catch up, every workshop's together, each turn
    (its registration and catch-up together) for at most 30 s. The journal gives each turn's
    time (`Waker turn of …`) and each workshop's whole start's (`Waker start: N bots caught up
    in …`).
  - **One TLS context per process,** for every Zulip client: httpx built one per client, about
    0.7 MB each, 170 MB for the waker's 118 bots. The bridge and the admin bots share theirs too.
  - **`MemoryMax=512M` stays,** nearly twice the worst start measured: up to about 270 MB, at 340
    bots on Python 3.14, after a long outage in a channel the VMs share, every message of it in
    its last 5 minutes (about 210 MB on the server's Python 3.12). The admin server keeps a 2 GB
    swap file (BOOTSTRAP §3.9).
  - **When nobody's VM wakes:** ORGANIZERS §4.1 and ADMIN §1.8, the waker's journal, Nov 1's
    mitigation and its removal.
  - **A waker that is down is reported:** after 5 minutes, the workshop's bot posts `waker down
    since …` once in its `#<ws>-fleet-alerts`, and `waker back` once it serves again; `!status`
    and the console say so meanwhile. The waker's status file gives each workshop's `ready_at`,
    which moves only once its waker has run a minute, so a waker killed at every start, as on
    Nov 1, shows as down although it rewrites the file at each start (ADMIN §1.8).
  - **A8, on the rig:** the waker stopped until the bots say it is down (in `#zab-dev-alerts` and
    `!status`), then a config change, a busy topic (asides with edit histories) and a DM, and its
    start: it catches up without a failure or an OOM kill, the DM wakes the VM through the
    catch-up, and the bots say it is back.
  - **Upgrade:** a server given Nov 1's mitigation removes its drop-in after the deploy, so that
    the unit's `MemoryMax` holds again (ADMIN §1.8, step 4), and keeps its swap file across
    reboots (BOOTSTRAP §3.9).
- **`!rollout`: the shared Mathlib and idle stop by default** (#704). A rollout that doesn't name
  `lean-shared` or `idle-stop=` applies their defaults. Launch day's rollout is now
  `!rollout v<release> all` (ORGANIZERS §3.5).
  - **The shared Mathlib.** On each VM whose Mathlib isn't shared yet, the rollout does what
    `lean-shared` does, its rollback included. A VM that has it is only probed, with no timing
    build. A VM with no Lean workspace is noted, not failed.
  - **Idle stop at 60 minutes,** last, on each VM whose waker is on and whose timer isn't on
    (ORGANIZERS §4.2: the waker first). The plan names the rows without the waker, with
    `!waker on`, then `!idle-stop 60`. A timer that is on stays as it is. Where the waker is on,
    a timer installed but not enabled is turned on again; where it isn't, a timer that is on is
    a warning in the row's note.
  - **Turned off on purpose.** A VM whose idle stop was turned off by `!idle-stop 0`,
    `idle-stop=0` or `!retire` stays off until an explicit `idle-stop=N` or `!idle-stop N`.
    `--idle-stop 0` records it on the VM, `/etc/zab/idle-stop.off`, and `--idle-stop N` removes
    the record. A VM turned off with 0.2.16 or older has no record, but keeps idle stop's log,
    `/var/log/zab-idle-stop.log`, which the default reads the same way.
  - **Whether a row's waker is on** is read from the waker's config, which `!waker on` and
    `!waker off` write, at plan time and again at apply: a row opted in or out in between makes
    the plan STALE. The VM must also be the instance that the waker names for the row, read
    from cloud-init's `/var/lib/cloud/data/instance-id`. A VM replaced by hand since its
    `!waker on` gets no idle stop, and its note gives `!waker on` first (ORGANIZERS §7.2).
  - **The plan and its dry run show the defaults.** The dry run shows each VM as a new one, with
    every step the defaults may take, and the grammar's usage line names them.
  - **`!adopt-spare`** gives a late joiner's VM the same defaults where the fleet's rollouts
    never named the words, idle stop only after the chain turns the waker on.
  - **`!retire`'s "To put it back in service" line** names `idle-stop=60` where the ledger has
    no minutes, in place of `<minutes>`.
  - **Hand runs:** `push_config.py --lean-shared-default`, `--idle-stop-default MINUTES` and
    `--has-waker SLUG[=INSTANCE]` (`workshop/admin/README.md` §2.4).
  - **Unchanged:** `lean-shared` and `idle-stop=N`, 0 included, keep their meaning, so 0.2.16's
    commands work as before. Until the server runs this release, a rollout has no defaults: add
    `lean-shared idle-stop=60`, as before.
- New admin scenario A23 (#704), the rollout's defaults: on a VM whose idle stop was never set,
  `!waker on`, then a `!rollout` without the words. Its plan passes the defaults, and the VM ends
  with idle stop at 60 and the shared Mathlib. It also checks VERIFY V19: `ubuntu` reads the VM's
  instance ID from `/var/lib/cloud/data/instance-id`, and the probe matches it to the waker's.
- **Nothing to change is a success, not a refusal** (#708). A command whose dry run says that
  everything is already as it asks gets no plan and no approval message. The bot answers with
  one message, marked `☑`: ``☑ **P-76** · `!roster sync`: nothing to change, as its dry run
  says; no plan to approve``, with the dry run under it (ORGANIZERS §2.2).
  - **`!roster sync`** with nothing to change was refused with `❌` before; it now gets `☑`.
  - **`!roster import`** of a CSV that changes no row, such as the same one sent again, posted a
    plan whose apply wrote nothing; it now gets `☑`.
  - **`!provision repos invites-only`** with nothing to change posted a plan whose buttons did
    nothing; it now gets `☑`. That is when it has nothing to invite or withdraw, no acceptance
    or other state to record, and no failure. Its summary line still names the rows whose
    login change is refused, and those not provisioned yet. By hand,
    `provision_repos.py --invites-only --dry-run` then prints `nothing to change: …` and exits 3.
  - **Every other command** posts its plan, as before. A failed dry run and a refusal keep `❌`,
    and so does a check that can't tell: a `!roster sync` whose only news is a row that left,
    whose holdings can't be checked, is refused.
  - **The plan's state is `unchanged`** (`!plan P-76`), and the audit log records `unchanged`,
    never `refused`. The console's audit view shows the new event.
- Admin scenarios: A22's second `!roster sync`, and its sync after `!roster remove`, expect `☑`
  with no plan; A5 adds `!provision repos invites-only all` twice in a row, the second `☑`
  (`test_a5_invites_only_twice`, #708).
- The refusal of a v0.2.4 `admin.toml`, by the loader (`zab.admin.config`) and by
  `render_policy.py`, names CHANGELOG 0.2.5's upgrade note for the migration, where it named ADMIN
  §8.4, whose cutover runbook is gone. Comments and tests that cited that runbook's steps cite
  "ADMIN §8.4 at v0.2.15" or the section that now holds their subject (#720).
- **The SSH rule in one command** (#674, its first part): `workshop/adminserver/ssh-rule.sh`, on
  the deployer's laptop, does what BOOTSTRAP §3.1's refresh block does, and checks the result.
  - **What it checks.** checkip's answer must be one address, with nothing but blanks around
    it. It refuses the home network's address, which `--record-home` keeps in
    `~/.config/zab/homeip` and never prints. It checks that the terminal's agent holds the
    dedicated key alone, and prints the commands that load it when it doesn't. All of that
    comes before any AWS call, and every rule ID before the first revoke.
  - **What it changes.** It revokes every rule for port 22 alone and admits this address's
    `/32`. It then reads the group back, and stops unless 443 and 80 from `0.0.0.0/0` and 22 from
    that `/32` are all there is.
  - **Options.** `--files-host` does the same for the files host; `--close` revokes port 22;
    `--dry-run` changes nothing. There are no built-in values: `--profile`, `--region` and
    `--key` come from flags, or from `~/.config/zab/ssh-rule.conf`, which `--save-defaults`
    writes. With neither, the run stops and says what to run.
  - **It is privileged** (`workshop/adminserver`), so the release's review shows it whole.
  - **Use it** from a release you have signed, once `--save-defaults` and `--record-home` have
    run at home (BOOTSTRAP §3.1). BOOTSTRAP §3.1's block stays, as the fallback.
- **"Start here": the sign-ins first, the repository last, no git terms** (#709, after Harvard's
  workshop). The sandbox's "Start here", which `!provision zulip` posts once, so only
  sandboxes posted after this release get it:
  - **Before you start,** four steps: the workshop's gifts, then signing in to Claude and ChatGPT
    with the accounts that received them; device code sign-in for Codex in ChatGPT's settings,
    under security, named by what it does rather than by today's menu; then one step per agent,
    each as its sign-in goes: Claude's link, then its code **pasted back** into the direct
    message; Codex's link and code, **typed there**, with nothing sent back. Each bot's name is
    the link that opens its direct message. Then: each sign-in waits 10 minutes.
  - **Then** a new topic for the first request, `@claude` or `@codex` for one agent, `!help`,
    and remembering; **your files** last: the repository, the GitHub field of the Zulip profile,
    and the file links, whose host is in code, so that Zulip doesn't link the server's root,
    which answers `Not found`.
  - **Gone:** each topic's branch and merging into main, which an agent does when asked ("use
    what we found in *Line 9*"); the "is not subscribed to this channel" warning, which can't come
    up in the sandbox (the quick-start keeps both); and the repository invitations' timing.
- **A workshop's own gift step** (#709): `gifts_step` in a workshop's entry of `admin.toml`, one
  line of Markdown (ADMIN §2.1), is the first sentence of "Start here"'s step 1 and of the
  sandbox's description; without it, the release's `GIFTS_STEP`, as before. `!provision zulip`
  and `!adopt-spare` pass it to `zulip_provision.py` as `--gifts-step-b64` (a hand run:
  `--gifts-step`), and the workshop's `refserve_url` as `--refserve-url`, whose host "Start
  here" names. **`src/zab/admin/config.py` is on the privileged list (D32):** its diff adds the
  optional key and its rule.
- **The sandbox's description** says the same: the gift step, device code sign-in for Codex, and
  `!login` in a **direct message** to each bot. The next full `!provision zulip` rewrites it in
  every sandbox.
- **The quick-start** (#709): the first paragraph says to ask the agent for one topic's work in
  another, without merging; step 2 names device code sign-in; steps 3 and 4 say **direct
  message** (not in your sandbox), and how each agent's code goes. **ORGANIZERS:** §1.6's
  gift step, §3.8's invitations, and *Press Enter to send* (§1.1) for a new workshop.
- **Admin e2e** (#709): the rig's `zab-admin-e2e` has a `gifts_step`. A9 releases its throwaway
  row's sandbox and reads back "Start here" and the description, word for word against
  `zulip_provision`'s texts, the file host in code and the bot names links to their DMs. Its plan
  carries `--refserve-url` and `--gifts-step-b64`, and shows the step in its dry run. A20's
  `!adopt-spare` carries the same flags.
- **Commands as people type them** (#709; SPEC §4.6), in DMs and topics: the verb in any case
  (`!Login`, from a phone keyboard's capital), and a message that is one code span holding a
  command (`` `!login` ``, which Zulip's paste of the rendered "Start here" gives). What follows
  the verb is kept as typed, and any other message, a prompt that starts with a code span
  included, is unchanged. The code span is one line: a fenced block is never a command.
  - **Which commands:** `!login` and `!login cancel`, the bridge's DM and topic commands, and
    `zab sentinel`'s `!vm` and `!restart`, which the bridge and the sentinel read alike, with the
    router's kind and mention, so that one of them answers (`zab.core.command_text`). A cancel
    is `!login cancel` in any case and spacing, and `!Restart Codex` is `!restart codex`.
  - **In a DM,** a leading mention of the bot is dropped too: a typed one of its kind first
    (#713's `@claude`), then a completed one, so ``@claude `!Login` `` is `!login`. The typed
    mention must come first: `@**Bot** @claude !vm` is no command.
  - **In a topic,** an unmentioned `!Help` is the VM's `!help` (#713's rules), and a multi-party
    topic's context leaves the owner's commands out as the router reads them
    (``@claude `!stop` `` too).
  - **Side questions.** Peer context and `zab history` read a side question (`!btw`, #716) as
    the routers read a command: after the bots' mentions (from `GET users`; `zab history` now
    makes that one read-only request, and falls back to any mention without it) and typed ones,
    so ``` ``!btw …`` ``` and `` ` !btw … ` `` are left out now, and ``@claude `!BTW` what?``
    and `@**Bob** !btw …` (a person's mention) are prompts, kept. Peer context's multi-party
    asides and owner commands read the same address list.
  - **End-to-end scenario 16** (fast): DM `!VM`, `` `!vm` ``, a mention with `!vm` and
    `@claude !VM`, one reply each, the sentinel's; `!Help`, `` `!status` ``, a mention with
    `!STATUS` and ``@Codex `!help` ``, one each; in a session topic, `` `!help` `` and `!Usage`
    unmentioned answered once, and ``@claude `!x` …`` delivered to Claude alone; `!login`
    started and cancelled four ways for each bot, never completed, Codex's reply word for word,
    both agents signed in before and after.
  - **Upgrade.** A VM's next `!rollout` of the release, for the bridge and the sentinel both: the
    rollout's `zab service install` restarts the sentinel on the new code.
- **Codex's `!login` reply** (#709) names the ChatGPT setting as ChatGPT now does (*Enable device
  code sign-in for Codex*, under **Security and login**), and ends "Nothing to send back here:
  I'll answer ✅ when you're done." Claude's still asks for the code pasted back into the DM.
- **The @ rule, per workshop** (#711; Harvard, Oct 31). A participant's bot mentions them when a
  turn fails, needs an approval, or ends after running `mention_after_s` seconds or more (SPEC
  §4.8), so a topic that waits on them after such a turn shows **@** in Zulip's left sidebar.
  - **`mention_after_s`** in a workshop's entry of `admin.toml`, whole seconds from 0 to 86400,
    120 when absent (ADMIN §2.1). At 0, every turn that completes or fails mentions its owner.
    As before, two endings never do: a turn that a bridge restart ended, and a failed sign-in,
    which gets the `!login` guidance instead.
    **`src/zab/admin/config.py` is on the privileged list (D32):** its diff adds the optional key
    and its rule.
  - **`!rollout`** and both `push_config.py` steps of **`!adopt-spare`** pass it to
    `push_config.py` as `--mention-after-s`, which writes `[bridge] mention_after_s` into each
    VM's `config.toml`. The dry run, and so the plan, prints `mention after: 120 s (…)`. A change
    reaches a VM at its next rollout: send the fleet's last `!rollout` again, with its words.
  - **Upgrade order:** deploy this release to the admin server first, then add the key to
    `admin.toml` (ADMIN §2.1). An older release's loader refuses the unknown key, and its bots
    exit at their start.
  - **Hand runs:** `push_config.py --mention-after-s SECONDS`, default 120, with a release only
    (`workshop/admin/README.md` §2.4).
  - **The quick-start's** *Talking to the agents* says it in one line, with 2 minutes; ORGANIZERS
    §1.1 says it for organizers, and §3.8 lists it among what to edit in the quick-start.
  - **Admin e2e:** the rig's `zab-admin-e2e` has `mention_after_s = 90`. A6 finds it on its
    rollout's step, in its dry run and in each VM's `config.toml`; A20 on both of its
    `!adopt-spare`'s `push_config.py` steps.

- **The file server serves share links, and keeps its own copy of each** (#710). A mirror now
  fetches `refs/zab/shares/*` with the turns, and a share, served at `/<repo>/<sha>/…` as a
  snapshot is, comes from the **share store**, `/var/lib/zab-files/shares/<repo>.git`, apart from
  the mirrors: its first request copies it there, only with its repository's GitHub ID (a 503
  otherwise, which `zab share` retries). So a share outlives its VM, the workshop, and its
  repository's deletion or transfer (#714), until it is revoked: when the repository it came
  from, looked up by that ID, has no share ref at its commit, at once through `POST
  /_zab/recheck/<repo>/<sha>` (which `zab share --revoke` sends; each share once every 10 s, 120
  an hour; a 503, never `kept`, when GitHub doesn't say), or within about 65 minutes at the hourly
  check. A repository that is gone revokes nothing. A revoked share is never taken again. A
  deployer lists and revokes shares with `server.py shares [<repo>]` and `server.py unshare
  <repo> <share|all>` (`workshop/files/README.md`, *Share links*). The share store is not backed
  up.
  - **Upgrade:** the files host's update, as for #714 above, in the same run: the unit gains
    `StateDirectory=zab-files`, which its restart creates.
- **`zab share`: a link to one file or folder alone** (#710). On the VM, for the agents, as
  `zab history` is: `zab share PATH…` commits only those paths, as they stand in the topic's
  working tree, with no parent and a random 256-bit nonce in the message, pushes it to
  `refs/zab/shares/<name>` with the deploy key, opens its link once so that the file server
  takes its copy, and prints the link: the file itself for one file, the folder for one folder,
  the share's root otherwise. The rules are a snapshot's (the denylist, 20 MB a file and 50 MB in
  all, no symbolic link, no submodule, nothing outside the repository): a path named that breaks
  one is refused with its reason, and nothing is pushed; inside a folder, such files are left out
  and listed. A share the server doesn't serve is withdrawn, and no link printed. `zab share
  --list` shows the repository's shares; `zab share --revoke LINK|NAME` deletes one's ref on
  GitHub and asks the server to drop it at once. The agents' instructions tell them to use it, to
  say what the link opens, that it lasts until revoked and how to revoke it, and never to offer
  an answer's own link for sharing; the quickstart's *Sharing results* and "Start here" say to
  ask for one. New participant e2e checks in scenario 9.
  - **Rollout:** the release's `!rollout` installs `zab share` and the instructions; it needs the
    files host's update of the entry above, which serves shares.
- **Participants administer their own repository** (#714). `provision_repos.py` invites each
  participant's login to `<org>/<slug>` as an admin, so that they can add collaborators, and
  transfer or delete it. A run raises an existing collaborator with less than admin to admin
  (`collaborator; raised to admin`), and a pending invitation (`pending; raised to admin`);
  `invites-only` does both, and its dry run shows them (`would raise to admin`), with `N raised
  to admin` in its summary line. Only admin counts as done (`collaborator`); an organization
  owner still does. `--no-rebind` still never removes access.
  - **After the release (the organizers):** one `!provision repos invites-only all` per workshop
    raises its participants, who were invited with push until now (ORGANIZERS §3.4).
  - **A new organization's settings** (BOOTSTRAP, *Before you start*, item 2): repository
    deletion and transfer allowed for admins, and visibility change allowed; outside
    collaborators need no setting; on a paid plan, each outside collaborator on a private
    repository takes a seat.
- **`!retire` and `!final-sync` with a repository that's gone** (#714). The sync now gets the
  GitHub token and looks up each row's repository first: one GitHub finds at another owner's is
  gone at once, and a 404 follows the file server's rule. One that
  its participant deleted or transferred is not synced, and its line says `<org>/<slug> no
  longer exists here: the participant deleted or transferred it`; a `!retire` row's cell reads
  `failed: repository gone`, and the note under its table points to `!retire <slug> unsynced`,
  which loses the work that is only on the VM. **A requirement:** the server's GitHub token
  (`secrets/github-token`, BOOTSTRAP §8.1), which `!final-sync` and `!retire` now need as
  `!provision repos` does: their sync fails when it can't be read. A transferred repository keeps its deploy key,
  and GitHub redirects git to its new place, so the VM's pushes would reach the participant's own
  account: the sync doesn't push there.
- **Docs** (#714): the quickstart's *Sharing results* says that the repository is the
  participant's, and what deleting or transferring it during the workshop stops; ORGANIZERS §4.3,
  what a participant's admin can break and its repair; ORGANIZERS §5.9, *When a participant asks
  for their data to be deleted*, every copy and who removes it.
- **The docs, for a public reader** (#691's docs pass: #697, #698, #699, #700, #701, #702): the
  README's entry points and the workshop's visuals, SPEC and DECISIONS, BOOTSTRAP, ADMIN,
  ORGANIZERS, and the admin, admin server, AMI, AWS, files and waker READMEs. Nothing that runs
  changes: outside the docs, #699 changes one docstring line in each of `src/zab/admin/pages.py`
  and `workshop/admin/status.py`, and #700 the comments of `admin.example.toml`.
- **Tests and CI:** the render tests bound how their CPU grows, not how long it takes (#707,
  #734); the fork tests wait for what they check and for quiet at teardown (#747, #751, #752);
  a second `!btw` is tested only once the first one's turn runs (#748, #750);
  `actions/setup-python` 7.1.0 (#718); the workflows on Ubuntu 24.04, with a lookalike test that
  also works on 26.04 (#719, also in 0.2.16).

## 0.2.16 — 2026-11-02

0.2.16 is 0.2.15 with #754 (PR #755): the bots edit their messages far less, and never more than
20 times each. Zulip Cloud's guidance is to edit a message only when something changes in an
important way, about once a minute at most: a message's edit history keeps every earlier version.
At 0.2.15, a running turn's status message was edited every 10 s, since its elapsed time changed
its text each time, so a three-hour turn edited it about 1,080 times. Nothing else changes but
two CI changes, which change nothing that ships: the workflows run on Ubuntu 24.04, and one test
also works on 26.04.

**Upgrade notes.** The role's policy, the server's units, `admin.toml`'s format and the release
paths are unchanged, and the files host needs no update. The admin bots' progress message changes
with the server's deploy, the bridge's status messages at each VM's `!rollout`. A configuration
that sets `status_edit_interval_s` below 60 no longer loads (below).

**Status messages** (#754; SPEC §4.9, D41)

- **The status message** shows when the turn started, as a Zulip time, instead of the elapsed
  time, and a running step no longer shows its own time; the summary keeps the turn's duration.
  Its actions, plan and notes are refreshed only when they changed: once as soon as they first
  do, from a minute into the turn (looking every 30 s), then at most every
  `status_edit_interval_s`, now 300 s by default. It is also edited when the turn's model
  becomes known, when an approval is requested or answered, when the turn is stopping or its
  backend reconnects, and with the summary.
- **Bounds on every message**, whatever edits it: two edits at least 60 s apart, and 20 edits at
  most. The turn's summary and the answer on an approval request come at once; an edit within its
  minute waits, merged with any later one; a summary that fails is tried again once a minute at
  most. After 19 edits the message says that updates are paused
  and that `!status` shows the turn's progress, and only the summary follows; a summary that can't
  be made by an edit is posted as a new status message.
- **The admin bots' progress message** shows the step and the output's last line, no longer the
  time so far, and is edited only when they change, at most once a minute and 19 times at most;
  the last says that the progress is paused, and the result, with the apply's duration, is
  still posted.
- **Operators:** `status_edit_interval_s` must now be at least 60: a configuration that sets it
  lower no longer loads (`bridge.status_edit_interval_s: must be at least 60`). Change it, or
  remove it for the default of 300. The workshop's `config.toml` (`push_config.py`) doesn't set
  it.

**Docs**

- SPEC §4.9 (the status message, and its new *Status edits* item), §4.10, the sample config and
  B.2; DECISIONS D41, with D6's 10 s marked superseded; ADMIN §2.4 and ORGANIZERS' progress
  message. ORGANIZERS' examples roll out `v0.2.16`.

**Known limits**

- 0.2.14's known limits (below) still hold.

## 0.2.15 — 2026-10-29

0.2.15 is 0.2.14 with #692: the template of the participants' repositories, and the GitHub
description each new one gets, name no event and no date, and the template says that Sage and
Magma are available where the image has them.

**Upgrade notes.** Nothing changes on the VMs but the version, and the files host needs no
update. A repository that `!provision repos` makes after the server's deploy starts from the new
template, with the new description; one made before keeps both.

**For participants' repositories**

- **The workshop repository's template** (#692; `workshop/template`, from which the server's
  `!provision repos` seeds each new participant repository). Its README names no event and no
  date, where it read "coding agents for number theorists, Oct 9–10, 2026" for every workshop,
  and neither does the GitHub description that `!provision repos` gives a new repository, now
  "Workshop repository" (it added "(coding agents for number theorists, Oct 2026)").
  Its `AGENTS.md` said that Sage and Magma "are installed too"; both are optional image
  components, so it now says they are available where the image has them, and that `!tools` lists
  what is installed. Repositories already made keep their text and their description.

**Docs**

- ORGANIZERS' examples roll out `v0.2.15`.

**Known limits**

- 0.2.14's known limits (below) still hold.

## 0.2.14 — 2026-10-28

0.2.14 is 0.2.13 with #687: in a participant's own sandbox, a new topic without a mention talks
to both agents.

**Upgrade notes.** The waker's change, and a new sandbox's "Start here", reach the fleet with the
admin server's deploy; the bridge's, at each VM's `!rollout`. Until a VM's rollout, its bridge
answers a new topic's message without a mention with neither bot, though the waker gives that
message both bots' ⏰s and starts the VM: roll out soon after the deploy. The files host needs no
update.

**For participants' VMs**

- **A new topic without a mention goes to both agents** (#687; SPEC §4.5, "A new topic without a
  mention"). In a channel with one human, the participant's sandbox, a new topic's first message
  needs no mention: each of the VM's bots answers it, as if it mentioned both, and its follow-ups go
  to both until a message mentions just one. A mention still talks to the agents it names. Commands
  count too: an unmentioned `!help` as a new topic's first message gets both bots' help, and the
  next message without a mention starts both sessions. A bot whose agent is known to be signed out
  stays out, silently, unless neither is signed in (then both answer, with their sign-in hint):
  Codex asks its daemon (`account/read`), Claude reads its last `claude auth status` (so a Claude
  not signed in may still answer, with its sign-in message), and unknown counts as signed in.
  Unchanged: multi-party channels (a mention for every prompt), topics with a session (whoever was
  last mentioned answers), a `!fork`'s topic, DMs, asides, other people's and bots' messages,
  `multi_party = false`.
- **Wording:** the session card says "Write in this topic to continue (mention me if others are
  in the channel)", `!help` and the DM hint say a new topic talks to both agents, and so do the
  participants' quick-start, the organizers' guide and a new sandbox's "Start here".

**The admin server**

- **The waker** gives a ⏰ from each bot that allows the sender on a channel message whose topic
  has no earlier mention of the VM's bots, no longer from the first configured bot alone (SPEC
  §13.4.2). A lookup that fails or times out still gives one ⏰, from the first bot. The
  waker cannot see sign-in, multi-party channels or a `!fork`'s topic: there, it still gives
  both ⏰s.
- **The waker's change reaches the fleet when the server is deployed,** not at a VM's
  `!rollout`: the fleet's waker is the admin server's. So does a new sandbox's "Start here",
  which the server's `!provision zulip` posts (`workshop/admin/zulip_provision.py`).

**Docs**

- `workshop/waker/README.md` states the waker's rule for a new topic (#687).
- ORGANIZERS' examples roll out `v0.2.14`.

**Known limits**

- Only a channel with one person in it, the VM's owner, gets the new default. In a channel with
  another person in it, with membership the bridge couldn't read, or with `multi_party` off
  (`!rollout … multi-party=off`), a new topic needs a mention, as before. The bridge counts the
  channel's subscribers who are people with active accounts, and reads them again on Zulip's
  subscription events and at most 3 minutes after it last did (SPEC §4.5).
- Which agent is signed out: Codex's bridge asks its daemon (`account/read`), waiting for it to
  connect until 15 seconds after the bridge's start (a wake, a restart), and 1 second otherwise;
  no answer in time counts as signed in. For Claude, the bridge has the last `claude auth status`
  it ran, for a `!vm`, `!status` or `!login` sent to that bot (never the admin bot's `!status`,
  which runs a doctor of its own), and believes a "not signed in" for 10 minutes. A Claude not
  asked since its bridge started counts as signed in: it takes part, and its turn fails with its
  usual sign-in message.
- A command sent as a new topic's first message without a mention goes to both bots, as if it
  mentioned both: `!help` is answered twice, and `!stop` gets
  `No session here; use !help or mention me with a prompt.` from each. In the empty topic
  (*general chat*) of a channel with one person in it, the first message without a mention gets
  each bot's one-time reply asking for a named topic, as a mention there does (SPEC §4.5 rule
  7.1); 0.2.13 ignored it.
- The waker can't see sign-in, a channel's people or a `!fork`'s topic: there, a message without
  a mention, in a topic where the owner's earlier messages mention neither of the VM's bots,
  still gets both bots' ⏰s, though one bot or none answers it. A mention further back than the
  owner's last 100 messages in the topic counts as none.
- A sandbox made before the server's deploy keeps its "Start here", which says to mention a bot:
  that still works.
- 0.2.13's known limits (below) still hold.

## 0.2.13 — 2026-10-28

0.2.12 was prepared on Oct 27 (#681) but never tagged, so 0.2.13 ships its changes (#673, #677,
#679) and #683's. There is no 0.2.12 release.

**Upgrade notes.** The waker's change reaches the fleet with the admin server's deploy, and the
server's release gains the two modules it now imports (below). The VMs get the bridge and the
agents' instructions at their next `!rollout`, which writes the instructions between the
`zab:managed` markers and keeps the participant's own section below them. No unit is new, the
role's policy and `admin.toml`'s format are unchanged, and the files host needs no update.

**The admin server**

- **A ⏰ from each bot that will answer** (#679, #683; SPEC §13.4.2). A message to both
  Claude and Codex that woke a stopped VM got its ⏰ from Codex alone, although both bots' bridges
  answer it with their 👀. And in the usual case after idle stop, the owner talked to Claude in a
  topic, the VM stopped, and the owner wrote there again without a mention: Claude answered, but
  the ⏰ came from Codex, the first configured bot. Now each owner message in a channel that waited
  for the wake gets a ⏰ from each bot that will answer it, as the bridge routes it (SPEC §4.5), if
  that bot allows the sender, in the configured order (Codex first):
  - **a message that mentions the VM's bots:** from each of them (#679);
  - **a follow-up without a mention:** from each bot that its topic last addressed, those of the
    owner's latest earlier message there that mentions either of the VM's bots, read as the bridge
    reads a mention (outside code, not escaped, not an aside): Claude alone after a mention of
    Claude, both after a mention of both (#683). That message is the latest such mention among the
    messages that waited for this wake, which wins, or else one from before them, which the waker
    reads in the topic's history after the start request. One request per topic, as a bot of the
    VM, of the owner's 100 earlier messages there at most, for 10 topics per wake at most, after
    one `users/me` per bot, all within 5 seconds; the start never waits for it. Only the owner's
    own messages count, never a bot's;
  - **the fallback,** with no mention found (in a new topic, say, or past those 10 topics), or if
    a request fails or runs out of time: one ⏰, from the first bot, in configured order, that saw
    the message and allows the sender. A failure is logged by Zulip's error code or the
    exception's class alone, and nothing read is kept.

  DMs still get one "⏳ Starting your VM" notice per wake, and a copy that arrives late still adds
  no ⏰: the memory of acknowledged messages is per message, so no bot adds a second. A message
  first seen after the start request gets no ⏰: the bridge's 👀 answers it, once the VM is up. The
  bridge's reading of a mention, and its code protection, move to two small modules that the
  waker imports, `zab.core.addressing` and `zab.render.code`. The server's release paths gain them
  (`workshop/adminserver/release-paths`), so the release review shows them whole. The fleet's
  waker is the admin server's: the change reaches the fleet when the server is deployed, not at a
  VM's `!rollout`.

**For participants' VMs**

- **The bridge's own messages say so** (#677; SPEC §4.5, §4.9, B.4). On Oct 27 a Claude bot
  posted `Permission denied for: Bash(cd …/search && pkill -f…)` as plain text under its own
  name, where it reads as Claude's own words, which would confuse someone who has never worked
  with Claude Code. A message from the bridge, or from the program the agent
  runs in, now starts `⚠️ **Bridge** (not Claude):` (or `(not Codex):`) for a warning or an error,
  and `🔧 **Bridge** (not …):` for information. Marked: each backend notice; a failed session
  operation; the recovery line under held messages; an answer whose delivery is pending or that
  couldn't be formatted; a missing diff; a rejected attachment; `!fork`'s failures and refusals; a
  failed session creation; the one-time replies to a plain mention in a public channel or an empty
  topic; an expired message, and the catch-up's "arrived while your VM was off"; and, inside an
  answer, a failed turn's error, the sign-in guidance, "No stop confirmation after 30 seconds",
  recovery's "The stored turn is no longer active.", a lost steer and a stopped background job. Not
  marked: the replies to `!` commands, the card, the status message, the ⏳ waiting line, approvals,
  the diff message, the `@user` ping, the `⑂` fork lines and the sentinel's DMs, which are plainly
  the bridge's already. Peer context leaves the marked messages out, as it does the other control
  lines. Text from elsewhere is redacted, its NULs dropped first, before a marker or a prefix goes
  in front of it (#678).
- **A command that Claude Code blocks is explained plainly** (#677; SPEC §9.4). One message per
  result: ``⚠️ **Bridge** (not Claude): Claude Code, the program Claude runs in, blocked this
  command under its safety rules. It needed approval, which can't be given from Zulip, so it did
  not run. Claude was told and can carry on.``, then a line for each blocked call, its command's
  start (at most 120 characters) or `Tool(title)` for another tool, with Claude Code's reason
  under it when the call's result gives one: the two lines after `What required approval:`, on one
  line, cut at 300 characters and then back to the end of its last whole sentence. Several
  denials make one message, `these 2 commands` (or `actions`). The command and the reason are
  code, so a `*` or a `$` in them stays as it is.
- **Codex's and Claude Code's other notices name who speaks** (#677; SPEC §8.5): a request zab
  doesn't support, such as `item/tool/requestUserInput`, is declined with
  ``🔧 **Bridge** (not Codex): Codex asked for something the bridge can't do from Zulip (…), so
  the bridge declined it. Codex was told and can carry on.``, now as information, since nothing
  failed; an error outside a turn reads ``⚠️ **Bridge** (not Codex): Codex reported an error
  outside a turn: …``; Claude Code's notices of an API key used instead of a Claude account
  sign-in, and of a third-party provider, name Claude Code.
- **The agents write times as Zulip times** (#673; SPEC §4.9, §11.1, §11.3). A Claude answer
  said "The job started at 01:08:38", in UTC with no zone, and quoted epoch seconds. The agents'
  instructions now ask for Zulip's `<time:2026-10-27T01:08:38Z>`, in UTC and
  not in backticks, which each reader sees in their own time zone; never a bare clock time, no
  epoch seconds in prose, durations as before (`3 min 0 s`), and times inside quoted logs,
  command output or code exactly as they are. The bridge passes such a time through whole: the
  link rewriter never reads a file reference inside one, and a status note cut to fit is cut
  before one, never inside it. The instructions reach a VM at its next `!rollout`.

**Docs**

- The participants' quickstart, *Talking to the agents* (#677): messages starting ⚠️ Bridge or
  🔧 Bridge come from the bridge or from the program the agent runs in, not from the agent. *Your
  VM sleeps when idle* (#679, #683): in a channel, each bot that will answer reacts with ⏰: each
  bot you mention, or, if you mention none, the bots you last mentioned in that topic (one bot in
  a new topic).
- ORGANIZERS §4.2 and `workshop/waker/README.md` state the waker's rule (#679, #683; SPEC
  §13.4.2). ADMIN §1.6 names the two modules that the waker adds to the server's release.
- ORGANIZERS' examples roll out `v0.2.13`.

**Known limits**

- A Claude turn that is steered into several loops, each ending in its own result, posts one
  denial message per loop, each with that loop's new denials, rather than one for the turn
  (#678).
- An agent can write the Bridge marker in its own answer, and nothing stops it: the marker tells
  the bridge's messages apart, it doesn't authenticate them (#678).
- Claude Code's reason is its own text, from the denied call's result; a denial without one,
  such as a `dontAsk` one, shows no reason line.
- A single line longer than a whole Zulip message part, about 9,800 characters, is cut at fixed
  offsets, and that cut can split a Zulip time, as it can a link or bold text (#676).
- The waker can't see a channel's people: in a channel with another person in it, where the
  bridge answers only a mention (SPEC §4.5, "Multi-party channels"), a follow-up without a
  mention still wakes the VM and gets the ⏰ of the bots its topic last addressed, though no bot
  answers it (#683).
- The waker can't see sessions: the fallback's one ⏰ comes from the first configured bot that
  allows the sender, whichever bot then answers (#679, #683).
- The waker reads mentions of the VM's own two bots alone, while the bridge's record follows a
  mention of any bot. After an owner message that mentions only another bot, the waker looks
  past it, to an older mention of the VM's bots or else the fallback; the bridge answers no
  follow-up without a mention, unless that message starts with another participant's
  `Claude · …` or `Codex · …`, which the bridge reads as the owner's own bot of that kind (with
  `multi_party` on, the default), and that bot answers (SPEC §4.5, "Mention detection"; #683).
- 0.2.11's known limits (below) still hold.

## 0.2.11 — 2026-10-27

0.2.11's admin bots take any run of spaces after their mention (#650), and their notes show in
italics (#651). On the VMs, the status message shows a ✅ once the agent has read a message sent
during its turn, the running step's time and the last three notes (#654); the agents'
instructions tell them to answer such a message at once, to check a long job every 30–50 seconds
with a note after each check, and to give thinking work, never a long computation, to a
background sub-agent (#653); and the Codex daemon may hold 65536 open files (#664).

**Upgrade notes.** The role's policy, the server's units and `admin.toml` are unchanged, and the
files host needs no update. The VMs change only at their next `!rollout`, which gives a running
Codex daemon its new open-file limit without restarting it.

**The admin server**

- **The admin bots take more than one space after their mention** (#650; ADMIN §2.2,
  ORGANIZERS §2.1). Zulip's autocomplete ends a mention with a space, and a sender who typed
  another got ``❌ put one space between the mention and the command``, although the message
  showed one. Any run of spaces, no-break spaces and tabs between the mention and the `!` now
  reads as one space. A mention glued to its command still gets a hint,
  ``❌ put a space between the mention and the command``, and any other white-space character
  there (U+3000, U+2028, …), which got no answer, gets
  ``❌ use plain spaces between the mention and the command``. An invisible character that
  isn't white space, such as U+200B, still leaves the message talk, with no answer. A human
  stranger's command after a no-break space, a tab or other white space now gets the 🚫
  refusal, as any of their commands does, where it got the ⚠ warning in a channel, or nothing
  in a DM; a participant bot's message still gets the warning.

- **The admin bots' notes show in italics** (#651; ORGANIZERS §2.2, §2.4, §5.1). Zulip has no
  `_…_` emphasis, so `!status all` ended with `_Retired, so left out: …._`, underscores and
  all, and so did the notes under a plan, a status or a result cut to fit, and under a long
  output. They are written `*…*` now, which Zulip shows in italics. A test fails on any `_…_`
  phrase, or `__…__` one but a dunder name, in a Python string literal of `src/zab` or
  `workshop/` (`lean-project` included), docstrings and the patterns given to `re` aside.

**For participants' VMs**

- **A message sent during a turn is marked read** (#654; SPEC §4.7, §7.3). Beside ↪️ (written
  into the running turn), the bot now reacts ✅ once the agent has taken the message in, at its
  next step: Codex's item for the steer, Claude's echo of it. Both come after the tool call that
  runs, so a long command delays the mark, as it delays the reading.
- **The status message shows more of what the agent is doing** (#654; SPEC §4.9, B.2): a step
  still running shows its time so far (`⏳ python3 run.py · 3m 20s`); the agent's last three
  notes, newest last, instead of only the latest; each note on one line, cut after a word with
  `…`, never mid-word. Nothing changes in how a turn ends or in its answer.
- **Secrets are redacted before text is put on one line or cut** (#658, #661; SPEC
  §11.6): the status message's notes and action titles (Claude's and Codex's), an approval's
  command and diff, a failed turn's error, the `!attach` listing's titles and previews, Claude's
  error and exit messages, and the `!login` failure DM. A zuliprc `key=…` is matched as a whole
  line, so a title joined onto one line, or cut through the key, showed it. And that line now
  ends wherever `str.splitlines` ends one (`\r`, U+2028, …), not only at `\n`: in v0.2.10,
  Claude's exit message showed whole a key line that followed a progress `\r`.
- **The agents stay visible and responsive during a turn** (#653; SPEC §8.4–§8.5, §9.4, §9.7,
  B.2). The agents' instructions, which both agents read and each rollout installs, gain three
  parts:
  - *During a turn: stay visible and responsive.* Until the final reply, the status message is
    all the participant sees: one short sentence before a step of more than about 10 seconds,
    and a note at least once a minute while waiting. A message sent during the turn is answered
    at once, in a sentence or two, and repeated in the final reply if it still matters, since
    the notes are gone once the turn ends. No call blocks for more than about 50 seconds, since
    the message waits for the current call; one that can't be put in the background, such as
    the first Lean call after a start, is announced.
  - *Long-running commands.* Anything longer than about 50 seconds runs between calls, checked
    every 30–50 seconds in calls of at most 50 seconds, with a progress note after each check:
    Claude's in the background with a log, in bounded loops (`seq 5` × `sleep 10`, where it was
    `seq 8` × `sleep 30`); Codex's started with `exec_command`, its output also in a log through
    `tee`, and checked with `write_stdin`, since a `&` or `nohup` job dies with its call.
  - *Sub-agents,* new: thinking work (a literature search, a proof attempt, an independent
    check), never a long computation, may go to a background sub-agent, one at a time: Claude's
    Agent tool in the background, Codex's `spawn_agent`, then `wait_agent` for at most 50
    seconds a call. It logs its progress to a file of its own, read at each check. The turn
    doesn't end while one runs; when it must, the agent stops it first (Codex:
    `interrupt_agent`) and says what it had found, and after a `!stop` it stops any Codex
    sub-agent left running. Sub-agents use the plan's limits faster (`!usage`).

  The live checks, for both agents, are in DECISIONS' verification log: #653's, of earlier
  wordings, on 2026-10-25; on 2026-10-26, the `!stop` check and #659's sanity check of three
  later ones, the shipped wording among them.
- **The Codex daemon may hold 65536 open files** (#664; SPEC §13.3). Each Codex thread it
  has loaded keeps a `lean-lsp-mcp` child (#632), and the default limit of 1024 was reached:
  on a test VM, 967 files a minute after boot, and a shell failed with "No file
  descriptors available". `codex-app-server.service` now sets `LimitNOFILE=65536`, kept
  within the user manager's hard limit. The rollout gives a running daemon the new limit at
  once with `prlimit`, without restarting it, so no Codex turn is cut; where `prlimit` fails,
  the VM's row says in a note that the limit applies at the daemon's next start. The bridge's
  unit is unchanged.

**Docs**

- The participants' quickstart, *Talking to the agents* (#654): you can write while an agent
  works; it reads the message at its next step, usually within a minute, ✅ shows that it has,
  and it replies in its status message; `!stop` interrupts it instead.
- ORGANIZERS' examples roll out `v0.2.11`.

**Known limits**

- A message sent while a Claude background sub-agent runs, after Claude's turn has reported its
  result, is held (⏳) until the sub-agent ends, since zab closes the turn's input at its first
  result; and a Codex sub-agent's answer is lost if its parent's turn ends first. The
  instructions tell both agents not to end a turn while a sub-agent runs, and to stop it first,
  saying what it had found, when the turn must end; `!stop` ends a turn, not a Codex sub-agent
  (#655).
- A bridge restart during a turn forgets the messages awaiting their ✅: they get none, though
  the agent reads them.
- Claude may skip the note a minute while it waits on a background sub-agent: the rule is the
  instructions', not the bridge's. In one check of the shipped wording, Claude went 130 seconds
  without a note, with a sub-agent doing thinking work; Codex, in the same check, went 53 seconds
  at most. At an earlier wording, Claude's notes kept within the minute with a background command
  (57 seconds at most); Claude with a background command hasn't been measured on the shipped
  wording. The running step's time (#654) still shows that the turn is alive (DECISIONS,
  2026-10-25 and 2026-10-26; #668).

## 0.2.10 — 2026-10-26

0.2.10 includes 0.2.9 and 0.2.8 (their sections, below), which had no deployment of their own.
New in 0.2.10: a workshop's participants' channel and the roster from its members (`!roster
sync`, with `!roster remove`, `restore` and `rename`: #627), `!provision zulip` without Zulip
invitations (#628), the sentinel's health in `zab doctor`, `!status` and the console (#588),
Codex's idle Lean servers stopped (#249), the image's Magma licence setup as the deployer's
script (#595), and `zab init --profile personal` (#619).

**Upgrade notes** (from 0.2.7 or 0.2.8):

- **The role's policy changes** (0.2.9's volume statements, for `!grow-disk` and the volumes'
  sizes): render it with the new release and apply it before the deploy, as BOOTSTRAP §6.5's
  step 3 applies a new render, the new second policy attached first, then the first replaced.
  Without them, the release prices every volume at 200 GB and refuses every `!grow-disk` at its
  dry run. `!grow-disk` also refuses a VM whose root volume lacks one of the scope tags.
- **`admin.toml`:** a workshop's entry may take one more key, `channel` (#627); the loader
  accepts every file 0.2.7's accepts. No server unit is new, and Caddy's configuration is
  unchanged.
- **The VMs** change only at their next `!rollout`, which installs the sentinel
  (`zab-sentinel.service`, a user unit of `agent`). The bridge's database gains a column,
  `sessions.parent_session_id`, at its first start; `user_version` stays 1, so an older zab still
  opens it after a rollback.
- **The files host** needs no update. **The next machine image** names the licence script with
  `--magma-licence-setup` (below); images already built are unchanged.

**The admin server**

- **A workshop's participants' channel** (#627: #638; NEW-WORKSHOP). `!new-workshop …
  channel="<name>"` records, in the workshop's `admin.toml` entry, the ID of the private channel
  whose subscribers will be its roster: `channel`, optional, which the loader
  (`zab.admin.config`, privileged) refuses when another workshop, a bot's command channel or an
  alerts channel has it. The channel is adopted when `zab-provisioner` is subscribed to it;
  refused, with what to do, when the provisioner sees it without being in it; and otherwise
  made, private with shared history, for the organizers and the provisioner, before anything
  else the apply makes in Zulip. A channel `admin.toml` binds already (a bot's,
  an alerts channel, another workshop's), or one named like a workshop's `#…-organizers` or
  `#…-fleet-alerts`, is never adopted: the plan says so. The name is the one value in quotes,
  straight or a phone's curly ones, and goes to `new_workshop.py` percent-encoded. The config
  helper (`zab.admin.confighelper`, privileged) takes the new key.
- **`!roster sync`: the roster from the workshop's channel** (#627: #641; ADMIN §3.1a,
  ORGANIZERS §3.1). Each active human subscriber of the channel gets a row, linked by Zulip user
  ID from the start, organizers included, never the provisioner's own account. Its slug is made
  from the Zulip name (folded to ASCII, cut to 32 characters, `p-<user ID>` when nothing is
  left), unique on the server for its whole life (`-2`, `-3`, …). A row is matched by its ID (a
  legacy one by its e-mail), and only its
  name is refreshed. Someone who left the channel, or was deactivated, loses the row only if
  nothing is provisioned for it (no VM, bots, sandbox or repository), and is otherwise reported
  and left for `!retire`. The plan is hashed on its whole dry run, which lists every change, one
  line each, and the apply writes only the approved changes. A sync with nothing to change is
  refused, saying so, and a public channel is warned about at every sync. A new row whose
  address the scripts refuse (an apostrophe) gets a placeholder address, and is linked by its ID.
  New script `workshop/admin/roster_sync.py`, in the release paths and the selfcheck's scripts
  (`workshop/adminserver/release-paths` and `zab.admin.checks`, both privileged: one line each).
- **`!roster remove`, `!roster restore`, `!roster rename`** (#627: #641). `remove` excludes a row
  (its `excluded` cell): no sync adds it again, `all` leaves it out of every verb, and a command
  that names it is refused; `restore` brings it back; `rename` changes a slug under the sync's
  rules. `remove` and `rename` are refused once anything is provisioned for the row.
  `!roster show` shows each row's `name` (else its first and last names) and marks the excluded
  ones.
- **Rows linked by Zulip ID, downstream** (#627: #641). `!provision zulip` (`--no-rebind`)
  matches a row whose e-mail binds nobody, a placeholder address included, by its recorded ID when
  that is an active human's; git's `user.name` on a VM is the row's `name`, else its first and
  last names, else its slug.
- **The plan's hash holds no Zulip lookup** (#589: #641): ADMIN §2.5 now says that
  `!roster import`'s own re-check at apply refuses a join since the plan, and the unused
  `Live.zulip` is gone.
- **`!provision zulip` sends no Zulip invitations: the organizers invite in Zulip** (#628: #636;
  it supersedes #610 and #322). The organization lets only administrators invite,
  and `zab-provisioner` is a Moderator, so Zulip refused its invitations
  (`Insufficient permission`). `zulip_provision.py` now neither sends nor reads one. A row whose
  address matches no Zulip user that the provisioner can see reads `not on Zulip` under
  `invite`, where `sent`, `pending` or `would send` stood, in the plan's dry run and in the result
  alike, with one line under the table that tells the organizers to invite that address in
  Zulip. It is not a failure: a later `!provision zulip` binds the participant once they have
  joined, and the roster's `zulip_invite` gets no `sent …` any more. ORGANIZERS §1.5, new, says
  how to invite: by e-mail in Zulip, with the participants' channel ticked (never the organizers
  or alerts channel), and revoking the invitation to an address that someone replaced.
  `!roster import` still revokes, at its apply, an open invitation to a corrected address that
  the provisioner sent before 0.2.10. The participants' quick start, their bots' `!restart`
  failures and `zab preflight`'s failure line send them to their workshop's channel instead of
  `#help`.
- **The sentinel's health** (#588: #642; SPEC §13.1, ADMIN §4.1). `zab doctor` gains a
  `<bot>:sentinel` check for each bot where `zab.service` is installed: `zab-sentinel.service`
  installed and active, and the bot's mark from this boot and fresh. Anything else is a warning,
  never a failure: DM `!restart` is then unavailable, and the bridge answers DM `!vm` itself
  (`unknown` when the doctor can't reach the user's systemd). `status.py` shows it in a
  `sentinel` column and its JSON (`ok`, `down`, `stale, no mark`, …; `?` from an older zab), and
  so `!status` (a line for one VM, a flag for each of several whose sentinel isn't `ok`) and the
  console (a column and a row), and ORGANIZERS §3.6 and §4.1 say what to do. It comes with the
  doctor's read, which idle stop doesn't count. With it, two small sentinel replies: the
  organizers' hold refuses `!restart codex` in words about Codex, not the bridge; and a `!vm`
  report that Zulip refuses for good says so ("⚠️ Zulip refused the VM report (…)"), not that
  the report failed.

**The machine image**

- **The AMI build's Magma licence setup is the deployer's script** (#593: #595; DECISIONS D39,
  `workshop/ami/README.md`). `build_ami.py --magma-licence-setup SCRIPT`
  (`build.sh`'s `MAGMA_LICENCE_SETUP`), optional with `--magma-dir` and `--magma-passfile`,
  replaces the build's earlier licence option. Beyond its `#!` line the build doesn't read the
  script: it installs it root-only and runs it as root at every boot through
  `magma-licence.service`, before logins and user services, which wait for it two and a half
  minutes at most. The smoke test checks that Magma computes `print 2+2`, before and after a
  reboot, and that the unit is active where the image has it. A failed tool check's reason and
  last lines, in the build log and the smoke output, hold no hardware address. **This changes
  the next image, not the VMs:** an image already built keeps what it has, and a build command
  that names the old option now stops before it creates anything.
  - Build the next image with `--smoke-warm`: in one smoke build, Lean's import timed out on a
    cold disk; after the VM's reboot it took 107 s.

**For participants' VMs**

- **Codex's idle Lean servers are stopped** (#249: #635; SPEC §7.12). The Codex app-server keeps
  one `lean-lsp-mcp` per loaded thread, and each keeps its Lean servers (`lake serve`,
  `lean --server`, a `lean --worker` per open file, about 1.3 GB of anonymous memory with
  Mathlib) until the daemon restarts. The bridge now stops a server once it has been idle for
  `[lean] reap_idle_minutes` (default 30; 0 turns it off), in any directory, `~/lean/workspace`
  included: the same processes, gaining at every pass less than 0.04 s of CPU a minute per
  process (at least 0.1 s). Idle trees gain about 0.01 s a minute per process, and a first check
  of a file about 20 s. That thread's `lean-lsp-mcp` stays, and its next Lean call starts a fresh
  server (DECISIONS, 2026-10-25). It never touches
  `lean-lsp-mcp`, `lake build`, `lake env lean`, anything under Claude, or another user's
  processes; it checks each PID's start time through a pidfd before SIGTERM, and again before
  SIGKILL 5 s later. Each stop is one `lean_reaped` line in the log: the PIDs, the directory, and
  the memory freed. `push_config` leaves the key out: the default is the workshop's value.
  - The agent instructions' "Mind the memory" says so, and drops the agents' own procedure for
    stopping servers, which covered only the topic's worktree and not `~/lean/workspace`. The
    quickstart's `!restart codex` line and ORGANIZERS' "A VM is slow" row say it too.
- **A `!fork` held back by a reopen** (#591: #634). A `!fork` waiting for its session to reopen
  (⏳) is handled when the reopen ends, as a new `!fork` would be, also when that reopen finds the
  session's last answer still undelivered (as when a card comes back to a private channel), or
  finds its Claude turn still running though the agent's snapshot no longer reports it. Such a
  fork waited for the agent's next reconnect.
- **`zab doctor`'s sentinel check** (#588: #642): above, *The sentinel's health*.
- **`!usage` no longer shows old Claude limits as current** (#647: #648; SPEC §4.6). Claude
  reports its account limits only during a turn, and `!usage` showed the last report whatever its
  age: a week-old 91%, with reset times in the past. A window whose reset time has passed now
  shows no bar and no percentage, but "unknown until the next Claude turn" and "reset at <time>".
  The limits table has its own line, "Account limits · as of <time>", the time the report was
  stored, apart from "This session · as of". The usage file is unchanged: it always recorded
  that time. Codex reads its limits for each `!usage`, so its figures were current; a reset that
  has passed there reads "unknown; try !usage again".
- The agents' instructions (#622: #623): a VM's list of its software nested too deeply to parse
  is now reported as `not JSON: nested too deeply` in the managed block. Python 3.14's own
  message gives the stack it used, which differs from run to run, so a second rollout with the
  same list changed the block.

**For your own machines**

- **`zab init --profile personal`** (#593: #619; SPEC §5.3). The profile for your
  own machines, still the default, is renamed from `drew` to `personal`, so that the public tool
  doesn't name a person. `--profile drew` still works as an alias: it writes the same config as
  `personal`, byte for byte. Nothing else changes. The profile's name was never written to the
  config, so existing configs load and behave as before.
- **`python -m zab.commands.init`** (and `python -m zab.commands.doctor`) passes its arguments,
  as `zab init` and `zab doctor` always did; it crashed before (#621: #633). SPEC §5.3 says what
  `zab init` writes for Claude: one account, `main`, the CLI's default directory `~/.claude`,
  without asking, when it adds a Claude bot to a config without a `[backends.claude]` table;
  with the personal profile, an existing table is left as it is.

**Docs and publication** (#593)

- The license's holder is Andrew V. Sutherland, in `LICENSE` and `workshop/files/LICENSE`, now
  the same file byte for byte (#620).
- `SECURITY.md` (#620): report a vulnerability privately, with the *Report a vulnerability*
  button of the repository's *Security* tab, never in a public issue or pull request.
- `CONTRIBUTING.md` (#620): only the maintainer merges a change that comes from a pull request,
  having reviewed it; no automated process or coding agent merges one. The README's
  "Status and support" drops its sentence on whose workshops zab is maintained for.
- DECISIONS D19 says what it decided: Magma on the image, from an installation tree and a
  licence file that are copied to the builder and never committed, its licence step superseded
  by D39. The verification log records the redaction; no decision, result or date changed.
  `workshop/ami/README.md` and the build's comments say that Magma is installed under the
  deployer's licence (#618).
- Two tests' public address is a documentation one (RFC 5737) now, and a test's docstring cites
  its issue alone (#617).
- ORGANIZERS' examples roll out `v0.2.10`.
- **`docs/WORKSHOP.md` is retired** (#629: #639): it was the Oct 9 workshop's plan and runbook,
  and it is out of date. What of it is still true moved: the image and its first boot to
  `workshop/ami/README.md`; the VMs and regions to `workshop/aws/README.md`; the provisioning
  scripts, the roster, the exports and idle stop to `workshop/admin/README.md`; the spare
  hand-over by hand to ORGANIZERS §7.2; the Zulip organization's settings to BOOTSTRAP §5.7.
  Every citation of it now points to a published file or stands alone, in the docs, the comments
  and the few messages that cited it. No behaviour changes.

**CI and tests**

- Python 3.14 (#622: #623). `requires-python = ">=3.11"` admits it, and CI now runs it: the
  `test (3.14)` and `timing (3.14)` jobs. The workshop image and the admin server still run
  Ubuntu 24.04's Python 3.12.
- The config helper's test of a 20000-deep request no longer depends on the Python. Python
  3.14 parses it, a list, and the helper refuses it as having other keys, where 3.11 to 3.13
  refuse it as not JSON. Either way no file changes. A new test raises the parser's
  RecursionError on every Python. The helper itself (privileged) is unchanged.
- New admin scenario A22 (#627: #641), the roster from a channel: a sync, `!roster remove`, a
  sync that doesn't add the row again, `!roster restore`, a rename, and a sync after an
  unsubscription. A2's invitation step reads `not on Zulip` (#628: #636). Participant scenario 15
  reads the sentinel's doctor check (#588: #642).
- Flaky tests fixed: the runner's group-SIGTERM tests wait on readiness, not a delay (#631:
  #640); the twice-main scrub test is timed after a cheap prefix that spends the run's budgets
  (#615: #644); the slow-client console test asks until it gets 200 (#643: #645).

**Known limits**

- The sentinel's DM `!vm` has neither the running turns nor the sign-ins: a topic's `!vm` and DM
  `!status` still show them.
- IAM can't yet be shown to cap the size `ModifyVolume` grows a volume to (VERIFY V18, open): the
  server's code caps it at 1,000 GB, and only through an approved plan.
- `!retire` takes at most 3 rows with `export`.
- `!roster sync` offers a row to anyone subscribed to the channel, whoever added them: read each
  plan's `+` lines before approving it (ORGANIZERS §3.1).

## 0.2.9 — 2026-10-24

0.2.9 had no deployment of its own: 0.2.10 includes it, and its upgrade notes cover this
release's change of the role's policy. New in 0.2.9: `!grow-disk` and `!retire`, a global
admin's approval alone for a destructive or server plan, each VM's disk size, prices by region,
the roster import's hardening, `!fork`, and `zab sentinel` for `!vm` and `!restart` in a DM.

**The admin server**

- **`!grow-disk slug NGB`: a participant's disk, grown** (#553: #556, #557, #560, #561; #563:
  #584; ADMIN §3 action 3, ORGANIZERS §4.1). One row's root volume, to N GB, at most 1,000: a
  change plan, one approver, no phrase (Q16). The dry run shows the volume, its type and size, the
  new size and what the added GB cost a month at $0.08 per GB, and on a running VM the disk's use.
  The apply sends `ModifyVolume` with the size alone, waits until EC2 reads `optimizing` or
  `completed`, then grows the file system online over SSH (`GROW`), and shows the new `df` line.
  A stopped VM isn't started: its next boot grows the file system. Refused at plan time, and
  again at the apply: a size that doesn't grow; a volume modified less than 6 hours ago (EBS's
  limit, with the time to request again), or still `modifying`; a VM `pending` or `stopping`; no
  single VM with the scope and `participant=<slug>`, or one not at the row's address; a root
  volume without every scope tag. The volume's own size on a running VM grows its file system
  alone, at no cost and without the 6-hour wait: that finishes a grow whose SSH step failed
  (#563). By hand, without the bot: ORGANIZERS §7.1.
  - The cost estimate prices each volume at the size EC2 shows, read every 10 minutes
    (`DescribeVolumes`), where it used to assume `launch.py`'s 200 GB (#556).
  - Admin scenario A21 grows a running VM's disk, and a stopped one's file system at its next
    boot; A13 checks that the role can't modify the server's volume, nor one outside the scope.
- **`!retire slug… [export] [unsynced]`: one plan ends named rows** (#559: #564, #565, #566,
  #592, with #567; ADMIN §3.3, ORGANIZERS §5). For each row in turn, alone, it runs
  the separate verbs' scripts: start and wait, idle stop off, the waker off, uninstall, final
  sync, export (with `export`, at most 3 rows), the protection off, teardown, the bots
  deactivated. A row stops at its first failure, and the others go on; a row whose sync fails is
  not torn down unless the command says `unsynced`, which also retires a row whose VM is gone.
  Destructive class: the phrase `retire <n>` (and `unsynced`), and a global admin's approval
  alone completes it (D36). The result is one table, a row per participant and a column per step,
  with each row's time, and says for each row stopped what its VM is left as and how to go on.
  - **Retired rows:** the ledger records each row's uninstall and sync as the separate verbs do,
    and the rows retired. `all` leaves them out for every verb, and so does the above-half phrase
    rule's fleet; `!status all` names them once, under its answer; the console leaves them out
    once their VM is gone; the verbs that reach a VM refuse a retired row named. A deployer clears
    its `host` and `ip` in the roster before a `!launch` or `!adopt-spare` brings it back, and
    both are refused until then; its bots stay deactivated, since only a Zulip organization
    administrator can reactivate them.
  - **The VM synced is the VM torn down.** A row is refused when the VM its steps reach (by its
    address, or by its tag in any case) is not the one tagged with its slug, which its teardown
    ends, and so is a row with two VMs; a row with no VM found is refused unless the command says
    `unsynced`. ADMIN §3.3 gives a deployer the read-only check of the tags and the retag.
  - `teardown.py --strict` fails a VM still read as protected. The 5-minute wait after a
    `!protect` holds a `!retire` back too (`no !teardown or !retire for 5 minutes`). `export` is
    now a reserved slug.
- **A released Elastic IP given again no longer jams the roster** (#604: #606; `workshop/aws/README.md`).
  EC2 gave an address released by a teardown to the next VM launched in the same workshop. The
  torn-down row kept it, and every admin script refused the roster (`duplicate ip`) until a
  deployer edited it. Now `teardown.py --roster`, which `!teardown` and `!retire` pass, clears the
  `ip` of each row at an address it released, keeping its `host`. `launch.py` and
  `adopt_spare.py` clear the `ip` of another row at the address EC2 shows on the VM they record,
  which also unsticks a roster stuck already: `!launch` the row whose VM has it, as the refusal
  now says. A spare handed over by hand (ORGANIZERS §7.2) is never taken for a stale row:
  `launch.py` only reports it, and `adopt_spare.py` refuses a spare whose address a row holds
  with its host keys. `scan_hosts.py` replaces a pinned key that the VM's console vouches for
  (above).
- **A global admin's approval alone completes a destructive or server plan** (#558: #562;
  DECISIONS D36). `!teardown`, `!deactivate-bots`, `!retire` (and the rebind class, when
  it lands) still take `destructive_approvals` distinct approvers from the organizers, but one
  approval from a global admin completes them, whatever was given before, the requester's own
  included. `!new-workshop` takes one global admin. The typed phrase is still required. The answer
  names the rule: `✅ P-50 approved (a global admin's approval)` or
  `✅ P-50 approved (2 distinct approvers)`; the approval message says
  `It takes 2 distinct approvers, or a global admin alone`. `admin.toml`'s loader (`config.py`,
  privileged, D32) accepts a workshop with fewer approvers than `destructive_approvals` when the
  server has a global admin, and `!new-workshop` needs one global admin rather than
  `destructive_approvals` of them: every `admin.toml` the 0.2.7 loader accepts, this one accepts
  too. Admin scenario A9 has a teardown approved by a global admin alone.
- **Each VM's disk size, in the console and `!status`** (#552: #555; ADMIN §4.2, ORGANIZERS §3.6
  and §3.9). The console shows the disk as `152 of 194 GB (78%)`, in binary GB as `!vm` and
  `df -h` count them, in the fleet's row and on the VM's page. `!status` keeps the percentage,
  and gives the free space of a disk over 90% full: `disk 92% (15 GB free)`. The VMs' read is
  unchanged: `status.py` keeps more of the same `df` line. A read from before the release shows
  the percentage alone. Admin scenarios A6 (a full disk) and A11 (the console's sizes) check both.
- **Prices by region** (#499: #581; ADMIN §11.2). The cost estimate's prices are by region and
  instance type, checked against AWS's price lists on Oct 23 (the r7a prices were slightly off).
  A workshop in a region without prices shows `no prices for <region>` instead of US prices, in
  `!status` and the console.
- **The roster import revokes a corrected address's invitation** (#322, #327: #576; ADMIN §3.1,
  ORGANIZERS §3.1). An e-mail change of a row not on Zulip, while an invitation to the old address
  is open, used to be refused until a Zulip administrator revoked it. Now the plan says
  `the apply first revokes the open invitation to the old address`, and the apply revokes it
  before it writes the roster. A revocation that fails refuses the import, and nothing is written.
  The provisioner's key sees only the invitations it sent: one sent by hand is still revoked by an
  administrator. Hardening (#327): a hand-edited `zulip_user_id` such as `0101` reads as user 101,
  and one that isn't digits refuses the roster; an uploaded `github` given as `@alice` or
  `https://github.com/alice/` is `alice`; `status.py` reads a VM's status of the wrong shape as
  nothing, not as an error row.
- **A stranger's window survives a failed record** (#510: #579). When the audit record of a
  closed window failed, the window was lost and the stranger's message went unanswered; now the
  window is kept and the message answered. With #375's tests, and its decision: the backups have
  no retention limit, since each is encrypted to the deployers' keys of its day (ADMIN §1.7).
- **v0.2.8's two changes** (its entry, below): a plan that needs a typed phrase shows its approve
  line in a code box with a copy button (#544), and a command sent as code gets an answer and
  never runs (#547).

**For participants' VMs**

- **`!fork [--name NAME] prompt`: a copy of the topic's session, in a new topic** (#202: #568,
  #572, #580; SPEC §7.11; quickstart). In a topic where the bot has a session, it starts
  `<topic> ⑂ 1` (or `⑂ NAME`), from a copy of the conversation through its last completed turn
  and of the files, uncommitted changes included, on a branch of its own, and runs the prompt
  there while the topic carries on. The parent topic gets a pointer, and, once the fork first
  answers, a link to that answer, which the parent's agent sees with its next message. The new
  topic works like any other. Codex forks with `thread/fork`, Claude with `--fork-session`; the
  parent's session is only read. E2e scenario 12 checks both agents: a fork from an idle topic,
  and a fork while the parent runs, whose turn completes as it would have.
- **`!vm` and `!restart` in a DM, even when the bridge is down** (#204, part 2: #573, #575; SPEC
  §4.6, §13.1). `zab service install` now also installs `zab-sentinel.service`, which runs
  `zab sentinel`: for each bot, its own Zulip queue for DMs. It answers the owner's `!vm` (the
  bridge's state: "bridge running, last Zulip event 40 seconds ago", "… no Zulip event processed
  for 7 minutes: send !restart", "bridge not running (failed)"), `!restart` (restarts
  `zab.service`, then confirms once it processes Zulip events again, within 2 minutes) and
  `!restart codex` (the Codex daemon, which also frees idle Lean servers' memory) in a one-to-one
  DM to either bot. Once the sentinel listens, the bridge stays silent on those two in DMs; it
  still answers DM `!vm` itself where the sentinel didn't receive the message, and a topic's
  `!vm` is the bridge's, with the turns and sign-in. While a rollout or a final sync holds the VM
  (idle stop's `/run/zab-idle-stop.hold`), both restarts are refused: `--lean-reclaim` and
  `--restart-codex` stop the bridge on purpose. A command the sentinel can't carry out gets a
  short "⚠️ … failed" reply. The sentinel's queue and reads never count as activity for idle
  stop. In e2e scenario 15's two runs, DM `!vm` got the sentinel's one reply in 3.1 s,
  `!restart` was confirmed in 46.5 to 58.7 s, with the bridge stopped too, and `!restart codex`
  in 3.2 s.
- **A `--tag` release installs from the public repository** (#593: #600), or from `ZAB_REPO`.
  The fleet installs a wheel, as before (`!rollout`'s), so nothing changes for it.

**Docs and publication** (#593)

- The license is MIT, Andrew V. Sutherland, with the package's metadata (#594). The README is
  for a public reader, with a `CONTRIBUTING.md` (#601). The documents, code comments and tests
  name machines and people by role (#598, #599, #602, #603, #608, #613, #614).
- BOOTSTRAP is a deployment-neutral runbook, with a new region end to end (#427, #529: #582);
  a second deployer machine, the secrets sealed between uses, and a secret replaced on a running
  server (#516, #406: #586); ADMIN's drift fixed, V5 closed, and a rebind by hand (#332, #397:
  #577).
- ORGANIZERS' examples roll out `v0.2.9`.

**The test harness**

- New admin scenarios and steps: A21 (`!grow-disk`); A9's step 6b (`!retire`, and the roster
  after a released address is drawn again); A2's revocation of an invitation (skipped while the
  organization lets only administrators invite); A13's `ModifyVolume` probes and its
  `AssociateAddress` of a fleet address onto a foreign interface (#415: #585); A6's full disk and
  A11's sizes (#555). New participant scenario 15 (the sentinel), and scenario 12's `!fork` parts.
- The harnesses: a VM the test waker started again is taken as it is; scenario 14 is in the fast
  group; the watchdog's bound is read from the installed unit; the admin runner confirms a
  server read `running` before using its address (#305, #493: #583).
- Flaky tests fixed: the login wait's fake tick (#545: #550), the fake claude's stop (#548:
  #551), a held turn's process, the logged ages, the 64-MiB re-pack bound and leaked slices (#590,
  #605, #609: #612). No tracked file holds an invisible character, and a test keeps it so (#388:
  #574); #351's limits are stated as accepted (ADMIN §2.7).

**Known limits**

- The sentinel's DM `!vm` has neither the running turns nor the sign-ins: a topic's `!vm` and DM
  `!status` still show them.
- IAM can't yet be shown to cap the size `ModifyVolume` grows a volume to (VERIFY V18, open): the
  server's code caps it at 1,000 GB, and only through an approved plan.
- `!retire` takes at most 3 rows with `export`.
- The roster import's revocation of an invitation (#322) is tested offline only: admin scenario
  A2 skips it while the Zulip organization lets only administrators invite.

## 0.2.8 — 2026-10-22

0.2.8 changes the admin bot alone: a plan that needs a typed phrase shows its approve line
(#544), and a command sent as code gets an answer and never runs (#547). It had no deployment of
its own: 0.2.10 includes it.

**Upgrade notes.** Nothing that the participants' VMs run changes: the release needs no rollout.
`admin.toml`'s shape, the role's policy and the units are unchanged, and the files host needs no
update.

**The admin bot**

- **A plan that needs a typed phrase shows its approve line** (#542: #544). Zulip's web client
  shows a message's widget instead of its text, and such a plan's widget had ❌ Reject alone. The
  screen showed that button and nothing else, and the line to send was only at the end of the
  folded plan (`!idle-stop 60 all`, Oct 21). Now its approval message has no widget, and every
  client, web, desktop and phone, shows this text:

  ````text
  P-<n> `<command>`: approve by sending this plan's approval line, exactly as it is. A plan like this one has no Approve button. Point at the line below and click its copy button, at its top right (or type the line), then send it as a new message.

  ```text
  !approve P-<n> <hash6> <phrase>
  ```

  It takes 1 approver. To reject, send `!reject P-<n>`.
  ````

  With two approvers, its last paragraph is
  ``It takes 2 distinct approvers: each one sends that line. To reject, send `!reject P-<n>`.``
  The web client gives the code block a copy button, which copies the bare line as plain text.
  A plan with no phrase is unchanged: ✅ Approve and ❌ Reject, and the same text for clients
  without widgets. Approving is unchanged too: the hash, the phrase, who may approve, and the
  message the plan is bound to.
  - ORGANIZERS §2.3 describes both kinds, and what to do when a pasted approval gets no 👀, and
    §8.1 says the same; ADMIN §2.4 and §2.5 give the reasons, from Zulip's source, and §7 the A1
    and A9 checks; NEW-WORKSHOP's approval step names the code box.
- **A command sent as code gets an answer, and never runs** (#546: #547). A copy and paste often
  sends a command as code: Zulip's compose box pastes a copied code span, or one line copied from
  a code block, as an inline code span, and a block copied with its newline as a fenced block.
  The grammar takes no such message as a command, so it got no 👀 and no answer. Now a message
  from a sender the bot recognizes, in a place it reads, that is, whole, one command as code (an
  inline code span, or a fenced block of one line with no info string, `text` or `Text only`,
  whose text starts with `!` and one of the bot's verbs) gets this, each time, with no 👀 and no
  audit record:

  ```text
  ❌ not run: your message is formatted as code, so it is not a command. Send it as plain text, starting with `!`: copy it with the code block's copy button (top right), or paste it and press Ctrl+Z (Cmd+Z on a Mac) once to undo the formatting. On a phone, type it.
  ```

  Nothing in it runs: the grammar is unchanged, and the message goes no further than the check.
  Other code and a command quoted in a sentence get no answer, and a stranger's message gets what
  it got before, never this answer.
  - ORGANIZERS §2.1, §2.3 and §8.1; ADMIN §2.1, §2.2, §2.5 and §7's A1.

**The test harness**

- Every plan that an admin scenario requests checks that its approval message shows the plan's
  exact approve line (#544). A1 checks that a plan with no phrase still has both buttons (#544),
  and the answer to a command sent as code (#547); A9, that a teardown's approval message has no
  widget (#544).

**Docs**

- ORGANIZERS' examples roll out `v0.2.8`.

## 0.2.7 — 2026-10-21

0.2.7's admin status reads no longer count as logins for idle stop (#514), the Oct 9 image's
`sage` gets the newer images' wrapper (#536), a message received as Zulip goes away is answered
(#533, #537), and keys stay out of tracebacks and Zulip posts (#525, #530, #531).

**Upgrade notes.** No unit is new, and Caddy's configuration is unchanged. A VM gets `zab-monitor`
at its next idle-stop step (`!idle-stop N`, or a rollout with `idle-stop=`); until then, each
status read of it is made as `ubuntu`, as before. The files host needs no update.

**For participants' VMs**

- **The admin server's status reads are not logins** (#507: #514). Idle stop used to count the
  admin bot's `!status` reads as logins, so a read that overlapped its once-a-minute check kept
  the VM up for another timeout. Now `status.py`, which the bot and the console's collector read
  through, reads as `zab-monitor`, and idle stop leaves exactly that user's sessions without a
  terminal (`zab-monitor@notty`) out of its logins. People's sessions count as before: `ubuntu@…`
  (deployers, the rollouts, `!final-sync`) and `agent@…`.
  - **A `zab-monitor` session is the read and nothing else.** Its login shell is the read, so
    every way in runs it, EC2 Instance Connect's keys included; its keys, copied by idle stop on
    every tick from `~ubuntu/.ssh/authorized_keys` (plain key lines only), are each forced to that
    command with `restrict`: no terminal, no forwarding. One sudo rule lets it run that root-owned
    script as `agent`, without arguments, and nothing else. An Instance Connect key may still
    forward ports and sockets, and use sftp where sshd serves it itself, as it may for `ubuntu`.
    Root on a VM (`ubuntu`, and `agent`, which has passwordless sudo) can still make such a
    session anything (DECISIONS, 2026-10-19).
  - **The fallback.** Where the user can't log in, or its read fails on the VM, the read is made
    as `ubuntu`, as before, and counts as a login. A timeout, or a VM that SSH can't reach, gets no
    second try.
  - On a test VM, the reads took a median 1.05 s, at most 1.64 s.
- **Sage on the Oct 9 image** (#482: #536). That image's `sage` is a plain link to the conda
  environment's, which leaves Sage's own programs off `PATH`, so Sage's `gap()`, `giac()` and
  `r()` failed there. Each release rollout now puts the newer images' 9-line wrapper in its place,
  which puts the environment's `bin` first on `PATH`, and only where the link is exactly
  `/home/agent/miniforge/envs/sage/bin/sage`, as the Oct 9 build made it. It first records the
  link in `~agent/.local/state/zab/sage-wrapper.json`; `sage_wrapper.py remove` puts it back
  (`workshop/admin/README.md` §6), and `!uninstall` leaves the wrapper. The Oct 9 tools list's SageMath line then
  says these work. On a VM from the Oct 9 image, with the environment's `bin` first on `PATH`,
  which is all the wrapper does, Giac, GAP, R, Singular and Maxima answered through Sage.
- **A message received as Zulip goes away is answered** (#532: #533). A first prompt in a new
  topic that reached the bridge just as Zulip became unreachable failed at its session's card, and
  was then dropped: claimed, but never answered, even after Zulip came back. Now a creation that
  fails because Zulip doesn't answer (a transport error, a 5xx, a 429) keeps its prompt, and the
  bridge creates the session after its next successful poll, before that poll's events. Any other
  failure gets its `⚠️` reply, as before. If the card's post reached Zulip but its answer was lost,
  the retry posts a second card; there is still one session and one answer.
- **Its follow-ups** (#534: #537, SPEC §7.2, §7.8.2):
  - when Zulip answers polls but keeps refusing a pending creation, the bridge retries it after
    1 s, doubling up to 60 s, without posting its pending answers again or logging the outage
    again; before, it retried every second, posting them each time;
  - a pending first prompt whose sender can't be checked, because the user list couldn't be read,
    is kept until it can be;
  - **a pending first prompt older than 2 hours now gets a `⚠️` instead of running late:**
    `This message could not start a session within 2 hours and has expired; send it again if it's
    still needed.`, linked to it, and is dropped, as the catch-up runs no request older than 2
    hours (SPEC §7.8.1). That holds at a start too, after a VM stayed off longer with such a prompt.
    Senders the bots don't take get no reply, as before.
- **Keys kept out of tracebacks** (#527: #531). `ZulipClient.from_zuliprc` names a malformed
  zuliprc as `<path>: <error type>` and never quotes its line, which configparser's errors do. On
  the VMs, only `zab sentinel`, which no unit runs yet, printed the line, in a traceback; the
  bridge (`zab run`), `zab init` and `zab doctor` already gave the type alone, and give the same
  text, except that a missing part now reads `no key in [api]` where they said `KeyError`.

**The admin server**

- **Keys kept out of Zulip posts:**
  - the admin scripts' `load_zuliprc` names a malformed zuliprc as `<path>: <error type>`, never
    its line (#523: #525). Before, a `!roster import` that changes an e-mail could be refused
    with a provisioner zuliprc's malformed key line in the refusal, which the bot posts (such as
    `key <32 characters>`, with no `=`, which the scrubber doesn't recognise); and
    `zulip_provision.py`, `provision_repos.py`, `new_workshop.py` and `move_bots.py` could print
    it in a traceback or their error line;
  - the scrubber learns a bot's key in every form configparser reads: `key:`, `KEY =`, any
    spacing, a value continued on indented lines, any section, and before the first header
    (#526: #530). Before, only `key=` lines. A continued value is read only in a file of at most
    4096 characters (a zuliprc has a few hundred), and a malformed file never fails the reload;
  - the bot's preflight, the wake checks and the waker give the same text as before for a
    malformed zuliprc, and `no key in [api]` for a missing part (#531);
  - `push_waker.py`'s direct mode, the files host's, names an AWS config that doesn't parse by its
    error type, never its line (#525).

**The test harness**

- A scenario's clean-up that fails is tried again (up to 3 tries, 15 s apart), and one that still
  fails is a run problem naming what it left on the VM, which stops the run where the later
  scenarios can't run with it; both runners keep `pytest.log` (mode 0600), and the admin runner
  sweeps it for the rig's secrets (#517: #518).
- A secrets file the harness can't read is named in a run problem, never quoted (#519: #522).
- An SSH connection that failed before its command ran (the connect or the banner) is tried
  again, 3 tries 5 s apart, within the caller's timeout, and each retry is in the report's notes
  (#517: #535).

**Docs**

- The image's runbook, now `workshop/ami/README.md`, "The images": the Oct 19 images, and which
  image `!new-workshop` takes: the region's newest `available` image with the fleet's scope tags,
  by creation date, refusing a tie; with a read-back to paste (#515: #521). ORGANIZERS §3.5: a
  workshop's image is the one its plan names.
- BOOTSTRAP §6.1 copies the current image into a new region, after checking that it is still its
  source region's newest (#524: #528).

**Known limits**

- Until a VM's idle-stop step, each read of it first gets one refused `zab-monitor` login, about a
  second, which its sshd logs as `Invalid user zab-monitor`, then reads as `ubuntu`, which counts
  as a login, as before (#514).
- The console's collector still skips VMs with idle stop in its own reads (ADMIN §4.1): reading
  them too is the rest of ADMIN §8.3's P-a, deferred.

## 0.2.6 — 2026-10-19

0.2.6 brings the admin server's console and cost estimate (#489), `!tools` and the agents'
"Software on this VM" section (#475, #486), the first-boot and wake-up cache warm-up (#464, #479,
#484), and the wider image (#474).

**Upgrade notes.** The deploy starts `zab-admin-collector` and `zab-console`, installed and dormant
since 0.2.4, rewrites Caddy's configuration (request buffering and read timeouts, below) and
restarts Caddy; the console is served at the server's `console_url`. A VM gets `!tools`, the
tools list, the instructions' tools section and `zab-warm-cache.service` at its next rollout, and
first runs the warm-up at its next boot or wake. The files host needs no update.

**For participants' VMs**

- After every boot, a waker's start included, `zab-warm-cache.service` reads into memory, in the
  background, what the agents are about to open: the agents' CLIs first (Claude Code and the Codex
  package, about 0.63 GB; #484), then the shared Mathlib and its toolchain, then, on a VM whose
  participant used Oscar in the last 7 days, the Julia files read or written since (#479). "Used"
  means Oscar's cache was read or written, a `Pkg` operation included; `warm_cache.py`'s docstring
  has the rule and its limits (`workshop/admin/README.md` §2.3). Its reads begin 10 s after the bridge is up, or after 2 minutes if it
  isn't. They run under cgroup caps of 80 MB/s and 1500 IOPS, and stop at half the memory
  available once the bridge is up.
  - On a test VM, one boot each (#464), a first `import Mathlib` took 109.7 s without it,
    84.6 s when started right after the start, and 10.1 s once the warm-up had finished, 179 s
    after boot (4.9 s warm).
  - A cold `using Oscar` took 30.3–32.8 s (#464, three boots); after a warm-up that read Oscar,
    15.8 s, as fast as warm (15.9 s; #479, one boot). With the CLIs and Oscar the warm-up read
    12.21 GB in 208.9 s (#484).
  - A release rollout installs and enables it, `!adopt-spare`'s included, without starting it, so
    each VM first runs it at its next boot or wake; `!uninstall` removes it (#461: #464; #475:
    #479; #483: #484).
- `!tools`, in a DM or a topic to either bot, lists the VM's software from `/etc/zab/tools.json`:
  by area, each tool with its version, the command to call it and a summary. The bridge answers it
  itself, never through a turn (3.1 s in e2e scenario 6); a missing or unreadable list gets one
  plain sentence (#475: #477). Images from #480 on carry their own list. On a VM without one, such
  as one from the Oct 9 image, each release rollout installs that image's list
  (`tools-nt-agents-2026-v3.json`, 22 tools) less any tool the VM lacks; it never replaces an
  image's own.
- The agents' instructions name exactly the tools of that list. Each release rollout replaces
  their "Mathematics" and "LaTeX" lists with a "Software on this VM" section made from it, with a
  standing rule: use the right tool unasked, prefer the listed call, say which tool gave a result,
  and run interpreters with stdin closed. Without a usable list the section says so and tells the
  agents to check that a tool exists first. The header names no field or dates. No string of the
  list can become a Claude Code `@` import: a call keeps an `@` only right after an ASCII letter,
  digit or `.`, or is left out, and in other text every `@` becomes U+FF20. That was checked live
  against Claude Code 2.1.302 and the fleet's 2.1.283 (#475: #478; #486: #488).
- The gift texts, in each sandbox's description, "Start here" and the quick-start's step 1, name
  ChatGPT Pro (#477). Existing "Start here" topics are not edited; descriptions are (*Known
  limits*, below).
- The bridge logs each `claude auth status` it runs: what asked for it (`!status`, `!vm` or
  `!login`), and its end, with its pid, how it ended, after how long and any refresh lock it left.
  A `zab doctor` run (a rollout's, or an admin `!status` read's) is a process of its own and logs
  none: at a lost race it shows only as a lock owner that isn't the bridge's. A turn logs its wait
  for an `auth status`. A lost OAuth refresh race logs the CLI's refresh lock: when it was touched;
  the pid its owner record names (alive, as which command; gone; or a zombie; and whether it is the
  bridge's own); how long before the lock that record was written, since a record can outlive its
  lock; and the legacy lock, if there is one. Only a command's name is logged (with `auth status`
  or `-p`), never the arguments that hold a turn's prompts; the lock and its owner record hold no
  credential. A turn still waits at most 15 s for an `auth status` (#483: #484). This doesn't fix
  #483's race (71.7 s): the new lines will show it if it recurs.
- Recovery (#306: #504). A Codex thread refused when its session reopens (one loaded with an
  unverified git identity) now puts only that session in `error`, and the rest of the reopen round
  goes on; before, the round stopped as if Codex were down. A message to a session in `error` is
  held and answered once, `⚠️ Error: this session needs recovery; use !status to retry.`, instead
  of an endless `⏳ Waiting for a free slot or backend.` `!status` in the topic also retries a
  session whose backend was down. A Codex turn that ends while its session reopens gets its own
  answer, not "Warning: stored turn no longer active.", unless another client ran two newer turns
  meanwhile (SPEC §8.4). The contract gains `SessionRefused` (additive; only Codex raises it).

**The admin server**

- **The console** (ADMIN §4, ORGANIZERS §3.9; #489: #490, #494, #495, #496, #497), at the server's
  `console_url`:
  - `zab-admin-collector` reads each workshop's VMs from EC2 every minute (state, launch time and
    type; termination protection at most every 10 minutes), the waker's status, the audit log, and
    the values of every `!status` the bots answer. It reads a running participant VM itself, every
    10 minutes, only while that VM's last read, made since its boot and since any apply of
    `!idle-stop`, `!rollout`, `!launch` or `!adopt-spare` naming it, showed idle stop off
    (ADMIN §4.1; why: #507, open). On a fleet with idle stop, each VM shows as its last `!status`
    read it, with the read's age. It writes a scrubbed snapshot and audit view, with no argument
    list, for the console.
  - A cost estimate per VM and per workshop, of the runs, the 200 GB volume and the IPv4 address:
    this month, projected to its end, and running hours today. Its prices are on-demand estimates
    for us-east-1, us-east-2 and us-west-2, kept as a constant (ADMIN §11.2; V13, against the
    bill, is open), and only the runs the collector saw count. While the collector runs, `!status`
    ends with the workshop's line, such as
    `cost (estimate): $412 this month, $1,050 projected · 9 of 40 running`.
  - `zab-console` serves the pages, refreshed every minute: the workshops; a workshop's totals,
    cost, open plans and one row per VM, problems first, with `!vm`'s bars; a VM's page, with the
    commands to copy into Zulip; the last 50 actions. VMs and roster rows appear by slug only,
    never with the roster's name or e-mail; the actions list names the Zulip users in each record:
    requesters, approvers, and anyone refused. A CSP allows only the console's own stylesheet and
    script, nothing inline. It is read-only: every change is still a command in Zulip.
  - Sign-in: `!console` sends a link by DM, valid for 5 minutes and once; the server keeps only its
    SHA-256. A session lasts 12 hours. Global admins see every workshop, a workshop's organizers
    theirs, and removal from the allowlists signs out at once. Each sign-in is audited
    (`console_login`), unless the bots restarted between the link and its use.
  - Caddy now buffers a request's body, up to its 16 KB limit, before passing it on, and gives its
    headers and its body 10 s each, so slow clients can't hold the console's 32 slots.
  - Admin e2e A11 checks that the console's page lists the workshop's VMs, and that a fresh `GET`
    through Caddy answers 200 while 40 clients trickle `POST` bodies (#497).
- The admin bots put 👀 on each command they take, at once, before its read or dry run (#466,
  #467). It is best effort,
  skipped while the request budget is down to its reserved third, so a missing 👀 alone is no
  reason to send a change again.
- `!final-sync` holds idle stop off on each VM while it syncs (#370, #462), best effort: a hold
  the VM refuses lets the sync run without it, and a VM whose hold connection fails isn't synced.
  The hold is released once the sync has ended on the VM, and left to lapse within the hour when
  the connection was cut, since the sync goes on there. It covers the sync alone, so
  ORGANIZERS §5.2's `!idle-stop 0` for the rows that end stays.
- `!adopt-spare` refuses at plan time a spare that isn't running, naming `!start`, or whose SSH
  doesn't answer yet; the apply checks again before it retags the spare (#455, #463).
- `power.py --wait`, which the month-end and manual runs use (no bot command passes it), waits for
  all its VMs under one deadline, 10 minutes after the last request plus one round: about
  11 minutes for up to 128 VMs whose SSH is silent, within the runner's 15. A VM that fails the
  wait counts as failed only (#334, #503). Every `power.py` call, the bots' `!start` and `!stop`
  included, now gives an EC2 request 2 tries of at most 10 s to connect and 20 s to read, so a
  throttled start fails that VM after one retry: send it again.
- A teardown that goes stale names the rows whose state changed, as in `since the plan: alice:
  synced after its uninstall → not uninstalled, not synced`: at most 20 changes in all, then
  `and N more` (#376, #502). ORGANIZERS §5.3: a row that was never launched needs `unsynced`, so
  tear it down on its own.
- The admin bot (#347: #505, #506) warns, in the topic `membership`, about any of its channels
  that isn't private (at start and hourly), and changes nothing. It backs off after errors, from
  1 s up to 2 minutes; answers a `!roster show` of a row removed meanwhile; remembers a failed user
  lookup for a minute, instead of asking again at each message; no longer lets a second signal cut
  its shutdown flush; and gives a refused `!roster import` of another workshop's slug its own hint,
  at most once per sender every 10 minutes.
- The runner, the wake monitor and the starter wait with `asyncio.timeout`, so their stops are
  never lost on Python 3.11 (#491, #500); the waker's and the bridge's `wait_for` remain (#498,
  open). The server runs 3.12, where `wait_for` already behaved; the one change there is that a
  step started after its deadline whose script has already exited counts as exited, not timed out.
- `deploy.sh --review` ends with the commands to sign, verify and push the release's tag, which
  paste as they are in any directory, or with `No tag command:` and the reason (#468, #469).
  v0.2.5's copy, which reviews this release, still prints its placeholder.

**The image** (not in the wheel; `workshop/ami/README.md`, DECISIONS D34, D35)

- The image built at this release passed all 59 tools' checks; on a fresh instance its first-boot
  warm-up took 894 s, `SMOKE-RESULT 0` held before and after a reboot, and it used 36 GB (#512).
  A VM from an older image gets only what the rollout brings.
- The build writes `/etc/zab/tools.json` from `tools.toml`, only once every tool has passed a real
  computation, and `build_ami.py --smoke` checks the list again on a fresh instance (#474: #480).
  `!tools` and the instructions read it.
- The wider stack (#474: #481): Singular, polymake, Maxima, R (with tidyverse, data.table,
  rmarkdown, knitr and IRkernel), Octave, Macaulay2 1.26.06 with gfan and msolve from a fixed PPA
  snapshot and its other helpers from Ubuntu's archive, atlas, Z3, CaDiCaL, pandoc, Graphviz,
  gnuplot and ImageMagick; 150 hash-pinned Python packages, among them SciPy, pandas, NetworkX,
  scikit-learn, cvxpy, OR-Tools, SnapPy and Regina; HomotopyContinuation.jl 2.22.4 in Oscar's
  environment, whose listed call runs as written (#486: #488). The first-boot warm-up reads about
  1.9 GB more. `sage` is a wrapper that puts its environment on `PATH`, so Sage's `gap()`,
  `giac()` and `r()` work (#480); VMs from the Oct 9 image keep the plain link (#482, which 0.2.7
  fixes).

**CI and tests**

- The offline tests run in parallel (pytest-xdist): a pull request's jobs took 5:28–6:20, against
  10:10–13:38 before (#473: #476). Five offline tests of several seconds are `slow` and run on
  pushes to `main` only. CI installs `age`, so the backup tests run. Long parametrized ids are
  abbreviated: printing them whole hung four CI runs.
- The CPU-ratio tests (the export re-pack, the scrubber's twice-main) run alone in new
  `timing (3.x)` jobs, against references timed in the same run, and skip under xdist (#485:
  #492; #508, in which twice-main re-measures once before it fails).
- Every offline test has a timeout (120 s, 300 s for `slow`; pytest-timeout), and a backstop ends
  a worker whose teardown hangs, so a hang fails by name, with its asyncio tasks' chains printed.
  ruff bans `asyncio.wait_for` in the admin server, `src/zab/testing` and the admin tests' fake
  Zulip (#491: #500).
- Four test races are fixed: a `/proc` read (#501); a fake `claude` rewritten under a running one,
  and a command line read before its `execve` ended (#509); and a pidfile written before the output
  (#511).

**Docs**

- ORGANIZERS §3.9, the console: signing in, what each page shows, how fresh it is, and why not to
  refresh it with routine `!status all` (#497; why: #507, open). BOOTSTRAP §12 checks it from the
  deployer's machine.
- The files host's update runbook: step 0's blocks inline and in order, each with what it prints;
  the selftests push over SSH (#470: #472).
- BOOTSTRAP §9 and the adminserver README quote the review's tag commands (#469).

**Known limits**

- A full `!provision zulip` rewrites every sandbox's description, so the next full run in a
  workshop changes its "ChatGPT gifts" to "ChatGPT Pro gifts" (#477).
- Each admin `!status` read of a running VM is an SSH session, which idle stop counts as a login
  (#507, which 0.2.7 fixes): don't refresh the console with routine `!status all` (ORGANIZERS
  §3.9).

## 0.2.5 — 2026-10-18

**For participants' VMs**

- A VM that idle stop powered off answers the message that woke it much sooner. On a test VM a
  waking DM was answered in 15.5 s and a waking mention in 19.6 s; 0.2.4 took 78–83 s (e2e
  scenario 10). The bridge starts its listener and answers the catch-up first; it confirms its
  sessions' cards and reconciles their topics afterwards, in the background, at the lowest
  priority, and only while a third of the per-minute request budget stays free for answers. A
  session's card is confirmed before anything is posted in its topic. Start-up no longer marks
  every session as recently used. A message sent while Zulip was unreachable is still answered
  after the event queue is lost (#443, #447).
  - Channel privacy is now cached for at most two minutes, less when Zulip reports a change to the
    channel (a VERIFY item, SPEC §4.10), so a session in a channel just made public can take that
    long to become read-only.
- Claude's first answer in a new topic no longer waits about a minute after `!status` or `!vm`
  (e2e scenario 1: 6.2 s, was 73–76 s). Those commands stop waiting for the bridge's own
  `claude auth status` after 2 s; that used to kill it, mid-refresh if one was running, which could leave Claude's
  refresh lock behind for a minute. Now it runs to its end, and at its own 30 s limit gets SIGTERM,
  which lets the CLI remove the lock, 10 s before any SIGKILL. The bridge starts no `auth status`
  while a turn of the same account is starting, and a starting turn waits up to 15 s for one that
  runs. A lost refresh race is retried on a schedule that outlasts a stale lock; when every retry
  loses, the answer is one plain sentence (#450, #451; #307).
- `!vm` and `!usage` give CPU use in CPUs: `sage 3.2 GB memory, 2.1 CPUs`, `1.4 of 8 CPUs`
  (#441, #444).
- Links to source files on the file server keep their line (`check.gp:42` opens at line 42; a range
  opens at its first line), and the agents' instructions say that files there render, with
  highlighting for the languages listed (#448, #449). **Rollout note:** update the files host
  (below) before rolling this release out, or the instruction is false.
- quickstart: "Your VM sleeps when idle" (its 15–20 s needs a fleet on 0.2.5); the Oct 9 sentence
  about agent commits is gone (#440, #454).

**The admin server**

- `!new-workshop` on the server's bot creates a workshop: its bot, its two channels, its key pair,
  its directories and its `admin.toml` entry, through a root helper that validates the whole new
  file; two global admins approve it (#418: #421, #423, #424, #428). The helper runs the system
  Python with a minimal import closure, can't rewrite the nftables or trust files, and sees neither
  the rest of `/etc/zab-admin/secrets/` (only `bots/`) nor `/var/lib/zab-admin`, with the fleet key
  and the participants' keys (#437, #438).
- The server's role is scoped by one fleet-wide tag, `zab-scope`, instead of by workshop names, so a
  new workshop in a region already set up needs no IAM change (#422: #425, #426).
  - **Upgrade note:** v0.2.5 refuses v0.2.4's `admin.toml` (`[global]` needs `scope`, `regions` and
    `instance_types`; a workshop takes no `tag`), and the deploy's self-check loads it, so a plain
    upgrade fails. Migrate in this order, while no plan is pending: first, the account owner tags
    `zab-scope=<scope>` on every workshop of the v0.2.4 `admin.toml` (its instances, spares
    included, their volumes and Elastic IPs, and its key pair; never the workshop's own image or
    group, since a second scoped group in a region breaks `!new-workshop`'s lookup, ADMIN §2.9),
    with their own profile, since the role can't, and reads it back; v0.2.4 ignores the extra tag.
    Then deploy v0.2.5, installing `admin.toml` in v0.2.5's shape (ADMIN §2.1) at its prompt:
    each region set up first (BOOTSTRAP §6.1), and each entry's `ami` and `security_group` the
    region's scoped ones. Then replace the role's policies with v0.2.5's render (ADMIN §5). The
    tags come first: v0.2.5 finds a VM only by every scope tag, and its policy acts only on what
    carries `zab-scope`. Review the release with v0.2.5's own `deploy.sh --review … --since
    v0.2.4`: v0.2.4's copy prints another SHA-256.
- `!status` answers with the plans' table and a few readable lines, never raw JSON, and reads a
  stopped VM from EC2 alone (3.3 s in admin e2e A7); the server's `!help` mentions SEL only where
  it applies (#439, #453).
- `!adopt-spare` turns the waker on for the late joiner when the fleet's waker is on (#440, #454),
  and no longer stops at its fifth step because the row's checkout is cloned only at the next one
  (#458, #459). Admin e2e A20 runs a late joiner's adoption end to end (#456, #457).
- Releases: `release-paths` lists only what the server runs, `privileged-paths` the files a
  signature certifies were read (DECISIONS D31, D32), and `deploy.sh --review` shows a per-file
  summary, then the privileged diffs, newly privileged files whole once, and `uv.lock` and the
  build constraints as packages and pins. `--wheel` builds from a minimal tree with the pinned
  backend only, and refuses build hooks, `hatch.toml` and `[tool.uv]` (#432, #436, #452).

**The files host**

- A safe update runbook: the new release is staged beside the running service and tested through a
  tunnel, before a switch and a rollback that are each one `&&` chain of renames; and an
  open-and-close block for its SSH (#442, #445). GAP and Macaulay2 are highlighted (#449).

**Docs**

- BOOTSTRAP sets up the server and its first workshop only; later workshops come from
  `!new-workshop`. Each region is set up once (§6.1: `zab-fleet` and its image). ADMIN §8.4's
  cutover of a fleet launched before its server is a two-stage canary (#417, #420).
- BOOTSTRAP: a dedicated deployer key for every key role, never a personal key; the key swap
  through EC2 Instance Connect; the shared-address check; fixes from the first production
  bootstrap, on Oct 17 (#429, #430, #431, #433, #434, #452).
- NEW-WORKSHOP: the global admins' runbook for `!new-workshop`, including what its restart does to
  the other workshops (#424, #428).
- ORGANIZERS: idle stop and the waker from launch day, for every workshop; `lean-shared` at launch;
  the daily break routine; the end of a workshop on the rows that end only (#440, #454).

## 0.2.4 — 2026-10-16

**For participants' VMs**

- Claude steering is on by default: a message sent while a Claude turn runs goes into that
  turn (↪️), as with Codex. `[backends.claude] steer = false` turns it off; then such
  messages are held (⏳) for the next turn, as before. `push_config.py` writes `steer = true`
  unless given the new `--no-claude-steer`, so a rollout no longer turns steering off when
  `--claude-steer` is forgotten; `--claude-steer` is still accepted and changes nothing. Its
  summary line always says `claude steer: ON` or `OFF` (#310, #318).
- `push_config.py` waits up to 60 s, not 15 s, for the Codex daemon to answer after
  `zab service install`, before it reports `codex:connection: daemon unavailable` (#386). Its
  `--dry-run` prints one line per VM naming each remote command and file; `--dry-run
  --verbose` prints the old full output (#380).
- `zab doctor`'s allowlist check resolves `allow_emails` as the bridge does, so it passes exactly
  when the bridge would admit anyone (the VM's participant); otherwise it says why (not joined
  yet, or the address is hidden from members). A rollout to a VM whose participant hasn't joined
  Zulip yet reads `ok (owner+sign-in pending)` and exits 0, instead of failing, and the admin
  server's `status.py` reads `active, owner+sign-in pending` for it (#401, #402).
- `zab status --json` reports `last_activity`, the newest message, held message or turn (idle
  stop's measure); the admin server's `status.py --json` shows it in UTC for VMs on this
  release, and null for 0.2.3 or older (#320).

**The admin server** (`docs/ADMIN.md`; for organizers, `docs/ORGANIZERS.md`, #390)

- All administration of a workshop goes through its own Zulip admin bot, with plans,
  approvals and a hash-chained audit log, on a server in the workshop's account that holds
  the keys; the coding agents can't reach it (#267). In order of the stages of ADMIN §8.1:
  - **the core:** the `admin.toml` loader, one bot per workshop and a global bot (#321); the
    command grammar (#323); each command's argument lists and plan hash (#333); plans and
    approvals, bound to the bot, with typed phrases, two approvers for destructive actions and
    a veto (#339); the bots' queues, routing, strangers and startup checks (#344); plans with
    `zform` approval buttons (`✅ Approve`, `❌ Reject`) and `!status` (#346, #366); `!console`
    answers that the console isn't built yet (stage 5);
    the runner, which applies approved plans with scrubbed progress and a lock per workshop
    (#353, #354); compact plans with a status table (#380); the audit log's hash chain and the
    output scrubber (#319, #349);
  - **the scripts it runs:** `roster_import.py` (#317); `launch.py` and `teardown.py` select
    only tagged instances, with `--only` and `--spares-file` (#316); `power.py` start and stop
    (#328); `status.py --json` and `--tag`, and `--no-rebind` for provisioning (#320);
    `protect.py` reads EC2 back, `teardown.py` retries terminations EC2 refuses, and no
    `!teardown` is planned within 5 minutes of a `!protect` (#382);
  - **the server:** its users, units, IMDS rule and role policy (#329); `deploy.sh`, which
    installs only a signed release tag after showing the reviewed diff, with hashed wheels and
    a self-check (#338); encrypted backups and recovery (#373);
  - **the end of a workshop:** `!uninstall`, `!final-sync`, `!export`, `!teardown` and
    `!deactivate-bots`, in that order: a sync counts only after the uninstall, so no agent adds
    unsynced work, and destructive steps need two approvers (#360, #367). Exports are re-packed
    into regular files and directories in a sandboxed, socket-activated unit with no network
    and no keys; symlinks are listed in `SYMLINKS.txt`, and names Windows can't take are
    renamed and listed in `RENAMED.txt` (#383, #384);
  - **the waker on the server:** it starts VMs only through `zab-admin-starter`, a start-only
    helper that checks each request (#343, #350); `!waker on|off` and each workshop's alerts
    channel (#362); the global admins are alerted when a VM is woken with no matching Zulip
    message, or when the waker starts a VM that isn't opted in (#391);
  - **moving a fleet launched before its server onto it:** `stage_server.py`, `join_server.py`
    and `move_bots.py`, and the cutover runbook, ADMIN §8.4 (#364, #365).
- The server's role can no longer launch an instance that attaches an existing network
  interface. A new deny on `network-interface/eni-*` for `RunInstances` keeps a compromised
  server from taking another service's private address and security groups. The interface a
  launch creates is unaffected. A13 probes it (#356, #414).
- The provisioner is a Zulip **Moderator** (`zab-provisioner`): it binds roster rows to users by
  e-mail wherever a user's visibility includes moderators; a Member can't see hidden addresses
  (VERIFY V5, #397). Two limits remain (ORGANIZERS §3.1, §3.3): a participant whose
  visibility is "Admins only" or "Nobody" can't be bound, and `!roster import` refuses e-mail
  changes and removals while any human's address is hidden that way. `move_bots.py` finds the
  provisioner by its user ID, never by e-mail, and `zulip_provision.py --deactivate` finds a bot
  that Zulip's bulk user listing leaves out by its own e-mail (#396, #398).
- The waker's starter client reads a refusal the starter sent before closing, instead of raising
  a broken pipe (#387, #399).
- The waker no longer misses a message sent while it restarts (each `!waker on|off`, a deploy, a
  reboot) or while Zulip has dropped its queue: after each registration it catches up on each
  bot's messages since the last one it handled, at most 100 and 5 minutes back, through the same
  checks as live ones, best effort (4 tries). A message sent well before (31 s) the waker last
  saw the VM up wakes nothing, nor does one sent while the bot wasn't opted in (#403, #405).
  - **Rollout note:** on the first start of this version no bot has a mark yet, so each catches
    up on its last 5 minutes (since the config's write, if that is later). A VM idle-stopped in
    those minutes, after its bridge answered a command such as `!status`, can be woken once for
    it; idle stop takes it down again (ADMIN §8.4).
- `docs/BOOTSTRAP.md`: the ordered checklist for bootstrapping a production admin server, from
  the laptop's signing key and the server's AWS resources to `deploy.sh --bootstrap`, with a
  refresh of the SSH rule from the laptop's current address and a read-back of the group's rules
  (#404, #407).
- The admin end-to-end suite on a test rig, `tests/e2e/admin/`: A1–A10, A12–A15, A18 and A19
  (#363, #368, #377, #381, #385, #392). A8 and A18 wait for the bridge's `!status` answer after
  the wake, not its `!login` hint, which a VM just booted may leave out (#408).

## 0.2.3 — 2026-10-15

- A turn begun while the bridge is starting is no longer ended as "⏹ interrupted by bridge
  restart" (Claude) or "Warning: stored turn no longer active." (Codex), with its answer
  dropped. That hit the first message of a new topic sent while a VM was booting, for example
  the one that woke it, or during a rollout's restart. Each session now takes its startup and
  reconnect recovery snapshot in its own queue, so a turn it has already begun is in that
  snapshot (#303).
- A Claude turn that loses the CLI's OAuth refresh race ("another Claude Code process is
  refreshing it"), as a VM's first turns can after a long stop, is retried automatically, up to
  twice and 20–30 s apart, when it hasn't produced anything yet. It is no longer reported as
  "This agent isn't connected to your Claude subscription yet: send me !login …"; if every try
  loses, the message says to send it again in a minute (#302).
- Waker: every owner message in a channel that waited for the wake gets its own ⏰, not just
  the newest. The ⏰ comes from the bot the message mentions; an unmentioned one gets the bot of
  the latest earlier mention in its topic among the messages of the same wake, else the first
  configured bot (Codex). DMs still get one "Starting your VM" notice per wake. httpx logs only
  warnings, not every long poll. Known limitation: an unmentioned message in a topic where
  Claude has a session, with no mention of Claude in that topic among the messages of the same
  wake, gets its ⏰ from Codex, although Claude answers it; the waker can't see sessions
  (#301).

## 0.2.2 — 2026-10-14

- `zab history`: agents read other topics and earlier conversations read-only, as their own
  bot (`ZAB_BOT` in both backends' shells); the instructions send them there and away from
  the bridge's zuliprc files (#248).
- Lean: one shared, read-only Mathlib per VM. `lean-project` links projects to it in seconds
  and megabytes; `push_config --lean-shared` converts a VM, `--lean-reclaim report|apply`
  frees private copies; new topics link committed projects through a worktree hook
  (`[bridge] worktree_post_create`, #205). The agent instructions also tell Codex to stop its
  own idle Lean servers (#249; zab itself doesn't enforce this yet).
- `!vm`: one line per resource (CPU, memory and disk; sizes in GB), each with a
  green-to-red bar, then a status line (turns, uptime, sign-ins, version). `!usage`: its limit
  bars use the same green-to-red function, and its VM line reads
  `VM: CPU …% · memory …% · disk …%` (#204).
- Claude commits carry exactly one author, the bot, without a Co-Authored-By trailer (#214).
- `!login` sent while a backend is still starting waits for it instead of failing.
- After a Zulip outage longer than the event queue's lifetime, the running bridge posts the
  answers it couldn't deliver and answers the messages sent during the outage, without a
  restart. A failed status refresh no longer puts the session in `error`, a status message
  never shows a raw state word, and a turn finished during startup is settled once.
- Idle stop: the VM stays up for a missing database, a Codex turn in the daemon, a rollout
  hold or a recent login; a turn's age never stops it, and a detached `running` row over
  24 hours with no recorded process is named as possibly stale in the verdict; the minimum
  is 10 minutes (#236).
- Fork groundwork: `WorkspaceManager.fork_base` and Claude's `fork_session` (#202); the
  `!fork` command itself is not in this release.
- The central waker takes its workshop tag from its config (`push_waker --workshop`).
- The central waker can start VMs in another AWS account through an assumed role
  (`push_waker --assume-role ARN`, undone by `--no-assume-role`); `push_waker --check` shows
  the identity and dry-runs a start per opted-in VM (#207).
- File server, deployed separately on the files host (#256): a browser gets rendered Markdown
  with KaTeX math and highlighted source with `#L42` line anchors; `?raw=1` returns the
  file's bytes.
- Workshop tooling: `build_ami.py --tag`, a repeatable `--tag` and `--ssh-from` for
  `launch.py`, and the end-to-end suite (`tests/e2e`, #263).

## 0.2.1 — 2026-10-12

- Fix Codex session start/resume failing with "Value after * must be an iterable,
  not NoneType" when the Codex config sets no shell-environment exclusions.

## 0.2.0 — 2026-10-12

- Startup catch-up for new topics and DMs, with expiry and addressing checks.
- The wake engine, central waker configuration/service/deployment, and idle-stop tooling.
- Listener liveness watchdog, stale-listener diagnostics, VM commands and usage reports.
- Per-bot commit identities, status display, and the idle Codex-daemon migration restart.
- Disk warnings, updated workshop instructions and the larger default disk.
