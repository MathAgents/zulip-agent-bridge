# Bootstrapping an admin server

For the deployer, who runs every step · Sources: [ADMIN](ADMIN.md) §1, §2.1, §2.9 and §5; [`workshop/adminserver/README.md`](../workshop/adminserver/README.md) ("the adminserver README" below) and `workshop/adminserver/deploy.sh`; the admin end-to-end harness, which bootstraps a test server for real (`tests/e2e/admin/harness/setup.py`, `aws_rig.py`, `admin_toml.py`, `zulip_rig.py`); [ORGANIZERS](ORGANIZERS.md) §1

This is the one-time bootstrap of a zab admin server (ADMIN §1), with the current release, in the AWS account that runs the workshops' fleets. **It sets up the server and one first workshop:**

- **The server** and its global admin bot, `Workshop admin`, in `#zab-admins`.
- **The first workshop, `<WS>`:** its admin bot, its two channels and its entry in `admin.toml`, made by hand here. The server's configuration holds at least one workshop, and `!new-workshop` copies a new workshop's settings from an existing one (`like=`), so the first can't come from it.

**Every later workshop** comes from a global admin's `!new-workshop` on `Workshop admin`, in `#zab-admins` (ADMIN §2.9; §7.3 below), then the organizers' runbook ([`docs/ORGANIZERS.md`](ORGANIZERS.md)), entirely on Zulip. `!new-workshop` creates the workshop's bot, channels, key pair and entry itself, so make no bot or channel for a later workshop while you follow this runbook (§5.3, §5.4). Its AWS side is its region's (§6.1), set up once per region, never per workshop; a region added after the bootstrap follows §6.5.

**Who runs what.** You, a deployer (ADMIN §2.1), run every step from your own machine, "the laptop" below:
- the AWS CLI, with a profile of the account's owner, which may create the server, its role and policies, and each region's fleet resources (ADMIN §1.1, §5);
- the Zulip web UI, as an owner of the Zulip organization.

No coding agent runs any of it, and none of it runs on a machine where coding agents run: such a machine holds no credential of the deployment and has no network path to the server (§3.1 says why).

**Order.** [Before you start](#before-you-start) comes first: §6.1 needs the workshop image, and §7 the GitHub organization and the files host's address. Steps 1–8 then need only those and your choices. Step 9 needs the release you deploy, a final tag `vX.Y.Z` that you review and sign. Steps 10–12 follow it.

**Conventions.**
- `<…>` is a value you choose or read off a previous step. In the shell blocks, most sit in a variable assignment at the top of their step (`NAME=<name>`), where one left in by mistake is a syntax error, never a wrong value.
- The commands use bash. §0's variables (`$P`, `$RS`, `$HOST`, `$SCOPE`, `$WS`, `$WR`) are used throughout: set them again, with §0's block, in every new terminal. Variables set in one step (`$SG`, `$I`, `$ALLOC`) are reused in the next. If you lose the shell, §3.7 finds every ID again from its tags.
- **A command that asks for something runs on a line of its own:** a passphrase (`ssh-keygen`, `ssh-add`, `age`), ssh's host-key question, the token's `read` (§8.1), or `deploy.sh`'s pager and its `Type vX.Y.Z to …` (§11). It reads the terminal, so a paste that goes on past it answers it with the paste's next line, or loses that line. Paste such a command alone, answer it, then paste the rest. The steps that prompt say so.
- **The laptop** is the machine you run this from, which holds `~/zab-admin` and the dedicated key (§2.1): a desktop serves as well, and a second machine of yours is set up with §15. From §2.1 on, run every step in §2.1's terminal, whose agent holds that key alone.

## Contents

- [Before you start](#before-you-start)
- [0. The values you choose](#0-the-values-you-choose)
- [1. Prerequisites on the laptop](#1-prerequisites-on-the-laptop)
- [2. SSH tag signing](#2-ssh-tag-signing)
- [3. AWS: the server, in `$RS`](#3-aws-the-server-in-rs)
- [4. DNS](#4-dns)
- [5. Zulip, in the web UI](#5-zulip-in-the-web-ui)
- [6. AWS: each region, once](#6-aws-each-region-once)
- [7. `admin.toml`](#7-admintoml)
- [8. The secrets and the trust files](#8-the-secrets-and-the-trust-files)
- [9. The release: review and sign it](#9-the-release-review-and-sign-it)
- [10. The role's policies, rendered on the laptop](#10-the-roles-policies-rendered-on-the-laptop)
- [11. `deploy.sh --bootstrap`](#11-deploysh---bootstrap)
- [12. First checks](#12-first-checks)
- [13. Rollback, and what never to do](#13-rollback-and-what-never-to-do)
- [14. Secrets between uses](#14-secrets-between-uses)
- [15. A second deployer machine](#15-a-second-deployer-machine)
- [16. Replace a secret on a running server](#16-replace-a-secret-on-a-running-server)
- [17. A lost or suspected key](#17-a-lost-or-suspected-key)

## Before you start

You need these, in this order: each is a prerequisite of a step below. The files host is the one that fits inside this runbook, after §2.1 and before §7. The image and the quotas take longest, so start them early.

1. **A Zulip organization** of which you are an owner: the workshops' Zulip. §5 sets up its accounts, bots, channels and settings.
2. **A GitHub organization you own,** for the participants' repositories: `<GITHUB_ORG>` (§0), `github_org` in §7.2, the resource owner of the server's token (§8.1), and the one organization the files host serves (item 6). The admin bot passes `github_org` to every rollout as the bridges' `refserve_owner`, so that they publish their turns' snapshots for its repositories (#693).

   **Its settings** (#714), which an owner of the organization sets once, under its *Settings → Member privileges*. Participants are their own repository's admins (`provision_repos.py`, `workshop/admin/README.md` §1b):
   - **Repository deletion and transfer:** allowed for members with admin permissions. Whether that covers the participants, who are outside collaborators with admin, not members, rests on GitHub's documentation alone, a VERIFY item (DECISIONS, the verification log): a participant's admin should be able to delete their repository, or transfer it to their own account.
   - **Repository visibility change:** allowed. A participant who can transfer their repository can make it public anyway.
   - **Outside collaborators:** nothing to set. Anyone with admin access to a repository can invite them, and only GitHub Enterprise Cloud can restrict that to owners (GitHub's *Setting permissions for adding outside collaborators*).
   - **Paid seats:** on a paid plan, such as Team, each outside collaborator on a private repository takes a paid seat, counted once however many repositories they reach (GitHub's *About per-user pricing*). The participants are outside collaborators themselves, so each takes one, and so does each collaborator they add. On the Free plan, none does.
3. **An AWS account** for the server and every workshop's fleet, with a profile of its owner on the laptop (`P`, §1). Read its quotas now (§6.4): an increase can take days.
4. **A domain** whose DNS you control, for the server's name (`HOST`, §4) and the files host's (`<FILE_SERVER_URL>`).
5. **The workshop image,** built once in that account with [`workshop/ami/README.md`](../workshop/ami/README.md), before §6.1, which copies it into each region. The build takes about 70 minutes, and its smoke test some more. It can run while you do §1–§5.
6. **The files host,** set up with [`workshop/files/README.md`](../workshop/files/README.md) after §2.1, whose key and agent its SSH steps use, with §0's `HOMEIP`, and before §7, whose `admin.toml` names its address as `refserve_url`, an `https://` URL that the loader requires. It serves one GitHub organization, `<GITHUB_ORG>`: its `ZAB_FILES_ORG`, which its §3 writes, and the organization of its token and of its selftest's scratch repository. Every workshop whose `refserve_url` is this host has that organization as its `github_org`; a workshop of another organization needs a files host of its own.
7. **The admin server:** this runbook, §0 to §12.

After it, the first workshop's organizers take over with [`docs/ORGANIZERS.md`](ORGANIZERS.md). Every later workshop starts with `!new-workshop` ([`docs/NEW-WORKSHOP.md`](NEW-WORKSHOP.md)).

## 0. The values you choose

| Value | What | Suggested |
|---|---|---|
| `P` | the AWS CLI profile of the account's owner (§1) | |
| `RS` | the server's region | the region of most of your workshops |
| `HOST` | the server's DNS name: the console's address (ADMIN §4), and ssh's and `deploy.sh`'s host | `admin.` and your domain |
| `SCOPE` | the fleet's scope: every resource of every workshop carries `zab-scope=<SCOPE>` (ADMIN §5) | a short name of your organization, `a-z` and `-` |
| `WS` | the first workshop's name: `a-z`, `0-9` and `-`, at most 47 characters, never reused (ADMIN §2.9). `<WS>` in tables and the template, `$WS` in commands | |
| `WR` | the first workshop's region, set up in §6.1 | |
| `<GITHUB_ORG>` | the GitHub organization of the workshops' repositories, which you own ([Before you start](#before-you-start), item 2): `github_org` in §7.2, the resource owner of the server's token (§8.1), and the files host's `ZAB_FILES_ORG` (its `<org>`) | |
| `<FILE_SERVER_URL>` | the files host's address, `https://` and its name ([`workshop/files/README.md`](../workshop/files/README.md)): `refserve_url` in §7.2 | `https://files.` and your domain |
| `<name>` | the server's own tag, `zab-admin=<name>`, on its instance, volume, Elastic IP and security group (ADMIN §1.1) | `prod` |
| `<key-pair>` | the EC2 key pair, in `$RS`, of the dedicated key (§2.1) | `zab-deployer` |
| `MYCIDR` | the one address the server admits SSH from: your laptop's public IPv4, as `/32`. Never an agent machine's: on a network you share with one, the SSH steps run from another network (§3.1). It changes every few hours, so §3.1's refresh sets it again before each SSH step | set on the laptop: `MYCIDR="$(curl -fsS https://checkip.amazonaws.com)/32"` |
| `HOMEIP` | the public IPv4, as `/32`, of a network you share with a machine where coding agents run, such as your home's: §3.1's refresh refuses it, and refuses to run without `HOMEIP` | set on that network, before you leave it: `HOMEIP="$(curl -fsS https://checkip.amazonaws.com)/32"`; if you share no network with one, `HOMEIP=none`. §3.1's script keeps it in a file instead, `~/.config/zab/homeip`, which its `--record-home` writes on that network, again whenever home's address may have changed |
| `<acct>` | the account's ID, only to compare with what `aws sts get-caller-identity` prints (§1); the other commands here leave it out of what they print when they succeed | |
| `<email>` | the principal of your line in `allowed_signers` | your `git config user.email` |
| `V` | the release you deploy: a final `vX.Y.Z`, whose commit you review and sign (§9) | the newest release |
| Global admins | you and the lead organizers, by Zulip user ID (§5.5): every admin bot obeys them (ADMIN §2.1) | |
| Zulip IDs | users, bots and channels, read back in §5.6 | |

Set the first six in every terminal you use for this runbook:

```sh
P=<aws-profile>; RS=<server-region>; HOST=<host>; SCOPE=<scope>; WS=<first-workshop>; WR=<its-region>
```

**The dedicated key** is `~/.ssh/zab-deployer` below: a plain `ssh-ed25519` key file with a passphrase, made for zab alone. It serves all four roles: SSH to the server, tag signing, break-glass access to the VMs and backup decryption (ADMIN §1.6, §1.7). It must be dedicated, never a personal key that also reaches other systems, such as your university's (§2.1). The file name is yours to choose: `zab-deployer` is this runbook's default, and an existing deployment may use another, as ADMIN's examples do. If yours has another name, use it wherever this runbook names `zab-deployer`.

## 1. Prerequisites on the laptop

1. **Tools.** `git` 2.34 or newer (SSH signing), bash 4.4 or newer (`deploy.sh` refuses older; macOS: `brew install bash`, with Homebrew's `bin` ahead of `/bin` in `PATH`), `python3` 3.11 or newer (for `tomllib`: `render_policy.py`, and `deploy.sh`'s `--wheel` check and `uv.lock` summary), `uv`, the AWS CLI (v2, or v1 1.46 or newer: every option used here is in botocore 1.43.62), `ssh-keygen` and `age`:

   ```sh
   git --version; bash --version | head -1; uv --version; aws --version; age --version; python3 --version
   ```

   `deploy.sh --wheel` notes when your `uv` is not 0.11.24, the version with which the build's hash checks were verified (ADMIN §9, V9). The note is for information and stops nothing: a newer uv, 0.12.22, built a release's wheel byte for byte as 0.11.24 did (DECISIONS, verification log). `uv self update 0.11.24` pins it if you want the checked version (standalone installs). Install `age` with `brew install age` or `apt install age`.
2. **The zab clone and a worktree of `main`,** for the checks before §9 makes the release's own worktree. `ZAB_REPO` is the repository you deploy from: the public zab repository, whose address `README.md`'s install line gives, or a fork or mirror whose releases you tag yourself. §9 says how their tags differ:

   ```sh
   ZAB_REPO=<the zab repository's clone URL>
   [ -d ~/zab ] || git clone "$ZAB_REPO" ~/zab
   git -C ~/zab fetch origin --tags
   git -C ~/zab worktree add --detach ~/zab-main origin/main
   ```
3. **The admin directory,** `~/zab-admin`: your copies of the server's configuration (`etc/`), its secrets (`secrets/`) and the role's policies (`policies/`). They are yours alone, so keep git from ever adding them:

   ```sh
   install -d -m 0700 ~/zab-admin ~/zab-admin/etc ~/zab-admin/etc/break-glass ~/zab-admin/secrets \
     ~/zab-admin/secrets/bots ~/zab-admin/policies
   if [ -e ~/zab-admin/.git ]; then
     (cd ~/zab-admin && printf '/etc/\n/secrets/\n/policies/\n' >> "$(git rev-parse --git-path info/exclude)")
   fi
   ```
4. **The AWS profile** reaches the account. If you use IAM Identity Center, `aws sso login --profile "$P"` first, so that no long-lived key sits on the laptop:

   ```sh
   aws sts get-caller-identity --profile "$P" --query Account --output text   # prints <acct>
   ```

## 2. SSH tag signing

### 2.1 The key

zab uses one key of its own for all four roles: `~/.ssh/zab-deployer`, a plain `ssh-ed25519` key file with a passphrase, comment `zab-deployer`. **Never a personal key,** one that also reaches other systems (your university's, GitHub, other servers):
- the scripts zab runs use the key through the agent, so they could use it wherever it reaches;
- a doubt about the key, from zab's side or another system's, would mean replacing it everywhere it reaches, and rotating the participant bot keys that the backups encrypted to it hold (ADMIN §1.7).

Make it once, and set a passphrase when asked. The first line asks twice, so run it alone, then the second:

```sh
[ -e ~/.ssh/zab-deployer ] || ssh-keygen -t ed25519 -C zab-deployer -f ~/.ssh/zab-deployer
ssh-keygen -l -f ~/.ssh/zab-deployer.pub    # 256 SHA256:… zab-deployer (ED25519)
```

It must be a plain key file: `age` decrypts the backups with the file, so a FIDO key (`ED25519-SK`) or a key only an agent holds can't serve (ADMIN §1.7). Keep its passphrase in your password manager.

**The agent.** Run zab work in a terminal with an agent of its own, which holds the dedicated key and nothing else. Every process run from that shell can use the agent, `deploy.sh`, `join_server.py` and whatever they start included. So never use a session agent (your desktop's, the macOS keychain's, a password manager's) that also holds personal keys, and use this terminal for zab alone. The first line asks for the passphrase: run it alone, then the second:

```sh
eval "$(ssh-agent -s)"; ssh-add -t 8h ~/.ssh/zab-deployer   # asks for the passphrase; forgets it after 8 hours
ssh-add -l                                                  # one line: 256 SHA256:… zab-deployer (ED25519)
```

ssh, `deploy.sh`, `join_server.py` and git's signing then use the key without asking. `age` reads the file, and asks for the passphrase itself (§8.2). In a new terminal, run both lines again; after 8 hours, run the `ssh-add` again. The agent outlives its terminal, so `eval "$(ssh-agent -k)"` stops it when you are done.

This limits which keys zab's commands are handed. It is no boundary against code that runs as you, which can reach any agent socket of yours.

**If you began with another key,** such as a personal one, redo with the dedicated key whatever you did with the old one:
- §2.2 and §2.3;
- §3.6's `~/.ssh/config` entry, then §3.8 for a server already launched;
- `admin_keys` in an `admin.toml` already written from §7.2;
- §8.2 and §8.3.

VMs launched with the old key in `admin_keys` keep it, spares included: `join_server.py` adds the dedicated key to the roster's VMs and removes the old one, over SSH, and the spares are torn down and launched again (§17, step 5, has the commands for both). Otherwise backups stay encrypted to the old key, and new VMs get it. A tag signed with the old key fails §11's check against the new `allowed_signers`: if you haven't pushed it, delete it and sign again (§13.1); if you have, make the next release instead. Then check, on a machine that holds the old key's `.pub`:

```sh
OLD=<old.pub>                                          # the old key's public half
grep -rlF -e "$(cut -d' ' -f2 "$OLD")" ~/zab-admin/etc  # prints nothing
ls ~/zab-admin/etc/break-glass                         # zab-deployer.pub
grep -h admin_keys ~/zab-admin/etc/admin.toml          # admin_keys = ["/etc/zab-admin/break-glass/zab-deployer.pub"]
git -C ~/zab config user.signingkey                    # …/.ssh/zab-deployer
```

The first grep looks for the old key's base64, which is all that `allowed_signers` and `backup_recipients` hold of it (no comment). The next two lines find it by file name: in `break-glass/` it may be under any name, and `admin_keys` names that file. The checks leave `~/.ssh/config` out, which may name the old key for other hosts: §3.6's `ssh -G` check covers zab's entry.

### 2.2 `allowed_signers`, as `deploy.sh` checks it

`deploy.sh` verifies every tag, on the laptop and again on the server, with `git verify-tag` against this file, and requires `Good "git" signature` (ADMIN §1.6). One line per signer, in the `git` namespace:

```sh
EMAIL=<email>
printf '%s namespaces="git" %s\n' "$EMAIL" "$(cut -d' ' -f1,2 ~/.ssh/zab-deployer.pub)" > ~/zab-admin/etc/allowed_signers
cat ~/zab-admin/etc/allowed_signers        # <email> namespaces="git" ssh-ed25519 AAAA…
```

It replaces the file: a line made earlier for another key goes.

### 2.3 git

In `~/zab` (its worktrees share the setting):

```sh
git -C ~/zab config gpg.format ssh
git -C ~/zab config user.signingkey "$HOME/.ssh/zab-deployer"
git -C ~/zab config gpg.ssh.allowedSignersFile "$HOME/zab-admin/etc/allowed_signers"
git -C ~/zab tag -s zab-signing-test -m test HEAD && git -C ~/zab tag -v zab-signing-test
git -C ~/zab tag -d zab-signing-test       # never pushed
```

`tag -v` prints `Good "git" signature for <email> with ED25519 key SHA256:…`. The signing of the release itself is §9.

### 2.4 Optional: a tag ruleset

Only for a repository whose releases you tag yourself (§9), on GitHub (ADMIN §1.6). The repository's *Settings → Rules → Rulesets → New tag ruleset*: target `v*`, exclude `v*rc*` (release candidates are tagged by whoever tests them), with *Restrict creations*, *Restrict updates* and *Restrict deletions*, and only the signers on the bypass list. It keeps final tags the signers' on GitHub. The deploy gate doesn't rely on it: the server checks the signature itself.

## 3. AWS: the server, in `$RS`

The harness makes the same resources for the test server (`aws_rig.py`), with `zab-role=test` added and a role named `zab-test-admin`. The server carries no `zab-role` tag, and never a `workshop=` or `zab-scope=` tag (ADMIN §1.1).

Cost: about $18 a month at list prices (a `t4g.small`, 30 GiB of gp3, the Elastic IP's public IPv4).

**A server already launched with another key pair:** don't launch another. Do §3.6's `~/.ssh/config` entry, then §3.8, which moves the server to the dedicated key.

```sh
NAME=<name>                 # zab-admin=<name>; e.g. prod
KEYPAIR=<key-pair>          # e.g. zab-deployer
MYCIDR="$(curl -fsS https://checkip.amazonaws.com)/32"   # this laptop's address; never an agent machine's (§3.1)
[[ $MYCIDR =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]] || echo "not one address: $MYCIDR"
```

If the check prints `not one address`, checkip's answer was malformed: set `MYCIDR` again before §3.1.

### 3.1 The security group `zab-admin`

443 and 80 from anywhere, for Caddy (TLS, the ACME challenge and the redirect); 22 from your address only. Outbound stays open: Zulip, GitHub, the AWS API, SSH to the VMs.

```sh
VPC=$(aws ec2 describe-vpcs --profile "$P" --region "$RS" \
  --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 create-security-group --profile "$P" --region "$RS" --group-name zab-admin \
  --description "zab admin server: 443 and 80 from anywhere, 22 from the deployers" --vpc-id "$VPC" \
  --tag-specifications "ResourceType=security-group,Tags=[{Key=zab-admin,Value=$NAME},{Key=Name,Value=zab-admin}]" \
  --query GroupId --output text)
aws ec2 authorize-security-group-ingress --profile "$P" --region "$RS" --group-id "$SG" \
  --ip-permissions 'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0,Description=caddy}]' \
                   'IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0,Description=acme}]' \
                   "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$MYCIDR,Description=deployer}]" \
  --query Return --output text                     # True
echo "$SG"
```

Each `authorize-security-group-ingress` and `revoke-security-group-ingress` in this runbook ends `--query Return --output text`, so it prints only `True`. By default, an authorize prints each new rule with the account ID (its `GroupOwnerId` and its ARN), and a revoke the rule it removed, with your old address. The refresh's read-back, below, shows the rules instead.

**Refresh the SSH rule** whenever your address may have changed, which can be every few hours. It needs no old value.

**In one command** (#674), `workshop/adminserver/ssh-rule.sh`, from a reviewed checkout: `~/zab-main` until you have signed a release, then the release you signed (§9). It does what the block below does, and checks the read-back itself. It also checks that this terminal's agent holds the dedicated key alone (§2.1), since the SSH step after it needs it.

**Its values,** saved in `~/.config/zab/ssh-rule.conf` so that later runs need none. A flag still overrides the saved value. With neither, the script stops and says what to give:

```sh
~/zab-main/workshop/adminserver/ssh-rule.sh --save-defaults --profile "$P" --region "$RS" --key ~/.ssh/zab-deployer
# saved, in $HOME/.config/zab/ssh-rule.conf: profile <P>, region <RS>, key $HOME/.ssh/zab-deployer
```

**Home's address,** the script's `HOMEIP` (§0), kept in `~/.config/zab/homeip`. Record it on the network you share with an agent machine, while still on it. Record it again whenever home's address may have changed: the script warns when the record is over 30 days old.

```sh
~/zab-main/workshop/adminserver/ssh-rule.sh --record-home
# recorded: this network's address, as home, in $HOME/.config/zab/homeip (not printed)
# From here, run ssh-rule.sh from another network: a phone's hotspot, a VPN, your office.
```

**Then, from another network, before each SSH step,** a dry run if you like, which changes nothing, then the run:

```sh
~/zab-main/workshop/adminserver/ssh-rule.sh --dry-run
~/zab-main/workshop/adminserver/ssh-rule.sh
```

The run prints these lines. On the rules lines, the fields are separated by tabs; the last three, IPv6 ranges, source groups and prefix lists, are empty, so each such line ends with three tabs; and the rules come in any order:

```text
home: not this network ($HOME/.config/zab/homeip)
address: <your address>/32 (checkip)
key: SHA256:…, alone in the agent
group: sg-…, the admin server's group (profile <P>, region <RS>)
revoke sgr-…: True
authorize tcp 22 from <your address>/32: True
rules (protocol, ports, then IPv4 ranges, IPv6 ranges, groups and prefix lists):
  tcp	80	80	0.0.0.0/0			
  tcp	22	22	<your address>/32			
  tcp	443	443	0.0.0.0/0			
ok: 443 and 80 from 0.0.0.0/0, 22 from <your address>/32 alone
```

There is one `revoke` line for each old rule for port 22, and none when there is none. The dry run prints `DRY RUN: nothing changes` first, then the same lines up to `group:`. Then come `would revoke sgr-…` and `would authorize tcp 22 from <your address>/32`, and the rules as they are now, the old 22 rules among them. It ends `ok: the run would leave 443 and 80 from 0.0.0.0/0, 22 from <your address>/32 alone`. These lines are from a run against a test group (#743).

- **No home address.** The script refuses to start without one. Pass `--home-unknown` instead when you share no network with an agent machine, or are away with none recorded; or write `none` into the file. Either prints a `warning:` line in place of the `home:` line.
- **The key.** When the agent lacks it, the script prints the commands that load it, since a script can't start an agent for the shell that runs it. Paste them, then run the script again: the `ssh-add` line asks for the passphrase.
- **A `stopped: …` line** says what to fix:
  - **Before the revokes** (the address, home, the key, the group, a rule ID): nothing changed.
  - **A revoke or the authorize that fails after a revoke:** the old rules may be gone and the new one not there, so port 22 may admit nothing. The line says so: `stopped: port 22 may admit nothing now: run this again, or BOOTSTRAP §3.1's block`. Run the script again: it finds no old rule, and admits your address. If AWS refuses again, its error is on the line above: fix that, or use the block below.
  - **After the read-back:** another rule reaches the group. See below.
- `--close` revokes every rule for port 22, from any network, and ends `ok: 443 and 80 from 0.0.0.0/0`; `--help` lists the options.

**By hand,** the fallback, in one block. On your laptop, it:
1. finds the server's group by its name and its `zab-admin` tag, so it never picks another group of that name in another VPC;
2. checks that the lookup gave one group ID (`sg-…`), and that checkip's answer is one address and not `$HOMEIP` (§0), and touches nothing otherwise;
3. revokes every rule for port 22 alone (22 to 22), whatever its source: an IPv4 or IPv6 range, a source group or a prefix list. Then it admits your current address alone;
4. reads the group's rules back.

```sh
SG=$(aws ec2 describe-security-groups --profile "$P" --region "$RS" \
  --filters Name=group-name,Values=zab-admin Name=tag-key,Values=zab-admin \
  --query 'SecurityGroups[0].GroupId' --output text)
NEW="$(curl -fsS https://checkip.amazonaws.com)/32"
if ! [[ ${HOMEIP:-} == none || ${HOMEIP:-} =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]]; then
  echo "stopped: set HOMEIP first (§0)"
elif [[ $NEW == "$HOMEIP" ]]; then
  echo "stopped: $NEW is HOMEIP, a network an agent machine shares: use another network"
elif [[ $SG =~ ^sg-[0-9a-f]+$ && $NEW =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]]; then
  for R in $(aws ec2 describe-security-group-rules --profile "$P" --region "$RS" \
      --filters "Name=group-id,Values=$SG" \
      --query 'SecurityGroupRules[?!IsEgress && FromPort==`22` && ToPort==`22`].SecurityGroupRuleId' --output text); do
    aws ec2 revoke-security-group-ingress --profile "$P" --region "$RS" --group-id "$SG" \
      --security-group-rule-ids "$R" --query Return --output text
  done
  aws ec2 authorize-security-group-ingress --profile "$P" --region "$RS" --group-id "$SG" \
    --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$NEW,Description=deployer}]" \
    --query Return --output text
else
  echo "stopped: group '$SG', address '$NEW'"
fi
aws ec2 describe-security-groups --profile "$P" --region "$RS" --group-ids "$SG" \
  --query 'SecurityGroups[0].IpPermissions[].[IpProtocol,FromPort,ToPort,join(`","`,IpRanges[].CidrIp || `[]`),join(`","`,Ipv6Ranges[].CidrIpv6 || `[]`),join(`","`,UserIdGroupPairs[].GroupId || `[]`),join(`","`,PrefixListIds[].PrefixListId || `[]`)]' --output text
```

The read-back prints one line per rule: protocol, ports, then its sources: IPv4 ranges, IPv6 ranges, source security groups and prefix lists. It must print exactly three rules:
- 443 from `0.0.0.0/0`;
- 80 from `0.0.0.0/0`;
- 22 from your `/32` alone.

The last three columns must be empty on every line. Any of these means someone changed the group:
- a fourth rule;
- an IPv6 range;
- a source group or prefix list, which can share the 22 rule and admit other hosts;
- `-1` (all traffic), or ports that cover 22.

Remove it before you connect. The refresh never revokes the last kind: it revokes only rules for port 22 alone, so that it never takes one that also carries 80 or 443. The script checks these lines itself, and stops if any other rule is there.

SSH is needed only to deploy, so run it right before:
- §3.6's first connection, or §3.8's;
- every deploy: §11's bootstrap and wheel, and later upgrades;
- §12's checks on the server, and any other SSH step later, such as §6.5's or §13.1's.

**Why no agent machine.** A coding agent runs code it writes or fetches, and a prompt injection in a file, a web page or a message can steer it. So treat any machine where coding agents run as compromised: it holds no credential of this deployment, and has no network path to the server, whose SSH admits your address alone (ADMIN §1.4).

**A network you share with an agent machine.** Machines on one network, such as your home's, reach the internet from one public IPv4, so checkip prints the same address on each. If a machine where coding agents run shares a network with you, a rule for your address there admits it too (§13.2), which is why the refresh refuses `$HOMEIP` (§0). Then run the refresh, and every SSH step after it, from another network: a phone's hotspot, a VPN, your office. The other AWS steps and the Zulip UI can stay there. A rule made from that network names the agent machine's address: refresh it from the other network before you connect.

### 3.2 The role and instance profile `zab-admin`

The role is made now, empty. Its managed policies come in §10, rendered on the laptop from your `admin.toml` by the signed release (`workshop/adminserver/README.md`, "The instance role's policies"). IAM evaluates a role's policies at each call, so the instance can run before they are attached. If `create-role` answers `EntityAlreadyExists`, stop: something else uses that name in the account. Each create prints only the name: by default it prints the new resource, whose ARN holds the account ID.

```sh
aws iam create-role --profile "$P" --role-name zab-admin \
  --assume-role-policy-document "file://$HOME/zab-main/workshop/waker/trust-ec2.json" \
  --description "zab admin server, docs/ADMIN.md section 5" \
  --query Role.RoleName --output text                                   # zab-admin
aws iam create-instance-profile --profile "$P" --instance-profile-name zab-admin \
  --query InstanceProfile.InstanceProfileName --output text             # zab-admin
aws iam add-role-to-instance-profile --profile "$P" --instance-profile-name zab-admin --role-name zab-admin
```

### 3.3 Your key pair in `$RS`

```sh
aws ec2 describe-key-pairs --profile "$P" --region "$RS" --key-names "$KEYPAIR" \
  --query 'KeyPairs[0].KeyFingerprint' --output text ||
aws ec2 import-key-pair --profile "$P" --region "$RS" --key-name "$KEYPAIR" \
  --public-key-material "fileb://$HOME/.ssh/zab-deployer.pub"
```

### 3.4 The instance

A `t4g.small` on stock Ubuntu 24.04 arm64, never the workshop AMI (ADMIN §1.1). The image comes from Canonical's public parameter; check its name and root device before the launch:

```sh
AMI=$(aws ssm get-parameter --profile "$P" --region "$RS" \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text)
aws ec2 describe-images --profile "$P" --region "$RS" --image-ids "$AMI" \
  --query 'Images[0].[Name,Architecture,RootDeviceName]' --output text
# ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-…   arm64   /dev/sda1
ROOTDEV=$(aws ec2 describe-images --profile "$P" --region "$RS" --image-ids "$AMI" \
  --query 'Images[0].RootDeviceName' --output text)
```

Then launch it: 30 GiB encrypted gp3, IMDSv2 with a hop limit of 1, termination protection, the instance profile, your key pair, and the server's tag on the instance and its volume. The volume mapping names the image's own root device (`$ROOTDEV`), as the harness does (`aws_rig.launch_server`): under any other name, EC2 would add a second volume and boot from the image's default 8 GiB root. A refusal naming the instance profile (`Invalid IAM Instance Profile name`) means IAM hasn't propagated it yet: wait 20 s and run the same command again.

```sh
I=$(aws ec2 run-instances --profile "$P" --region "$RS" \
  --image-id "$AMI" --instance-type t4g.small --count 1 \
  --key-name "$KEYPAIR" --security-group-ids "$SG" \
  --iam-instance-profile Name=zab-admin --disable-api-termination \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled \
  --block-device-mappings "DeviceName=$ROOTDEV,Ebs={VolumeSize=30,VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=zab-admin,Value=$NAME},{Key=Name,Value=zab-admin}]" \
                       "ResourceType=volume,Tags=[{Key=zab-admin,Value=$NAME},{Key=Name,Value=zab-admin}]" \
  --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --profile "$P" --region "$RS" --instance-ids "$I"
aws ec2 describe-instances --profile "$P" --region "$RS" --instance-ids "$I" \
  --query 'Reservations[0].Instances[0].[InstanceType,MetadataOptions.HttpTokens,MetadataOptions.HttpPutResponseHopLimit,ends_with(IamInstanceProfile.Arn || `""`, `":instance-profile/zab-admin"`)]' --output text
aws ec2 describe-instance-attribute --profile "$P" --region "$RS" --instance-id "$I" \
  --attribute disableApiTermination --query DisableApiTermination.Value
# t4g.small  required  1  True   and   true
aws ec2 describe-volumes --profile "$P" --region "$RS" --filters "Name=attachment.instance-id,Values=$I" \
  --query 'Volumes[].[Attachments[0].Device,Size,VolumeType,Encrypted]' --output text
# exactly one line: /dev/sda1  30  gp3  True
```

The first line's `True` says the instance has the profile `zab-admin`: it checks the end of the profile's ARN, which holds the account ID, without printing it. `False` means another profile, or none.

Anything else from `describe-volumes` (two lines, `8`, `False`) means the root volume isn't the one ADMIN §1.1 asks for. Terminate the instance with §13.1's first three commands (the protection lifted, the terminate, the wait), then launch again.

### 3.5 The Elastic IP

```sh
ALLOC=$(aws ec2 allocate-address --profile "$P" --region "$RS" --domain vpc \
  --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=zab-admin,Value=$NAME},{Key=Name,Value=zab-admin}]" \
  --query AllocationId --output text)
aws ec2 associate-address --profile "$P" --region "$RS" --instance-id "$I" --allocation-id "$ALLOC"
EIP=$(aws ec2 describe-addresses --profile "$P" --region "$RS" --allocation-ids "$ALLOC" \
  --query 'Addresses[0].PublicIp' --output text)
echo "$EIP"
```

### 3.6 SSH, with the host key pinned

The harness pins the server's host key from the fingerprints cloud-init prints on the console at the first boot, never on first use (`aws_rig.pin_host_key`). Do the same. The console may take a few minutes to show them:

```sh
aws ec2 get-console-output --profile "$P" --region "$RS" --instance-id "$I" --latest --output text |
  sed -n '/BEGIN SSH HOST KEY FINGERPRINTS/,/END SSH HOST KEY FINGERPRINTS/p'
```

Add this entry to `~/.ssh/config`, with `$HOST`'s and `$EIP`'s values. No agent or X11 forwarding, ever. `IdentityAgent SSH_AUTH_SOCK` keeps ssh on §2.1's agent when a later block names another, such as a password manager's.

**Put it above any `Host *` block.** ssh takes the *first* value it finds for each option, so an earlier `Host *` with `ForwardAgent yes`, `IdentityAgent …` or `StrictHostKeyChecking accept-new` would override this entry without a word. `deploy.sh` passes none of these options itself.

```text
Host <the server's name, $HOST>
  HostName <the Elastic IP>
  User ubuntu
  IdentityFile ~/.ssh/zab-deployer
  IdentitiesOnly yes
  IdentityAgent SSH_AUTH_SOCK
  ForwardAgent no
  ForwardX11 no
  ClearAllForwardings yes
  StrictHostKeyChecking yes
```

Check what ssh will actually use:

```sh
ssh -G "$HOST" | grep -i -E '^(hostname|user|identityfile|identityagent|forwardagent|forwardx11|stricthostkeychecking) '
# user ubuntu, hostname <the Elastic IP>, forwardx11 no, stricthostkeychecking true,
# identityagent SSH_AUTH_SOCK, identityfile ~/.ssh/zab-deployer, forwardagent no (one line each)
```

Any other `forwardagent`, `identityagent` or `stricthostkeychecking` means a block above this one wins: move this entry up. A second `identityfile` line comes from such a block too (`IdentityFile` adds up).

**A server launched with another key pair** refuses the dedicated key: its first connection is §3.8's. Otherwise, refresh the SSH rule (§3.1), and connect once, with `-o StrictHostKeyChecking=ask` (the command line comes before the file, so ssh asks this one time, on a line of its own). Accept the key only if the `ED25519` fingerprint ssh shows is the one the console printed:

```sh
ssh -o StrictHostKeyChecking=ask "$HOST" 'lsb_release -ds; uname -m; sudo -n true && echo sudo ok'
# Ubuntu 24.04… LTS   aarch64   sudo ok
```

From then on, `StrictHostKeyChecking yes` refuses any other host key. `deploy.sh --host "$HOST"` uses this entry. EC2 Instance Connect (§3.8) stops working once the bootstrap installs the IMDS rule, as intended (the adminserver README, "IMDS").

### 3.7 Finding the IDs again

```sh
NAME=<name>
SG=$(aws ec2 describe-security-groups --profile "$P" --region "$RS" \
  --filters Name=group-name,Values=zab-admin Name=tag-key,Values=zab-admin \
  --query 'SecurityGroups[0].GroupId' --output text)
I=$(aws ec2 describe-instances --profile "$P" --region "$RS" \
  --filters "Name=tag:zab-admin,Values=$NAME" Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
ALLOC=$(aws ec2 describe-addresses --profile "$P" --region "$RS" \
  --filters "Name=tag:zab-admin,Values=$NAME" --query 'Addresses[0].AllocationId' --output text)
EIP=$(aws ec2 describe-addresses --profile "$P" --region "$RS" --allocation-ids "$ALLOC" \
  --query 'Addresses[0].PublicIp' --output text)
```

### 3.8 A server launched with another key

For a server launched with a key pair other than the dedicated key's. This puts the dedicated key alone in `~ubuntu/.ssh/authorized_keys`, without ever using the old key. EC2 Instance Connect pushes the dedicated key's public half to the instance, which admits it for 60 s. That works only until the bootstrap installs the IMDS rule (§3.6), so do this before §11. The instance's key pair can stay: cloud-init writes its key at the first boot only.

1. **Ready:**
   - §2.1's terminal, its agent holding the dedicated key;
   - §3.6's `~/.ssh/config` entry, with `IdentityFile ~/.ssh/zab-deployer`, and its `ssh -G` check;
   - `$I` (§3.7);
   - the SSH rule refreshed from where you are (§3.1).
2. **The host key's fingerprint,** to check this machine's first connection against. On the machine where you pinned it in §3.6, this prints it. Otherwise use the console's, from §3.6's command, if it still shows the first boot:

   ```sh
   ssh-keygen -l -F <the Elastic IP>    # or "$EIP" where §3.7 set it; … ED25519 SHA256:…
   ```
3. **Push the key, then connect within 60 s.** The connection replaces `authorized_keys` with the dedicated key alone, and prints the file back. Accept the host key only if its `ED25519` fingerprint is step 2's. The `ssh` asks for it, so paste the two commands one at a time:

   ```sh
   aws ec2-instance-connect send-ssh-public-key --profile "$P" --region "$RS" --instance-id "$I" \
     --instance-os-user ubuntu --ssh-public-key "file://$HOME/.ssh/zab-deployer.pub" --query Success --output text
   # True
   ssh -o StrictHostKeyChecking=ask "$HOST" \
     'umask 077 && cat > ~/.ssh/ak.new && test -s ~/.ssh/ak.new && mv ~/.ssh/ak.new ~/.ssh/authorized_keys && cat ~/.ssh/authorized_keys' \
     < ~/.ssh/zab-deployer.pub
   # ssh-ed25519 AAAA… zab-deployer
   ```

   `Permission denied (publickey)` means the push expired first, such as while you compared the fingerprint: run both commands again. A host key you accepted isn't asked again.
4. **A minute later,** once the push has expired, check with the dedicated key by itself:

   ```sh
   ssh "$HOST" 'cat ~/.ssh/authorized_keys; sudo -n true && echo sudo ok'
   # ssh-ed25519 AAAA… zab-deployer, then sudo ok
   ```

   Getting in now proves that the file admits the key, since the push no longer does. If it refuses, push again and fix the file over that connection: until §11, EC2 Instance Connect is the way back in.

### 3.9 Swap: a 2 GB file

**The server keeps a 2 GB swap file** (#717). A `t4g.small` has 2 GiB and no swap, and the bot and the collector have no memory cap of their own, so a spike, such as a deploy's build beside the running units, then slows the server instead of having the kernel kill one of them. It is not for the waker's start-up, which since #717 peaks at up to about 270 MB, measured, under its unit's `MemoryMax=512M` (ADMIN §1.2). Make it once, before §11; the `/etc/fstab` line keeps it across reboots:

```sh
ssh "$HOST" 'sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab'
# Setting up swapspace version 1, size = 2 GiB (2147479552 bytes), a UUID line, then: /swapfile none swap sw 0 0
ssh "$HOST" 'swapon --show=NAME,SIZE --noheadings; grep -c "^/swapfile " /etc/fstab'
# /swapfile   2G, then: 1
```

**A server that has a swap file already,** such as Nov 1's, made by hand (ADMIN §1.8): the second command alone. If `grep` prints `0`, the file is not in `/etc/fstab`, and the next reboot drops it: add the line with `ssh "$HOST" 'echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab'`, then run the second command again.

## 4. DNS

In your domain's DNS, add:

| Name | Type | Value | TTL |
|---|---|---|---|
| `$HOST` | A | the Elastic IP (`$EIP`) | 300 |

No AAAA record: the server has no IPv6 address. If the zone has CAA records, they must allow `letsencrypt.org`, Caddy's CA. Check it before §11, since Caddy asks for its certificate when it first starts:

```sh
dig +short "$HOST" A                  # the Elastic IP
dig +short "${HOST#*.}" CAA           # nothing, or a line naming letsencrypt.org
```

## 5. Zulip, in the web UI

Everything here is an organization owner's action in the web UI. The harness does the same on a test organization through the API (`zulip_rig.py`).

### 5.1 `zab-provisioner`: one account, a Moderator

`zab-provisioner` is the human account whose key provisions participants: it owns their bots, binds them to their roster rows by e-mail, and creates new workshops' bots and channels (ADMIN §1.5, §5). **One account, ever:** a second would hold a second key with the provisioner's power, and `move_bots.py` pins its target by user ID and checks the full name, so two accounts of that name are an ambiguity it must never meet.

1. **The account.** If the organization has none yet, invite one (*Invite users*, role **Moderator**, an address you control that no other Zulip account uses), accept the invitation in a private window, and set its full name to exactly `zab-provisioner`. If it exists, find it under *Organization settings → Users*: its role must be **Moderator** (set it if it isn't), and its full name exactly `zab-provisioner`. A Moderator can read every address whose visibility includes moderators, which binding participants by e-mail needs, and can't change the organization's settings or anyone's role.
2. **Its user ID.** Note it from its profile; §5.6 prints it too.
3. **Its key.** Signed in as `zab-provisioner`: *Personal settings → Account & privacy → API key → Manage your API key → Download zuliprc*. Then, on the laptop, with the downloaded file's path:

   ```sh
   F=<the downloaded zuliprc>
   install -m 0600 "$F" ~/zab-admin/secrets/provisioner.zuliprc && rm "$F"
   ```

   Don't *Generate new API key* unless the old one leaked. §5.6 checks that the key is this account's, a `moderator`.
4. **Its password: keep it,** in your password manager. §13.3, for a server that may be compromised, signs in as `zab-provisioner` to regenerate its key and the admin bots' keys.

It owns every participant bot, so it is **never** subscribed to `#zab-admins` or to any `#…-organizers` channel (ADMIN §1.5). It **is** subscribed to each `#…-fleet-alerts` channel (§5.4).

### 5.2 The org's default e-mail visibility

*Organization settings → Default user settings*, under privacy: *Who can access user's email address* is **Admins, moderators and members** (API value 2; ORGANIZERS §1.1 item 7). Set it before any participant is invited: it is the default for accounts created afterwards. The provisioner, a Moderator, then binds new participants by e-mail, and so do their own bots, which are members. The trade-off: members can see each other's addresses (SPEC §5.2).

### 5.3 The admin bots

**Create them signed in as `zab-provisioner`** (a private window, with its password: §5.1). Each bot then starts out owned by it, the owner ADMIN §1.7's recovery assumes, with no owner change to make. It also checks early that a Moderator may create a generic bot, as `!new-workshop` will with the provisioner's key (ADMIN §9, V11).

Create each one under *Personal settings → Bots → Add a new bot*, as a **Generic bot**, with exactly these names: commands may mention a bot by its full name, and the bots' hints name each other by it (ADMIN §2.1).

| Full name | Bot e-mail (short name) | Serves | Its key, saved as |
|---|---|---|---|
| `Workshop admin` | `workshop-admin` | the server-level verbs, `!new-workshop` among them, in `#zab-admins` | `~/zab-admin/secrets/global.zuliprc` |
| `Workshop admin · <WS>` | `<WS>-admin` | the first workshop, in `#<WS>-organizers` | `~/zab-admin/secrets/bots/<WS>.zuliprc` |

These two only. The `·` is U+00B7, a middle dot (macOS: Option-Shift-9). For each bot, *Download zuliprc* from its row in *Personal settings → Bots*, and move the file at once to the path in the table (each download is named `zuliprc`):

```sh
F=<the downloaded zuliprc>; install -m 0600 "$F" ~/zab-admin/secrets/global.zuliprc && rm "$F"
F=<the next download>; install -m 0600 "$F" ~/zab-admin/secrets/bots/"$WS".zuliprc && rm "$F"
```

Then sign out of `zab-provisioner`'s window: §5.4 is done from your own account.

- **If Zulip doesn't let the provisioner add a bot,** the organization's bot-creation setting leaves Moderators out, and `!new-workshop` would fail the same way: let Moderators add bots, in *Organization settings → Organization permissions*, then add the bot again.
- **A bot you made as yourself** works as well once `zab-provisioner` owns it: *Organization settings → Bots*, edit the bot, *Owner*, as an organization administrator or organization owner: Zulip Cloud lets no one else change a bot's owner, not even the bot's owner (#758). The move changes no key (ADMIN §9, V15).

§5.6 prints each bot's owner.

**No bot for a later workshop.** `!new-workshop` creates each new workshop's bot itself, `Workshop admin · <name>`, with the provisioner's key (ADMIN §2.9). One made by hand under that name would leave `!new-workshop` the name taken.

### 5.4 The channels

Create each one **from your own account, never as `zab-provisioner`:** a channel's creator is subscribed to it, and becomes its administrator by default. Use *Channel settings → Create channel* (the gear menu). Each is **private with shared history**, which Zulip's form doesn't name as such:
- *Who can access this channel*: **Private**;
- *Subscribers can view messages sent before they joined*: ticked, which is shared history.

After the creation, both settings are in the channel's *Permissions* tab, and §5.6 prints them. Give each exactly these subscribers. Each bot warns, at its start and hourly, about any other subscriber of its own channel (ADMIN §2.1); it never changes them.

| Channel | Subscribers | Never |
|---|---|---|
| `#zab-admins` | the global admins, `Workshop admin` | `zab-provisioner`, any other bot |
| `#<WS>-organizers` | the global admins, the workshop's organizers, `Workshop admin · <WS>` | `zab-provisioner`, helpers, participants |
| `#<WS>-fleet-alerts` | the global admins, the workshop's organizers, `zab-provisioner` | |

- **These three only.** `!new-workshop` creates a later workshop's channels, `#<name>-organizers` and `#<name>-fleet-alerts`, with their members (ADMIN §2.9). So don't create one by hand: `!new-workshop` would find the name taken.
- **The first workshop's participants' channel, optional** (#627). Its subscribers become the workshop's roster (ORGANIZERS §3.1): a private channel with shared history, with the workshop's organizers and `zab-provisioner`, into which the organizers invite the participants by e-mail. Its ID goes into `channel` (§7.2). Without one, delete that line: the organizers then import the roster as a CSV. A later workshop's is `!new-workshop`'s `channel=` (ADMIN §2.9).
- **The alerts channel** takes no commands. The waker posts a failed wake there through the participant's own bot, which may post in a private channel only if it or its owner is subscribed: hence `zab-provisioner`.
- **A channel of one of these names that exists already** may be reused only if its subscribers are exactly those of the table.

### 5.5 The global admins' user IDs

Yours: click your name, then *View profile*: the profile shows the *User ID*. Each other global admin's (§0), and each of the first workshop's organizers', the same way, on their profiles. §5.6 prints them anyway, from the channels' member lists: check them there.

### 5.6 Read the IDs back from the keys

This reads, with each key you saved, its own identity and channels, and prints no key. It checks the keys work, and gives every ID `admin.toml` needs:

```sh
cd ~/zab-admin/secrets && python3 -I - global.zuliprc bots/*.zuliprc provisioner.zuliprc <<'EOF'
import base64, configparser, json, sys, urllib.request
ROLES = {100: "owner", 200: "administrator", 300: "moderator", 400: "member", 600: "guest"}
names = {}
def api(rc, path):
    config = configparser.ConfigParser(interpolation=None)
    try:  # never shows the error: it may quote a line, and a line may be the key
        found = config.read(rc)
    except (configparser.Error, UnicodeDecodeError):
        sys.exit("%s: not a zuliprc (it doesn't parse)" % rc)
    if not found or not config.has_section("api"):
        sys.exit("%s: missing, or not a zuliprc (no [api] section)" % rc)
    a = config["api"]
    if not all(a.get(field) for field in ("site", "email", "key")):
        sys.exit("%s: its [api] section lacks site, email or key" % rc)
    request = urllib.request.Request(a["site"].rstrip("/") + "/api/v1/" + path)
    pair = (a["email"] + ":" + a["key"]).encode()
    request.add_header("Authorization", "Basic " + base64.b64encode(pair).decode())
    with urllib.request.urlopen(request, timeout=30) as answer:
        return json.load(answer)
def who(rc, uid):
    if uid not in names:
        names[uid] = api(rc, "users/%d" % uid)["user"]["full_name"]
    return "%d %s" % (uid, names[uid])
for rc in sys.argv[1:]:
    me = api(rc, "users/me")
    kind = "bot" if me["is_bot"] else ROLES.get(me.get("role"), "role %s" % me.get("role"))
    if me["is_bot"]:
        kind += ", owner " + (who(rc, me["bot_owner_id"]) if me.get("bot_owner_id") else "none")
    print('%s: user %d "%s" <%s>, %s' % (rc, me["user_id"], me["full_name"], me["email"], kind))
    for s in api(rc, "users/me/subscriptions")["subscriptions"]:
        line = "  #%s: channel %d" % (s["name"], s["stream_id"])
        if s["invite_only"]:
            shared = "shared" if s.get("history_public_to_subscribers") else "protected"
            members = api(rc, "streams/%d/members" % s["stream_id"])["subscribers"]
            line += ", private, %s history, members: " % shared
            line += ", ".join(who(rc, u) for u in sorted(members))
        else:
            line += ", web-public" if s.get("is_web_public") else ", public"
        print(line)
EOF
```

A file that is missing, or isn't a zuliprc, stops it with one line naming the file and never quoting it, such as `global.zuliprc: missing, or not a zuliprc (no [api] section)` or `…: not a zuliprc (it doesn't parse)`. Put the right zuliprc at the path §5.1 or §5.3's table gives, downloaded again if need be, then run it again.

Check what it prints, for three keys: `global.zuliprc`, `bots/<WS>.zuliprc` (the only file under `bots/`) and `provisioner.zuliprc`:
- each of the two bots is a `bot, owner <id> zab-provisioner`, with the full name of §5.3, subscribed to its own private channel of §5.4 and to no other private channel (public default channels, if Zulip added any, don't matter);
- `provisioner.zuliprc` is `"zab-provisioner"`, a `moderator`, in `#<WS>-fleet-alerts` (and in the participants' channel, if there is one) and in no `#…-organizers` or `#zab-admins`;
- each of §5.4's channels is `private, shared history`, and its member list is the table's.

What to fix:
- **A §5.4 channel whose line says `public`,** with no members after it, was created public. In its *Permissions* tab, set *Who can access this channel* to **Private** and tick *Subscribers can view messages sent before they joined*. Anyone in the org could join it while it was public, so remove from its *Subscribers* tab everyone the table doesn't name, then run this again.
- **`protected history`:** tick that box in the channel's *Permissions* tab.
- **A bot's `owner`** other than `zab-provisioner`: make it the owner (§5.3).

Note each bot's user ID and e-mail, each channel's ID, and the global admins' and organizers' user IDs, for §7.

### 5.7 The organization's settings for workshops

Once per organization, an owner checks these, besides §5.2's e-mail visibility. The bridge depends on the first three (SPEC §4.1); the participants' sandboxes and the repository invitations on the rest.

- *Organization permissions → Message editing:* editing allowed, with no time limit. The bridge edits its status messages throughout turns that can last hours.
- *Organization permissions → Moving messages:* members may edit topics and move messages, with no time limit, so that participants can reorganize their sessions.
- *Organization settings → Compose settings:* the *general chat* topic is not allowed. Topic = session.
- *Organization permissions → Channel permissions:* members may create public and private channels and subscribe others to channels: participants administer their sandboxes, may make them public, create channels and add collaborators and their own bots to them (`docs/quickstart.md`). **Web-public channels stay disabled,** so that the agents' output stays inside the organization. Wildcard mentions of many users: administrators and moderators only.
- *Organization permissions → Other permissions:* members may create bots, for a participant who goes on with zab on their own machine (`README.md`).
- *Organization settings → Custom profile fields:* a field named **GitHub**, of type *External account → GitHub*. Participants put their GitHub username there, and `provision_repos.py` invites that login to their repository. It is self-reported: it never binds a Zulip user to a roster row, which the e-mail does ([`workshop/admin/README.md`](../workshop/admin/README.md) §1, §1b).
- *Default user settings* (for users who join afterwards): follow the topics you start or take part in, with followed-topic notifications on, so that a participant hears when their agent answers.

## 6. AWS: each region, once

**Each region the workshops use is set up once,** by the account owner (ADMIN §5, "Setting up a region"): a security group and an AMI copy for the fleet's VMs, both tagged `zab-scope=$SCOPE` (§6.1). Nothing is made per workshop by hand: the server imports each workshop's key pair itself, at the bootstrap for the first workshop (`deploy.sh`'s `fleet_key`, §6.3) and through `!new-workshop` for the others.
- **At the bootstrap:** the first workshop's region, `$WR`, with §6.1; its IDs for `admin.toml` with §6.2.
- **A region added later,** for a later workshop: §6.5, which runs §6.1 there, adds the region to `admin.toml`'s `[global] regions` and the role's policy, and checks it with `!new-workshop`.
- **A new image** in a region already set up: only §6.1's copy and its tag, with the new image as the source; the group exists already. `!new-workshop` uses the region's newest image tagged `zab-scope=$SCOPE`, and shows it in its plan; an existing workshop keeps the image its entry names. Once no workshop needs the older image, remove its tag: `aws ec2 delete-tags --profile "$P" --region "$R" --resources <older-ami> --tags Key=zab-scope`.

### 6.1 The fleet's security group and image, per region

Run this section once per region, with `R` set to it. These are the fleet's, never the server's: the server's own group `zab-admin` (§3.1) never carries `zab-scope`.

**The security group `zab-fleet`,** SSH only: exactly TCP 22, from `0.0.0.0/0` and `::/0`, as `launch.py` requires (`validate_security_group`: no other inbound rule, and never the VPC's default group):

```sh
R=<region>                  # the region to set up: $WR at the bootstrap
VPCW=$(aws ec2 describe-vpcs --profile "$P" --region "$R" \
  --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
SGW=$(aws ec2 create-security-group --profile "$P" --region "$R" --group-name zab-fleet \
  --description "zab workshop VMs: SSH only" --vpc-id "$VPCW" \
  --tag-specifications "ResourceType=security-group,Tags=[{Key=zab-scope,Value=$SCOPE},{Key=Name,Value=zab-fleet}]" \
  --query GroupId --output text)
aws ec2 authorize-security-group-ingress --profile "$P" --region "$R" --group-id "$SGW" \
  --ip-permissions 'IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=0.0.0.0/0}],Ipv6Ranges=[{CidrIpv6=::/0}]' \
  --query Return --output text                     # True
aws ec2 describe-security-groups --profile "$P" --region "$R" --group-ids "$SGW" \
  --query 'SecurityGroups[0].IpPermissions[].[IpProtocol,FromPort,ToPort,join(`","`,IpRanges[].CidrIp || `[]`),join(`","`,Ipv6Ranges[].CidrIpv6 || `[]`),join(`","`,UserIdGroupPairs[].GroupId || `[]`),join(`","`,PrefixListIds[].PrefixListId || `[]`)]' --output text
echo "$R $SGW"
```

The read-back has §3.1's columns: protocol, ports, IPv4 ranges, IPv6 ranges, source groups, prefix lists. It must print exactly one line, `tcp 22 22 0.0.0.0/0 ::/0`, with the last two columns empty.

**The image,** a dated copy of the workshop image, `zab-fleet-YYYYMMDD`, tagged with the scope at its creation, the image and its snapshots. Its name is the source's where that is a `zab-fleet-…` copy, so that one image has one name in every region, and otherwise the day of the copy. Its source is one of two, each with its own block:

- **Your first region, at the bootstrap:** the image you built ([`workshop/ami/README.md`](../workshop/ami/README.md)), which must be this account's. No region has a scoped image yet, so give its ID and region by hand:

  ```sh
  SRC_REGION=<the built image's region>; SRC=<its ami-…>
  aws ec2 describe-images --profile "$P" --region "$SRC_REGION" --image-ids "$SRC" \
    --query 'Images[0].[ImageId,Name,State]' --output text       # ami-…  <the name build_ami.py gave it>  available
  ```
- **A region set up already,** for every later region: its newest image tagged `zab-scope=$SCOPE`, as `!new-workshop` reads it:

  ```sh
  SRC_REGION=<a region set up already>
  SRC=$(aws ec2 describe-images --profile "$P" --region "$SRC_REGION" --owners self \
    --filters "Name=tag:zab-scope,Values=$SCOPE" Name=state,Values=available \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
  aws ec2 describe-images --profile "$P" --region "$SRC_REGION" --image-ids "$SRC" \
    --query 'Images[0].[ImageId,Name,State]' --output text       # ami-…  zab-fleet-…  available
  ```

Either read-back prints one line: the source's ID, name and `available`. An error, or `None` from the lookup, means no source there: check the region, or stop and set it up first.

Then copy it into the region. Run the copy once: a second run fails with `InvalidAMIName.Duplicate`. After that error, the copy exists already: go on with the next block, which finds it by name, waits for it and tags it. The copy can take longer than the waiter's 10 minutes, after which the waiter gives up (`Waiter ImageAvailable failed: Max attempts exceeded`): run only the `wait` line again, until it returns.

```sh
NAMEW=zab-fleet-<YYYYMMDD>  # the source's name, or the day of the copy: a name no image of the region has
AMIW=$(aws ec2 copy-image --profile "$P" --region "$R" --source-region "$SRC_REGION" \
  --source-image-id "$SRC" --name "$NAMEW" \
  --tag-specifications "ResourceType=image,Tags=[{Key=zab-scope,Value=$SCOPE}]" \
                       "ResourceType=snapshot,Tags=[{Key=zab-scope,Value=$SCOPE}]" \
  --query ImageId --output text)
aws ec2 wait image-available --profile "$P" --region "$R" --image-ids "$AMIW"
echo "$R $AMIW"
```

**If you copied it without `--tag-specifications`,** or lost the shell: find the copy by its name, wait until it is `available` (until then its snapshots may not be listed yet), and tag it and its snapshots. Tagging again changes nothing:

```sh
R=<region>; NAMEW=<its zab-fleet-YYYYMMDD>   # again, if you lost the shell
AMIW=$(aws ec2 describe-images --profile "$P" --region "$R" --owners self \
  --filters "Name=name,Values=$NAMEW" --query 'Images[0].ImageId' --output text)
aws ec2 wait image-available --profile "$P" --region "$R" --image-ids "$AMIW"
SNAPS=$(aws ec2 describe-images --profile "$P" --region "$R" --image-ids "$AMIW" \
  --query 'Images[0].BlockDeviceMappings[].Ebs.SnapshotId' --output text)
aws ec2 create-tags --profile "$P" --region "$R" --resources "$AMIW" $SNAPS --tags "Key=zab-scope,Value=$SCOPE"
echo "$R $AMIW $SNAPS"
```

**Check the region,** once both exist, as `!new-workshop` reads it (ADMIN §2.9): exactly one group in the scope, the newest image in the scope, and the image's snapshots tagged. The first command prints exactly one line; the second, one line per scoped image of the region (one, until a newer copy), the newest last, which is the one `!new-workshop` takes; the last, one line per snapshot of the image (one), each ending in `$SCOPE`'s value. A snapshot line ending in `None` is untagged: tag it with `aws ec2 create-tags --profile "$P" --region "$R" --resources $SNAPS --tags "Key=zab-scope,Value=$SCOPE"`, then run the last command again.

```sh
aws ec2 describe-security-groups --profile "$P" --region "$R" --filters "Name=tag:zab-scope,Values=$SCOPE" \
  --query 'SecurityGroups[].[GroupId,GroupName]' --output text        # sg-…  zab-fleet
aws ec2 describe-images --profile "$P" --region "$R" --owners self --filters "Name=tag:zab-scope,Values=$SCOPE" \
  --query 'sort_by(Images,&CreationDate)[].[ImageId,Name,State]' --output text   # ami-…  zab-fleet-…  available
SNAPS=$(aws ec2 describe-images --profile "$P" --region "$R" --owners self \
  --filters "Name=name,Values=$NAMEW" --query 'Images[0].BlockDeviceMappings[].Ebs.SnapshotId' --output text)
aws ec2 describe-snapshots --profile "$P" --region "$R" --snapshot-ids $SNAPS \
  --query "Snapshots[].[SnapshotId,Tags[?Key=='zab-scope']|[0].Value]" --output text   # snap-…  <SCOPE>
```

### 6.2 The first workshop's image and group: their IDs

`admin.toml`'s entry names its region's image and group by ID (§7.2). Read them as `!new-workshop` would: the one group named `zab-fleet` in the scope, and the newest scoped image:

```sh
aws ec2 describe-security-groups --profile "$P" --region "$WR" \
  --filters "Name=tag:zab-scope,Values=$SCOPE" Name=group-name,Values=zab-fleet \
  --query 'SecurityGroups[].GroupId' --output text                          # one sg-…: §7.2's <SG_ID>
aws ec2 describe-images --profile "$P" --region "$WR" --owners self \
  --filters "Name=tag:zab-scope,Values=$SCOPE" Name=state,Values=available \
  --query 'sort_by(Images,&CreationDate)[-1].[ImageId,Name]' --output text  # ami-…  zab-fleet-…: §7.2's <AMI_ID>
```

Each prints one line. Anything else means §6.1 isn't done in `$WR`.

### 6.3 No key pair of the server's yet

The bootstrap imports `zab-admin-<WS>` from the server's new fleet key, and refuses a pair of that name with another key (`import_key_pair.py`). So it may not exist yet:

```sh
aws ec2 describe-key-pairs --profile "$P" --region "$WR" \
  --filters "Name=key-name,Values=zab-admin-$WS" --query 'KeyPairs[].KeyName'
# []
```

A name of an earlier server's: §13.1, "A new server, after a bootstrapped one".

### 6.4 Quotas in each region

Not part of the bootstrap, but an increase takes days. Each running `m7a.2xlarge` counts 8 vCPUs (ORGANIZERS §1.1 item 6), and every VM holds an Elastic IP, running or stopped ([`workshop/aws/README.md`](../workshop/aws/README.md), "The VMs"). Count, per region, every workshop's VMs and spares, and the VMs kept for a month after their workshop (ORGANIZERS §5.7), which overlap the next workshop's:
- **vCPUs:** 8 per VM that may run at once;
- **Elastic IPs:** one per VM.

Read the region's two quotas, and request an increase (*Service Quotas* in the console) where they don't cover the count:

```sh
aws service-quotas get-service-quota --profile "$P" --region "$WR" --service-code ec2 \
  --quota-code L-1216C47A --query Quota.Value     # running On-Demand standard vCPUs
aws service-quotas get-service-quota --profile "$P" --region "$WR" --service-code ec2 \
  --quota-code L-0263D0A3 --query Quota.Value     # Elastic IPs
```

### 6.5 A new region, end to end

For a later workshop in a region the server doesn't serve yet. `!new-workshop` refuses such a region at once: ``region=<R> is not set up for the fleet: [global] regions are … (BOOTSTRAP §6 sets a region up; a deployer adds it)``. A region takes five steps, in this order: its AWS resources, the region in `admin.toml`, the role's policy, that `admin.toml` on the server, and a check through `!new-workshop`. `!new-workshop` never makes the `[global]` change itself (ADMIN §2.9).

**Who.** The account owner, with `$P`, for steps 1 and 3; a deployer for steps 2 and 4, on the laptop and over SSH (refresh the SSH rule first, §3.1); a global admin for step 5, in `#zab-admins`.

**When.** While no plan is pending or applying on any of the server's bots: each workshop's `!plans` answers `No plan is pending on this bot.`, and no progress message is waiting for its result. Step 4 restarts the bot, which cuts an apply short.

**The release the server runs,** whose loader and renderer the steps use, from its worktree (§9). Set `R` too, in the same shell:

```sh
R=<the new region>
V=$(ssh "$HOST" readlink /opt/zab-admin/current); V=${V##*/}; echo "$V"   # vX.Y.Z
git -C ~/zab tag -v "$V" 2>&1 | grep Good                                # Good "git" signature for …
[ -d ~/zab-"$V" ] || git -C ~/zab worktree add --detach ~/zab-"$V" "$V"
```

1. **The region's AWS resources,** by the account owner: §6.1 in the new region, with `R` as set above, the group `zab-fleet`, the image copied from a region set up already, and the check at its end. Then §6.4's quotas, with `$R` for `$WR`.
2. **`admin.toml`: the server's, plus the region,** on the laptop. Start from the server's file, not your copy: `!new-workshop` adds its entries on the server alone, and a copy installed without them would remove those workshops.

   ```sh
   scp "$HOST:/etc/zab-admin/admin.toml" ~/zab-admin/etc/admin.toml.server
   diff -u ~/zab-admin/etc/admin.toml ~/zab-admin/etc/admin.toml.server && echo same
   ```

   It prints `same`, or a diff whose changed lines are all `+`: the `[[workshop]]` entries `!new-workshop` added since your copy was made. Then take the server's, and add the region to `[global] regions`, keeping the others, as `regions = ["<a region>", "<the new region>"]`:

   ```sh
   cp ~/zab-admin/etc/admin.toml.server ~/zab-admin/etc/admin.toml
   ${EDITOR:-nano} ~/zab-admin/etc/admin.toml
   diff -u ~/zab-admin/etc/admin.toml.server ~/zab-admin/etc/admin.toml   # only the regions line: - old, + new
   cd ~/zab-"$V" && uv run python -m zab.admin.config --config ~/zab-admin/etc/admin.toml check && echo ok
   ```

   A `-` line in the first diff, or a changed value, is a change made on one side only: find out which is right before you go on. The last command prints `ok`; a refusal names the key and the rule.
3. **The role's policy,** by the account owner (ADMIN §5; the adminserver README, "The instance role's policies"). Render it twice, from the server's release, into new directories: from the server's file of step 2, which is what the policies in force were rendered from, and from your edited one. Compare the two. Nothing here reads `~/zab-admin/policies`, which a second machine (§15) doesn't have and either machine may hold an older render in:

   ```sh
   OLDP=~/zab-admin/policies.inforce; NEWP=~/zab-admin/policies.new; rm -rf "$OLDP" "$NEWP"
   cd ~/zab-"$V" && python3 -I workshop/adminserver/render_policy.py \
     --config ~/zab-admin/etc/admin.toml.server --out-dir "$OLDP" >/dev/null &&
     python3 -I workshop/adminserver/render_policy.py --config ~/zab-admin/etc/admin.toml --out-dir "$NEWP"
   diff -r "$OLDP" "$NEWP"
   ```

   The second render prints `…/zab-admin-N.json: <n> characters`, one line per file. The diff shows:
   - the new region added to the `Read` statement's `aws:RequestedRegion`;
   - the new region's statements (`<Region>Launch`, `…LaunchVolume`, `…LaunchFrom`, `…Create`, `…TagAtCreation`, `…Retag`, `…Fleet`, `…Volume`), their resources `arn:aws:ec2:<R>:*:…`;
   - often one more file: a managed policy holds a little under two regions (`MANAGED_LIMIT`, 6,144 characters), so a second region already needs `zab-admin-2.json`, which the diff lists as `Only in …`, and statements move between the files.

   Nothing else: no account number, no workshop's name, no action outside `ec2:`. Read every file with §10's listing, on `"$NEWP"/zab-admin-*.json` instead of `~/zab-admin/policies/zab-admin-*.json`, against §10's checklist, with the new region among `[global] regions`.

   Then apply it in an order that never leaves the role with less than either render grants. Every file carries every deny (§10), so the union of the two renders is safe, and the apply goes through it:
   1. **The new render, as temporary copies,** `zab-admin-next-N`, each created (or, left by an earlier run, updated to its file) and attached to the role. From here the role holds both renders, whatever happens next: a statement that moves from one file to another is never missing.
   2. **The policies themselves,** `zab-admin-N`: each created and attached if new, or given its file as a new default version. IAM keeps five versions of a managed policy, so where five exist the oldest that isn't the default is deleted first.
   3. **The policies the new render lacks** (a region or an instance type removed: §10, "Later"), detached and deleted, their other versions first, as IAM requires. That list comes from IAM's list of the role's policies, so a policy in force is never left attached, granting what it did, whatever this machine holds.

   Any failure stops the block where it is, and nothing after it runs: a failed replacement never lets a removal follow. Running the block again is safe, and goes on from the start. The role holds up to ten managed policies by default (the adminserver README, "The instance role's policies"), and the temporary copies count until the last block below removes them. The block prints only names and version IDs, never an ARN, which holds the account ID:

   ```sh
   upd() {   # policy $1 holds file $2, as its default version, and is attached to the role
     local arn old v
     arn=$(aws iam list-policies --profile "$P" --scope Local \
       --query "Policies[?PolicyName=='$1'].Arn" --output text) || return 1
     if [ -z "$arn" ]; then
       arn=$(aws iam create-policy --profile "$P" --policy-name "$1" --policy-document "file://$2" \
         --query Policy.Arn --output text) || return 1
       echo "$1: created"
     else
       if [ "$(aws iam list-policy-versions --profile "$P" --policy-arn "$arn" \
           --query 'length(Versions)' --output text)" -ge 5 ]; then
         old=$(aws iam list-policy-versions --profile "$P" --policy-arn "$arn" \
           --query 'sort_by(Versions[?!IsDefaultVersion],&CreateDate)[0].VersionId' --output text) &&
           aws iam delete-policy-version --profile "$P" --policy-arn "$arn" --version-id "$old" || return 1
         echo "$1: deleted $old"
       fi
       v=$(aws iam create-policy-version --profile "$P" --policy-arn "$arn" --policy-document "file://$2" \
         --set-as-default --query PolicyVersion.VersionId --output text) || return 1
       echo "$1: $v, now the default"
     fi
     aws iam attach-role-policy --profile "$P" --role-name zab-admin --policy-arn "$arn"
   }
   ok=1
   for f in "$NEWP"/zab-admin-*.json; do                 # 1. the new render, as temporary copies
     n=$(basename "$f" .json); upd "zab-admin-next-${n#zab-admin-}" "$f" || { ok=0; break; }
   done
   if [ "$ok" = 1 ]; then                                 # 2. the policies themselves
     for f in "$NEWP"/zab-admin-*.json; do upd "$(basename "$f" .json)" "$f" || { ok=0; break; }; done
   fi
   if [ "$ok" = 1 ]; then                                 # 3. those the new render lacks
     for n in $(aws iam list-attached-role-policies --profile "$P" --role-name zab-admin \
         --query 'AttachedPolicies[].PolicyName' --output text); do
       case $n in zab-admin-[0-9]*) ;; *) continue ;; esac
       [ ! -e "$NEWP/$n.json" ] || continue
       arn=$(aws iam list-policies --profile "$P" --scope Local \
         --query "Policies[?PolicyName=='$n'].Arn" --output text) &&
         aws iam detach-role-policy --profile "$P" --role-name zab-admin --policy-arn "$arn" || { ok=0; break; }
       for old in $(aws iam list-policy-versions --profile "$P" --policy-arn "$arn" \
           --query 'Versions[?!IsDefaultVersion].VersionId' --output text); do
         aws iam delete-policy-version --profile "$P" --policy-arn "$arn" --version-id "$old"
       done
       aws iam delete-policy --profile "$P" --policy-arn "$arn" && echo "$n: detached and deleted"
     done
   fi
   [ "$ok" = 1 ] && echo applied || echo "stopped: nothing after the failed step ran; run the block again"
   aws iam list-attached-role-policies --profile "$P" --role-name zab-admin \
     --query 'AttachedPolicies[].PolicyName' --output text
   ```

   It prints, for each file, `zab-admin-next-1: created` (or `…: v2, now the default` when an earlier run left it), then `zab-admin-1: v2, now the default` for a policy that exists (after `zab-admin-1: deleted v1` where five versions existed) and `zab-admin-2: created` for a new one, then, only for a render with fewer files, `zab-admin-3: detached and deleted`, then `applied`. The last command lists the new render's names, `zab-admin-1` … `zab-admin-N`, and their temporary copies, `zab-admin-next-1` … `zab-admin-next-N`. Then read each policy in force back, and compare it with its file, a temporary copy with the file it copies:

   ```sh
   for n in $(aws iam list-attached-role-policies --profile "$P" --role-name zab-admin \
       --query 'AttachedPolicies[].PolicyName' --output text); do
     A=$(aws iam list-policies --profile "$P" --scope Local --query "Policies[?PolicyName=='$n'].Arn" --output text)
     D=$(aws iam get-policy --profile "$P" --policy-arn "$A" --query Policy.DefaultVersionId --output text)
     aws iam get-policy-version --profile "$P" --policy-arn "$A" --version-id "$D" \
       --query PolicyVersion.Document --output json | python3 -I -c 'import json, os, sys; f = sys.argv[1]
   print(sys.argv[2], "same" if os.path.exists(f) and json.load(sys.stdin) == json.load(open(f)) else "DIFFERENT")' \
       "$NEWP/zab-admin-${n##*-}.json" "$n"
   done
   ```

   One line per policy, each `same`. `DIFFERENT` means the policy in force is not its file: run the apply block again. A `zab-admin-next-N` left by an interrupted earlier run whose render had more files than this one has no file here, so it reads `DIFFERENT` whatever you apply until the removal below deletes it. Once every other line says `same`, remove the temporary copies, the same way as the policies a render lacks:

   ```sh
   for n in $(aws iam list-attached-role-policies --profile "$P" --role-name zab-admin \
       --query 'AttachedPolicies[].PolicyName' --output text); do
     case $n in zab-admin-next-*) ;; *) continue ;; esac
     arn=$(aws iam list-policies --profile "$P" --scope Local \
       --query "Policies[?PolicyName=='$n'].Arn" --output text) &&
       aws iam detach-role-policy --profile "$P" --role-name zab-admin --policy-arn "$arn" || continue
     for old in $(aws iam list-policy-versions --profile "$P" --policy-arn "$arn" \
         --query 'Versions[?!IsDefaultVersion].VersionId' --output text); do
       aws iam delete-policy-version --profile "$P" --policy-arn "$arn" --version-id "$old"
     done
     aws iam delete-policy --profile "$P" --policy-arn "$arn" && echo "$n: detached and deleted"
   done
   aws iam list-attached-role-policies --profile "$P" --role-name zab-admin \
     --query 'AttachedPolicies[].PolicyName' --output text
   ```

   It prints `zab-admin-next-N: detached and deleted` for each copy, then exactly the new render's names. Then keep the new render as the one in force on this machine:

   ```sh
   rm -rf ~/zab-admin/policies && mv "$NEWP" ~/zab-admin/policies && rm -r "$OLDP"
   ```
4. **`admin.toml` on the server,** by a deployer, as root's 0644 file, after the server's own loader has checked it. Stage it first, and compare it with the server's: the one difference is the regions line. Anything more is an entry `!new-workshop` added since step 2: remove the staged file (`ssh "$HOST" rm -r "$T"`), and redo step 2.

   ```sh
   T=$(ssh "$HOST" mktemp -d) && scp ~/zab-admin/etc/admin.toml "$HOST:$T/" && echo "$T"
   ssh "$HOST" "diff -u /etc/zab-admin/admin.toml $T/admin.toml"   # only the regions line
   ```

   Then check it with the server's loader, install it, and restart the bot. The bot reads `admin.toml` only when it starts; the starter and the collector restart themselves when the file changes, and the console reads it again at each request (ADMIN §1.2), so the bot is the one unit to restart. The command asks for nothing, but `-t` lets `sudo` ask for a password where one is set:

   ```sh
   ssh -t "$HOST" "/opt/zab-admin/current/.venv/bin/python -E -s -m zab.admin.config --config $T/admin.toml check &&
     sudo install -o root -g root -m 0644 $T/admin.toml /etc/zab-admin/admin.toml && rm -r $T &&
     sudo systemctl restart zab-admin-bot.service &&
     sudo -u zab-admin /opt/zab-admin/current/.venv/bin/python -I -m zab.admin bot --check"
   ```

   It ends `bot check: OK (<n> bots)`, the global bot and one per workshop. Your copy, `~/zab-admin/etc/admin.toml`, is now the server's, so the next deploy shows no `admin.toml` difference.
5. **The check,** by a global admin in `#zab-admins`: a `!new-workshop` plan for the new region, never approved. Any name no workshop has, any workshop of the server as `like=`, your own user ID as the organizer:

   ```
   !new-workshop region-check region=<the new region> like=<a workshop of the server> organizers=<your user ID>
   ```

   The plan's `Prerequisites:` read:

   ```text
     ok: security group sg-… (zab-fleet), tagged zab-scope=<SCOPE>
     ok: image ami-… (zab-fleet-…, made …), tagged zab-scope=<SCOPE>
     ok: the role may launch m7a.2xlarge from them in <the new region> (DryRun)
     ok: the role may import the key pair zab-admin-region-check (DryRun)
   ```

   then an `unchecked:` line about the channel names. Reject it with the `!reject` line its approval message gives. A `MISSING:` line refuses the plan instead, and names what is missing: §6.1's group or image (step 1), or the role's permission in the region (step 3).

## 7. `admin.toml`

### 7.1 Your choices in it

- **`admins`**, the global admins (ADMIN §2.1): you and the lead organizers, by user ID (§5.5). Every bot obeys them. A global admin's approval alone completes a destructive or server plan (ADMIN §2.5, D36):
  - `!new-workshop` is a global admin's (ADMIN §2.5, the server class);
  - with `organizers = []`, the first workshop's plans are the global admins' alone, each of whom approves a destructive one alone (`destructive_approvals = 2` binds two organizers who are not global admins; the loader refuses a workshop with neither a global admin nor that many approvers).
- **`deployers`**: you, and any other deployer: they deploy (§11) and get the encrypted backups by DM (§8.2).
- **`instance_types`**: `m7a.2xlarge` (`workshop/aws/README.md`, "The VMs"). The first is the default, and the role allows only these (ADMIN §5).
- **One workshop,** the first: `!new-workshop` adds the others (§7.3). Its `organizers` are its own organizers' user IDs, who act through its bot only; the global admins act through every bot, so they aren't listed.

### 7.2 The template

Write this to `~/zab-admin/etc/admin.toml`, and replace every `<…>` with §0's values and what §5.6 and §6.2 printed. Each field the loader (`src/zab/admin/config.py`) reads is here, so nothing falls back to a default. `tests/workshop/test_bootstrap_doc.py` loads this very block with the strict loader, filled with sample values, and renders its policy, so it can't drift from the loader. The real file is never committed (ADMIN §2.1).

<!-- tested: tests/workshop/test_bootstrap_doc.py loads this block -->
```toml
# ~/zab-admin/etc/admin.toml: the server's (docs/BOOTSTRAP.md §7). Never commit it.
provider = "aws"
admins = [<YOUR_USER_ID>, <ORGANIZER_USER_ID>]  # global admins (ADMIN §2.1): every bot obeys them
deployers = [<YOUR_USER_ID>]                 # SSH and deploy; get the backups by DM (ADMIN §1.7)
destructive_approvals = 2                    # ADMIN §2.5
plan_ttl_minutes = 10
release = "final"                            # signed final tags only: vX.Y.Z, never an rc
console_url = "https://<HOST>"               # the console (ADMIN §4); its host goes into the Caddyfile

[global]                                     # the fleet's scope (ADMIN §5); `Workshop admin`
scope = "<SCOPE>"                            # every fleet resource carries zab-scope=<SCOPE>
regions = ["<WR>"]                           # each set up once: its zab-fleet image and group
instance_types = ["m7a.2xlarge"]             # the role launches only these
tags = []                                    # no other scope tag (the test server: zab-role=test)
zuliprc = "/etc/zab-admin/secrets/global.zuliprc"
user_id = <GLOBAL_BOT_USER_ID>
email = "<GLOBAL_BOT_EMAIL>"
channels = [<ZAB_ADMINS_CHANNEL_ID>]

[[workshop]]
name = "<WS>"                                # the first workshop; !new-workshop adds the others
github_org = "<GITHUB_ORG>"                  # its participants' repositories (workshop/admin/README.md §1b)
refserve_url = "<FILE_SERVER_URL>"           # the central file server, which serves github_org (workshop/files/README.md)
roster = "/var/lib/zab-admin/ws/<WS>/roster.csv"
out_dir = "/var/lib/zab-admin/ws/<WS>/out"
organizers = []                              # its own organizers' user IDs (§7.1)
mention_after_s = 120                        # a turn this long or longer mentions its owner (ADMIN §2.1)
alerts_channel = <WS_ALERTS_CHANNEL_ID>      # #<WS>-fleet-alerts
channel = <WS_PARTICIPANTS_CHANNEL_ID>       # its participants' channel (§5.4); optional: or delete
# "Start here"'s first step, about gift plans, in its own words (ADMIN §2.1); optional: or delete
gifts_step = "Accept any gift subscriptions: Claude Max (from Anthropic) and ChatGPT Pro (from OpenAI)."
spare_idle_stop = 10                         # a spare's idle stop, in minutes (ORGANIZERS §3.7)
[workshop.bot]                               # `Workshop admin · <WS>`
zuliprc = "/etc/zab-admin/secrets/bots/<WS>.zuliprc"
user_id = <WS_BOT_USER_ID>
email = "<WS_BOT_EMAIL>"
channels = [<WS_ORGANIZERS_CHANNEL_ID>]      # #<WS>-organizers
[workshop.aws]
region = "<WR>"
ami = "<AMI_ID>"                             # the region's image and group (§6.2)
security_group = "<SG_ID>"
instance_types = ["m7a.2xlarge"]
key_name = "zab-admin-<WS>"                  # imported by the server at the bootstrap
admin_keys = ["/etc/zab-admin/break-glass/zab-deployer.pub"]

# Later workshops are added by !new-workshop: docs/BOOTSTRAP.md §7.3.
```

### 7.3 New workshops, later: `!new-workshop`

Not at the bootstrap. After it, every new workshop is added in `#zab-admins` by a global admin's `!new-workshop` on `Workshop admin` (ADMIN §2.9, [`docs/NEW-WORKSHOP.md`](NEW-WORKSHOP.md)), with `like=<WS>` the first time:
- **Its plan** shows the new entry, copied from the `like=` workshop's, with the region's `zab-scope=<SCOPE>` image (the newest) and group (§6.1), and the organizers by user ID.
- **Its approval** takes a global admin (the requester may), and the phrase `new-workshop <name>` (ADMIN §2.5).
- **Its apply** creates the bot, both channels, the workshop's directories and its key pair, then installs the entry and restarts the bots, through the root helper.
- **Before it:** its region set up (§6.1) and among `[global] regions`, with the role's policy rendered for it: a new region is §6.5. A region that is set up needs nothing more in AWS.
- **After it:** the organizers' runbook, `docs/ORGANIZERS.md`, from the roster on (`!roster sync`, or `!roster import`), entirely on Zulip.

### 7.4 Check it

With `main`'s loader now; §9 checks again with the release's, which is the one that counts:

```sh
grep -n '<[A-Z_]*>' ~/zab-admin/etc/admin.toml        # prints nothing: every placeholder filled
cd ~/zab-main && uv run python -m zab.admin.config --config ~/zab-admin/etc/admin.toml check && echo ok
uv run python -m zab.admin.config --config ~/zab-admin/etc/admin.toml zuliprcs
```

`check` prints nothing, then `ok`. A refusal names the key and the rule. `zuliprcs` prints the two admin bots' key paths on the server, which §8.1's files mirror.

## 8. The secrets and the trust files

### 8.1 The secrets directory

`deploy.sh --bootstrap --secrets ~/zab-admin/secrets` installs these, and only these, as `root:zab-admin 0640` under `/etc/zab-admin/secrets/`: `provisioner.zuliprc`, `github-token`, and every zuliprc `admin.toml` names, at its path under `secrets/` (the adminserver README, "Deploying"; `install_secrets`). A missing one stops the bootstrap; an extra one is ignored, with a note.

**The GitHub token** (ADMIN §5; `workshop/admin/README.md` §1b), the server's own:
- resource owner: the workshops' GitHub organization, `github_org` in §7.2;
- **All repositories**;
- repository permissions **Administration: Read and write** and **Contents: Read and write** (Metadata read-only comes with them);
- an expiration after the last month any VM of the workshops it serves is kept (ORGANIZERS §5.7).

Make it on github.com, as an owner of that organization: *Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token*, with the settings above. If the organization requires approval of fine-grained tokens, approve it under the organization's *Settings → Personal access tokens → Pending requests*. Administration write can delete any repository of the organization (ADMIN §5).

Save it without echoing it or keeping it in your history. **Run this line on its own, never inside a larger paste:** its `read` takes the next line the terminal gets, so a paste that goes on answers it with that line, and saves it instead of the token. At `token:`, paste the token alone and press Enter. Nothing echoes:

```sh
(umask 077; printf 'token: '; read -rs T; echo; printf '%s\n' "$T" > ~/zab-admin/secrets/github-token; unset T)
```

**Check the file,** without printing the token:

```sh
ls -l ~/zab-admin/secrets/github-token                   # -rw------- , one line
wc -c < ~/zab-admin/secrets/github-token                 # 94 (macOS pads it with spaces)
head -c 11 ~/zab-admin/secrets/github-token; echo        # github_pat_
```

A fine-grained token is 93 characters, so the file is 94 bytes with its newline. Any other size, such as `1` (an empty line), or another prefix means the save got no token, or not this one: run the save line again, by itself.

**Renewing it** before it expires is §16, which replaces one secret on the running server.

Then check the directory:

```sh
chmod -R go-rwx ~/zab-admin/secrets
cd ~/zab-admin/secrets && find . -type f | sort
```

<!-- tested: tests/workshop/test_bootstrap_doc.py compares this listing with what deploy.sh installs -->
```text
./bots/<WS>.zuliprc
./github-token
./global.zuliprc
./provisioner.zuliprc
```

### 8.2 The backup recipients (ADMIN §1.7)

After every change, each deployer gets `backup-<time>.tar.gz.age` by DM, encrypted to these keys. One `ssh-ed25519` public key per line, of a plain key file: yours is the dedicated key (§2.1). Check now that you can decrypt. The second line's `age -d` asks for the key's passphrase, so run it on its own, after the first:

```sh
cut -d' ' -f1,2 ~/.ssh/zab-deployer.pub > ~/zab-admin/etc/backup_recipients
echo decrypted | age -R ~/zab-admin/etc/backup_recipients | age -d -i ~/.ssh/zab-deployer   # decrypted
```

Each other deployer's key is a line of its own. Old backups are never deleted. A key removed from this file later, or suspected, therefore means rotating the participant bot keys (ADMIN §1.7).

### 8.3 The break-glass key

The dedicated key's public half. Every VM launched through the server gets it for `ubuntu` at first boot (`admin_keys`), so you reach the fleet without the server (ADMIN §1.7):

```sh
cp ~/.ssh/zab-deployer.pub ~/zab-admin/etc/break-glass/zab-deployer.pub
ls ~/zab-admin/etc ~/zab-admin/etc/break-glass
# admin.toml  allowed_signers  backup_recipients  break-glass      zab-deployer.pub
```

If an earlier key's file is there, under whatever name you gave it, remove it, such as `rm ~/zab-admin/etc/break-glass/<old>.pub`: the bootstrap installs every `.pub` of that directory on the server (`deploy.sh`'s `install_etc`).

## 9. The release: review and sign it

**`V` is the release you deploy:** a final `vX.Y.Z`, normally the newest, and `C` its commit on `main`, whose `pyproject.toml` says `version = "X.Y.Z"`. The steps from here rely on a final tag signed by a key of your `allowed_signers` (§2.2):
- `release = "final"` admits only `vX.Y.Z`; `deploy.sh` refuses an rc in production, and an unsigned or lightweight tag anywhere (ADMIN §1.6). Don't set `release = "rc"` to start earlier: that pattern is the gate.
- `deploy.sh` refuses a tag whose `pyproject.toml` names another version, so the commit must say `version = "X.Y.Z"`.
- It is the server's first release, so nothing earlier can be the review's base: you review every release path whole, from the empty tree, and `deploy.sh` runs without `--since`.

**Whose tag.** That depends on `ZAB_REPO`, the repository you deploy from (§1):
- **The public zab repository.** Each release there is one commit on `main`, with a tag signed by the maintainer's key, which [SECURITY.md, *Verifying a release*](../SECURITY.md#verifying-a-release) publishes, with its fingerprint, its `allowed_signers` line and the commands to check a tag. `deploy.sh` runs only a tag signed by a key of your `allowed_signers` (§2.2), and the maintainer's is one only if you add that line, which you do only if you take the maintainer's review of each release as yours (ADMIN §1.6). Otherwise you review that commit, then sign a tag of your own, of the same name, on the same commit, in your clone alone. You never push it. `deploy.sh` sends the server the tag from your clone, never from GitHub, so nothing else changes. `C` is the commit the public tag names: once the block below has fetched it, `git -C ~/zab rev-parse "$V^{commit}"` prints it.
- **A repository whose releases you tag yourself,** such as your own fork or mirror: `C` is the release's commit on its `main`, untagged until you sign it, and you push your tag there.

```sh
V=<release>                     # vX.Y.Z, final
git -C ~/zab fetch origin --tags
C=<release-commit>              # its commit on main: the public tag's commit, or your release's
git -C ~/zab merge-base --is-ancestor "$C" origin/main && echo on main
git -C ~/zab show "$C:pyproject.toml" | grep '^version'        # version = "X.Y.Z", $V without its v
git -C ~/zab worktree add --detach ~/zab-"$V" "$C"
less ~/zab-"$V"/workshop/adminserver/deploy.sh                 # read it whole before you run it
~/zab-"$V"/workshop/adminserver/deploy.sh --review "$C" --etc ~/zab-admin/etc --out ~/zab-admin
```

Once you have signed a tag of the public repository, every `git fetch origin --tags` prints a `[rejected]` line for it, `vX.Y.Z -> vX.Y.Z (would clobber existing tag)`, keeps yours, and exits 1: that is expected, and the fetch still brings everything else, but never chain it with `&&`. Never fetch with `--force`, which would put the public tag back over yours.

`--review` pages the diff of every release path. It first shows one line per file, `P` for each privileged one; then the privileged files' diff (with `uv.lock` and `build-constraints.txt` as their changed packages and pins, never their hashes), every file that became privileged since the base (whole, once), and the rest. It writes that review beside the diff (`….review`), and ends with:

```text
The reviewed diff, the empty tree..<first 12 of C>, is ~/zab-admin/zab-admin-empty..<first 12 of C>.diff
  SHA-256 <hex>; release tree <hex> (<n> paths of workshop/adminserver/release-paths)
Sign it once reviewed, then verify and push it:
  git -C ~/zab-vX.Y.Z tag -s vX.Y.Z -m "zab vX.Y.Z" <C>
  git -C ~/zab-vX.Y.Z tag -v vX.Y.Z
  git -C ~/zab-vX.Y.Z push origin vX.Y.Z
```

The last four lines are the commands to paste, in any directory: they name the clone `deploy.sh` read, as an absolute path (here the worktree, which shares `~/zab`'s tags and `origin`), the version of C's own `pyproject.toml` and all of C. Where C's `pyproject.toml` has no single `version = "X.Y.Z"` line, the review prints `No tag command:` and the reason instead.

**If the clone has the tag already,** the review says `No tag command: … already has a tag vX.Y.Z` instead. `git -C ~/zab tag -v "$V"` says whose it is:
- **`Good "git" signature with ED25519 key SHA256:gTGOWot/…`, then `No principal matched.`, with SECURITY.md's fingerprint:** the public repository's tag, which you fetched, signed by the maintainer's key, which your `allowed_signers` doesn't list. Sign your own on the same commit, below.
- **`Good "git" signature for <email>`, a line of your `allowed_signers`:** yours, from an earlier run, or the maintainer's, with SECURITY.md's address, if you added SECURITY.md's line. Go on to the check after the signing.
- **`Good "git" signature with ED25519 key SHA256:…`, then `No principal matched.`, with another fingerprint:** a valid signature by a key outside your `allowed_signers`: another signer's. It starts `Good` too, but names no `<email>`, and `tag -v` exits 1, as `deploy.sh`'s check refuses it. `deploy.sh` accepts another signer's tag only if that key is a line of your `allowed_signers` (§2.2). Add their line only if you take their review of the release as yours (ADMIN §1.6). Otherwise sign your own on the same commit, as for the public repository's tag.
- **`error: no signature found`:** a tag with no signature, which `deploy.sh` refuses (`not signed with an SSH key`): not the public repository's, whose tags are signed. A failed `tag -s` can leave one. When the agent doesn't hold the key, ssh-keygen falls back to the key file that §2.3's `user.signingkey` names, and asks for its passphrase. If that fails too, as with no agent and no terminal, git 2.43 prints the error (`error: Enter passphrase: Load key "…": incorrect passphrase supplied to decrypt private key?`), exits 0, and makes the tag anyway, unsigned. Delete it, `git -C ~/zab tag -d "$V"`, load the key into the agent (§2.1), and sign again.

Any other output is a signature that doesn't verify. Read the review first, whichever it is. Keep the SHA-256: the bootstrap prints the same one for the same base. **What you certify by signing** (ADMIN §1.6; DECISIONS D31, D32): that this is the release's commit, which passed the checks your process requires (the maintainer's releases pass CI, the reviews and the end-to-end suite), and that you read its privileged surface yourself: `deploy.sh`, the systemd units, `sysusers.conf`, `tmpfiles.conf`, the nftables rules, the Caddyfile and `render_policy.py` (or its policy, §10), and the further files `workshop/adminserver/privileged-paths` lists: the root helper's `confighelper.py`, `config.py` and `checks.py`, `pyproject.toml`, `build-constraints.txt`, and `uv.lock` through its summary by package. The review marks them all `P`. Read every line of those, then sign.

**In a repository whose releases you tag yourself,** sign, verify and push, with the three lines the review printed or these:

```sh
git -C ~/zab tag -s "$V" -m "zab $V" "$C"
git -C ~/zab tag -v "$V"                       # Good "git" signature for <email> with ED25519 key …
git -C ~/zab push origin "$V"
```

**From the public repository,** replace its tag with yours, in your clone alone, and never push it: the public repository keeps its own tag, and yours serves your deploys. The order is the fetch of the first block, the review, then these four lines together. The `tag -d` removes only your clone's copy of the public tag, and `tag -s` signs the same commit right after it: between the two, any fetch, with `--tags` or without, brings the public tag back, since tags follow the commits a clone already has.

```sh
git -C ~/zab tag -v "$V" 2>&1 >/dev/null       # Good "git" signature with ED25519 key SHA256:gTGOWot/…, No principal matched.
git -C ~/zab tag -d "$V"
git -C ~/zab tag -s "$V" -m "zab $V" "$C"
git -C ~/zab tag -v "$V"                       # Good "git" signature for <email> with ED25519 key …
```

The first line prints only the signature's two lines: the maintainer's, `Good "git" signature with ED25519 key SHA256:gTGOWot/…` with SECURITY.md's fingerprint, then `No principal matched.` If a fetch did run between them, start again from the review: it says `No tag command` again, and `tag -v` prints the maintainer's signature again. A second machine of yours signs the same release again in its own clone, with the same key (§15, step 7).

The worktree `~/zab-$V` is the tag's commit, so deploy from it. Check `admin.toml` again with the release's loader:

```sh
cd ~/zab-"$V" && uv run python -m zab.admin.config --config ~/zab-admin/etc/admin.toml check && echo ok
```

## 10. The role's policies, rendered on the laptop

From the signed release and your `admin.toml` (the adminserver README, "The instance role's policies"; ADMIN §5), with the system's Python alone: the script needs only its standard library, so nothing else of the tree runs (ADMIN §1.6). The output names no account:

```sh
cd ~/zab-"$V" && python3 -I workshop/adminserver/render_policy.py \
  --config ~/zab-admin/etc/admin.toml --out-dir ~/zab-admin/policies
ls ~/zab-admin/policies                        # zab-admin-1.json …
```

**Read them before you create anything.** This is the one place where a person checks ADMIN §5's scope. List each statement:

```sh
python3 -I - ~/zab-admin/policies/zab-admin-*.json <<'EOF'
import json, sys
for path in sys.argv[1:]:
    for s in json.load(open(path))["Statement"]:
        print(path.rsplit("/", 1)[-1], s["Sid"], s["Effect"], s["Action"], s.get("Resource"),
              json.dumps(s.get("Condition", {}), sort_keys=True))
EOF
```

Each file must hold:
- **the five denies:**
  - `DenyServer`: every `ec2:` action on anything tagged `zab-admin`;
  - `DenyServerTag`: any request that tags `zab-admin`;
  - `DenyScopeRetag`: no `workshop`, `zab-scope` or `zab-admin` tag outside creation, so nothing is ever tagged into the scope after it exists;
  - `DenyUntag`: no `DeleteTags`;
  - `DenyExistingInterface`: no `RunInstances` on `arn:aws:ec2:*:*:network-interface/eni-*`, an existing network interface, in any region: a launch creates its own, and never takes another service's address and security groups;
- **every Allow scoped** to the fleet's tag `zab-scope` = `<SCOPE>`, with two exceptions, as ADMIN §5 says:
  - `Read`: the `Describe` calls, on `*`, limited to `aws:RequestedRegion` in `[global] regions`;
  - `LaunchNetwork`: the subnet and network interface of `RunInstances`, unconditioned (the interface only as the launch creates it: `DenyExistingInterface`);
- **a `workshop` tag, never a workshop's name:** every scoped Allow but `LaunchFrom` (the region's image and group, and the key pairs) also requires that what it creates or acts on carries some `workshop` tag (`"Null": {"aws:…Tag/workshop": "false"}`). No workshop's name appears anywhere: IAM doesn't tell one workshop's VMs from another's, the server's code does (ADMIN §5);
- **`[global] regions` only** (here `<WR>`): every resource is `arn:aws:ec2:<region>:*:…` of one of them, or `*` for the denies and `Read`, except `DenyExistingInterface`'s, which denies in every region. No account number;
- **`[global] instance_types` only** (here `m7a.2xlarge`): the launch's `ec2:InstanceType`;
- **no `iam:` action,** and nothing outside `ec2:`.

`tests/workshop/test_bootstrap_doc.py` checks the same on §7.2's template, filled with sample values. Then create one managed policy per file and attach each to the role:

```sh
for f in ~/zab-admin/policies/zab-admin-*.json; do
  n=$(basename "$f" .json)
  arn=$(aws iam create-policy --profile "$P" --policy-name "$n" --policy-document "file://$f" \
    --query Policy.Arn --output text) &&
    aws iam attach-role-policy --profile "$P" --role-name zab-admin --policy-arn "$arn"
done
aws iam list-attached-role-policies --profile "$P" --role-name zab-admin --query 'AttachedPolicies[].PolicyName'
```

Every file carries every deny, so no file grants alone what the denies forbid. The bootstrap needs these attached: it imports the first workshop's key pair with the role (§6.3).

**From here, the role holds EC2 power** over every resource in `[global] regions` that carries `zab-scope=<SCOPE>` and some `workshop` tag: start, stop, terminate and modify (ADMIN §5). Nothing carries them until the first launch, but the bootstrap's key pair.

**Later,** when `[global]` changes (a region, the scope or an instance type): §6.5's steps 2–4, with that change in place of the region. A release that changes what the renderer writes, as the one with `!grow-disk` did, says so in its upgrade notes (`CHANGELOG.md`). Step 3 renders the policies in force and the new ones, and applies the new ones in an order that never grants less than either: new files attached first, then the existing ones replaced, then the policies the new render lacks, listed from IAM, detached and deleted (left attached, they would still grant what they did). A new workshop in a region that is set up changes nothing here (ADMIN §2.9).

## 11. `deploy.sh --bootstrap`

Refresh the SSH rule first (§3.1), as before every deploy. Run it from the tag's own worktree (its server half must be the tag's `deploy.sh`, byte for byte). About 10 minutes. **Run it on its own, never inside a larger paste:** it pages the diff, then waits for you to type the tag, and whatever a paste holds after it would go to the pager, or answer the prompt:

```sh
~/zab-"$V"/workshop/adminserver/deploy.sh --host "$HOST" --etc ~/zab-admin/etc \
  --bootstrap --secrets ~/zab-admin/secrets --out ~/zab-admin "$V"
```

**What it shows,** on the laptop first:
1. **The checks** (ADMIN §1.6), silent when they pass: `$V` is an annotated tag signed by a key of your `allowed_signers`, named `$V` in the tag object itself, matching `release = "final"`, and its `pyproject.toml` says the same version. A refusal is one line, `deploy.sh: <reason>`, such as `vX.Y.Z: not signed by a key in …/allowed_signers`.
2. **The reviewed diff,** paged, the same as §9's, then its file and `SHA-256 <hex>`. Compare the hash with §9's.
3. **`Type vX.Y.Z to deploy it to <host>:`** Type the release's name, then Enter. Anything else stops it, `deploy.sh: not confirmed`, before it changes anything on the server.

Then, on the server, through `sudo`:
- the packages (`python3 git caddy openssh-client age nftables curl`), and your `etc/` files installed under `/etc/zab-admin/`;
- the same checks again, with the server's own copies of `allowed_signers` and `admin.toml`;
- `uv` 0.11.24, pinned by SHA-256; then the users, the directories and the IMDS rule;
- the build at `/opt/zab-admin/releases/vX.Y.Z`, by `zab-build`, then `selfcheck: OK`, `waker selfcheck: OK, <n> modules` and `archive_check: selfcheck ok`;
- the secrets and the fleet key, with one line per workshop of `admin.toml`, here one: `zab-admin-<WS>: imported, tagged workshop=<WS>, zab-scope=<SCOPE>`;
- `/opt/zab-admin/current is vX.Y.Z.`, then the units' restart.

**Success** is that last line, no `deploy.sh:` line after it, and exit status 0 (`echo $?`).

**If it stops:**
- **Before `current is vX.Y.Z`** (a missing secret, a refused key pair import, a failed self-check), nothing is current: fix the cause and run the same command again. Every step is idempotent.
- **After it,** the release is installed and recorded, and `deploy.sh` names the units that failed and the `sudo systemctl restart …` to run once the cause is fixed. A second `--bootstrap` is then refused (`already bootstrapped`).

**Then place the fleet wheel,** which `!rollout` needs. It is built on the laptop with the pinned build backend:

```sh
~/zab-"$V"/workshop/adminserver/deploy.sh --host "$HOST" --etc ~/zab-admin/etc --wheel "$V"
```

Run it on its own as well. Once the wheel is built, it prints its SHA-256 and asks `Type vX.Y.Z to place it on <host> for !rollout:`: type the release's name. It ends with `Placed zulip_agent_bridge-X.Y.Z-py3-none-any.whl for !rollout.`

**Then seal `~/zab-admin/secrets`** with §14: from here it is needed only to replace a secret (§16), and no machine keeps it in plaintext.

## 12. First checks

**On the server** (refresh the SSH rule first if hours have passed, §3.1):

```sh
ssh "$HOST" 'readlink /opt/zab-admin/current; systemctl is-active zab-admin-imds.service caddy.service zab-admin-starter.socket zab-archive-check.socket zab-admin-bot.service zab-admin-collector.service zab-console.service zab-waker.service'
# /opt/zab-admin/releases/vX.Y.Z, then eight lines: active
ssh -t "$HOST" sudo -u zab-admin /opt/zab-admin/current/.venv/bin/python -I -m zab.admin bot --check
# bot check: OK (2 bots)
ssh "$HOST" 'sudo journalctl -u zab-waker.service -n 5 --no-pager'
# no error: the waker idles on a config with no workshop until the first !waker on
ssh "$HOST" 'sudo nft list table inet zab_imds >/dev/null && echo imds rule; sudo -u nobody curl -sS -m 5 http://169.254.169.254/latest/meta-data/'
# imds rule, then curl: (7) Failed to connect …: only root, zab-admin and zab-starter reach IMDS
```

`bot --check` names whatever it refuses (an owner, a mode, a key whose `GET /users/me` disagrees with `admin.toml`). Fix that before anything else.

**From the laptop:**

```sh
curl -sS -o /dev/null -D - "https://$HOST/" | head -1   # HTTP/2 303, and no certificate error
aws ec2 describe-key-pairs --profile "$P" --region "$WR" \
  --filters "Name=key-name,Values=zab-admin-$WS" \
  --query "KeyPairs[].[KeyName,Tags[?Key=='workshop']|[0].Value,Tags[?Key=='zab-scope']|[0].Value]" --output text
# zab-admin-<WS>  <WS>  <SCOPE>
```

**`HTTP/2 303`** (to `/login`): Caddy passes every request to the console on `127.0.0.1:8090`. The check is a `GET`: the console answers a `HEAD` `405`. A `curl` built without HTTP/2 prints `HTTP/1.1 303 See Other` instead; that is the same answer. A status line, any status, proves that DNS reaches the server and that Caddy holds a valid certificate for the name. A certificate or name error, and no status line, is the failure: check §4's records, and Caddy's log (`ssh "$HOST" sudo journalctl -u caddy.service -n 20 --no-pager`).

**In Zulip,** in `#<WS>-organizers`, in a topic such as `bot check` (ORGANIZERS §1.3). Each answer ends with the audit footer `audit #… · chain …`:

| Send | Answer |
|---|---|
| `!help` | `**<WS>'s bot** takes:`, then every command |
| `!version` | ``zab vX.Y.Z · release `final` · <n> global admins``, then `- <WS>: <WR>, <n> organizers, its bot user <id>` |
| `!status all` | `❌ this workshop's roster has no participants yet: nothing to select` |
| `!roster show` | with a participants' channel (`channel`, §7.2): ``The roster has no participants yet: `!roster sync`.``; without one: ``The roster has no participants yet: `!roster import`, with the CSV attached.`` |
| `!plans` | `No plan is pending on this bot.` |

In `#zab-admins`, `!help` answers `**the server's bot** takes:`, and `!version` lists the one workshop, `<WS>`.

No bot may post `⚠ subscribed here, but not recognized by this bot: …` in a `membership` topic. If one does, fix that channel's subscribers (§5.4).

**The first backup** comes by DM after the first apply that changes a workshop's files, such as a `!roster sync` or `!roster import`. Decrypt it once, as the adminserver README's "Backups and recovery" says, to know your key works.

**What comes next** is not the bootstrap:
- the first workshop's organizers' runbook, [`docs/ORGANIZERS.md`](ORGANIZERS.md), from the roster on (`!roster sync`, or `!roster import`);
- new workshops through `!new-workshop` (§7.3), and a new region first where one needs it (§6.5).

## 13. Rollback, and what never to do

### 13.1 If a step fails

- **§3 (AWS):** each resource is found by its tag (§3.7). To start over, remove them in this order:

  ```sh
  aws ec2 modify-instance-attribute --profile "$P" --region "$RS" --instance-id "$I" --no-disable-api-termination
  aws ec2 terminate-instances --profile "$P" --region "$RS" --instance-ids "$I"
  aws ec2 wait instance-terminated --profile "$P" --region "$RS" --instance-ids "$I"
  aws ec2 release-address --profile "$P" --region "$RS" --allocation-id "$ALLOC"
  aws ec2 delete-security-group --profile "$P" --region "$RS" --group-id "$SG"
  ```

  The role and instance profile can stay. To remove them too: `aws iam remove-role-from-instance-profile`, `delete-instance-profile`, `detach-role-policy` and `delete-policy` for each `zab-admin-N`, `delete-role-policy` for an inline policy such as `AWSRevokeOlderSessions` (§13.3, step 2), then `delete-role`. EC2 may refuse a terminate for some minutes after the protection is lifted (#378): run it again.
- **§6.1 (a region's group or image):** remove a wrong one in its region, `aws ec2 delete-security-group --group-id …`, or `aws ec2 deregister-image --image-id …` and then `aws ec2 delete-snapshot` for its snapshot, and make it again.
- **§9 (a wrong tag, before you pushed, or one of the public repository's, which you never push):** `git -C ~/zab tag -d "$V"`, and sign again. After a push, don't move it: anyone may have fetched it. Make the next release instead.
- **§11, before `current`:** fix the cause and run the same command again (§11).
- **§11, after `current`:** the release is installed. Fix what `deploy.sh` names, then restart the units it lists.
- **A new server, after a bootstrapped one:** the old server's key pairs stay, one per workshop, `zab-admin-<name>` in its region. The new bootstrap refuses them (§6.3). Once the old server is gone, delete them first: `aws ec2 delete-key-pair --profile "$P" --region <its region> --key-name zab-admin-<name>`, for each workshop of the new `admin.toml`, as the harness does (`aws_rig.drop_key_pairs`).
- **A key pasted where it shouldn't be:** regenerate it at once, and install the new one on the server: §16, whose step 1 regenerates each kind of key, and whose step 5 deletes a GitHub token.
- **A server lost, or that may be compromised:** §13.3. A compromised one is stopped through AWS, never over SSH, its role's policies detached and its keys revoked.

### 13.2 Never

These hold before, during and after the bootstrap. They keep the deployment's keys where no coding agent can reach them (ADMIN §1.4).

- Never run any of this from a machine where coding agents run, or give an agent the account's profile, the server's SSH access, or any key here. The dedicated key is never on such a machine (§2.1).
- Never add a personal key to §2.1's agent, or use one for zab.
- Never paste a zuliprc, the token or a private key into a chat, Zulip, an issue, a PR, an agent's session or a shell command line. Never commit `admin.toml`, `etc/` or `secrets/`.
- Never forward your SSH agent or X11 to the server, and never copy AWS credentials or an SSH private key onto it.
- Never admit SSH from an agent machine's address, or from anywhere wide, in `zab-admin`. On a network you share with one, your address is its address (§3.1).
- Never tag the server, its volume, its Elastic IP or its group `workshop=…` or `zab-scope=…`. The `zab-admin` tag keeps them out of the role's reach and out of every teardown.
- Never subscribe `zab-provisioner` to `#zab-admins` or to a `#…-organizers` channel, and never give it a role above Moderator (§5.1).
- Never keep `~/zab-admin/secrets` in plaintext between uses (§14), or copy it, or the bundle, into a synced folder.
- Never deploy from a checkout of `main`, or with a `deploy.sh` you haven't read.
- Never set `release = "rc"` in production.
- On the server, never `nft flush ruleset` or a bare `nft -f /etc/nftables.conf`: apply firewall changes with `systemctl reload nftables` (the adminserver README, "IMDS").

### 13.3 A server that is lost, or may be compromised

**Which case.**
- **Compromised,** or suspected. Signs (ORGANIZERS §8.3): a plan or result that nobody requested, an audit footer whose number goes back, warnings about subscribers nobody added. Run steps 1 to 6 at once, in order, then step 7. Never over SSH: a sudo password typed on the server would be handed over too. A compromised server is never used again.
- **Lost:** gone, or unreachable, and not suspected. The VMs and the participants' agents keep running, but the waker went with the server: a VM that idle stop powers off stays off until you start it (`power.py`, [`workshop/aws/README.md`](../workshop/aws/README.md)). Run steps 4 and 5, then step 7.

1. **Stop it through AWS,** with §3.7's `$I`. Its bots, waker and console stop with it:

   ```sh
   aws ec2 stop-instances --profile "$P" --region "$RS" --instance-ids "$I" \
     --query 'StoppingInstances[0].CurrentState.Name' --output text      # stopping
   ```
2. **Detach the role's policies,** by the account owner, so that the role's credentials, wherever they were copied, can do nothing more:

   ```sh
   for arn in $(aws iam list-attached-role-policies --profile "$P" --role-name zab-admin \
       --query 'AttachedPolicies[].PolicyArn' --output text); do
     aws iam detach-role-policy --profile "$P" --role-name zab-admin --policy-arn "$arn"
   done
   aws iam list-attached-role-policies --profile "$P" --role-name zab-admin --query 'AttachedPolicies[].PolicyName'   # []
   ```

   Then revoke the role's active sessions, in the IAM console, *Roles → zab-admin → Revoke sessions*: it adds an inline policy, `AWSRevokeOlderSessions`, that denies every credential issued before now, so that none the old server held works again once step 7 attaches policies.
3. **Revoke the keys it held,** which work from any machine. Open the bundle first (§14, "Open it"), and put each new key in `$S/secrets/` as §16's step 1 does:
   - **the provisioner's key,** which owns every participant bot and every admin bot, and so can read, regenerate or give away their keys: signed in as `zab-provisioner` (§5.1), *Personal settings → Account & privacy → API key → Manage your API key → Generate new API key*;
   - **the admin bots' keys,** in the same session (§5.3): *Personal settings → Bots*, each admin bot's *Regenerate API key*;
   - **the GitHub token:** on github.com, as its owner, *Settings → Developer settings → Personal access tokens → Fine-grained tokens*, the server's token, *Delete*; then a new one with §8.1's settings.

   Keep them off any server until step 7.
4. **The server's fleet key,** on every VM it launched, through each workshop's key pair, `zab-admin-<name>`. Save its public half from one of them, then delete every one, each in its region, so that no launch authorizes the key again, and so that step 7's bootstrap can import its own (`import_key_pair.py` refuses a pair of that name with another key):

   ```sh
   R=<a workshop's region>; N=<that workshop's name>
   [ -s ~/zab-admin/fleet-key.pub ] || aws ec2 describe-key-pairs --profile "$P" --region "$R" \
     --key-names "zab-admin-$N" --include-public-key --query 'KeyPairs[0].PublicKey' --output text > ~/zab-admin/fleet-key.pub
   grep -q '^ssh-ed25519 ' ~/zab-admin/fleet-key.pub &&
     aws ec2 delete-key-pair --profile "$P" --region "$R" --key-name "zab-admin-$N"
   ```

   Run it once per workshop of the server. It deletes a key pair only once `~/zab-admin/fleet-key.pub` holds the key, one `ssh-ed25519 …` line; if the file holds anything else, remove it and run the block again.
5. **Take the key off the VMs,** over your break-glass key, per workshop, with its directory from the latest backup: the roster, the pinned host keys and `out/`. The `age` line asks for your key's passphrase, so it is a block of its own:

   ```sh
   B=<the latest backup-….tar.gz.age, from its DM>; WS=<a workshop>
   W=$(mktemp -d) && age -d -i ~/.ssh/zab-deployer "$B" | tar -xzf - -C "$W" "ws/$WS"
   ```

   ```sh
   cd ~/zab-main && uv run python workshop/admin/join_server.py --roster "$W/ws/$WS/roster.csv" \
     --known-hosts "$W/ws/$WS/out/known_hosts" --identity ~/.ssh/zab-deployer \
     --server-key ~/zab-admin/fleet-key.pub --remove --dry-run
   ```

   The dry run says `would remove 1` on each row; then run it without `--dry-run`, which says `ok: removed 1` and ends `N of N ok`. A stopped VM fails to connect: start it by hand (`power.py`), and run again. The spares, which no row lists, keep the key: tear them down by hand, `teardown.py --only zab-spare-N` with the workshop's scope tags and region. After a lost server, `rm -r "$W"` now: it holds personal data and bot keys. After a compromise, step 6 uses it first.

   **What the key reached,** after a compromise. It logs in as `ubuntu`, which has sudo, so the server could read anything on the VMs it reached, the participants' agent sign-ins included: where that matters, the participants sign their agents out and in again, or revoke those sessions in their accounts. The server's role could also act as root on every VM of its scope, through the VM's user data. Removing the key undoes neither: where it matters, replace the VMs once their work is on GitHub.
6. **The participant bots' keys,** which the server held: regenerate them now, from the laptop, with step 3's new provisioner key, so that the copies it held stop working. Per workshop, with the flags the bot uses, a dry run first:

   ```sh
   ORG=<its github_org>
   HOLD=--hold     # while its sandboxes are held (ORGANIZERS §3.3); HOLD= once they are released
   entry() { python3 -I -c 'import sys, tomllib; w = {e["name"]: e for e in tomllib.load(open(sys.argv[1], "rb"))["workshop"]}[sys.argv[2]]; print(w.get(sys.argv[3], ""))' ~/zab-admin/etc/admin.toml "$WS" "$1"; }
   URL=$(entry refserve_url); GIFTS=$(entry gifts_step)   # as the bot passes them (ADMIN §2.1)
   cd ~/zab-main && uv run python workshop/admin/zulip_provision.py --roster "$W/ws/$WS/roster.csv" \
     --zuliprc "$S/secrets/provisioner.zuliprc" --out-dir "$W/ws/$WS/out" \
     --repo-owner "$ORG" --refserve-url "$URL" ${GIFTS:+--gifts-step "$GIFTS"} \
     --no-rebind $HOLD --regenerate-keys --dry-run
   ```

   A full run sets each sandbox's description again, and posts "Start here" where there is none: `--refserve-url` and `--gifts-step` keep them the workshop's.

   Then the same without `--dry-run`: each row reads `codex: key regenerated, claude: key regenerated`. Without `--hold`, a held workshop would be released, its participants subscribed and greeted. `--no-rebind` keeps each row's recorded Zulip ID. The VMs' bridges then hold dead keys, and the participants' agents don't answer, until step 7 rolls the new ones out. Then `rm -r "$W"`, and seal the bundle again (§14's block, `D="$S"`).
7. **A replacement server.** It is a bootstrap into an account that holds the old server's resources, and it has not been run end to end: read each step it names before you run it. Where it differs from a first bootstrap:
   - **The old instance, its address and its group:** §13.1's first block removes them, once you have looked at a compromised one.
   - **§3,** without §3.2: the role and its instance profile exist, and §3.3 finds your key pair. **§4** changes the A record to the new Elastic IP. **§3.6** changes the `HostName` of your `~/.ssh/config` entry: a second entry would lose to the first.
   - **§5:** only §5.6, with your keys: the bots and channels exist. After a lost server, regenerate each admin bot's key that your bundle lacks, those `!new-workshop` made, as step 3 does.
   - **§6:** only §6.2, and §6.3's check, which finds no key pair once step 4 has run.
   - **§7:** your copy of `admin.toml`, which the backups don't hold. It lacks any workshop `!new-workshop` added since you last fetched the server's (§15, step 6): add each from the plan that made it, which showed the entry (§7.3).
   - **§8** with the bundle's secrets (§14, "Open it"), step 3's new ones after a compromise. **§9** for the release.
   - **§10:** render as there, into `NEWP=~/zab-admin/policies.new`, and read it with §10's listing, on `"$NEWP"/zab-admin-*.json`. Then apply it with §6.5's step 3 (the apply block, its read-back and the removal of the copies), not §10's `create-policy` loop: the policies `zab-admin-N` exist, and `create-policy` refuses their names. Then `rm -rf ~/zab-admin/policies && mv "$NEWP" ~/zab-admin/policies`.
   - **§11 and §12,** then the restore of the latest backup (the adminserver README, "Backups and recovery"). Its last step gives the VMs the new server's fleet key over your break-glass key: `describe-key-pairs --include-public-key` prints it, as in step 4, into a file other than the old key's. Then `!waker on` again for the VMs that had it, since the backups don't hold the waker's opt-ins.
   - **After a compromise,** the restored `out/` holds the participant bots' keys of before step 6, which no longer work: §17's step 7, on the new server, regenerates them again and rolls them out.

## 14. Secrets between uses

After §11, `~/zab-admin/secrets` is needed again only to replace a secret on the running server (§16) or to bootstrap a new server (§13.1). Between uses, neither machine of yours keeps it in plaintext: it is one `age` bundle, `~/zab-admin/secrets.tar.age`, encrypted to `backup_recipients`, the deployers' keys (§8.2). So the dedicated key and its passphrase open it, as they open the backups, and so does each other deployer's.

**Seal it** with this block: right after §11, with `D=~/zab-admin`; and after a change to an opened bundle (§16, §17) or to `backup_recipients`, with `D="$S"`. `D` is the directory that holds `secrets/`. Run it on its own: it asks for the key's passphrase once, to check the new bundle.

```sh
D=<the directory that holds secrets/: ~/zab-admin after §11, or "$S">
( set -o pipefail; umask 077
  COPYFILE_DISABLE=1 tar -C "$D" -cf - secrets | age -R ~/zab-admin/etc/backup_recipients > ~/zab-admin/secrets.tar.age.new &&
  diff <(age -d -i ~/.ssh/zab-deployer ~/zab-admin/secrets.tar.age.new | tar -tf - | sed 's|/$||' | sort) \
       <(cd "$D" && find secrets | sort) &&
  mv ~/zab-admin/secrets.tar.age.new ~/zab-admin/secrets.tar.age && rm -r "$D/secrets" && echo sealed )
```

It seals into `secrets.tar.age.new`, under `pipefail`, so a failed `tar` or `age` (a bad `backup_recipients`, a full disk) stops it; opens that file with your key, here the dedicated key, and compares its listing with `secrets/`; and only then replaces the bundle and removes the plaintext. It ends `sealed`. Anything else, a `diff` or an error, leaves the old bundle and `secrets/` as they were: fix the cause, and run it again. (`COPYFILE_DISABLE=1` keeps macOS's `tar` from adding `._` files.) After §11, `secrets/` holds §8.1's files, so the bundle lists:

```text
secrets/
secrets/bots/
secrets/bots/<WS>.zuliprc
secrets/github-token
secrets/global.zuliprc
secrets/provisioner.zuliprc
```

`age -d -i ~/.ssh/zab-deployer ~/zab-admin/secrets.tar.age | tar -tf - | sort` lists it again at any time. On an SSD, `rm` can't overwrite what a file held: keep the machine's disk encrypted (FileVault, LUKS), which covers what it leaves. Never copy the bundle into a synced folder: it is safe only while every key of `backup_recipients` is.

**Open it** to a temporary directory, for one use (it asks for the passphrase):

```sh
S=$(mktemp -d) && age -d -i ~/.ssh/zab-deployer ~/zab-admin/secrets.tar.age | tar -C "$S" -xf - && echo "$S"
ls -l "$S/secrets"                     # -rw------- each, and bots/
```

Use `"$S/secrets"`: §16's file, or `deploy.sh`'s `--secrets "$S/secrets"` for a new server's `--bootstrap`. Then, if you changed nothing, `rm -r "$S"`. If you changed a file, seal it with the block above and `D="$S"`, then `rmdir "$S"`, which removes the directory only once the block has emptied it.

**When `backup_recipients` changes** (a deployer added, or a key replaced), open the bundle and seal it again with `D="$S"`: the block encrypts to the new file. After a key replacement, open it with the old key, as §17's step 6 does. A copy sealed to the old recipients stays readable to them: delete every such copy.

## 15. A second deployer machine

For your second machine (a laptop beside a desktop), or one that replaces a lost machine: what it needs once, after which it deploys and checks as the first does. It is the same deployer, with the same key: one key, one passphrase (§2.1). A second *deployer*, a person, makes a key of their own instead (§2.1), which the server's `allowed_signers`, `backup_recipients` and `authorized_keys` then also list (§17's commands install those files).

**The server is the source of truth** for the `etc/` files: `!new-workshop` changes `admin.toml` there, and an upgrade installs none of yours. So this machine fetches them from the server (step 6), never from the first machine, and so does any machine before it edits or deploys.

1. **Tools:** §1's step 1.
2. **The dedicated key,** the first machine's own files, `~/.ssh/zab-deployer` and `~/.ssh/zab-deployer.pub`, copied over a channel of your own: `scp` between your two machines, or a USB stick you keep. Never a synced folder, a chat or an e-mail: the private file is encrypted with the passphrase, which slows a thief, no more. Then, on this machine:

   ```sh
   chmod 700 ~/.ssh && chmod 600 ~/.ssh/zab-deployer && chmod 644 ~/.ssh/zab-deployer.pub
   ls -l ~/.ssh/zab-deployer ~/.ssh/zab-deployer.pub   # -rw------- … zab-deployer, then -rw-r--r-- … zab-deployer.pub
   ssh-keygen -l -f ~/.ssh/zab-deployer.pub            # the first machine's line: 256 SHA256:… zab-deployer (ED25519)
   ```

   Then §2.1's two agent lines. The `ssh-add` asks for the passphrase, from your password manager, and `ssh-add -l` must print the same `SHA256:…` as the `.pub`: that proves the private file is the key.
3. **§0's variables and the AWS profile.** Set §0's block. Make the profile `$P` with `aws configure --profile "$P"`; or, where the account uses IAM Identity Center, `aws configure sso --profile "$P"`, then `aws sso login --profile "$P"` before each session, so that no long-lived key sits on the machine. Check it:

   ```sh
   aws sts get-caller-identity --profile "$P" --query Account --output text   # <acct>
   ```
4. **The clone and the admin directory:** §1's steps 2 and 3.
5. **SSH to the server, its host key pinned.** Copy §3.6's `~/.ssh/config` entry from the first machine, above any `Host *` block, and run §3.6's `ssh -G` check. Refresh the SSH rule from here (§3.1): it admits one address, so refresh it again from the first machine when you go back to it. On the first machine, read the fingerprint it pinned:

   ```sh
   ssh-keygen -l -F <the Elastic IP>      # on the first machine: … ED25519 SHA256:…
   ```

   Then connect from this machine once, asking (a line of its own), and accept the key only if its `ED25519` fingerprint is that one:

   ```sh
   ssh -o StrictHostKeyChecking=ask "$HOST" 'hostname; sudo -n true && echo sudo ok'   # …, then sudo ok
   ssh-keygen -l -F <the Elastic IP>      # here too now: the same ED25519 line
   ```
6. **The server's `etc/` files,** fetched into a new directory, compared with this machine's (none the first time: `Only in …` for each), then put in place, the old ones kept as `etc.old`. The server holds them as root's 0644 files, which `ubuntu` reads:

   ```sh
   T=$(mktemp -d)
   ssh "$HOST" tar -C /etc/zab-admin -cf - admin.toml allowed_signers backup_recipients break-glass | tar -C "$T" -xf -
   ls "$T" "$T/break-glass"               # admin.toml allowed_signers backup_recipients break-glass, then the .pub files
   diff -ru ~/zab-admin/etc "$T"
   rm -rf ~/zab-admin/etc.old && { [ ! -d ~/zab-admin/etc ] || mv ~/zab-admin/etc ~/zab-admin/etc.old; } && mv "$T" ~/zab-admin/etc
   ```

   On a machine whose copy is stale, the `diff` shows what changed on the server, such as `+` entries from `!new-workshop`. A `-` line is something only your copy had: find out why before you drop it. Run this block on either machine before you edit `admin.toml` (§6.5) or deploy: `deploy.sh` shows the server's differences from your copy at every upgrade (`The server's admin.toml differs from …`), and installs nothing of them.
7. **Signing.** §2.3's three `git config` lines, now that `allowed_signers` is here, then a release's tag:

   ```sh
   git -C ~/zab tag -v "$(ssh "$HOST" readlink /opt/zab-admin/current | sed 's|.*/||')"   # Good "git" signature for <email> …
   ```

   From the public repository, this clone has the public tag instead, and the check prints the maintainer's signature, `Good "git" signature with ED25519 key SHA256:gTGOWot/…`, then `No principal matched.` (§9; with SECURITY.md's line in your `allowed_signers`, it prints `Good "git" signature for <the maintainer's address> …`, and this machine signs nothing). Sign that release here too, on the commit the server runs, which its deploy record names (`/opt/zab-admin/deploys.jsonl`, root's 0644 file), never on whatever the public tag names today. The last line prints `same commit` only when this clone's tag names that commit:

   ```sh
   V=$(ssh "$HOST" readlink /opt/zab-admin/current | sed 's|.*/||')
   C=$(ssh "$HOST" cat /opt/zab-admin/deploys.jsonl | grep -E '"event":"(bootstrap|upgrade)"' |
     grep -F "\"version\":\"$V\"" | tail -n 1 | sed 's/.*"commit":"\([0-9a-f]*\)".*/\1/')
   [ -n "$C" ] && [ "$(git -C ~/zab rev-parse "$V^{commit}")" = "$C" ] && echo same commit
   ```

   After `same commit`, and only then, run §9's block for the public repository, with these `V` and `C`. The same key signs it, so `deploy.sh` accepts this machine's tag as it does the first's, and an upgrade's `--since` base too. Anything else: stop, since this clone's tag doesn't name the commit the server runs.
8. **The secrets,** sealed: copy `~/zab-admin/secrets.tar.age` from the first machine (§14), as the key in step 2, then list it with §14's second command. It opens with this machine's key, which is the same.

From here, everything of this runbook and of the deploys runs from either machine. Keep both current: a change of a secret (§16) seals a new bundle, which you copy to the other machine; a change of the key (§17) is made on both.

## 16. Replace a secret on a running server

For one secret of `/etc/zab-admin/secrets/`: the GitHub token, before it expires or after a leak; the provisioner's key; an admin bot's key, `global.zuliprc` or `bots/<name>.zuliprc`, after a leak. `deploy.sh` installs secrets only at `--bootstrap`, so this is done by hand, as root, with the owner and mode `install_secrets` gives them: `root:zab-admin`, `0640`. Not here: a participant bot's key, under `ws/<name>/out/` (§17, step 7), and the fleet's SSH key, which never leaves the server.

**When.** While no plan is pending or applying on any of the server's bots: each workshop's `!plans` answers `No plan is pending on this bot.`, and no progress message is waiting for its result. Step 3 restarts the bot, which cuts an apply short.

**Which units read the secrets** (`workshop/adminserver/zab-*.service`):
- `zab-admin-bot.service` reads each admin bot's key at its start, for its own Zulip client, so a bot whose key changed answers nothing until it restarts. It hands the provisioner's key to the scripts by its path, and the runner reads the GitHub token afresh for each step that names it, so those take effect at the next run. It also registers every secret with its scrubber;
- `zab-admin-collector.service` reads them only to scrub, and again whenever a file changes;
- the starter, the console and the waker can't read `secrets/` at all (`InaccessiblePaths`), and the config helper reaches only `secrets/bots/`, to write a new workshop's key.

So step 3 restarts the bot alone: it is needed for an admin bot's key, and does no harm for the others, whose new value it then registers at once.

1. **The new secret, on the laptop,** in the bundle opened to `$S` (§14's "Open it"):
   - **the GitHub token:** a new one with §8.1's settings, saved with §8.1's line, here into `$S`. Run it on its own:

     ```sh
     (umask 077; printf 'token: '; read -rs T; echo; printf '%s\n' "$T" > "$S/secrets/github-token"; unset T)
     ```

     Then §8.1's three checks, on `"$S/secrets/github-token"`: 94 bytes, `github_pat_`. The old token works until you delete it (step 5), so renewing it takes nothing down;
   - **the provisioner's key:** signed in as `zab-provisioner`, *Personal settings → Account & privacy → API key → Manage your API key → Generate new API key*, then *Download zuliprc*. The old key stops at once, so a provisioning step fails until step 2;
   - **an admin bot's key:** signed in as `zab-provisioner`, which owns the admin bots (§5.3), *Personal settings → Bots*, the bot's *Regenerate API key*, then *Download zuliprc*. The old key stops at once, and that bot answers nothing until step 3.

   Put a downloaded zuliprc at its path in `$S`, `F` below:

   ```sh
   F=<its path under secrets/: github-token, provisioner.zuliprc, global.zuliprc or bots/NAME.zuliprc>
   D=<the downloaded zuliprc>; install -m 0600 "$D" "$S/secrets/$F" && rm "$D"   # not for the token
   ```
2. **Install it on the server,** over a temporary directory of its own, then remove that directory. Refresh the SSH rule first (§3.1). The first line stages the file only if the server has one at that path, so a typing error stages nothing, and the install then finds nothing to install:

   ```sh
   T= && ssh "$HOST" sudo test -f "/etc/zab-admin/secrets/$F" && T=$(ssh "$HOST" mktemp -d) && scp "$S/secrets/$F" "$HOST:$T/new" && echo "staged in $T"
   ssh -t "$HOST" "sudo install -o root -g zab-admin -m 0640 $T/new /etc/zab-admin/secrets/$F && rm -r $T &&
     sudo stat -c '%U:%G %a %n' /etc/zab-admin/secrets/$F"
   # root:zab-admin 640 /etc/zab-admin/secrets/<F>
   ```
3. **Restart the bot, and run its checks:**

   ```sh
   ssh -t "$HOST" "sudo systemctl restart zab-admin-bot.service &&
     sudo -u zab-admin /opt/zab-admin/current/.venv/bin/python -I -m zab.admin bot --check"
   # bot check: OK (<n> bots), the global bot and one per workshop
   ```

   `bot --check` reads each admin bot's key with `GET /users/me`, and checks every secret's owner and mode. It doesn't use the provisioner's key or the token: step 4 does.
4. **Check that the secret works,** through a bot, with a plan you then reject (its `!reject` line, in its approval message):
   - **an admin bot's key:** `!version` in its channel answers;
   - **the provisioner's key:** `!provision zulip alice`, for a row of a workshop, here `alice` (`!provision zulip hold alice` while the workshop is held): the plan's dry run reads Zulip with that key;
   - **the GitHub token:** `!provision repos invites-only alice`, for a row whose repository is provisioned and whose participant has a GitHub login: the plan's dry run reads GitHub with the token. When it has nothing to change, there is no plan to reject: the answer is ``☑ **P-n** · `!provision repos alice invites-only`: nothing to change, as its dry run says; no plan to approve``, with that dry run under it, which shows that the token works just the same (ORGANIZERS §2.2).
5. **Seal the bundle again, and retire the old secret.** Seal it with §14's block and `D="$S"`: it replaces the bundle only once the new one opens with your key and lists the same files, and only then removes the plaintext. Once it has printed `sealed`, `rmdir "$S"`, and copy the new `secrets.tar.age` to your other machine (§15). A replaced GitHub token still works until you delete it: on github.com, as its owner, *Settings → Developer settings → Personal access tokens → Fine-grained tokens*, the old token, *Delete*. A Zulip key stopped at step 1.

## 17. A lost or suspected key

The dedicated key (§2.1) opens everything a deployer holds: SSH to the server and, as break-glass, to every VM launched with `admin_keys`; signing releases; the backups; the secrets bundle (§14). One key, one passphrase, on your two machines (§15), with the passphrase in your password manager.

**Lost** (both copies, or the passphrase), with no other deployer: nothing opens the backups or the bundle, which are encrypted to that key alone, and nobody reaches the server or the VMs over SSH. The server and its bots keep running and serving the organizers, but nothing can be deployed to it. One way back is a new key and a new server, bootstrapped with new secrets (§5.1's and §5.3's keys regenerated, a new token), with each workshop's roster imported again from its organizers' CSV; the old server is stopped through AWS once nothing needs it (§13.3, step 1). In the same account, that bootstrap is §13.3's replacement server, step 7. Another keeps the server and its state, rosters and secrets included, and is not written out here: through AWS, the account owner stops the instance, attaches its encrypted root volume to a rescue instance of the account, writes the new key into `~ubuntu/.ssh/authorized_keys`, the new lines into `/etc/zab-admin/allowed_signers` and `backup_recipients`, and the new public key over the break-glass file that `admin_keys` names under `/etc/zab-admin/break-glass/`, so that VMs launched from then get it; puts the volume back as the root device and starts the instance; then "Suspected" below from step 6, with new secrets in place of the bundle. **Either way, the VMs launched before keep the lost key as their only break-glass key,** and get the new one only through a key that reaches them: `join_server.py` (step 5) runs on the laptop over a break-glass key, which is lost, and the one key that still reaches them is the server's own fleet key, which no shipped tool uses to add a key. Until they are replaced, the server that launched them is the only way into them. A second deployer, whose key the server's files and the workshops' `admin_keys` also list, so that every VM has it, avoids all of it.

**Suspected** (a copy may have leaked): replace it everywhere it reaches, while the old key still works. In this order:

1. **A new key,** with a new passphrase. Move the old files aside first, then make the new one with §2.1's first block, under the same name, so that the configuration naming it (`IdentityFile`, `admin_keys`, `user.signingkey`) stays:

   ```sh
   mv ~/.ssh/zab-deployer ~/.ssh/zab-deployer.old && mv ~/.ssh/zab-deployer.pub ~/.ssh/zab-deployer.old.pub
   ```

   Then §2.1's agent lines, and the new files to your other machine (§15, step 2). Until step 2, the server admits only the old key: its one command names the old file explicitly. Refresh the SSH rule first (§3.1).
2. **SSH to the server:** `ubuntu`'s `authorized_keys`, replaced over the old key by the new one. The command line's `IdentityFile` is tried first, so this connects with the old key; if other deployers' keys are in that file, put their `.pub` files after yours in the `cat`:

   ```sh
   cat ~/.ssh/zab-deployer.pub | ssh -o IdentityFile=~/.ssh/zab-deployer.old "$HOST" \
     'umask 077 && cat > ~/.ssh/ak.new && test -s ~/.ssh/ak.new && mv ~/.ssh/ak.new ~/.ssh/authorized_keys && cat ~/.ssh/authorized_keys'
   # ssh-ed25519 AAAA… zab-deployer
   ssh "$HOST" 'sudo -n true && echo sudo ok'      # with the new key alone: sudo ok
   ```
3. **Your `etc/` files:** fetch the server's first, over the new key (§15, step 6). Then swap your line alone in `allowed_signers` and `backup_recipients`, keeping every other deployer's, and replace the break-glass file:

   ```sh
   OLDB=$(cut -d' ' -f2 ~/.ssh/zab-deployer.old.pub); NEWK=$(cut -d' ' -f1,2 ~/.ssh/zab-deployer.pub)
   sed -i.bak "s|ssh-ed25519 $OLDB|$NEWK|" ~/zab-admin/etc/allowed_signers ~/zab-admin/etc/backup_recipients &&
     rm ~/zab-admin/etc/allowed_signers.bak ~/zab-admin/etc/backup_recipients.bak
   cp ~/.ssh/zab-deployer.pub ~/zab-admin/etc/break-glass/zab-deployer.pub
   grep -rlF -e "$OLDB" ~/zab-admin/etc                                       # prints nothing
   grep -cF -e "$(cut -d' ' -f2 ~/.ssh/zab-deployer.pub)" ~/zab-admin/etc/allowed_signers ~/zab-admin/etc/backup_recipients
   # …/allowed_signers:1, then …/backup_recipients:1
   ```

   The `sed` keeps each line's principal and options (`<email> namespaces="git"` in `allowed_signers`) and changes only the key. A break-glass file of another name that holds the old key shows in the first `grep`: remove it.
4. **The server's copies,** installed as root's 0644 files, the way the bootstrap installed them (`install_etc`). Nothing restarts: the bot reads `backup_recipients` again for each backup, `deploy.sh` reads `allowed_signers` at each deploy, and each launch reads the break-glass key:

   ```sh
   T=$(ssh "$HOST" mktemp -d) && scp ~/zab-admin/etc/allowed_signers ~/zab-admin/etc/backup_recipients \
     ~/zab-admin/etc/break-glass/zab-deployer.pub "$HOST:$T/" && echo "$T"
   ssh -t "$HOST" "sudo install -o root -g root -m 0644 $T/allowed_signers $T/backup_recipients /etc/zab-admin/ &&
     sudo install -o root -g root -m 0644 $T/zab-deployer.pub /etc/zab-admin/break-glass/ && rm -r $T &&
     sudo -u zab-admin /opt/zab-admin/current/.venv/bin/python -I -m zab.admin bot --check"
   ```

   It ends `bot check: OK (<n> bots)`. New backups are encrypted to the new key from the next one, and VMs launched from now get it.
5. **The VMs,** each launched with the old key as break-glass for `ubuntu`: `join_server.py` adds the new key over the old one, then removes the old one over the new, on every row with a VM. It runs on the laptop, with a workshop's roster and pinned host keys, which the latest backup holds (the adminserver README, "Backups and recovery"), and reaches only running VMs: `!start all` through the workshop's bot first (a started VM stays up for its first hour). Per workshop, from `~/zab-main`:
   1. **The roster and host keys,** from the latest backup, into a directory of their own. Since step 4, new backups are sealed to the new key, and this step's `!launch` of the spares writes `spares.json`, which makes one per workshop, so `age` is given both keys and opens a backup from either side of the change (it asks for the passphrase of the key it uses):

      ```sh
      B=<the latest backup-….tar.gz.age, from its DM>; WS=<a workshop>
      W=$(mktemp -d) && age -d -i ~/.ssh/zab-deployer.old -i ~/.ssh/zab-deployer "$B" |
        tar -xzf - -C "$W" "ws/$WS/roster.csv" "ws/$WS/out/known_hosts"
      ```
   2. **Add the new key, over the old one,** in an agent of its own that holds the old key alone and ends with the command (it asks for the passphrase). The dry run says `would add` on each row; then run the same without `--dry-run`, which says `ok: added`:

      ```sh
      cd ~/zab-main && ssh-agent bash -c 'ssh-add "$1" && exec uv run python workshop/admin/join_server.py \
        --roster "$2/ws/$3/roster.csv" --known-hosts "$2/ws/$3/out/known_hosts" --identity "$1" \
        --server-key ~/.ssh/zab-deployer.pub --dry-run' _ ~/.ssh/zab-deployer.old "$W" "$WS"
      ```
   3. **Remove the old key, over the new one,** in §2.1's terminal, whose agent holds the new key. The dry run must say `would remove 1` on every row; then the same without `--dry-run`, `ok: removed 1`:

      ```sh
      cd ~/zab-main && uv run python workshop/admin/join_server.py --roster "$W/ws/$WS/roster.csv" \
        --known-hosts "$W/ws/$WS/out/known_hosts" --identity ~/.ssh/zab-deployer \
        --server-key ~/.ssh/zab-deployer.old.pub --remove --dry-run
      ```
   4. **Remove the roster,** which is personal data: `rm -r "$W"`.

   Each run ends `N of N ok`. A stopped VM fails to connect: start it and run again. Until its run, a VM still admits the old key.

   **The spares,** each workshop's `zab-spare-N`, have the old key too: `launch.py --admin-key` gives it to every VM it launches, spares included, and `!adopt-spare` changes nothing of `ubuntu`'s keys, so a spare adopted later would bring the old key into the roster. `join_server.py` serves roster rows only, and the spares are recorded apart, in `spares.json`, with no row and no pinned key in `known_hosts`. So replace them instead, through each workshop's bot, now that step 4 has put the new key in `break-glass/`:
   1. `!teardown spares`: a destructive plan, which takes a global admin's approval, or two organizers', with the phrase `teardown <n>`. Its plan lists every spare it terminates; note how many. A workshop with no spare answers that nothing is selected: skip this.
   2. `!launch all spares=2`, with as many spares as the teardown listed in place of `2`. Its plan says `would run_instances …` for each spare, launched from `admin_keys`, now the new key, and `exists: … not launching` for each row's VM. The `WARNING --admin-key applies at first boot only` under it names those rows' VMs, which this step's `join_server.py` runs covered.
6. **The provisioner's key, before any other key.** `GET /bots` gives the provisioner the current API key of every bot it owns: every participant bot, and, through §5.3 and `!new-workshop`, every admin bot. So anyone with the old key and a copy of the bundle, which holds `provisioner.zuliprc`, reads each key regenerated before the provisioner's own changes. Open the bundle with the **old** key, which sealed it, and list the server's secrets:

   ```sh
   S=$(mktemp -d) && age -d -i ~/.ssh/zab-deployer.old ~/zab-admin/secrets.tar.age | tar -C "$S" -xf - && echo "$S"
   ssh -t "$HOST" sudo find /etc/zab-admin/secrets -type f      # every secret the server has
   ```

   Then §16's steps 1–4 for `provisioner.zuliprc` alone, into `$S`. From its step 1 on, the old provisioner key reads nothing.
7. **The participant bots' keys:** every past backup stays readable to the old key, with the participant bot keys it holds (ADMIN §1.7). Through each workshop's bot first, `!start all`: the rollout below needs each VM running, and a VM started now is up by then, which shortens the time its bridges hold dead keys. Then regenerate the keys, per workshop, on the server (`ssh -t "$HOST"`, then the block), while no plan is pending or applying, with the bot's own flags and the new provisioner key:

   ```sh
   cd /
   WS=<a workshop>; ORG=<its github_org>
   HOLD=--hold     # while its sandboxes are held (ORGANIZERS §3.3); HOLD= once they are released
   entry() { /opt/zab-admin/current/.venv/bin/python -I -c 'import sys, tomllib; w = {e["name"]: e for e in tomllib.load(open(sys.argv[1], "rb"))["workshop"]}[sys.argv[2]]; print(w.get(sys.argv[3], ""))' /etc/zab-admin/admin.toml "$WS" "$1"; }
   URL=$(entry refserve_url); GIFTS=$(entry gifts_step)   # as the bot passes them (ADMIN §2.1)
   sudo -u zab-admin /opt/zab-admin/current/.venv/bin/python -E -s -B -u \
     /opt/zab-admin/current/workshop/admin/zulip_provision.py --roster "/var/lib/zab-admin/ws/$WS/roster.csv" \
     --zuliprc /etc/zab-admin/secrets/provisioner.zuliprc --out-dir "/var/lib/zab-admin/ws/$WS/out" \
     --repo-owner "$ORG" --refserve-url "$URL" ${GIFTS:+--gifts-step "$GIFTS"} \
     --no-rebind $HOLD --regenerate-keys
   ```

   Without `--hold`, a held workshop would be released: its participants subscribed and greeted ("Start here"). Each row reads `codex: key regenerated, claude: key regenerated`. Then, through the workshop's bot: `!waker on all` where the waker is on, which copies the new keys to it, and a `!rollout` of the release it runs to `all`, which puts them on the VMs. Until the rollout, each VM's bridges hold the old keys, which no longer work: roll out at once.
8. **The other secrets:** §16's steps 1–4 for every other file the server listed in step 6: the GitHub token (a new one; §16's step 5 deletes the old), and every admin bot's key, `global.zuliprc` and each `bots/<name>.zuliprc`, those `!new-workshop` made on the server included, which go into `$S/secrets/bots/` as you replace them. Then §16's step 5 seals `$S` with §14's block, to `~/zab-admin/etc/backup_recipients`, which step 3 made the new recipients. The block opens the new bundle with `~/.ssh/zab-deployer`, now the new key, before it replaces the old bundle or removes anything: its `sealed` is the check that the new key opens it. Copy the new bundle to your other machine, and delete every older copy you know of.

   **Then delete the suspected private key,** on both machines. Nothing needs it any more; step 9 needs only its public half:

   ```sh
   rm ~/.ssh/zab-deployer.old
   ```
9. **Signing:** re-sign nothing you have pushed. `deploy.sh` checks the release the server runs, which is the review's base, against your `--etc` directory's `allowed_signers` (the server checks only the new tag, against its own file), and so does `--review … --since`. So the next release's review and upgrade refuse that base, `vX.Y.Z: not signed by a key in …/allowed_signers`, for now (a fix is tracked privately, #587). For that one release, give both a copy of `etc/` whose `allowed_signers` also holds the old key's line, and remove it after. The server's file never gets the old line back. Each `deploy.sh` line runs on its own (§11):

   ```sh
   EMAIL=<email>; RUNNING=$(ssh "$HOST" readlink /opt/zab-admin/current); RUNNING=${RUNNING##*/}
   E=$(mktemp -d) && cp -R ~/zab-admin/etc/. "$E"/ &&
     printf '%s namespaces="git" %s\n' "$EMAIL" "$(cut -d' ' -f1,2 ~/.ssh/zab-deployer.old.pub)" >> "$E/allowed_signers"
   ~/zab-"$V"/workshop/adminserver/deploy.sh --review "$C" --etc "$E" --since "$RUNNING" --out ~/zab-admin
   ~/zab-"$V"/workshop/adminserver/deploy.sh --host "$HOST" --etc "$E" "$V"
   rm -r "$E"
   ```

   `V` and `C` are the new release and its commit (§9). The upgrade prints `The server's allowed_signers differs from …/allowed_signers (- server, + yours)` with the old line marked `+`: that is expected, since the server's file has the new key alone.

   **From the public repository,** your tags were never pushed, so you may sign the running release's tag again instead, with the new key, on each machine. The review and the upgrade then need no copy of `etc/`. Since step 3, `git -C ~/zab tag -v "$RUNNING"` prints `Good "git" signature with ED25519 key SHA256:…`, then `No principal matched.`: the old key's signature, which your `allowed_signers` no longer lists. The block reads the tag's commit before it deletes the tag, and signs that commit again:

   ```sh
   RUNNING=$(ssh "$HOST" readlink /opt/zab-admin/current); RUNNING=${RUNNING##*/}
   RC=$(git -C ~/zab rev-parse "$RUNNING^{commit}") && echo "$RUNNING $RC"
   git -C ~/zab tag -d "$RUNNING" && git -C ~/zab tag -s "$RUNNING" -m "zab $RUNNING" "$RC"
   git -C ~/zab tag -v "$RUNNING"                 # Good "git" signature for <email> with ED25519 key …
   ```

   Run the third line only once the second has printed the release and a 40-digit commit. Should the `tag -s` fail after the `tag -d`, `git -C ~/zab fetch origin --tags` brings the public tag back, and §15's step 7 checks its commit against the server's record before you sign again.
10. **Then delete the old public half,** on both machines, once that upgrade is done: `rm ~/.ssh/zab-deployer.old.pub`.
