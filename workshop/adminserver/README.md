# The admin server's own files (docs/ADMIN.md §1, §4.3, §5)

`deploy.sh --bootstrap` installs these; nothing here touches a server or an account. One
server serves several workshops, each in one region with its own admin bot, plus an optional
global bot; the waker runs here too.

| File | Installed as |
|---|---|
| `sysusers.conf` | `/etc/sysusers.d/zab-admin.conf`: users `zab-admin`, `zab-starter`, `zab-console`, `zab-waker`, `zab-build` |
| `tmpfiles.conf` | `/etc/tmpfiles.d/zab-admin.conf`: the directories and modes of ADMIN §1.3, `secrets/bots/` for the workshop bots' keys, an empty `retired-slugs.txt` (0600, never truncated) for `roster_import.py --retired`, and `audit.jsonl`, append-only |
| `imds.nft`, `zab-admin-imds.service` | `/etc/zab-admin/imds.nft`, `/etc/systemd/system/` |
| `nftables-imds.nft`, `nftables-stop-imds.nft`, `nftables.service.d/zab-admin-imds.conf` | `/etc/zab-admin/`, `/etc/systemd/system/nftables.service.d/` |
| `zab-admin-bot.service`, `zab-admin-collector.service`, `zab-admin-config.service` and `.path`, `zab-admin-starter.service` and `.socket`, `zab-archive-check@.service` and `zab-archive-check.socket`, `zab-console.service`, `zab-waker.service` | `/etc/systemd/system/` |
| `Caddyfile` | `/etc/caddy/Caddyfile`, with `<CONSOLE_HOST>` set to the host of `console_url` |
| `caddy.service.d/zab-admin.conf` | `/etc/systemd/system/caddy.service.d/zab-admin.conf` |
| `admin.example.toml` | the model for `/etc/zab-admin/admin.toml` (root 0644); it loads with `zab.admin.config` |
| `render_policy.py` | not installed: a deployer runs it to make the instance role's policies |
| `ssh-rule.sh` | not installed: a deployer runs it on their laptop, right before an SSH step, so that the server's port 22 admits their address alone (docs/BOOTSTRAP.md §3.1); `--files-host`, the files host's |
| `upgrade.py` | not installed: the upgrade driver (#772), which a deployer runs on their machine from the signed copy of the release the server runs, with `python3 -I`; `--setup` once per machine. It reaches no server: it reviews the new release with that copy's `deploy.sh`, signs, verifies and pushes its tag, and makes its copy `~/zab-vN`, from which the server is upgraded (below; `!upgrade-server` once #569 adds it) |
| `deploy.sh`, `release-paths`, `privileged-paths` | not installed by hand: the deployer runs `deploy.sh` (below), which installs the paths `release-paths` lists, and whose review shows the paths of `privileged-paths` first |

## Processes

| Unit | User | Writes | Reads beyond its own | IMDS |
|---|---|---|---|---|
| `zab-admin-bot` | `zab-admin` | `/var/lib/zab-admin`, except `.ssh/` | `/etc/zab-admin/secrets/` (group), the starter's start log (group) | yes |
| `zab-admin-collector` | `zab-admin` | `console/` and its own `collector/` | the bot's keys, read-only | yes |
| `zab-admin-config` (#418), a oneshot started by its `.path` unit | root | `/etc/zab-admin` itself (a new workshop's entry in `admin.toml` and `admin.toml.bak`, by renames) and `secrets/bots/` (the bot's key), every other file there read-only and the other secrets hidden (#437); `/var/lib/zab-admin-config`: the request it deletes in `requests/` (zab-admin's 0700 directory, in root's), its outcome `result.json`, and `changes.jsonl` | the request | no: no network |
| `zab-admin-upgrade-plan` (#569), a oneshot started by its `.path` unit | root | `/var/lib/zab-admin-upgrade` only | of `secrets/`, the release key alone; `/var/lib/zab-admin`, the console's and Caddy's state, `/etc/ssh` and the password hashes are hidden | no: `IPAddressDeny` |
| `zab-admin-upgrade` (#569), a oneshot started by its `.path` unit | root | what `sudo bash deploy.sh --server` writes, but `/usr`, `/boot` and the home directories | what root reads | as root |
| `zab-admin-starter` | `zab-starter` | `/var/lib/zab-starter`, its start log in `log/` | nothing under `/var/lib/zab-admin` or `secrets/` | yes |
| `zab-archive-check@` (#374) | its own per instance (`DynamicUser`) | nothing | nothing under `/etc/zab-admin` or `/var/lib/zab-admin`; only the archive it is handed, read-only | no: no network, no socket (`socket()` and io_uring denied), the shared sockets under `/run` hidden |
| `zab-console` | `zab-console` | `console/tokens/` (deletes used tokens), `/var/lib/zab-console` | sees only `console/` | no: `IPAddressDeny=any` |
| `zab-waker` | `zab-waker` | `/var/lib/zab-waker` only, its status file in `status/`; it connects to the starter's socket | sees only `waker/` | no: the rule and `IPAddressDeny` |
| `caddy` | `caddy` | `/var/lib/caddy` | no zab file | no: the rule and `IPAddressDeny` |

Every unit but the upgrade's apply helper (below) has the hardening of
`workshop/files/zab-files.service` (`ProtectSystem=strict`,
`ProtectHome`, `PrivateTmp`, `NoNewPrivileges`, `UMask=0077`, an empty
`CapabilityBoundingSet`), checked by `tests/workshop/test_adminserver_units.py`. Two keep one
capability: `zab-admin-imds` (`CAP_NET_ADMIN`, to load the rule) and Caddy
(`CAP_NET_BIND_SERVICE`). The config helper keeps two: `CAP_DAC_OVERRIDE`, to read and delete
the request in `zab-admin`'s 0700 directory, and `CAP_CHOWN`, to give the key its group; the
upgrade's plan helper, `CAP_DAC_OVERRIDE` alone, for its request. The upgrade's apply helper
runs the release's own `deploy.sh`, as `sudo` over SSH does, so it keeps root's capabilities
and the units' `NoNewPrivileges`, `PrivateTmp`, `ProtectHome` and `UMask=0077`, with `/usr`
and `/boot` read-only (`ProtectSystem=true`; ADMIN §1.6). The units run
`/opt/zab-admin/current/.venv/bin/python -I -m zab.admin bot|collector|console|starter` and
`-m zab.waker run`, from the release tree and venv of ADMIN §1.6. The config helper and the two
upgrade helpers run `/usr/bin/python3 -I -B -S -c …`, the system interpreter the venv is built
from, never the venv's, so nothing of the venv runs as root. Each imports its module
(`zab.admin.confighelper`, `zab.admin.upgradehelper`) from the release's `src/` and the
standard library alone, with no site processing, so no `.pth` line runs (ADMIN §1.6, #437).

**The config helper** (ADMIN §2.9, #418) adds a `!new-workshop` entry. The bot writes
`/var/lib/zab-admin-config/requests/request.json` (the entry as JSON, and the new bot's
zuliprc; `requests/` is the bot's, inside the helper's root-owned state);
`zab-admin-config.path` starts the helper, which claims it by a rename (so the bot can withdraw
an unanswered one the same way), deletes it, checks everything (a new name; the whole new
`admin.toml` through the strict loader; nothing changed but the new entry; the key's place,
`secrets/bots/<name>.zuliprc`; the entry's break-glass keys), then writes the key
(`root:zab-admin` 0640, never over a file), a line of `changes.jsonl`, and `admin.toml`,
atomically, keeping `admin.toml.bak`. A refusal changes no file. Its outcome is in
`/var/lib/zab-admin-config/result.json` and the journal (`journalctl -u zab-admin-config`). It
restarts nothing: the bot restarts itself after its apply's result, and the starter on any
change of `admin.toml`. To undo an entry by hand: `sudo cp -p admin.toml.bak admin.toml`, remove
the key, and restart the bot.

**One service hosts every bot.** `zab-admin-bot.service` runs all the bots of `admin.toml`. They
share `state.db`, the apply lock and the audit log's hash chain, which needs one writer. Adding
a workshop is an edit of `admin.toml` and a restart. A template unit per bot would isolate
nothing: every bot runs as `zab-admin` and reaches the same provisioner key, GitHub token and
fleet SSH key. The cost: one crash stops every bot until systemd restarts it (5 s).

**IMDS.** `imds.nft` rejects packets to `169.254.169.254` and `fd00:ec2::254` from every user
but root, `zab-admin` and `zab-starter`, in its own table. Reject, not drop, so a refused client fails at once.
EC2 Instance Connect stops working, as intended: deployers SSH in with their own keys.
- `zab-admin-imds.service` loads it at boot, before the network, after the users exist and
  after `nftables.service`. Stopping it leaves the rule in place.
- Ubuntu's `nftables.service` flushes the whole ruleset when it starts, reloads or stops, each
  in a transaction of its own. Its drop-in replaces those three commands with `nft -f` of
  `nftables-imds.nft` (`/etc/nftables.conf`, then the rule) and `nftables-stop-imds.nft` (a
  flush, then the rule): one transaction each, so the ruleset is never without the rule, even
  for a moment, whether or not that unit is enabled. A broken `nftables.conf` is refused whole,
  rule kept. **Apply firewall changes with `systemctl reload nftables`**: a bare
  `nft -f /etc/nftables.conf` or `nft flush ruleset` removes the rule and nothing restores it.
- Caddy `Requires` the loader, so it stays down if the rule fails to load (an unknown user,
  for one). The console (`IPAddressDeny=any`), Caddy and the waker are also cut off from IMDS
  by their own IP filters.

**The waker holds no AWS power** (ADMIN §5).
The role could stop a fleet VM, rewrite its user data and start it: root on every fleet VM, which
is the fleet SSH key's power, kept from this process because it handles any Zulip user's
messages. Instead:
- The waker reads `/var/lib/zab-admin/waker/`: its `config.toml` and the opted-in bots'
  zuliprcs, `<ws>/<slug>/{codex,claude}.zuliprc`, all `zab-admin:zab-waker` 0640 (the
  directory is setgid; the writer sets each file's mode itself, as for the console's files,
  ADMIN §1.3). It refuses any other owner, group or mode, and a zuliprc outside that directory.
  `push_waker.py --config-dir` writes them (below); the waker exits when its config changes,
  and systemd starts it again with the new one. `tmpfiles.conf` creates a first config with
  no workshop, on which the waker idles, and the unit's `--wait-for-config` makes it wait,
  logging once, should that file be missing.
- Each workshop's waker is supervised on its own: one that fails (the starter doesn't know
  the workshop, after it left `admin.toml` or before the starter reloads it; or any other
  error) is logged once and retried with a backoff from 5 s to 10 minutes, while the other
  workshops' wakers run on.
- It asks `zab-admin-starter`, on `/run/zab-admin-starter/start.sock`, for its VMs' states
  (every 30 s, and before a start) and for starts: one JSON line per connection, a workshop
  and instance IDs, never message text (the protocol is in `src/zab/admin/starter.py`). Only
  root and the `zab-waker` group may connect, and the directory is root's. `connect()` needs
  no writable mount. In this mode the waker never imports boto3.
- It writes `/var/lib/zab-waker/status/waker.json` every 30 s for the collector: per
  workshop, each opted-in bot's VM, its last seen state and the last wake attempt. Its state
  directory is 0711 and `status/` is setgid `zab-admin` (`tmpfiles.conf`), so the file, 0640,
  is the collector's to read and nothing else of the waker's is.
- `zab-admin-starter` (socket-activated) holds the role but no key, and calls only
  `DescribeInstances` and `StartInstances`. A VM is a workshop's when it carries every scope
  tag of the workshop (`workshop=<name>` and `[global]`'s `zab-scope` and extra tags), a
  non-blank `participant` tag and no `zab-admin` tag (in any
  case), in an availability zone of the workshop's region; it answers states for those only,
  and starts one only when it is stopped, at most once per VM every 30 s (kept in
  `/var/lib/zab-starter`). It answers only root and `zab-waker` (`SO_PEERCRED`), and exits when
  `admin.toml` changes, to be started again by the next request. It runs as its own user,
  `zab-starter`, and can read nothing under `/var/lib/zab-admin` or `secrets/`: as `zab-admin`
  it could read the bot's keys through `/proc/<pid>/root`, whatever its own mounts hide.
- **The limit, stated.** A compromised waker can at most ask for starts of stopped fleet VMs,
  each at most once every 30 s, and read their states. That means any participant VM of any
  workshop in `admin.toml`, opted in or not: the starter can't read the opt-ins, which live
  under `/var/lib/zab-admin`. It could thus keep a whole fleet running against idle stop,
  which costs money but reaches no VM. There is no server-wide cap on starts: its worst case
  is legitimate spend. Instead the starter logs each start it makes, in
  `/var/lib/zab-starter/log/starts.jsonl` (`log/` setgid `zab-admin` 2750, each file 0640,
  `tmpfiles.conf`), and makes none it can't log; the bot's wake monitor alerts the global
  admins at once about a start the waker asked for of a VM its config doesn't opt in, and
  checks every other against the participant's own messages to their bots, alerting about
  any that none explains (ADMIN §1.2, §1.5; #359). The waker holds the same bot keys, so it
  can make a start look explained, or a check fail: ADMIN §1.5 says what the monitor doesn't stop.
  The bots refuse to start when `log/` or `/var/lib/zab-starter` has another owner or mode:
  `bot --check` names it.

Opting VMs in, as `zab-admin` (the bot's `!waker on`, ADMIN §3, action 5), with the workshop's
`name`, `region`, scope tags and `alerts_channel` (its `#<ws>-fleet-alerts`) from `admin.toml`:

```sh
sudo -u zab-admin /opt/zab-admin/current/.venv/bin/python -E -s \
  /opt/zab-admin/current/workshop/admin/push_waker.py --config-dir /var/lib/zab-admin/waker \
  --workshop harvard-2026 --region us-east-2 --tag workshop=harvard-2026 --tag zab-scope=<scope> \
  --roster /var/lib/zab-admin/ws/harvard-2026/roster.csv \
  --keys /var/lib/zab-admin/ws/harvard-2026/out --organizers-channel ALERTS --only SLUG
# Inspect the dry run, then repeat with --apply. --remove --only SLUG opts out (no AWS call).
```

It looks each VM up by the roster's IP within the workshop's scope, with the server's role,
refuses one without a `participant` tag or with a `zab-admin` tag, copies the two zuliprcs,
and keeps the other workshops' and earlier opt-ins. It prints only this workshop's entry of
the config it writes: the bot posts that dry run in the workshop's organizers channel, which
must not see another workshop's participants.

**One run at a time.** Workshops apply concurrently, and every run rewrites the one config, so
`--apply` holds `waker/.lock` from reading the config to its last deletion.
- The lock is 0600 and `zab-admin`'s own, a file the waker can't open. A lock on anything the
  waker can read (`waker/` itself, its files) would let a compromised waker hold every
  `!waker on`/`off`, since an `flock` needs only a read-only fd (#348).
- A run waits up to 60 s for another's lock, then is refused, changing nothing.
- **When that happens.** A run holds the lock through its lookups: one `DescribeInstances` per
  selected participant, so a large `!waker on` under EC2 throttling can hold it past 60 s. A
  concurrent `!waker on`/`off` of another workshop is then refused cleanly, with
  "another push_waker.py run has held … for 60 s". Run it again once the first run's result
  is in; nothing of it was applied.
- `--apply` runs as `zab-admin`, never root: root's files and lock would shut `zab-admin` out.

## The instance role's policies

```sh
python3 -I workshop/adminserver/render_policy.py --config admin.toml --out-dir policies/
```

The account owner creates the role `zab-admin` with `workshop/waker/trust-ec2.json` and attaches
it to the server through an instance profile. The policies are **managed policies**, one per
file, `zab-admin-1` … `zab-admin-N`, all attached to the role and no inline policy: a managed
policy holds 6,144 characters, a little under two regions' worth since the volume statements
(#553), while a role's inline policies share
10,240. First time, for each file: `aws iam create-policy --policy-name zab-admin-N
--policy-document file://policies/zab-admin-N.json`, then `aws iam attach-role-policy
--role-name zab-admin --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/zab-admin-N`. The policy
depends on `[global]` alone (#422): render again only after changing its scope, a
region or an instance type, never for a new workshop, and update each with
`aws iam create-policy-version … --set-as-default` (IAM keeps five versions; delete the oldest
first). When a render needs fewer files, the renderer removes the extra `zab-admin-N.json` and
says so: detach (`detach-role-policy`) and delete (`delete-policy`) those managed policies, or
their allows stay in force. Every file carries every deny, so a file attached alone grants
nothing the denies forbid. A role holds 10 managed policies by default; the renderer warns
above that, and AWS raises the quota to 20 on request. The output names no account;
`--out-dir` omitted prints a policy that fits one file.

In each of `[global] regions`, the role may launch instances of `[global] instance_types`
tagged with the fleet's scope (`zab-scope=<scope>` and the extra tags) and some `workshop`, from
images, key pairs and security groups in the scope (the region's, made once by the account
owner: ADMIN §5); tag at creation, with the scope and a `workshop` tag; set only `Name` and
`participant` on the fleet's instances, volumes and addresses; start, stop, terminate, modify
and read the attributes of its instances (an instance's attributes include its user data, so
never another workload's); manage its addresses; and describe the rest, in those regions. The
fleet is what carries the scope and a `workshop` tag: IAM tells no workshop from another, the
server's code does (ADMIN §5, A15). Denied outright, whatever else the role holds: every EC2
action on a resource tagged `zab-admin` or a request that tags one; any scope key (`workshop`,
`zab-scope`, the extra ones), in any case, in a `CreateTags` outside creation, so nothing is
ever tagged into the scope; any `DeleteTags`; any `RunInstances`
with an existing network interface (`network-interface/eni-*`), which would take another
service's address and security groups: a launch creates its own (#356). **VERIFY V4**
passed in the IAM policy simulator (DECISIONS, Oct 14), on the policy before that last deny;
A13 confirms the resource and condition-key assumptions live, and checks the existing-interface
deny itself (DECISIONS, Oct 16). The fleet-wide scope (#422) changes only the tags the
conditions name, and adds `Null` on `workshop`: A13 probes a launch without a `workshop` tag and
one in another scope, both refused beside an allowed control. **V10** (narrowing
`ModifyInstanceAttribute`) is open.

## Deploying (docs/ADMIN.md §1.6)

`deploy.sh` runs on the deployer's laptop: bash 4.4 or newer, git 2.34 or newer, `ssh-keygen`,
`uv` for `--wheel`, and an SSH login to the server with sudo. Only ever run a copy you have
reviewed: never one from a checkout of `main`. Review with the copy of the last release you
signed; deploy with the copy of the tag you deploy, whose server half is the one it sends.
The production bootstrap, every step from the AWS account to the first checks, is
`docs/BOOTSTRAP.md`.

```sh
git -C ~/zab fetch --tags
git -C ~/zab worktree add --detach ~/zab-v0.2.9 v0.2.9     # the last release you signed
~/zab-v0.2.9/workshop/adminserver/deploy.sh --etc ~/zab-admin/etc --since v0.2.9 --review origin/main
# what the review prints last, to paste (gpg.format=ssh, your dedicated key):
git -C ~/zab-v0.2.9 tag -s v0.3.0 -m "zab v0.3.0" <the commit you reviewed, in full>
git -C ~/zab-v0.2.9 tag -v v0.3.0
git -C ~/zab-v0.2.9 push origin v0.3.0

git -C ~/zab worktree add --detach ~/zab-v0.3.0 v0.3.0
D=~/zab-v0.3.0/workshop/adminserver/deploy.sh
$D --host <host> --etc ~/zab-admin/etc v0.3.0          # an upgrade
$D --host <host> --etc ~/zab-admin/etc --wheel v0.3.0  # a fleet wheel
$D --host <host> --etc ~/zab-admin/etc --expect-diff-sha256 <the review's SHA-256> --with-wheel v0.3.0
                                                       # both, with one confirmation (#772)
```

The bootstrap is `$D --host … --etc … --bootstrap --secrets ~/zab-admin/secrets vX.Y.Z`. The
first release has no earlier signed copy to review with: read its `deploy.sh` before you run
`--review` with it (without `--since`, the diff is every release path, whole).

**Deploying from the public repository.** Its release tags are signed with the maintainer's
key (`SECURITY.md`, *Verifying a release*), and `deploy.sh` runs only a tag signed by a key of
your `allowed_signers`. So, unless you take the maintainer's review of each release as yours and
add that key's line there, you review the release's commit and sign a tag of your own, of the
same name, on it, in your clone alone, and never push it. `docs/BOOTSTRAP.md` §9 has the
commands.

- **`--etc DIR`** holds your copies of the server's `admin.toml`, `allowed_signers` (the
  signers' SSH keys, `<email> namespaces="git" ssh-ed25519 …`), `backup_recipients` and
  `break-glass/*.pub`. The bootstrap installs them; every later deploy shows how the server's
  differ from yours, since `!new-workshop` may have added a workshop there.
- **`--secrets DIR`** (bootstrap only): `provisioner.zuliprc`, `github-token`, and each bot's
  zuliprc at its path under `/etc/zab-admin/secrets/` as `admin.toml` names it, for example
  `bots/harvard-2026.zuliprc`. They are installed root:zab-admin 0640. A later deploy installs
  none: one secret is replaced on a running server by hand, with the same owner and mode
  (`docs/BOOTSTRAP.md` §16).
- **The review.** Before you sign, `--review` writes the diff of the release paths since the
  last signed tag, in full, with whole any path that `release-paths` gained (and that is not
  under a path it held), and prints its SHA-256. The deploy writes the same diff, prints the
  same SHA-256 for the same base, and records it. What it pages, and writes beside the diff as
  `….review`, is made from it. Its parts:
  - one line per file: `P` if the base's `privileged-paths` or the commit's covers it; `A`
    (shown whole), `M`, `D` or `T`; lines added and removed; text or binary;
  - the privileged files' diff, with `uv.lock` and `build-constraints.txt` as data, never as
    their hashes (`python3 -I`): each changed package with its versions and source and every
    other field that changed (dependencies, markers, groups, the lock's own), and each changed
    pin, flagged on a file or hash changed without its version or a source other than PyPI
    (D32; the raw diff stays in the `.diff` file). Where a summary can't be made (no `python3`
    with `tomllib`, a file it can't read), the file's raw diff is shown in its place;
  - whole, once, each file privileged at the commit but not at the base (`uv.lock` and
    `build-constraints.txt` as every package and pin);
  - the rest's diff.

  Then it names the privileged files that changed and the newly privileged ones. **Read every
  line of those** (DECISIONS D31, D32: what your signature certifies). It refuses a binary file (a
  NUL in its first 8000 bytes) outside the privileged paths, which it could only list, and any
  `__pycache__/`, `*.pyc`, `*.so`, `*.pth` or file name outside `[A-Za-z0-9._@+-]` in a release
  path. A deleted binary is listed, not shown. The pager is `less` in secure mode, with no
  `LESSOPEN`.
  What a terminal would hide is shown as `^X`, `<U+XXXX>` or `<XX>`: C0 and C1 controls, format
  characters (bidirectional overrides among them), every default-ignorable code point (zero
  widths, Hangul fillers such as U+3164, which Python accepts in identifiers, variation
  selectors, tag characters), private use, any space but the ASCII one and tab, and bytes that
  are not UTF-8. The `.diff` file holds the raw diff, in git's order, which the SHA-256 covers.
  Last, on stderr and outside what the SHA-256 covers, it prints the commands to sign, verify
  and push the commit, which work pasted in any directory (#468): `Sign it once reviewed, then
  verify and push it:`, then `git -C <the clone it read> tag -s vX.Y.Z -m "zab vX.Y.Z" <the
  commit>`, `… tag -v vX.Y.Z` and `… push origin vX.Y.Z`. The clone is an absolute path, and
  `vX.Y.Z` the version of the commit's own `pyproject.toml`, which the deploy requires. Where
  that file has no single `version = "X.Y.Z"` line, or the clone has that tag already, it prints
  `No tag command:` and the reason instead.
- **The checks,** on the laptop and again on the server with its own `allowed_signers` and
  `admin.toml`: an annotated tag signed with an SSH key of `allowed_signers`; the tag object's
  own name is the one requested; `release = "final"` takes `vX.Y.Z` only, `"rc"` also
  `vX.Y.ZrcN`; no release older than the running one without `--downgrade`. A branch, a
  lightweight tag and an unsigned one are refused, and so is a tag whose `pyproject.toml` names
  another version. You then type the version to confirm.
- **Two options of a deploy** (the bootstrap or an upgrade; #772), for a deploy of the release
  you have just reviewed:
  - **`--expect-diff-sha256 SHA`,** the SHA-256 the review printed: the deploy refuses, before
    its prompt, a diff with another one (`this deploy's diff has SHA-256 …, not …, the one you
    reviewed`). Its diff runs from its base, `--since`'s when given, otherwise the release the
    server runs: so a review made from another base is refused. Its review is written beside the
    diff, as always, but not paged again, and the diff is compared before the wheel is built.
    An empty value is refused, as for every option that takes one.
  - **`--with-wheel`** builds the release's fleet wheel before the prompt, so that a build that
    fails stops the deploy before anything changes, and places it after the deploy, as
    `--wheel` does: one confirmation, `Type vX.Y.Z to deploy it to <host>, which runs …, and
    place its wheel for !rollout:`. The wheel goes from a payload of its own, never with the
    bootstrap's `etc/` or secrets. A run that ends before the wheel is placed says why, and
    prints the `--wheel` command to paste, with this run's paths: the server did not run the
    release when the run ended (the same deploy, run again, places both, and the command is for
    a rerun that says the server runs it already), or it is current but its deploy did not
    finish, or it is deployed and only the wheel's placement failed, or the server did not say
    what it runs. After an interrupt or a dropped connection, the server half may still be
    running: a rerun refuses while it does (`another deploy is running`).
- **`--base-signers FILE`, after a deployer key is replaced** (#587; `docs/BOOTSTRAP.md` §17):
  FILE holds the retired keys' `allowed_signers` lines. The base, the review's `--since` or the
  release the server runs, may be signed by one of them or by a key of `--etc`'s
  `allowed_signers`; the release deployed only by the latter, on the laptop as on the server. A
  retired key may have leaked, and a base it signed could hide code from the review, which shows
  only what changed since the base, and so from what you sign. So the base must be the release
  the server runs (no other `--since`), at the commit its own record names
  (`/opt/zab-admin/deploys.jsonl`): the review checks it before it shows anything, and so takes
  `--host` too, and the deploy again, which also requires `--expect-diff-sha256`, the diff you
  reviewed. Without `--base-signers`, a review still takes no host. A bootstrap and `--wheel`
  refuse it: they have no base.
- **The install,** by root through sudo. Root first copies the payload into its own 0700
  directory, refusing anything but plain files, and removes the secrets from the SSH user's
  staging area. Its git repository reads no configuration of root's, and after the fetch
  `git fsck --strict` (with `..` entries an error) checks every object the bundle brought:
  `fetch.fsckObjects` doesn't apply to a bundle in git 2.43. An `info/attributes` there turns
  off every attribute `git archive` would apply, so a `.gitattributes` in the tree changes
  nothing. The release paths at the tag
  go into `/opt/zab-admin/releases/vX.Y.Z/`, owned by `zab-build` while it builds there, and
  root checks every file against its blob and mode before anything runs there. `zab-build`
  installs the `admin` group's wheels from `uv.lock` with `uv pip install --require-hashes
  --no-deps --no-build` (never `uv sync`: VERIFY V9), writes the `.pth` that names this
  release's `src/`, and runs the self-check: the venv imports the release's own code, and the
  release's `python -m zab.admin selfcheck`, always, loads `admin.toml` with the strict loader,
  imports what every unit needs (the starter's boto3; the bot's Zulip client and httpx), and runs
  the `--help` of each script the bot runs. Then `python -m zab.waker selfcheck` imports every
  module of the waker's closure, the ones it imports lazily included (#348), and
  `archive_check.py selfcheck` re-packs a small archive in memory, as its unit runs it (#374). It checks no owner or mode: `zab-build` can't read the
  secrets, which a bootstrap hasn't installed yet; the bot checks them at its start, and
  `sudo -u zab-admin /opt/zab-admin/current/.venv/bin/python -I -m zab.admin bot --check` runs those checks alone. Each step runs in a session of its
  own, without the terminal or the deploy lock, with `no_new_privs`, and no `zab-build` process
  is left afterwards; `zab-build` is in `/etc/cron.deny` and `/etc/at.deny`, so it can
  schedule nothing to start later. Root then takes the tree back, checks every file again, and
  installs every file of the table above from the new release (the Caddyfile with the host of
  `console_url`, validated first). Only then does it rename a new `current` symlink over the
  old one, write the change as a line of `/opt/zab-admin/deploys.jsonl`, and restart the
  units, which reloads the IMDS rule. A failure before the rename (a failed self-check among
  them) leaves `current` as it was and removes the directory, so the same command can run again. A unit that fails to start after it is an operational fault: the release is
  installed and recorded, and deploy.sh names the units and the `sudo systemctl restart …` to run
  once the cause is fixed.
- **The bootstrap** also installs the packages and a pinned `uv`, the users, directories and
  IMDS rule before the build (the deploy refuses to run while `zab-admin-imds.service` is
  inactive), the secrets, and the fleet key. It imports that key as each workshop's key pair,
  tagged with its scope (`python -m zab.admin.config key-pairs`: `workshop=<name>`, then
  `[global]`'s `zab-scope` and extra tags). All of it happens before `current` exists, and each
  step can run twice: after a failure (a missing secret, a refused import), fix the cause and
  run the same `--bootstrap` again. To import the key for a workshop added later, on the server,
  with every scope tag that `key-pairs` prints for it:

  ```sh
  sudo -u zab-admin /opt/zab-admin/current/.venv/bin/python -E -s \
    /opt/zab-admin/current/workshop/aws/import_key_pair.py --region REGION \
    --key-name zab-admin-WS --public-key /var/lib/zab-admin/.ssh/id_ed25519.pub \
    --tag workshop=WS --tag zab-scope=SCOPE
  ```
- **The units it enables and restarts**, at every deploy and in this order, after the release
  is current: the IMDS rule, Caddy, then `SERVICES` in `deploy.sh`.
  - `zab-admin-starter.socket` comes first. It is restarted and its service stopped, so that the
    next request starts the new release's starter.
  - `zab-archive-check.socket` (#374) next. Its service runs an instance per connection, which
    ends with it: nothing to stop, and the next connection runs the new release.
  - `zab-admin-config.path` (#418) next. Its oneshot runs only on a request, from the release
    `current` points to then.
  - `zab-admin-bot.service`, which hosts the runner, comes next.
  - `zab-admin-collector.service` and `zab-console.service` (#489), the console's, after it.
  - `zab-waker.service` comes last, after the bot.

  The new release's unit files are installed, with a `daemon-reload`, only after the switch.
  A deploy that fails before it, at the self-check or later (the Caddyfile, a secret), leaves
  the running release's units in place and loaded, so a unit that restarts on its own (the
  waker at every config change, the socket-activated starter) still runs the code its unit was
  written for (#348). Units that fail to install after the switch are an operational fault, like
  a unit that fails to start: `deploy.sh` names the commands that install them and restart, since
  a rerun of the running version is refused.
- **The bot's stop** is `KillMode=mixed`: SIGTERM goes to the bot alone, which stops each running
  script's process group within 5 s and records the apply as interrupted; SIGKILL still reaches
  whatever is left in the unit (`TimeoutStopSec=60`). So **a deploy interrupts any apply under
  way**: the plan ends INTERRUPTED and is not resumed. Deploy between applies (`!plans` lists the
  pending ones; an apply's progress message shows it running), and request again after.
- **The bot** refuses to start until its checks pass: the owners and modes of `admin.toml`, the
  keys and the break-glass files, `backup_recipients` (root's, and every line a key `age` can
  encrypt to), and each bot's `GET /users/me`. It starts at the bootstrap,
  which installs the secrets. Once they are in, run the same checks alone on the server, and fix
  whatever they name before anything else:

  ```sh
  sudo -u zab-admin /opt/zab-admin/current/.venv/bin/python -I -m zab.admin bot --check
  ```
- **Order.** `deploy.sh` creates the users and installs `imds.nft` before the
  `nftables.service` drop-in, which includes `imds.nft`: a drop-in ahead of them would keep the
  host's own firewall rules from loading. To uninstall, remove the drop-in
  (`/etc/systemd/system/nftables.service.d/zab-admin-imds.conf`) first, then `systemctl
  daemon-reload`, then the rest. Check the drop-in again when the Ubuntu release changes.
- **Fleet wheels** (`--wheel`) are built on your laptop from the verified tag with hatchling
  pinned by hash (`uv build --build-constraints build-constraints.txt --require-hashes`), from
  `pyproject.toml`, `build-constraints.txt`, `src/zab` and the readme, license and `.gitignore`
  alone (the same wheel, byte for byte, as from the whole tree). A `hatch.toml`, or a
  `pyproject.toml` that configures the build beyond hatchling's backend and the wheel's
  packages (a hook, `backend-path`, a dynamic field, `[tool.uv]`), is refused, and `uv build`
  runs with `--no-sources`: no code of the tree runs on the laptop (#436; `python3` 3.12 or
  newer checks it). The wheel is
  placed in `/var/lib/zab-admin/releases/wheels/` for `!rollout`, zab-admin 0640: once root has
  checked its SHA-256, `zab-admin` writes and renames it, since root never works by name in a
  directory another user owns. A version's wheel is never replaced. deploy.sh notes when your
  uv is not 0.13.7, the version VERIFY V9 was checked with.
- **uv on the server** is reinstalled whenever its pinned version or tarball hash changes.
- **The self-check** (the server half must be the tag's own `deploy.sh`, byte for byte)
  protects against running the wrong copy, not against a compromised deployer account: by then
  bash is already reading that user-writable file.
- **`release-paths` must list `workshop/adminserver`** (or `workshop`): the script root runs,
  the units and rules it installs, and the list itself are always in the reviewed diff. It
  names only what the server runs or installs (ADMIN §1.6), which a deploy test checks both
  ways. **`privileged-paths`** must list `workshop/adminserver` too, and only release paths:
  whatever is added there is privileged at once.

The tests (`tests/workshop/test_adminserver_deploy.py`) run both halves offline: tags signed with
throwaway keys, a local package index, a fake `ssh` set by `ZAB_DEPLOY_SSH`, and
`ZAB_DEPLOY_PREFIX`, which roots every server path in a temporary directory and only logs the
steps that need root (apt, systemd, chown). The server half refuses that variable as root. Under
it, `ZAB_DEPLOY_SYSTEMCTL` may name a stub for the restarts, and `ZAB_DEPLOY_IMDS` one that
says whether the IMDS rule is loaded (it fails when not), which every build checks first. On the server the payload goes to
`/tmp/zab-deploy.XXXXXXXXXX` (under the prefix in the tests), and the laptop sends `rm` only for
a name of exactly that form.

## Backups and recovery (docs/ADMIN.md §1.7)

After every apply that changes anything under `ws/` (every workshop's directory, retired ones
too), the plan's bot DMs each deployer `backup-<time>.tar.gz.age`, with the audit log's head it
holds (`audit #SEQ · chain HEX`) and what it left out. Never quote or forward that DM into a
channel: its readers could then fetch the file. Old backups are never deleted, so a key removed
from `backup_recipients`, or suspected, means rotating the participant bot keys (ADMIN §1.7). A failed backup is said in the plan's topic. `backup_recipients`
holds one key per line: the `ssh-ed25519` public key of a plain key file on your laptop (a FIDO
`sk-` key, or one only an agent holds, can't decrypt), or an X25519 key from `age-keygen`;
blank lines and `#` comments are skipped. Decrypt the first backup after the bootstrap once, to
know that your key works.

**Decrypt it,** on your laptop, with `age` installed (`apt install age`, `brew install age`):

```sh
umask 077                                                  # the plaintext holds bot keys
age -d -i ~/.ssh/KEY -o backup.tar.gz backup-<time>.tar.gz.age
mkdir ~/zab-admin && tar -xzf backup.tar.gz -C ~/zab-admin  # to read it, or for the manual path
uv run python -m zab.admin audit verify ~/zab-admin/audit.jsonl --head SEQ:HEX   # in a checkout
```

`KEY` is the private half of your line in `backup_recipients`; `SEQ:HEX` is the head the DM
names. Each workshop's roster is `ws/<name>/roster.csv`, and its `out/` is `ws/<name>/out/`:
those are the `--roster` and `--out-dir` of the scripts on the manual path (`workshop/admin/README.md`). Delete
the plaintext when you are done.

**Restore onto a new server,** once `deploy.sh --bootstrap` has installed it, with its own
fleet key and the workshops' bot keys regenerated by the provisioner (`--secrets`):

```sh
scp backup.tar.gz HOST:                                    # 0600, as umask 077 made it
ssh HOST                                                   # then, on the server:
sudo systemctl stop zab-waker.service zab-admin-bot.service
sudo chattr -a /var/lib/zab-admin/audit.jsonl              # the restored log replaces it
sudo tar -xzpf ~/backup.tar.gz -C /var/lib/zab-admin       # owners by name, and modes
sudo chattr +a /var/lib/zab-admin/audit.jsonl
rm ~/backup.tar.gz
sudo -u zab-admin /opt/zab-admin/current/.venv/bin/python -I -m zab.admin audit verify \
  /var/lib/zab-admin/audit.jsonl --head SEQ:HEX            # must print ok
sudo systemctl start zab-admin-bot.service zab-waker.service
```

The paths in the backup are those of the state directory, so the roster, `out/`,
`retired-slugs.txt` and the audit log land where they were. At its start the bot copies this
server's own `deploys.jsonl` lines into the restored chain, by digest (#344), and hands out
plan IDs above the restored log's. Then give the VMs the new fleet key over a break-glass key
(`workshop/admin/join_server.py`, ADMIN §1.7), check with `!status all`, and run `!waker on` again for the VMs that
had it: the waker's opt-ins are not in the backup. `tests/admin/test_admin_backup.py` follows
these steps offline, as far as `chattr` and `systemctl`.
