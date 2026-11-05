# Workshop AMI

`build.sh` prepares a fresh Ubuntu 24.04 (amd64) instance so that an image of it is the workshop
AMI: system packages, the `agent` user (passwordless sudo, lingering), `uv` with a Python
environment (`~/venv`), the pinned Codex CLI and Claude Code with their updaters off and the LMFDB
MCP server configured, zab (the `setup-repo` and `preflight` tools; the bridge itself is
configured at rollout, `workshop/admin/README.md` §2), **PARI/GP 2.17.4 built from source** with
the elldata, galdata, seadata, galpol and nflistdata packages (sha256-pinned), GAP, and optionally
**Magma** and **SageMath 10.9**; TeX Live, Lean
v4.34.1 with a ready Mathlib workspace, lean-lsp-mcp, the lean4 plugin for both agents,
comparator (with landrun, lean4export and nanoda), Codex's `codex-code-mode-host`, and one
instruction file for both agents. `files/` holds what the build installs besides packages: the
instructions, the smoke-test sources and the `comparator-run` wrapper; `build_ami.py` copies it
as `ami-files.tar.gz` (`AMI_FILES`), and by hand `build.sh` finds it next to itself.

Issue #474 widens the stack beyond number theory: Macaulay2 from a Launchpad
snapshot of its PPA (`build.sh` step 1d says how to move to a newer one),
Singular, polymake, Maxima, R, Octave and the figure tools from noble, atlas from source, a
hash-pinned venv (`files/venv-requirements.in` says how to recompile it) and
HomotopyContinuation.jl. `files/tools.toml` lists every tool with a version command and a small
computation; the build runs them all through `files/bin/tools_manifest.py` and writes
`/etc/zab/tools.json` (with `IMAGE_NAME`, from `--name`) only if every one passes, and `--smoke`
runs them again on the fresh instance. To check one tool on a VM, as agent with the services'
`PATH` (sudo's own `PATH` lacks the venv, elan and `~/.local/bin`):

```bash
w=/usr/local/share/workshop
sudo -u agent -H env -i HOME=/home/agent USER=agent LOGNAME=agent SHELL=/bin/bash LANG=C.UTF-8 \
  PATH=/home/agent/venv/bin:/home/agent/.elan/bin:/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin \
  /usr/bin/python3 $w/bin/tools_manifest.py check --source $w/tools.toml \
  --manifest /etc/zab/tools.json --have magma,sage --only Macaulay2
```

`build_ami.py` drives the whole build from your laptop: it launches one builder, copies the
script and the files named below, runs it, creates the image, terminates the builder, and with
`--smoke` launches one instance from the new image, runs the smoke test (below, including a
reboot, so that Magma's licence is known to hold again after one) and terminates it. Nothing else
outlasts the build (DECISIONS D21), and every resource it creates carries the build's `--tag`
tags ("Build", below).

## What the image holds

- **Users.** `ubuntu`, for administration, with the deployers' keys (`workshop/aws/README.md`,
  `--admin-key`). `agent`, the participant's: passwordless `sudo`, so that the agents can install
  software, and lingering (`loginctl enable-linger agent`), so that its user services run without
  a login.
- **System packages** (`build.sh` step 1): build tools, git, Python 3, `pipx`, tmux, screen, htop,
  ripgrep, jq, `ssh-import-id`, the GMP, readline and FLINT headers, and GAP with its data
  packages. **TeX Live 2023** from noble (step 1b): the collections `build.sh` lists, about 3.3 GB,
  against 7.95 GB for `texlive-full`, of which 2.5 GB is documentation (DECISIONS D22).
- **PARI/GP 2.17.4 from source** into `/usr/local`, with GMP and readline, and the data packages
  elldata, galdata, seadata, galpol and nflistdata from the PARI site, each checked against a
  recorded sha256 (DECISIONS D18). Ubuntu's `pari-gp` 2.15.4 stays at `/usr/bin/gp`, since GAP's
  Alnuth package (`gap-alnuth`) depends on it; the `gp` on `PATH` is `/usr/local/bin/gp`.
- **The wider stack** (#474, DECISIONS D35), so that the image serves more than number theory:
  - from noble: Singular, polymake, Maxima, R with the recommended packages, `r-base-dev`,
    tidyverse, data.table, rmarkdown, knitr and the R Jupyter kernel (`ir`), Octave, Graphviz,
    gnuplot, ImageMagick, Poppler, librsvg, Ghostscript, Z3, CaDiCaL, pandoc, and the programs
    Macaulay2 calls (4ti2, lrslib, nauty, Normaliz, TOPCOM, CSDP, cohomCalg);
  - Macaulay2, gfan and msolve from Launchpad's dated snapshot of the Macaulay2 team's PPA, not the
    PPA itself, which keeps only its newest upload: a rebuild, and a VM's `apt upgrade`, get the
    versions tested. The PPA's signing key is committed (`files/apt/macaulay2-ppa.asc`) and
    checked against Launchpad's fingerprint;
  - atlas (the Atlas of Lie Groups and Representations software), built from a pinned commit,
    whose archive is checked by sha256, with `/usr/local/bin/atlas` loading the whole library;
  - not on the image: Wolfram/Mathematica, Agda, Rocq and Isabelle.
- **For `agent`:**
  - `uv`, and `~/venv`, pinned with hashes (`files/venv-requirements.txt`, compiled from
    `venv-requirements.in`, which lists the packages and says why each is pinned);
  - **the Codex CLI**, pinned (SPEC §8.3): the release package of `rust-v0.157.1`, sha256-pinned,
    in `~/.local/lib/codex-0.157.1`, with `~/.local/bin/codex` a link to its `bin/codex`. The
    package carries `codex-code-mode-host`, through which Codex runs every tool call. Its config:
    `approval_policy = "never"`, `sandbox_mode = "danger-full-access"`,
    `check_for_update_on_startup = false` and the LMFDB server, `[mcp_servers.lmfdb]`. The VM is
    disposable and single-purpose, and the agents need `sudo`;
  - **Claude Code**, pinned, from the native installer. `/etc/claude-code/managed-settings.json`,
    which the participant's own settings can't override, sets `DISABLE_AUTOUPDATER=1` and turns
    claude.ai connectors off; the LMFDB server is added at user scope. The bridge sets its own
    permission mode (SPEC §9.5). A CLI is upgraded deliberately, by hand on each VM; a Codex
    upgrade also needs a bridge release with the new pin (SPEC §8.3);
  - **zab**, from the release tag (`--zab-tag`) or a wheel built from it (`--zab-wheel`), for
    `zab setup-repo`, which `provision_repos.py` runs on each VM, and `zab preflight`;
  - **Lean 4:** elan, Lean v4.34.1, and `~/lean/workspace`, a Lake project on Mathlib's tag
    `v4.34.1` with its cache built; `lean-lsp-mcp` for both CLIs (Codex's with a 900 s tool
    timeout); the `lean4` plugin of lean4-skills for both (under Codex its hooks run only once
    the participant trusts them in `/hooks`; the skill works either way); comparator with
    landrun, lean4export and the nanoda kernel in `/usr/local/lib/comparator`, run through
    `comparator-run` in a systemd user unit without `AF_UNIX`, for Lean v4.34.1 projects only;
  - **Julia 1.12.7 and Oscar 1.8.2**, precompiled, with HomotopyContinuation.jl added to Oscar's
    environment;
  - **SageMath 10.9** (conda-forge, `python=3.12`, `--with-sage`). `~agent/.local/bin/sage` is a
    wrapper (`files/bin/sage`) that puts the Sage environment's `bin` first on `PATH`, as `conda
    activate` does, so that Sage's `giac(…)`, `r(…)` and `gap(…)` find Sage's own programs. An
    image built before the wrapper has a link there instead, which a rollout replaces with the
    wrapper (`workshop/admin/README.md` §2.2);
  - **Magma**, under the deployer's licence (below): the installation tree in `/usr/local/magma`,
    with `magma` linked into `/usr/local/bin`;
  - **the agents' instructions:** `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, one text
    (`files/agent-instructions.md`). The image's copy is a starting point only: every rollout
    installs the current text, with a tools section made from the VM's list of its software
    (`workshop/admin/README.md` §2.2).
- **The tools manifest,** `/etc/zab/tools.json`, world-readable (DECISIONS D34):
  `{"schema": 1, "image": <the build's --name>, "built": <UTC date>, "tools": [{"name",
  "version", "area", "call", "summary"}, …]}`, the tools grouped by area, the areas in their fixed
  order. `!tools` and the agents' instructions are made from it. Its source, `files/tools.toml`,
  holds each tool's version command and a small real computation with its expected result, and
  documents the areas. At the end of the build, `files/bin/tools_manifest.py` runs every check as
  `agent` with the services' `PATH` and writes the manifest only if all pass: a tool that fails
  fails the build. A tool present twice, such as GAP, Singular or polymake (apt's and Oscar's),
  has an entry per copy, each saying when to prefer it. Every `call` is a command to paste:
  interpreters that would wait at their prompt after a script (gp, GAP, Magma, Singular) take
  `</dev/null`, `latexmk` takes `-interaction=nonstopmode -halt-on-error`, and atlas reads its
  script on stdin (`atlas < file.at`). The LMFDB is the one remote tool: its check is tried three
  times, and a server down for all three fails the build.

The image's disk, network, metadata and tags are the launch's (`workshop/aws/README.md`, "The
VMs"). The first boot (below) applies the swap file, the update pins and the MCP setup again, so
that they hold on images built before them.

## First boot

`launch.py` gives every VM a cloud-init script, rendered per roster row from `aws_common.py`'s
templates, every value shell-quoted. It runs once, as root, at the instance's first boot, without
`set -e`, so that every step runs even when an earlier one fails:

1. the hostname `ws-<slug>`;
2. the deployers' keys for `ubuntu` (`launch.py --admin-key`);
3. git's defaults for `agent`, from the roster's names and e-mail (required): those of the
   participant's own commits, over SSH for example. The agents' commits carry their bot's name
   and e-mail instead (SPEC §5.5);
4. `~/work`, and `/etc/zab-participant.json`, the VM's own record of its owner (its slug and
   checkout), which `push_config.py` and `provision_repos.py` check before they change anything;
5. then, idempotently, the decisions taken after the image was specified, mirrored in
   `build.sh`:
   - an 8 GiB swap file, `/swapfile` (`fallocate -l 8G /swapfile`, then `mkswap` and `swapon`,
     and an `/etc/fstab` line with `nofail`): Ubuntu's images have none, and a big Magma or Sage
     job would otherwise get an agent or the bridge OOM-killed;
   - Claude Code's auto-updater off (`DISABLE_AUTOUPDATER` in the managed settings) and
     claude.ai connectors off;
   - Codex's update check off, `check_for_update_on_startup = false`, written at the top of
     `config.toml` so that it stays a root key, and the LMFDB server, `[mcp_servers.lmfdb]`;
   - the LMFDB server for Claude Code, `claude mcp add --transport http --scope user lmfdb`, as
     `agent`, bounded by `timeout` and logged to `/var/log/zab-first-boot.log`; the
     participant's first sign-in still runs Claude Code's onboarding;
   - the volume's warm-up: a volume restored from the image's snapshot fetches each block from S3
     on its first read (a fresh VM's first `import Mathlib` ran past 15 minutes, against seconds
     once read). A transient unit, `zab-warm-volume`, reads the big trees once, Lean's first, in
     parallel at the lowest priority (`aws_common.WARM_PATHS`), then writes how long it took to
     `/var/log/zab-warm.log` and marks its end with `/var/lib/zab-warm.done`.

A spare (`--spares N`, tagged `participant=zab-spare-N`, a name no roster slug may take) gets the
same script without the participant's identity: no git defaults, and `zab-spare-N` in its record,
which a hand-over rewrites (`push_config.py --adopt-spare`). There is no key import: participants
get no SSH, and the roster's GitHub columns play no part in the first boot.

## The images

**Which image a workshop launches from.** The admin server launches a workshop's VMs from the
`ami` of its entry in `admin.toml` (`_build_launch` in `src/zab/admin/actions.py` passes it to
`launch.py --ami`). An entry keeps its `ami`; a new one gets its region's newest scoped image:

- **Each region set up for the fleet** holds dated copies of the workshop image,
  `zab-fleet-YYYYMMDD`, made by the account owner, the image and its snapshots tagged with the
  fleet's scope (`zab-scope=<[global] scope>`; BOOTSTRAP §6.1). A new region gets a copy of the
  newest scoped image of a region set up already (BOOTSTRAP §6.5).
- **`!new-workshop` takes the region's newest** for the new entry (ADMIN §2.9): `region_setup`
  (`workshop/admin/new_workshop.py`) asks `find_images` (`workshop/aws/aws_common.py`) for this
  account's `available` images in the region that carry every fleet-wide scope tag, the scope and
  `[global]`'s extra `tags`, and takes the one with the latest `CreationDate`. When two share the
  latest `CreationDate`, both are named and the plan is refused.
- **A rebuild** of the fleet's image needs the account owner's go (D21, D35).

To see what `!new-workshop` would take, list each region's scoped images, oldest first, with the
account owner's profile and the fleet's scope (BOOTSTRAP §0's `$P` and `$SCOPE`); for each extra
tag in `[global] tags`, add a `Name=tag:<key>,Values=<value>` filter. The last line under each
region is the one it takes, and if the last two have the same `CreationDate`, the plan is refused:

```sh
for R in <region> <another region>; do       # [global] regions
  echo "== $R"
  aws ec2 describe-images --profile "$P" --region "$R" --owners self \
    --filters "Name=tag:zab-scope,Values=$SCOPE" Name=state,Values=available \
    --query 'sort_by(Images,&CreationDate)[].[ImageId,Name,CreationDate]' --output text
done
```

**Record your own images,** in your own notes, not in this repository: for each image you build,
its name and AMI ID, the region, the build's date, the release it installs (`--zab-tag`), whether
it has Magma and SageMath, how many tools its manifest checked, and the smoke test's result
(warm-up time, disk used, before and after the reboot); then, for each region, the `zab-fleet-…`
copies made from it (BOOTSTRAP §6.1), and which workshop's entry names which. A copy is what a
workshop launches from; the image you built is only its source.

## Build

From your laptop, in BOOTSTRAP §2.1's terminal, with the account owner's profile, `$P` (BOOTSTRAP
§1), in the region you build in. The image installs zab from a release tag, `--zab-tag vX.Y.Z`, of the public repository
(`--zab-repo`, or `ZAB_REPO`, names another). `--name` is the image's name, which
`/etc/zab/tools.json` records: a generic one such as `zab-workshop-<YYYYMMDD>`. `--tag` sets the
tags of everything the build creates, the image and its snapshots included. Pass your own, such as
`--tag zab-build=ami`: without it, the build tags everything `workshop=nt-agents-2026`, the
maintainer's first workshop. Never `zab-scope=…`: BOOTSTRAP §6.1's copy of the image is what joins
the fleet's scope.

```bash
uv run --with boto3 python workshop/ami/build_ami.py --profile "$P" --account-id <12 digits> \
    --region <region> --name zab-workshop-<YYYYMMDD> --zab-tag vX.Y.Z --tag zab-build=ami \
    --ssh-public-key ~/.ssh/zab-deployer.pub \
    --magma-dir /opt/magma --magma-passfile <Magma's licence file for these instances> \
    --magma-licence-setup <your licence setup script, if your licence needs one> \
    --with-sage --smoke --dry-run                          # the plan; then the real run, detached:
setsid nohup uv run --with boto3 python workshop/ami/build_ami.py … --with-sage --smoke \
    > ~/ami-build.log 2>&1 < /dev/null &
tail -f ~/ami-build.log                                    # "AMI ami-…" appears as soon as the image is requested
```

Magma and SageMath are optional: leave out the `--magma-…` options, or `--with-sage`, to build
without them. A release the builder can't fetch, such as one of a private repository, goes in as a
wheel built from its tag instead: `--zab-wheel dist/zulip_agent_bridge-X.Y.Z-py3-none-any.whl`.

**Pre-flight on the building machine** (about 10 minutes):
1. `aws sts get-caller-identity --profile "$P"` shows the account; `--account-id` is required, and
   is compared with it before anything is created.
2. Magma: `/opt/magma` (resolved if a symlink) holds `magma.exe`, the `magma` driver, `libs/`
   and `package/`; the driver computes its `ROOT` from its own path (`grep -n 'ROOT\|PASSFILE'
   /opt/magma/magma`), and `build.sh` refuses one that still refers to `/opt/magma`. If you
   pass a licence setup script, read it (below): the build runs it as root and, beyond its
   `#!` line, doesn't read it.
3. The PARI sha256 pins in `build.sh` match a fresh download of the published files.
4. Pass `--my-ip` if an HTTPS proxy is in use.
5. `--dry-run` first, then the detached run above.

The build checks Claude Code's `Auto-updates: disabled` itself, on the builder and on the smoke
instance: nothing of the agents runs on the building machine.

The run is detached so that a closed session cannot kill it half-way; if it is killed anyway,
`SIGTERM`/`SIGHUP` run the cleanup (terminate, delete the group and key pair), and the builder
itself halts, and so terminates, after four hours (`shutdown -h +240` with
`InstanceInitiatedShutdownBehavior=terminate`; the smoke instance after 90 minutes). A wheel
must be built from the release tag, not from `main`: `build_ami.py` refuses a wheel whose version
does not match `--zab-tag`.

- The account's credential is the owner's profile, `$P`, through IAM Identity Center where the
  account uses it, so that no long-lived key sits on the laptop (BOOTSTRAP §1, step 4). Never put
  an AWS key in a repository, a log or Zulip.
- The builder is an `m7a.xlarge` from the latest Canonical Ubuntu 24.04 amd64 image, 100 GiB
  gp3, IMDSv2 required, no IAM role, in a temporary security group that admits SSH from the
  building machine's egress address only, with a temporary key pair imported from `--ssh-public-key`.
  Its default, `~/.ssh/id_ed25519.pub`, is usually a personal key: pass the dedicated key's,
  `~/.ssh/zab-deployer.pub`, whose agent in BOOTSTRAP §2.1's terminal serves the build's ssh. The host key is checked against the fingerprints cloud-init
  prints to the instance console. Group and key pair are deleted at the end, also on failure.
- The Magma files are copied for the build only: the installation tree is tarred without any
  licence file, and the licence and the licence setup script go up separately (`chmod 0600` and
  `0700`). The last command on the builder, right before the image is taken and checked so that a
  failure prevents imaging, removes the licence, the tarballs, the scripts, the wheel and both
  `authorized_keys` files cloud-init wrote for the building machine's key (root's forced-command
  copy included), so no VM accepts that key for `ubuntu`. **Never commit the Magma files**
  (`.gitignore` refuses `workshop/ami/magma*`).
- `build.sh` takes about 70 minutes on the builder: PARI from source, conda for Sage, the 1 GiB
  Magma copy, TeX Live (about 3.3 GB), Lean with the Mathlib cache (about 12 GB) and the wider
  stack. `--smoke` adds a fresh instance's boot, warm-up and checks, before and after a reboot.
  `build.sh` is idempotent; `--keep` leaves a failed builder running for inspection.
- The builder is `--instance-type` (default `m7a.xlarge`); the smoke instance runs on the fleet's
  type, `--smoke-instance-type` (default `m7a.2xlarge`, DECISIONS D23).
- `--smoke-only ami-…` runs just the smoke test against an existing image (`--assume-magma`,
  `--assume-sage` say what it contains).

**The permissions it needs.** The account owner's profile has them. A profile of its own for
the build needs these EC2 actions, on `*` (the build makes and removes its own resources, whose
IDs it learns as it goes), and nothing else:
- reading: `ec2:DescribeImages`, `ec2:DescribeVpcs`, `ec2:DescribeSubnets`,
  `ec2:DescribeInstances` and `ec2:GetConsoleOutput` (the host key's fingerprints);
- the temporary group and key pair: `ec2:CreateSecurityGroup`,
  `ec2:AuthorizeSecurityGroupIngress`, `ec2:DeleteSecurityGroup`, `ec2:ImportKeyPair` and
  `ec2:DeleteKeyPair`;
- the builder and the smoke instance: `ec2:RunInstances` and `ec2:TerminateInstances`;
- the image: `ec2:CreateImage`;
- `ec2:CreateTags`, for the tags given at each creation: the group, the key pair, the instances
  and their volumes, the image and its snapshots.

`sts:GetCallerIdentity`, the account check, needs no permission. No `iam:` action: the builder
and the smoke instance have no role. `--smoke-only` needs the same but `ec2:CreateImage`.

Knobs are environment variables of `build.sh` (see its header); `build_ami.py` sets them from its
options. By hand, the equivalent is `scp` of the files to the builder and
`ssh ubuntu@<builder> 'ZAB_WHEEL=… MAGMA_TARBALL=… MAGMA_PASSFILE=… MAGMA_LICENCE_SETUP=… WITH_SAGE=1 bash build.sh'`,
then `aws ec2 create-image … --tag-specifications 'ResourceType=image,Tags=[{Key=<key>,Value=<value>}]'`,
with the build's tags.

## Magma's licence

Magma reads its licence file, `/usr/local/magma/magmapassfile` (`--magma-passfile`). Depending on
the licence, the host may also need some setup at every boot before Magma accepts it. zab doesn't
know what that is: the deployer supplies a script that performs it, `--magma-licence-setup`
(`MAGMA_LICENCE_SETUP`). The build installs it as `/usr/local/sbin/magma-licence-setup` (root
only) and `magma-licence.service`, a systemd oneshot that runs it as root at every boot, after the
network is up and before logins and user services start (so before the bridge and the agents),
with two minutes to finish. One that overruns gets 10 s for each of systemd's two stop signals,
so logins wait for it two and a half minutes at most once the network is up. It runs once on the
builder too, before the smoke test. The script:

- starts with a `#!` line (systemd runs it directly; `build_ami.py` refuses one without);
- runs once at every boot, so it must succeed on a boot after one where it already ran;
- must leave the instance reachable: the smoke test reboots once to prove it;
- carries what it needs itself: the build neither reads nor checks it beyond its `#!` line, and
  doesn't log it.

Without `--magma-licence-setup` Magma is installed all the same and runs where its licence file
alone suffices. Either way the smoke test checks that `echo 'print 2+2;' | magma -b` prints 4 at
the end of the build and on a fresh instance before and after a reboot, and that
`magma-licence.service` is active where the image has it; where Magma doesn't print 4, the check
asks whether its licence is accepted on that host.

## Smoke test

`build.sh` runs it at the end of the build, and `build_ami.py --smoke` on a fresh instance from the
image, before and after a reboot:

- PARI/GP: `gp --version-short` is 2.17.4, and `/usr/local/bin/gp` is the `gp` on `PATH`;
  `ellinit("11a1")` (elldata), `polgalois` in degree 8 (galdata), `ellsea` over a 31-digit prime
  (seadata), `galoisgetpol(10,1)` (galpol) and `nflist("A5", …)` (nflistdata) work; GAP's
  `SmallGroup`, and its Alnuth package, which needs `/usr/bin/gp`;
- `codex --version` and `claude --version` match the pins; `claude doctor` says
  `Auto-updates: disabled`; both CLIs have the LMFDB server configured; `zab --version`;
  `import cypari2`; sudo for `agent`; `/etc/machine-id` and the SSH host keys differ from the
  builder's;
- `codex-code-mode-host --help` runs next to the real `codex`; `claude mcp get lean-lsp` reports
  Connected, and `lean4@lean4-skills` is enabled; both instruction files exist; `latexmk -pdf`
  builds the smoke file (amsart, tikz-cd, xy, biblatex, cleveref); `lake env lean` runs the
  Mathlib smoke file from `~/lean/workspace`; `comparator-run` accepts the smoke challenge; and
  `python3`, `lean`, `lake`, `latexmk` and `comparator-run` resolve on the services' `PATH`;
- with Magma, `magma -b` prints 4 (above); with Sage, `sage -c 'print(EllipticCurve("11a1").rank())'`
  prints 0;
- every tool of `files/tools.toml`, each with its small computation (59 tools with Magma and
  Sage). `--smoke` runs them again on the fresh instance and requires the versions the manifest
  records.
