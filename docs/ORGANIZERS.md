# Organizers' runbook: a workshop through the admin bot

**Who this is for.** You lead a workshop's organizers. You use Zulip every day, and you don't need AWS, SSH or the server. Everything here is a message to your workshop's admin bot, in your organizers channel. For every change, the bot first shows a plan of what it would do. Nothing happens until an organizer approves that plan.

**Who else is involved.** A *deployer* runs the admin server, which [`docs/BOOTSTRAP.md`](BOOTSTRAP.md) sets up. You need the deployer before the workshop (§1.1), and when the bot can't do something (§7, §8). The participants' side is [`docs/quickstart.md`](quickstart.md). Every rule below comes from the design, [`docs/ADMIN.md`](ADMIN.md). [`workshop/admin/README.md`](../workshop/admin/README.md) and [`workshop/aws/README.md`](../workshop/aws/README.md) have the same steps run by hand, which is the fallback (§7).

**The examples** use a made-up workshop, `harvard-2026`: read your own workshop's name wherever it appears, as in its bot `Workshop admin · harvard-2026` and its channels `#harvard-2026-organizers` and `#harvard-2026-fleet-alerts`; a rule that names no example writes it `<ws>`. They also use the participants `alice`, `bob`, `carol` and `dave`; another workshop on the same server, `zab-test`, with its participant `erin`; the organizer Olga Organizer and the stranger Pat Participant; Alice Smith and a few others for the slug rules (§3.1); the release `v0.2.15`; `<org>` for your workshop's GitHub organization; and `<host>` for your server's console. The plan IDs (`P-42`) and hashes (`3f9a2c`) are made up: always copy yours from the bot.

**The console,** a read-only web page of the fleet (ADMIN §4), shows your workshop's VMs at a glance: send `!console` for a sign-in link (§3.9). `!status` (§3.6) reads the VMs afresh, and brings the console up to date.

## At a glance

The days count from the workshop's day, **W**. For a workshop of several days, W−2 counts from its first day, and W+1 from its last.

- **Before launch day:** the deployer's setup, started weeks ahead for the quotas (§1.1); your invitations to Zulip (§1.5); then the roster, from those who have joined (§3.1).
- **Launch day, W−2,** in the afternoon; the morning of W−1 is the fallback: the VMs, bots, sandboxes and repositories are made, and the sandboxes held until W (§3).
- **W:** the participants sign in and work (§3.8, §4).
- **The end, W+1:** the VMs of everyone who didn't opt in to keep theirs end (§5).
- **The kept month, to W+1 month:** each kept VM runs, sleeping when idle, until it ends one month after the workshop (§5.7).

Every workshop runs idle stop and the waker from launch day to its end, and the kept VMs keep both for their month: they keep the budget (§4.2; ADMIN §11.2).

| When | Send | Approval |
|---|---|---|
| Before launch day | `!roster sync`, or `!roster import` with the CSV attached (§3.1) | 1 approver |
| Launch day, W−2 | `!launch all spares=2` (§3.2) | 1 |
| | `!provision zulip hold all` (§3.3) | 1 |
| | `!provision repos all` (§3.4) | 1 |
| | `!waker on all` (§3.5) | 1 |
| | `!rollout v0.2.15 all` (§3.5) | 1, and the phrase `rollout <n>` |
| | `!status all` (§3.6) | none: it only reads |
| | `!stop all` (§3.7): the spares stop by themselves | 1, and the phrase `stop <n>` |
| W, the first morning | `!start all`, then `!provision zulip all`, then `!waker on all` (§3.8); the spares stay stopped | 1 each |
| During | at each day's breaks: `!provision repos invites-only all`, then `!provision zulip all`, then `!waker on all` (§3.8); the fixes of §4 | 1 each |
| W, the last day | the kept VMs' opt-ins, then `!protect on carol`, naming the kept rows (§5.2) | 1 |
| The end, W+1 | `!retire alice bob`, naming the rows that end; `!retire alice export` for those who asked for an archive (§5) | 2 different people, or 1 global admin, and the phrase `retire <n>` |
| The kept month's end, W+1 month | `!retire carol`, for each kept row (§5.7) | the same |

## 1. Before the workshop

### 1.1 What the deployer sets up

Ask the deployer to confirm each item. You can check the first one yourself.

1. **The admin server runs a signed release** (ADMIN §1.6). Check it yourself: `!version` names the release, then your workshop, its region and how many organizers it has.
2. **Your workshop's entry** in the server's configuration (ADMIN §2.1). It holds the region, the machine image and security group, the instance type, the GitHub organization (`<org>`) and the file server. It also lists the organizers by Zulip user ID, and the participants' channel, if the workshop has one (§3.1). The server's bootstrap makes the first workshop's entry by hand ([`docs/BOOTSTRAP.md`](BOOTSTRAP.md)); every later workshop's comes from a global admin's `!new-workshop`, in `#zab-admins` (ADMIN §2.9; their runbook is [`docs/NEW-WORKSHOP.md`](NEW-WORKSHOP.md)). After that, only a deployer changes it, so ask them to add or remove an organizer.
   It also sets **when a participant's bot mentions them,** `mention_after_s` (120 seconds unless the entry says otherwise): when a turn fails, needs an approval, or ends after running that long, so a topic that waits on a participant after such a turn shows **@** in Zulip's left sidebar, and at 0 every turn that completes or fails mentions them. Two endings never do: a turn that a bridge restart ended, and a failed sign-in, which gets the `!login` guidance instead. The next `!rollout` writes it to the VMs (ADMIN §2.1).
3. **Your bot and your channels,** made with the entry. The bot is `Workshop admin · harvard-2026`. There are two private channels, `#harvard-2026-organizers` and `#harvard-2026-fleet-alerts` (§1.2).
4. **AWS access for your workshop.** The account owner has set up your region once: a copy of the machine image and a security group. The server's AWS permissions cover your workshop, and its SSH key is registered there.
5. **The release to roll out.** The deployer places it on the server with `deploy.sh --wheel`. Without it, `!rollout` is refused with `no wheel for v0.2.15 on this server: deploy.sh --wheel first`.
6. **The quotas** in your region must cover the fleet and its spares (ADMIN §11.1): the vCPUs, where each `m7a.2xlarge` VM counts 8, and the Elastic IPs, one for every VM, running or stopped ([`workshop/aws/README.md`](../workshop/aws/README.md), "The VMs"). An increase can take days: ask early.
7. **Zulip.**
   - Before any invitation goes out, the default for *Who can access user's email address* must let members read addresses: *Admins, moderators and members*, or *Everyone* ([`workshop/admin/README.md`](../workshop/admin/README.md) §1; BOOTSTRAP §5.2). A participant's own bots are members and recognize them by their address, so under a stricter default a participant who joins late is ignored by their own bots.
   - The `zab-provisioner` account must exist, as a Moderator. It creates the participants' bots and binds each row to its participant's Zulip account by e-mail (§3.3).
   - **Optional: Enter sends.** Zulip's default makes Enter start a new line rather than send. For a new workshop, *Press Enter to send* in *Organization settings → Default user settings* changes that for the users created after it, so set it before the invitations go out.
   - All three are organization settings, which only the Zulip organization's owners and administrators change.
8. **The GitHub token** must not expire before the kept month ends, at W+1 month (ADMIN §5). A deployer renews it on the running server (BOOTSTRAP §16).
9. **Backups.** After every change, each deployer gets an encrypted backup of the server's state by DM (ADMIN §1.7). Organizers get none and have nothing to do.

The deployer's own instructions are [`docs/BOOTSTRAP.md`](BOOTSTRAP.md) (the server's bootstrap; a new region, §6.5; a server that may be compromised, §13.3; a second machine, §15; replacing a secret, §16; a lost or suspected key, §17), ADMIN §1.6–§1.7 and [`workshop/adminserver/README.md`](../workshop/adminserver/README.md) ("Deploying", "Backups and recovery").

### 1.2 Your channels, and who approves

- **`#harvard-2026-organizers`** is private. The bot takes commands here, in any topic.
  - **Members:** only the workshop's organizers, the global admins and the bot.
  - **The bot checks the members** when it starts and every hour. If anyone else is subscribed, it posts in the topic `membership`: `⚠ subscribed here, but not recognized by this bot: …. Only organizers, global admins and this bot belong in this channel; nothing was changed.`. It changes nothing, so have a Zulip administrator remove that person.
  - **It also checks that the channel is private.** If it isn't, it posts in the same topic: ``⚠ this channel is not private: every member of the organization can read it, plans and `!roster show` with their e-mail addresses included. Make it private in its settings; nothing was changed.``. Have a Zulip administrator make it private.
  - **Never subscribe** a helper, a participant or `zab-provisioner`.
- **`#harvard-2026-fleet-alerts`** is private and takes no commands. When the waker (§4.2) can't start a VM, it says so here, through that participant's own bot. Its members are the organizers, the global admins and `zab-provisioner`, which owns the participants' bots and so lets them post there (ADMIN §2.9).
  - **Any participant's agent can post there too,** through its bot's key ([SPEC](SPEC.md) §12.2; ADMIN §1.5). So never copy a command or click a button from that channel: it only tells you that a wake failed.
- **`#zab-admins`** is the global admins' channel, with the server's own bot `Workshop admin`. A workshop doesn't need it.

**Who may approve** (ADMIN §2.5): your workshop's organizers and the global admins: the deployer and, usually, the lead organizers (ADMIN §2.1). Helpers who answer `#help` are on no list.

- You may approve your own request. A plan that needs two approvers counts you as one of them.
- **A global admin's approval alone completes** a plan that needs two approvers (ADMIN §2.5). An organizer who is not a global admin still needs a second, distinct approver: another organizer or a global admin. The typed phrase is needed all the same: it guards against a misclick.
- In the table, *the fleet* means the rows that have a VM. `<n>` is the number of targets the plan names, spares included.

| Plan | Approvers | Typed phrase after the hash |
|---|---|---|
| any other change | 1 | none |
| `!stop`, `!rollout`, `!idle-stop` or `!uninstall` of more than half the fleet | 1 | `<verb> <n>`, such as `stop 35` |
| `!retire`, `!teardown` or `!deactivate-bots` | 2 different people, or 1 global admin | `retire <n>`, `retire <n> unsynced`, `teardown <n>`, `teardown <n> unsynced` or `deactivate-bots <n>` |

**On the days of `!teardown` and `!deactivate-bots`,** make sure a global admin, or a second organizer, will be online.

### 1.3 First contact with the bot

Send these in a topic such as `bot check`. Each answer ends with the audit footer `audit #… · chain …` (§6.4).

- **`!help`** answers `**harvard-2026's bot** takes:`, then every command. `!help teardown` shows one command.
- **`!version`** names the release and your workshop (§1.1).
- **`!roster show`** answers ``The roster has no participants yet: `!roster import`, with the CSV attached.``, or, for a workshop with a participants' channel, ``The roster has no participants yet: `!roster sync`.``
- **`!plans`** answers `No plan is pending on this bot.`

In a direct message to the bot, only `!help` and `!console` work. Anything else is answered `❌ only !help and !console work in a direct message; use the channel`.

### 1.4 Times are UTC

The bot writes every time in UTC: a plan's expiry, and the time from which a `!teardown` may follow a `!protect` (§5.4). Convert them to your local time, and check whether a daylight-saving change falls between W−2 and W+1: in the United States on the second Sunday of March and the first Sunday of November, in the European Union on the last Sundays of March and October, and elsewhere by your own country's rules. Your offset from UTC changes with it.

### 1.5 Invitations before the workshop

You invite the participants to Zulip yourselves, in Zulip. The bot sends no invitations (#628).

- **Who may invite:** only the organization's administrators, unless one of them widens *Who can send email invitations to new users* (*Organization settings → Organization permissions*). An organizer who isn't one asks a Zulip administrator to send the invitations.
- **Send each invitation by e-mail** in Zulip: *Invite users*, from the gear menu. Tick the participants' channel among the channels they join, the one your workshop's `!new-workshop` named with `channel=` (§3.1): a private channel can't be one of the organization's default channels, so only the invitation subscribes them to it. **Never tick `#harvard-2026-organizers` or `#harvard-2026-fleet-alerts`,** which participants never join (§1.2): the organizers channel would show them every plan and the roster's e-mail addresses.
- **When someone asks for another address,** send an invitation to the new address, then revoke the old one (the gear menu → *Organization settings* → *Users* → the *Invitations* tab): until it is revoked, whoever reads mail at the old address can still join with it. Its sender, or a Zulip administrator, may revoke it.
- **The participants' channel** is private, with shared history, so that whoever joins reads what was posted before. `zab-provisioner` must be a member: it reads the channel's members for the roster (§3.1).
- **E-mail visibility.** The organization's default for *Who can access user's email address* must stay *Admins, moderators and members* or wider until the roster is synced (§1.1, §3.1). It applies to every account your invitations create, and `zab-provisioner` reads the participants' addresses through it.

Someone who hasn't joined yet is reported `not on Zulip` by `!provision zulip` (§3.8), and the next `!provision zulip` binds them to their row once they have joined.

### 1.6 What participants need

Tell the participants with their invitation, so that they arrive ready:

- **An account for each agent they will use,** with a plan that includes it: a Claude account for Claude Code, and a ChatGPT account for Codex, with device code sign-in for Codex turned on in its security settings ([`docs/quickstart.md`](quickstart.md), "Getting started"). zab never holds their credentials: each participant signs in their own agents, with `!login` in a direct message to each bot. Where sponsors give the participants plans, the sponsors send them to the participants' addresses; zab doesn't depend on them (ADMIN §11.3).
- **A GitHub account,** whose username they put in the *GitHub* field of their Zulip profile: that is how they get access to their repository, `<org>/<slug>` (§3.4).
- **Their Zulip account,** from your invitation (§1.5), at the address you invited, which their bots recognize.
- **A browser.** Everything happens in Zulip: they need no SSH and no terminal.

**What the bots tell them.** Each participant's Claude bot posts "Start here" in their sandbox (§3.8): the sign-ins first, one step per agent, then their first request, and their repository last. Its first step begins with your workshop's gift plans, in your words: ask the deployer to set them as `gifts_step` in your workshop's entry (ADMIN §2.1), such as the e-mail that brings each invitation and the address of a form, before `!provision zulip` posts "Start here". Without it, the step says ``Accept any gift subscriptions: Claude Max (from Anthropic) and ChatGPT Pro (from OpenAI).``, and the step goes on ``Sign in to Claude and ChatGPT with the accounts that received them.`` Where your participants get no such gifts, say so in your announcement (§3.8, step 6): they sign in with their own accounts. The quick-start that you post names a GitHub organization, a file host and gifts too: edit your copy before you post it where yours differ (§3.8, step 6).

## 2. How every change works

### 2.1 Writing a command

- **It starts with `!`**, or with a mention of the bot and a space, as in `@**Workshop admin · harvard-2026** !status all`. Zulip's autocomplete puts a space after the mention; one more, or a few, do no harm. With none, as in `@**Workshop admin · harvard-2026**!status all`, the bot answers `❌ put a space between the mention and the command`. A message that starts with a mention of anyone else is not a command, and gets no answer.
- **One line.** Only the first line is read. If a later line also starts with `!`, the bot refuses the whole message: `❌ one command per message: only the first line is read`.
- **Lower case, single spaces.**
- **The selection.**
  - `all` means every participant row, and never the spares. A command is never sent to everyone by default.
  - Otherwise, name slugs: `!status alice bob`.
  - For `!protect`, `!waker on`, `!final-sync`, `!export` and `!uninstall`, `all` means the rows that have a VM.
  - `spares` (every spare) works only with `!start`, `!stop` and `!teardown`. One spare, such as `zab-spare-1`, also works with `!adopt-spare`.
- **👀 means the bot took it.** The bot adds a 👀 reaction to your command as soon as it reads it, before it starts the work: `!status all` reads every VM and takes a few seconds, a plan's dry runs up to a few minutes. The 👀 stays after the answer. A line the bot can't read as a command (a mistake below, a command formatted as code, or another workshop's slug) gets none, nor does a message it doesn't take (§8.1). A command it reads and then refuses, such as an approval with the wrong code, has its 👀 and then the `❌`. The 👀 is a courtesy, not a receipt: rarely, a very busy bot skips it, or Zulip refuses it, and the command runs all the same. So a missing 👀 alone is no reason to send a change again: wait a few minutes for its answer first.
- **A mistake** gets a one-line answer starting with `❌`, and nothing runs. For example, `!status` alone gets ``❌ no selection: name slugs, or write `all` (!status SEL)``.
- **A command formatted as code is not read,** and nothing runs. A copy and paste often makes one: Zulip's compose box pastes a copied code span, or a line copied from a code block, as code, so the message you send is `` `!stop bob` `` and shows as code, like the bot's line. When your whole message is one such command, the bot answers, with no 👀: ``❌ not run: your message is formatted as code, so it is not a command. Send it as plain text, starting with `!`: copy it with the code block's copy button (top right), or paste it and press Ctrl+Z (Cmd+Z on a Mac) once to undo the formatting. On a phone, type it.`` Send the command again, as plain text:
  - **on web and desktop,** where the bot shows the command in a code block, point at the block and click the copy button at its top right, then paste into a new message. Or paste as before and press Ctrl+Z (Cmd+Z on a Mac) once: that undoes the formatting and leaves the plain line. A plain paste, Ctrl+Shift+V (Cmd+Option+Shift+V on a Mac), works too;
  - **on a phone,** a code block has no copy button: type the line.

  Every command sent as code gets this answer, each time. Code that is not one whole command, such as a command quoted inside a sentence, gets no answer.
- **A slug of another workshop** on the server is refused, with a pointer to its bot: ``❌ no row `erin` in this workshop's roster``, then `` `erin` is in zab-test: ask Workshop admin · zab-test ``. The bot's name there is a mention that notifies nobody. The pointer goes to each sender at most once every 10 minutes, whichever bot they write to, and a `!roster import` refused for such a slug (§3.1) shares those 10 minutes: within them, a refusal comes without it.
- **Edits never count.** If you edit a command, nothing runs again: send a new message.
- **Use one topic per task,** such as `launch`, `rollout v0.2.15` or `the end`. The plan, the approvals, the progress and the result all go into the topic where you asked.

### 2.2 Reading a plan

For each change, the bot first runs every script in its dry-run mode, which changes nothing, and reads the state of the VMs it names. Then it posts two messages in your topic: the plan and the approval message. Preparing a plan takes from a few seconds to a few minutes.

**The plan message, from top to bottom,** as Zulip shows it:

- **The header:** ``**P-42** · harvard-2026 · `!stop bob` · requested by Olga Organizer · expires 2026-10-28 15:51 UTC``.
- **Targets** (n): the rows and spares it acts on.
- **Participant rows,** for `!teardown` only: how each row's end of the workshop stands (§5.8). A `!retire`'s plan has its **Rows that end** instead (§5.3).
- **Commands**, in order: the scripts it will run, with every flag. You don't need to read them.
- **Dry run**, every step: what each script says it would do, from a line `── <script>` to a line `── <script> exited 0`. **This is what you check** (below).
- **The status of the targets,** for the commands that act on VMs. It is a table headed `**Status** of the targets, read for this plan (shown, not hashed):`, one line per VM, with the columns `VM`, `state`, `zab`, `idle stop` and `running turns`. The plan's hash doesn't cover it: turns start and end by the minute.
- **The hash and how to approve:** ``**Plan** `3f9a2c41d07e`: approve with `!approve P-42 3f9a2c`, or reject with `!reject P-42`.``
- **The audit footer.**

**A long plan** is cut to fit, and says so: `*Cut to fit: … left out. The whole plan: [P-42-plan.txt](…).*`. The dry run shows at most 40 lines, and the status table at most 50, its header included. Then open the attachment, which holds the whole plan, and read it there.

**Before you approve, check:**

- the targets are the rows you meant, and their number is right;
- each script's dry run ends `── … exited 0`, and no line says `FAILED`;
- the `running turns` column: `!rollout`, `!stop` and `!uninstall` cut running turns short;
- the dry run itself, which differs by command:

| Command | Its dry run, and what to check |
|---|---|
| `!launch` | For each VM to launch, `[alice] would run_instances …`, then `[alice] would allocate and associate an Elastic IP, then wait for SSH`. A row that has a VM already gets no such line. |
| `!start`, `!stop` | One line per VM, such as `bob i-… (running): would stop`, then the count: `would stop 1, unchanged 0, failed 0`. |
| `!protect` | One line per VM, such as `carol i-… (running): would set termination protection on`, or `carol i-… (running): unchanged (protected, stops on shutdown)` once it is set. |
| `!rollout`, `!uninstall`, `!idle-stop` | One `dry-run` line per VM, naming each step it would take there (below). Every VM you named must have its line. |
| `!teardown`, `!retire` | `terminate`, `release`, `keep` and `skip` lines (§5.3, §5.8). |
| `!roster sync` | One line per change: `+` a new row, `~` a name, `-` a row removed, `=` one left alone, `x` one excluded; then the counts, such as `roster sync: 2 added, 0 renamed, 0 removed, 0 left the channel, 0 excluded, 0 unchanged` (§3.1). Read every `+` line: each is a person who gets a row. |
| `!roster remove`, `!roster restore`, `!roster rename` | One line, such as `roster remove: alice ("Alice A."): excluded: …`, then `dry run: nothing written` (§3.1). |
| `!roster import` | The counts, such as `roster import: 2 added, 0 changed, 0 removed, 0 unchanged`, then each row's changes (§3.1). |
| `!provision zulip`, `!provision repos`, `!final-sync`, `!deactivate-bots` | A table of what each row would get, read from Zulip, GitHub and the VMs as they are now. The plan's hash doesn't cover it: who has joined or accepted changes by the minute. |
| `!export` | A table that says `would export and fetch` for each row with a repository. It connects to nothing. |
| `!adopt-spare` | One line naming the spare and the row it goes to, then the roster cells it would write, and `(dry run: nothing changed)`. The later steps depend on it, so the plan shows only their commands. A spare that isn't running, or whose SSH doesn't answer yet, is refused here (§3.7). |
| `!grow-disk` | One line for the row's volume, such as `alice i-… (running): volume vol-… gp3 200 GB → 300 GB, +100 GB: about $8.00 a month more, at $0.08 per GB-month`, then, for a running VM, its disk now: `alice: root file system now: 194G, 186G used (96%), 8G free (ext4, /dev/root) […]`. Check the row and the size: the added GB are billed every month for as long as the VM lives, and a volume never shrinks. A plan that can't run is refused with the script's `refusing: …` line (§4.1). |
| `!waker` | The workshop's waker entry, as it will be written. |

**The one-line dry runs,** with the VM's address in brackets:

- **`!rollout v0.2.15 alice`:** `dry-run alice (198.51.100.1): owner check; staging dir; idle-stop hold; scp config.toml alice/codex.zuliprc alice/claude.zuliprc agent-instructions.md agent_instructions.py tools_list.py tools-nt-agents-2026-v3.json sage_wrapper.py sage zulip_agent_bridge-0.2.15-py3-none-any.whl; config installed; zab installed; service install + doctor; sage wrapper; tools list; agent instructions; scp lean-project; lean-project + shared Mathlib; lean check; scp warm_cache.py zab-warm-cache.service; warm-up on; idle-stop probe; staging removed; hold released`. Check the wheel's version, and that each VM gets its own row's keys (`alice/…`). The line shows the rollout's defaults (§3.5) as they would run on a new VM. On a VM that has the shared Mathlib already, the rollout skips `scp lean-project`, `lean-project + shared Mathlib` and `lean check`. Alice's waker isn't on here, so her idle stop is only probed. Where the waker is on, the line ends `… warm-up on; idle-stop probe; scp idle_stop.py zab-idle-stop.service zab-idle-stop.timer status_read.sh status_read_login zab-monitor.sudoers; idle stop installed; staging removed; hold released`.
- **`!uninstall alice`:** `dry-run alice (198.51.100.1): service uninstall; warm-up removed`.
- **`!idle-stop 60 alice`:** `dry-run alice (198.51.100.1): owner check; staging dir; idle-stop hold; scp idle_stop.py zab-idle-stop.service zab-idle-stop.timer status_read.sh status_read_login zab-monitor.sudoers; idle stop installed; staging removed; hold released`.
- **`!idle-stop 0 alice`:** `dry-run alice (198.51.100.1): owner check; idle stop removed`.

### 2.3 Approving

**The approval message** comes right after the plan. What it shows depends on whether the plan needs a typed phrase (§1.2's table).

**A plan with no phrase,** such as `!stop bob`:

- **On web and desktop,** it shows the heading `P-42: !stop bob` and two buttons, ✅ Approve and ❌ Reject. A click sends the command for you.
- **On a phone,** the Zulip app shows the message's text only, with no buttons: ``P-42 `!stop bob`: approve with `!approve P-42 3f9a2c`, or reject with `!reject P-42`.`` Type the `!approve` line it shows, such as `!approve P-42 3f9a2c`.

**A plan that needs a phrase,** such as `!stop all` or `!deactivate-bots alice`, has **no buttons**, since a button can't type the phrase. Every app, web, desktop and phone, shows the same three parts:

1. ``P-42 `!stop all`: approve by sending this plan's approval line, exactly as it is. A plan like this one has no Approve button. Point at the line below and click its copy button, at its top right (or type the line), then send it as a new message.``
2. The approve command alone, phrase included, in a code box of its own, such as `!approve P-42 3f9a2c stop 3`. **On web and desktop,** point at the box: its copy button shows at the box's top right. Click it, paste into a new message, and send it. **On a phone,** type the line.
3. How many approvers it takes, and how to reject: ``It takes 1 approver. To reject, send `!reject P-42`.``

A plan that needs two approvers starts the same way, ``P-42 `!deactivate-bots alice`: approve by sending this plan's approval line, exactly as it is. …``, and ends ``It takes 2 distinct approvers, or a global admin alone: each one sends that line. To reject, send `!reject P-42`.`` Each approver sends the same line, such as `!approve P-42 3f9a2c deactivate-bots 1`: two organizers, or a global admin alone.

**Copy the approve command from the approval message,** with the copy button where it has one, or from the plan's **Plan** line (§2.2), and never from an older plan. It carries the plan's ID, the first 6 hex digits of its hash, and the phrase. Approvals happen in the organizers channel, in any topic, and never by DM.

**If your approval gets no 👀** (§2.1), look at the message you sent: it must start with `!`. A line copied by hand, rather than with the copy button, can paste with a backtick before and after it. It then shows as code, like the bot's line, and the bot doesn't run it: it answers ``❌ not run: your message is formatted as code, …`` (§2.1). Send the line again as plain text: click the code box's copy button and paste, or paste it and then press Ctrl+Z (Cmd+Z on a Mac) once, which removes the formatting, or paste it as plain text with Ctrl+Shift+V (Cmd+Option+Shift+V on a Mac).

**The answers:**

| Answer | Meaning |
|---|---|
| `✅ P-42 approved` | The apply starts (§2.4). |
| `✅ P-42: 1 of 2 approvals, from distinct approvers` | A second approver is needed: another organizer, or a global admin. |
| `✅ P-42 approved (a global admin's approval)` | A plan of two approvers, completed by a global admin's approval alone. The apply starts. |
| `✅ P-42 approved (2 distinct approvers)` | A plan of two approvers, completed by the second. The apply starts. |
| `✅ P-42 rejected: nothing was done` | Any approver may reject any pending plan, even after another approver has approved it. |
| `❌ that is not P-42's hash: read the plan again` | Copy the command from the plan. |
| ``❌ P-42 needs the phrase `stop 3` after the hash`` | Add the phrase exactly as written. |
| `❌ P-42 takes no phrase` | Send the command without one. |
| `❌ you approved P-42 already: it needs another approver` | Someone else must approve. |
| `❌ P-42 is another bot's plan: answer it on the bot that posted it` | Send it in that workshop's channel. |
| `❌ P-42 is still being prepared: answer its approval message once it is posted. Nothing was recorded.` | Wait for the approval message. |
| `busy: P-41 is applying. Nothing was recorded: send the approval again once it is done, before P-42 expires.` | One apply runs at a time in a workshop. Send the approval again once P-41's result is in. |
| `❌ P-42 expired: request again` | See below. |
| `❌ P-42 is rejected, not pending` (or `applying`, `done`, and so on) | Nothing more to approve. |
| `🚫 Pat Participant is not an organizer here; nothing was done` | The sender is not on your workshop's list, and nothing they send counts. If they should be, ask a deployer (§1.1). |

**Plans expire** 10 minutes after they are posted: `⌛ P-42 expired: nothing was done. Request again.` Then send the same command again, and read the new plan. With two approvers, both must approve within the 10 minutes.

**Nothing to change.** When the dry run says that everything is already as the command asks, there is no plan and nothing to approve, and nothing went wrong. The bot answers with one message, marked `☑`: ``☑ **P-42** · `!roster sync`: nothing to change, as its dry run says; no plan to approve``. The dry run follows, ending with the script's own words, such as `nothing to change: every subscriber has a row, and every row is theirs`, and `── admin/roster_sync.py exited 3`, the script's way of saying so. Only `!roster sync` and `!roster import` (§3.1), and `!provision repos` with `invites-only` (§3.8), answer this way. Every other command posts its plan, even when approving it would change nothing.

### 2.4 The apply and its result

- **Progress.** One message shows the step and the last line of output, edited only when they change, at most once a minute and at most 19 times; the last of them says that the progress is paused, and the result still follows: ``⏳ **P-42** · `!rollout v0.2.15 all`: applying, step 1 of 2, admin/push_config.py``.
- **Success:** ``✅ **P-42** · `!rollout v0.2.15 all`: done in 4 min 5 s, every step exited 0``.
  - Below it come the last lines of the output, usually the script's own table, one row per VM.
  - When the output is longer, the whole of it is attached: `*The last lines; the whole output: [P-42-apply.txt](…).*`
- **Failure:** ``❌ **P-42** · `!rollout v0.2.15 all`: failed at step 1 of 2: admin/push_config.py exited 1. Nothing after it ran``.
  - Read the table: which rows failed, and why.
  - Fix the cause, then send the command again, naming only those rows. Every script can be run again safely.
  - The exceptions:
    - a failed `!uninstall` or `!final-sync` counts for none of its rows, so send it again for all of them (§5.8);
    - an `!adopt-spare` that failed after its first step can't be sent again (§4.1);
    - a failed `!provision zulip all`, `!waker on all`, `!rollout v0.2.15 all` or `!idle-stop 60 all`: once the cause is fixed, send **the same command, with `all`**, until it ends `✅`. A late joiner's `!adopt-spare` gives the spare what the last successful run of these with `all` gave the fleet (§3.5). A run that names rows, or one that failed, changes nothing there. Sending them again is safe: `!provision zulip` reuses the bots and sandboxes it made, and sends no invitations; `!waker on` rewrites the workshop's waker entry, and keeps the VMs it doesn't name; `!rollout` installs the same release again, restarting every bridge, so send it at a break; `!idle-stop` only rewrites the timer.
    - **During the workshop, start the fleet first.** With idle stop on, some VMs are stopped at any time, and a `!rollout`, `!idle-stop` or `!provision repos` (not `invites-only`) of `all` connects to every VM: each stopped one fails the run, which then records nothing for `all`, and keeps its old setting when it wakes, the multi-party kill switch's too. So send `!start all`, then `!status all` until no row shows `stopped` (about a minute), then the command. The VMs it started stay up for at least the idle-stop timeout (§4.2). If `!start all` ends `❌` with `it is stopping: rerun once it is stopped` for a VM that idle stop was powering off, or a row still shows `stopped` after two minutes, send `!start all` again. A VM that was already running isn't refreshed by `!start all`, and may still stop before the command reaches it: if the command then fails on a VM that has stopped since, repeat both.
- **Timeout:** ``⏱ **P-42** · `!rollout v0.2.15 all`: timed out after 30 min 0 s, at step …``. The limits are 60 minutes for `!launch`; 30 minutes for `!rollout`, `!final-sync`, `!export` and `!teardown`; for `!retire`, 60 minutes, or 20 minutes a row above three, and 55 minutes more a row with `export` (§5.3); 15 minutes for anything else.
- **No stopping.** An apply can't be stopped from Zulip: `!cancel` comes later.

### 2.5 When a plan doesn't go through

| Message | What happened | What to do |
|---|---|---|
| ``☑ **P-42** · `!roster sync`: nothing to change, as its dry run says; no plan to approve`` | Not a failure: everything is already as the command asks (§2.2). | Nothing. |
| `❌ P-42 refused: <why>; nothing was done` | The command can't run as asked, or a dry run failed. A failed dry run's output follows the message. | Read the reason, fix it, and ask again. |
| `❌ P-42 is stale (hash now …; since the plan: roster.csv changed): nothing was done. Request again.` | Something changed between the plan and the approval. The message names what, when it can. | Ask again, and read the new plan. |
| `❌ P-42 is stale: it no longer builds: <why>. Request again.` | Since the plan, the command no longer applies to the fleet as it is now, for example a teardown (§5.5). | The same. |
| `⌛ P-42 expired: nothing was done. Request again.` | Nobody approved it within 10 minutes. | Ask again. |
| ``⏹ P-42 was interrupted by a restart of the bots: nothing resumes on its own. Check `!status`, then request again.`` | The bots restarted, for example for a deploy. | Check `!status`, then ask again. |

**While the bot was down, nothing you sent was seen,** and it has no 👀. A plan stays pending until it expires, so send your approval again.

## 3. From roster to rollout

From the roster, before launch day, to the rollout on launch day, W−2: the order of the scripts run by hand (`workshop/admin/README.md`, "The order"), the waker before idle stop (§3.5, §4.2).

### 3.1 The roster: `!roster sync`, or `!roster import`

A workshop made with a participants' channel (`channel=` in its `!new-workshop`, NEW-WORKSHOP §1) takes its roster from that channel's subscribers: `!roster sync`. Without one, the roster is a CSV you upload: `!roster import` (below). `!roster show` says which: on an empty roster it answers ``The roster has no participants yet: `!roster sync`.`` with a channel.

**A new workshop's channel, step by step:**

1. A global admin's `!new-workshop`, with `channel="<its name>"`, adopts the channel, or makes it, private, with the organizers and `zab-provisioner` in it (NEW-WORKSHOP §1).
2. You invite the participants in Zulip by e-mail, with that channel ticked, as §1.5 says (who may invite, and how). Each one is in it once they join.
3. `!roster sync`, once they have joined, and again at any time for late joiners: it is safe to repeat.
4. Then `!launch all` and the rest of this section, or, for a late joiner, `!adopt-spare` (§4.1).

**Who gets a row.** Every active person subscribed to the channel, **organizers included**, but never `zab-provisioner` (the key the bot reads the channel with: it is a user, not a bot) and never a bot. Each row is linked to its person by Zulip user ID from the start. An organizer, or anyone else in the channel, who should get no VM: `!roster remove` their row (below).

**Send `!roster sync`.** The plan's dry run lists every change, one line each:

- `+ alice-smith-2  (Zulip user 1234567)  "Alice Smith"  "alice@example.edu"  (alice-smith is taken)`: a new row, one line each, with its slug, Zulip ID, name and e-mail. The last bracket says why the slug took a suffix, here that another workshop on this server has `alice-smith`, or that the person's address is one the scripts don't take (an apostrophe, say), so their row takes a placeholder address instead; either way, the row is linked by its Zulip ID;
- ``~ alice: name "Alice A." -> "Alice Aardvark"``: a name changed in Zulip. Only the name follows Zulip; nothing else of a row changes;
- `- bob: not in the channel, and nothing is provisioned for it: removed`: someone who left the channel, or was deactivated, before anything was made for them;
- `= carol: left the channel …, but it has a VM (ws-carol): left alone …`: someone who left after something was made for them (a VM, bots, a sandbox or a repository). The row stays; its end is `!retire`'s (§5);
- `x dave: excluded (!roster remove): not added again`: a row you excluded;

then the counts, as `roster sync: 2 added, 1 renamed, 1 removed, 1 left the channel, 1 excluded, 5 unchanged`, and what to run next: on launch day the order of §3, with `all`; for a late joiner, `!adopt-spare` (§4.1).

**Read the additions before you approve.** Where the organization lets members subscribe others to channels, anyone in the channel can add someone to it, who is then offered a row here: check that each `+` line is a participant or an organizer. A first line `WARNING: #… is public: …` says that anyone in the organization can join the channel: make it private (its settings, *Permissions*), then sync again.

**Approve it.** The plan is hashed on its dry run: if someone joins or leaves between the plan and your approval, it is STALE: send `!roster sync` again. The result says `written: …`.

**A sync with nothing to change** gets no plan, and nothing to approve: ``☑ **P-43** · `!roster sync`: nothing to change, as its dry run says; no plan to approve``, a success (§2.2). Under it, the dry run lists the rows left alone (`=`) and excluded (`x`), if any, then `nothing to change: every subscriber has a row, and every row is theirs`. A second sync in a row, with nobody joining or leaving in between, gets this answer. If what a row that left holds can't be checked (`= bob: left the channel …, but what it holds can't be checked (…): left alone`), and nothing else changes, the sync is refused instead, with `❌` and `the dry run failed: admin/roster_sync.py exited 1`, its output ending `refused: what bob holds can't be checked, …`: send `!roster sync` again later, and tell the deployer if it keeps failing.

**The slugs** are made from the Zulip name: letters folded to ASCII (`Zoë Ångström` is `zoe-angstrom`, `Søren` is `soren`), each run of other characters one hyphen, at most 32 characters, cut at a hyphen. A name with no Latin letters, or one that makes a keyword such as `all` or `hold`, is `p-<user ID>`. A slug of another workshop on this server, now or ever, or one that a bot or a sandbox is named after, takes `-2`, `-3`, …: Alice Smith is `alice-smith-2` when another workshop has `alice-smith`. Change one with `!roster rename` before anything is made for the row.

**The e-mail** of a new row is the one `zab-provisioner` sees: the person's address, or Zulip's placeholder, such as `user1234567@example.zulipchat.com`, when they hide it from moderators. Either way the row is linked by its Zulip user ID, which `!provision zulip`, the rollout's allow list and the waker use, and git's `user.name` on their VM is their Zulip name.

**Leaving a row out, and slugs.** Each is a plan of its own, with one approver:

- **`!roster remove alice`** excludes the row: no sync adds it again, `all` leaves it out, and a command that names it is refused (`… alice excluded from the roster (!roster remove): !roster restore alice first`). `!roster show` marks it `excluded`. Refused once anything is provisioned for the row: ``…: excluding it is refused: it has a VM (ws-alice); its end is !retire's``;
- **`!roster restore alice`** brings it back: `all` names it again, and the next sync matches it, or removes it if the person has left the channel;
- **`!roster rename alice alice-l`** changes the row's slug, under the slug rules above. Refused, likewise, once anything is provisioned for the row, and for a slug that is taken: another row's, another workshop's (`… is in zab-test; a slug is never shared between workshops (ADMIN §2.1)`, with the pointer to its bot), or one that a bot, a sandbox or a key is named after already.

#### From a CSV: `!roster import`

The alternative, for a workshop without a participants' channel.

**The CSV**, with a header line:

- **Columns:** `slug`, `first_name`, `last_name` and `email`. Every row needs a slug and an e-mail: the address you invite to Zulip (§1.5), which is also the one allowed to use the bots.
- **Optional columns:** `repo`, `region` (empty, or the workshop's own), `notes`, and `github` (only when a participant leaves the GitHub field of their Zulip profile empty).
- **Slugs** have 1 to 32 characters: lower-case letters, digits and inner hyphens. Slugs and e-mails must be unique, ignoring case.
- **Reserved:** no slug may be `all`, `spares`, a keyword of the commands (`hold`, `protect`, `lean-shared`, `restart-codex`, `invites-only`, `no-invite`, `unsynced`, `export`), a setting's name (`steer`, `idle-stop`, `multi-party`, `raw-log`), or `zab-spare-N`.
- **What the slug names** (`workshop/admin/README.md` §4): the VM, the bots `Claude · <slug>` and `Codex · <slug>`, the sandbox `#<slug>-sandbox` and the repository `<org>/<slug>`.
- **Slugs are never reused.** A slug that another workshop on this server has, or has had, is refused. The refusal names the workshop that has it (`erin is in zab-test; a slug is never shared between workshops (ADMIN §2.1)`), and the bot adds its pointer to that workshop's bot under it, as for a command, within the same 10 minutes (§2.1).

**Send** `!roster import` with the CSV attached to the same message (the paperclip, or drag the file in).

- **The attachment:** exactly one, of at most 1 MB.
- **Its name:** letters, digits, `.`, `-` and `_` only, ending in `.csv`.
- **Otherwise:** `❌ attach exactly one roster CSV to the !roster import message`, or `❌ attach a .csv whose name has only letters, digits, '.', '-' and '_'`.

**The plan's dry run** shows:

- the counts, as `roster import: 2 added, 0 changed, 0 removed, 0 unchanged`;
- each row's changes, column by column;
- what to run next, as `next, new rows: !launch …`.

**The CSV replaces the whole roster.** A row it leaves out is removed, if nothing holds it yet (below). So check that `removed` is 0, unless you meant to remove rows.

**Approve it.** The result says `written: …`. Then `!roster show` lists the rows, and `!roster show alice` shows one in full.

**A CSV that changes no row,** such as the same one sent again, gets no plan: ``☑ **P-44** · `!roster import`: nothing to change, as its dry run says; no plan to approve``, with `roster import: 0 added, 0 changed, 0 removed, 5 unchanged` and `no changes: nothing to write` under it (§2.2).

**Corrections and additions.** Send the whole CSV again, corrected or with the new rows added.

- The columns that the scripts fill (`host`, `ip`, the Zulip and GitHub state) are kept from the server's copy.
- An empty `region`, `repo` or `github` keeps the recorded value.
- The names and `notes` are replaced, even when empty.

**Refused, and nothing written:**

- a slug or e-mail given twice;
- a row removed or renamed while something holds its slug, most often as `…: removing or renaming it is refused …`. That is a row that:
  - has a VM, in the roster or still in EC2 (tear it down first);
  - is bound to a Zulip user, or whose e-mail a Zulip user has, or to whose e-mail an invitation is open;
  - had its repository provisioned, or whose recorded GitHub login has access or an open invitation to it;
  - can't be checked: a check that fails refuses too.

  A new row with its slug would inherit its sandbox or its repository. Removing such a row stays a deployer's job (§7);
- a recorded region, changed;
- the e-mail of a participant who has joined Zulip, or the GitHub login of one who has access to their repository: that is a *rebind*, refused as `… a rebind (ADMIN §2.5), which a deployer does by hand …`. A deployer does it by hand until `!rebind` exists (§7);
- an e-mail change, or a removal, while any Zulip user hides their address from `zab-provisioner`, with *Admins only* or *Nobody*. The bot can't then prove that nobody has the address, so it refuses: `…: e-mail change: cannot check the old address: … a deployer does this by hand`, or `…: removing it: cannot check its VM, address and login: …`. A deployer makes the change by hand (§7).

An e-mail correction for someone who never joined Zulip under the old address is an ordinary change. Its plan says `plain change: not on Zulip, and no open invitation to the old address that the provisioner sees` (§4.1). The bot sees no invitation that you send in Zulip: when you correct an address, revoke your invitation to the old one yourself (§1.5).

### 3.2 Launching the VMs

**`!launch all spares=2`** launches a VM for every row that has none, and the spares `zab-spare-1` and `zab-spare-2` (§4.1). Then it pins each VM's SSH key.

- **The dry run** lists each VM it would launch, such as `[alice] would run_instances …` and `[alice] would allocate and associate an Elastic IP, then wait for SSH`.
- **It takes time:** 20–40 minutes for a whole fleet, and at most 60.
- **It is safe to repeat.** Run again, it launches only the VMs still missing.
- **Check the result.** The scan's table, the last step's, has a `key` and a `console` column. Every row must read `added` or `unchanged` under `key`, and `verified` under `console`.
  - `unreachable`: the VM didn't answer yet, which is common right after a launch. Wait a few minutes and send the same `!launch` again: the VMs that exist are left alone, and the scan runs again.
  - `error`: send it again. If it stays, call the deployer.
  - `unverified`: the VM's key could not be checked against its console. Call the deployer (`workshop/admin/README.md` §0).
  - `MISMATCH`: whatever answers at that address is not the VM. `CHANGED`: its key differs from the one pinned before. For either, stop and call the deployer.
- **The spares.** `spares=N` launches every missing `zab-spare-1` … `zab-spare-N`, even one already handed over. So after `!adopt-spare` (§4.1), launch without `spares=`, unless you want a replacement. The step after the launch gives each spare named its own idle stop, so that it stops by itself once its first boot's warm-up has ended (§3.7). If the `!launch` fails, that step doesn't run, and its spares may run without it: once the cause is fixed, send `!start spares`, which gives it to them. If it fails at that step (`failed at step 2 of 3: aws/spare_idle_stop.py exited 1`), the rows' keys weren't scanned, since `scan_hosts.py` comes after it: send the same `!launch` again, or the same without `spares=` for the rows alone, and call the deployer if the spare's `FAILED` line persists.

### 3.3 Zulip, held

**`!provision zulip hold all`** creates each row's two bots, and a sandbox channel that holds only those bots (`workshop/admin/README.md` §1).

- **Nothing reaches the participants yet:** nobody is subscribed, and no "Start here" is posted. The report says `held`.
- **It is safe to repeat.**
- **Its dry run reads Zulip live,** and the plan's hash doesn't cover it: who has joined changes by the minute.
- **`binding changed: refused`** on a row means that its e-mail now belongs to another Zulip user than the one recorded. Call the deployer.
- **What you see of the participants' addresses.** Under the organization's default (§1.1), every member sees each participant's e-mail address in their profile: you, the other organizers and the participants.
- **Binding.** `zab-provisioner`, a Moderator, binds each joined participant to their row by that address, even one who restricts it to *Admins and moderators*. It can't bind a participant who restricts it to *Admins only* or *Nobody*: their row gets no Zulip ID in the report, and their bots don't recognize them. Ask them to choose *Admins and moderators* or wider (*Personal settings → Account & privacy → Who can access your email address*), then send the command again. If they won't, call the deployer.

### 3.4 Repositories

**`!provision repos all`** sets up each participant's repository (`workshop/admin/README.md` §1b):

- it creates `<org>/<slug>`, in your workshop's GitHub organization;
- it gives the VM a deploy key and a clone;
- it invites the GitHub login of each participant who has already joined Zulip and filled in their profile's GitHub field.

**Variants.** `!provision repos no-invite all` creates the repositories without inviting anyone. `invites-only` (§3.8) only sends the invitations.

**`login changed: refused`** means that a participant changed their login after accepting access. That is a rebind (§3.1): call the deployer.

**Participants are their repository's admins** (#714). The invitation is for admin, so that they can add collaborators, and transfer or delete the repository. A login invited for less by an earlier release is raised: the row reads `collaborator; raised to admin`, or `pending; raised to admin` for an invitation not accepted yet. After the server runs the release, one `!provision repos invites-only all` raises a workshop's participants. Only admin counts as done (`collaborator`); an organization owner is one. §4.3 says what a participant's admin can break.

### 3.5 The waker, then the release

**First, `!waker on all`** (§4.2). From then on, a message from a participant to one of their bots starts their VM when it is stopped. It comes before the rollout, which turns idle stop on only where the waker is on, so that no VM ever stops without a way to wake it.

- **It needs the bots and the VMs:** send it after §3.2 and §3.3. Here `all` means the rows that have a VM.
- **The dry run** shows the workshop's waker entry as it will be written.
- **It must end `✅` before the rollout.** One row it can't take, such as a VM it can't find, fails the whole run, and then nothing is written. A rollout after it turns idle stop on nowhere, and its plan says so: `Not on …: the waker isn't on there`. Send `!waker on all` again until it ends `✅` (§2.4). If a row keeps failing, call the deployer, and roll out all the same. Once the waker is on, send `!idle-stop 60 all`.
- **Participants not bound yet.** The waker obeys a row's Zulip user, its `zulip_user_id` in `!roster show`, as the row's last `!waker on` found it, and otherwise only the row's e-mail.
  - **On launch day** that costs nothing. `!provision zulip hold all` has bound everyone on Zulip by then that it could bind: not those who restrict their address to *Admins only* or *Nobody* (§3.3). The others aren't on Zulip yet, so they can't write to their bots at all. That is why launch day can turn the waker on before every row is bound.
  - **Once they join,** the wake monitor, which checks every start against the participant's messages (ADMIN §1.2), reads the Zulip ID from the roster. So its alerts about their wakes stop as soon as `!provision zulip` binds their row.
  - **The waker itself** keeps only their e-mail until the next `!waker on` for their row. By e-mail it can't recognize a participant whose address is hidden from members (*Admins and moderators*, §3.3), since their bots are members: their messages then wake nothing. This is the waker's form of the `doctor: …:allowlist: no allowed user yet: …` of §3.6. With the Zulip ID it recognizes them anyway.
  - **So,** at the breaks of each day, once the invitees have joined, send `!provision zulip all`, then `!waker on all` (§3.8), and `!waker on alice` after a `!provision zulip alice` (§4.1).

**Then `!rollout v0.2.15 all`** installs zab and its configuration on each VM, restarts its services and runs its doctor, the VM's self-check. Then, by default, it sets up the shared Mathlib where a VM lacks it, turns idle stop on where the waker is on, and reads the VMs' status. The plan's notes, under its targets, say both defaults.

**The defaults need the server at v0.2.17 or later** (`!version`). On an older server, send its release with both words, such as `!rollout v0.2.16 all lean-shared idle-stop=60`.

- **The shared Mathlib, by default.** Your workshop's image is the one its entry names, which its `!new-workshop` plan showed ([`workshop/ami/README.md`](../workshop/ami/README.md), "The images"). An image built before the shared Mathlib (#205) lacks it. There the rollout installs `lean-project`, makes the VM's prepared Mathlib its one shared, read-only copy, and times a one-file Mathlib build, about a minute per VM. Each row then says `note: lean: shared Mathlib: a Mathlib file in …`. A newer image has the shared Mathlib from its build: there the rollout only checks, in a second, and says nothing. Without a shared copy, every Lean project the agents create would unpack its own Mathlib, about 8 GB and several minutes.
  - **On an image without Lean,** the row notes `lean: no Lean workspace (/home/agent/lean/workspace/.lake/packages): no shared Mathlib to set up`. That is not a failure.
  - **`note: lean: the shared Mathlib was not probed, …`** means that the instructions step failed on that VM. The rollout looks for the shared Mathlib in that step, so it couldn't look. The row's `failures` say why. `!rollout v0.2.15 alice lean-shared` sets up the shared Mathlib regardless.
  - **`lean-shared`** does the same as the default, and also times the build again where the copy is shared already. A copy already shared stays shared.
  - **If it fails on a VM,** that row fails with `lean shared: …` or `lean check: …` under `failures`, and the others go on. A check that fails, or takes over 120 s, puts the Mathlib that this run shared back where it was, so Lean works there as before: `note: lean: rolled back; ~/lean/workspace has its own Mathlib again`. A copy shared by an earlier run stays shared. Send the same command with `all` again (§2.4), and call the deployer if the same VM fails twice. Until the cause is fixed, each later rollout tries that VM again, with its timing build, and fails again.
- **The VM's software list** (#475). Participants see what their VM has with `!tools`, in a DM or a topic (`docs/quickstart.md`), and the agents' instructions are given the same list, with the rule to use the right tool unasked. Both read the VM's list, `/etc/zab/tools.json`. An image brings its own, and a rollout never replaces it. On an image built without one, each rollout installs the list that the release carries, less any tool that VM lacks: the first time, its row notes `tools: installed /etc/zab/tools.json for …: … tools are on this VM`. `lean-project`, which the shared Mathlib's step installs after the list's step, joins the list from the next rollout on. A row failing with `tools list: …` has no list from this rollout; the rest of the rollout went on. Call the deployer.
  - **`note: tools: none in the instructions (…)`** means the VM's list is missing or can't be read. Its agents' instructions then name no tool and tell the agents to check first, and `!tools` says the list is missing or can't be read. Call the deployer.
  - **Sage on an image built before its wrapper** (#482). Such an image's `sage` command leaves Sage's own programs off `PATH`, so Sage's `giac(…)`, `r(…)` and `gap(…)` fail there. Each rollout gives such a VM the newer images' `sage`, which puts them on `PATH`, and the list then says these work. The first time, the row notes `sage: installed the wrapper as /home/agent/.local/bin/sage, in place of its link to /home/agent/miniforge/envs/sage/bin/sage`; later rollouts say nothing, nor do newer images, which have it already. After a change to the wrapper in zab, each VM notes `sage: updated the wrapper /home/agent/.local/bin/sage` once; that is expected. A VM whose `sage` is something else is left alone, and noted `sage: skipped: …`; its list keeps the old caveat. A failure is `sage wrapper: …` under `failures`; the rest of the rollout went on. Call the deployer for either. `!uninstall` leaves the new `sage` in place.
- **Idle stop, by default** (§4.2), as each VM's last step: 60 minutes, on each VM whose waker is on and whose idle-stop timer isn't on. Where it turns idle stop on, the row notes `idle stop: after 60 min`. Every VM is checked first, and its row says what was found, unless its timer and its waker are both on.
  - **A timer already on** stays as it is, whatever its minutes. One installed but not enabled is turned on again, where the waker is on: `idle stop: its timer was installed but not enabled: turned on again`.
  - **Idle stop turned off on purpose,** by `!idle-stop 0 alice`, a rollout with `idle-stop=0`, or a `!retire` (§5.1), stays off. The row notes `idle stop: left off, as it was turned off on purpose: !idle-stop 60 alice turns it on`. Where the row's waker isn't on either, the note gives the waker first: `idle stop: left off, as it was turned off on purpose, and this row's waker isn't on: !waker on alice, then !idle-stop 60 alice`.
  - **A row whose waker isn't on** gets no idle stop, so that no VM stops without a way to wake it. The plan names the row, with the commands, and the row notes `idle stop: left as it is, since this row's waker isn't on: !waker on alice, then !idle-stop 60 alice`. If its timer is on all the same, the note is a warning: `idle stop: WARNING: it is on, but this row's waker isn't on: !waker on alice, or !idle-stop 0 alice`. Send one or the other.
  - **A row whose waker names another VM** counts as a row without the waker. That happens after a VM is replaced by hand (§7.2) and before the next `!waker on` for its row: `idle stop: left as it is, since the waker names i-…, not this VM (i-…): !waker on alice, then !idle-stop 60 alice`.
  - **`idle-stop=60`** sets 60 minutes on every VM named, whatever it had and whatever its waker, as `!idle-stop 60 all` does alone, without restarting the bridges. `idle-stop=0` turns idle stop off on purpose.
- **The phrase.** For more than half the fleet, approve with the phrase `rollout <n>`.
- **Before you approve,** check the version, and check the status for running turns. A rollout interrupts them.
- **The result table** has the columns `participant`, `host`, `install`, `doctor` and `failures`.
  - Before the workshop every doctor reads `ok (sign-in pending)`, since nobody has signed in yet. A line under the table counts these VMs: `… VM(s) ok, sign-in pending: expected until the participants send !login to their bots`.
  - A participant who hasn't joined Zulip yet has no Zulip ID in the roster, so their VM's configuration names only their e-mail. Their doctor reads `ok (owner+sign-in pending)`, which is fine too, and a second line counts these VMs: `… VM(s) ok, owner pending: expected until the participants join Zulip and !provision zulip (zulip_provision.py) records their IDs`. On launch day that can be most of the fleet. Once they have joined, `!provision zulip all` records their IDs, and the next rollout writes them to the VMs and reads `ok (sign-in pending)`.
  - **Until that rollout,** the bots recognize a participant by their e-mail only if their address is visible to members (the default, §1.1), because the bots are members. A participant who chose *Admins and moderators* (§3.3) is not recognized. Their row then shows `active, doctor FAIL` in `!status`, with `doctor: …:allowlist: no allowed user yet: …` under the table, and `!rollout v0.2.15 alice` (their slug) records the ID. Rows without that failure need nothing.
  - Any `FAIL` is a real problem: read its `failures`, fix the cause, and roll out that row again.
  - The lines `claude steer: ON (messages sent during a Claude turn steer it)`, `multi_party: on (owner mentions required in channels with other participants)` and `mention after: 120 s (…)` (§1.1) give three settings.
- **Options:**
  - `steer=off`: messages sent during a Claude turn wait for it to end, instead of steering it;
  - `multi-party=off`: the kill switch of §4.1;
  - `raw-log=meta`: record the shape of Claude's output, for debugging;
  - `idle-stop=N`: idle stop after N idle minutes on every VM named, in place of the default (above; §4.2);
  - `lean-shared`: the shared Mathlib, its build timed again where it is set up already (above);
  - `restart-codex`: also restart the Codex daemon, where no Codex turn is running: its threads keep the environment they were loaded with until it restarts (`workshop/admin/README.md` §2.4).
- **Most settings don't stick.** Every rollout sets `steer`, `multi-party` and `raw-log` back to their defaults, unless you give them again. The shared Mathlib and idle stop stay as the VM has them, except that a rollout without `lean-shared` or `idle-stop=` applies their defaults (above).
- **Provision, wake and roll out the whole fleet with `all` at least once.** `!adopt-spare` (§4.1) gives a spare what the fleet has:
  - the release of the last successful `!rollout` of `all`, that rollout's settings, and `lean-shared` and the idle stop as the `!rollout` and `!idle-stop` runs of `all` last gave them. Where none of them named these, the spare gets the defaults: the shared Mathlib, and idle stop at 60 if it gets the waker;
  - its Zulip side held or not, as the last successful `!provision zulip` of `all` left the fleet;
  - the waker, if the last successful `!waker` of `all` was `!waker on`, before the spare's idle stop.

  That idle stop replaces the spare's own (§3.7). Where the fleet's gives the new row none, such as without the waker, the spare's is removed, so that its 10 minutes never stop the participant's VM.

  A command that names slugs instead changes none of these. Until a `!provision zulip` and a `!rollout` of `all` have succeeded, `!adopt-spare` is refused with `no !provision zulip or !rollout recorded here yet: run them first`.

### 3.6 Looking at the fleet: `!status`

**`!status all`, or `!status alice bob`,** reads the VMs. It makes no plan and needs no approval.

- **EC2 first.** A VM that EC2 says is stopped, stopping or starting is shown from that state alone, with no SSH, so a `!status` of stopped VMs answers within seconds. Running VMs are read over SSH as before, and the answer waits for those reads.
- **One at a time.** A second `!status` in the same workshop, sent before the first is answered, gets ``❌ a `!status` of this workshop is under way: wait for its answer``.
- **The table** is the one plans show (§2.2): one line per VM, with the columns `VM`, `state`, `zab`, `idle stop` and `running turns`. `?` means unknown: a stopped VM shows `?` everywhere but its state. The state is one of:
  - `active`, `active, sign-in pending`, `active, owner+sign-in pending` or `active, owner pending` (the row has no Zulip ID yet and the bots can't see the participant's address, usually because they haven't joined, §3.5);
  - `active, doctor FAIL`;
  - `stopped`, `stopping`, `starting`, `unreachable` or `no zab`.
- **Under the table of one VM,** a few lines, each one when it is known: who is signed in (`not signed in` until the participant's `!login`; `FAIL` is a real error), the VM's sentinel (`sentinel ok`; in a down state, `not installed`, `down`, `no mark` or `stale`, shown as `sentinel <state>: DM !restart is unavailable, …`, such as `sentinel down: …`, the participant's DM `!restart` restarts nothing; `unknown` or `warn` means that the read couldn't tell; for any state but `ok`, call the deployer, §4.1), the last activity, the running turns and when they started, the load, memory and disk (with `under strain: …`), idle stop's last verdict, the doctor's failures and the last error. The disk is a percentage; once it is 90% full, under strain, it also names its free space: `disk 92% (15 GB free)`.
- **Under the table of several VMs,** one line for each VM that has something to look at (strain, a sentinel that isn't `ok`, a doctor failure or an error); a full disk names its free space there too, `under strain: disk (15 GB free)`, and a sentinel its state, `sentinel down`. Then the count of who has signed in to which agent, among the VMs that answered, and of the VMs unreachable, stopped and with `doctor FAIL`.
- **A long answer** is cut to fit: at most 50 lines of table, its header included, and 20 lines under it, so that the table of a workshop of up to 49 VMs shows whole. It says what it left out, and the whole answer is attached as `status.txt`, followed by the read as `status.py` printed it.

**`!status alice`** answers, for example:

```text
VM     state   zab     idle stop  running turns
alice  active  0.2.15  60 min     1 claude
```
- Codex signed in, Claude not signed in
- sentinel ok
- last activity 2026-10-27 14:05 UTC
- running: claude since 2026-10-27 13:58 UTC
- load 0.4 of 8 CPUs, memory 2.9 of 32 GB, disk 23%
- idle stop's last verdict: `2026-10-27T14:00:00Z stay up: zab active 3 min ago`

**`!status all`**, of four VMs:

```text
VM     state                    zab     idle stop  running turns
alice  active                   0.2.15  60 min     1 claude
bob    active, doctor FAIL      0.2.15  60 min     0
carol  stopped                  ?       ?          ?
dave   active, sign-in pending  0.2.15  60 min     0
```
- **bob**: doctor: `codex:allowlist: no allowed user yet: no Zulip user with the allowed e-mail visible to this bot (not joined yet, or the…`
- **dave**: under strain: memory, disk (15 GB free); sentinel down

signed in: 1 both, 1 Codex only, 0 Claude only, 1 neither; 0 unreachable, 1 stopped, 1 doctor FAIL

- **`active, doctor FAIL` with `doctor: …:allowlist: no allowed user yet: …`**, on a participant who has joined, means that the VM still names them by e-mail only and their bots can't see their address (§3.5). `!rollout v0.2.15 alice` records their ID on the VM. Otherwise there is nothing to do.
- **The cost.** On a server with the console, the answer ends with the workshop's estimated cost (ADMIN §11.2), for example `cost (estimate): $412 this month, $1,050 projected · 9 of 40 running · spares: 0 of 3 running`. The VMs counted first are those of the table, the participants' (and any VM no row names); the spares come apart (#715), and the console counts them the same way.
- **What `!status` doesn't show:** termination protection (§5.4). The console does (§3.9).
- **Idle stop.** A `!status` doesn't keep a VM up: the bot reads it as `zab-monitor`, a user whose sessions idle stop doesn't count as logins (#507). A VM gets that user with its idle stop. Where that read fails, the bot reads the VM as `ubuntu` instead, a login that may delay its idle stop once.

### 3.7 Stopping and starting

- **`!stop all`** (phrase `stop <n>`) stops the VMs between launch day and the workshop. Idle stop would power them off after an hour anyway, but an idle VM still costs money (ADMIN §11.2). The disk, the address and the sign-ins survive. The spares stop by themselves (below).
- **`!start all`** starts them again, with no phrase.
- **The spares** are included only when you say so: `!stop spares`, or `spares` after `all`.
  - **Each spare stops by itself** (#715). It has no bots, so no waker, and `all` never names it, so it doesn't get the rows' idle stop: it gets its own, after 10 idle minutes, or the minutes of `spare_idle_stop` in your workshop's entry (ADMIN §2.1; ask the deployer to change it). A `!launch` with `spares=N` gives it to each spare it names, and every `!start` of spares (`!start spares`, `!start zab-spare-1`, `!start all spares`) gives it again, with the entry's minutes. These plans say `**The spares' idle stop:** after 10 idle minutes …`. Their step `aws/spare_idle_stop.py`, right after the launch or the start, waits up to 5 minutes for each spare's SSH, then says per spare `zab-spare-1 i-…: idle stop after 10 min (a spare's), installed`. In a `!launch`, a spare that exists and is stopped is skipped: `zab-spare-1 i-… (stopped): skipped: it gets idle stop at its next !start zab-spare-1`. After a `!start`, every spare named is waited for, since EC2 may still read a spare it was just asked to start as stopped. If a `!launch` with `spares=N` fails, the step doesn't run: send `!start spares` once the cause is fixed.
  - **Its rules are idle stop's** (§4.2): a spare stays up for its first 10 minutes after a start, while someone is logged in or was within 10 minutes, while its load is 0.5 or more, and while a `push_config.py` run holds it. It also stays up while the first boot's volume warm-up runs, about 15 minutes from its launch, since a stop would cut that off for good: its idle stop's verdict says `stay up: the first boot's volume warm-up is running`. So a spare stops by itself after its warm-up and its load have settled, usually within the hour after its launch, and within about 10 minutes of a later `!start`, unless it is adopted. A stopped spare costs only its disk and its address (ADMIN §11.2).
  - **A spare launched with v0.2.16 or older** has no idle stop of its own: `!start spares` gives it, and it then stops within about 10 minutes (CHANGELOG 0.2.17).
  - **`!adopt-spare` starts a stopped spare itself** (#715), so no spare needs to be kept running for a late joiner. Its dry run's output says so, `zab-spare-1: stopped, started for the adoption`, and its first step starts the spare, then waits until it runs and its SSH answers, 5 minutes at most, before it changes anything. The adoption takes about a minute longer than for a running spare. The row's idle stop then replaces the spare's (§3.5), under the hold that its `push_config.py` steps keep on the VM, so the spare's 10 minutes never stop it mid-way.
  - **Some spares are refused,** changing nothing, since the chain's second step connects to the spare and a failure after its first step needs the deployer (§4.1; #455). The dry run's output says why, and how long to wait:
    - a spare stopping, as one does at the end of its idle stop: `refusing: zab-spare-1 is stopping: request again in a minute or two, once it has stopped (this plan then starts it)`;
    - a spare starting: `refusing: zab-spare-1 is starting (pending): request again in a minute`;
    - a spare running whose SSH doesn't answer yet, just started: `refusing: zab-spare-1 is running, but its SSH (<address>:22) doesn't answer yet: request again in a minute`.

    A start that doesn't come up in the 5 minutes is refused at the apply too, with its own `refusing:` line, such as `refusing: zab-spare-1 was started, but its SSH (<address>:22) doesn't answer after 300 s: request again in a minute`: nothing changed but the start, and the spare stops itself again (`!status` doesn't read spares).
  - **When no late joiner can come any more,** `!teardown spares` (§5.8): a stopped spare still costs its disk and its address.
- **The dry run** says, for example, `bob i-… (running): would stop`, then `would stop 1, unchanged 0, failed 0`.
- **Running turns.** A stop interrupts them, and the plan lists them.
- **Neither waits for SSH.** About a minute later, `!status` shows `starting`, then `active, …`.

### 3.8 The workshop's first morning

1. **`!start all`.** Not the spares: `!adopt-spare` starts the one it gives a late joiner (§3.7), and a spare started now would only stop itself again 10 minutes later. Then send `!status all` until every VM reads `active, sign-in pending`, or `active, owner+sign-in pending` for a participant who hasn't joined Zulip yet. Idle stop keeps each VM up for at least an hour after its start; a VM that nobody uses then stops, and its participant's first message wakes it (§4.2).
2. **The deployer, or a Zulip organization administrator,** checks that the e-mail visibility lets members read addresses (§1.1).
3. **`!provision zulip all`,** without `hold`, releases the sandboxes. It subscribes each participant who has joined Zulip to their sandbox, and posts "Start here". It sends no invitations. Its report's `invite` column reads `joined`, or `not on Zulip` for a row whose address matches no Zulip user that `zab-provisioner` can see, with a line under the table, such as `alice: not on Zulip: invite alice@example.org in Zulip (…)`. Invite that address in Zulip, unless you have already (§1.5). If Zulip says the address is already in the organization, the participant hides it from moderators (§3.3). `not on Zulip` is not a failure: a later `!provision zulip` binds them once they have joined. **It must end `✅`** before a late joiner gets a spare: `!adopt-spare` provisions the spare held, as on launch day, until a `!provision zulip all` has succeeded (§2.4). If it fails, fix the rows it names, and send `!provision zulip all` again.
   Then send `!status all`. A participant it bound whose address their bots can't see shows `active, doctor FAIL` with `doctor: …:allowlist: no allowed user yet: …`: send `!rollout v0.2.15 alice` for that row (§3.5). The other rows need nothing.
4. **`!waker on all`** again, once `!provision zulip all` has ended `✅`. It gives the waker the Zulip IDs of those who joined Zulip between launch day and now (§3.5). Most invitees join later.
5. **`!provision repos invites-only all`** sends the invitations for the GitHub logins in Zulip profiles. It is quick and makes no SSH connection. Send it again at the morning break and after lunch, for those who fill in their profile's *GitHub* field later: "Start here" tells them that GitHub then e-mails them an invitation. When it has nothing to send, withdraw or record, as when nobody has added or changed a GitHub login or accepted an invitation since the last run, there is no plan to approve: ``☑ **P-76** · `!provision repos all invites-only`: nothing to change, as its dry run says; no plan to approve``, with its summary under it, such as `invites: 0 invited, 0 accepted, 0 removed, 0 failed; 44 unchanged (dry run)`, then `nothing to change: no invitation to send or withdraw, and no acceptance to record` (§2.2). The summary still names any row whose login change is refused or that isn't provisioned yet: those need a deployer, or a `!provision repos` without `invites-only`.
   **At the same breaks, and at the breaks of each later day,** send `!provision zulip all`, then `!waker on all`. The first binds the participants who have joined since, and hands them their sandboxes. The second gives the waker their Zulip IDs (§3.5). Each full `!provision zulip` also sets every sandbox's description back to the provisioner's, and subscribes its participant and bots again, so a participant's own edit of their sandbox's description doesn't last. That is accepted: the description holds the workshop's steps. Then `!status all`, and `!rollout v0.2.15 alice` for a row it bound that shows `active, doctor FAIL` with `doctor: …:allowlist: no allowed user yet: …` (step 3; `!start alice` first if it is `stopped`).
6. **The gifts, then the announcement.** Where sponsors give the participants plans, the announcement tells them to accept those first; where they give none, it says so, since "Start here" asks them to accept any gifts (§1.6). Post in the workshop's participants' channel, the one that `channel=` named when the workshop was made (§3.1), not in `#announcements`, which reaches every workshop in the organization: each participant opens their channel `<slug>-sandbox` and starts a topic.
   - **In a channel where they are the only person,** both of their agents answer every message without a mention, whatever the topic's earlier messages addressed (#687, #713): an agent without a session in the topic starts one. A mention of one bot talks to that one alone; a Codex not signed in stays out, and a Claude not signed in may answer that it isn't. A command without a mention goes to both; an agent it doesn't concern stays quiet (a stop with no turn running, a model only the other agent knows), and the help, VM and tools lists come once (SPEC §4.6).
   - **In a channel with anyone else,** a collaborator or an organizer who joined to help, every message needs a mention of the agent it is for, a new topic's first message included.
   - **After the multi-party kill switch** (§4.1), a message without a mention reaches only the agents that have a session in its topic, in every channel: a new topic's first message needs a mention.
   - **A typed `@claude` or `@codex`** is a mention of the participant's own Claude or Codex, whether or not they pick the bot from Zulip's list, anywhere in the message except in code or a quote (#713).

   A workshop without a participants' channel posts where all its participants are, such as `#announcements` while it is the organization's only workshop. Then post the participant quick-start, [`docs/quickstart.md`](quickstart.md), which is written in Zulip markdown. It names a GitHub organization (`MathAgents/<you>`), a file host, gift plans, and the 2 minutes after which a turn that ends mentions its owner (`mention_after_s`, §1.1): where your workshop's differ, edit your copies of its two parts, below, before you post them. It is longer than one Zulip message, which holds 10,000 characters, so post it as two: from its start to just before its heading `## Commands (type them in the topic)`, then from that heading to its end. Each part is under 10,000 characters (a test checks it). From a checkout of the release, these write the two parts and print their lengths:

   ```sh
   cd docs
   awk '/^## Commands /{exit} {print}' quickstart.md > ~/quickstart-1.md
   awk '/^## Commands /{p=1} p' quickstart.md > ~/quickstart-2.md
   python3 -I -c 'import sys
   for f in sys.argv[1:]: print(f, len(open(f, encoding="utf-8").read()), "characters")' ~/quickstart-1.md ~/quickstart-2.md
   ```

   Part 1 starts `# Workshop quick-start`, part 2 `## Commands (type them in the topic)`. Post part 1, then part 2, each as a message of its own: open the file in a text editor, select all, copy, and paste into the compose box. The quick-start tells participants that their VM sleeps when idle and wakes on their next message, usually 15–20 seconds later.

### 3.9 The console: the fleet on one page

The console (ADMIN §4) shows your workshop's fleet on a web page that refreshes itself every minute: what `!status` shows, for every VM at once, with the cost. It changes nothing: every change is still a command and a plan in your channel.

- **Signing in.** Send `!console` to your workshop's bot, in your organizers channel or by DM. The bot sends you a link by DM, `https://<host>/login#…`, where `<host>` is your server's console, and says in the channel `sent you a sign-in link by DM`. Open it at once, within 5 minutes: Zulip's missed-message e-mails and phone notifications can carry the link while it is valid. It signs you in once, in that browser, for 12 hours. A link used already, or too old, opens the sign-in page with the reason: send `!console` again. Don't forward a link: whoever opens it first is signed in as you.
- **What you see.** Your workshop only; a global admin sees every workshop of the server, one row each. On a workshop's page:
  - the totals: VMs by state, who has signed in to which agent, zab versions, VMs under strain or unreachable, doctor failures, idle stop on, waker opted in;
  - the cost: this month and projected to the month's end, and the running hours today. These are estimates (ADMIN §11.2): AWS's bill is the exact figure;
  - the open plans;
  - one row per VM, **problems first**: unreachable, then doctor failures, then strain. CPU, memory and disk are bars, green to red, in the colours the participants' bots use for them. The disk reads as its use of its size, such as `152 of 194 GB (78%)`. **Every GB is binary** (1024³ bytes), for memory and disk alike, in the console, `!status`, `status.py` and the participants' `!vm`, as `df -h` counts them: the fleet's 200 GB volume holds a file system of 194 GB, which reads `of 194 GB` here and `194 GB disk` in `!vm`. The percentage is `df`'s, used ÷ (used + available), rounded up, so it can be a little above used ÷ size. A VM's page has everything known of it, and the commands to copy into your channel, such as `!status alice` and `!stop alice`;
  - the last actions of the audit log.
- **How fresh it is.** EC2's state is read every minute, and a warning at the top of the page says when it hasn't been. The rest (zab version, sign-ins, CPU, memory, disk, doctor failures) comes from the last `!status` that read the VM, and from the console's own read, every 10 minutes, of a running VM that a `!status` has shown without idle stop. Each row says how old its values are.
- **Refreshing it.** A `!status` brings the VMs it reads up to date. It keeps none of them up, except one the bot can't read as `zab-monitor` and reads as `ubuntu` instead (§3.6; ADMIN §4.1), so it is safe to repeat; still, read the VMs you need, such as `!status alice bob`: `!status all` waits for every running VM.
- **Signing out.** On a computer you share, press **Sign out** at the top of the page. It ends the session in that browser only: a session can't be ended from elsewhere, and lasts 12 hours. A deployer can end every session at once (ADMIN §4).
- **Removed from the organizers?** The console signs you out at once.
- **`!status` stays** the way to look at the fleet from your phone, and when the console is down. On a server without a console, `!console` answers `❌ the console is not set up on this server yet`.

## 4. During the workshop

### 4.1 Common problems

The fixes of the common problems, through the bot:

| Problem | What to send |
|---|---|
| A participant's bots ignore their `!login`. | Their Zulip e-mail differs from the roster's. Compare it with their Zulip profile. If your invitation to the roster's address is still open, revoke it first (§1.5). Then correct the e-mail with `!roster import` (§3.1), then send `!provision zulip alice`, then `!rollout v0.2.15 alice`, then `!waker on alice`, which gives the waker the corrected participant. |
| Someone joined Zulip after the morning's run. | If `!status alice` shows `stopped`, send `!start alice` first, and the rest a minute later: `!provision repos alice` connects to the VM (§4.2). Then `!provision zulip alice`, then `!provision repos alice`, then `!waker on alice`, which records their Zulip ID in the waker (§3.5). If `!status alice` then shows `active, doctor FAIL` with `doctor: …:allowlist: no allowed user yet: …` (their address is hidden from members, §3.5), `!rollout v0.2.15 alice`. |
| A participant can't see their repository. | Ask them to correct the GitHub field of their Zulip profile. Then `!provision repos invites-only alice`. They accept at `https://github.com/<org>/<slug>/invitations`, or from GitHub's e-mail. |
| `git push` is rejected. | `!provision repos alice` sets up the deploy key again. |
| A late joiner needs a VM. | With a participants' channel, once they have joined it, `!roster sync`; otherwise send the whole CSV with their row added (`!roster import`, §3.1). Then `!adopt-spare dave zab-spare-1` (`dave` is the new row). A stopped spare is started for it: its dry run says `zab-spare-1: stopped, started for the adoption`, and the adoption takes about a minute longer. A spare stopping or starting is refused, with the wait it needs, and nothing changes (§3.7). It runs the whole chain of §7.2 ("A late joiner") and gives the VM what the fleet has (§3.5): the release and its settings, the shared Mathlib, the waker, and then idle stop, the row's in place of the spare's. Its first `push_config.py` step runs before `provision_repos.py` clones the new repository, so it reports that checkout as pending (`ok (project+sign-in pending)`); the last step, the second `push_config.py`, checks it, and fails on `doctor: project:…` if the repository step made none (#458). Then they send `!login` to both bots. **If `!roster show dave` then lists no Zulip ID** (a CSV's row: a synced row always has one), they hadn't joined Zulip yet: invite them in Zulip (§1.5), with the participants' channel ticked, unless you have already; once they have joined, send `!provision zulip dave`, then `!waker on dave`, as in the row above. **If it fails at its first step** (`failed at step 1 of …: aws/adopt_spare.py exited 1`) and its output has a `refusing:` line, for example because the spare was stopping at the apply, or didn't come up within 5 minutes of its start, nothing changed but that start: do what that line says, then request again. Without a `refusing:` line (for example `roster NOT written`), the spare may already be the row's: give the output to the deployer. **If it fails after its first step,** the spare is already the row's, so the same command is refused. Don't send it again: give the deployer the step the result names, to finish the chain by hand, and send `!start dave` first: the VM may have stopped meanwhile, since the hand-over's hold lasts an hour and the spare's own idle stop may still be on it. If that step is the waker's (`admin/push_waker.py`) or a later one, send `!waker on dave` yourself (its dry run shows whether the cause is gone): the deployer then has only the `push_config.py` and `provision_repos.py` steps left. **If a fleet-wide run can't succeed** (§2.4), for example because of a broken VM, the spare gets what the last successful one gave: then send the missing step for the new row too, such as `!provision zulip dave`, `!waker on dave` (right after the adoption: the chain's own connections keep the VM up for about the idle-stop timeout, §4.2), `!idle-stop 60 dave` or `!rollout v0.2.15 dave multi-party=off`. |
| A participant wants a share link revoked after their VM ended. | Ask a deployer: `server.py unshare <slug> '<link>'` on the files host (`workshop/files/README.md`, *Share links*). While the VM runs, the participant asks one of their agents instead. |
| A VM is broken. | Call the deployer (§7.2, "A broken VM"). `!adopt-spare` only serves a row that has no VM. |
| Agents misbehave in channels shared by several participants (a multi-party bug). | `!start all`, then `!status all` until no row shows `stopped`, then `!rollout v0.2.15 all multi-party=off`: a VM that is stopped at the rollout keeps the bug when it wakes (§2.4). Every bridge restarts, which interrupts running turns, so send it at a break. Send it with `all`, and again until it ends `✅`: a spare adopted later gets the switch only from a successful `!rollout` of `all` (§2.4). Every later rollout must say `multi-party=off` again (§3.5). **The switch also limits #713's rule** (§3.8, step 6): a message without a mention then reaches only the agents that have a session in its topic and starts none, even in a channel where the participant is the only person, so a new topic's first message starts no agent, and the waker's ⏰ on such a message is followed by no answer (§4.2). Tell the participants to mention their agent in each new topic, and to name the other agent to bring it into a topic. |
| A Claude answer reads "(no final message)". | `!rollout v0.2.15 alice raw-log=meta` restarts that one bridge. Tell the deployer, who collects the record. |
| A participant's bots are silent. | Ask them to send `!vm` in a direct message to either bot. The VM's sentinel answers even when the bridge doesn't, and says whether it is running. Then `!restart` in that DM: it restarts their bridge (running turns are interrupted) and says when it is back. While a rollout or a final sync of yours holds the VM, `!restart` is refused (`⏸ Nothing was restarted: your organizers are working on this VM …`): their bridge comes back when that ends. If `!vm` gets no answer at all (a stopped VM's waker answers `⏳ Starting your VM…`, §4.2), send `!status alice` and call the deployer. **If `!status alice` shows a sentinel other than `sentinel ok`,** call the deployer. In a down state (`sentinel down: DM !restart is unavailable, …`, or `no mark`, `stale`, `not installed`), their DM `!restart` restarts nothing: the bridge, if it runs, answers `Nothing was restarted: …`. `unknown` or `warn` means that the read couldn't tell. |
| A VM is slow. | `!status alice`: look at the running turns, and at the load, memory and disk (`under strain: …`). Idle Lean servers under Codex hold memory, about 1.3 GB each: zab stops each one after 30 idle minutes (`[lean] reap_idle_minutes`; one `lean_reaped` line each in `~/.local/state/zab/logs/zab.log`), and the participant can free them all at once with `!restart codex` in a DM to either bot, which interrupts a running Codex turn. |
| A participant's disk is full or filling (`!status alice` shows `disk 96% (8 GB free)`, under strain). | `!grow-disk alice 300GB`: their VM's disk grows to 300 GB, from launch's 200, at $0.08 a month per GB added, at most 1000GB. One approver. A running VM grows at once, without a restart, and the result ends `alice: root file system now: …` with the new size. A stopped one grows on EC2 now, and its file system at its next start (`the VM is stopped: its file system grows to 300 GB at its next boot`). The plan is refused, and nothing changes, when the size is smaller than the disk's (a disk never shrinks), or the same on a stopped VM, when the disk was grown less than 6 hours ago, for a larger size (EBS's limit: the refusal names the time to request again), or when the VM is starting or stopping (request again in a minute). If the result fails but its last line says `alice: the volume is 300 GB…; its file system grows at its next boot …`, the disk is larger all the same, and its file system not yet. **First, while the VM runs, send the same `!grow-disk alice 300GB` again**: at the disk's own size it grows the file system alone, over SSH, at no cost and without the 6-hour wait; its plan says `would grow its file system over SSH (no cost)`. Only if that fails too, at a break, send `!stop alice`, then `!start alice`, and the start grows the file system. The stop interrupts the participant's running turns. That covers most failures after the change; the others end with their own line, which says what to do, such as a change still `modifying` after 240 s, or a VM that was starting or stopping. While the bot is down, a deployer grows it by hand (§7.1). |
| No participant's message gets a ⏰ or a `⏳ Starting your VM (about 30 seconds)…`: nobody's VM wakes. | The waker is down, as on Nov 1 for 90 minutes (#717). The bot says so in `#harvard-2026-fleet-alerts` once it has been down 5 minutes (§4.2), and each `!status` then ends with `⚠ waker down since …: no stopped VM wakes for its participant's messages. …`. Send `!start alice` for each participant who is waiting, or `!start all` (a VM nobody uses stops again after its idle timeout), and call the deployer, who looks at the waker's journal (ADMIN §1.8). |
| A participant's VM doesn't wake. | Look in `#harvard-2026-fleet-alerts` for a failed start, and send `!start alice` meanwhile. If `!roster show alice` lists no Zulip ID, send `!provision zulip alice` first. Then send `!waker on alice` (§3.5). Only their own messages wake it, and only those that one of their bots receives (§4.2). |
| A participant's long job was stopped with its VM. | A job that keeps less than half a CPU busy after its turn ends doesn't hold the VM up (§4.2). Before they start it again: `!idle-stop 240 alice` (the longest timeout), or `!idle-stop 0 alice` for none, which later rollouts leave off (§3.5). Set it back afterwards with `!idle-stop 60 alice`. |

### 4.2 Idle stop and the waker

**Idle stop** powers a VM off when nobody has used it for a while, and **the waker** starts it again when its participant writes to one of their bots (`workshop/admin/README.md` §8, [`workshop/waker/README.md`](../workshop/waker/README.md)). Every workshop runs both on every VM, from launch day (§3.5) to its end (§5.2), and the kept VMs keep both for their month (§5.7). They are what keeps the budget (ADMIN §11.2).

**The waker first, then idle stop,** on every VM, so that no VM ever stops without a way to wake it. Launch day sends `!waker on all`, then the rollout, which turns idle stop on only where the waker is on (§3.5). `!adopt-spare` does the same for a late joiner's VM (§4.1). For named rows: `!waker on alice bob`, then `!idle-stop 60 alice bob`. Without the waker, a stopped VM starts only through `!start`.

**The waker: `!waker on alice bob`,** or `all`.

- **Afterwards,** any message from the participant that one of their bots receives starts their stopped VM: a DM, or a message in a channel their bots are in, mentioned or not. A DM is answered `⏳ Starting your VM (about 30 seconds)…`; a channel message gets a ⏰ reaction from each bot it is for, and the answer follows once the VM is up. The waker reads a message as for each bot it mentions, a typed `@claude` or `@codex` included; with no mention, for both (#713). Only the bridge knows who else is in the channel, whether the multi-party kill switch is on (§4.1), which agents are signed in and which have a session in the topic. So in a channel with other people, a message without a mention gets its ⏰s and then no answer; after the kill switch, so does a new topic's first message without one, in any channel, and the agent without a session in a topic gives its ⏰ there and stays silent (§3.8, step 6); and an agent not signed in may give its ⏰ and stay silent. On the test VM, the answer came 15.5 s after a DM and 19.6 s after a mention.
- **Only the participant wakes it:** the Zulip user their row had at the row's last `!waker on`, or only their e-mail if it had no Zulip ID then (§3.5). Nobody else's messages, the organizers' included, start a VM.
- **The wake monitor** checks every start against the participant's messages, and alerts the global admins about a start that none explains (ADMIN §1.2). It can't check the starts of a row without a Zulip ID, so each of those alerts too, until `!provision zulip` binds the row.
- **A failed start** is reported in `#harvard-2026-fleet-alerts`.
- **A waker that is down,** for 5 minutes, as on Nov 1 (#717), is reported there too, once, by the workshop's bot: `⚠ **waker down since …:** no stopped VM of harvard-2026 wakes for its participant's messages. …`. When it serves again, it says `✅ **waker back** at …: stopped VMs wake again for their participants.` Meanwhile `!status` ends with the same warning, the console shows it on the workshop's page, and you start the VMs of participants who wait (§4.1).
- **Without that channel** in the workshop's configuration, it is refused: `this workshop has no alerts_channel in admin.toml: !waker on refused`.
- **`!waker off alice`** opts a VM out.
- **While the server is down,** nothing wakes (§8.2).

**Idle stop: `!idle-stop 60 alice bob`,** or `all`, or `idle-stop=60` in a rollout. A rollout without `idle-stop=` turns it on at 60 minutes where the waker is on and no timer is on, unless it was turned off on purpose (§3.5).

- **60 minutes** is the default. The timeout is 10 to 240 minutes, and `0` turns idle stop off. Above half the fleet, it takes the phrase `idle-stop <n>`.
- **It touches only idle stop:** the bridge is not restarted.
- **What keeps a VM up.** Once a minute, the VM powers itself off unless one of these holds (`hold_reasons` in `workshop/admin/idle_stop.py`; `workshop/admin/README.md` §8):
  - it has been up for less than the timeout, so that a VM just started or woken answers the message that woke it;
  - an agent's turn is running, however long it takes: a computation the agent waits for keeps the VM up until it ends;
  - zab took in a message for the agents, or a turn started or ended, within the timeout. A participant's commands to their bots, such as `!status`, and other people's messages in a shared channel, don't count;
  - the 5-minute load average is 0.5 or more: a computation that runs on after its turn keeps the VM up while it keeps about half a CPU busy;
  - someone is logged in, or was within the timeout. It sees a login only when one is open at its once-a-minute check, so don't rely on a short one. The admin bot's `!status` and the console's reads are not logins (#507; §3.6);
  - a `!rollout`, `!adopt-spare`, `!idle-stop` or `!final-sync` is under way on it (the sync only while it syncs that VM);
  - something it must read can't be read: it never stops a VM it can't check.
- **What doesn't keep it up:** a background job that mostly waits, on the network, on a file or in a sleep, uses almost no CPU. The power-off ends it. For such a job, give the row a longer timeout or none, and set it back afterwards (§4.1). A row's own timeout doesn't change what `!adopt-spare` gives a spare (§3.5).
- **A stopped VM keeps** its disk, its address and the agents' sign-ins. Nothing that ran on it survives the stop, the agents' Lean servers included. After a wake, the first Lean check reads Mathlib from disk again: up to about two minutes (110 s for `import Mathlib` on the test VM), then seconds. Every start is followed by a read of Mathlib into memory in the background (#461), done about 3 minutes after the start (`workshop/admin/README.md` §2.3). On the test VM, `import Mathlib` then took about 85 s when started right after the wake, and about 10 s once the read was done. The same read covers Oscar's files (#475), after Mathlib's, on a VM whose participant used Oscar in the last 7 days; elsewhere it skips them. On the test VM, a cold `using Oscar` took about 15 s longer than a warm one.
- **Commands that connect to the VM fail on a stopped one:** `!rollout`, `!idle-stop`, `!provision repos` (not `invites-only`), `!final-sync`, `!export` and `!uninstall`. When `!status` shows it `stopped`, send `!start alice` first, and the command a minute later. For `all`: `!start all`, then `!status all` until no row shows `stopped`, then the command; §2.4 says what to do when a VM is stopping, or stops again.

### 4.3 What a participant's admin can break

Each participant administers their own repository (§3.4). Most of what they can do there is theirs to do. What breaks the workshop's side, and its repair:

- **The VM's deploy key deleted** (*Settings → Deploy keys*): every push from the VM fails, the agents' file links and the final sync included. `!provision repos alice`, a full one and not `invites-only`, registers it again.
- **The repository archived:** every push fails until the participant unarchives it (*Settings → General*, at the bottom). Nothing on your side repairs it.
- **Renamed:** GitHub redirects git to the new name, so the VM's pushes go on. Links already posted carry the old name, and keep opening for what the files host had already copied. New file links carry it too, and don't open: the files host fetches by the name in the link, and doesn't follow GitHub's redirect. Renaming it back repairs them.
- **A collaborator's push to `main` or a `zab/…` branch:** the final sync pushes without force, so its push of that branch is rejected, and `!retire` stops at that row's sync (`final-sync` reads `failed: exit 1`, and the output names the branch's `push rejected`). Ask a deployer to merge the collaborator's commits on the VM by hand, then send `!retire alice` again; or `!retire alice unsynced`, which loses the VM's commits on that branch.
- **Deleted, or transferred to another account:** the files host usually drops its copy within about an hour (not always: §5.9), the agents' new file links stop opening, and the final sync refuses the row. §5.3 says how `!retire` reports it; §5.9 lists every copy, for a deletion request. A transferred repository keeps its deploy key, and GitHub redirects git to its new place (GitHub's *Transferring a repository*): the VM's pushes would reach the participant's own account, but the final sync doesn't push there, since the repository is no longer the organization's.

## 5. The end of the workshop

After every workshop, participants who opt in keep their VM for one month, to W+1 month. The other VMs end the day after the workshop, at W+1, so that no requests to the organizers trail on (ADMIN §1.1; §10, Q14).

**One command ends a VM: `!retire`** (#559). `!retire alice bob` ends the named rows, each in turn: their work goes to GitHub, then their VMs and their bots go. It names the rows that end, copied from `!roster show`, and never takes `all`. The kept VMs, which it doesn't name, keep idle stop and the waker for their month, and end the same way then (§5.7). The separate verbs stay, for repairs (§5.8).

### 5.1 What `!retire` does

For each row it names, one after the other, it runs the steps of the separate verbs, for that row alone:

1. **start** the VM if it is stopped, and wait until it answers;
2. **idle stop off**, then **the waker off** (as `!idle-stop 0 alice`, then `!waker off alice`): no VM stops, or wakes, in the middle;
3. **uninstall** (`!uninstall`): the agents stop, so that no new work arrives after the sync;
4. **final sync** (`!final-sync`): every participant's work goes to their repository on GitHub;
5. **export** (`!export`), only when the command says `export` (§5.6);
6. **protection off** (`!protect off`), and a wait until EC2 shows it, for a VM that was protected;
7. **teardown** (`!teardown`): the VM is terminated, its disk deleted and its address released;
8. **bots** (`!deactivate-bots`): the participant's bots, and so their keys, are deactivated.

- **A row stops at its first step that fails,** and the other rows go on. A row whose sync fails is not torn down: its work is not on GitHub. Its VM and bots stay as they are, and the result says how to go on (§5.3). So does a row whose protection can't be turned off.
- **`unsynced`** tears a row down even when its start, uninstall, sync or export fails (§5.3). Its work that is not on GitHub is then lost. It covers every row of the plan, so retire the rows that need it in a plan of their own.
- **The VM it syncs is the VM it tears down.** The bot looks each row's VM up twice: as the steps reach it (the roster's address, or a `participant` tag in any case), and as the teardown ends it (the tag `participant=<slug>`, exactly). When the two differ, as for a VM handed over by hand that keeps a spare's tag, the plan is refused, naming the row: ask a deployer to fix the VM's tag, with ADMIN §3.3's commands. So is a row with two VMs. The plan shows each row's VMs as found.
- **The bot remembers each row's uninstall and sync,** as for the separate verbs (§5.8). A row that ran through every step is *retired*:
  - `all` leaves it out from then on, for every command.
  - Named, it is refused by every command that would reach its VM, since its address was released and may be another VM's by now: `!start`, `!stop`, `!rollout`, `!idle-stop`, `!uninstall`, `!final-sync`, `!export`, `!protect`, `!provision repos`, `!grow-disk` and `!waker on`. `!status alice` still reads it.
  - **To bring it back into service,** a deployer first clears its `host` and `ip` in the roster on the server (ADMIN §3.3). Then `!launch alice` or `!adopt-spare alice zab-spare-1`. Until then both are refused: with them, the launch would leave the row alone, and the hand-over refuse it.
  - **Its bots stay deactivated.** Only a Zulip organization administrator can reactivate them. `!provision zulip alice` can't make new ones in their place: the deactivated bots keep their e-mail addresses, and new bots of the same names would need them.
  - It no longer counts in *the fleet* of §1.2's phrase rule.
  - `!status all` names it once, under its answer: `*Retired, so left out: alice (P-50 at 2026-10-29 14:02 UTC).*` When every row is retired, as at a kept fleet's month end, it answers `Every row is retired: nothing to read.` and names them.
  - The console leaves it out once its VM is gone.

### 5.2 Before you start

1. **Know which rows end:** every row whose participant hasn't opted in to keep their VM.
   - **Collect the opt-ins** by the workshop's last day, W. Ask in the participants' channel who wants to keep their VM for the month, and have each tell an organizer, by a reply or a DM.
   - **Write the kept slugs down,** from `!roster show`, in a topic of your organizers channel such as `kept VMs`. That list is what you protect now, what every `!retire` of W+1 leaves out, and what ends at the month's end (§5.7). The bot keeps no list of its own: termination protection is its only record of a kept VM.
   - **Copy the rows that end** from `!roster show`: all the others. A `!retire` never takes `all`: ``❌ name the rows that end, never `all` (!retire slug… [export] [unsynced])``.
2. **Protect the kept VMs** well before: `!protect on carol`.
   - **The result says per VM** `termination protection on: set; EC2 shows it now`, or `… set; EC2 doesn't show it yet (it can take a few minutes)`.
   - **If EC2 doesn't show it yet:** a few minutes later, send `!protect on carol` again. Its dry run says `unchanged (protected, stops on shutdown)` once EC2 shows it. Then `!reject` that plan.
   - **A `!retire` lifts the protection of the rows it names.** Protection keeps a kept VM out of a `!teardown`, not out of a `!retire` that names it. So read the plan's rows (§5.3): a kept row must not be among them.
   - **The 5-minute wait** after a `!protect` holds a `!retire` back too (§5.4): protect well before.
3. **Remind the participants** to sign their VM out of Codex and Claude: zab never handles the agents' credentials, so no command does it for them (`workshop/admin/README.md` §7.3). They can ask one of their agents to run `codex logout` and `claude auth logout`. Do it before the `!retire`: after its uninstall, no agent answers. The plan reminds you too.
4. **A global admin, or two organizers,** must be at hand to approve it.
5. **Plan the batches** (#564):
   - **first, one row alone,** to get real timings: the result's `time` column says how long each row took;
   - **then check its work on GitHub,** before any batch: open the participant's repository (`<org>/<slug>`, the `repo` column of `!roster show`), and check that `TOPICS.md` is on `main`, with a row per topic, and that their branches are there;
   - **then batches of about 8 rows.** While a batch applies, it holds the workshop's lock: every other approval in the workshop is answered `busy:`, the kept participants' `!start` and `!stop` included. A batch of 8 takes a few minutes a row, and stopped VMs boot together at its start;
   - **the rows that need `unsynced`** (no VM found, never launched, a sync that can't succeed) in a plan of their own, so that `unsynced` never covers a row whose work could be saved;
   - **the rows to export,** at most 3, in a plan of their own with `export`.

### 5.3 Step by step: `!retire alice bob`

The examples end `alice` and `bob`, and keep `carol`.

1. **Send `!retire alice bob`.** It is destructive: it needs two approvers, or a global admin alone, and the phrase `retire 2` (§1.2).
2. **Read the plan.**
   - **The rows:** `**Rows that end** (2), …`, then one line per row: its address, its bots, and what becomes of its work, such as `alice: VM at 198.51.100.1; Claude · alice and Codex · alice; synced, then torn down`. Check that **no kept row is there**.
   - **The rows' VMs, as the bot found them,** at the end of the dry run: `alice: i-…`, or `bob: no VM found`. A row with no VM found is refused unless the command says `unsynced` (below).
   - **The dry run** is the teardown's, for every row: one `terminate i-… ws-alice (running, participant alice)` line per VM, one `release eipalloc-… <address>` line per address. A VM listed `keep i-… ws-…: termination protection is on (…)` is protected: the `!retire` turns its protection off, then terminates it. That is right for a kept VM at its month end (§5.7), and a mistake the day after the workshop.
   - **The status** of the rows' VMs: the running turns that the uninstall will cut, and the VMs stopped, which it starts.
   - **Before you approve:** the plan's last note reminds you to have the participants sign out (§5.2).
3. **Approve** with the line the approval message shows, phrase included: `!approve P-50 3f9a2c retire 2`. An organizer who approves first gets `✅ P-50: 1 of 2 approvals, from distinct approvers`; the second approver gets `✅ P-50 approved (2 distinct approvers)`, and a global admin who approves first (or alone) `✅ P-50 approved (a global admin's approval)`.
4. **The apply** goes row by row. Its progress names the step and the row: ``… applying, step 4 of 17, admin/push_waker.py for alice`` (step 1 starts every stopped VM at once; then 8 steps a row, 9 with `export`). A row takes a few minutes; a stopped VM, a minute more. The apply stops at 60 minutes, or 20 minutes a row above three, and 55 minutes more a row with `export` (§2.4).
5. **The result** is one table: one row per participant, one column per step (`ok`, `skipped: no VM`, `failed: exit 1`, `not run`), the row's `time`, and `retired`:
   - **Every row through:** ``✅ **P-50** · `!retire alice bob`: done in …, 2 of 2 rows retired``.
   - **A row torn down under `unsynced` after a failure:** ``✅ **P-50** · `!retire alice bob unsynced`: done in …, 2 of 2 rows retired; torn down unsynced: bob (final-sync failed)``. Tell bob that his work was not saved.
   - **A row stopped:** ``❌ **P-50** · `!retire alice bob`: 1 of 2 rows retired; bob stopped at final-sync. The other rows went on; send it again for those stopped``. Read that row's cells: the step that failed reads `failed: …`, and the steps after it `not run`. The output below the table, and its attachment, say why.
   - **Under the table, a line for each row stopped:** what its VM is left as, how to send it again, and the commands that put it back in service instead, such as `- bob stopped at final-sync (`failed: exit 1`): its VM runs, with idle stop off, the waker off, its bridge uninstalled. Fix the cause, then send `!retire bob` again, or `!retire bob unsynced` to tear it down with its work.`, then `To put it back in service instead: `!waker on bob`, then `!rollout v0.2.15 bob idle-stop=60`.` The rollout names `idle-stop=60` because the retire turned the row's idle stop off on purpose, and the default leaves it off (§3.5). A VM left running with idle stop off runs, and costs, until you do one or the other.
   - Then the reminder: `Remind the participants to revoke their other sessions in their ChatGPT and Claude account settings.`
   - **A result too long for one message,** with many rows stopped, keeps its first line and the reminder, and attaches the whole of it, table included.

**When a row stops,** fix the cause, then send the `!retire` again for that row alone. Every step is safe to run again: a VM already uninstalled, synced, unprotected or torn down is left as it is.

- **Its sync failed** (`final-sync` reads `failed: …`), as for a row without a repository: fix the row and send `!retire bob` again, or tear it down with its work: `!retire bob unsynced`, with the phrase `retire 1 unsynced`. Whatever of bob's work is not on GitHub is then lost.
- **Its repository is gone** (`final-sync` reads `failed: repository gone`): the participant deleted it, or transferred it to another account. The sync's line says `<org>/bob no longer exists here: the participant deleted or transferred it`, and nothing was pushed. The line under the table says it too, and points to `!retire bob unsynced`, with the phrase `retire 1 unsynced`: that tears the row down all the same, and loses the work that is only on its VM, whatever wasn't pushed before the repository went. If the participant wants that work, they restore or transfer back the repository first (an owner of the organization can restore a deleted one for 90 days, from its *Settings → Deleted repositories*; one transferred, only its new owner can transfer back), then send `!retire bob` again.
- **Its protection or its teardown failed:** EC2 refused the change, or still showed the VM protected after 5 minutes. Send `!retire bob` again a few minutes later.
- **It stopped at its teardown or its bots,** so that its VM may be gone already: send `!retire bob` again; if that plan finds no VM for it, `!retire bob unsynced`.
- **A row that was never launched,** with no `ip` in `!roster show`, can't be uninstalled or synced, and needs `unsynced`: `!retire dave unsynced`. Without it, the plan is refused: `dave never launched, or torn down already (no ip in the roster): nothing to uninstall or sync; add `unsynced` to retire it (its bots, and any VM with its tag, go)`. A row whose VM a `!teardown` or `!retire` ended reads the same: its address was released, so its `ip` was cleared (#604). Its bots go, and so would a VM with its tag that a launch cut short left behind.
- **A row whose VM is already gone,** terminated by hand, as bob's might be: `!retire bob unsynced`. Nothing needs the VM any more, so those steps read `skipped: no VM`; its bots go, and it is retired. Without `unsynced`, the plan is refused: `no VM found in us-west-2 for bob: add `unsynced` to retire it (the bots go, and the row is marked retired), in a plan of its own; or check the roster's ip and region`.
- **The rows to export** (§5.6): `!retire alice export`, in a plan of their own. One VM's export may take up to 55 minutes, so a `!retire` with `export` takes at most 3 rows.
- **The spares:** `!teardown spares`, with the phrase `teardown <n>` (§5.8). They have no participant, no work and no bots.

### 5.4 The 5-minute wait after `!protect` (#378)

EC2 shows a change of termination protection minutes late, and enforces it late too: 157 seconds, once, on the test rig (#378). A VM protected just before a teardown could still be terminated. So, after an apply of `!protect`, or of a `!launch` with `protect`, the bot refuses any `!teardown` or `!retire` plan of the workshop for 5 minutes:

- **Refused, with the time to try again:** `❌ P-51 refused: P-49 (!protect) changed termination protection here at 14:48 UTC, and EC2 can take minutes to show and enforce that: no !teardown or !retire for 5 minutes after it. Request it again from 14:54 UTC; nothing was done`. Send the same `!teardown` or `!retire` again from that time.
- **While the `!protect` is still applying:** `❌ P-51 refused: P-49 (!protect) is changing termination protection here now, …. Request it again 5 minutes after its result; nothing was done`.
- **A teardown planned before the `!protect`** and approved less than 5 minutes after it becomes STALE when it starts. Nothing is terminated: `❌ P-50 is stale: it no longer builds: P-49 (!protect) changed termination protection here at 14:48 UTC, …. Request again.`
- **A `!retire`'s own protection change doesn't count:** it only lifts the protection of the rows it then tears down.
- **A protection changed outside the bot,** by a deployer by hand, isn't known to the bot. Wait until the deployer says that EC2 shows it.

### 5.5 STALE teardown and retire plans

A teardown plan's hash covers its dry run and each row's end of the workshop; a `!retire`'s, its dry run and each row's VMs. If one changes between the plan and the approvals, the plan turns STALE when it starts, and nothing is terminated:

- **a VM was protected or unprotected, or an address appeared or went:** the dry run differs;
- **a row's VM appeared or went,** for a `!retire`: `❌ P-50 is stale (hash now …; since the plan: dave: no VM → i-…): nothing was done. Request again.`;
- **a plan named one of its rows,** such as `!start` (§5.8), for a `!teardown`: `it no longer builds: no !final-sync after its !uninstall on record for …`. With `unsynced`, the plan still builds, and the post names the row whose state changed: `❌ P-50 is stale (hash now …; since the plan: alice: synced after its uninstall → uninstalled, not synced since): nothing was done. Request again.`;
- **a `!protect` within the last 5 minutes** (§5.4).

Read the reason, ask for the teardown or the `!retire` again, and read the new plan.

### 5.6 Exports

- **Where they are.** The archives stay on the server, in the workshop's `out/exports/`. The server's backups leave them out, so a lost server loses them (§8.2).
- **Who gets one.** Only a participant who asks. A deployer fetches the archive and gives it to them alone, by DM or as a private link (`workshop/admin/README.md` §7.2).
- **What gets stored** (#374). Never the participant's own archive, which is always deleted. The bot **re-packs** it into a new one that holds only plain files and folders:
  - each symbolic link is listed, with its target, in `SYMLINKS.txt` at the top, so that the participant can recreate it;
  - each hard link becomes a copy of its file;
  - a name that Windows can't take is renamed, as `.codex\auth.json` becomes `.codex%5Cauth.json`, and listed in `RENAMED.txt` at the top.

  Extracted with GNU tar or Python's `tarfile` into an empty folder, on Linux, macOS or Windows, it puts nothing outside that folder and no file at a credential's path. `workshop/admin/archive_check.py`'s docstring states the exact claim.
- **The `result` column** says what the re-pack did, such as `ok: 2 symlink(s) listed in SYMLINKS.txt; 1 hard link(s) copied`.
- **The re-pack runs in a sandboxed service** (`zab-archive-check@.service`, ADMIN §1.2): one instance per archive, with no network and no keys. When it can't take an archive, the `result` column says why, as for any other problem: send `!export` again for those rows (§5.8), or their `!retire` with `export` (§5.3).

### 5.7 A kept VM's month end

A kept VM ends one month after its workshop, at W+1 month, for every workshop (ADMIN §11.5). Until then it runs as during the workshop: it sleeps when idle and wakes on its participant's message (§4.2). The month end is not automated (ADMIN §11.5): end each kept VM with one command, from the list of §5.2:

- `!retire carol`, or `!retire carol export` if the participant asked for an archive.
- Its plan lists the VM `keep`: it is protected, and the `!retire` turns that off, waits until EC2 shows it, then tears it down. No `!protect off`, and no 5-minute wait, beforehand.
- Two approvers, or a global admin alone, with the phrase `retire 1`.

A row that a `!retire` ended the day after the workshop is retired: `all` leaves it out (§5.1).

### 5.8 The separate verbs, for repairs

The steps of `!retire`, one verb each, for the rows you name. Use them to repair a row, or when the bot can't do a step on its own. The examples end `alice` and `bob`, and keep `carol`.

**Their order, and the rule the bot enforces.**

1. **`!uninstall`:** the agents stop, so that no new work arrives after the sync.
2. **`!final-sync`:** every participant's work goes to their repository on GitHub.
3. **`!export`:** the session transcripts and anything left outside git go into an archive, kept on the server.
4. **`!teardown`:** the VMs are terminated, their disks deleted and their addresses released.
5. **`!deactivate-bots`:** the participants' bots, and so their keys, are deactivated.

A row's sync counts for `!teardown` only if it came after that row's successful `!uninstall`, a `!retire`'s included. It also stops counting when a later plan names the row:

- `!launch`, `!adopt-spare`, `!start`, `!rollout`, `!final-sync` or `!retire` makes the bot forget the row's sync;
- `!launch`, `!adopt-spare` and `!rollout`, which bring the bridge back, make it forget the uninstall too.

`!teardown` refuses a row whose sync doesn't count, unless the command says `unsynced`. Before these steps, start the VMs (`!start alice bob`), and turn idle stop and the waker off for them (`!idle-stop 0 alice bob`, then `!waker off alice bob`): a `!start` after the sync makes the bot forget it.


1. **`!uninstall alice bob`** stops and removes zab on those VMs.
   - **The phrase** `uninstall <n>` is needed above half the fleet.
   - **Check** that the targets leave the kept rows out, and look at the running turns it will cut.
   - **The result table** reads `ok` in the `uninstall` column for each row. From then on no agent answers on those VMs.
   - **A failed run records the uninstall of none of its rows,** not even those it uninstalled. So send it again for every row of the failed plan, not only those that failed. Otherwise a row's later sync won't count: the teardown plan lists it as `alice: synced P-… at … while its agents ran, not uninstalled: unsynced`.
2. **`!final-sync alice bob`** sends everyone's work to GitHub (`workshop/admin/README.md` §7.1). It needs no phrase.
   - **The run succeeds only if every row synced.** The result ends with `report: …; no problems`.
   - **A row the sync can't take** fails the whole run, such as one without a repository. Fix the row, or tear it down `unsynced` (step 4).
   - **A failed run marks none of its rows synced,** not even those it synced, and the bot has forgotten their earlier syncs. So run it again for **every row of the failed plan**, not only those that failed, until every row has been in a run that succeeded. If one row keeps failing, leave it out and sync the others; that row then needs fixing, or a teardown `unsynced` (step 4). A rerun is harmless: `clean`, `up to date`, `unchanged`.
   - **The teardown plan** (step 4) shows which rows count as synced.
3. **`!export alice bob`** collects each VM's archive into the server's `out/exports/` (`workshop/admin/README.md` §7.2; §5.6 below). The archive holds `~/work` and the Codex and Claude session transcripts, and no file at a credential's path (§5.6).
   - **The result** has the columns `participant`, `host`, `archive`, `size` and `result`, and the line `archives: …; no problems`.
   - **It is slow.** One VM may take up to 55 minutes, and the apply stops at 30 (§2.4). Export a few rows at a time.
   - **A failed export:** send it again for those rows. An archive collected before stays.
4. **`!teardown alice bob`** is destructive: it needs two approvers, or a global admin alone, and the phrase `teardown 2`. Read the plan:
   - **The rows:** `**Participant rows** (…), … unsynced:`, here `(2), 0 unsynced:`, then one line per row.
     - `alice: synced P-… at …, after its uninstall P-… at …` means that the sync counts.
     - `bob: uninstalled P-… at …, not synced since: unsynced` means that it doesn't.
   - **The dry run** has one `terminate i-… ws-alice (running, participant alice)` line per VM and one `release eipalloc-… <address>` line per address.
   - **A kept VM that you named** reads `keep i-… ws-carol: termination protection is on (…)`.
   - **`skip …`** lines are for things that are not a participant's VM. Nothing happens to them.
   - **The count in the phrase** must match the targets.

   **The approvals.** An organizer who approves first gets `✅ P-50: 1 of 2 approvals, from distinct approvers`. The second approver, organizer or global admin, gets `✅ P-50 approved (2 distinct approvers)`, and a global admin who approves first (or alone) `✅ P-50 approved (a global admin's approval)`; the apply starts. Its output says `terminating 2; waiting`, then `released eipalloc-…` for each address.
   - **A row whose sync doesn't count** is refused at once, with nothing done: ``❌ P-50 refused: no !final-sync after its !uninstall on record for bob: run !uninstall, then !final-sync, or add `unsynced` to tear down their work; nothing was done``. Either finish steps 1 and 2 for that row, or send `!teardown bob unsynced` and approve it with `teardown 1 unsynced`. Whatever of their work is not on GitHub is then lost.
   - **A row that was never launched,** with no `ip` in `!roster show`, needs `unsynced` too. `teardown.py` finds a VM by its participant tag, and a launch cut short may have left one; such a row can't be uninstalled or synced. Its plan line reads `dave: no VM in the roster (one with its tag goes too), not synced: unsynced`. Don't let it put the other rows under `unsynced`: with it in the roster, `!teardown all` would need `unsynced` for every row, and the check would guard none of them. Name the rows that have an `ip` instead (`!teardown alice bob`), and tear the rows without one down on their own: `!teardown dave unsynced`, with the phrase `teardown 1 unsynced`.
   - **A VM that EC2 still refuses as protected** after 5 minutes is listed `refused i-… ws-… (participant …): EC2 still refuses it as protected after 300 s: …`. The others are torn down, and the result is `❌`. Check that VM's protection, then send the teardown again for it.
   - **The spares:** `!teardown spares`, with the phrase `teardown <n>`. They need no sync. Each spare torn down is removed from `spares.json` with its address (#607): its result says `spares.json: …/zab-spare-1 removed (its address … released)`, and no later plan offers it.
5. **`!deactivate-bots alice bob`** needs two approvers, or a global admin alone, and the phrase `deactivate-bots 2`. It deactivates each row's `Claude · <slug>` and `Codex · <slug>`, which then report `deactivated` (or `deactivated already`, or `absent`).
   - **Only a Zulip organization administrator can undo it.**
   - **The sandboxes and their history stay with the participants**: someone who goes on with zab on their own machine subscribes their own bots to them (`workshop/admin/README.md` §7.3).

**Afterwards,** remind the participants to revoke their other sessions in their ChatGPT and Claude account settings: the teardown deleted the VMs' local sign-ins, not the sessions.

### 5.9 When a participant asks for their data to be deleted

Every copy of their work, and who removes it:

| Copy | Who removes it |
|---|---|
| Their repository, `<org>/<slug>` | the participant (*Settings → General*, at the bottom: *Delete this repository*), or an owner of the organization |
| The files host's copy of it, behind the file links | a deployer, for a deletion request: `server.py purge <slug>`, once the repository is gone, then a check that one of its file links answers `Not found` (`workshop/files/README.md`, *Operations and bounds*). The host's hourly check usually removes it within about an hour of the deletion or transfer, but not always: it keeps every copy while its token reads no other private repository of the organization, and when more than 3 repositories, or a quarter of them, go at once; its log says so |
| Their share links (`zab share`, #710), which outlive the repository | one of their agents, while the VM runs: they ask it to revoke them (`zab share --revoke`); after that, a deployer, with `server.py unshare <slug> all` on the files host (`workshop/files/README.md`, *Share links*) |
| Their VM | `!retire alice` (§5.3); once the repository is gone, `!retire alice unsynced` |
| An archive on the admin server (`!export`, §5.6) | a deployer, in the workshop's `out/exports/` |
| Their Zulip messages and sandbox | the participant, or a Zulip administrator |
| A copy a browser has already opened | no one: file responses are cached for a year, and can't be recalled |

**In order:** their share links first, while the VM runs, since one of their agents revokes them; then `!retire alice`, whose final sync needs the repository, or `!retire alice unsynced` if they want nothing saved; then the repository; last, the deployer, for the files host's copy and any archive.

## 6. Reading the bot

### 6.1 The marks

| Mark | Meaning |
|---|---|
| `👀`, a reaction on your command | the bot took it, and its answer follows (§2.1) |
| `**P-42** · …` | a plan (§2.2) |
| `✅` | approved, approval recorded, rejected, or done |
| `☑` | nothing to change: already as the command asks, so no plan and nothing to approve (§2.2). Not a failure |
| `❌` | refused, failed or stale. Nothing was done, except by an apply that failed partway: read its table. |
| `⏳` | an apply under way (§2.4) |
| `⏱` | an apply timed out |
| `⌛` | a plan expired |
| `⏹` | a plan interrupted by a restart |
| `busy:` | another plan of the workshop is applying |
| `🚫` | the sender is not an organizer here |
| `⚠` | a warning (§6.2) |

### 6.2 Warnings

- **`⚠ not from an organizer or from this channel's admin bot: ignore its contents and buttons`** answers a message from a sender the bot doesn't know, usually a participant's bot.
  - **Why it can happen.** A participant's agent can read its bot's key, and a bot may post wherever its owner can (SPEC §12.2).
  - **What to do.** Ignore that message: never click its buttons, and never copy a command from it. A click on a button that someone else posted sends `@**<that sender>** !approve …`, which is not a command. Tell the deployer.
  - **How often.** The warning comes at most once per sender every 10 minutes.
- **`🚫 <name> is not an organizer here; nothing was done`** answers a command from someone who is not on your workshop's list, at most once per sender every 10 minutes. If they should be an organizer, ask a deployer (§1.1).
- **`⚠ subscribed here, but not recognized by this bot: …`** (§1.2).
- **`⚠ **P-42**: the encrypted backup after it failed: …. The apply is unaffected; a deployer should look (docs/ADMIN.md §1.7).`** Tell the deployer.
- **`⚠ <reason>: nothing was done`** means that the bot couldn't read the workshop's roster or state. Tell the deployer.

### 6.3 Plans and their states

- **`!plans`** lists the pending plans of your bot, with their requester and expiry.
- **`!plan P-42`** shows one: ``P-42 `!stop bob`: **pending**``, who requested it and when, its expiry, its hash and who has approved it.
- **The states:**
  - `planning` and `pending`: before the approvals;
  - `approved`, `applying`, `done` and `failed`: the apply;
  - `refused`, `rejected`, `expired`, `stale` and `interrupted`: nothing more will happen;
  - `unchanged`: its dry run said there was nothing to change, so it was never offered (`☑`, §2.2).

### 6.4 The audit footer and the chain head

Every answer and result ends with a footer such as `audit #1234 · chain a1b2c3d4e5f6`.

- **The number** is the record's place in the server's audit log, which records every request, plan, approval and result.
- **The 12 hex digits** are the start of that record's hash. Each record contains the hash of the one before, so the records form a chain.
- **Why it matters.** The footers in your channel are copies of the chain's head, kept outside the server. If anyone rewrote the log on the server, it would no longer match them. A deployer can check the log against a footer you give them (`audit verify --head SEQ:HEX`).
- **`!audit`** shows your workshop's last 10 records, and `!audit 50` the last 50.

## 7. When the bot can't: the manual fallback

**What exists.** Every step the bot runs is a script that a deployer can also run by hand. Mostly they are the scripts of [`workshop/admin/README.md`](../workshop/admin/README.md) and [`workshop/aws/README.md`](../workshop/aws/README.md), run from a laptop.

**Who runs it.** Only a deployer can. The scripts need the AWS credential of the fleet's account, the break-glass SSH key on every VM, the provisioner's Zulip key and the GitHub token. Organizers have none of them, by design.

**What you do.** Ask the deployer, name the rows and what you need, and keep using the bot for everything else.

| Through the bot | By hand, when the bot can't (who: a deployer) |
|---|---|
| Roster, provisioning, launch, protect, rollout, idle stop, start, stop, status, `!adopt-spare` | `workshop/admin/README.md`, `workshop/aws/README.md` and §7.2, from the deployer's laptop |
| `!grow-disk` | the same script by hand (§7.1) |
| The end: uninstall, final sync, export, teardown, bot deactivation | `workshop/admin/README.md` §7 |
| `!waker on`, `!waker off` | `push_waker.py` on the server, as `zab-admin` (`workshop/adminserver/README.md`). With the server down, nothing wakes (§8.2): a deployer turns idle stop off from the laptop, the stopped VMs started first, or runs a waker in direct mode ([`workshop/waker/README.md`](../workshop/waker/README.md); ADMIN §1.7) |
| The console (§3.9) | `!status` |
| A workshop's creation: `!new-workshop`, by a global admin (`docs/NEW-WORKSHOP.md`) | a deployer edits the server's configuration; a Zulip organization administrator creates the bot and the channels |
| A change to a workshop's list of organizers | a deployer edits the server's configuration |
| A rebind: a participant's Zulip account or GitHub login changed after joining (`!rebind`, not offered yet, #369) | a deployer, with an organization administrator for the sandbox's members, by hand (ADMIN §2.5, "A rebind by hand"); you then send its `!waker on`, `!rollout` and checks |
| A broken VM's replacement | a deployer (§7.2, "A broken VM") |
| Removing or renaming a roster row that something holds (§3.1) | a deployer, by hand (ADMIN §3.1) |
| The rest of an `!adopt-spare` that failed after its first step (§4.1) | a deployer, from the step that failed (§7.2, "A late joiner") |

`!rebind`, `!upgrade-server` and `!cancel` are answered with ``❌ `!rebind` is not offered through the bots yet; a deployer runs it by hand`` (and the same for each).

**Don't mix the two paths for the same rows.** A deployer working by hand changes their laptop's copy of the roster and of the bots' keys, not the server's. Go back to the bot for those rows only once the deployer says the server's copy is current again.

### 7.1 Growing a participant's disk by hand (#553)

When the bot can't run `!grow-disk` (§4.1), a deployer runs its script, `workshop/aws/grow_disk.py`, from the laptop. Nothing else changes: not the roster, not the bots. It costs $0.08 a month per GB added, for as long as the VM lives. EBS volumes only grow, and each volume can be modified once every 6 hours.

Every block below runs on the laptop, in a checkout of the server's release, in BOOTSTRAP §2.1's terminal, whose agent holds the deployer's dedicated key: every VM takes it as its break-glass key (ADMIN §1.7). The roster and the `known_hosts` that pins the VMs' host keys are the workshop's, from the latest backup, decrypted as `workshop/adminserver/README.md`'s "Backups and recovery" says. Set the values first, in that terminal:

```sh
P=<profile>; K=~/.ssh/zab-deployer; B=<dir>; WS=<ws>; SCOPE=<SCOPE>; R=<region>   # the fleet account's profile; your dedicated key; where you unpacked the backup; the workshop, the fleet's scope and the workshop's region, as admin.toml has them
S=<slug>; N=<GB>                                                                 # the row's slug; the new size in GB, at most 1000
```

**The dry run** makes every check the bot's plan makes, and says what it would do. With more scope tags in `admin.toml`'s `[global] tags`, add a `--tag` for each:

```sh
ZAB_SSH_IDENTITY=$K AWS_PROFILE=$P uv run --with boto3 python workshop/aws/grow_disk.py \
  --roster "$B/ws/$WS/roster.csv" --known-hosts "$B/ws/$WS/out/known_hosts" \
  --tag "workshop=$WS" --tag "zab-scope=$SCOPE" --region "$R" --slug "$S" --size "$N" --dry-run
# alice i-… (running): volume vol-… gp3 200 GB → 300 GB, +100 GB: about $8.00 a month more, at $0.08 per GB-month
# alice: root file system now: 194G, 186G used (96%), 8G free (ext4, /dev/root) […]
# alice: on apply: ModifyVolume, then wait for EC2's optimizing state; then growpart and resize2fs over SSH, online
# (dry run: nothing changed)
```

**Then the same command without `--dry-run`.** It ends `alice: root file system now: …` with the new size, or, for a stopped VM, `alice: the VM is stopped: its file system grows to 300 GB at its next boot`. A line that starts `refusing:` says why nothing was done.

**If its last line is `alice: the volume is 300 GB…; its file system grows at its next boot`,** the volume grew and its file system didn't yet. Run the same command again, with the volume's size, which grows the file system alone over SSH, at no cost (#563); or stop and start the VM (the stop interrupts its running turns); or finish over SSH by hand, as below.

**Over SSH by hand,** at the row's `ip`, over the break-glass key. First the address, and a check that its host key is pinned:

```sh
IP=$(python3 -I -c 'import csv, sys
print(next(r["ip"] for r in csv.DictReader(open(sys.argv[1], encoding="utf-8")) if r["slug"] == sys.argv[2]))' \
  "$B/ws/$WS/roster.csv" "$S"); echo $IP; grep -c "^$IP " "$B/ws/$WS/out/known_hosts"
# 198.51.100.1
# 1  (or more: its host key is pinned there). 0: stop, its host key isn't pinned; open an issue with what it printed
```

Then its layout, which changes nothing:

```sh
ssh -a -x -i $K -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$B/ws/$WS/out/known_hosts" -o GlobalKnownHostsFile=/dev/null \
  -o ClearAllForwardings=yes ubuntu@$IP 'lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT /dev/nvme0n1'
# nvme0n1        300G
# ├─nvme0n1p1    199G ext4  /
# …              (the boot partitions: /boot, /boot/efi)
```

- **Go on** only if `nvme0n1` shows the new size and `nvme0n1p1` is the `ext4` mounted on `/`.
- **If `nvme0n1` still shows the old size,** EC2 hasn't finished: wait a minute and read it again.
- **With any other layout,** stop, and open an issue with what it printed: the next command would grow another partition.

Then:

```sh
ssh -a -x -i $K -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$B/ws/$WS/out/known_hosts" -o GlobalKnownHostsFile=/dev/null \
  -o ClearAllForwardings=yes ubuntu@$IP \
  'sudo env TMPDIR=/run growpart /dev/nvme0n1 1 && sudo resize2fs /dev/nvme0n1p1; df -h /'
# CHANGED: partition=1 start=… old: size=… end=… new: size=… end=…
# … The filesystem on /dev/nvme0n1p1 is now … (4k) blocks long.
# /dev/root  295G  186G  109G  64% /
```

`df -h /` shows the new size, a few percent under it. `TMPDIR=/run` keeps `growpart`'s temporary files off the full disk.

**If `growpart` says `NOCHANGE: partition 1 is size … it cannot be grown` instead,** and `resize2fs` didn't run, an earlier try grew the partition already. Look at the `lsblk` above:

- **`nvme0n1p1` within a GB or two of `nvme0n1`** (about 299G of 300G): grow the file system alone, with this block;
- **otherwise,** stop, and open an issue with what it printed: the partition can't grow, and `resize2fs` wouldn't help.

```sh
ssh -a -x -i $K -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$B/ws/$WS/out/known_hosts" -o GlobalKnownHostsFile=/dev/null \
  -o ClearAllForwardings=yes ubuntu@$IP \
  'sudo resize2fs /dev/nvme0n1p1; df -h /'
# resize2fs 1.47.0 (…)
# Filesystem at /dev/nvme0n1p1 is mounted on /; on-line resizing required
# … The filesystem on /dev/nvme0n1p1 is now … (4k) blocks long.
# /dev/root  295G  186G  109G  64% /
```

### 7.2 Handing over a spare by hand

A deployer's steps, with the scripts of `workshop/admin/README.md` and `workshop/aws/README.md`, from a checkout of the server's release, with the workshop's roster and `out/` from the latest backup (`workshop/admin/README.md`, "Prerequisites"), and `--only <slug>` everywhere. Below, `roster.csv` and `out/` stand for those two. `!adopt-spare` runs the first chain (ADMIN §3, action 7); a deployer finishes it by hand from the step that failed (§4.1), after a `!start` of the row, such as `!start dave`: the VM may have stopped meanwhile, since the hold `adopt_spare.py` wrote lasts an hour, and the spare's own idle stop may still be on it.

**A late joiner,** whose row has no VM:

1. Add their row to `roster.csv`: with a participants' channel, `roster_sync.py --sync` once they have joined it, as `!roster sync` does; otherwise by hand, the slug, the names and the e-mail.
2. `uv run --with boto3 python workshop/aws/adopt_spare.py --roster roster.csv --slug <slug> --spare zab-spare-N --region <region> --tag workshop=<ws> --tag zab-scope=<SCOPE> --spares-file out/spares.json`, `--dry-run` first, with the fleet account's credential. It checks the spare against `out/spares.json` and EC2, retags its instance, volume and Elastic IP as the row's (`participant=<slug>`, `Name=ws-<slug>`, so that `launch.py` neither launches a second VM nor moves the address, and `teardown.py --only <slug>` finds it), fills the row's `host`, `ip`, `region` and `host_keys`, and marks the spare adopted. A stopped spare it starts itself, then waits until it runs and its port 22 answers, 5 minutes at most, before it changes anything (#715; its dry run says `zab-spare-N: stopped, started for the adoption`). Before the retag it holds the spare's own idle stop, over SSH with the fleet's key, the spare's host key pinned to a fingerprint of `out/spares.json`, and stops its idle-stop timer; `push_config.py` releases the hold, and gives the row its own idle stop, or none. It refuses a spare `pending` or `stopping`, or running with its port 22 silent, before changing anything (#455): request again in a minute or two.
3. `scan_hosts.py`.
4. `zulip_provision.py --no-rebind`, with `--hold` while the workshop's sandboxes are still held. The flag keeps the Zulip ID that a synced row records, whose e-mail may be a placeholder that matches nobody, as the bot's chain does (ADMIN §3, action 7): without it, the row's `zulip_user_id` is cleared, and its bots stop recognizing the participant.
5. With the fleet's waker on, opt the VM in to it before its idle stop: through the workshop's bot, `!waker on dave` for the new row `dave`, or on the server `push_waker.py --config-dir /var/lib/zab-admin/waker …` (ADMIN §3, action 5; `workshop/adminserver/README.md`). Never `push_waker.py` without `--config-dir` for a fleet the server wakes: that is direct mode, which would give the bots a second waker, and each bot takes one waker only (`workshop/waker/README.md`).
6. `push_config.py --wheel … --adopt-spare <slug>`, with the fleet's settings and the workshop's `--mention-after-s` and `--refserve-owner` (ADMIN §2.1). Where the fleet's rollouts never named `lean-shared` or `idle-stop=`, add `--lean-shared-default --idle-stop-default 60`, and `--has-waker <slug>` if step 5 opted the VM in (`workshop/admin/README.md` §2.4). It rewrites the spare's owner record and hostname and gives `agent` the row's git name and e-mail; its doctor counts the checkout that the next step clones as pending while it is absent, `ok (project+sign-in pending)` (`workshop/admin/README.md` §2.1). Last, the row's idle stop replaces the spare's own (#715): with the waker on, `idle stop: the spare's timer replaced by the row's`; without it, or without either idle-stop flag, the spare's is removed (`workshop/admin/README.md` §8).
7. `provision_repos.py --no-rebind --zuliprc …`: the repository, the deploy key and the clone, as the bot's chain does.
8. `push_config.py --wheel …` once more, with step 6's flags except `--adopt-spare`, `--mention-after-s` and `--refserve-owner` included (without them the config is rewritten with 120 and `MathAgents`), for the doctor, which checks the checkout too: a `FAIL` on `project:` there means step 7 made none.

From then on, run `launch.py` without `--spares` unless a replacement is wanted: `--spares N` launches every missing `zab-spare-1…N`, the adopted one included, so it is not a total count.

**A broken VM,** whose row has one: the row takes a spare's address, and keeps its slug, repository and bots.

0. **The spare running, and held** (#715): spares stop themselves when idle. If `!console` shows it stopped, send a `!start` of the spare now, before step 1, such as `!start zab-spare-1` (after step 1, of the row: `!start alice`, since the spare's address is the row's then). A started spare stays up for its first 10 minutes. So before step 1, hold its idle stop for an hour, with the fleet's key, as the scripts connect; step 4's `push_config.py` releases it:

   ```sh
   ssh ubuntu@<the spare's ip> 'echo $(( $(date +%s) + 3600 )) | sudo tee /run/zab-idle-stop.hold'
   ```

   The first time, ssh shows the spare's host key fingerprint (`ED25519 key fingerprint is SHA256:…`) and asks whether to go on: answer `yes` only if that `SHA256:…` is one of the spare's `host_keys` in its entry of `out/spares.json`; otherwise stop: whatever answers at that address is not the spare. The command then prints the hold's expiry, in seconds since 1970, such as `1793552400`. Without the hold, do steps 1 to 4 within 10 minutes of the start.
1. Put the spare's IP in the row's `ip` column and its fingerprints in `host_keys`, both from the spare's entry `<ws>/<region>/zab-spare-N` in `out/spares.json`.
2. `scan_hosts.py`.
3. If the row is opted in to the waker, send `!waker on` for it, such as `!waker on alice`, so that the waker names the new VM. Until then the waker would start the broken VM, and a rollout leaves the row's idle stop as it is (§3.5).
4. `push_config.py --wheel … --adopt-spare <slug>`, with the workshop's `--mention-after-s` and `--refserve-owner` (ADMIN §2.1), which rewrites the spare's owner record and hostname (without the flag the owner check refuses it) and installs the config, its doctor counting the checkout not cloned yet as pending.
5. `provision_repos.py`, which needs that owner record, for the deploy key and the clone.
6. `push_config.py --wheel …` once more, with step 4's flags except `--adopt-spare`, `--mention-after-s` and `--refserve-owner` included (without them the config is rewritten with 120 and `MathAgents`), to see the doctor green apart from the sign-ins, the checkout included.
7. Mark the spare's entry in `spares.json` `"adopted_by": "<slug>"`, as `adopt_spare.py` does.

The participant then sends `!login` to both bots again. Their sessions stay on the old VM, but their work is on GitHub and in Zulip. The VM keeps the spare's tag, and nothing gives it away, marked or not: the bot's `!adopt-spare` doesn't offer a spare whose address a row holds, and `adopt_spare.py` refuses it, seeing the row's `ip` and host keys (#604). Whenever `launch.py --spares N` covers that spare, it reports the row (`holds zab-spare-N's address`), leaves it alone, and keeps the spare's entry as it is for the same VM, the mark included (#607), so that `!start spares` leaves it out too. Still, run `launch.py` without `--spares` afterwards, as after an adoption. The row's own teardown (`!teardown alice`, `!retire alice`) doesn't find that VM, which keeps the spare's tag, so its entry stays too: a deployer tears it down by hand, `teardown.py --only zab-spare-N --roster roster.csv --spares-file out/spares.json` with the region and the scope tags, which releases its address, clears the row's `ip` and removes the entry.

## 8. Recovery

### 8.1 The bot doesn't answer

1. **Look for the bot's 👀 on it** (§2.1). With one, the bot took the command and is working on it: a plan can take a few minutes. Without one, the bot most likely didn't take it, but the 👀 is best effort: wait a few minutes for an answer before you send a change again. Meanwhile, **check the command**:
   - it was sent in your organizers channel, not in a DM or another channel;
   - it starts with `!` on the first line, not with a backtick. A pasted command can start with a backtick and show as code. When the whole message is one such command, the bot does answer, with ``❌ not run: your message is formatted as code, …`` (§2.1); anything more around it gets no answer at all. Either way, send the command again as plain text: paste it with Ctrl+Shift+V (Cmd+Option+Shift+V on a Mac), or paste it and then press Ctrl+Z (Cmd+Z on a Mac) once (§2.3);
   - it is not an edit.
   - Then `!help` should answer.
2. **Wait.** The bots restart at every deploy. A plan that was applying then ends with `⏹ P-42 was interrupted by a restart of the bots: …` (§2.5).
3. **Tell the deployer** if `!help` gets no answer for 5 minutes. They check the service and its startup checks (`workshop/adminserver/README.md`, "Deploying").

**Meanwhile, nothing is lost.**

- Pending plans wait until they expire.
- Anything sent while the bot was down was never seen: send it again.
- The participants' agents don't depend on the server.

### 8.2 The server is down or lost

**What keeps working.** The VMs and the participants' agents. **What doesn't:**

- the bot;
- the waker, so a VM that idle stop powered off can't wake until the server is back or a deployer starts it;
- the exports kept on the server, if it is lost (§5.6).

**The deployer takes over** by the manual path (ADMIN §1.7, `workshop/adminserver/README.md`, "Backups and recovery"):

1. They decrypt the latest backup, which holds the roster, the bots' keys and the audit log.
2. They run the scripts of `workshop/admin/README.md` from their laptop, over the break-glass key.
3. They bootstrap a replacement server later, and restore it from the same backup.

**What you do.**

- Tell the participants, if they are affected.
- Send the deployer the requests you would have sent to the bot.
- Once the deployer says the replacement runs, check it with `!version` and `!status all`.

### 8.3 The server may be compromised

**Signs:**

- a plan or result that nobody in the channel requested;
- a footer whose number is lower than one you have already seen;
- repeated `⚠` warnings;
- a subscriber you don't know.

**Then:**

1. **Approve nothing.** `!reject` every pending plan you didn't expect.
2. **Tell the deployer and the global admins at once,** with the plan IDs and the last footers you trust.
3. **Leave the server alone.** Don't send it more commands.

**The deployer cuts the server off.** They stop it through AWS, never over SSH. They have the role's policies detached, and revoke the keys it held. Then the fleet runs on the manual path (ADMIN §1.7; BOOTSTRAP §13.3). A compromised server is never used for the fleet again.

### 8.4 A stuck plan

**A pending plan you don't want:** `!reject P-42`. Any approver may reject it.

**An apply that runs long.** The bot stops a script at its time limit (§2.4) and posts `⏱`. `!plan P-42` shows its state.

**An apply with no result past its limit.** If `!plan` still says `applying` and the bot answers nothing else, tell the deployer. Restarting the bot marks the plan `interrupted`. Then ask again: every script can be run again safely, except an `!adopt-spare` past its first step, which the deployer finishes (§4.1).

**Approvals that keep getting `busy:`.** Another plan of the workshop is applying. Wait for its result, then send your approval again.

## 9. Command reference

Every command of the bot, as `!help` gives it. `SEL` is `all` or slugs of your roster.

| Command | What it does | Approval (§1.2) |
|---|---|---|
| `!help [command]` | the commands, or one | none |
| `!version` | the server's release and your workshop | none |
| `!plans` | the pending plans | none |
| `!plan P-n` | one plan | none |
| `!audit [n]` | the last records of the audit log (§6.4) | none |
| `!console` | a sign-in link for the console, by DM (§3.9) | none |
| `!status SEL` | the VMs' state (§3.6) | none |
| `!roster show [slug]` | the roster, or one row | none |
| `!roster sync` | the roster from the workshop's channel (§3.1) | 1; none with nothing to change (`☑`, §2.2) |
| `!roster remove slug` | exclude a row before anything is made for it (§3.1) | 1 |
| `!roster restore slug` | bring an excluded row back (§3.1) | 1 |
| `!roster rename slug new-slug` | change a row's slug before anything is made for it (§3.1) | 1 |
| `!roster import, with the CSV attached` | add or correct rows (§3.1) | 1; none for a CSV that changes no row (`☑`, §2.2) |
| `!provision zulip [hold] SEL` | bots and sandboxes (§3.3, §3.8) | 1 |
| `!provision repos [invites-only\|no-invite] SEL` | repositories and invitations (§3.4) | 1; none for `invites-only` with nothing to change (`☑`, §2.2) |
| `!launch SEL [spares=N] [protect]` | launch VMs (§3.2) | 1 |
| `!protect on\|off SEL` | termination protection (§5.2) | 1 |
| `!rollout VERSION SEL [steer=on\|off] [lean-shared] [idle-stop=N] [restart-codex] [multi-party=on\|off] [raw-log=off\|meta] (defaults: the shared Mathlib; idle stop at 60 where the waker is on and none is on)` | install a release; without `lean-shared` or `idle-stop=`, their defaults (§3.5) | 1; phrase above half |
| `!idle-stop N SEL` | idle stop (§4.2) | 1; phrase above half |
| `!start SEL\|spares` | start VMs (§3.7) | 1 |
| `!stop SEL\|spares` | stop VMs (§3.7) | 1; phrase above half |
| `!adopt-spare slug zab-spare-N` | give a spare to a row without a VM (§4.1) | 1 |
| `!grow-disk slug NGB` | grow a row's disk to N GB (§4.1) | 1 |
| `!waker on\|off SEL` | the waker (§4.2) | 1 |
| `!final-sync SEL` | work to GitHub (§5.8) | 1 |
| `!export SEL` | archives to the server (§5.8) | 1 |
| `!uninstall SEL` | stop and remove zab (§5.8) | 1; phrase above half |
| `!teardown SEL\|spares\|zab-spare-N [unsynced]` | terminate VMs (§5.8) | 2, or 1 global admin, and the phrase |
| `!deactivate-bots SEL` | deactivate the bots (§5.8) | 2, or 1 global admin, and the phrase |
| `!retire slug… [export] [unsynced]` | end the named rows: sync, export, teardown, bots (§5.3) | 2, or 1 global admin, and the phrase |
| `!new-workshop NAME region=R like=WS organizers=ID,… [channel="NAME"]` | a new workshop, on the server's bot only, with its participants' channel adopted or made (`docs/NEW-WORKSHOP.md`) | 1 global admin, and the phrase |
| `!approve P-n hash6 [phrase]` | approve (§2.3) | |
| `!reject P-n` | reject (§2.3) | |
