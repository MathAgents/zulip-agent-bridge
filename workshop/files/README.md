# Central turn files — the deployment runbook

**One GitHub organization, `<org>`** (#693). The server mirrors the repositories of one
organization, the one its `ZAB_FILES_ORG` names, which its unit reads from
`/etc/zab-files/zab-files.env` (§3): such as `MathAgents` for the MathAgents workshops. There is
no built-in one: without it, the service doesn't start. `<org>` is the `github_org` of each
workshop whose `refserve_url` is this host (BOOTSTRAP §7.2). The bridges publish a turn's
snapshot only for a checkout of a repository of `<org>`: their `[render] refserve_owner`, which
`push_config.py` writes from that `github_org` (*Wire up the VMs*, §4). The selftest pushes to a
repository of `<org>` too (§4).

`https://<host>/<repo>/<sha>/<path>` serves only full 40-hex commit
SHAs at tips of `refs/zab/turns/*` in `<org>/<repo>`, and share links: tips of
`refs/zab/shares/*` there, which the server keeps a copy of (*Share links*, below). `<host>` is the files host's name,
`files.` and your domain, such as `files.mathagents.org` for the MathAgents workshops. There is no
repository index, branch-name access, or secret URL prefix. Someone with a snapshot URL can
browse that snapshot's permitted files. Treat these URLs as capabilities.
The SPEC §11.4 denylist is applied again, including to tracked files and listings;
symlinks and submodules are not served. HTML renders, text is UTF-8, directories
list their children, and successful content responses are immutable for one year.
Scriptable HTML/SVG/XML responses carry a CSP sandbox with `allow-scripts`,
`allow-popups`, `allow-popups-to-escape-sandbox`, `allow-modals` and
`allow-downloads`, but never `allow-same-origin`. They get isolated opaque origins
so one snapshot cannot read another opener's URL/DOM. PDFs and non-scriptable
files are not sandboxed, allowing Chrome's PDF viewer to work. Successful
responses also send `Access-Control-Allow-Origin: *`: URLs are capabilities,
with no cookies or other browser credentials. Relative data fetches, module
scripts and fonts can therefore load from an opaque origin. Storage and forms
remain restricted. An explicit MIME map serves PDFs/images/scripts/fonts with
their correct types; other UTF-8 files (including `.csv`) display as plain text,
validated incrementally without decoding a whole blob at once.

**A deleted or transferred repository** (#714): its mirror goes within the hour, and its links
then answer 404 (*Operations and bounds*, below). A copy that a browser has already opened
can't be recalled: content responses are immutable for a year, and the browser may keep it.

**Rendered views (issue #256).** When a browser navigates to a Markdown or source
file (`Sec-Fetch-Dest: document` or `iframe`), it gets a page:
- **Markdown** renders like GitHub, with math typeset by the vendored KaTeX.
- **Source files** are highlighted, with line numbers and `#L42` anchors. That covers Lean,
  Python, Sage, C, Julia, Rust, PARI/GP and Magma, where `.m` is Magma, plus GAP, Macaulay2
  (`.m2`), C++, shell, TeX, BibTeX, TOML, YAML, JSON, JavaScript and CSS. The bridge links
  lines of exactly these files (`SERVED_LINE_EXT` in `src/zab/render/links.py`; a test keeps
  it equal to `LANGUAGES`).
- **Everything else gets the exact bytes, with today's content type:** scripts, stylesheets,
  `fetch()`, curl, and any URL with `?raw=1`, which every page's Raw link uses. Those responses
  carry `Vary: Sec-Fetch-Dest`.
- **The same CSP sandbox as agent HTML** applies to rendered pages. Raw HTML inside Markdown is
  escaped.
- **Plain text for good:** a file stays plain text if it isn't UTF-8, has over 20 000 lines, or
  is over 1 MB (a source file) or 200 KB (Markdown). Markdown over 60 000 block-level tokens,
  such as a very large table, also stays plain text. Those bytes are cached like any content.
- **One render at a time** (`RENDER_SLOTS` in `server.py`): a worst-case render takes 1–2 s and
  about 100 MB. A navigation waits up to 0.5 s for the slot. Then it gets the bytes with
  `Cache-Control: no-store`, as it does when the renderer fails, so a reload can still get the
  page. A `HEAD` request for a page never renders it: it gets the bytes' headers, uncached.
- **Assets** are served from `/_zab/static/`: the stylesheet, KaTeX and its fonts. No page loads
  anything from a CDN.
- **Caching:** content responses are immutable for a year, so a browser that fetched a URL
  before this feature was deployed keeps showing its raw text. Adding any query string, such as
  `?view`, makes a new URL.
- **Libraries:** rendering uses markdown-it-py, mdit-py-plugins and Pygments, which are this
  service's only libraries. They're pinned with hashes in `requirements.txt` and installed in
  `/opt/zab-files/venv`. Without them, files stay plain text and the service logs
  `rendering off`.


The Python service uses the standard library and the checked-in zab denylist.
It adapts Git tree parsing and MIME handling from refserve (MIT; notice in
`workshop/files/LICENSE`), through #135.
The per-VM TLS/ACME and branch modes are intentionally absent; refserve's share mode is too:
a share link here is a commit of its own, holding only what was shared (*Share links*, below).
Caddy handles HTTPS. This directory deploys independently of the participant fleet and wheel.

## 1. Launch, in the fleets' AWS account

**The account** is the one that runs the workshops' fleets and the admin server
([ADMIN](../../docs/ADMIN.md) §1.1), with its owner's profile,
[BOOTSTRAP](../../docs/BOOTSTRAP.md) §0's `$P`. One account keeps one profile, one bill and one
set of tags for everything a deployment runs. It also lets this host's waker, for a setup without
an admin server ([`workshop/waker/README.md`](../waker/README.md), direct mode), start the fleet's
VMs with an instance profile of its own account, with no cross-account role.

Run these commands yourself on your laptop, from a reviewed checkout, in BOOTSTRAP §2.1's
terminal, never on a machine where coding agents run (BOOTSTRAP §3.1). AWS CLI v2 and an EC2 key
pair in the selected region are prerequisites: the dedicated key's serves, imported with BOOTSTRAP
§3.3's command and `--region "$FR"`, since §3.3 imports it into the server's region alone. This example uses a
default VPC public subnet. The group admits 80 and 443 alone: port 22 opens to your own address
for each SSH step only, and closes after it (*SSH to the files host*, below).

```bash
P=<aws-profile>; FR=<the files host's region>; FILES_ACCOUNT_ID=<the account's ID>
FILES_KEY_NAME=<an EC2 key pair in that region>
files_aws() { aws --profile "$P" --region "$FR" "$@"; }
test "$(files_aws sts get-caller-identity --query Account --output text)" = "$FILES_ACCOUNT_ID" || exit 1
FILES_VPC=$(files_aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
FILES_SUBNET=$(files_aws ec2 describe-subnets --filters "Name=vpc-id,Values=$FILES_VPC" Name=default-for-az,Values=true --query 'Subnets[0].SubnetId' --output text)
test "$FILES_VPC" != None && test "$FILES_SUBNET" != None || exit 1
FILES_AMI=$(files_aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text)
FILES_SG=$(files_aws ec2 create-security-group --group-name zab-central-files --description 'Central snapshot HTTPS' --vpc-id "$FILES_VPC" --query GroupId --output text)
files_aws ec2 authorize-security-group-ingress --group-id "$FILES_SG" --protocol tcp --port 80 --cidr 0.0.0.0/0 --query Return --output text
files_aws ec2 authorize-security-group-ingress --group-id "$FILES_SG" --protocol tcp --port 443 --cidr 0.0.0.0/0 --query Return --output text
FILES_INSTANCE=$(files_aws ec2 run-instances --image-id "$FILES_AMI" --instance-type t4g.small --key-name "$FILES_KEY_NAME" --subnet-id "$FILES_SUBNET" --security-group-ids "$FILES_SG" --associate-public-ip-address --metadata-options HttpTokens=required --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":30,"VolumeType":"gp3","Encrypted":true,"DeleteOnTermination":true}}]' --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=zab-central-files}]' --query 'Instances[0].InstanceId' --output text)
files_aws ec2 wait instance-status-ok --instance-ids "$FILES_INSTANCE"
FILES_ALLOCATION=$(files_aws ec2 allocate-address --domain vpc --query AllocationId --output text)
files_aws ec2 associate-address --allocation-id "$FILES_ALLOCATION" --instance-id "$FILES_INSTANCE"
FILES_IP=$(files_aws ec2 describe-addresses --allocation-ids "$FILES_ALLOCATION" --query 'Addresses[0].PublicIp' --output text)
printf 'Instance %s; static IPv4 %s\n' "$FILES_INSTANCE" "$FILES_IP"
```

Use `amd64` in the SSM parameter and `t3.small` if choosing x86 instead. The
[Ubuntu image lookup documentation](https://ubuntu.com/aws/docs/aws-how-to/instances/find-ubuntu-images/)
describes the public SSM image parameters. Save the instance, security group,
and allocation IDs in your private admin notes. The Elastic IP keeps DNS stable
across stop/start; it and the VM incur AWS charges until released/terminated.
The rest of this page finds the instance by its `Name` tag, `zab-central-files`.

## 2. DNS and source installation

In your domain's DNS, add an **A** record: the name `<host>`, the value the IPv4 printed above,
and a TTL of 300. Replace a conflicting record for that name; do not add an AAAA record for this
IPv4-only deployment. Keep inbound 80/443 open for HTTPS certificate issuance/renewal.

Then open port 22 to your address, with *SSH to the files host*'s blocks, below: `HOMEIP`, the
host's group, Open, and the read-back. Before accepting SSH's first host key, compare
its fingerprint with the EC2 console output obtained using the authenticated AWS API:

```bash
files_aws ec2 get-console-output --instance-id "$FILES_INSTANCE" --latest --query Output --output text
ssh "ubuntu@$FILES_IP"
```

Find `BEGIN SSH HOST KEY FINGERPRINTS` in the output; retry if cloud-init has not
printed it yet. Use your selected key via ssh-agent or `ssh -i /path/to/key`.
On the VM, install prerequisites and create the service user/directory:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git caddy
sudo useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin zab-files
sudo install -d -m 0755 /opt/zab-files
exit
```

Caddy is available in [Ubuntu 24.04's repositories](https://packages.ubuntu.com/noble/caddy); the
[official Caddy packages](https://caddyserver.com/docs/install#debian-ubuntu-raspbian)
are an alternative. From your **laptop's reviewed repository checkout**,
copy only committed source files (no working-tree credentials or admin rosters). The shipped
`Caddyfile` names `files.mathagents.org`: the `sed` installs it with your `<host>` in its place.

```bash
git archive HEAD src/zab/__init__.py src/zab/workspace/__init__.py src/zab/workspace/denylist.py workshop/files | ssh "ubuntu@$FILES_IP" 'sudo tar -x -C /opt/zab-files'
ssh "ubuntu@$FILES_IP"
FILES_HOST=<host>
FILES_ORG=<org>
sudo chmod 0755 /opt/zab-files/workshop/files/askpass.py
sudo python3 -m venv /opt/zab-files/venv
sudo /opt/zab-files/venv/bin/pip install --quiet --require-hashes --no-deps --only-binary :all: -r /opt/zab-files/workshop/files/requirements.txt
sudo install -m 0644 /opt/zab-files/workshop/files/zab-files.service /etc/systemd/system/zab-files.service
sed "s/^files\.mathagents\.org {/$FILES_HOST {/" /opt/zab-files/workshop/files/Caddyfile | sudo tee /etc/caddy/Caddyfile >/dev/null
grep -n ' {$' /etc/caddy/Caddyfile        # 5:<host> {
```

## 3. Install the token and start

Create a GitHub **fine-grained personal access token**: resource owner the organization the
server fetches from, `<org>` (top of this page), **all repositories**, **Contents:
read-only** (Metadata read is automatic). All of them: the hourly check of *Operations and
bounds* takes a repository that the token can't read for a deleted one, and removes its mirror.
Complete any organization approval before starting. The
participant deploy keys still push snapshots; this token cannot push. The server token is
separate from the admin server's token (BOOTSTRAP §8.1). On the VM, in the session of §2, which
set `FILES_ORG`:

```bash
sudo install -d -m 0700 /etc/zab-files
[[ $FILES_ORG =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?$ ]] && sudo install -m 0644 -o root -g root /dev/null /etc/zab-files/zab-files.env && printf 'ZAB_FILES_ORG=%s\n' "$FILES_ORG" | sudo tee /etc/zab-files/zab-files.env
sudo install -m 0600 /dev/null /etc/zab-files/github-token
sudo systemd-ask-password 'GitHub read-only token:' | sudo tee /etc/zab-files/github-token >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now zab-files
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable caddy
sudo systemctl restart caddy
sudo systemctl --no-pager --full status zab-files caddy
curl --retry 12 --retry-delay 5 --retry-all-errors -I "https://$FILES_HOST/"
sudo journalctl -u zab-files --no-pager -o cat | grep -e serving -e organization
```

The first `tee` prints `ZAB_FILES_ORG=<org>`, and the last line `serving the repositories of
<org>`. That `tee`'s line writes the file, `root:root` with mode `0644`, only when `FILES_ORG`
has the form of a GitHub organization's name, as the server checks it: letters, digits and
inner hyphens, at most 39 characters; otherwise it prints nothing. The service reads the
organization from `/etc/zab-files/zab-files.env` at each start (the unit's `EnvironmentFile`). An update writes
that file only where it is missing (*Updating a running server*, step 3), and never changes it.
Without it, or with a name GitHub can't have, the service exits at its start, with status 78
(`EX_CONFIG`) and `no GitHub organization: …` or `not a GitHub organization: '…'` in its
journal, and isn't restarted. Then set `FILES_ORG`, run that line again, and `sudo systemctl
restart zab-files`. A host serves one organization for good: its links and its mirrors name a
repository by its name alone, which a repository of another organization may have too. Another
organization gets a host of its own.

With `admin off`, `systemctl reload caddy` fails because reload needs the admin API;
use `systemctl restart caddy` as above. The curl retries allow time for initial ACME issuance. The root URL should return **404**. Caddy obtains and renews the certificate for
the configured hostname ([automatic HTTPS](https://caddyserver.com/docs/automatic-https)).
The token source is root:root 0600 under a 0700 directory. systemd
[`LoadCredential`](https://systemd.io/CREDENTIALS/) exposes a private read-only copy to the unprivileged service;
`askpass.py` reads it only for GitHub password prompts. Git receives the token
through the askpass pipe, never its argv, remote URL, environment or git config.
The Python service suppresses request-path logs; fetch failures log the repo slug,
exit code and scrubbed first stderr line without SHAs. Caddy access logs are off,
but its **error logs can include request URIs**: keep journal access restricted
and redact SHA URLs before sharing diagnostics. Do not enable
git tracing or shell `set -x` while installing credentials.

To rotate: repeat the `systemd-ask-password | tee` command, then
`sudo systemctl restart zab-files`. Never paste the token into a shell command.

Then log out, and close port 22: *SSH to the files host*'s Close, then its read-back, which must
show the 80 and 443 lines alone. Nothing needs port 22 until the next SSH step.

## 4. End-to-end check, then wire up the VMs

On your laptop, choose a **private scratch repository in `<org>`** that you can push
to (existing, and readable by the token). Your own participant repository, if you have one
(`<org>/<your slug>`), is a fine one: this script creates no remote branch and changes
no existing ref. It only adds one uniquely named synthetic turn ref, under
`refs/zab/turns/selftest/`, which is no branch, so a clone doesn't fetch it. No `gh`
installation or login is required. Push with your GitHub SSH key. `--org` names the organization
of §3 and `--url` your host; both are required. The snapshot comes back only from a server
that serves `<org>`. Each form is a block of its own, since its push may ask for something, such
as your key's passphrase:

```bash
./workshop/files/selftest.sh YOUR_SCRATCH_REPO_SLUG --org <org> --url https://<host> --push-url git@github.com:<org>/YOUR_SCRATCH_REPO_SLUG.git
```

Or over HTTPS, with the credentials git has stored for it:

```bash
./workshop/files/selftest.sh YOUR_SCRATCH_REPO_SLUG --org <org> --url https://<host>
```

- **Your key's passphrase.** In a terminal with BOOTSTRAP §2.1's agent, which holds `zab-deployer`
  alone, ssh offers that key first. It is not on your GitHub account, so ssh goes on to your
  usual GitHub key, which may ask for its passphrase.
- **`Repository not found`** from the push (`remote: Repository not found.` over HTTPS,
  `ERROR: Repository not found.` over SSH): GitHub refused it. The credentials the push used
  can't see this private repository, or no repository has that name: GitHub answers both alike.
  Over HTTPS, that is whatever credentials git has stored, perhaps another account's: the HTTPS
  form can fail this way where the SSH form passes.

Expected: `PASS: snapshot file served; branch SHA, tracked auth.json and traversal
return 404`. The branch SHA is an ancestor present in the fetched history but
not a turn tip. `auth.json` contains only a synthetic denylist decoy. Before pushing,
the script probes the [repository API](https://docs.github.com/en/rest/repos/repos#get-a-repository)
anonymously: public (200) or unverifiable (for example, 403 rate limit) responses
are refused; 404 permits the authenticated git push. A missing repository also
returns 404, but its push then fails. Thus a successful push and snapshot read
exercise the server's access to a repository unavailable anonymously.
New snapshots wait out the five-second success cooldown; 60-second failed-repo
backoff or the new-mirror budget can still return 404. The test makes at most 12
HTTP attempts, with six-second retry delays and a 40-second timeout per request.
To remove the synthetic ref later, use its printed name:
`git push git@github.com:<org>/YOUR_SCRATCH_REPO_SLUG.git :refs/zab/turns/selftest/PRINTED_NUMBER`.
The offline suite runs only the local-bare-repository tests, not this live push. A `FAIL:
snapshot content unavailable or incorrect` after a push that went through is most often a server
that serves another organization than `--org`: its journal's `serving the repositories of …`
line (§3) names the one it serves.

**Wire up the VMs.** Each workshop's entry in the admin server's `admin.toml` names this host as
`refserve_url`, `https://<host>`, and `<org>` as `github_org` (BOOTSTRAP §7.2). Every `!rollout`
writes both into each VM's bridge configuration: `push_config.py --refserve-url` as `[render]
refserve_url`, and `--refserve-owner` as `[render] refserve_owner`, the organization whose
repositories get snapshots (#693). A hand run of `push_config.py` passes both
([`workshop/admin/README.md`](../admin/README.md), at its top). The bridge then
pushes `refs/zab/turns/<session-uuid>/<turn-id>` with each repository's deploy key, and links the
turn's files here. Check one real file link from a completed bot turn, after the first rollout.

## Operations and bounds

- Cache: `/var/cache/zab-files/<repo>.git`, no worktrees. Fetch only when a requested
  SHA is not a known turn tip; a missing path in a known snapshot never fetches.
  Fetches use the atomic, pruning turn-ref refspec. Only fetches lock per repo;
  cached readers proceed during network work. Each repository has its own lock,
  without hash collisions. At most eight distinct unknown tips per repository are admitted; requests for the same tip share one wait;
  a fetch started after their arrival answers the whole waiting group, even when
  their SHA remains unknown. Failed new mirrors leave no lock-map entry. Existing
  mirrors retain their gate/start time; these entries grow only with the mirror cache.
  Repository names accept ASCII
  letters, digits, `_`, `-` and `.` (1–100 characters, except `.` and `..`);
  lowercase names key the cache, throttle and repository gate. Filename deny rules
  apply to blobs' full paths; directories use only the directory deny rules.
- Successful fetches wait out a 5-second per-repo cooldown before fetching a new
  tip; failures are cached for 60 seconds. Failed first fetches remove the new
  mirror. New mirrors have a separate 60-attempt rolling-minute budget (enough
  for an initial 35-person burst); existing mirrors have no global fetch cap.
  New mirrors are refused if expiring fetch history has reached 256 entries;
  existing mirrors can still refresh. Budget, history, negative-cache and fetch
  queue refusals log the repo and reason, never the SHA. They return retryable
  404s; cached files remain usable.
- Cached tips prune on the next successful fetch for an unknown SHA. Deleted
  remote refs and cached browser content are not revoked immediately.
- **Deleted and transferred repositories** (#714). A request for a snapshot already mirrored
  never asks GitHub, so the service checks: every hour, the first time 5 minutes after it
  starts, it looks up each mirrored repository with the REST API and its token, read afresh
  each time. It removes a mirror when its repository is gone, so the mirror of a repository
  deleted or transferred goes within about 65 minutes, and that repository's links answer 404
  from then on. Gone means all of these:
  - GitHub answers 404 for `<org>/<repo>`, or redirects it to a repository of another
    owner (a transfer; a rename within `<org>` is not gone, and its links keep working);
  - then the token lists the organization's private repositories, and reads another of them
    afresh: a 404 from a token that reads nothing private is not taken for a deletion;
  - then GitHub answers 404 for it again, and the listing didn't show it.

  So no failure removes a mirror: no answer, a 5xx, a rate limit, an outage, a token expired or
  revoked (401) or refused (403) all leave it as it is, and so does a 200 that doesn't name the
  repository's owner. A check that would remove more than 3 mirrors as gone, or a quarter of them
  if that is more, removes none, and names them (`kept …`): that looks like a token narrowed to
  some repositories. The listing is read page by page, up to 5,000 repositories. The first check of a mirror fetches it again, from the repository's name
  on GitHub now (a renamed one's new name), which prunes its turn refs to the repository's own,
  then writes the repository's GitHub ID into it (`zab-github-id`): a repository deleted and
  made again under its name before that check, renamed or not, keeps none of the old one's
  snapshots. A repository found under another ID later is a new one
  with the old one's name, and the old one's mirror goes. So the first check after this
  release's deploy fetches every mirror once. Each check logs one line, `gone check: <n> mirrors, … removed
  <repos>`, and each removal `mirror removed repo=<repo>: <why>`.

  **Removing one mirror at once,** say for a deletion request (ORGANIZERS §5.9), or one a check
  kept (`kept …`, above): on the host (*SSH to the files host*, below), with the repository's
  name as in its links. It is safe while the service runs. Run it once the repository is deleted
  or transferred: before that, the next request for one of its snapshots fetches the repository
  again. Its outcome goes to the journal too (`journalctl -t zab-files`), and sudo's own log
  records the command (`journalctl _COMM=sudo`). Then check that a link of it answers `Not
  found`.

  ```bash
  sudo -u zab-files env PYTHONPATH=/opt/zab-files/src /opt/zab-files/venv/bin/python \
    /opt/zab-files/workshop/files/server.py purge <repo>
  # mirror removed repo=<repo>: purged by hand
  # removed mirror of <repo>     (or this line alone: no mirror of <repo>)
  ```
- At most 32 handler threads, eight blob responses and two concurrent fetches.
  Fetch/lock/cooldown waits do not consume response slots. Admission waits up
  to 35 seconds before creating a thread, with a 64-connection listen backlog to
  absorb bursts; only timeout returns 503. Response-slot waits also time out at
  35 seconds; load-shedding 503s are logged without request URLs. Unknown-tip
  repository gates cap distinct unknown-tip groups at eight per repo and 32 globally.
  Any number of admitted HTTP requests for the same tip share that group's result,
  so a room can open a new snapshot together. Groups wait up to 35 seconds through
  a slow fetch or cooldown; the 32-handler cap still bounds actual HTTP requests. Excess requests and wait exhaustion
  return logged 503s, not false missing-file responses. New-mirror fetch-pool timeouts undo their history/backoff/budget reservation. Git subprocesses fit below
  the service task limit. Responses write 256 KiB chunks, each with a 40-second
  socket timeout and a progress deadline of max(40 seconds, bytes already sent / 64 KiB per second).
  A write failure closes the partial response; it never appends a second 404.
  Git gets SIGTERM after 30 seconds and a 5-second grace before group SIGKILL;
  stale Git lock files older than 35 seconds are cleaned before the next fetch.
  Automatic GC stays attached. The maximum blob is 32 MiB (larger returns 404).
  There is no cache eviction: check `df -h /var/cache/zab-files` regularly.
- Troubleshoot with `sudo journalctl -u zab-files -u caddy --since '10 minutes ago'`.
  Failed GitHub fetches identify the repo, exit code and first stderr line with
  known token formats and SHA strings scrubbed. HTTP errors still return 404;
  examine token approval/expiry with your admin access, never print the token.
- Update the code, its libraries and the unit with *Updating a running server* (below), never
  in place. A changed `Caddyfile` is installed on its own, after that update, with your `<host>`
  in place of the shipped `files.mathagents.org`, as §2 installed it. The update's step 8 has
  closed port 22: open it again for this (*SSH to the files host*), and close it after.

  ```bash
  FILES_HOST=<host>; C=$(mktemp -d)
  sed "s/^files\.mathagents\.org {/$FILES_HOST {/" /opt/zab-files/workshop/files/Caddyfile > "$C/Caddyfile" &&
    sudo caddy validate --config "$C/Caddyfile" &&
    sudo install -m 0644 "$C/Caddyfile" /etc/caddy/Caddyfile &&
    sudo systemctl restart caddy
  rm -r "$C"
  ```

  A cache can be discarded while the service is stopped; misses rebuild it.
  Service state is the mirrors and the share store (*Share links*, below), which isn't a cache:
  don't discard it. No participant VM or roster state lives here.

Offline validation from the repository root:
`uv run pytest tests/workshop/files -q`. These tests use local `file://` bare
repositories; the production command has no remote URL override.

## Share links

`zab share PATH…` on a VM (SPEC §10.7, #710) commits only those files or folders, with a random
nonce of 256 bits in the commit's message, and pushes it to `refs/zab/shares/<name>` of the
participant's repository. Its link is `https://<host>/<repo>/<sha>/…`, as a snapshot's; the
nonce makes the commit, and so the link, unguessable even to someone who knows the files. It is
served as a snapshot is: the denylist applies again, and the listing of its root shows only what
was shared. `zab share` opens its link once after the push, which is when the server takes its
copy.

**The server's own copy.** A mirror fetches `refs/zab/shares/*` with the turns. A share is
served from the **share store**, `/var/lib/zab-files/shares/<repo>.git` (the unit's
`StateDirectory`), one bare repository per participant's repository, apart from the mirrors in
`/var/cache/zab-files`. The first request for a share, after a fetch that began after it and
succeeded, copies the share there, under its commit (`refs/zab/shares/<sha>`, its names on
GitHub in `zab-share-names`), with its repository's GitHub ID (`zab-share-ids`), looked up
first: without the ID (no token, no answer from GitHub) the share isn't taken, and its request
answers 503, which `zab share` retries. A repository made again under the name, with a share of
the same name, takes nothing from one kept. So a share keeps working after its VM ends, after
the workshop, and after its repository is deleted or transferred, which removes only the mirror
(*Operations and bounds*): until it is revoked. **The share store is not backed up.** A host
lost, or the store discarded, loses every share whose repository is gone; and with the record
of revocations gone too, every share revoked by hand whose ref is still on GitHub comes back
with its next request, as does every other share whose ref is still there.

**Revoked** (bounds): a share is looked up by the GitHub ID recorded with it
(`/repositories/<id>`, which follows a rename and never names a new repository that took the
name). When that repository exists and has no share ref at the share's commit (`git ls-remote`
of its name now), the share is revoked: its ref deleted from the store, its objects pruned, and
its commit recorded in the store's `zab-revoked`, so that no later fetch takes it again.
- `zab share --revoke` deletes the ref on GitHub, then asks the server to check that share at
  once (`POST /_zab/recheck/<repo>/<sha>`; each share once every 10 seconds, 120 checks an hour
  in all), so its link answers 404 within seconds. The check answers `revoked` or `kept`, or a
  503 when GitHub doesn't say (no token, no answer, the limit): never `kept` for a share it
  couldn't decide.
- The hourly check (*Operations and bounds*) checks every share too: a ref deleted any other way
  goes within about 65 minutes, or once GitHub answers again.
- A repository that is gone, or transferred to another owner, revokes nothing; nor does a
  failure, an outage or a bad token, as for the mirrors.
- A browser that has opened a share keeps it: content responses are immutable for a year.

**By hand** (a deployer, on the host, *SSH to the files host* below), for an organizer's
request, after `!retire` or once the repository is gone. List a repository's shares, or every
one's without a name: one line each, with the repository, the share's name, its date, its
link's path and the paths shared:

```bash
sudo -u zab-files env PYTHONPATH=/opt/zab-files/src /opt/zab-files/venv/bin/python \
  /opt/zab-files/workshop/files/server.py shares <repo>
# <repo>  <name>  2026-11-02T14:03:11+00:00  /<repo>/<sha>/  paper/main.pdf
```

Revoke one at once, by its link, its commit or its name, or `all` of the repository's (a
deletion request, ORGANIZERS §5.9). It is safe while the service runs, and holds even while the
ref is still on GitHub:

```bash
sudo -u zab-files env PYTHONPATH=/opt/zab-files/src /opt/zab-files/venv/bin/python \
  /opt/zab-files/workshop/files/server.py unshare <repo> '<link, commit, name, or all>'
# share revoked repo=<repo>: revoked by hand
# revoked <name> (<sha's first 12>) of <repo>
```

It exits 1, with `not revoked: <name> (…) of <repo>: <why>`, for a share it didn't revoke (the
store busy for 35 seconds: try again). Its outcome goes to the journal too, without the commit.

## SSH to the files host: open port 22, then close it

The host's security group admits only 80 and 443 (§1). Nothing else needs its port 22: the VMs
push their snapshots to GitHub, and the host fetches them from there. Open it for your own
address right before an SSH step, and close it when you are done: for the setup (§2, §3), the
update below, and anything else on the host, such as the waker in direct mode
(`workshop/waker/README.md`). The update repeats these blocks in its steps 0 and 8, where it runs
them; `tests/workshop/files/test_readme.py` keeps their commands the same as
here.

**Where from.** Your laptop, with the profile of the fleets' account (§1), never a machine
where coding agents run (BOOTSTRAP §13.2). **Never from a network you share with one,** such as
your home's if an agent machine is on it: the network reaches the internet from one address, so a
rule for it would admit that machine too (BOOTSTRAP §3.1). Open, and run every SSH step after it,
from another network: a phone's hotspot, a VPN, your office. The closing can run from anywhere. A
hotspot's address can change: if SSH stops connecting, open again.

**In one command** (#674): `workshop/adminserver/ssh-rule.sh --files-host`, from a reviewed
checkout, opens port 22 to this network's address alone, and `--files-host --close` closes it.
Each does what the blocks below do, finds the group the same way, and checks the read-back
itself. Opening also checks that the agent holds the dedicated key alone, and refuses the home
address, which it keeps in `~/.config/zab/homeip`. BOOTSTRAP §3.1 says how to record it
(`--record-home`, on that network) and how to save the dedicated key and the account's profile
(`--save-defaults`), and `--help` lists the options. The files host's profile and region, given
here, override the saved ones:

```bash
P=<aws-profile>; FR=<the files host's region>   # the fleets' account (§1)
~/zab-main/workshop/adminserver/ssh-rule.sh --files-host --profile "$P" --region "$FR"
```

It prints these lines. On the rules lines, the fields are separated by tabs; the last three, IPv6
ranges, source groups and prefix lists, are empty, so each such line ends with three tabs; and
the rules come in any order:

```text
home: not this network ($HOME/.config/zab/homeip)
address: <your address>/32 (checkip)
key: SHA256:…, alone in the agent
group: sg-…, the files host's group (profile <P>, region <FR>)
revoke sgr-…: True
authorize tcp 22 from <your address>/32: True
rules (protocol, ports, then IPv4 ranges, IPv6 ranges, groups and prefix lists):
  tcp	80	80	0.0.0.0/0			
  tcp	22	22	<your address>/32			
  tcp	443	443	0.0.0.0/0			
ok: 443 and 80 from 0.0.0.0/0, 22 from <your address>/32 alone
```

There is one `revoke` line for each old rule for port 22, and none when there is none. With
`--dry-run`, which changes nothing, it prints `DRY RUN: nothing changes` first, then the same
lines up to `group:`. Then come `would revoke sgr-…` and `would authorize tcp 22 from <your
address>/32`, and the rules as they are now. It ends `ok: the run would leave 443 and 80 from
0.0.0.0/0, 22 from <your address>/32 alone`. A `stopped: …` line says what to fix (BOOTSTRAP
§3.1).

Close, from any network:

```bash
~/zab-main/workshop/adminserver/ssh-rule.sh --files-host --close --profile "$P" --region "$FR"
```

```text
group: sg-…, the files host's group (profile <P>, region <FR>)
revoke sgr-…: True
rules (protocol, ports, then IPv4 ranges, IPv6 ranges, groups and prefix lists):
  tcp	80	80	0.0.0.0/0			
  tcp	443	443	0.0.0.0/0			
ok: 443 and 80 from 0.0.0.0/0
```

The blocks below are the same steps by hand, the fallback.

**That network's address, first:** `HOMEIP`, as in BOOTSTRAP §0. While still on that network, in
the terminal you will open from, record it: it is the agent machine's address too, and Open
refuses it, and refuses to run without it. If you share no network with an agent machine, or
have no such address at hand, set `HOMEIP=none` instead.

```bash
HOMEIP="$(curl -fsS https://checkip.amazonaws.com)/32"
echo "$HOMEIP"
# one address, /32
```

**Find the host's group,** in each new shell, before opening or closing. It finds the running
instance by its Name tag and its group through the instance, so that it never edits another
group of that name:

```bash
P=<aws-profile>; FR=<the files host's region>   # the fleets' account (§1)
FI=$(aws ec2 describe-instances --profile "$P" --region "$FR" \
  --filters Name=tag:Name,Values=zab-central-files Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId' --output text)
FSG=$(aws ec2 describe-instances --profile "$P" --region "$FR" --instance-ids "$FI" \
  --query 'Reservations[].Instances[].SecurityGroups[].GroupId' --output text)
[[ $FI =~ ^i-[0-9a-f]+$ && $FSG =~ ^sg-[0-9a-f]+$ ]] && echo ok || echo "stopped: instance '$FI', group '$FSG'"
# ok
```

`stopped` means not exactly one running instance with exactly one group: look before going on.

**Open.** It checks that checkip's answer is one address and not `HOMEIP`, and touches
nothing otherwise. Then it revokes every rule for port 22 alone (22 to 22), whatever its source:
an IPv4 or IPv6 range, a source group or a prefix list. Last, it admits your current address
alone. Each ingress call ends `--query Return --output text`, so it prints only `True`: by
default it would print the rule with the account ID.

```bash
FNEW="$(curl -fsS https://checkip.amazonaws.com)/32"
if ! [[ ${HOMEIP:-} == none || ${HOMEIP:-} =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]]; then
  echo "stopped: set HOMEIP first (above)"
elif [[ $FNEW == "$HOMEIP" ]]; then
  echo "stopped: $FNEW is HOMEIP, an agent machine's address too: use another network"
elif [[ $FSG =~ ^sg-[0-9a-f]+$ && $FNEW =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]]; then
  for R in $(aws ec2 describe-security-group-rules --profile "$P" --region "$FR" \
      --filters "Name=group-id,Values=$FSG" \
      --query 'SecurityGroupRules[?!IsEgress && FromPort==`22` && ToPort==`22`].SecurityGroupRuleId' --output text); do
    aws ec2 revoke-security-group-ingress --profile "$P" --region "$FR" --group-id "$FSG" \
      --security-group-rule-ids "$R" --query Return --output text
  done
  aws ec2 authorize-security-group-ingress --profile "$P" --region "$FR" --group-id "$FSG" \
    --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$FNEW,Description=admin}]" \
    --query Return --output text
else
  echo "stopped: group '$FSG', address '$FNEW'"
fi
# True (one more True before it for each old 22 rule it revoked)
```

**Close,** when you are done. It revokes every rule for port 22 alone, as Open does:

```bash
if [[ $FSG =~ ^sg-[0-9a-f]+$ ]]; then
  for R in $(aws ec2 describe-security-group-rules --profile "$P" --region "$FR" \
      --filters "Name=group-id,Values=$FSG" \
      --query 'SecurityGroupRules[?!IsEgress && FromPort==`22` && ToPort==`22`].SecurityGroupRuleId' --output text); do
    aws ec2 revoke-security-group-ingress --profile "$P" --region "$FR" --group-id "$FSG" \
      --security-group-rule-ids "$R" --query Return --output text
  done
else
  echo "stopped: group '$FSG'"
fi
# True, once for each 22 rule
```

**Read the rules back** after each: one line per rule, with its protocol, ports, then its
sources: IPv4 ranges, IPv6 ranges, source groups and prefix lists.

```bash
aws ec2 describe-security-groups --profile "$P" --region "$FR" --group-ids "$FSG" \
  --query 'SecurityGroups[0].IpPermissions[].[IpProtocol,FromPort,ToPort,join(`","`,IpRanges[].CidrIp || `[]`),join(`","`,Ipv6Ranges[].CidrIpv6 || `[]`),join(`","`,UserIdGroupPairs[].GroupId || `[]`),join(`","`,PrefixListIds[].PrefixListId || `[]`)]' --output text
# opened: tcp 22 22 <your address>/32, then the 80 and 443 lines, in any order
# closed: the 80 and 443 lines only
```

When open, the 22 line names your `/32` alone, with its last three columns empty. Anything else
that reaches port 22 means someone changed the group. A second range or source on the 22 line
is gone after Close. `-1` (all traffic) or a port range around 22 is not: Open and Close revoke
only rules for port 22 alone, so that they never take one that also carries 80 or 443. Remove
such a rule by hand, and look, before you connect.

**Connect** as `ubuntu`, with your own key, and never forward your agent or X11:

```bash
ssh -o ForwardAgent=no -o ForwardX11=no ubuntu@<host>
```

Your `known_hosts` has the host's key from your earlier logins. If ssh asks you to confirm one,
answer only after comparing its `ED25519` fingerprint with the one those logins accepted
(`ssh-keygen -l -F <the host's address>`) or with the console's (§2).

## Updating a running server

For the deployer, who runs every step. It deploys a reviewed commit of `workshop/files`, with the zab
denylist it imports, to the files host, and never leaves it without a working service. It works
the same on a host installed with §1–§3 as on one updated before. In outline:
- **Steps 1–5 build and test the new release beside the running one,** which they don't touch:
  the code in `/opt/zab-files.new`, its own venv inside it, and a copy of the new unit, on port
  8081, that the selftest reaches through an SSH tunnel.
- **Step 6, the switch,** saves the running unit beside the running code, renames the code
  directories, installs the new unit and restarts the service once. Until that restart, the
  running process serves from what it loaded. The previous release stays as
  `/opt/zab-files.old`, with its unit.
- **The rollback,** at the end, puts the previous code and unit back in one block.

It takes about 30 minutes, with port 22 open throughout. The service is down only for its
restart, a second or two, while Caddy answers 502.

**The organization, from a release before #693.** Releases before #693 (zab 0.2.17 and
earlier) served `MathAgents` alone, built into the code. Since #693 there is no built-in
organization: the unit reads it from `/etc/zab-files/zab-files.env` (§3), which a host installed
or updated before has none of. Before the service restarts on such a release, that file must
exist, owned by `root:root` with mode `0644`, holding one line, `ZAB_FILES_ORG=<org>`:
`ZAB_FILES_ORG=MathAgents` for the MathAgents workshops' host. Without it the service exits at
its start with status 78 and stays down. Step 3 writes it, only where it is missing, from the
`FILES_ORG` that step 1 sets; the running release ignores it, and so does the rollback's. Steps 5
and 7 check that the new release serves it.

**Two terminals on the laptop,** both on a network no agent machine shares from step 0.6 on:
**A** holds the SSH session, **B** runs everything else (AWS, git, the selftest, curl). Each step says where it
runs. A command that may ask for something is alone in its block, so a paste never answers it:
git and ssh (a sign-in, a host key, your key's passphrase) and the selftest (its push).

### 0. Ready (B)

All of step 0 runs in **B**. On a network you share with an agent machine, start there: 0.1 to
0.5 run there, and 0.6 switches this machine to another network. Keep B open until step 8. It
keeps what step 0 sets: `FILES_HOST`, `FILES_ORG`, `REV`, `SCRATCH` and `URL` for the steps
after it, and `HOMEIP`, `P`, `FR` and `FSG` to open port 22 again (0.8) and to close it (step 8).

#### 0.1 The values

```bash
FILES_HOST=<host>
FILES_ORG=<org>
REV=<the reviewed commit to deploy>
SCRATCH=<a private repository in that organization, by its name alone>
URL='<a snapshot link to a Markdown or source file>'
```

- **`FILES_ORG`** is the GitHub organization the host serves (the top of this page), such as
  `MathAgents` for the MathAgents workshops' host.
- **`SCRATCH`** is the selftest's private scratch repository (§4), without `$FILES_ORG/`. Your
  own participant repository, `<your slug>`, is a fine one: the selftest only adds a ref
  under `refs/zab/turns/selftest/`. It creates no branch and changes no existing ref.
- **The snapshot link** is any answer of one of your own bots that links a Markdown or source
  file on your host, `https://<host>/…`. Copy the link from Zulip. It is a capability: keep it in
  this terminal. **A host that has served no bot turn yet** has the snapshot §4's selftest left,
  with a `README.md`: `https://<host>/<scratch>/<its commit>/README.md`, its commit the first
  field of the line that `git ls-remote "git@github.com:$FILES_ORG/<scratch>.git"
  'refs/zab/turns/selftest/*'` prints. Run that alone: ssh may ask for your key's passphrase.

#### 0.2 The checkout

`~/zab` is BOOTSTRAP §1's clone, made here if this machine has none. Each of these two lines may
ask for your GitHub sign-in or your key's passphrase, so each is a block of its own: paste the
first, then the second.

```bash
ZAB_REPO=<the zab repository's clone URL>   # the one you deploy from, as in BOOTSTRAP §1
[ -d ~/zab ] || git clone "$ZAB_REPO" ~/zab
```

```bash
git -C ~/zab fetch origin
```

#### 0.3 The worktree

Only for a commit that is on `main`. The block checks `$REV` out in `~/zab-files-update`, and
makes that worktree first on a machine that has none:

```bash
if ! git -C ~/zab merge-base --is-ancestor "$REV" origin/main; then
  echo "stopped: $REV is not on origin/main"
elif [ -d ~/zab-files-update ]; then
  git -C ~/zab-files-update checkout --detach "$REV" &&
    git -C ~/zab-files-update log -1 --format='%h %s'
else
  git -C ~/zab worktree add --detach ~/zab-files-update "$REV" &&
    git -C ~/zab-files-update log -1 --format='%h %s'
fi
# git's own lines (HEAD is now at …), then, last, the commit and its subject
```

#### 0.4 Before anything changes

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "https://$FILES_HOST/_zab/static/files.css"
# 200
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' -H 'Sec-Fetch-Dest: document' "$URL"
# 200 text/html; charset=utf-8    (the rendered page)
```

#### 0.5 `HOMEIP`, while still on that network

The address of a network you share with an agent machine, which is that machine's address too.
0.8 refuses to open port 22 to it, and refuses to run without it. If you share no network with an
agent machine, or start away from it with no such address at hand, set `HOMEIP=none` instead.

```bash
HOMEIP="$(curl -fsS https://checkip.amazonaws.com)/32"
echo "$HOMEIP"
# one address, /32
```

#### 0.6 Another network

Switch this machine to a network no agent machine shares, such as a phone's hotspot. Both
terminals use it from here on, through step 8. Nothing to paste: 0.8 checks that the address it
opens is not `HOMEIP`.

#### 0.7 The host's group

It finds the running instance by its Name tag, and its group through the instance, so that it
never edits another group of that name:

```bash
P=<aws-profile>; FR=<the files host's region>   # the fleets' account (§1)
FI=$(aws ec2 describe-instances --profile "$P" --region "$FR" \
  --filters Name=tag:Name,Values=zab-central-files Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId' --output text)
FSG=$(aws ec2 describe-instances --profile "$P" --region "$FR" --instance-ids "$FI" \
  --query 'Reservations[].Instances[].SecurityGroups[].GroupId' --output text)
[[ $FI =~ ^i-[0-9a-f]+$ && $FSG =~ ^sg-[0-9a-f]+$ ]] && echo ok || echo "stopped: instance '$FI', group '$FSG'"
# ok
```

`stopped` means not exactly one running instance with exactly one group: look before going on.

#### 0.8 Open port 22, to this address alone

It checks that checkip's answer is one address and not `HOMEIP`, and touches nothing otherwise.
Then it revokes every rule for port 22 alone (22 to 22), whatever its source, and admits this
address alone:

```bash
FNEW="$(curl -fsS https://checkip.amazonaws.com)/32"
if ! [[ ${HOMEIP:-} == none || ${HOMEIP:-} =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]]; then
  echo "stopped: set HOMEIP first (above)"
elif [[ $FNEW == "$HOMEIP" ]]; then
  echo "stopped: $FNEW is HOMEIP, an agent machine's address too: use another network"
elif [[ $FSG =~ ^sg-[0-9a-f]+$ && $FNEW =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]]; then
  for R in $(aws ec2 describe-security-group-rules --profile "$P" --region "$FR" \
      --filters "Name=group-id,Values=$FSG" \
      --query 'SecurityGroupRules[?!IsEgress && FromPort==`22` && ToPort==`22`].SecurityGroupRuleId' --output text); do
    aws ec2 revoke-security-group-ingress --profile "$P" --region "$FR" --group-id "$FSG" \
      --security-group-rule-ids "$R" --query Return --output text
  done
  aws ec2 authorize-security-group-ingress --profile "$P" --region "$FR" --group-id "$FSG" \
    --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$FNEW,Description=admin}]" \
    --query Return --output text
else
  echo "stopped: group '$FSG', address '$FNEW'"
fi
# True (one more True before it for each old 22 rule it revoked)
```

`stopped: … is HOMEIP` means this machine is still on that network: do 0.6, then this block
again. If SSH stops connecting later, this network's address may have changed: this block and
0.9 again, in B.

#### 0.9 Read the rules back

One line per rule: its protocol, its ports, then its sources (IPv4 ranges, IPv6 ranges, source
groups, prefix lists):

```bash
aws ec2 describe-security-groups --profile "$P" --region "$FR" --group-ids "$FSG" \
  --query 'SecurityGroups[0].IpPermissions[].[IpProtocol,FromPort,ToPort,join(`","`,IpRanges[].CidrIp || `[]`),join(`","`,Ipv6Ranges[].CidrIpv6 || `[]`),join(`","`,UserIdGroupPairs[].GroupId || `[]`),join(`","`,PrefixListIds[].PrefixListId || `[]`)]' --output text
# tcp 22 22 <your address>/32, then the 80 and 443 lines, in any order
```

The 22 line names your `/32` alone, with its last three columns empty. Anything else that reaches
port 22, such as `-1` (all traffic) or a port range around 22, means someone changed the group.
0.8 and step 8 revoke only rules for port 22 alone, so that they never take one that also carries
80 or 443: remove such a rule by hand, and look, before you connect.

### 1. Log in, with the tunnel (A)

```bash
ssh -o ForwardAgent=no -o ForwardX11=no -o ClearAllForwardings=no -o ExitOnForwardFailure=yes \
  -L 8081:127.0.0.1:8081 ubuntu@<host>
```

`-L` forwards the laptop's port 8081 to step 5's staged server, as long as this session lasts.
If ssh says that port is in use on the laptop, use another one on the left
(`-L 8082:127.0.0.1:8081`) and in step 5's `--url` and `curl`. Until step 5, ssh may print
`connect failed: Connection refused` for it: harmless.

In that session, the host's name and organization for step 3 (0.1's), and the starting state:

```bash
cd /
FILES_HOST=<host>
FILES_ORG=<org>
lsb_release -ds; python3 --version
# Ubuntu 24.04.… LTS
# Python 3.12.…
systemctl is-active zab-files caddy
# active
# active
systemctl cat zab-files | grep -E '^# /|^ExecStart='
# # /etc/systemd/system/zab-files.service
# ExecStart=/opt/zab-files/venv/bin/python /opt/zab-files/workshop/files/server.py
ls -d /opt/zab-files*; ls -A /opt/zab-files
# /opt/zab-files, and /opt/zab-files.old after an earlier update
# src  venv  workshop, and REVISION first after an earlier update
systemctl is-active zab-files-staged
# inactive
ss -ltn | grep -c ':8081 '
# 0
sudo cat /etc/zab-files/zab-files.env
# ZAB_FILES_ORG=<org>, or, on a host that runs a release before #693:
# cat: /etc/zab-files/zab-files.env: No such file or directory
```

Where the output differs otherwise:
- **another organization than `$FILES_ORG`** in `zab-files.env`: the host serves that one. Stop,
  and find out which is right: an update never changes it (§3);
- **no `/opt/zab-files`:** an earlier switch or rollback stopped between its two renames, and
  `/opt/zab-files.old` holds the code that ran. Remove nothing: see the rollback's last
  paragraph first;
- **`zab-files-staged` not `inactive`:** a staged server left by an earlier attempt, perhaps
  from a `.new` that the next bullet removes. Run step 5's last block, which stops and removes
  it;
- **`/opt/zab-files.new` or `/opt/zab-files.failed`,** from an earlier attempt: the steps below
  need these names free, so `sudo rm -rf /opt/zab-files.new /opt/zab-files.failed`;
- **port 8081 in use,** with `zab-files-staged` inactive: log out, and take another port
  throughout (the `-L`, step 5's `sed` and `curl`);
- **anything else,** such as another Python (`requirements.txt` is pinned for 3.12) or a second
  `# /` line (a drop-in, which would apply to the new unit too): stop, and open an issue
  with what you found.

Last, `/opt/zab-files.old`, after an earlier update: the release before the running one, which
that update kept for a rollback (step 8). Step 6 needs the name free, so remove it; that ends the rollback to it. The
block removes it only while `/opt/zab-files` holds the running code, and changes nothing if
`.old` is already gone:

```bash
test -d /opt/zab-files/workshop && sudo rm -rf /opt/zab-files.old
ls -d /opt/zab-files*
# /opt/zab-files
```

### 2. `python3-venv` (A)

```bash
sudo apt-get update -q
sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l \
  apt-get install -y -q -o DPkg::Lock::Timeout=300 python3-venv
dpkg-query -W -f='${Status}\n' python3-venv
# install ok installed
```

`NEEDRESTART_MODE=l` makes needrestart list the services to restart, and restart none:
`zab-files` keeps running on what it loaded until step 6, even if apt upgrades `python3.12`
with it (`python3.12-venv` needs the same version). The lock timeout waits out
unattended-upgrades.

### 3. Stage the code (B, then A)

In **B**, only the committed files the service needs, as in §2, and the commit's ID. `mkdir`
fails if `/opt/zab-files.new` exists, and then nothing is extracted:

```bash
git -C ~/zab-files-update archive HEAD src/zab/__init__.py src/zab/workspace/__init__.py \
    src/zab/workspace/denylist.py workshop/files |
  ssh -o ForwardAgent=no -o ForwardX11=no ubuntu@"$FILES_HOST" \
    "sudo mkdir -m 0755 /opt/zab-files.new && sudo tar -x -C /opt/zab-files.new &&
     echo $(git -C ~/zab-files-update rev-parse HEAD) | sudo tee /opt/zab-files.new/REVISION"
# the commit's full ID
```

In **A**:

```bash
sudo chmod 0755 /opt/zab-files.new/workshop/files/askpass.py
ls -A /opt/zab-files.new
# REVISION  src  workshop
sed "s/^files\.mathagents\.org {/$FILES_HOST {/" /opt/zab-files.new/workshop/files/Caddyfile |
  diff /etc/caddy/Caddyfile - && echo same Caddyfile
# same Caddyfile
```

The `sed` puts your host in the shipped Caddyfile's place, as §2 did. If the Caddyfiles differ
otherwise, go on: the server runs behind either. Nothing here restarts Caddy: install the new one
after the update ("Operations and bounds").

Then the organization the new release serves (*The organization, from a release before #693*,
above), still in **A**. The block writes `zab-files.env` from step 1's `FILES_ORG` only where it
is missing, as `root:root`, mode `0644`, and never changes one that is there. It writes nothing
unless `FILES_ORG` has the form of a GitHub organization's name, as the server checks it:
letters, digits and inner hyphens, at most 39 characters:

```bash
if [[ $FILES_ORG =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?$ ]]; then
  sudo test -f /etc/zab-files/zab-files.env ||
    { sudo install -m 0644 -o root -g root /dev/null /etc/zab-files/zab-files.env &&
      printf 'ZAB_FILES_ORG=%s\n' "$FILES_ORG" | sudo tee /etc/zab-files/zab-files.env >/dev/null; }
  sudo stat -c '%a %U:%G' /etc/zab-files/zab-files.env
  sudo cat /etc/zab-files/zab-files.env
else
  echo "stopped: FILES_ORG='$FILES_ORG' is no GitHub organization's name"
fi
# 644 root:root
# ZAB_FILES_ORG=<org>
```

`stopped: FILES_ORG='…'` means this shell's `FILES_ORG` is empty or misspelt, and nothing was
written: set it as in step 1, then run the block again. Another mode or owner, on a file written
before, still works, since systemd reads the file as root. To match, run
`sudo chown root:root /etc/zab-files/zab-files.env`, then
`sudo chmod 0644 /etc/zab-files/zab-files.env`.

### 4. The venv, and its imports (A)

```bash
sudo python3 -m venv /opt/zab-files.new/venv
sudo /opt/zab-files.new/venv/bin/python -m pip install -q --disable-pip-version-check \
  --root-user-action=ignore --require-hashes --no-deps --only-binary :all: \
  -r /opt/zab-files.new/workshop/files/requirements.txt
/opt/zab-files.new/venv/bin/python -m pip list --disable-pip-version-check
# linkify-it-py 2.2.0, markdown-it-py 4.2.0, mdit-py-plugins 0.6.1, mdurl 0.1.2, pip 24.0,
# Pygments 2.21.0: one a line, under a header
```

- `ensurepip is not available` means step 2 didn't install `python3-venv`: run
  `sudo rm -rf /opt/zab-files.new/venv`, then steps 2 and 4 again.
- A hash mismatch, or no matching distribution: stop, and open an issue. pip installs
  exactly the pinned wheels, and `--only-binary :all:` never builds a package as root.

Then the imports, as the service's user, with its unit's `PYTHONPATH`. Loading `server.py`
loads `render.py` and warms it up, as the service does:

```bash
sudo -u zab-files env PYTHONPATH=/opt/zab-files.new/src /opt/zab-files.new/venv/bin/python - <<'CHECK'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("staged", "/opt/zab-files.new/workshop/files/server.py")
server = sys.modules["staged"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
page = server.RENDER and server.RENDER.render("a.md", b"$x^2$\n", base="/r/0")
print(sys.prefix, "rendering on" if page and b'class="math inline"' in page else "RENDERING OFF")
CHECK
# /opt/zab-files.new/venv rendering on
```

`RENDERING OFF`, after a line `rendering off: <error>`, means the venv lacks a library: stop.

### 5. The staged server, and the selftest through the tunnel (A, B, then A)

In **A**, the new unit as `zab-files-staged`: it runs the staged directory, on port 8081, with a
cache and a share store of its own, under the new unit's sandbox and credential, and its first gone check
(*Operations and bounds*) 10 seconds after a start rather than 5 minutes. It goes in `/run/systemd/system`,
which a reboot clears, and it is never enabled:

```bash
sed -e 's#/opt/zab-files/#/opt/zab-files.new/#g' \
    -e 's#/server\.py$#/server.py --port 8081 --cache /var/cache/zab-files-staged --shares /var/lib/zab-files-staged/shares --check-delay 10#' \
    -e 's#^CacheDirectory=zab-files$#CacheDirectory=zab-files-staged#' \
    -e 's#^StateDirectory=zab-files$#StateDirectory=zab-files-staged#' \
    /opt/zab-files.new/workshop/files/zab-files.service |
  sudo tee /run/systemd/system/zab-files-staged.service >/dev/null
diff /opt/zab-files.new/workshop/files/zab-files.service /run/systemd/system/zab-files-staged.service
# 9,10c9,10
# < ExecStart=/opt/zab-files/venv/bin/python /opt/zab-files/workshop/files/server.py
# < Environment=PYTHONPATH=/opt/zab-files/src
# ---
# > ExecStart=/opt/zab-files.new/venv/bin/python /opt/zab-files.new/workshop/files/server.py --port 8081 --cache /var/cache/zab-files-staged --shares /var/lib/zab-files-staged/shares --check-delay 10
# > Environment=PYTHONPATH=/opt/zab-files.new/src
# 12c12
# < CacheDirectory=zab-files
# ---
# > CacheDirectory=zab-files-staged
# 15c15
# < StateDirectory=zab-files
# ---
# > StateDirectory=zab-files-staged
sudo systemctl daemon-reload
sudo systemctl restart zab-files-staged
sleep 5; systemctl is-active zab-files-staged
# active
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/_zab/static/files.css
# 200
sudo journalctl -u zab-files-staged --since '2 min ago' --no-pager -o cat | grep -e serving -e organization
# serving the repositories of <org>
```

If it isn't `active`, `sudo journalctl -u zab-files-staged --no-pager -n 30` says why. The last
line must name `$FILES_ORG`.
- **`no GitHub organization: …`** or **`not a GitHub organization: '…'`**: `zab-files.env` is
  missing, or names no organization. Fix it, then start the staged server again. The running
  service isn't affected, since it read the file, if at all, when it started.
  1. Remove the file: `sudo rm -f /etc/zab-files/zab-files.env`.
  2. Set `FILES_ORG` as in step 1.
  3. Run step 3's last block again. It must print `644 root:root`, then `ZAB_FILES_ORG=<org>`.
  4. Run `sudo systemctl restart zab-files-staged`.
  5. Repeat this block's last three commands, from `sleep 5`.
- **`serving the repositories of …` names another organization than `$FILES_ORG`:** stop, as
  for step 1's `zab-files.env`.

In **B**, the selftest through the tunnel. It pushes to `$SCRATCH` over SSH, with your GitHub
key, and is alone in its block, since ssh may ask for that key's passphrase:

```bash
cd ~/zab-files-update &&
  ./workshop/files/selftest.sh "$SCRATCH" --org "$FILES_ORG" --url http://127.0.0.1:8081 --push-url "git@github.com:$FILES_ORG/$SCRATCH.git"
# PASS: snapshot file served; branch SHA, tracked auth.json and traversal return 404
# PASS: 9 files rendered (Markdown with math, 8 languages); ?raw=1 exact
# Retained synthetic test ref: refs/zab/turns/selftest/… in <org>/<scratch>
```

- **Your key's passphrase.** If B runs BOOTSTRAP §2.1's agent, which holds `zab-deployer` alone, ssh
  offers that key first. It is not on your GitHub account, so ssh goes on to your usual GitHub
  key, which may ask for its passphrase.
- **`ERROR: Repository not found.`** GitHub refused the push: the key ssh used can't see this
  private repository, or `$SCRATCH` names none (`echo "$SCRATCH"`). GitHub answers both alike.
  Without `--push-url`, the push goes over HTTPS with whatever credentials git has stored for
  it, perhaps another account's, and can fail the same way (`remote: Repository not found.`)
  where this SSH form passes.
- **`anonymous repository visibility check failed (HTTP 403)`** is GitHub's anonymous rate limit,
  which a phone's shared address can reach: try again later.

Then the known snapshot, through the staged server. Its first request fetches that whole
repository with the read-only token: if it prints nothing, try again after a minute. If it
fails again, `sudo journalctl -u zab-files-staged` in A says why. A `fetch failed` timeout on a
large repository doesn't stop the update: the selftest covered the rest, and step 7 checks this
link on the running service, whose mirror of it exists.

```bash
curl -sS -H 'Sec-Fetch-Dest: document' "http://127.0.0.1:8081/${URL#https://$FILES_HOST/}" |
  grep -o '<main class="[a-z]*"'
# <main class="markdown"    (or "source", for a source file)
```

**The gone check, with the production token** (#714), in **A**: a copy of one of the staged
server's mirrors under a name that no repository has, which its first check after a restart
must remove. It touches only the staged cache, `/var/cache/zab-files-staged`:

```bash
M=$(sudo ls /var/cache/zab-files-staged | grep -m1 -E '^[a-z0-9._-]+\.git$'); echo "$M"
# one of its mirrors: <your scratch repository>.git, or the link's
PROBE="zab-gone-probe-$(date +%s)"
sudo cp -a "/var/cache/zab-files-staged/$M" "/var/cache/zab-files-staged/$PROBE.git"
sudo systemctl restart zab-files-staged
sleep 60; sudo journalctl -u zab-files-staged --since '2 min ago' --no-pager -o cat |
  grep -e 'mirror removed' -e 'gone check'
# mirror removed repo=zab-gone-probe-…: deleted or transferred
# gone check: <n> mirrors, <n> exist, 1 not found, 0 unknown; removed zab-gone-probe-…
sudo ls /var/cache/zab-files-staged | grep -c zab-gone-probe
# 0
```

Anything else is a failure of step 5: `unknown` above 0, `the token reads no other private
repository`, or no `mirror removed` line. The production token can't read what the check needs:
check its repository access and expiry (§3) before going on.

**A share, end to end** (#710), through the tunnel: a parentless commit holding one file,
pushed to `refs/zab/shares/probe-<time>` of `$SCRATCH`, as `zab share` pushes one; opened, its
root listed, its ref deleted, the recheck, and then the 404. In **B**, the commit and its names:

```bash
P=$(mktemp -d) && git -C "$P" init -q && printf 'probe\n' > "$P/probe.txt" &&
  git -C "$P" add probe.txt &&
  git -C "$P" -c user.name=probe -c user.email=probe@example.invalid commit -q \
    -m "zab share probe" -m "Path: probe.txt" -m "Nonce: $(openssl rand -hex 32)" &&
  PROBE_SHA=$(git -C "$P" rev-parse HEAD) && PROBE_REF="refs/zab/shares/probe-$(date +%s)" &&
  echo "$PROBE_REF $PROBE_SHA"
# refs/zab/shares/probe-<time> <its commit>
```

The push, alone, since ssh may ask for your GitHub key's passphrase:

```bash
git -C "$P" push -q "git@github.com:$FILES_ORG/$SCRATCH.git" "HEAD:$PROBE_REF"
```

Then, still in **B**, the file and the share's root:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8081/$SCRATCH/$PROBE_SHA/probe.txt"
# 200    (a 404 or 503 in the first seconds: the fetch's cooldown; try again after 10 seconds)
curl -sS "http://127.0.0.1:8081/$SCRATCH/$PROBE_SHA/" | grep -o 'href="[^"]*"'
# href="/<scratch>/<its commit>/probe.txt"    (that line alone)
```

In **A**, the deployer's listing of the staged store:

```bash
sudo -u zab-files env PYTHONPATH=/opt/zab-files.new/src /opt/zab-files.new/venv/bin/python \
  /opt/zab-files.new/workshop/files/server.py --cache /var/cache/zab-files-staged \
  --shares /var/lib/zab-files-staged/shares shares
# <scratch>  probe-<time>  <date>  /<scratch>/<its commit>/  probe.txt
```

In **B**, the ref deleted, alone (the passphrase again):

```bash
git -C "$P" push -q "git@github.com:$FILES_ORG/$SCRATCH.git" ":$PROBE_REF"
```

Then the recheck, the 404, and the scratch commit gone:

```bash
curl -sS -X POST "http://127.0.0.1:8081/_zab/recheck/$SCRATCH/$PROBE_SHA"
# revoked
curl -sS -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8081/$SCRATCH/$PROBE_SHA/probe.txt"
# 404
rm -rf "$P"
```

In **A**, the listing again prints nothing (the block above, again). A recheck that answers
`Busy; retry shortly` is GitHub not answering the token: wait a minute, then the recheck again
(each share once every 10 seconds). Anything else is a failure of step 5: stop, as above.

Back in **A**, stop and remove the staged server, whatever the results:

```bash
sudo systemctl stop zab-files-staged
sudo rm /run/systemd/system/zab-files-staged.service
sudo systemctl daemon-reload
sudo rm -rf /var/cache/zab-files-staged /var/lib/zab-files-staged
```

**If step 4 or 5 failed,** stop here: `sudo rm -rf /opt/zab-files.new` in A, close port 22
(step 8's two blocks, in B), and open an issue with what failed. The running service was
never touched.

### 6. The switch (A)

One chain: if a command fails, the rest doesn't run, and it prints `stopped`. It checks that
step 5 is cleaned up, saves the running unit as `zab-files.service.installed` in the running
code's directory, renames that directory to `/opt/zab-files.old` and the staged one to
`/opt/zab-files`, installs the new unit, and restarts the service:

```bash
test ! -e /opt/zab-files.old && test ! -e /run/systemd/system/zab-files-staged.service &&
  test -x /opt/zab-files.new/venv/bin/python &&
  sudo cp /etc/systemd/system/zab-files.service /opt/zab-files/zab-files.service.installed &&
  sudo mv -T /opt/zab-files /opt/zab-files.old &&
  sudo mv -T /opt/zab-files.new /opt/zab-files &&
  sudo install -m 0644 /opt/zab-files/workshop/files/zab-files.service /etc/systemd/system/zab-files.service &&
  sudo systemctl daemon-reload &&
  sudo systemctl restart zab-files &&
  sleep 5 && systemctl is-active zab-files ||
  echo "stopped: ls -d /opt/zab-files* tells where (below)"
# active
```

Anything else: `ls -d /opt/zab-files*`. With both `/opt/zab-files` and `/opt/zab-files.new`
there, the chain stopped before the renames, and the service runs as before: read the error
above `stopped`, if any (a failed `test` prints none: something from step 1 or 5 is left).
Without `/opt/zab-files`, see the rollback's last paragraph. Otherwise, roll back (below).

The venv works under its new path: Python finds it from the interpreter's path. Its own scripts,
such as `venv/bin/pip`, still name `/opt/zab-files.new` and no longer run. Nothing uses them; run
`venv/bin/python -m pip` instead.

### 7. Check (A, then B)

In **A**:

```bash
grep '^ExecStart=' /etc/systemd/system/zab-files.service; cat /opt/zab-files/REVISION
# ExecStart=/opt/zab-files/venv/bin/python /opt/zab-files/workshop/files/server.py
# the full ID of $REV
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/_zab/static/files.css
# 200
sudo journalctl -u zab-files --since '10 min ago' --no-pager -o cat | grep -c -e 'rendering off' -e Traceback
# 0
sudo journalctl -u zab-files --since '10 min ago' --no-pager -o cat | grep -e serving -e organization
# serving the repositories of <org>
```

In **B**:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "https://$FILES_HOST/_zab/static/files.css"
# 200
curl -sS -H 'Sec-Fetch-Dest: document' "$URL" | grep -o '<main class="[a-z]*"'
# <main class="markdown"    (or "source")
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' "$URL?raw=1"
# 200 text/plain; charset=utf-8
```

Back in **A**, the running service's first gone check (#714), 5 minutes after its restart: one
line, with nothing removed unless a repository was deleted.

```bash
sudo ls -ld /var/lib/zab-files
# drwx------ … zab-files zab-files … /var/lib/zab-files    (the share store's state, #710)
sleep 330; sudo journalctl -u zab-files --since '10 min ago' --no-pager -o cat |
  grep -e 'gone check' -e 'share check'
# gone check: <n> mirrors, <n> exist, 0 not found, 0 unknown; removed none
# share check: <n> repositories with shares; revoked 0
```

- **`unknown` above 0:** GitHub didn't answer for those repositories, or refused the token (an
  expiry, a revoked approval, an outage). Nothing was removed.
- **`the token reads no other private repository: none removed, kept …`:** the token lists no
  private repository of the organization it reads: its access, or its approval, is gone.
- **`… gone at once: none removed, kept …`:** more than 3 repositories, or a quarter, not found at
  once; the line names them.
- **`not found` above 0, with `removed …`:** those repositories are gone on GitHub, and their
  mirrors with them.

For each of the first three, stop and check the token's repository access (§3: all
repositories, Contents read-only) before purging anything; then purge by hand those the line
names that are deleted on GitHub (*Operations and bounds*). The update itself stands.

Then, in **B**, the selftest against your host, alone, since ssh may ask for your GitHub key's
passphrase. It pushes over SSH, as in step 5, whose notes on the passphrase and on
`Repository not found` hold here too:

```bash
cd ~/zab-files-update &&
  ./workshop/files/selftest.sh "$SCRATCH" --org "$FILES_ORG" --url "https://$FILES_HOST" --push-url "git@github.com:$FILES_ORG/$SCRATCH.git"
# the same three lines as in step 5
```

Last, open `$URL?view` in a browser: the page renders, with its math typeset. Without `?view`, a
browser that opened the link before keeps its cached raw text (*Rendered views*, Caching).

If a check fails, roll back.

### 8. Close port 22 (B)

In **B**, which still holds `P`, `FR` and `FSG` from step 0.7. Close revokes every rule for port 22
alone, as 0.8 does:

```bash
if [[ $FSG =~ ^sg-[0-9a-f]+$ ]]; then
  for R in $(aws ec2 describe-security-group-rules --profile "$P" --region "$FR" \
      --filters "Name=group-id,Values=$FSG" \
      --query 'SecurityGroupRules[?!IsEgress && FromPort==`22` && ToPort==`22`].SecurityGroupRuleId' --output text); do
    aws ec2 revoke-security-group-ingress --profile "$P" --region "$FR" --group-id "$FSG" \
      --security-group-rule-ids "$R" --query Return --output text
  done
else
  echo "stopped: group '$FSG'"
fi
# True, once for each 22 rule
```

`stopped: group ''` means this shell has no `FSG`, such as a new terminal: run step 0.7's block,
then this one again.

Then read the rules back, as in 0.9:

```bash
aws ec2 describe-security-groups --profile "$P" --region "$FR" --group-ids "$FSG" \
  --query 'SecurityGroups[0].IpPermissions[].[IpProtocol,FromPort,ToPort,join(`","`,IpRanges[].CidrIp || `[]`),join(`","`,Ipv6Ranges[].CidrIpv6 || `[]`),join(`","`,UserIdGroupPairs[].GroupId || `[]`),join(`","`,PrefixListIds[].PrefixListId || `[]`)]' --output text
# the 80 and 443 lines only
```

Any other line is a rule that still admits something: Close revokes only rules for port 22
alone, never `-1` (all traffic) or a port range around 22. Remove such a rule by hand, and open
an issue.

Then, for a deployment with the end-to-end rig (a test VM, the test users and bots: SPEC §15.3),
run its scenario 9 against the host from a checkout of the deployed commit:
`ZAB_E2E_FILES_URL=https://<host> uv run --with boto3 python tests/e2e/run.py --scenarios 9` (it
sets `ZAB_LIVE=e2e`; without `ZAB_E2E_FILES_URL`, it checks `https://files.mathagents.org`). The
rig's repository, `ZAB_E2E_REPO`, is one of `<org>`'s: `MathAgents/zab-test` without it.

Keep `/opt/zab-files.old` until the next update: the rollback needs it.

### Rollback (A, then B)

When step 6 or 7 fails, or later, while `/opt/zab-files.old` exists. Port 22 is still open after
a failed step 6 or 7. Later, start in a new B: 0.1's `FILES_HOST=` and `URL=` lines (B's check
below uses them), step 0.5 on the network you share with an agent machine, then 0.6 to 0.9;
then log in, in A, as in step 1. In **A**, one chain puts the
previous code and its unit back, and restarts; it prints `stopped` if a command fails:

```bash
test -f /opt/zab-files.old/zab-files.service.installed && test ! -e /opt/zab-files.failed &&
  sudo mv -T /opt/zab-files /opt/zab-files.failed &&
  sudo mv -T /opt/zab-files.old /opt/zab-files &&
  sudo install -m 0644 /opt/zab-files/zab-files.service.installed /etc/systemd/system/zab-files.service &&
  sudo systemctl daemon-reload &&
  sudo systemctl restart zab-files &&
  sleep 5 && systemctl is-active zab-files ||
  echo "stopped: ls -d /opt/zab-files* tells where (below)"
# active
grep '^ExecStart=' /etc/systemd/system/zab-files.service
# step 1's line: ExecStart=/opt/zab-files/venv/bin/python /opt/zab-files/workshop/files/server.py
```

The rollback puts back the release before the running one, which is in `/opt/zab-files.old`: the
release this update's step 1 showed. The `grep` prints step 1's line, and B's curls print what
0.4 printed, `200` and `200 text/html; charset=utf-8`. Leave `/etc/zab-files/zab-files.env`,
which step 3 may have written: a release before #693 ignores it, and the next update reads it.

In **B**:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "https://$FILES_HOST/_zab/static/files.css"
# what 0.4 printed: 200
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' -H 'Sec-Fetch-Dest: document' "$URL"
# what 0.4 printed: 200 text/html; charset=utf-8
```

Still in **B**, save what the failed release logged, for an issue, on the laptop; alone,
since ssh may ask for your key's passphrase. The service logs neither SHAs nor tokens; read the
file before you send it:

```bash
ssh -o ForwardAgent=no -o ForwardX11=no ubuntu@"$FILES_HOST" \
  "sudo journalctl -u zab-files -u zab-files-staged --since '2 hours ago' --no-pager" > ~/zab-files-update.log
```

Then close port 22: step 8's two blocks, in B. `/opt/zab-files.failed` holds the failed release,
for a look; step 1 of the next attempt removes it.

**If a chain stopped between its two renames,** `ls -d /opt/zab-files*` shows no
`/opt/zab-files`:
- **the switch's,** with `/opt/zab-files.old` and `/opt/zab-files.new`:
  `sudo mv -T /opt/zab-files.old /opt/zab-files` is the whole rollback, since the unit and the
  service weren't changed yet;
- **the rollback's,** with `/opt/zab-files.failed` and `/opt/zab-files.old`: finish it from its
  second rename on.

```bash
test ! -e /opt/zab-files && test -d /opt/zab-files.failed &&
  sudo mv -T /opt/zab-files.old /opt/zab-files &&
  sudo install -m 0644 /opt/zab-files/zab-files.service.installed /etc/systemd/system/zab-files.service &&
  sudo systemctl daemon-reload &&
  sudo systemctl restart zab-files &&
  sleep 5 && systemctl is-active zab-files ||
  echo "stopped: ls -d /opt/zab-files*"
# active
```
