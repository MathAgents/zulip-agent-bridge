# Security policy

## Reporting a vulnerability

Don't report a security problem in a public issue or pull request. Report it privately through GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability): open this repository's *Security and quality* tab (under the ⋯ menu if it isn't shown) and choose *Report a vulnerability*, or go straight to [its form](https://github.com/MathAgents/zulip-agent-bridge/security/advisories/new). You need a GitHub account. The report is private to you and the repository's maintainers, unless they publish an advisory about it.

Include:

- **the zab version** (`zab --version`);
- **the backend**, Codex or Claude Code, and its version;
- **the steps** that reproduce the problem, with tokens, keys and addresses removed;
- **the impact:** what an attacker could read, run or change, and what access the attacker needs first: for example, a user the bot obeys, any member of the Zulip organization, text that an agent reads, a workshop organizer, or none at all, since the admin server's web console is on the internet.

If that tab offers no *Report a vulnerability*, open an issue that asks the maintainer for a private way to report, and put no detail of the problem in it.

## What happens next

The report reaches the maintainer privately. A fix ships in a release. Say in the report whether you want to be credited, and under what name.

## Before you report

The README's [security model](README.md#security-model-in-brief) and [SPEC §12](docs/SPEC.md#12-security-model) say what zab defends against and which risks it accepts: zab doesn't sandbox the agents, and an agent can read its own bot's Zulip key (SPEC §12.2). A way around a documented mitigation, or a risk wider than the one documented, is a vulnerability: report it as above.

**A files host's links are capabilities** ([SPEC §10.7](docs/SPEC.md#107-turn-snapshots-for-the-file-server), [`workshop/files/README.md`](workshop/files/README.md)). Anyone who has the link in an answer can open every file of that turn's snapshot, without signing in, and anyone who has a `zab share` link can open what was shared, until it is revoked. A browser that has already opened a share keeps its copy after that: the files host marks what it serves as cacheable for a year. That is by design, not a vulnerability. A way to find or guess such a link without being given one, to read a file the denylist leaves out, or, from one snapshot's page, to read the address or content of another page the reader has open, is a vulnerability.

## Verifying a release

Each release tag of this repository is signed with the maintainer's SSH key:

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFn751Mjzz953Fef520ptf66Acjv5XMPiFX/rCngmeFx
```

Its fingerprint is `SHA256:gTGOWot/5rxMwPKceRsy7CGeDWQMLK/LvuDeq0xzQds`. As a line of git's `allowed_signers` file:

```text
drew@math.mit.edu namespaces="git" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFn751Mjzz953Fef520ptf66Acjv5XMPiFX/rCngmeFx
```

To check a tag, with git 2.34 or newer and OpenSSH 8.2 or newer, in a clone of this repository, where the file goes in `.git/`, outside the work tree:

```sh
git clone https://github.com/MathAgents/zulip-agent-bridge.git && cd zulip-agent-bridge
echo 'drew@math.mit.edu namespaces="git" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFn751Mjzz953Fef520ptf66Acjv5XMPiFX/rCngmeFx' > .git/allowed_signers
git config gpg.ssh.allowedSignersFile "$PWD/.git/allowed_signers"
git tag -v vX.Y.Z
```

For a good tag, `git tag -v` exits 0 and prints the tag (`object <its commit>`, `type commit`, `tag vX.Y.Z`, the tagger and the message `zab vX.Y.Z`) and this line:

```text
Good "git" signature for drew@math.mit.edu with ED25519 key SHA256:gTGOWot/5rxMwPKceRsy7CGeDWQMLK/LvuDeq0xzQds
```

Check also that its `tag` line is the version you asked for: git checks the signature, not the tag's name. An older release's tag, moved under a newer name, prints the same `Good` line and exits 0, but its `tag` line names the older version. That tag is the maintainer's, but not the release you asked for: don't use it, and report it, as above.

Any other output exits 1, and the tag is not the maintainer's:
- **`Good "git" signature with ED25519 key SHA256:…`, then `No principal matched.`:** a valid signature by another key. It starts with `Good`, but names no address.
- **`error: no signature found`:** the tag is not signed.
- **`error: gpg.ssh.allowedSignersFile needs to be configured and exist …`:** the `git config` line didn't run in this clone.

A good signature says that the maintainer tagged that commit as the release. It is worth as much as your copy of the key: someone who could change a tag here could change this file too. Keep the line from the release you first verify, and report a later tag that fails against it, as above. A deployer of the admin server: [BOOTSTRAP §9](docs/BOOTSTRAP.md#9-the-release-review-and-sign-it) says when to add this line to your own `allowed_signers`, and when to sign a release yourself.
