# Starting a new workshop: `!new-workshop`

For the global admins. Every workshop on the admin server is created this way, on Zulip: a global admin sends `!new-workshop` to the global admin bot, `Workshop admin`, in `#zab-admins`, a global admin approves it (the requester may), and the organizers then follow [`docs/ORGANIZERS.md`](ORGANIZERS.md). The design is [ADMIN §2.9](ADMIN.md#29-adding-a-workshop-new-workshop); the issue is #418.

**The examples** use ORGANIZERS' made-up workshop, `harvard-2026`, copied from another workshop of the same server, `zab-test` (`like=`, §1), and a made-up participants' channel, "Harvard 2026 participants": read your own names wherever they appear. The user ID `1234567`, the plan ID `P-12` and the hash `3f9a2c` are made up too: always copy yours from Zulip and the bot.

## 1. Before

- **The server's release has the verb.** `!help new-workshop` in `#zab-admins` answers with its usage.
- **The region must already be set up:** it is in the server's `[global] regions`, and the account owner made its security group `zab-fleet` and its image, both tagged with the fleet's scope ([BOOTSTRAP §6.1](BOOTSTRAP.md#61-the-fleets-security-group-and-image-per-region)). A region the server doesn't serve yet takes [BOOTSTRAP §6.5](BOOTSTRAP.md#65-a-new-region-end-to-end) first, end to end: its AWS resources, `[global] regions`, the role's policy, and a check with a `!new-workshop` plan that is rejected. A region not in `[global] regions` is refused at once, with the regions that are; in one whose group or image is missing, the plan is refused and says what is missing.
- **Your choices:**
  - **the name:** `a-z`, `0-9` and `-`, at most 47 characters, so that `#<name>-fleet-alerts` fits Zulip's 60. It must be new on this server: a workshop's name, like a participant's slug, is never reused ([ADMIN §2.1](ADMIN.md#21-the-bots-their-channels-and-the-configuration));
  - **the region**, one per workshop;
  - **a workshop to copy, `like=`:** the new one takes its GitHub organization, file server, instance types and break-glass keys. They show in the plan; a deployer changes them later, if needed. Its scope is never copied: it is `workshop=<name>` and the fleet's, `[global]`'s, so the entry names no tag;
  - **the organizers,** by Zulip user ID ([BOOTSTRAP §5.5](BOOTSTRAP.md#55-the-global-admins-user-ids) shows how to read one), at most 20, never `zab-provisioner`. They need not include the global admins, who act through every bot anyway. But `organizers=` takes at least one ID: if every lead organizer is a global admin, name them anyway. A global admin listed as an organizer is fine;
  - **the participants' channel, optional:** `channel="<its name>"`, in double quotes (a phone's curly ones, `“…”`, work too), exactly as Zulip shows it but for case. The workshop's roster is then read from its subscribers (`!roster sync`, [ORGANIZERS §3.1](ORGANIZERS.md#31-the-roster-roster-sync-or-roster-import)), and the organizers invite people in Zulip by e-mail with that channel ticked. Its name may hold letters of any script, digits, single spaces and `- _ . , ' & ( ) + : ! ? /`, at most 60 characters. **An existing channel** is adopted only if `zab-provisioner` is subscribed to it: subscribe it first (the channel's settings, *Subscribers*). It should be private: anyone can join a public one. It may not be a channel the server uses already (a workshop's `#…-organizers` or `#…-fleet-alerts`, or another workshop's participants' channel): the plan is refused. **A new channel** is made private, with shared history, for the organizers and `zab-provisioner`. Where the organization lets members subscribe others, any subscriber can add someone to it, who is then offered a row: read each `!roster sync` plan's additions before you approve it. The configuration records the channel's ID, so you may rename it later. Without `channel=`, the organizers import a roster CSV (`!roster import`) instead.
- **A global admin's approval,** within the plan's 10 minutes, with the typed phrase: one approval completes the plan, the requester's own included ([ADMIN §2.5](ADMIN.md#25-approval-rules), the server class). The phrase guards against a misclick.
- **Quotas.** The region's vCPU and Elastic IP quotas must cover the fleet ([ORGANIZERS §1.1](ORGANIZERS.md#11-what-the-deployer-sets-up), item 6; reading them and requesting an increase: [BOOTSTRAP §6.4](BOOTSTRAP.md#64-quotas-in-each-region)). The plan doesn't check them.

## 2. The command

In `#zab-admins`, in a topic of its own:

```
!new-workshop harvard-2026 region=us-east-2 like=zab-test organizers=1234567 channel="Harvard 2026 participants"
```

The bot answers with a plan, `**P-12** · server · …`. Read it:

- **the entry** for the server's configuration, with the image and security group of the region's setup (the group `zab-fleet`; the newest image, with its name and date), and `<new>` for the IDs the apply makes (the bot's, the channels');
- **the people:** each organizer's and global admin's name next to their ID. A wrong name means a wrong ID: reject the plan (`!reject P-12`);
- **the participants' channel,** with `channel=`: in the entry as `channel = <its ID>` and under *To make* as `#Harvard 2026 participants, the participants' channel: adopted (<its ID>)`, with an `ok:` line among the prerequisites; or, for a channel to make, `channel = "<new>"`, `#… (private), the participants' channel: the organizers, zab-provisioner (…)` and an `unchecked:` line (below). `MISSING: #… exists, and zab-provisioner is not subscribed to it` refuses the plan: subscribe `zab-provisioner` to the channel, then send the command again. A `warning:` line says the channel is public: make it private in its settings before the participants join;
- **what it makes:** the bot `Workshop admin · <name>`; `#<name>-organizers`, private, with the organizers, the global admins and the bot; `#<name>-fleet-alerts`, private, with the organizers, the global admins and `zab-provisioner` (the waker posts failed wakes there with the participants' bots, which `zab-provisioner` owns); `ws/<name>/` with an empty roster; the key pair `zab-admin-<name>`; the entry;
- **the prerequisites:** every line `ok:`. A plan with a `MISSING:` line is refused, never offered: fix what it names, and send the command again.
- **what it can't check,** on an `unchecked:` line: the two channel names are free only as far as `zab-provisioner` can see. It lists no private channel it isn't in and no archived one, and an archived channel keeps its name. If such a channel has one of the names, the apply fails at that channel, after it made the bot: send the command again under another name. The same goes for a participants' channel to make: if a private channel of that name exists, the apply stops at it, before it makes anything in Zulip, with `… could not be made …: if a private channel of that name exists, subscribe zab-provisioner to it first …`. Do that, and send the command again: it then adopts the channel.

**Approve** with the line that the approval message, right after the plan, shows in a code box with a copy button: the plan takes a phrase, so it has no Approve button ([ORGANIZERS §2.3](ORGANIZERS.md#23-approving)). A global admin, the requester or another, sends

```
!approve P-12 3f9a2c new-workshop harvard-2026
```

The answer is `✅ P-12 approved (a global admin's approval)`, and the apply runs. Its result, `✅ **P-12** · …`, ends with the config helper's line: `harvard-2026 added to admin.toml; admin.toml.bak kept`. Then the bots restart, which takes about 10 seconds.

**The restart touches every workshop on the server,** so avoid a workshop's busy hour. No apply is running then: `!new-workshop` waits for every workshop's apply, and holds them all until the restart. But:

- a plan whose dry run is running at that moment ends `⏹ interrupted`: its requester sends the command again;
- a `!status`, or another read, in flight gets no answer: send it again;
- approvals sent while the bots are down are lost: send them again;
- if a deploy, or anything else, cuts a `!new-workshop` apply short, its plan ends interrupted, and the workshop may or may not exist: check `!version` in `#zab-admins` before sending the command again.

## 3. Check

- `!version` in `#zab-admins` lists the new workshop, its region and its organizers' count.
- In `#<name>-organizers`, `!version` gets an answer from `Workshop admin · <name>`.
- With `channel=`: the participants' channel exists, private, with the organizers and `zab-provisioner` in it.
- The organizers channel's members are the organizers, the global admins and the bot. The bot checks this every hour and warns about anyone else.

## 4. Hand over

- **To the organizers:** they are in `#<name>-organizers`; their runbook is [`docs/ORGANIZERS.md`](ORGANIZERS.md), from [§1](ORGANIZERS.md#1-before-the-workshop). Their first step is `!roster sync` once the participants have joined the participants' channel, or, without one, `!roster import` ([ORGANIZERS §3.1](ORGANIZERS.md#31-the-roster-roster-sync-or-roster-import)).
- **To the deployers:** the server's configuration has a new entry. [`deploy.sh`](../workshop/adminserver/deploy.sh) shows it as a difference from their own copy at the next deploy until they add it there.

## 5. When it fails

- **A refused plan** made nothing. Its message says why.
- **A failed apply** keeps what it made: the bot, the channels, the directory, the key pair. The same command, sent again once the cause is fixed, reuses them. The config helper's refusal is in the result; a deployer reads more with `journalctl -u zab-admin-config` on the server.
- **No answer from the config helper** within 60 seconds: the apply withdraws its request, and the result says `the request is withdrawn, and nothing was added`; send the command again once a deployer has found why (`systemctl status zab-admin-config.path`). If the helper had taken the request already, the result says `the entry may be added`: a deployer reads `/var/lib/zab-admin-config/result.json` and the journal before anyone sends it again.
- **Undoing a workshop** is not offered through the bots. A deployer restores `/etc/zab-admin/admin.toml.bak`, removes `secrets/bots/<name>.zuliprc` and restarts the bots ([`workshop/adminserver/README.md`](../workshop/adminserver/README.md#processes), "The config helper"); in Zulip, the bot is deactivated and the channels archived. The name stays used.
- **Changing the organizers** later is a deployer's edit of the configuration ([ADMIN §2.9](ADMIN.md#29-adding-a-workshop-new-workshop)).
