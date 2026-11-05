# The waker (#207; SPEC §13.4)

The waker starts a participant's stopped VM when a message from them reaches one of its bots, so
that a fleet can run idle stop and still answer (docs/ADMIN.md §11.2). It runs in one of two
modes, and its config decides which: `starter` and the admin server's file rule go together, and
a config with only one of them is refused.

- **Starter mode, on an admin server:** the usual setup, and the one to use when you have a
  server (below).
- **Direct mode, on a host of its own:** for a setup without an admin server; the rest of this
  page.

## Starter mode: on the admin server

`deploy.sh` installs and restarts the waker with every release of the server: there is nothing
to install by hand (`workshop/adminserver/README.md`; docs/ADMIN.md §1.2, §1.3).

- It runs as user `zab-waker`, with no AWS credential. It asks `zab-admin-starter`, over
  `/run/zab-admin-starter/start.sock`, for its VMs' states and for starts, and the starter
  checks each VM against its workshop in `admin.toml`. The bot's wake monitor alerts the global
  admins about any start that no message of the VM's participant explains.
- One waker serves every workshop of the server. Its config and the opted-in bots' zuliprcs are
  `zab-admin:zab-waker` 0640 under `/var/lib/zab-admin/waker/`.
- **Opting VMs in and out** is the organizers' `!waker on|off`, through their workshop's bot
  (docs/ORGANIZERS.md §3.5). The bot runs `push_waker.py --config-dir` (docs/ADMIN.md §3,
  action 5; the command by hand is in `workshop/adminserver/README.md`), and the waker restarts
  itself with the new config. A wake that fails is posted, with the participant's own bot, in
  the workshop's `#<ws>-fleet-alerts`.
- Turn the waker on before idle stop, on every VM, so that no VM stops without a way to wake
  it. One waker per bot: stop a waker in direct mode before the server's takes its bots.

## What it does, in either mode

`waker run` refreshes state every 30 seconds, skips running VMs before owner queries, waits up to
five minutes for stopping VMs, and shares a durable minute cooldown between both bots. Capacity
failures retry after 60 and 120 seconds. Every owner message in a channel that waited for the
wake gets a ⏰ from each bot that will answer it and allows its sender, in configured order: each
bot it mentions (#679), with Zulip's mention or a typed `@claude` or `@codex`, which names the bot
whose configured name starts `claude-` or `codex-` (#713); with no mention, each bot, as the bridge
sends such a message to each of the VM's bots, whatever its topic's earlier messages addressed
(#713; #683's topic-history lookup went). The waker can't see a channel with other people, an
agent not signed in or a detached session, where a bot stays silent: there too, each bot gives a
⏰. The owner's DMs of one wake share one "Starting your VM" DM (SPEC §13.4.2).
No message body is persisted or logged, and `httpx` logs warnings only. A waker that starts
catches up on the messages its bots received while it was down (SPEC §13.4.3).

Allowlist rules match the bridge. At least one participant ID or e-mail is required per VM;
a hidden or unresolved e-mail fails closed.

## Direct mode: a host of its own

In direct mode the waker's files are root-owned 0600 under `/etc/zab-waker/`, and it starts VMs
with boto3 and its host's instance role. One waker serves one workshop. The deployer runs
`push_waker.py` from a checkout on the waker's host. The IAM files here name the host's role
`zab-files-host` and the workshop `nt-agents-2026`: the first deployment ran its waker on its
files host, for that workshop. Pass your own workshop everywhere, as shown.

When the VMs are in the host's own account, give the host an instance profile with
`iam-policy.json`, as in *VMs in the host's account* below: DescribeInstances is read-only on all
resources; StartInstances is limited to instances tagged with the workshop. When they are in
another account, see *Waking VMs in another account* below. The script creates no IAM objects or
VMs. The service additionally restricts starts to explicitly selected roster VMs and rechecks
tags. One host can serve VMs in several regions; each instance routes to its configured region.
Describe pagination is followed.

Stage the roster, the wheel of the release the VMs run, and only the selected bots' zuliprcs on
the host, by your own secure transfer. Never place agent login files there. The script takes
`--keys` pointing at directories shaped `<slug>/codex.zuliprc` and `<slug>/claude.zuliprc`.

```sh
sudo /opt/zab-waker/bin/python workshop/admin/push_waker.py \
  --roster /root/roster.csv --keys /root/bot-keys \
  --wheel /root/zulip_agent_bridge-X.Y.Z-py3-none-any.whl \
  --workshop <WS> --only <slug> --organizers-channel <channel ID>
# Inspect the dry run, then repeat with --apply.
```

Copy the wheel under its real name, as `uv build` wrote it: pip refuses a file renamed to
something like `zab.whl`.

On first install, create `/opt/zab-waker` with `python3 -m venv` and install this folder's
`requirements.txt` there so the operator script can describe EC2. The dry run performs read-only
EC2 discovery and prints config, never bot-file contents. `--only` is mandatory and repeatable.
Selected entries are added or updated; existing opt-ins are retained. No VM is started by
deployment itself. `--apply` must run as root: it installs root-owned 0600 zuliprcs and config
under 0700 `/etc/zab-waker`, installs the wheel and waker-only boto3 dependency, validates
config, and enables/restarts `zab-waker.service`. It does not change participant VMs.

**`--workshop <WS>`, every time:** the VMs' `workshop` tag value, used for the deployment lookup
and written to the config's `workshop` key, which the service filters on. Its default,
`nt-agents-2026`, is the first deployment's. A config installed for another workshop is refused
rather than mixed. The test waker (issue #263) runs with `--workshop zab-test` and an instance
profile `zab-test-waker` whose policy is `iam-policy-test.json`, used as it is: StartInstances
only on instances tagged both `workshop=zab-test` and `zab-role=test` (one `StringEquals`, so IAM
requires both), the limit for `zab-test-*` roles: they act only on test infrastructure. Don't
derive it from `iam-policy.json` by hand.

The rendered TOML has `organizers_channel`, `workshop`, `state_dir`, and `[[bots]]` entries:
`name`, `instance_id`, `region`, `zuliprc`, `allow_user_ids`, `allow_emails`. Config may use a
top-level `region` as the default. The runtime rejects non-root or non-0600 credential files.

`sudo /opt/zab-waker/bin/waker status` reports cached states, observation time, and last wake
attempts without AWS or credential reads.

**Acceptance, on one VM first** (your own): stop it, then test an owner DM, a new-topic mention,
a typed `@claude` (its ⏰ from Claude alone), a new topic without a mention and an existing-topic
follow-up without one (each with a ⏰ from each bot); measure first reply against
the 45-second target. Check a nonowner cannot wake it and both bots wake it only once. Verify
catch-up executes the recent triggering input once. Inspect journal and `waker status`. Only
after that test opt in the other VMs and turn their idle stop on.

### VMs in the host's account

One role, `zab-files-host`, holding `iam-policy.json` with your workshop's tag, on the host's
instance profile, and no `--assume-role`. From a checkout's root, with an admin profile for the
account (`$P`), the host's region (`$R`) and instance ID (`$HOST_ID`; zsh presets `$HOST` to
the host name):

```sh
sed "s/nt-agents-2026/<WS>/" workshop/waker/iam-policy.json > /tmp/waker.json
aws --profile $P iam create-role --role-name zab-files-host \
  --assume-role-policy-document file://workshop/waker/trust-ec2.json
aws --profile $P iam put-role-policy --role-name zab-files-host \
  --policy-name zab-waker --policy-document file:///tmp/waker.json
aws --profile $P iam create-instance-profile --instance-profile-name zab-files-host
aws --profile $P iam add-role-to-instance-profile \
  --instance-profile-name zab-files-host --role-name zab-files-host
aws --profile $P ec2 associate-iam-instance-profile --region $R \
  --instance-id $HOST_ID --iam-instance-profile Name=zab-files-host
```

A new profile takes a few seconds to be accepted: rerun only the last command after an
"Invalid IAM Instance Profile name". If the host already has a profile
(`describe-iam-instance-profile-associations`, as in step 1 below), put the policy on its role
instead and create nothing. *Check* on the host with the IMDSv2 command of step 1 below, then
deploy with the command above and `--check` (step 4 below), without `--assume-role`.

**Undo:** `sudo systemctl disable --now zab-waker` on the host, then:
- **If the host had a profile before**, leave that profile and its role alone: `aws
  --profile $P iam delete-role-policy --role-name <that role> --policy-name zab-waker`,
  nothing else.
- **Otherwise**, the association ID from `aws --profile $P ec2
  describe-iam-instance-profile-associations --region $R --filters
  Name=instance-id,Values=$HOST_ID`, then `ec2 disassociate-iam-instance-profile --region
  $R --association-id <ID>`, `iam remove-role-from-instance-profile
  --instance-profile-name zab-files-host --role-name zab-files-host`, `iam
  delete-instance-profile --instance-profile-name zab-files-host`, `iam delete-role-policy
  --role-name zab-files-host --policy-name zab-waker`, `iam delete-role --role-name
  zab-files-host`.

### Waking VMs in another account (#207)

For a workshop whose VMs are in another account than the waker's host. The host's role may only
assume a role in the fleet account; that role holds `iam-policy.json` with its workshop's tag
(step 2) and trusts only the host's role; and `zab-waker.service` assumes it through an AWS
profile. Run the commands from a checkout's root, on a machine with an admin profile for each
account (`files` and `fleet` below stand for their names), or do the same in the IAM console. The
account IDs come from the profiles; `<HOST_REGION>` and `<HOST_INSTANCE_ID>` are the host's:

```sh
FILES=$(aws --profile files sts get-caller-identity --query Account --output text)
FLEET=$(aws --profile fleet sts get-caller-identity --query Account --output text)
```

**1. The host's account: a role for the host.** First look for a profile already on the
instance: `aws --profile files ec2 describe-iam-instance-profile-associations --region
<HOST_REGION> --filters Name=instance-id,Values=<HOST_INSTANCE_ID>`. If it has one, run only the
`sed` and `put-role-policy` lines below, on its role instead of `zab-files-host`, put that role's
name for `zab-files-host` in step 2's trust policy, and note that the profile was there before:
the undo must leave it.

```sh
aws --profile files iam create-role --role-name zab-files-host \
  --assume-role-policy-document file://workshop/waker/trust-ec2.json
sed "s/<FLEET_ACCOUNT_ID>/$FLEET/" workshop/waker/files-host-policy.json > /tmp/host.json
aws --profile files iam put-role-policy --role-name zab-files-host \
  --policy-name zab-assume-waker --policy-document file:///tmp/host.json
aws --profile files iam create-instance-profile --instance-profile-name zab-files-host
aws --profile files iam add-role-to-instance-profile \
  --instance-profile-name zab-files-host --role-name zab-files-host
aws --profile files ec2 associate-iam-instance-profile --region <HOST_REGION> \
  --instance-id <HOST_INSTANCE_ID> --iam-instance-profile Name=zab-files-host
```

Its only permission is `sts:AssumeRole` on `zab-waker-*` roles in the fleet account.
The profile attaches to the running instance, without a reboot; a new profile can take
a few seconds to be accepted, so retry an "Invalid IAM Instance Profile name". *Check*,
on the host (IMDSv2; prints no keys):
`T=$(curl -sX PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'); curl -sH "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/iam/info`
names `instance-profile/zab-files-host`.

**2. Fleet account: the role the waker assumes.** `iam-policy.json` with the workshop's
`workshop` tag value (`<WS>` below) in place of `nt-agents-2026`: the policy lets
the role start only VMs carrying that tag.

```sh
sed "s/<FILES_HOST_ACCOUNT_ID>/$FILES/" workshop/waker/trust-fleet-role.json > /tmp/trust.json
sed "s/nt-agents-2026/<WS>/" workshop/waker/iam-policy.json > /tmp/waker.json
aws --profile fleet iam create-role --role-name zab-waker-<WS> \
  --assume-role-policy-document file:///tmp/trust.json
aws --profile fleet iam put-role-policy --role-name zab-waker-<WS> \
  --policy-name zab-waker --policy-document file:///tmp/waker.json
```

The trust names the host's role, not its account, so nothing else in the host's account can
assume it; IAM refuses it until step 1's role exists. *Check:* `aws --profile
fleet iam get-role --role-name zab-waker-<WS> --query Role.Arn`.

**3. The host: the service assumes the role.** Add to the command above
`--assume-role arn:aws:iam::<FLEET_ACCOUNT_ID>:role/zab-waker-<WS>`. The dry
run already looks the VMs up through the role, and prints the profile and
`AWS identity: arn:aws:sts::<FLEET_ACCOUNT_ID>:assumed-role/zab-waker-<WS>/zab-waker`.
`--apply` writes the profile to `/etc/zab-waker/aws-config` (root, 0600) and a drop-in,
`/etc/systemd/system/zab-waker.service.d/assume-role.conf`, that sets `AWS_CONFIG_FILE`
and `AWS_PROFILE` for the service, then reloads systemd and restarts it. Later runs keep
the role without the flag; `--no-assume-role` removes both files. On a waker already
deployed, either flag alone (`sudo /opt/zab-waker/bin/python workshop/admin/push_waker.py
--assume-role ARN`, then with `--apply`) changes only these two files, after proving the
identity through the profile, and restarts the service only if it runs: no roster, keys
or wheel needed. The ARN is the one step 2's check printed.

The profile is `role_arn` with `credential_source = Ec2InstanceMetadata`: botocore's
`AssumeRoleProvider` (checked in 1.35 and 1.43, and against fake endpoints) takes the
instance profile's credentials over IMDSv2, calls AssumeRole as session `zab-waker` in
the first bot entry's region (the profile sets none), and renews the one-hour credentials.

**4. Check, before enabling idle stop:**
`sudo /opt/zab-waker/bin/python workshop/admin/push_waker.py --check`, read-only, uses
the service's environment from systemd. It prints the role, the identity, the VMs tagged
with the workshop by state, and `allowed (dry run)` for each opted-in VM (StartInstances
with `DryRun`: nothing starts), then `check: OK`. A refusal is reported, e.g.
`<region>: cannot list VMs: UnauthorizedOperation` with a hint, then `check: FAILED`
and exit 2.

**ExternalId: not recommended.** It guards against a confused deputy (a third party
assuming roles for many customers); this trust names one role that only the deployer controls.
If wanted anyway: `"Condition": {"StringEquals": {"sts:ExternalId": "<ID>"}}` in the
trust policy and `external_id = <ID>` in `/etc/zab-waker/aws-config`.

**Undo**, in this order:
1. Stop waking: `sudo systemctl disable --now zab-waker`.
2. Revoke, in the fleet account (takes effect at once): `aws --profile fleet iam
   delete-role-policy --role-name zab-waker-<WS> --policy-name zab-waker`, then
   `aws --profile fleet iam delete-role --role-name zab-waker-<WS>`.
3. On the host: `sudo /opt/zab-waker/bin/python workshop/admin/push_waker.py
   --no-assume-role --apply`. It removes the profile and the drop-in without calling AWS,
   so it works at any point. A stopped waker stays stopped (`try-restart`).
4. The host's role:
   - **If step 1 found a profile already there**, leave that profile and role alone:
     `aws --profile files iam delete-role-policy --role-name <that role> --policy-name
     zab-assume-waker`, nothing else.
   - **Otherwise**, look up the association ID with step 1's `describe-iam-instance-profile-associations`
     command (`--query 'IamInstanceProfileAssociations[0].AssociationId'`), then:
     `aws --profile files ec2 disassociate-iam-instance-profile --region <HOST_REGION>
     --association-id <ID>`; `iam remove-role-from-instance-profile
     --instance-profile-name zab-files-host --role-name zab-files-host`;
     `iam delete-instance-profile --instance-profile-name zab-files-host`;
     `iam delete-role-policy --role-name zab-files-host --policy-name zab-assume-waker`;
     `iam delete-role --role-name zab-files-host`.

A recreated `zab-files-host` needs step 2's trust policy saved again (`iam
update-assume-role-policy`): IAM keeps the deleted role's ID.

### Proving it on the test rig

The test rig's variant, in the test account, with the test waker (`zab-test-waker`
instance and role) as the host and `zab-test-waker-target` as the fleet role:

```sh
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
for f in trust-fleet-role-test files-host-policy-test; do
  sed "s/<ACCOUNT_ID>/$ACCOUNT/" workshop/waker/$f.json > /tmp/$f.json
done
aws iam create-role --role-name zab-test-waker-target --tags Key=zab-role,Value=test \
  --assume-role-policy-document file:///tmp/trust-fleet-role-test.json
aws iam put-role-policy --role-name zab-test-waker-target --policy-name zab-test-waker \
  --policy-document file://workshop/waker/iam-policy-test.json
aws iam put-role-policy --role-name zab-test-waker --policy-name zab-test-assume \
  --policy-document file:///tmp/files-host-policy-test.json
```

`files-host-policy-test.json` lets `zab-test-waker` assume only this account's
`zab-test-*` roles tagged `zab-role=test`, test infrastructure only. `zab-test-waker` should hold only that statement during
the run, so that only the assumed role can start VMs: delete its EC2 policy (`iam list-role-policies`), and put
`iam-policy-test.json` back afterwards. Then, on the deployed test waker, run
`push_waker.py --assume-role arn:aws:iam::<ACCOUNT_ID>:role/zab-test-waker-target`,
the same with `--apply`, `--check`, and the end-to-end suite's scenario 10
(`tests/e2e/test_scenarios_slow.py`).

The test trust names the account (`:root`), not `zab-test-waker`: a trust naming the role
would by itself let it assume the role within one account. Naming the account delegates
to identity policies, so the AssumeRole needs `files-host-policy-test.json`, as across
accounts. The rig then also exercises the host's half (its test variant, with the tag
condition). The cost: the account's admins may assume it too, and it can only start
`zab-test` VMs. An existing `zab-test-waker-target` gets the new trust with `aws iam
update-assume-role-policy --role-name zab-test-waker-target --policy-document
file:///tmp/trust-fleet-role-test.json`.
