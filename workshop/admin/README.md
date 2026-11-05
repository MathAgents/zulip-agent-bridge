# Workshop admin scripts

The admin bot runs these scripts on the admin server, one plan at a time
([`docs/ORGANIZERS.md`](../../docs/ORGANIZERS.md), the organizers' runbook;
[`docs/ADMIN.md`](../../docs/ADMIN.md) §3, the design). A deployer runs them by hand, from a laptop,
when the bot can't: that is the manual path, the fallback (ORGANIZERS §7), and the whole path for a
fleet run without a server. The scripts run with `uv run python …`, from a checkout of the release,
outside any synced folder (Dropbox, iCloud Drive, OneDrive, Google Drive): `out/` holds the bots'
API keys and the pinned `known_hosts`, and a sync client would copy the keys off the machine. They
are standalone (not part of the `zab` package), idempotent, and never print an API key. Everything
they write that is personal or secret goes to `out/` (`--out-dir`, by default `workshop/admin/out/`,
git-ignored) or back into `roster.csv` (kept out of git, §4).

> **Your deployment's values.** The scripts' defaults are one deployment's, the MathAgents
> workshops', for its hand runs. The admin bot passes each of them from `admin.toml` (ADMIN §2.1),
> and never relies on a default. A hand run must pass yours too:
>
> | Flag | Scripts | Default | Yours |
> |---|---|---|---|
> | `--org` | `provision_repos.py`, `roster_import.py` | `MathAgents` | the workshop's `github_org` |
> | `--repo-owner` | `zulip_provision.py` | `MathAgents` | the same organization |
> | `--refserve-url` | `push_config.py`, `zulip_provision.py` | `https://files.mathagents.org` | the workshop's `refserve_url`; `''` leaves file links off, and "Start here" names no file host |
> | `--refserve-owner ORG` | `push_config.py`, with a release | `MathAgents` | the workshop's `github_org`, the organization the files host serves (#693) |
> | `--gifts-step TEXT` | `zulip_provision.py` | `GIFTS_STEP` (any Claude Max and ChatGPT Pro gifts) | the workshop's `gifts_step`, if it has one (ADMIN §2.1) |
> | `--mention-after-s SECONDS` | `push_config.py`, with a release | `120` | the workshop's `mention_after_s` (ADMIN §2.1) |
> | `--tag KEY=VALUE`, repeatable | `status.py`, `roster_import.py`, `roster_sync.py`, `push_waker.py --config-dir` and the scripts of `workshop/aws/` | `workshop=nt-agents-2026`; `push_waker.py`'s, `workshop=` its `--workshop` | `workshop=<ws>` and `zab-scope=<SCOPE>`, and each of `[global] tags` |
> | `--region` | `roster_import.py`, `roster_sync.py` and the scripts of `workshop/aws/`; `push_waker.py --config-dir`, which requires it | mostly `us-west-2` | the workshop's region |
> | `--workshop` | `push_waker.py`, which requires it with `--config-dir` | `nt-agents-2026` | the workshop's `name` |
>
> **The files host has no default organization:** it serves the one its `ZAB_FILES_ORG` names
> ([`workshop/files/README.md`](../files/README.md), §3), which is `--refserve-owner`'s. The
> bridges publish the snapshots that file links open only for repositories of
> `--refserve-owner`'s organization: with another one than the files host's, they publish
> nothing it can serve.

**The order** (every step is idempotent, and every step after the roster takes `--only <slug>`;
after a failure, fix the row and rerun the same command). The admin bot's launch day is the same,
through its commands (ORGANIZERS §3):

1. **The roster** (§4): `roster_sync.py --sync` from the workshop's participants' channel (§4a), or
   the organizers' CSV through `roster_import.py` (§5);
2. `launch.py` ([`workshop/aws/README.md`](../aws/README.md)): one VM per roster row, and the
   spares;
3. `scan_hosts.py` (§0): pin every VM's host key;
4. `zulip_provision.py --no-rebind --hold` (§1): bots, their zuliprc files, and sandboxes with only
   the bots in them; nothing a participant sees;
5. `provision_repos.py --no-rebind` (§1b): the repositories, deploy keys and clones;
6. **the waker** (§9), before idle stop, so that no VM ever stops without a way to wake it;
7. `push_config.py --wheel … --lean-shared --idle-stop 60` (§2, §8): the release, the bridge's
   config and services, the shared Mathlib (§2.4), and idle stop last;
8. `status.py` (§3);
9. `power.py --stop --all --spares` ([`workshop/aws/README.md`](../aws/README.md), "Stopping and
   starting the fleet: power.py"), until the first morning: idle stop powers the VMs off anyway,
   but a spare never stops by itself (ORGANIZERS §3.7);
10. on the workshop's first morning, `power.py --start --all --spares`; then
    `zulip_provision.py --no-rebind` without `--hold`, which releases the sandboxes; then the
    waker again (§9), for the Zulip IDs it records; and
    `provision_repos.py --no-rebind --invites-only`, which sends the repository invitations
    (ORGANIZERS §3.8). At the breaks, the same three again, for the participants who joined or
    added a GitHub login since.

**Prerequisites.** Two cases, by who made the fleet.

**A fleet the admin server made,** run by hand while the bot can't (ORGANIZERS §7, §8.2):

- **The roster and `out/`,** from the latest backup, decrypted as
  [`workshop/adminserver/README.md`](../adminserver/README.md)'s "Backups and recovery" says: the
  workshop's `ws/<ws>/roster.csv` and `ws/<ws>/out/`, with the bots' zuliprcs, `known_hosts` and
  `spares.json`. Pass them wherever a script takes them: `--roster`; `--out-dir <out>`;
  `--known-hosts <out>/known_hosts`; `--spares-file <out>/spares.json`. The commands below show the
  defaults instead.
- **The provisioner's zuliprc,** `zab-provisioner`'s, from your sealed secrets bundle (BOOTSTRAP
  §14, "Open it"), for `--zuliprc`. The provisioner owns every participant bot (ADMIN §1.5), and
  `GET /bots` lists only its caller's bots: with any other key, `zulip_provision.py` finds none of
  them, its dry run says `would create`, its run fails on names that exist already, and
  `--deactivate` reports each bot as `not among the provisioner's active bots`.
- **The break-glass key,** your dedicated key, `~/.ssh/zab-deployer` (BOOTSTRAP §2.1), which every
  VM got for `ubuntu` at its first boot (`admin_keys`; ADMIN §1.7). Run the scripts in BOOTSTRAP
  §2.1's terminal, whose agent holds it: the key has a passphrase, which ssh can't ask for under
  `BatchMode=yes`. Also set `ZAB_SSH_IDENTITY=~/.ssh/zab-deployer`, so that every script that
  connects to the VMs uses that key alone. The server's own fleet key never leaves the server.
- **The fleet account's AWS profile,** `AWS_PROFILE=<profile>`, for `status.py`, `roster_import.py`,
  `roster_sync.py` and the scripts of `workshop/aws/`; and the server's GitHub token, from the same
  bundle, as `GH_TOKEN` (§1b).
- **Don't mix the two paths.** What you change by hand is in your copy, not the server's: tell the
  organizers which rows are yours, and make the server's copy current again before they use the bot
  for those rows (ORGANIZERS §7).

**A fleet made by hand,** from a laptop, with no server:

- **The roster** you launched with, and `out/` (by default `workshop/admin/out/`).
- **The bots' owner's zuliprc.** A bot belongs to the user whose key created it, and Zulip lets it
  post wherever its owner can, so a participant's agent could post in every private channel that
  owner is in. Make the bots with a dedicated provisioning account, a Moderator such as BOOTSTRAP
  §5.1's `zab-provisioner`, never with your own key. Bots made with your own key can be given to
  that account with `move_bots.py --to provisioner`, a dry run first (its docstring; ADMIN §1.5,
  §8.4), with an organization administrator's or organization owner's key: Zulip Cloud refuses a
  bot's owner change to anyone else, the bot's owner included (#758). Bots made in Zulip's web UI
  are reused only if the key's user owns them.
- **SSH as `ubuntu`** to every VM, with the key `launch.py` gave them (`--key-name`, `--admin-key`),
  loaded in `ssh-agent`, and named by `ZAB_SSH_IDENTITY` to use it alone. A key file without a
  passphrase may be named by `ZAB_SSH_IDENTITY` alone.

**Either way:**

- `roster.csv` in the §4 format, with `ip` filled in by `workshop/aws/launch.py`. The scripts
  connect to the `ip` column only (the `host` alias `ws-<slug>` resolves only through your own
  `~/.ssh/config`), and they refuse a roster in which two rows share a login, e-mail, host, IP or
  Zulip id, since that would put one person's keys on another's VM.
- The scripts run `ssh` and `scp` non-interactively (`BatchMode=yes`), so a key with a passphrase
  must be in `ssh-agent`. Agent forwarding is always off, and so is X11 forwarding
  (`ForwardAgent=no`, `ForwardX11=no`), whatever `~/.ssh/config` says.
- The VMs' host keys recorded once with `scan_hosts.py` (§0): every later connection pins them
  (`StrictHostKeyChecking=yes` against that `known_hosts` alone).

## 0. `scan_hosts.py`, once, right after the launch

```bash
uv run python workshop/admin/scan_hosts.py --roster roster.csv
```

Runs `ssh-keyscan` against every `ip` and checks each ed25519 key's `SHA256:` fingerprint
against the row's `host_keys`: the fingerprints cloud-init printed to the instance's console at
first boot, which `launch.py` read and recorded. Only a matching key goes into `out/known_hosts`
(git-ignored), so **you compare nothing by hand**; a key that is not among them is refused as
`MISMATCH`, even with `--replace` (whatever answered on that address is not the VM: stop and
look). A row without `host_keys` (a VM launched before the column, or one stopped before
`launch.py` read its console: cloud-init prints the block once per instance, so after a stop and
start it is gone for good) is recorded with a warning and reads `unverified`. A VM that holds no
work yet is best relaunched, in the region its row records and with that region's credential:
`teardown.py --region <its region> --only <slug>`, clear the row's `host`, `ip` and `host_keys`
(keep `region`), `launch.py --only <slug>` with the launch's flags, then scan again. Otherwise
compare the key with `aws ec2 get-console-output --latest --instance-id … --output text`,
between the `BEGIN SSH HOST KEY FINGERPRINTS` markers, or accept it knowingly: it was trusted on
first use. A key that differs from the one already in `known_hosts` is refused unless
`--replace` is given (a reinstalled VM; anything else is a reason to stop and look).

## 1. `zulip_provision.py`

```bash
# before the workshop: bots, zuliprc files, bots-only sandboxes; nothing a participant sees
uv run python workshop/admin/zulip_provision.py --roster roster.csv --zuliprc provisioner.zuliprc --no-rebind --hold --dry-run
uv run python workshop/admin/zulip_provision.py --roster roster.csv --zuliprc provisioner.zuliprc --no-rebind --hold
# the first morning: everything below
uv run python workshop/admin/zulip_provision.py --roster roster.csv --zuliprc provisioner.zuliprc --no-rebind --dry-run
uv run python workshop/admin/zulip_provision.py --roster roster.csv --zuliprc provisioner.zuliprc --no-rebind
```

`--no-rebind`, as the admin bot passes it (§1b, below), keeps the `zulip_user_id` of a row that
`roster_sync.py` linked by its ID: its e-mail may be a placeholder that matches nobody, and
without the flag the run clears the ID, so the participant's bots no longer recognize them.

**Prerequisite, before any invitation goes out:** the organization's default for *Who can
access user's email address* is *Admins, moderators and members*; *Everyone* works too
(BOOTSTRAP §5.2; SPEC §5.2).
Each bot recognizes its participant by `allow_emails`, matched against the address Zulip shows
the bot, which is a member, and the provisioner, a Moderator, binds each row by that address.
Under a stricter default a participant who joins later shows the bot a synthetic address, and
their own bots ignore their `!login`. The setting is the default for accounts created
afterwards; users who joined earlier keep their own, but this script records their
`zulip_user_id`, which `push_config.py` renders into `allow_user_ids`, so they are covered
unless their address is hidden from the provisioning key (ORGANIZERS §3.3).

**With `--hold`** the script does steps 1 and 2 below and creates each sandbox with **only the
two bots** subscribed and a neutral description ("Your workshop sandbox. Instructions will be
posted here on the first morning of the workshop."). It subscribes no participant and posts no
"Start here"; the table says `held`, and the roster changes only in `zulip_user_id` and
`zulip_channel_id`. Participants must not see instructions or talk to agents before the
workshop, when their sign-in gifts arrive. A later run without `--hold` releases everything: the
participant joins the sandbox and administers it, the description and "Start here" follow, once.
With `--hold`, an existing channel keeps its description (only a new one gets the neutral text),
so a `--hold` run after a full one takes nothing back.

**Without `--hold`**, for each participant it:

1. looks their Zulip user up by the roster **e-mail** alone (Zulip verified it at signup). Not
   having joined yet is normal: the organizers invite them in Zulip (step 4). The self-reported
   **GitHub** custom profile field (BOOTSTRAP §5.7) never binds a user; it is only reported, as
   a warning (`provision_repos.py` reads it for the repository invitation). A Zulip user matched
   by two rows is bound to neither, and its rows get nothing provisioned until the roster is
   fixed;
2. creates the generic bots `Codex · <slug>` and `Claude · <slug>` (reusing existing ones, also
   ones named with the login's capitals from before slugs), with `POST /api/v1/bots`, and writes
   `out/<slug>/codex.zuliprc` and `out/<slug>/claude.zuliprc` (0600, in a 0700 directory).
   Slugs keep bot names unique, so mentions stay unambiguous (SPEC §4.5). The bots are owned by
   their creator, the provisioning account; each VM's config allows only the participant
   (`allow_emails`, then `allow_user_ids`; SPEC §5.2), so nobody else, the organizers included,
   spends the participant's subscriptions;
3. creates the private channel `<slug>-sandbox`, with shared history, with both bots, and the
   participant if they are already a user, subscribed. **The key's user is not subscribed**, but the
   channel lets it add subscribers (`can_add_subscribers_group` = that user), which Zulip requires
   for subscribing the participant once they have joined and for repairing its membership on a
   rerun. The participant administers it (`can_administer_channel_group`): they invite
   collaborators, who see its whole history, and may make it public or change its history. A sandbox
   created before they joined is handed over on their first run as a member, the run that records
   `zulip_invite = joined`: they join its administrators, and protected history becomes shared.
   Later runs never change its administrators, history or privacy. A public sandbox is reconciled
   like a private one, and the roster's `zulip_channel_id` finds a renamed one. An archived one is
   not found, and recreating it fails with "already exists" (archived channels keep their names):
   unarchive it, or leave the row's note alone. The description carries the workshop steps;
4. reports a participant who has not joined as `not on Zulip` under `invite`, with a line
   after the table, `<slug>: not on Zulip: invite <e-mail> in Zulip (…)`. It sends **no
   invitation** (#628): the organizers invite each participant in Zulip, with the
   participants' channel ticked (`docs/ORGANIZERS.md` §1.5), and a rerun binds them once they
   have joined. A participant who joined but hides their address from the key is reported the
   same way (`docs/ORGANIZERS.md` §3.3).
   It is not a failure;
5. has the participant's Claude bot post the steps in a **"Start here"** topic, once (#709): the
   gifts (`--gifts-step`, or `GIFTS_STEP`), then signing in to Claude and ChatGPT; device code
   sign-in for Codex in ChatGPT's settings; `!login` in a direct message to each bot, with how
   each agent's sign-in goes (Claude's code is pasted back into the DM, Codex's is typed at its
   link). There and in the channel description the two bot names are links that open a direct
   message with each bot (`#narrow/dm/<bot id>-<name>`). Last come the repository `<org>/<slug>`
   (`--repo-owner`), with the participant's GitHub username in their Zulip profile's GitHub field
   to see it, and that links to files on the file server (`--refserve-url`'s host) open that copy
   of the repository to anyone who has them (SPEC §10.7).

It fills `zulip_user_id`, `zulip_invite` (`joined`) and `zulip_channel_id` in the roster. `--only
SLUG` limits the run; `--regenerate-keys` rotates existing bot keys. Rows marked `warning:` in the
table succeeded; read the warning anyway. Rerun it any time: everything is idempotent, and a
participant who joined meanwhile is reported as `joined`. For a late joiner, rerun it with `--only
<slug>`, then `provision_repos.py --only <slug>`. Zulip's API rate limit (HTTP 429) is retried
automatically, here and in `provision_repos.py`: a run may pause for up to a minute or so (`zulip:
rate limited, waiting … s` on stderr).

A sandbox that doesn't let the key's user add subscribers is reported for its row: set "Who
can subscribe others" to include that user in the channel's settings, or delete the channel,
and rerun.

**`--deactivate`** deactivates the workshop's bots, which invalidates their keys (§7.3); with
`--only SLUG` (repeatable), only those participants' bots. It uses `DELETE /bots/{id}`:
`DELETE /users/{id}` refuses bots with "No such user".

**Bot endpoints** (shapes read from Zulip's `zerver/views/users.py`, `get_bots_backend` and
`add_bot_backend`): `GET /bots` lists the bots with `username` (e-mail) and `api_key` but no id,
and `POST /bots` returns `user_id` and `api_key` but no e-mail; the script fills the gaps from
`GET /users` and `GET /users/{id}` and never writes a zuliprc with an empty `email=`.
`GET /bots` lists only the bots of the key's user: a bot made in Zulip's web UI is reused, by
name, only if that user owns it ("Prerequisites").

## 1b. `provision_repos.py`: the participants' repositories, before `push_config.py`

```bash
export GH_TOKEN=…   # or `gh auth login`; the script also asks `gh auth token`
uv run python workshop/admin/provision_repos.py --roster roster.csv --zuliprc provisioner.zuliprc --org <org> --no-rebind --dry-run
uv run python workshop/admin/provision_repos.py --roster roster.csv --zuliprc provisioner.zuliprc --org <org> --no-rebind
```

For each participant row, the script uses the row's `slug`, or its `github` login in a roster
without slugs, as the repository name; that name must match what `launch.py` recorded on the VM.
It then:

1. creates the private repository `<org>/<slug>` (unless it exists, and refuses an existing
   one that is public) and pushes `workshop/template/` as its first commit (unless the repository
   already has commits);
2. checks over SSH, as `ubuntu`, that the VM's own record names this participant;
3. generates the deploy key `~/.ssh/zab_<slug>` on the VM, as `agent`, and registers its public
   half on the repository with write access (unless it is already there);
4. runs `zab setup-repo <org>/<slug> --yes` on the VM, as `agent`. That sets up the SSH
   alias, clones to `~/work/<slug>`, records the checkout for `zab preflight`, and test-pushes a
   branch, which it deletes again;
5. invites the participant to **their own repository** as its **admin** (#714: they add
   collaborators, and may transfer or delete it), unless the login is already an admin (an
   organization owner is one too) or has an open invitation. A collaborator with less is
   raised to admin, and so is an open invitation (`PATCH /repos/{owner}/{repo}/invitations/{id}`):
   the row says `collaborator; raised to admin` or `pending; raised to admin`. The login is the
   "GitHub" custom profile
   field of the participant's Zulip user (the row's `zulip_user_id`), read with the key of
   `--zuliprc`; the roster's `github` is used only while that field is empty, as a
   manual override. It is self-reported (a typo is the participant's): the script checks its
   syntax, looks it up on GitHub (`unknown login` otherwise) and invites nobody for a login two
   rows claim. When the login changed since the last run, the old login's invitation is
   withdrawn and its access removed first. There are no organization invitations.

The roster gets `repo` (`<org>/<slug>`), `repo_status`, `github` (the login) and
`github_invite` (`invited`, `pending`, `collaborator`, which means admin, `no login in Zulip`,
`not on Zulip yet`, `unknown login`, …). A failed invitation is reported but does not fail the row. Reruns are
safe: they only redo what is missing and re-test the push; **rerun during the workshop** to
invite logins participants added to Zulip since. `--only SLUG` limits the run; `--no-invite`
skips step 5 and needs no zuliprc. `--dry-run` reads GitHub and Zulip and runs the owner check,
and changes nothing. If you replace one manual override in `github` with another by hand, the
script cannot know the old one: remove it on GitHub (repository Settings → Collaborators).
Emptying the Zulip GitHub field does **not** revoke access: the row's `github`, the last login
recorded, is then the fallback. To take access away, put the right login in the field (the old
one is removed on the next run), or clear both the field and the row's `github` and remove the
collaborator on GitHub by hand. A row without `zulip_user_id` (someone who joined after
`zulip_provision.py` ran) is bound by its roster e-mail, so this script alone invites late
joiners; the next `zulip_provision.py` run records the id.

**`--no-rebind`** (both scripts; the admin bot always passes it, docs/ADMIN.md §2.4): identity never
moves on its own. `zulip_provision.py` never replaces or clears a recorded `zulip_user_id`; a row
whose e-mail now matches another user, or none, is left untouched and reported (`binding changed:
refused`, exit 1). `provision_repos.py` never removes a previous login that has access: it withdraws
only that login's pending invitations, checks its permission again, and reports a row whose old
login has access (`login changed: refused`), keeping it in the roster. A rebind is done by hand.
With or without the flag, `zulip_provision.py --only` never binds a user whose id another row of the
roster records, and a `zulip_user_id` that is not ASCII digits refuses the run (`0101` is user 101).
`zulip_provision.py --repo-owner ORG` names the owner of a roster `repo` given without one in "Start
here": pass your organization (the box at the top).

**Invitations only, during the workshop** (`--invites-only`):

```bash
uv run python workshop/admin/provision_repos.py --roster roster.csv --zuliprc provisioner.zuliprc --org <org> --no-rebind --invites-only
```

This does step 5 alone, and only for rows already provisioned (`repo_status` = `ok`). There is no
SSH, no repository creation and no deploy key. It invites new logins, replaces a changed login
(the old one's invitation and access go first) and an expired invitation, raises a collaborator
or an invitation with less than admin, and notices accepted ones. Every row with a login gets three or four GitHub reads per run, so a collaborator removed
by hand is invited again on the next run. It prints only the rows whose state changed or
failed, then one summary line, e.g. `invites: 1 invited, 2 accepted, 0 removed, 0 failed; 36
unchanged`, with `N raised to admin, ` after the invited when there are any. A run that changes nothing prints that line alone and leaves the roster file
untouched. Rows never provisioned are listed in that line (`… not provisioned yet (…): run
provision_repos.py without --invites-only`) and not touched. The exit code is 1 only when an
invitation failed. With `--dry-run`, a run that would change nothing (nothing to invite or
withdraw, no acceptance or other state to record, no failure) prints `nothing to change: no
invitation to send or withdraw, and no acceptance to record` under that line and exits 3, which
the admin bot answers with no plan (#708). It is safe in a loop, e.g. every 15 minutes:
`while sleep 900; do … --invites-only; done`.

**Your token stays on your laptop.** It goes only to api.github.com and, for the template push,
to github.com, through git's environment (never an argument, a URL or a file). It is never
printed or written to the roster, and it never reaches a VM: each VM holds only its own deploy
key.

**Token** (a fine-grained personal access token is best):

- Resource owner: **your workshops' GitHub organization**, `<org>`. Repository access: **All
  repositories**, so that the repositories the script creates are covered. The organization must
  allow fine-grained tokens (Settings → Personal access tokens), and may have to approve this one.
- Repository permissions:
  - **Administration: read and write**. It covers creating the repositories
    (`POST /orgs/{org}/repos`), the deploy keys (`GET`/`POST /repos/{owner}/{repo}/keys`) and
    the collaborators and their invitations (`PUT`/`DELETE
    /repos/{owner}/{repo}/collaborators/{username}`, `GET`/`PATCH`/`DELETE
    /repos/{owner}/{repo}/invitations`).
  - **Contents: read and write**, for the template push and the empty-repository check.
  - **Metadata: read**, which is always included; it covers the access check
    (`GET /repos/{owner}/{repo}/collaborators/{username}/permission`).
- No organization permission: participants are invited to their own repository only.
- With a classic token instead, the scope is **`repo`**.

(From GitHub's "Permissions required for fine-grained personal access tokens", checked on
2026-10-04, and for the collaborator endpoints on 2026-10-05.)

**In the organization's settings**, set Member privileges → Base permissions to **No
permission**: participants are invited to their own repository, never to the organization, and
a member would otherwise read every workshop repository.

## 2. `push_config.py`

```bash
# the release must be named: a wheel built with `uv build` from the release tag, or the tag
uv run python workshop/admin/push_config.py --roster roster.csv --refserve-url <refserve_url> --refserve-owner <github_org> --mention-after-s <mention_after_s> --wheel dist/zulip_agent_bridge-<version>-py3-none-any.whl --dry-run
uv run python workshop/admin/push_config.py --roster roster.csv --refserve-url <refserve_url> --refserve-owner <github_org> --mention-after-s <mention_after_s> --wheel dist/zulip_agent_bridge-<version>-py3-none-any.whl
uv run python workshop/admin/push_config.py --roster roster.csv --refserve-url <refserve_url> --refserve-owner <github_org> --mention-after-s <mention_after_s> --tag v<version>   # from the public repository
```

Over SSH as `ubuntu` to the roster's `ip`, for each VM, in parallel (`--parallel 8`) and
independently:

0. **The owner check.** It reads the VM's own record of its owner (`/etc/zab-participant.json`
   and the hostname, both written at first boot, `workshop/ami/README.md`, "First boot"), and
   unless both name this participant, fails the row and copies nothing. A spare is accepted only
   for a row named in `--adopt-spare <slug>` (repeatable), which rewrites the record and the
   hostname for the new owner first, and gives `agent` the row's git name and e-mail.
1. It upgrades zab as `agent` (`uv tool install --force`, from the wheel or the tag).
2. It installs the two zuliprc files into `/home/agent/.config/zab/` (0600, owner `agent`).
3. It writes `config.toml` from the workshop profile (SPEC §5.3): `machine = <slug>`,
   `default_project = ~/work/<repo>`, `default_workspace = "worktree"` (each topic works on its
   own branch in a worktree of the checkout), `max_concurrent_turns = 4`, `mention_after_s`
   (`--mention-after-s`, §2.4), both bots allowed only for the participant (`allow_emails` set
   to the roster e-mail, and `allow_user_ids` to the participant's id once `zulip_provision.py`
   has recorded it), Codex `never` +
   `danger-full-access`, Claude `bypassPermissions`, both at effort `high`, and `[render]
   refserve_url`, `--refserve-url`'s value, the central file server for turn snapshots (SPEC
   §10.7): a turn whose answer or diff links a file pushes its snapshot to the participant's
   repository as `refs/zab/turns/<session>/<turn>` (never a branch), and the links point to
   `<refserve_url>/<repo>/<sha>/<path>`, readable by anyone with the link. Its default is the
   MathAgents deployment's file server (the box at the top); `--refserve-url ''` leaves links
   off. Beside it, `refserve_owner`, `--refserve-owner`'s value (#693): the GitHub organization
   that file server serves, its `ZAB_FILES_ORG` ([`workshop/files/README.md`](../files/README.md)
   §3), which is the workshop's `github_org`. The bridge pushes a snapshot only for a checkout of
   a repository of it, so a workshop of another organization than the default, `MathAgents`,
   passes its own, or gets no file links. A VM keeps the value until a run with a release
   rewrites its `config.toml`; a config written before #693 has none, and its bridge takes
   `MathAgents`.
4. It installs the services and checks them, as `agent`, with the user runtime directory set
   (lingering makes it exist):

   ```bash
   sudo -u agent XDG_RUNTIME_DIR=/run/user/$(id -u agent) \
     DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u agent)/bus \
     bash -lc 'zab service install && zab doctor --service-env --json'
   ```

   `zab service install` restarts a unit whose text changed. One exception (#664): a Codex
   daemon whose unit gained only its open-file limit, `LimitNOFILE=`, keeps running with its
   turns, and gets the limit at once with `prlimit`. If that
   fails, the row says so in a note, `codex-app-server.service: its open-file limit (65536)
   applies at its next start`: the daemon keeps its old limit until the VM boots or the
   participant sends `!restart codex`.

   The Codex daemon can still be starting when the first doctor runs: while a check reads
   "daemon unavailable", the doctor alone is retried every 3 s for up to 60 s before the VM is
   reported. What a failing check means for the row is §2.1.
5. Last, the agents' instructions, after the VM's list of its software and the `sage` wrapper
   (§2.2). This step runs after the release, the services and the doctor, so it never holds them
   up.
6. Then, after `--lean-shared` and before `--idle-stop`, or their defaults (§2.4), the page-cache
   warm-up (§2.3).

It collects the results into one table; the exit code is non-zero if any VM failed. Every
rollout holds idle stop off the VM while it runs (§8).

`--dry-run` connects to nothing. It prints one line per VM with each command by name, and the
files it would send (a bot key with the `out/` directory it comes from), then the table:
`dry-run alice (203.0.113.5): owner check; staging dir; idle-stop hold; scp config.toml
alice/codex.zuliprc …; config installed; zab installed; service install + doctor; …`. That line
is what an admin bot's plan shows and hashes (docs/ADMIN.md §2.4, #361). `--dry-run --verbose`
prints every `ssh` and `scp` command whole instead.

### 2.1 Pending rows

Before the participants sign in, every doctor reports both accounts as signed out (`codex:login`,
and `claude account main`: "not signed in: send !login to this bot in a direct message"). A VM
whose **only** failing checks are these sign-in checks, matched by name, reads `ok (sign-in
pending)`: it counts neither towards "N VM(s) failed" nor the exit code. Any other failing check
makes the row `FAIL`.

**Before a participant joins Zulip** (#401), their row has no `zulip_user_id`, so the config
allows only their e-mail. The doctor resolves it as the bridge does (SPEC §5.2): the
`<bot>:allowlist` check passes when a Zulip user the bot can see has that address, which needs
the address to be visible to members. Otherwise both bots' allowlist checks fail ("no allowed
user yet: …"). On such an **unbound** row these are pending too: the row reads `ok (owner+sign-in
pending)`, or `ok (owner pending)`, with exit 0, and a line under the table counts these VMs:
the next `zulip_provision.py` records the participant's ID once they have joined, and the rollout
after it writes the ID. On a **bound** row the rollout writes the ID, so the check passes; a
failing allowlist check there stays a `FAIL`, and so does the doctor's "nobody allowed: …" on
any row. The rule is in the admin checkout (`admin_common.allowlist_pending`), so it covers VMs
on older releases too.

**During a hand-over** (`--adopt-spare <slug>`, ORGANIZERS §7.2; #458), the row's checkout
`~/work/<repo>` doesn't exist yet: a spare's first boot makes only `~/work`, and
`provision_repos.py` clones the checkout after this run. With `--adopt-spare`, and only for the
rows it names, the doctor's check of exactly that path (`project:/home/agent/work/<repo>`) is
pending while nothing at all is there, not even a dangling link: the row reads `ok
(project+sign-in pending)` with exit 0. Anything else at the path, another `project:` check, or a
look at the path that fails stays a `FAIL`. The run without `--adopt-spare` that ends the
hand-over checks the doctor in full, so a checkout still missing then fails it.

### 2.2 The agents' instructions, the tools list and the `sage` wrapper

In step 5, in this order:

- **The `sage` wrapper** (#482): as `agent`, `sage_wrapper.py install` replaces the link
  `~agent/.local/bin/sage` of an image built before the wrapper with the newer images' wrapper
  (`workshop/ami/files/bin/sage`),
  which puts the Sage environment on `PATH` so that Sage's `giac(…)`, `r(…)` and `gap(…)` work.
  It acts only on what it recognises exactly, and notes `sage: …` for the row; its module
  docstring has the rules. A failure is `sage wrapper: …` in the failures column, and holds
  nothing up.
- **The VM's list of its software** (#475), `/etc/zab/tools.json`, which participants see with
  `!tools` (`workshop/ami/README.md`, "What the image holds"). As root, `tools_list.py install`
  gives an image built without one the list that the release carries, the first image's
  (`tools-nt-agents-2026-v3.json`), less each tool whose paths that VM lacks, marked as the
  rollout's own so that a later rollout may replace it. An image's own list is never replaced. A
  failure is `tools list: …` in the failures column, and holds nothing up.
- **The instructions:** `~/.codex/AGENTS.md` gets a managed block, between
  `<!-- zab:managed:start -->` and `<!-- zab:managed:end -->`, from
  `workshop/ami/files/agent-instructions.md`, whose line `<!-- zab:tools -->` becomes the tools
  section, made from that list, so that the instructions never name a tool the VM lacks. Below
  the block is the participant's own section, *About me and standing instructions*, where the
  agents note what the participant asks them to remember. A rerun replaces only the block.
  `~/.claude/CLAUDE.md` becomes `@~/.codex/AGENTS.md`, so both agents read one file. The splice
  runs on the VM as `agent` (`agent_instructions.py`, uploaded with the text); a file with broken
  markers, or that is not UTF-8, is left alone and reported (`agent instructions: …`). Every
  string of the list is made inert before it reaches the block: no `@` that Claude Code's import
  scanner could take for an import (DECISIONS' verification log, 2026-10-18), and no HTML comment
  or format character; `tools_list.py` and `agent_instructions.py` say how.

### 2.3 The page-cache warm-up

Every release rollout installs `/usr/local/lib/zab/warm_cache.py` and the root unit
`zab-warm-cache.service` (#461, #483), enabled for every later boot, a start by the waker
included, and not started by the rollout, which runs its dry run instead: a row fails if the
script can't run there. After each boot it waits for the bridge, then reads into the page cache
the agents' CLIs, then the shared Mathlib and its toolchain, then Oscar's files on a VM whose
participant used Oscar in the last 7 days (#475). Its reads are capped (80 MB/s and 1500 IOPS,
under gp3's baseline, which the bridge and the agents share), it reads at most half the
available memory, and its own reads leave access times alone. On the test VM, an `import
Mathlib` started right after a wake took 85 s with it (110 s without), and about 10 s once it was
done, about 3 minutes after boot. `journalctl -u zab-warm-cache` has its line for each boot,
`zab-warm-cache: read …`. `warm_cache.py`'s module docstring has the order, the rule for Oscar
and its limits; `--uninstall` removes it.

### 2.4 Other options

- `--claude-steer` (the default from #310) writes `[backends.claude] steer = true`: a message sent
  during a Claude turn goes into it (↪️), as with Codex. `--no-claude-steer` writes `steer =
  false`, and such messages wait for the turn to end (⏳). One line says which: `claude steer: ON`
  or `OFF`.
- `--multi-party` (the default) writes `bridge.multi_party` where the release has it (#35), and
  prints `multi_party: on`, `OFF` or `absent from this release`. **`--no-multi-party` is the
  fleet-wide kill switch** for a multi-party bug: it rewrites every VM's config and restarts
  every bridge, which interrupts running turns, so run it at a break, or with `--only` for the
  affected rows first. With it off, an agent mentioned in a topic also answers its owner's later
  messages there without a mention, and a message that starts with a mention of someone else's
  bot no longer reaches the sender's own. It also limits #713's rule: with it on, a message
  without a mention goes to both agents in a channel where the participant is the only person,
  starting the session of one without a session in the topic (SPEC §4.5); with it off, such a
  message goes only to the agents with a session in its topic, in any channel, and starts none,
  so new topics need a mention (ORGANIZERS §4.1).
- `--mention-after-s SECONDS` (#711), 0 to 86400, default 120, writes `[bridge] mention_after_s`
  (SPEC §4.8): a turn that ends after running SECONDS or more mentions its owner, as a failed
  turn and an approval do, so the topic shows **@** in Zulip's left sidebar; at 0, every turn
  that completes or fails does. A turn that a bridge restart ended, and a failed sign-in, never
  mention (ADMIN §2.1). Pass the workshop's, `admin.toml`'s `mention_after_s`, as the
  admin bot does: a run without it writes 120. One line says the value: `mention after: 120 s
  (…)`. It needs a release, since only a release rollout writes `config.toml`.
- `--claude-raw-log meta` records each later Claude turn's raw stream, line types, block kinds and
  lengths, no content, in `/home/agent/.local/state/zab/raw/<turn>.jsonl`, for a lost answer
  ("(no final message)"); `content` records the lines themselves, the participant's work, so
  only with their consent. It restarts that bridge; a rerun without the flag turns it off.
- `--restart-codex` stops the old bridge first, then restarts the Codex daemon unless a Codex turn
  is running (the row's note says so; rerun it later), then starts the new bridge. Codex threads
  loaded before an upgrade keep their old environment until the daemon restarts, and the bridge
  refuses to reopen them until then: a release that changes that environment needs it, as the
  per-bot commit identity did (#214). It needs a release.
- `--lean-shared` (#205) makes `~/lean/workspace`'s Mathlib the VM's one shared, read-only copy
  that Lean projects link to; harmless where it is shared already. `--lean-reclaim report|apply`
  lists, or replaces with the shared link, the private Mathlib copies in the participant's
  checkouts and worktrees (only while no turn runs).
- `--idle-stop MINUTES` (§8).
- **The defaults** of the admin bot's `!rollout`, which passes them when the command leaves out
  `lean-shared` or `idle-stop=` (ORGANIZERS §3.5). Both need a release, and each excludes its
  explicit flag. `--has-waker` goes with `--idle-stop-default`.
  - `--lean-shared-default` runs `--lean-shared`'s step, its check and rollback included, on a
    VM whose Mathlib isn't shared yet: `~/lean/workspace/.lake/packages` is a directory, or
    `lean-project` is missing. On a VM that has both, only a probe runs, with the instructions
    step, and no timing build. A VM with no Lean workspace is noted, not failed.
  - `--idle-stop-default MINUTES` (at least 10) runs `--idle-stop MINUTES`'s step, last, on each
    VM whose waker is on and whose idle-stop timer isn't on, unless its idle stop was turned off
    on purpose (§8). Every VM is probed first. A timer that is on stays as it is. Where the waker
    is on, a timer installed but not enabled is turned on again. Every other VM is left as it
    is, with a note, which is a warning where the timer is on and the waker isn't.
  - `--has-waker SLUG[=INSTANCE]` (repeatable) names a row whose waker is on; `all` names every
    selected row. With `=INSTANCE`, as the admin bot passes it, the VM must also be that EC2
    instance, the one the waker's config names for the row. The VM's own ID is read from
    cloud-init's `/var/lib/cloud/data/instance-id`. A VM replaced by hand since its `!waker on`
    is one the waker can't start, so it gets no idle stop. A slug that isn't in the roster is
    an error, and so is a slug given twice.

  A hand rollout equal to the bot's `!rollout v<version> all`, once every row's waker is on (§8:
  the waker first):

  ```bash
  uv run python workshop/admin/push_config.py --roster roster.csv --refserve-url <refserve_url> --refserve-owner <github_org> --mention-after-s <mention_after_s> --wheel dist/zulip_agent_bridge-<version>-py3-none-any.whl --lean-shared-default --idle-stop-default 60 --has-waker all
  ```

  Otherwise name the rows whose waker is on: `--has-waker alice --has-waker bob`. The dry run
  shows each VM as a new one, with every step the defaults may take.
- `--uninstall` (§6).

## 3. `status.py`

```bash
AWS_PROFILE=<profile> uv run --with boto3 python workshop/admin/status.py --roster roster.csv \
    --tag workshop=<ws> --tag zab-scope=<SCOPE> --csv status-$(date +%H%M).csv
```

Gathers `preflight.json`, `zab doctor --service-env --json`, as `push_config.py` runs it, and `zab
status --json` from every VM, and prints a table: participant, host, Codex login, Claude login,
git push, math tools, bridge, sentinel (#588: `ok`, or a state such as `down` in which DM
`!restart` won't work), active sessions, load, memory, disk, last error, then one line such as
`signed in: 12 both, 3 Codex only, 2 Claude only, 19 neither; 0 unreachable, 0 stopped, 0 doctor
FAIL`. Unreachable hosts are rows. Run it every 30 minutes during the workshop.

- **The logins** come from the live doctor, so that a stale `preflight.json` can't hide a
  sign-out: `ok` when signed in, `no` when only the sign-in checks fail, `FAIL` for any other
  failure (a Codex daemon down, `claude auth|config <name>`). Git push comes from a preflight push
  test, else the roster's `repo_status`; the math tools from the doctor's `service:python` and
  `service:gp`.
- **The bridge** reads `active, sign-in pending` when the only failing doctor checks are the
  sign-ins (as `push_config.py`'s `ok (sign-in pending)`), and an unbound row's allowlist checks
  add `owner`: `active, owner+sign-in pending` (#401; §2.1). A bound row whose VM still allows the
  e-mail only passes while the bots can see the address, and otherwise shows `active, doctor
  FAIL` until a rollout of that row writes the ID; any other failing check shows `active, doctor
  FAIL` and the check under *last error*.
- **Strain.** Load (the 1-minute average over the CPU count), memory in use and the root file
  system's use are each marked `!` when the VM is under strain (load at least the CPU count, under
  10% of memory available, disk 90% full; a full disk also names its free space, in binary GB as
  `df -h` counts them: `92% ! (15 GB free)`), and a line before the summary names those VMs, for
  example `under strain: bob (mem)`: look there first when a participant reports a slow or silent
  agent.
- **Stopped VMs** (idle stop, §8): EC2 is read first, by the scope tags, once per region in the
  roster's `region` column (us-west-2 when empty). The match is the instance at the row's `ip`,
  never the `participant` tag, which after a hand-over still names the old VM. A VM that EC2 says
  is stopped, stopping or starting is not tried over SSH (#439): its bridge reads `stopped`,
  `stopping` or `starting`. A running VM that SSH can't reach is looked up again, and otherwise
  stays `unreachable`: a real problem. The lookup needs boto3 and a credential with
  `ec2:DescribeInstances` (`AWS_PROFILE=<your profile> uv run --with boto3 python
  workshop/admin/status.py …`); without them every VM is tried over SSH. `--no-ec2` skips it.

`--json` prints the same rows as JSON for the admin server (docs/ADMIN.md §4.1), plus per VM the
zab version, idle stop (timer enabled and active, its minutes, the last verdict in
`/var/log/zab-idle-stop.log`) and the last activity. The
table is unchanged. `--tag KEY=VALUE` (repeatable) scopes the EC2 lookup, as in `workshop/aws/`.

## 4. The roster: `roster.csv`

```
slug,github,first_name,last_name,email,repo,host,ip,region,host_keys,zulip_user_id,zulip_invite,zulip_channel_id,notes
```

Those are the columns every roster has. The scripts add four more, `repo_status`,
`github_invite`, `name` and `excluded`, and keep any column they don't know. The organizers
supply the input columns (ORGANIZERS §3.1), or `roster_sync.py` makes the rows from the
workshop's participants' channel (§4a): one per subscriber, linked by `zulip_user_id` from the
start, with their Zulip name in `name`.

- `slug`: the participant's identity, from which everything is named: the VM `ws-<slug>` and its
  `participant` tag, the bots `Claude · <slug>` and `Codex · <slug>`, the channel
  `#<slug>-sandbox`, the repository `<org>/<slug>` and `~/work/<slug>`. Lower-case letters,
  digits and inner hyphens, 1 to 32 characters. **Reserved**, since the admin commands read
  them as keywords or settings (`RESERVED_SLUGS` in `roster_import.py`, which `roster_sync.py`
  shares): `all`, `spares`, `hold`, `protect`, `lean-shared`, `restart-codex`, `invites-only`,
  `no-invite`, `unsynced`, `export`, `steer`, `idle-stop`, `multi-party` and `raw-log`; and
  `zab-spare-N`, the spares' names.
- the names, `first_name` and `last_name`, and `email` (required): the address the organizers
  invite to Zulip (ORGANIZERS §1.5), which the bots also allow (`allow_emails`), and git's
  `user.email` on the VM. A row of `roster_sync.py` has the address the provisioner sees, or
  Zulip's placeholder for one hidden from it, and its `zulip_user_id` binds it either way.
- `repo` (optional): another checkout name under `~/work/` instead of the slug, or `owner/name`.
- `region` (optional): assigns a row to a region before its first launch
  ([`workshop/aws/README.md`](../aws/README.md), "Regions").
- `github` (optional): a manual override for a participant whose Zulip GitHub field stays empty
  (§1b); otherwise `provision_repos.py` writes it.
- `notes` (optional): free text for the organizers; no script acts on it.

Slugs and e-mails must be unique, compared case-insensitively. The scripts fill in the rest:

| Column | Filled in by |
|---|---|
| `host`, `ip`, `region`, `host_keys` | `launch.py`, or `adopt_spare.py` for a spare handed over (`host_keys` holds the console's host-key fingerprints, which `scan_hosts.py` checks) |
| `zulip_user_id`, `zulip_invite`, `zulip_channel_id` (the sandbox) | `zulip_provision.py` |
| `repo`, `repo_status`, `github`, `github_invite` | `provision_repos.py` |
| `name`, `excluded` | `roster_sync.py` (`excluded` = `yes` for a row `!roster remove` took out) |

Each script writes the roster back atomically, only its own columns' changes, keeping
`roster.csv.bak`, every row and column. The roster contains personal data: keep it out of git
(`.gitignore` covers `roster*.csv`).

## 4a. The roster from the participants' channel: `roster_sync.py` (ADMIN §3.1a)

The admin bot runs it for `!roster sync`, `!roster remove`, `!roster restore` and `!roster
rename` (ORGANIZERS §3.1). By hand, with the provisioner's key, which must be subscribed to the
channel, the fleet account's AWS profile for its EC2 check, and the channel's ID, `channel` in
the workshop's entry in `admin.toml`:

```bash
AWS_PROFILE=<profile> uv run --with boto3 python workshop/admin/roster_sync.py \
    --roster roster.csv --zuliprc provisioner.zuliprc --out-dir workshop/admin/out \
    --tag workshop=<ws> --tag zab-scope=<SCOPE> --region <region> \
    --sync <channel ID> --changes sync-changes.txt --dry-run
```

The dry run lists every change, one line each (`+` a row added, `~` a name changed, `-` a row
removed, `=` one left alone, `x` one excluded), and writes that list to `--changes`. Then the
same command without `--dry-run` writes the roster, only if the changes it finds are still the
listed ones; otherwise it refuses, and writes nothing. Each subscriber without a row gets one,
with a slug made from their Zulip name; a row matched by no subscriber is removed only while
nothing is provisioned for it. `--remove SLUG`, `--restore SLUG` and `--rename SLUG NEW` take the
place of `--sync` and `--changes`, for one row each. On the server, pass the other workshops'
rosters (`--workshop-roster WS=ROSTER`, repeatable) and `--retired`, as for `roster_import.py`
(§5), so that no slug is shared between workshops. The module docstring has every rule.

## 5. Roster corrections: `roster_import.py` (ADMIN §3.1)

```bash
AWS_PROFILE=<profile> GH_TOKEN=… uv run --with boto3 python workshop/admin/roster_import.py \
    --roster roster.csv --input upload.csv --zuliprc provisioner.zuliprc --org <org> \
    --tag workshop=<ws> --tag zab-scope=<SCOPE> --region <region> --dry-run
```

The admin bot runs it for `!roster import`; it works the same by hand. It replaces the roster's
participant rows with the upload's input columns (`slug`, names, `email`, `repo`, `region`,
`notes`, `github`), keeps every column the scripts filled, and prints the diff. Empty `region`,
`repo` and `github` cells keep the recorded values. It refuses:

- duplicates, `zab-spare-N` slugs, and the reserved slugs of §4;
- a new row taking a slug another workshop on the server uses (`--workshop-roster WS=ROSTER`,
  repeatable) or a retired one (`--retired retired-slugs.txt`);
- a `region` other than the workshop's (`--region`);
- removing or renaming a row that has a VM (in the roster, or in EC2), a participant on Zulip
  (`zulip_user_id`, `zulip_invite = joined`, or a user with its address), an open invitation, a
  provisioned repository, or a recorded login with access or an invitation to it: remove such
  a row by hand after teardown;
- changing the region of a row whose VM exists;
- an e-mail change while an invitation to the old address is open that the provisioner's key
  lists, when the apply can't revoke it, or when another workshop's row has the address. The
  organizers' own invitations are not among them: the key sees only its own, and it sends none
  (#628). The organizers revoke theirs in Zulip (ORGANIZERS §1.5);
- any rebind: a new e-mail for a row on Zulip, or a new `github` whose recorded login has access
  to its repository or an open invitation to it (checked with `GH_TOKEN`).

Every check it cannot make refuses, including a Zulip listing in which someone hides their
address from the key. Without `--dry-run` it writes the roster atomically and keeps
`roster.csv.bak`.

## 6. Rollback

```bash
uv run python workshop/admin/push_config.py --roster roster.csv --uninstall
```

Runs `zab service uninstall` on every VM, with the environment of §2 step 4: it removes zab's
units, the bridge, the Codex daemon and the sentinel of `!vm` and `!restart` in DMs (#204). It
then removes the page-cache warm-up (§2.3). The bots remain but do nothing; CLI use is
unaffected. The VM's list of its software and the `sage` wrapper that a rollout gave a VM from
an image built before the wrapper (#482) stay, so Sage keeps working as the agents'
instructions describe it.

Should the wrapper itself break Sage on such a VM, this puts the image's link
back where a rollout replaced it (a newer image's own wrapper has no record, and is left alone);
fix the wrapper in the repository before the next rollout, which installs it again. As the
rollout does it: a fresh directory of `ubuntu`'s, readable by `agent`, the script run with `-I`,
and the directory removed:

```bash
IP=<the VM's address>
PIN='-o StrictHostKeyChecking=yes -o UserKnownHostsFile=workshop/admin/out/known_hosts'
SSH="ssh $PIN -o ForwardAgent=no -o ForwardX11=no -o ClearAllForwardings=yes"
d=$($SSH ubuntu@$IP 'd=$(mktemp -d /tmp/zab-sage.XXXXXX) && chmod 0755 "$d" && echo "$d"')
scp $PIN workshop/admin/sage_wrapper.py "ubuntu@$IP:$d/sage_wrapper.py"
$SSH ubuntu@$IP "cd / && chmod 0644 $d/sage_wrapper.py && sudo -u agent -H /usr/bin/python3 -I $d/sage_wrapper.py remove; rm -rf -- $d"
```

It prints `sage: restored /home/agent/.local/bin/sage as a link to
/home/agent/miniforge/envs/sage/bin/sage`, or why it left the file alone (`sage: nothing to
restore: …`, `sage: skipped: …`).

## 7. After the workshop

The admin bot's `!retire` ends the rows it names, one after the other, each through these steps
(ORGANIZERS §5.1); its separate verbs run them one at a time, for repairs (ORGANIZERS §5.8). By
hand, the same order, with `--only <slug>` at each step, and each step only once the row's last
one has succeeded: a row whose sync fails is not torn down.

1. **Start the VM, and wait until SSH answers:** `power.py --start --wait`
   ([`workshop/aws/README.md`](../aws/README.md), "Stopping and starting the fleet: power.py"). A
   VM that idle stop powered off can't be synced.
2. **Idle stop off:** `push_config.py --idle-stop 0` (§8), so that the VM doesn't stop midway.
3. **The waker off,** where the server's waker serves the fleet: `push_waker.py --config-dir …
   --remove --apply`, on the server (§9), so that nothing starts the VM again.
4. **Uninstall:** `push_config.py --uninstall` (§6): the agents stop, so no new work comes.
5. **Final sync** (§7.1).
6. **Export,** for a participant who asked for an archive (§7.2).
7. **Protection off,** for a kept VM at its month's end: `protect.py --off`, until EC2 shows it
   (`workshop/aws/README.md`).
8. **Teardown:** `teardown.py` (§7.3).
9. **The bots:** `zulip_provision.py --deactivate` (§7.3).

### 7.1 Final sync

```bash
uv run python workshop/admin/final_sync.py --roster roster.csv --dry-run
uv run python workshop/admin/final_sync.py --roster roster.csv
```

Before any VM is stopped for good or deleted, every participant's work goes to GitHub. For each
provisioned row, `final_sync_remote.py` runs on the VM as `agent`, read from stdin, and for the
checkout and each topic worktree it commits what isn't committed (unless a turn is running there;
never `.lake`, files over 50 MB or anything that looks like a credential, which are listed as
*left out*), pushes the branch and every other local branch, never forced, and writes
`TOPICS.md` on main: one row per Zulip topic and branch. The table ends with the path of a JSON
report in `out/`. A rerun is harmless (`clean`, `up to date`, `unchanged`): run it after the last
session, once turns have finished, since a worktree with a turn running is skipped and reported.
Session transcripts and files outside the repository don't go to GitHub: they stay on the VM, or
go into an export (§7.2). Each VM's sync runs under the idle-stop hold (§8). `final_sync.py`'s
module docstring has the details; `--strict`, the admin bot's mode, fails a row with a VM that it
would skip as not provisioned.

### 7.2 Exports

`export.sh`, run on a VM as `agent`, writes `~/export-<slug>.tar.gz`: `~/work` with `.git` and
any uncommitted changes, `~/.codex/sessions`, `~/.claude/projects` (the session transcripts) and
the agents' instructions. It **excludes every credential** (`~/.codex/auth.json`,
`~/.claude/.credentials.json`, `~/.config/zab/`) and the build trees (`.lake`, which holds a
whole Mathlib, `__pycache__`), and refuses to leave an archive that names a credential.
Participants have no SSH, so a deployer collects the archives, after the uninstall, so that no
turn is still writing, and before the teardown:

```bash
uv run python workshop/admin/collect_exports.py --roster roster.csv \
    --known-hosts workshop/admin/out/known_hosts --out-dir workshop/admin/out/exports --dry-run
```

then the same without `--dry-run`, or `--only SLUG`. For every provisioned row it runs:

1. `export.sh` on the VM as `agent`, read from stdin;
2. the archive fetched as `agent`, never as root (which would follow a link `agent` planted),
   into a 0600 file in a 0700 directory;
3. the check: the archive goes, read-only, to the sandboxed checker `archive_check.py`, in
   `zab-archive-check@.service` (ADMIN §1.2, `--checker-socket`). It re-packs a whole, canonical
   gzip tar, none of whose names goes through a link or is a credential's, into a new archive of
   regular files and directories only: symlinks listed in `SYMLINKS.txt`, names Windows can't
   take renamed and listed in `RENAMED.txt`. Only the new archive is kept, as
   `export-<slug>.tar.gz`; a refusal reaches the table with its reason. On a machine without the
   unit, every archive is refused;
4. the participant's own archive deleted.

What the stored archive is, and what is not claimed (a credential under another name, or inside
a nested archive), is stated once, in `archive_check.py`'s docstring. Step 5 stays by hand: give
each archive to its participant only, by DM on Zulip if it fits the upload limit, otherwise as a
private link, and delete the local copy once it is delivered. Keep `exports/` outside any synced
folder, like `out/`: the transcripts are the participant's private work. `collect_exports.py`'s
module docstring has the limits.

### 7.3 The bots, the VMs and the sign-ins

- **Sign-outs.** zab never handles agent credentials (CONTRIBUTING.md, rule 2), so no script
  signs a VM out of Codex or Claude: the participants do it themselves before the uninstall,
  with the CLIs' own `codex logout` and `claude auth logout` (an agent can run them for them).
  The teardown deletes the local tokens with the volume anyway, and the participants revoke the
  sessions themselves in their ChatGPT and Claude account settings afterwards.
- **The VMs:** `workshop/aws/teardown.py` terminates them, once per region, and releases their
  Elastic IPs; kept VMs are protected first (`protect.py`), and the teardown waits until EC2
  shows it (`workshop/aws/README.md`).
- **The bots:** `zulip_provision.py --roster roster.csv --zuliprc provisioner.zuliprc --deactivate`
  deactivates them, which invalidates their keys. Each bot is the one whose e-mail its
  `out/<slug>/<kind>.zuliprc` holds (`--out-dir`), and it must carry the row's name; a mismatch
  or a missing file is reported and left alone.
- **The sandboxes and their history stay with the participants.** Someone who goes on with zab on
  their own machine creates their own bots, `zab init`, and subscribes them to those channels.

## 8. Idle stop

```bash
uv run python workshop/admin/push_config.py --roster roster.csv --idle-stop 60 [--only <slug> …]
```

Every workshop runs idle stop and the waker on every VM, from launch day to its end, and the
kept VMs keep both for their month: a VM runs only while it's used, powers itself off when idle,
and the waker starts it again when its owner writes to one of their bots (ORGANIZERS §4.2;
§9). **The waker first, then idle stop**, so that no VM stops without a way to wake it.

`--idle-stop N` (at least 10 minutes) installs `/usr/local/lib/zab/idle_stop.py` and the root
timer `zab-idle-stop.timer`, and the user `zab-monitor` that `status.py` reads as, which idle stop
doesn't count as a login (#507). Without `--tag`/`--wheel` only that step runs, after the owner
check: the bridge isn't touched or restarted. With a release, it runs last. `--idle-stop 0`
removes it all again, and records that idle stop was turned off on purpose:
`/etc/zab/idle-stop.off`, which the next `--idle-stop N` removes. `--idle-stop-default` (§2.4)
leaves such a VM off. A VM turned off with v0.2.16 or older has no record, but it keeps idle
stop's log, `/var/log/zab-idle-stop.log`: `--idle-stop-default` reads a VM with the log and no
timer as turned off on purpose too.

Once a minute it checks, and keeps the VM up while any of these holds: the VM has been up for
less than the timeout; a turn is running, at any age; zab took a message in for its agents, or a
turn started or ended, within the timeout; the 5-minute load average is 0.5 or more; someone is
logged in, or was within the timeout; a `push_config.py` or `final_sync.py` run holds it. Every
rollout writes `/run/zab-idle-stop.hold`, with an expiry an hour ahead, before its first change,
and removes it at the end, so no tick between its SSH connections can stop the VM; `final_sync.py`
holds each VM the same way around its sync (#370). Anything it can't read keeps the VM up.
Otherwise it runs `systemctl poweroff`, and the instance stops (`launch.py` and `protect.py` set
`InstanceInitiatedShutdownBehavior=stop`): the disk, the Elastic IP and the agents' sign-ins
survive. `idle_stop.py`'s module docstring has every rule.

- `journalctl -u zab-idle-stop` has one verdict a minute, and `/var/log/zab-idle-stop.log` a line
  when the verdict changes, for example from "stay up: zab active 3 min ago" to "power off: idle
  for 15 min (…)".
- To try it without stopping anything: `sudo /usr/bin/python3 /usr/local/lib/zab/idle_stop.py
  --minutes 15 --dry-run` on the VM. Over SSH it always says "stay up: logged in: …", because you
  are; its other reasons are still worth reading.
- **The first boot's volume warm-up** (`aws_common.WARM_FIRST_BOOT`, #715) holds every VM up while
  it can still end: its marker `/var/lib/zab-warm.done` is missing, and `zab-warm-volume.service`,
  or cloud-init's last stage, which starts it, is running. A stop would cut it off for good. The
  verdict says "stay up: the first boot's volume warm-up is running". It holds no longer than 3
  hours from the boot (the warm-up takes about 15 minutes), so that a stage hung in `activating`
  can't hold the VM for good: past that, the verdict says "(ignoring the first boot's volume
  warm-up, unfinished after 3 h up)".

**The spares** (#715; ORGANIZERS §3.7) get their own idle stop, `idle_stop.py --spare --minutes N`,
from `spare_idle_stop.py` (`workshop/aws/README.md`), which the admin bot runs after `!launch …
spares=N` and after each `!start` of spares, with admin.toml's `spare_idle_stop` (10 by default).
By hand:

```bash
uv run --with boto3 python workshop/aws/spare_idle_stop.py --roster roster.csv --minutes 10 \
    --only zab-spare-1 --only zab-spare-2 --region <region> --tag workshop=<ws> \
    --tag zab-scope=<scope> --spares-file out/spares.json
```

A spare has no bridge yet, so no zab database, which `--spare` doesn't count as a reason to stay
up; every other rule above holds. Its verdicts go to `/var/log/zab-idle-stop-spare.log`, so that
the log that `--idle-stop-default` reads as "turned off on purpose" (§2.4) is never a spare's.
`push_config.py --adopt-spare` replaces the spare's timer with the row's: `--idle-stop N`
replaces it, `--idle-stop-default` replaces it where the row's waker is on and removes it
elsewhere, and a hand-over run with neither removes it (ORGANIZERS §7.2).

## 9. The waker: `push_waker.py`

The waker starts a participant's stopped VM when a message from them reaches one of their bots
([`workshop/waker/README.md`](../waker/README.md)). Opt a VM in before its idle stop (§8).
`push_waker.py` opts VMs in and out, a dry run unless `--apply`, in one of two modes. Never both
for one fleet: each bot takes one waker.

**On the admin server,** `--config-dir`, for a fleet the server made: the bot's `!waker on` and
`!waker off` (ORGANIZERS §4.2; ADMIN §3, action 5). By hand, as `zab-admin` on the server, with
the workshop's values from `admin.toml`:

```bash
sudo -u zab-admin /opt/zab-admin/current/.venv/bin/python -E -s \
  /opt/zab-admin/current/workshop/admin/push_waker.py --config-dir /var/lib/zab-admin/waker \
  --workshop <ws> --region <region> --tag workshop=<ws> --tag zab-scope=<SCOPE> \
  --roster /var/lib/zab-admin/ws/<ws>/roster.csv --keys /var/lib/zab-admin/ws/<ws>/out \
  --organizers-channel <alerts_channel> --only <slug>
# the dry run prints the workshop's entry as it will be written; then the same with --apply
```

- `--organizers-channel` takes the ID of the workshop's `#<ws>-fleet-alerts`, `alerts_channel` in
  `admin.toml`: the channel where a failed wake is posted.
- `--remove --only <slug>`, with `--workshop`, `--roster` and `--config-dir`, opts a VM out, with
  no AWS call.
- The waker knows a row's Zulip ID only from the last run that named the row: run it again for
  the rows that `zulip_provision.py` has bound since (ORGANIZERS §3.5).
- The waker restarts itself on the new config.
  [`workshop/adminserver/README.md`](../adminserver/README.md) has the rest: the lock that
  serializes the runs, and why they run as `zab-admin`, never root.

**In direct mode,** on a host of its own, for a fleet run without a server: the waker's own
README has its install and its runs of `push_waker.py`.

## See also

- [`docs/ORGANIZERS.md`](../../docs/ORGANIZERS.md): the same steps through the admin bot; its §7
  is the manual fallback, and its §7.2 a spare handed over by hand.
- [`docs/ADMIN.md`](../../docs/ADMIN.md): the design; its §3 maps each bot command to these
  scripts, and its §1.7 covers backups and recovery.
- [`docs/BOOTSTRAP.md`](../../docs/BOOTSTRAP.md): the admin server's setup, with the dedicated key
  (§2.1), the provisioner (§5.1), a server that may be compromised (§13.3) and the secrets
  bundle (§14).
- [`docs/NEW-WORKSHOP.md`](../../docs/NEW-WORKSHOP.md): a new workshop on the server.
- [`docs/quickstart.md`](../../docs/quickstart.md): the participants' side.
- [`workshop/adminserver/README.md`](../adminserver/README.md): deploying, and "Backups and
  recovery".
- [`workshop/aws/README.md`](../aws/README.md): the VMs: launch, power, protection, disks and
  teardown.
- [`workshop/waker/README.md`](../waker/README.md): the waker, in either mode.
- [`workshop/ami/README.md`](../ami/README.md): the image the VMs boot from.
