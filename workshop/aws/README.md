# Workshop VMs

The scripts that create and manage the participants' VMs: `launch.py`, `power.py`, `protect.py`,
`adopt_spare.py`, `grow_disk.py`, `teardown.py` and `import_key_pair.py`. They are not part of the
`zab` package, and need `boto3`, which is not a zab dependency.

**The admin bot runs them.** On an admin server ([`docs/BOOTSTRAP.md`](../../docs/BOOTSTRAP.md)),
each workshop's bot runs them on the server, with the server's instance role and the workshop's
entry in `admin.toml`: its region, its scope tags (`workshop=<ws>` and `zab-scope=<SCOPE>`), its
image and security group, its key pair `zab-admin-<ws>`, and the deployers' break-glass keys.
Organizers send the commands; they never run a script.

| The bot's command | Runs |
|---|---|
| `!launch` | `launch.py`, then `scan_hosts.py` |
| `!start`, `!stop` | `power.py` |
| `!protect on`, `!protect off` | `protect.py` |
| `!adopt-spare` | `adopt_spare.py`, then the rest of the hand-over ([ORGANIZERS §7.2](../../docs/ORGANIZERS.md)) |
| `!grow-disk` | `grow_disk.py` |
| `!teardown` | `teardown.py` |
| `!retire` | `power.py`, `protect.py --off` and `teardown.py --strict`, among its steps |
| `!new-workshop`, on the server's bot, `Workshop admin` | `import_key_pair.py`'s import, for the new workshop's key pair |

`deploy.sh --bootstrap` runs `import_key_pair.py` itself, for every workshop of `admin.toml`: the
first one, at a bootstrap.

**A laptop is the fallback.** When the bot can't ([ORGANIZERS §7](../../docs/ORGANIZERS.md)), a
deployer runs the same scripts from a laptop, with an AWS profile for the fleets' account, and
with the bot's settings, so that the VMs stay ones the bot can manage: `<ws>`, `<SCOPE>`,
`<region>`, `<ami>`, `<sg>` and `<type>` (the first of its `instance_types`) are the workshop's
entry in the server's `admin.toml` (BOOTSTRAP §7.2). The commands below are that fallback's. Every script takes `--tag` and `--region`, and
refuses to run without them (#808): there is no default workshop or region.

```bash
AWS_PROFILE=<aws-profile> uv run --with boto3 python workshop/aws/launch.py --roster roster.csv \
    --region <region> --ami <ami> --security-group-id <sg> --instance-type <type> --key-name zab-admin-<ws> \
    --admin-key ~/zab-admin/etc/break-glass/zab-deployer.pub --tag workshop=<ws> --tag zab-scope=<SCOPE> \
    --spares 2 --dry-run                                     # look first; then without --dry-run
AWS_PROFILE=<aws-profile> uv run --with boto3 python workshop/aws/teardown.py --region <region> \
    --tag workshop=<ws> --tag zab-scope=<SCOPE> [--only <slug> …] [--dry-run] [--yes] \
    [--roster roster.csv]                     # clears the ip of each row whose address it releases
    [--spares-file out/spares.json]           # drops the spares at an address it releases (#607)
```

`--key-name zab-admin-<ws>` is the key pair the server imported from its fleet key, so the server
reaches a VM launched by hand as it does its own. Give one `--admin-key` per entry of the workshop's
`admin_keys`, as the bot does: those are files of the server's `/etc/zab-admin/break-glass/`, and
your copies are in `~/zab-admin/etc/break-glass/` (BOOTSTRAP §8.3).
Without an admin server, `--key-name` names a key pair of your own, such as the dedicated key's
(BOOTSTRAP §3.3), and `--tag workshop=<ws>` alone is the scope.

`--only SLUG` is repeatable in `launch.py`, `teardown.py` and `protect.py`, and names a roster
slug or a spare (`zab-spare-N`). `launch.py` refuses a name that is neither, before any AWS
call; a spare needs `--spares N` with N at least its number.

**Only participant VMs and spares are ever selected.** Even with no `--only`, `teardown.py`
skips, and lists as `skip`, any instance or Elastic IP that carries the scope tags but no
`participant` tag, and `protect.py` does the same for instances (it never touches addresses).
That covers the admin server, should someone tag it `workshop=…` by mistake (docs/ADMIN.md
§1.1, §3.2). `launch.py` and `adopt_spare.py` look every resource up by its `participant` tag
already. Every script (`power.py` too) also leaves alone anything tagged `zab-admin`, even with
a `participant` tag: the server's role denies it to the bot, and this covers a manual run with
another credential.

`tests/workshop/` exercises the scripts with a fake EC2.

## The VMs

- **Instance:** `m7a.2xlarge` (DECISIONS D23), `--instance-type`: 8 vCPUs, each a physical Zen 4
  core with AVX-512, and 32 GiB of memory. Every process that imports Mathlib holds about
  7.3 GB, mostly shared pages. Each agent runs up to 10 turns at once (`max_concurrent_turns`,
  #919), and a turn that uses Lean starts a Lean server of its own, about 1.3 GB idle with one
  small file open: with every turn in Lean at once, those and the agents' other processes fill
  the VM's usable memory and run into the 8 GiB swap (SPEC §7.10). The common case, turns that
  wait on the model, needs much less; Sage, Magma and comparator runs come on top. Where the
  region's vCPU quota is short,
  `--instance-type r7a.xlarge` keeps the 32 GiB on 4 vCPUs: the image is the same (x86-64), and
  `build_ami.py --smoke-only <ami> --smoke-instance-type r7a.xlarge` checks it on that type.
- **Root volume:** gp3, 200 GiB (#205), deleted on termination.
- **Metadata:** IMDSv2 required. **No instance profile or IAM role,** so the agents on a VM can't
  reach AWS.
- **Network:** the default VPC; a security group that admits TCP 22 alone (below), outbound
  unrestricted; SSH by key only. One Elastic IP per VM, so that a stop and start, for example to
  resize a VM, keeps its address, and the roster's `ip` and the pinned host keys stay valid.
- **Tags:** `Name=ws-<slug>`, `participant=<slug>` and the scope tags (`--tag`).
- **Quotas:** each running `m7a.2xlarge` counts 8 vCPUs against the region's quota for running
  On-Demand standard instances, and every VM holds an Elastic IP, running or stopped. Both
  quotas are per region, and an increase can take days: BOOTSTRAP §6.4 reads and requests them.

## Before the launch: the security group

The fleet's group, `zab-fleet`, is made once per region by the account owner, and tagged with the
fleet's scope, `zab-scope=<SCOPE>`, which is how `!new-workshop` finds it: BOOTSTRAP §6.1 has the
commands. `--security-group-id` is required, and `launch.py` refuses the VPC default group and any
group whose inbound rules are not exactly TCP 22 (`validate_security_group`): the default group
admits no SSH from the internet (nobody could log in) while letting every VM reach every other, and
the agents have sudo. Outbound stays unrestricted (the default egress rule), as "The VMs" says.

## Several scope tags

`--tag KEY=VALUE` is repeatable in every script here. Every resource a script creates (instance,
root volume, Elastic IP, key pair) carries every scope tag, and every lookup requires all of them,
so a resource that has only one of them is out of scope. The admin server's VMs carry two,
`workshop=<ws>` and `zab-scope=<SCOPE>`; a test VM carries `zab-role=test` among its own. `Name` and
`participant` are set per participant and can't be scope tags. `--ssh-from CIDR` (repeatable)
makes `launch.py` refuse a security group whose TCP 22 admits any other source. A test VM, with
termination protection off (no `--protect`), from a roster of its own:

```bash
uv run --with boto3 python workshop/aws/launch.py --roster test-roster.csv \
    --region <region> --ami <ami> --security-group-id <sg> --key-name <key-pair> --only <slug> \
    --tag zab-role=test --tag workshop=<test-ws> --ssh-from <your address>/32
```

## launch.py

- Reads the roster (`slug,first_name,last_name,email,repo,github,host,ip,region,zulip_user_id,…`;
  `workshop/admin/README.md` §4)
  as `utf-8-sig` with lower-cased header names (a spreadsheet's byte-order mark or a `Slug`
  header would otherwise hide every participant). The **slug** names everything (a roster
  from before the column takes the GitHub login in lower case). It refuses duplicate slugs or
  e-mail addresses (case-insensitively; a duplicate slug would give the second row the first
  row's VM) and validates them before they reach a tag or the first-boot script.
- **No GitHub** (issue #36): participants get no SSH, so the launch makes no GitHub
  request, checks no profile name and imports no keys. The roster's `github`, `github_id` and
  `github_id_login` columns are ignored even when present (`provision_repos.py` writes
  `github` now, for the repository invitation), so a relaunch or a spare never starts importing
  keys. `--allow-name-mismatch` and `--verify-logins` are gone.
- **Dry run first.** `--dry-run` runs only queries and prints what it would do. A slug edited
  since the last run leaves the previous VM running: the summary names it for
  `teardown.py --only`.
- Regions: the roster's `region` column records where each VM is (written on launch or when
  the VM is found). A run with another `--region` leaves such rows alone, as it does rows from
  before the column (`host` = `ws-<slug>`) whose VM it cannot see; fill in `region` beforehand
  to assign rows to a second region ("Regions", below).
- Idempotent: a row whose `participant` tag already has a live instance **in this workshop** is
  not launched again; its Elastic IP is reconciled instead (allocated or attached if a previous
  run died in between). Resources of another event, even with the same login, are ignored.
- Launches from the AMI with the settings of "The VMs": `m7a.2xlarge` (8 vCPUs, 32 GiB; D23), 200 GiB
  gp3 root (issue #205) deleted on termination, IMDSv2 required, no IAM role, tags `Name=ws-<slug>`,
  `participant=<slug>` and the scope tags, and the first-boot script
  (`workshop/ami/README.md`, "First boot"): host name,
  git identity (the roster names and e-mail; a row without an e-mail fails), `~/work`,
  `/etc/zab-participant.json` (slug and checkout), then the post-AMI decisions (an 8 GiB
  swap file, Claude Code's auto-updater off through `/etc/claude-code/managed-settings.json`,
  Codex's `check_for_update_on_startup = false`, the LMFDB MCP server for both CLIs; all
  idempotent, mirrored in `workshop/ami/build.sh`). No key import and no cron job:
  participants get no SSH, organizers keep `ubuntu` with their own keys. A participant who
  asks for SSH gets it by hand, as `ubuntu`: `sudo -u agent ssh-import-id gh:<login>`. Check the
  login with them first: whoever owns it gets a shell with sudo.
- `--spares N` launches `zab-spare-1 … zab-spare-N`, VMs held ready for a late joiner or a
  broken VM. The bot's `!adopt-spare` hands one over (ORGANIZERS §4.1), or a deployer by hand
  (ORGANIZERS §7.2), with `push_config.py --adopt-spare` among the steps. They are
  tagged `participant=zab-spare-N` and are not written to the roster; a roster slug of that
  form is refused so that no participant can share a spare's tags. For a late joiner, whose row
  has no VM, `adopt_spare.py --roster roster.csv --slug <slug> --spare zab-spare-N`, with the
  scope tags and region, first
  retags the spare's instance, volume and Elastic IP as the participant's (`Name` and
  `participant` only: the scope tags are already there, and the admin server's role refuses a
  retag naming any other key, docs/ADMIN.md §5), fills the row's
  `host`, `ip`, `region` and `host_keys` from `spares.json`, and marks the spare adopted there
  (`--dry-run` changes nothing). It refuses a spare that isn't `running`, or whose port 22
  doesn't answer, before changing anything (#455): start it first (`power.py --start --wait
  --only zab-spare-N`, with the scope tags and region, waits for its port 22). `--spares N` means "make sure `zab-spare-1` …
  `zab-spare-N` exist", not a total: after a hand-over, run `launch.py` without `--spares`
  unless a replacement for the adopted spare is wanted (it would be launched under the same
  name).
- Errors are handled per row (a row without an e-mail, a failed allocation) and summarized at
  the end; the exit code is 1 when any row failed. The roster is always written back, atomically,
  with the previous version kept as `roster.csv.bak`; every row and column is preserved.
- `--admin-key FILE` (repeatable): more public keys for `ubuntu`, next to the `--key-name` key
  pair's, appended at first boot (unless already there) on participant VMs and spares alike.
  Each file must hold exactly one OpenSSH public key (no options, no private key; a comment
  with spaces is fine). Only new VMs get them: a run that reuses existing VMs lists them in a
  warning with their addresses; append the key by hand there (`ssh ubuntu@<ip> 'cat >>
  ~/.ssh/authorized_keys' < key.pub`) or relaunch them.
- **Host keys.** After the launches, the script reads each new VM's console
  (`get_console_output(Latest=True)`: on Nitro the default output stays empty) until cloud-init's
  `BEGIN/END SSH HOST KEY FINGERPRINTS` block appears (up to 15 minutes in all; it is printed at
  the end of the first boot), and records the fingerprints in the roster's `host_keys` column;
  `scan_hosts.py` checks the scanned keys against them. An existing VM without `host_keys` is
  read once on a rerun (cloud-init prints the block once per instance, so after a stop and
  start it is gone). A spare's fingerprints are printed in the summary and kept, with its
  address, region and instance id, in `workshop/admin/out/spares.json` (git-ignored, 0600), so
  a hand-over days after the launch does not depend on its terminal. Entries are keyed by
  `<workshop>/<region>/<name>`, since each region can have its own `zab-spare-1`:
  `{"<ws>/<region>/zab-spare-1": {"name", "workshop", "region", "instance_id", "ip",
  "host_keys": "SHA256:… SHA256:…", "updated"}}`. On a rerun, an entry keeps its earlier
  fingerprints and any other field, such as the `adopted_by` mark of a hand-over by hand, only
  for the same instance (a replacement can reuse the tagged Elastic IP); and another entry of the
  scope and region at the address it records, a spare torn down whose address EC2 gave this
  one, is dropped (#607). `teardown.py --spares-file` drops the entries at the addresses it
  releases. `--spares-file FILE`
  puts it elsewhere (the admin server keeps it outside the read-only release tree; pass the
  same file to `adopt_spare.py --spares-file`).
- Allocates an Elastic IP per VM, associates it, waits for port 22, writes `host`, `ip` and
  `host_keys` back to the roster, and prints an `~/.ssh/config` snippet (`User ubuntu`) per participant and
  spare.

Prerequisites: the quotas of "The VMs", the region's group and image (BOOTSTRAP §6.1; the
image is built with [`workshop/ami/`](../ami/README.md)), the key pair for the `ubuntu` admin user
(participants have no SSH), and the break-glass keys.

## Regions

`launch.py` works in one region (`--region`) and sees only that region's
instances, so the roster records each VM's region in its `region` column, written when a VM is
launched or found. A row whose `region` names another region is **left alone**, reported as
"other region", not as an error; so is a row whose `host` says `ws-<slug>` exists but whose VM is
not in this region: it was launched elsewhere, or it is gone. Run `launch.py` in its region to
record it, or clear `host` and `ip` when its VM is really gone, which is also how a VM is replaced
after `teardown.py`. Filling in a row's `region` before its first launch assigns it to that
region. **Never change a recorded region**: `roster_import.py` refuses it for a row with a VM.

The admin server serves each workshop in one region, the region of its entry: its bot's scripts
run there alone. A fleet split over two regions is managed by hand, and needs, in the second one:

1. the fleet's image and security group, with their scope tags (BOOTSTRAP §6.1; a new region end
   to end is BOOTSTRAP §6.5), and the workshop's key pair, `zab-admin-<ws>`, imported there with
   `import_key_pair.py` from the server's fleet key (below);
2. its own vCPU and Elastic IP quotas ("The VMs");
3. `region` written in the rows that go there, before their first launch, and only in rows
   without a VM yet (empty `host`);
4. a launch there with a credential allowed in that region: `launch.py --region <region> --ami
   <its image> --security-group-id <its group>`, with the same `--key-name`, `--admin-key` and
   scope `--tag`s as the first region's launch. It launches that region's rows and leaves the others alone, as the
   first region's runs leave these alone.

The admin scripts after the launch (`scan_hosts`, `zulip_provision`, `provision_repos`,
`push_config`, `status`) work from the `ip` column and don't care about regions. `teardown.py`,
`protect.py` and `power.py` take `--region` and run once per region.

## Stopping and starting the fleet: power.py

Idle stop and the waker stop and start each VM by itself (ORGANIZERS §4.2). `power.py`, behind
`!start` and `!stop`, stops or starts a selection at once, such as the whole fleet between
provisioning and the workshop's first morning. `S` holds the scope and region, as above:

```bash
S=(--region <region> --tag workshop=<ws> --tag zab-scope=<SCOPE>)
uv run --with boto3 python workshop/aws/power.py --roster roster.csv "${S[@]}" --stop --all --spares \
    [--wait] --dry-run                                # look first; then without --dry-run
uv run --with boto3 python workshop/aws/power.py --roster roster.csv "${S[@]}" --start --all --spares --wait
uv run --with boto3 python workshop/aws/power.py --roster roster.csv "${S[@]}" --start --only <slug> …
```

- **What it selects: always named, never implicitly everyone** (docs/ADMIN.md §2.2):
  - `--all`: every roster row with an `ip` in the region, never a spare. Rows without a VM
    and rows in another region are listed as `skip`.
  - `--spares`: every spare of the scope and region in `spares.json` that is neither adopted
    nor handed over (its address in a roster row); the others are listed as `skip`.
  - Both together: the whole fleet, as the overnight stop wants.
  - `--only` (repeatable, slugs or `zab-spare-N`), alone: those names. A name that can't be
    served is refused before any AWS call: no `ip`, another region, unknown, an adopted
    spare, or a spare whose address a row now uses.
  - Two roster rows with one `ip` are a roster error, since either would act on the other's
    VM: refused under `--only`, and otherwise listed as `skip` and counted as failures.
  - A `--spares-file` that is named must exist when spares are selected (`--spares`, or a
    `zab-spare-N`). A selection of rows needs no file, and a missing one reads as empty:
    `launch.py` writes it only when it launches spares, and the admin bot always names it. An
    existing file is still read, and a corrupt one still refuses the run.
- **How it finds the VMs.** A row's VM is the instance at the row's `ip`, as `status.py` finds
  it. A spare's VM is the instance at its address in `spares.json`, and it must carry the
  spare's own tag. Only instances with every scope tag **and** a `participant` tag, and no
  `zab-admin` tag, are candidates, so the admin server is never started or stopped. An
  instance tagged as another participant is refused (a typo in `ip`).
- **The region.** `--region`, required (#808). A row with a VM and an empty `region` cell is one
  launched before `launch.py` recorded it, in `us-west-2`, as `status.py` reads it.
- **Idempotent.** It prints one line per target and a summary. A VM moving the other way
  (`stopping` for `--start`) fails and the others still run; the exit code is then 1.
  `--dry-run` reads EC2 and prints the same lines with "would". A failed EC2 lookup (an
  expired credential) is one `FAILED` line, as in `teardown.py` and `protect.py`.
- **`--wait`** waits until each VM it serves is stopped, or running with port 22 answering,
  whether this run started it or it was already on its way (`!retire` waits so for each row
  before it syncs the row's work).
  - **One deadline for all of them** (#334), 10 minutes after the last request. In each round,
    one EC2 lookup reads every VM's state, and the port 22 of each running VM is tried, all in
    parallel; the next round starts 5 s after one ends. Every VM is read at least once
    (unless the first lookup fails or outlasts the deadline), and tried whenever it runs.
  - **The bound.** The rounds stop at the deadline, so `--wait` takes at most 10 minutes plus
    one last round, however many VMs fail. That round is:
    - one EC2 lookup, which asks for no further page once the deadline has passed, and whose
      every request gives up within about a minute (power.py's client: two tries, each at most
      10 s to connect and 20 s to read). A lookup cut short (or failed) fails every VM
      still waiting;
    - 5 s of SSH tries per 128 running VMs whose port 22 is silent.

    For up to 128 such VMs that is at most about 11 minutes, below the admin bot's 15-minute
    timeout (docs/ADMIN.md §2.4).
  - **Failures.** A VM not there by the deadline fails, as does one moving the other way
    (`stopping` after a start, `pending` after a stop) or gone. It counts as `failed` only,
    never also as `start`, `stop` or `unchanged`, so the summary counts each VM once.

`power.py` needs `ec2:DescribeInstances`, `ec2:StartInstances` and `ec2:StopInstances`. By
hand, without the script:

- **Console:** EC2 → Instances, filter by the tags `workshop` = `<ws>` and `zab-scope` =
  `<SCOPE>`, select all, Instance state → Stop (or Start). Once per region the roster uses.
- **CLI**, per region. The `participant` filter keeps out anything that isn't a participant's VM
  or a spare:

  ```bash
  ids=$(aws ec2 describe-instances --region <region> \
        --filters Name=tag:workshop,Values=<ws> Name=tag:zab-scope,Values=<SCOPE> \
                  Name=tag-key,Values=participant Name=instance-state-name,Values=running \
        --query 'Reservations[].Instances[].InstanceId' --output text)
  aws ec2 stop-instances --region <region> --instance-ids $ids
  # start: the same with Values=stopped and start-instances; one VM: add
  # Name=tag:participant,Values=<slug> to the filters
  ```

What survives a stop and start: the Elastic IP (so the roster's `ip` and the pinned
`known_hosts` stay valid), the host keys, `/etc/zab-participant.json`, the hostname and the EBS
volume, with everything on it. Cloud-init's per-instance modules do not run again (the first-boot
script, the host-key fingerprints on the console). `zab` and the Codex app server are
`systemctl --user` units of `agent`, who lingers, so they come back with the VM: check that on
one VM after the first rollout. A stopped VM costs only its volume and its Elastic IP.

Run it with a credential for the fleets' account that may start and stop its instances
(`AWS_PROFILE=<aws-profile>`).

## The spares' own idle stop: spare_idle_stop.py (#715)

A spare gets neither the waker nor a row's idle stop. `spare_idle_stop.py` gives each spare named
its own, `idle_stop.py --spare --minutes N` (`workshop/admin/README.md` §8): it stops by itself
once nobody has used it for N minutes, never before its first boot's volume warm-up has ended.
The admin bot runs it after `!launch … spares=N` and after each `!start` of spares, with the
workshop's `spare_idle_stop` (ORGANIZERS §3.7). By hand, with `S` as above:

```bash
uv run --with boto3 python workshop/aws/spare_idle_stop.py --roster roster.csv "${S[@]}" \
    --spares-file out/spares.json --minutes 10 --only zab-spare-1 --only zab-spare-2 [--dry-run]
```

- **Which spares:** each one named that `spares.json` has for the scope and region, neither
  adopted nor handed over (its address a roster row's): those are skipped, as a name without an
  entry is.
- **Its VM in EC2:** the instance at its address, tagged as the spare. One stopped or stopping is
  skipped: run it again after the spare's next start. The others are waited for, until their
  port 22 answers, 5 minutes at most for all of them together. With `--started` (the bot's
  `!start`, right after `power.py --start`), a stopped one is waited for too: EC2 may still read
  a VM it was just asked to start as `stopped`.
- **Its host key** is pinned to a fingerprint its console printed at the first boot,
  `spares.json`'s `host_keys`, in a `known_hosts` file of this run alone. A spare without them is
  refused: `launch.py` reads them only while the console still shows them.
- **On the VM**, as `ubuntu`: the owner check (still the spare), then the script and its units
  as root, and the timer (re)started. Run again, it installs the same files, the minutes too.
- **It needs** `ec2:DescribeInstances`, and the fleet's SSH key. `--dry-run` reads `spares.json`
  and EC2, and makes no SSH connection.

## teardown.py (after the workshop, `workshop/admin/README.md` §7)

Lists the matching instances and addresses, asks for confirmation (unless `--yes`), terminates
the instances, waits, and releases the Elastic IPs. The volumes go with the instances
(`DeleteOnTermination`), and with them any sign-in still on disk. First, uninstall zab
(`push_config.py --uninstall`), sync each participant's work to GitHub (`final_sync.py`) and
collect the exports they asked for (`workshop/admin/README.md` §7); the participants sign their
agents out themselves (ORGANIZERS §5.2).

An instance with termination protection, a kept VM, is listed as `keep …` and left alone,
together with the Elastic IP associated with it. To remove one after all, lift the protection
first with `protect.py --off --only <slug>`.

EC2 enforces a change of termination protection minutes late (#378). A VM it still refuses as
protected (`OperationNotPermitted`) is tried again for up to 5 minutes (`RETRY_SECONDS`), then
listed `refused …` with its Elastic IP kept, while the others are torn down; the exit code is
then 1. A failed wait or release is reported the same way and never stops the rest. The other
direction is worse: a VM protected moments ago may still read as unprotected and be terminated.
So after `protect.py`, run `teardown.py` only once `protect.py` has said `EC2 shows it now` for
each VM, or 5 minutes later.

**`--strict`** (behind `!retire`): every VM selected must go. One that EC2 still reads as
protected, which would be kept, is listed `refused …` instead, with its Elastic IP kept, and the
exit code is 1. The others are torn down as without it.

**`--roster`** (behind `!teardown` and `!retire`): the `ip` of every roster row whose
address the run released is cleared, and its `host` kept, so that no row keeps an address EC2 may
give to the next VM it allocates one for.

**`--spares-file`** (behind `!teardown` and `!retire`, #607): every entry of the scope and region
in `spares.json` at an address the run released goes, written atomically, so that no later run
offers a spare that is gone, nor takes a new spare at its recycled address for it. A file it
can't read or write is reported with the entries to remove by hand, and the exit code is 1.

Listed as `skip …` and left alone:

- an instance or address without a `participant` tag;
- an address associated with anything this run does not terminate: another instance, or a
  network interface alone. In a default VPC, releasing it would silently disassociate it.

A whole-scope teardown therefore never removes a builder or smoke instance that
`build_ami.py --keep` left running (tagged `role=ami-build`, no `participant`). Terminate such
an instance by hand, then delete its security group and key pair, as `--keep` says.

## protect.py (kept VMs, issue #207)

```bash
AWS_PROFILE=<aws-profile> uv run --with boto3 python workshop/aws/protect.py \
    --region <region> --tag workshop=<ws> --tag zab-scope=<SCOPE> \
    [--only <slug> …] [--off] [--dry-run] [--no-wait]
```

For every live instance with the scope tags and a `participant` tag, or only the `--only`
participants, it turns on
**termination protection** (`DisableApiTermination`), so a stray `teardown.py` or a console click
cannot delete a VM somebody kept. It also makes an OS shutdown **stop** the instance
(`InstanceInitiatedShutdownBehavior=stop`), because idle stop powers kept VMs off from inside.
`--off` lifts the protection again, before a teardown; shutdown still means stop.

It is idempotent: it reads both attributes, changes only what differs, and prints one line per
instance. `--dry-run` only reads. Run it once per region. EC2 shows a change of termination
protection late, sometimes by minutes (#378), so after setting it, the script reads it back
every 10 s for up to 5 minutes and says per VM `set; EC2 shows it now` or `set; EC2 doesn't show
it yet (it can take a few minutes)`; never a failure for that. `--no-wait` skips the reads. It needs `ec2:DescribeInstances`,
`ec2:DescribeInstanceAttribute` and `ec2:ModifyInstanceAttribute`; `teardown.py` now needs
`ec2:DescribeInstanceAttribute` as well.

New VMs get the same at launch: `launch.py` always sets `InstanceInitiatedShutdownBehavior=stop`,
and `launch.py --protect` adds termination protection.

## grow_disk.py (a participant's disk)

```bash
AWS_PROFILE=<aws-profile> uv run --with boto3 python workshop/aws/grow_disk.py --roster roster.csv \
    --slug <slug> --size <GB> --region <region> --tag workshop=<ws> --tag zab-scope=<SCOPE> \
    [--known-hosts workshop/admin/out/known_hosts] --dry-run     # look first; then without --dry-run
```

Behind `!grow-disk` (ORGANIZERS §4.1). It grows one row's root volume to `--size` GB, at most
1,000 (`MAX_GB`), then its file system.

- **The checks,** at the dry run and again at the apply. Each failure is one `refusing: …` line,
  exit 1, and nothing changed:
  - the row is the roster's, not a spare, with an `ip`;
  - EC2 has exactly one live instance with every scope tag and the row's `participant` tag, and
    no `zab-admin` tag, `running` or `stopped`, at the row's `ip`;
  - its root volume carries every scope tag (the server's role may modify only such a volume);
  - the size is larger than the volume's, since EBS volumes never shrink, or equal to it on a
    running VM, for the file system alone (below);
  - the volume's last modification began more than 6 hours ago, and is over: EBS allows one
    every 6 hours. The file system alone needs only the second half.
- **The dry run** shows the volume, its new size, what the added GB cost a month (gp3, $0.08 per
  GB), and, on a running VM, the root file system's use, over SSH.
- **The apply** modifies the volume and waits until the VM sees the new size. On a running VM it
  then grows the partition and the file system online, over SSH as `ubuntu` with sudo. A stopped
  VM's grows at its next boot, through cloud-init's `growpart`; the script never starts it.
- **The file system alone:** the volume's own size, on a running VM, finishes a grow whose SSH
  step failed, at no cost and without the 6 hours' wait.

SSH goes through the host key pinned in `--known-hosts`, with no forwarding. It needs
`ec2:DescribeInstances`, `ec2:DescribeVolumes`, `ec2:DescribeVolumesModifications` and
`ec2:ModifyVolume`.

## import_key_pair.py (a workshop's key pair)

As `deploy.sh --bootstrap` runs it on the admin server, as `zab-admin`, with the release's own
Python, whose venv holds boto3:

```bash
sudo -u zab-admin /opt/zab-admin/current/.venv/bin/python -E -s -B \
    /opt/zab-admin/current/workshop/aws/import_key_pair.py --region <region> --key-name zab-admin-<ws> \
    --public-key /var/lib/zab-admin/.ssh/id_ed25519.pub \
    --tag workshop=<ws> --tag zab-scope=<SCOPE> [--dry-run]
```

It imports the admin server's fleet key as a workshop's EC2 key pair, `zab-admin-<ws>`, tagged
with the workshop's scope from its creation: the server's role may import a key pair only so
tagged, and launch only with one so tagged (docs/ADMIN.md §5). The server makes one fleet key,
`/var/lib/zab-admin/.ssh/id_ed25519`, and imports it once per workshop: `deploy.sh --bootstrap`
runs this script, as `zab-admin` with the instance role, for every workshop of `admin.toml`, and
`!new-workshop` makes the same import for each later one.

It is idempotent: a key pair of that name with the same key and the scope tags is left alone
(`present, with this key and …`). One with another key, or without the tags, is refused and never
replaced: replacing a fleet's key pair is a deployer's decision, made by hand. `--tag` is required.
By hand, from a laptop (a second region, "Regions" above), run it with `uv run --with boto3 python`
and an AWS profile, as the other scripts; `--public-key` is then a copy of the fleet key's public
half, which this prints from a region that has the key pair:
`aws ec2 describe-key-pairs --region <region> --key-names zab-admin-<ws> --include-public-key
--query 'KeyPairs[0].PublicKey' --output text`. The script needs `ec2:DescribeKeyPairs` and
`ec2:ImportKeyPair`, and `ec2:CreateTags` for the tags given at the import.

The roster contains personal data: keep it out of git (`.gitignore` already covers `roster*.csv`).
