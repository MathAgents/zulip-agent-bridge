#!/usr/bin/env bash
# Build the workshop AMI on a fresh Ubuntu 24.04 (amd64) builder instance.
# workshop/ami/README.md. `workshop/ami/build_ami.py` copies this script (and the files named
# by the knobs below) to the builder and runs it as `ubuntu` (with sudo); it can also be run by
# hand. It is idempotent, non-interactive and ends with the smoke test (README.md). Nothing here
# handles agent credentials: participants log in to both agents themselves on their own VM
# (SPEC §9.1), and the VMs carry no IAM role (workshop/aws/README.md, "The VMs"; IMDSv2 only).
#
# Knobs (environment variables):
#   CODEX_VERSION     Codex CLI release to install            (default 0.157.1, docs/DECISIONS.md, SPEC §8.3)
#   CLAUDE_VERSION    Claude Code version for the installer   (default 2.1.283, docs/DECISIONS.md)
#   ZAB_TAG           zab git tag to install with uv; required without ZAB_WHEEL (DECISIONS D16)
#   ZAB_WHEEL         path to a zab wheel copied to the builder; used instead of the git URL by
#                     a private deployment, built from the tag
#   ZAB_REPO          the repository ZAB_TAG is installed from, an https:// or ssh:// URL
#                     (default the public one, https://github.com/MathAgents/zulip-agent-bridge,
#                     aws_common.ZAB_REPO_URL; build_ami.py passes --zab-repo)
#   PARI_VERSION      PARI/GP release built from source       (default 2.17.4; sha256 pinned below)
#   MAGMA_TARBALL     tarball of a Magma installation tree (the `/opt/magma` layout: magma.exe,
#                     the `magma` driver script, libs/, package/, doc/, InternalHelp/); installed
#                     to /usr/local/magma. Optional: without it Magma is skipped.
#   MAGMA_PASSFILE    Magma's licence file (installed as /usr/local/magma/magmapassfile).
#                     Required with MAGMA_TARBALL.
#   MAGMA_LICENCE_SETUP  the deployer's script for whatever host-specific setup Magma's licence
#                     needs, an executable with a `#!` line (installed as
#                     /usr/local/sbin/magma-licence-setup, run as root at every boot by
#                     magma-licence.service). Optional, with MAGMA_TARBALL; never read here.
#   WITH_SAGE=1       also install SageMath through conda (adds ~10-20 min)
#   SAGE_VERSION      conda-forge sage version                (default 10.9, with SAGE_PYTHON 3.12)
#   LMFDB_MCP_URL     the LMFDB MCP server                    (default https://mcp.lmfdb.org/mcp)
#   AMI_FILES         workshop/ami/files (a directory, or a tarball of it, as build_ami.py copies
#                     it): the agents' instruction file, the smoke-test sources, the comparator
#                     wrapper. Default: the files/ directory next to this script.
#   LEAN_LSP_MCP_VERSION  lean-lsp-mcp from PyPI               (default 0.31.0)
#   LEAN4_SKILLS_TAG, LEAN4_SKILLS_SHA  the lean4-skills plugin (default v4.11.3 = b6243b8…)
#   IMAGE_NAME        the image's name in /etc/zab/tools.json (build_ami.py passes --name)
# Never commit the Magma files: build_ami.py copies them from Drew's machine for the build only.
set -euo pipefail

CODEX_VERSION="${CODEX_VERSION:-0.157.1}"   # the M1 pin (docs/DECISIONS.md, SPEC §8.3)
CLAUDE_VERSION="${CLAUDE_VERSION:-2.1.283}"
ZAB_TAG="${ZAB_TAG:-}"
ZAB_WHEEL="${ZAB_WHEEL:-}"
ZAB_REPO="${ZAB_REPO:-https://github.com/MathAgents/zulip-agent-bridge}"
PARI_VERSION="${PARI_VERSION:-2.17.4}"
MAGMA_TARBALL="${MAGMA_TARBALL:-}"
MAGMA_PASSFILE="${MAGMA_PASSFILE:-}"
MAGMA_LICENCE_SETUP="${MAGMA_LICENCE_SETUP:-}"
SAGE_VERSION="${SAGE_VERSION:-10.9}"
SAGE_PYTHON="${SAGE_PYTHON:-3.12}"
LMFDB_MCP_URL="${LMFDB_MCP_URL:-https://mcp.lmfdb.org/mcp}"
AMI_FILES="${AMI_FILES:-$(dirname "$(realpath "$0")")/files}"
LEAN_LSP_MCP_VERSION="${LEAN_LSP_MCP_VERSION:-0.31.0}"
LEAN4_SKILLS_TAG="${LEAN4_SKILLS_TAG:-v4.11.3}"
LEAN4_SKILLS_SHA="${LEAN4_SKILLS_SHA:-b6243b85b9b0a0ddff5bb6773889044daf687f8e}"
# AMI v3 (Drew, Sun 20:20 on #36): Julia from the official tarball (sha256 from
# julialang-s3.julialang.org/bin/versions.json), the newest release line Oscar's CI tests (1.10,
# 1.12; 1.13 only as nightly at Oscar v1.8.2), and Oscar from the committed, pinned Manifest.
JULIA_VERSION="${JULIA_VERSION:-1.12.7}"
JULIA_SHA256="${JULIA_SHA256:-4e7e9e776634d24835250de67cde39b0d4af15bc432eb20697e6be6c28ea69e8}"
OSCAR_VERSION="${OSCAR_VERSION:-1.8.2}"
# certbot for Let's Encrypt IP address certificates (refserve, rc6): --ip-address since 5.3.0,
# webroot since 5.4.0; pinned with hashes in files/certbot-requirements.txt (uv pip compile).
CERTBOT_VERSION="${CERTBOT_VERSION:-5.8.0}"
# The image's name for /etc/zab/tools.json (issue #474).
IMAGE_NAME="${IMAGE_NAME:-unnamed}"
AGENT=agent
AGENT_HOME=/home/$AGENT

# PARI/GP source and data packages from https://pari.math.u-bordeaux.fr/pub/pari/ (sha256 recorded
# on 2026-10-02 from the downloaded files; docs/DECISIONS.md). The data packages are the ones the
# Ubuntu pari-* packages used to supply: elldata (Cremona's tables), galdata (polgalois in degree
# 8-11), seadata (SEA point counting), galpol (galoisgetpol) and nflistdata (nflist).
PARI_SHA256="02651d99c391007d384b3fadbc20abc6916b77036f9e496c99e9ce8688ca4b53"
declare -A PARI_DATA_SHA256=(
  [elldata]="dd551e64932d4ab27b3f2b2d1da871c2353672fc1a74705c52e3c0de84bd0cf6"
  [galdata]="b7c1650099b24a20bdade47a85a928351c586287f0d4c73933313873e63290dd"
  [seadata]="c9282a525ea3f92c1f9c6c69e37ac5a87b48fb9ccd943cfd7c881a3851195833"
  [galpol]="562af28316ee335ee38c1172c2d5ecccb79f55c368fb9f2c6f40fc0f416bb01b"
  [nflistdata]="2c19a3e02afd3bba2af3071a7faa80924a75b00bb9713286c886b7fb460944bc"
)
PARI_URL="https://pari.math.u-bordeaux.fr/pub/pari"
# The Codex release package of rust-v0.157.1 (sha256 of the downloaded asset, 2026-10-03). Its
# bin/codex is byte-identical to the plain codex-x86_64-unknown-linux-musl binary v1 installed;
# the package adds bin/codex-code-mode-host, which 0.157.1 runs every tool call through
# (feature code_mode_host, stable and on by default) and without which no shell command runs
# (AMI v1, PM on #36), plus codex-path/rg and codex-resources/bwrap.
CODEX_PKG_SHA256="${CODEX_PKG_SHA256:-0e211868c9fd73cb49ad35ac675b5eafdf6b9f453df8a493df980c59a590fe5f}"

log() { printf '\n==> %s\n' "$*"; }
magma_layout() {  # tarball → "nested" (one top-level directory holding magma.exe) or "flat"
  # The whole listing is consumed first: `tar -tzf … | head -n1` would hit SIGPIPE on a large
  # archive and, under pipefail, abort the build (codex round 1 on PR #56).
  local listing top
  listing=$(mktemp)
  tar -tzf "$1" > "$listing"
  top=$(head -n1 "$listing" | cut -d/ -f1)
  if [ -n "$top" ] && grep -qxF "$top/magma.exe" "$listing"; then echo nested; else echo flat; fi
  rm -f "$listing"
}
as_agent() { sudo -u "$AGENT" -H env HOME="$AGENT_HOME" PATH="$AGENT_HOME/.local/bin:$PATH" "$@"; }
fetch_checked() {  # sha256 dest url...: download once (first url that answers), verify, fail loudly
  local sum=$1 dest=$2 url
  shift 2
  if [ ! -f "$dest" ] || ! echo "$sum  $dest" | sha256sum --check --status; then
    for url in "$@"; do  # the PARI mirror cut a 57 MB transfer short once (curl 18): retry
      if curl -fsSL --retry 5 --retry-all-errors --retry-delay 10 "$url" -o "$dest"; then break; fi
    done
  fi
  echo "$sum  $dest" | sha256sum --check --status || { echo "checksum mismatch or download failed: $*" >&2; exit 1; }
}

if [ "$(id -u)" -eq 0 ]; then
  echo "run this as ubuntu (it uses sudo), not as root" >&2
  exit 1
fi
if ! grep -q 'VERSION_ID="24.04"' /etc/os-release; then
  echo "expected Ubuntu 24.04; refusing to continue" >&2
  exit 1
fi
if [ -z "$ZAB_WHEEL" ] && [ -z "$ZAB_TAG" ]; then
  echo "ZAB_TAG is required without ZAB_WHEEL: the release to install from $ZAB_REPO" >&2
  exit 1
fi
if [ -n "$MAGMA_TARBALL" ] && [ -z "$MAGMA_PASSFILE" ]; then
  echo "MAGMA_TARBALL needs MAGMA_PASSFILE too" >&2
  exit 1
fi
if [ -n "$MAGMA_LICENCE_SETUP" ] && [ -z "$MAGMA_TARBALL" ]; then
  echo "MAGMA_LICENCE_SETUP is for Magma: it needs MAGMA_TARBALL and MAGMA_PASSFILE" >&2
  exit 1
fi
# Work from a directory every user can read. uv, run as agent below, looks for uv.toml in the
# working directory and its parents and aborts on an unreadable one, such as /home/ubuntu (0750
# on 24.04), where `ssh ubuntu@builder 'bash build.sh'` starts.
for var in ZAB_WHEEL MAGMA_TARBALL MAGMA_PASSFILE MAGMA_LICENCE_SETUP AMI_FILES; do
  if [ -n "${!var}" ]; then printf -v "$var" '%s' "$(realpath -- "${!var}")"; fi
done
cd /
# The files to install, as a directory every user can read (agent reads the smoke sources).
FILES=/usr/local/share/workshop
if [ -d "$AMI_FILES" ]; then
  sudo install -d -m 0755 "$FILES"
  sudo cp -r "$AMI_FILES"/. "$FILES"/
elif [ -f "$AMI_FILES" ]; then
  sudo install -d -m 0755 "$FILES"
  sudo tar -xzf "$AMI_FILES" -C "$FILES"
else
  echo "AMI_FILES: no directory or tarball at $AMI_FILES" >&2
  exit 1
fi
sudo chown -R root:root "$FILES"
sudo chmod -R a+rX,go-w "$FILES"

# --- 1. system packages (README.md, "What the image holds") ----------------------------------
log "apt packages"
# A fresh instance may still be running cloud-init's own package steps: wait for it, then let
# apt wait for the dpkg lock instead of failing on it, and retry the index update (mirrors do
# time out). sudo resets the environment, so DEBIAN_FRONTEND is passed through `env`.
cloud-init status --wait >/dev/null 2>&1 || true
APT="sudo env DEBIAN_FRONTEND=noninteractive apt-get -q -o DPkg::Lock::Timeout=600"
apt_update() {
  local attempt
  for attempt in 1 2 3; do
    if $APT update; then return 0; fi
    [ "$attempt" -lt 3 ] || { echo "apt-get update failed three times" >&2; exit 1; }
    sleep 15
  done
}
apt_update
# python3-dev: cypari2 ships manylinux wheels for CPython 3.12 (checked on PyPI, 2.2.4), but if
# pip ever falls back to the sdist it compiles against the Python headers and the PARI built
# into /usr/local in step 3. libgmp-dev and libreadline-dev are what that PARI build needs. The GAP
# data packages are only Recommends of gap, which --no-install-recommends would drop: without
# them SmallGroup and friends fail. They are listed explicitly instead.
$APT install -y --no-install-recommends \
  build-essential git python3 python3-dev python3-venv pipx tmux screen htop ripgrep jq unzip \
  curl ssh-import-id libgmp-dev libreadline-dev libflint-dev gap ca-certificates \
  gap-doc gap-dev gap-transgrp gap-primgrp gap-smallgrp gap-autpgrp gap-alnuth \
  gap-character-tables gap-factint gap-fga gap-laguna gap-polycyclic gap-table-of-marks
# Ubuntu's pari-gp (2.15.4) stays installed: gap-alnuth depends on it, and purging it took
# Alnuth away (codex review on #56). It lives at /usr/bin/gp; the gp on PATH is the 2.17.4 built
# into /usr/local in step 3, which comes first (the smoke test checks both). The apt data
# packages are not installed (no Recommends), the 2.17.4 data goes to /usr/local/share/pari.
$APT autoremove -y
# pari-gp's conffile /etc/gprc (`colors = lightbg`, `lines = 40`) is read by every gp, the 2.17.4
# one included, and makes piped results come back colour-coded and cut off at 40 lines with
# `[+++]` (PM review on #60). Delete it; dpkg keeps a deleted conffile deleted on upgrades.
sudo rm -f /etc/gprc

# --- 1b. LaTeX (PM's AMI v2 list, item 1) -----------------------------------------------------
# TeX Live 2023 from noble. The PM's list, plus babel for European, French and German papers,
# pstricks, the plain-generic macros, latexindent/texcount (extra-utils) and pygments for
# minted: 271 packages, about 3.3 GB installed with --no-install-recommends, against 7.95 GB for
# texlive-full (2.5 GB of it documentation; apt-cache on noble, 2026-10-03). Coverage checked
# by package: amsart (latex-base), tikz-cd, xy, pgfplots (pictures), cleveref, thmtools,
# enumitem (latex-extra), biblatex (bibtex-extra) with biber 2.19, stmaryrd and quiver.sty
# (science), mathrsfs, mathtools, microtype (latex-recommended). The agent user has
# passwordless sudo, so a missing package is one `apt install` away.
log "TeX Live"
$APT install -y --no-install-recommends \
  texlive-latex-base texlive-latex-recommended texlive-latex-extra texlive-science \
  texlive-pictures texlive-publishers texlive-bibtex-extra texlive-fonts-recommended \
  texlive-fonts-extra cm-super texlive-luatex texlive-xetex biber latexmk chktex latexdiff \
  texlive-lang-european texlive-lang-french texlive-lang-german texlive-pstricks \
  texlive-plain-generic texlive-extra-utils python3-pygments

# --- 1c. the wider stack from noble (issue #474) -------------------------------------------
# Drew, Oct 18 (the software-stack proposal's two tiers, for SLmath and Harvard): Singular,
# polymake and Maxima on PATH; R 4.3 with the recommended packages, r-base-dev for
# install.packages(), tidyverse, data.table, rmarkdown, knitr and the Jupyter kernel (Debian's
# r-cran-* builds: nothing is compiled from CRAN); Octave 8.4; the figure and PDF tools; z3,
# CaDiCaL and pandoc; and the programs Macaulay2 calls (its Recommends, which
# --no-install-recommends drops; noble's are the ones the PPA builds against). bison builds
# atlas (step 3b). All from noble's archive, like the TeX Live above.
log "the wider stack (apt)"
$APT install -y --no-install-recommends \
  singular polymake maxima maxima-share r-base-core r-base-dev r-recommended r-cran-tidyverse \
  r-cran-data.table r-cran-rmarkdown r-cran-knitr r-cran-irkernel octave graphviz gnuplot-nox \
  imagemagick poppler-utils librsvg2-bin ghostscript z3 cadical pandoc 4ti2 lrslib nauty \
  normaliz topcom coinor-csdp cohomcalg bison gpg

# --- 1d. Macaulay2 from its official PPA (issue #474) -------------------------------------
# noble's macaulay2 is 1.22; the Macaulay2 team's PPA (ppa:macaulay2/macaulay2, their
# documented route for Ubuntu) builds the current release for noble, with newer gfan and msolve.
# Its signing key is committed (files/apt/macaulay2-ppa.asc, from keyserver.ubuntu.com) and
# checked against the fingerprint Launchpad publishes for the archive (the file must hold that
# one key: apt trusts every key in it); apt then checks every package against the signed index.
# The source is Launchpad's snapshot of the PPA at M2_PPA_SNAPSHOT, not the PPA itself: a PPA
# keeps only its newest upload, while a snapshot serves the same signed index and files for
# good, so Macaulay2, gfan and msolve stay exactly as tested here and on later rebuilds, and the
# VMs' own `apt upgrade` never pulls a newer upload. The versions are pinned on top. To move to
# a newer Macaulay2 (review #481): pick a later snapshot time (UTC, YYYYMMDDTHHMMSSZ), read its
# dists/noble/main/binary-amd64/Packages for the new versions, change M2_PPA_SNAPSHOT and the
# three versions below and their test (tests/workshop/test_ami_stack.py) in a PR, and rebuild.
M2_PPA_FPR=5B17CD31DA995CFAA1B0D4FD08416FF271BC6AA8
M2_PPA_SNAPSHOT=20261018T000000Z
M2_VERSION=1.26.06+ds-2~ubuntu24.04.1        # macaulay2, macaulay2-common
M2_GFAN_VERSION=0.8~beta-2~ubuntu24.04.1
M2_MSOLVE_VERSION=0.9.4-2ppa2~ubuntu24.04.1  # msolve, libmsolve3
log "Macaulay2 $M2_VERSION (PPA snapshot $M2_PPA_SNAPSHOT)"
gnupg=$(mktemp -d)
keys=$(gpg --homedir "$gnupg" --show-keys --with-colons "$FILES/apt/macaulay2-ppa.asc")
rm -rf "$gnupg"
got=$(awk -F: '$1 == "fpr" && !n++ {print $10}' <<<"$keys")  # the primary key's fingerprint
[ "$(grep -c '^pub:' <<<"$keys")" = 1 ] && [ "$got" = "$M2_PPA_FPR" ] \
  || { echo "the Macaulay2 PPA key file must hold exactly key $M2_PPA_FPR (first: '$got')" >&2; exit 1; }
sudo install -d -m 0755 /etc/apt/keyrings
sudo install -m 0644 "$FILES/apt/macaulay2-ppa.asc" /etc/apt/keyrings/macaulay2-ppa.asc
printf '%s\n' 'Types: deb' \
  "URIs: https://snapshot.ppa.launchpadcontent.net/macaulay2/macaulay2/ubuntu/$M2_PPA_SNAPSHOT" \
  'Suites: noble' 'Components: main' 'Signed-By: /etc/apt/keyrings/macaulay2-ppa.asc' \
  | sudo tee /etc/apt/sources.list.d/macaulay2.sources >/dev/null
apt_update
$APT install -y --no-install-recommends "macaulay2=$M2_VERSION" "macaulay2-common=$M2_VERSION" \
  "gfan=$M2_GFAN_VERSION" "msolve=$M2_MSOLVE_VERSION" "libmsolve3=$M2_MSOLVE_VERSION"

# --- 2. the participant's user -----------------------------------------------------------------
log "user $AGENT"
if ! id "$AGENT" >/dev/null 2>&1; then
  sudo adduser --disabled-password --gecos "workshop participant" "$AGENT"
fi
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$AGENT" | sudo tee /etc/sudoers.d/$AGENT >/dev/null
sudo chmod 0440 /etc/sudoers.d/$AGENT
sudo visudo -cf /etc/sudoers.d/$AGENT
sudo loginctl enable-linger "$AGENT"
# Every intermediate directory is named explicitly: `install -d` applies -o/-g/-m only to the
# directories it is given and creates missing parents with root's defaults, which would leave
# ~agent/.local root-owned and break the uv and Claude installs run as agent.
sudo install -d -o "$AGENT" -g "$AGENT" -m 0755 "$AGENT_HOME/work" "$AGENT_HOME/.local" \
  "$AGENT_HOME/.local/bin" "$AGENT_HOME/.config" "$AGENT_HOME/.codex"
sudo install -d -o "$AGENT" -g "$AGENT" -m 0700 "$AGENT_HOME/.ssh"
# No GitHub SSH key is imported at first boot (README.md, "First boot"): participants get no SSH.

# --- 3. PARI/GP from source, with the data packages -------------------------------------------
# Built before the Python environment, so that a cypari2 source build (if pip ever falls back
# to the sdist; codex round 7 on PR #56) finds this PARI's headers and library in /usr/local.
# The gp on PATH, /usr/local/bin/gp, with GMP and readline, ahead of Ubuntu's 2.15.4 at
# /usr/bin/gp, which stays for Alnuth (step 1); the data packages
# go to /usr/local/share/pari/<name>/ (the tarballs unpack as data/<name>/).
log "PARI/GP $PARI_VERSION from source"
if ! [ -x /usr/local/bin/gp ] || [ "$(/usr/local/bin/gp --version-short 2>/dev/null)" != "$PARI_VERSION" ]; then
  src=$(mktemp -d /tmp/pari-src.XXXXXX)
  # the current release sits in unix/; after the next release it moves to unix/OLD/<major.minor>/
  fetch_checked "$PARI_SHA256" "$src/pari-$PARI_VERSION.tar.gz" \
    "$PARI_URL/unix/pari-$PARI_VERSION.tar.gz" "$PARI_URL/unix/OLD/${PARI_VERSION%.*}/pari-$PARI_VERSION.tar.gz"
  tar -xzf "$src/pari-$PARI_VERSION.tar.gz" -C "$src"
  ( cd "$src/pari-$PARI_VERSION" \
    && ./Configure --prefix=/usr/local --with-gmp --with-readline >/dev/null \
    && make -j"$(nproc)" gp >/dev/null \
    && sudo make install-nodata >/dev/null )  # not `install`: that also builds the TeX docs
  rm -rf "$src"
fi
sudo ldconfig
sudo install -d -m 0755 /usr/local/share/pari
for name in "${!PARI_DATA_SHA256[@]}"; do
  if [ ! -d "/usr/local/share/pari/$name" ]; then
    fetch_checked "${PARI_DATA_SHA256[$name]}" "/tmp/$name.tgz" "$PARI_URL/packages/$name.tgz"
    sudo tar -xzf "/tmp/$name.tgz" -C /usr/local/share/pari --strip-components=1
    rm -f "/tmp/$name.tgz"
  fi
done

# --- 3b. atlas, the Atlas of Lie Groups and Representations software (issue #474) ---------
# Built from source as its INSTALL file describes for a system-wide installation (liegroups.org
# names no other route for Linux): optimized, the binaries, messages/ and atlas-scripts/ in
# /usr/local/atlas, and /usr/local/bin/atlas the script `make install` writes, which runs it with
# `--path=/usr/local/atlas/atlas-scripts all.at` (the whole library). No release has been
# tagged since 1.0 (2017); the project's `latest` branch carries its releases into master, and
# its commit 0416c2a of Oct 13, 2026 set the version to 2.0 (2.1 for the axis language). GitHub's
# archive of that commit, checked against its sha256 (two downloads, Oct 18, identical).
ATLAS_COMMIT=0416c2a2c02b8448376ddf3744e340730a5172d4
ATLAS_SHA256=07508ed9710702c6024eb5b3789d6ee9527307133e0945bbf5d6e83229d77914
log "atlas ${ATLAS_COMMIT:0:7}"
if ! [ -x /usr/local/atlas/atlas ] || [ "$(cat /usr/local/atlas/COMMIT 2>/dev/null)" != "$ATLAS_COMMIT" ]; then
  src=$(mktemp -d /tmp/atlas-src.XXXXXX)
  fetch_checked "$ATLAS_SHA256" "$src/atlas.tar.gz" \
    "https://github.com/jeffreyadams/atlasofliegroups/archive/$ATLAS_COMMIT.tar.gz"
  tar -xzf "$src/atlas.tar.gz" -C "$src"
  # INSTALLDIR at compile time too: the messages' directory is compiled into Fokko (INSTALL).
  ( cd "$src/atlasofliegroups-$ATLAS_COMMIT" \
    && make -j"$(nproc)" optimize=true INSTALLDIR=/usr/local/atlas >/dev/null \
    && sudo make install optimize=true INSTALLDIR=/usr/local/atlas BINDIR=/usr/local/bin >/dev/null )
  sudo chmod 0755 /usr/local/bin/atlas  # written under sudo's umask
  echo "$ATLAS_COMMIT" | sudo tee /usr/local/atlas/COMMIT >/dev/null
  rm -rf "$src"
fi

# --- 4. uv and the Python environment ---------------------------------------------------------
log "uv"
if ! as_agent test -x "$AGENT_HOME/.local/bin/uv"; then
  as_agent bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --no-modify-path'
fi
log "python venv ~/venv"
# --allow-existing: current uv refuses an existing venv without it, and build.sh must rerun
# (review on #481).
as_agent "$AGENT_HOME/.local/bin/uv" venv --python 3.12 -q --allow-existing "$AGENT_HOME/venv"
# Every package pinned with its hashes (files/venv-requirements.in says how it was compiled;
# issue #474): numpy, sympy, matplotlib, jupyter, cypari2, and the wider stack's numerics,
# statistics, optimization and topology. 150 packages, about 1.3 GB. Only PyX, SnapPy's pure
# Python figure library, comes as a source distribution; --require-hashes doesn't cover what
# building it fetches, so its build backend is hash-pinned through --build-constraints.
as_agent "$AGENT_HOME/.local/bin/uv" pip install -q --python "$AGENT_HOME/venv/bin/python" \
  --require-hashes -r "$FILES/venv-requirements.txt" \
  --build-constraints "$FILES/venv-build-constraints.txt"
# The R kernel for Jupyter (r-cran-irkernel, step 1c), registered for agent in
# ~/.local/share/jupyter/kernels/ir; installspec runs `jupyter kernelspec`, from the venv.
as_agent env PATH="$AGENT_HOME/venv/bin:$PATH" \
  Rscript -e 'IRkernel::installspec(name = "ir", displayname = "R")' >/dev/null
# Make the venv the default python for interactive shells (agents inherit it through the login shell).
as_agent bash -c 'grep -q "venv/bin/activate" ~/.bashrc || printf "\n# workshop python\n. ~/venv/bin/activate\nexport PATH=\$HOME/.local/bin:\$PATH\n" >> ~/.bashrc'

# --- 4b. lean-lsp-mcp (PM's AMI v2 list, item 3) --------------------------------------------
# Installed as a uv tool, so that nothing is downloaded when an agent starts it; it needs Python
# 3.10 or newer and finds the Lean project from the file paths it is given (no
# LEAN_PROJECT_PATH), so it works in the participants' own repositories too. Registered with
# both agents in steps 5 and 6.
log "lean-lsp-mcp $LEAN_LSP_MCP_VERSION"
# --exclude-newer bounds the unpinned transitive dependencies to what existed at the pin.
as_agent "$AGENT_HOME/.local/bin/uv" tool install -q --force --exclude-newer 2026-10-04T00:00:00Z \
  "lean-lsp-mcp==$LEAN_LSP_MCP_VERSION"

# --- 5. Codex CLI from the GitHub release package (SPEC §8.3) ---------------------------------
# The whole package goes to ~/.local/lib/codex-<version>, with ~/.local/bin/codex a symlink to
# its bin/codex. Codex canonicalizes its own path, recognises the package by bin/ next to
# codex-package.json, and finds codex-code-mode-host and bwrap there (codex-rs
# install-context/src/lib.rs at rust-v0.157.1). The members are named explicitly: v1 picked the
# binary with `find | head -n1` and shipped without the host (PM on #36).
log "codex $CODEX_VERSION"
CODEX_URL="https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-package-x86_64-unknown-linux-musl.tar.gz"
CODEX_DIR="$AGENT_HOME/.local/lib/codex-$CODEX_VERSION"
if ! as_agent test -x "$CODEX_DIR/bin/codex-code-mode-host" \
  || ! as_agent "$AGENT_HOME/.local/bin/codex" --version 2>/dev/null | grep -q "$CODEX_VERSION"; then
  tmp=$(mktemp -d)
  chmod 0755 "$tmp"
  fetch_checked "$CODEX_PKG_SHA256" "$tmp/codex-package.tar.gz" "$CODEX_URL"
  sudo install -d -o "$AGENT" -g "$AGENT" -m 0755 "$AGENT_HOME/.local/lib"
  as_agent rm -rf "$CODEX_DIR"
  as_agent mkdir "$CODEX_DIR"
  as_agent tar -xzf "$tmp/codex-package.tar.gz" -C "$CODEX_DIR" \
    bin/codex bin/codex-code-mode-host codex-package.json codex-path codex-resources
  as_agent ln -sfn "$CODEX_DIR/bin/codex" "$AGENT_HOME/.local/bin/codex"
  rm -rf "$tmp"
fi
# Codex config (README.md, "What the image holds"): approvals off, full access; the update
# check off (the VMs are pinned, a Codex upgrade needs a bridge release with the new pin,
# SPEC §8.3); the LMFDB MCP server.
# `check_for_update_on_startup` and `mcp_servers.<id>.url` are in the 0.157.1 config schema
# (codex-rs/core/config.schema.json at rust-v0.157.1). Same lines as the first boot
# (README.md, "First boot").
codexrc="$AGENT_HOME/.codex/config.toml"
if ! as_agent test -f "$codexrc"; then
  printf '# workshop defaults (zab: workshop/ami/README.md)\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n' \
    | sudo -u "$AGENT" tee "$codexrc" >/dev/null
fi
# Everything under /home/agent is read as agent: the home is 0750 on 24.04, so a test, grep or
# cat run as ubuntu fails there (codex round 2 on PR #56; the smoke test checks the same way).
if ! as_agent grep -q '^check_for_update_on_startup' "$codexrc"; then
  # prepended, so that it is a root key even when the file ends inside a table
  as_agent bash -c 'printf "check_for_update_on_startup = false\n" | cat - "$1" > "$1.tmp" && mv "$1.tmp" "$1"' _ "$codexrc"
fi
as_agent grep -q '^\[mcp_servers\.lmfdb\]' "$codexrc" \
  || printf '\n[mcp_servers.lmfdb]\nurl = "%s"\n' "$LMFDB_MCP_URL" | sudo -u "$AGENT" tee -a "$codexrc" >/dev/null
# lean-lsp-mcp over stdio (keys `command`, `startup_timeout_sec`, `tool_timeout_sec` in the
# 0.157.1 config schema; defaults 30 s and 300 s). A Mathlib build or a slow goal can take
# minutes, so the tool timeout is 15 minutes.
as_agent grep -q '^\[mcp_servers\.lean-lsp\]' "$codexrc" \
  || printf '\n[mcp_servers.lean-lsp]\ncommand = "%s"\nstartup_timeout_sec = 60\ntool_timeout_sec = 900\n' \
    "$AGENT_HOME/.local/bin/lean-lsp-mcp" | sudo -u "$AGENT" tee -a "$codexrc" >/dev/null
# The lean4-skills plugin at a pinned commit (`--ref` clones, then checks the commit out; codex-rs
# at rust-v0.157.1). Not run on the development machine, which doesn't run Codex, so a failure is a
# warning, and the smoke test reports the plugin without failing on it. Its hooks stay inactive
# under the app-server until trusted (untrusted hooks are skipped silently); the participant can
# review and trust them in the TUI's /hooks. The skill itself works without them.
if ! as_agent bash -c 'timeout 60 codex plugin list </dev/null 2>/dev/null | grep -q "lean4@lean4-skills"'; then
  if ! as_agent bash -c 'timeout 300 codex plugin marketplace add cameronfreer/lean4-skills --ref "$1" </dev/null \
      && timeout 300 codex plugin add lean4@lean4-skills </dev/null' _ "$LEAN4_SKILLS_SHA"; then
    echo "WARNING: the lean4 plugin for Codex did not install (see above); continuing" >&2
  fi
fi

# --- 6. Claude Code with the native installer -------------------------------------------------
# The installer takes `stable`, `latest` or a version (checked in https://claude.ai/install.sh on
# 2026-10-02: `bash -s <version>` runs `claude install <version>`).
log "claude $CLAUDE_VERSION"
if ! as_agent "$AGENT_HOME/.local/bin/claude" --version 2>/dev/null | grep -q "$CLAUDE_VERSION"; then
  as_agent bash -c "curl -fsSL https://claude.ai/install.sh | bash -s $CLAUDE_VERSION"
fi
# Auto-updater off through the managed settings file: the participant's own settings cannot
# override it, and the CLI reads it under zab.service too. Verified on 2.1.285: `claude doctor`
# then reports "Auto-updates: disabled (set by env: DISABLE_AUTOUPDATER)". Same as the first boot.
sudo install -d -m 0755 /etc/claude-code
# claude.ai connectors off as well (Drew, Sun 20:42 on #36; DECISIONS): an interactive `claude`
# over SSH must not reach the participant's Gmail or Drive either. Merged into an existing file.
sudo python3 - <<'PY'
import json, os
path = "/etc/claude-code/managed-settings.json"
data = json.load(open(path)) if os.path.exists(path) else {}
data.setdefault("env", {})["DISABLE_AUTOUPDATER"] = "1"
data["disableClaudeAiConnectors"] = True
with open(path, "w") as f:
    json.dump(data, f)
PY
sudo chmod 0644 /etc/claude-code/managed-settings.json
# The LMFDB MCP server at user scope. `claude mcp add --scope user` writes `mcpServers` plus
# first-start bookkeeping to ~/.claude.json and nothing about onboarding (verified on 2.1.285), so
# the participant's first login still runs it. The builder's machine and user ids are dropped in
# the housekeeping step at the very end (after the smoke test ran claude); the CLI generates
# fresh ones on each VM.
as_agent "$AGENT_HOME/.local/bin/claude" mcp get lmfdb >/dev/null 2>&1 \
  || as_agent "$AGENT_HOME/.local/bin/claude" mcp add --transport http --scope user lmfdb "$LMFDB_MCP_URL"
# lean-lsp-mcp at user scope, by absolute path (the CLI does not expand `~`). `mcp get` starts
# the server, so it doubles as the smoke check.
as_agent "$AGENT_HOME/.local/bin/claude" mcp get lean-lsp >/dev/null 2>&1 \
  || as_agent "$AGENT_HOME/.local/bin/claude" mcp add --scope user lean-lsp -- "$AGENT_HOME/.local/bin/lean-lsp-mcp"
# The lean4-skills plugin (not its lean4-contribute sibling, which files GitHub issues). The
# marketplace clones with `git clone --branch`, so it takes a tag, not a sha (verified on
# 2.1.288): pin the tag and check the commit. Its hooks run under `claude -p` with zab's flags
# and no trust prompt, and its guardrails allow commits and pushes (they block `reset --hard`
# and force pushes inside Lean projects only); issue #36, AMI v2 item 3.
if ! as_agent bash -c 'claude plugin list --json </dev/null 2>/dev/null | jq -e ".[] | select(.id == \"lean4@lean4-skills\")" >/dev/null'; then
  as_agent "$AGENT_HOME/.local/bin/claude" plugin marketplace add \
    "https://github.com/cameronfreer/lean4-skills.git#$LEAN4_SKILLS_TAG" </dev/null
  as_agent "$AGENT_HOME/.local/bin/claude" plugin install lean4@lean4-skills </dev/null
fi
got=$(as_agent git -C "$AGENT_HOME/.claude/plugins/marketplaces/lean4-skills" rev-parse HEAD)
[ "$got" = "$LEAN4_SKILLS_SHA" ] || { echo "lean4-skills is at $got, expected $LEAN4_SKILLS_SHA" >&2; exit 1; }

# --- 7. zab (tools only; the bridge is configured at rollout, ../admin/README.md §2) -----------
log "zab $ZAB_TAG"
if [ -n "$ZAB_WHEEL" ]; then
  # The wheel usually sits under /home/ubuntu, which agent cannot traverse (home directories
  # are 0750 on Ubuntu 24.04), so stage an agent-readable copy in a world-searchable directory.
  staged=$(mktemp -d /tmp/zab-wheel.XXXXXX)
  chmod 0755 "$staged"
  sudo install -o "$AGENT" -g "$AGENT" -m 0644 "$ZAB_WHEEL" "$staged/$(basename "$ZAB_WHEEL")"
  as_agent "$AGENT_HOME/.local/bin/uv" tool install -q --force "$staged/$(basename "$ZAB_WHEEL")"
  rm -rf "$staged"
else
  as_agent "$AGENT_HOME/.local/bin/uv" tool install -q --force "git+${ZAB_REPO}@${ZAB_TAG}"
fi

# --- 8. Magma (optional) ----------------------------------------------------------------------
# Magma is installed under the deployer's licence. Besides its licence file, Magma's licence may
# need host-specific setup at every boot: the deployer supplies a script that performs it
# (MAGMA_LICENCE_SETUP), and the image runs it as root through magma-licence.service, a oneshot
# ordered before logins and user services (so before the bridge and the agents start Magma).
# build.sh neither reads nor checks the script (build_ami.py looks only at its #! line); the smoke
# test checks that Magma runs (README.md, "Magma's licence"). Logins wait for the network and
# then for the script: 120 s to start, and 10 s for each of systemd's two stop signals if it
# overruns, so a script that hangs holds them back for about two and a half minutes at most.
if [ -n "$MAGMA_TARBALL" ]; then
  log "Magma"
  sudo install -d -m 0755 /usr/local/magma
  # The tarball may carry the tree directly or under one top-level directory (e.g. `magma/`).
  if [ "$(magma_layout "$MAGMA_TARBALL")" = nested ]; then
    sudo tar -xzf "$MAGMA_TARBALL" -C /usr/local/magma --strip-components=1
  else
    sudo tar -xzf "$MAGMA_TARBALL" -C /usr/local/magma
  fi
  sudo chown -R root:root /usr/local/magma
  sudo chmod 0755 /usr/local/magma/magma /usr/local/magma/magma.exe
  sudo install -m 0644 "$MAGMA_PASSFILE" /usr/local/magma/magmapassfile
  sudo rm -f /usr/local/magma/magmapassfile_cloud /usr/local/magma/oldmagmapassfile
  # The driver computes ROOT from its own path (readlink -f); an older driver with a hardcoded
  # ROOT is rewritten, and nothing may still point at /opt/magma (PM review on #56).
  if ! grep -q 'readlink -f' /usr/local/magma/magma; then
    sudo sed -i 's|^ROOT=.*|ROOT="/usr/local/magma"|' /usr/local/magma/magma
  fi
  if grep -q '/opt/magma' /usr/local/magma/magma; then
    echo "the magma driver still refers to /opt/magma; fix the tarball's driver script" >&2
    exit 1
  fi
  sudo ln -sf /usr/local/magma/magma /usr/local/bin/magma
  if [ -n "$MAGMA_LICENCE_SETUP" ]; then
    sudo install -o root -g root -m 0700 "$MAGMA_LICENCE_SETUP" /usr/local/sbin/magma-licence-setup
    sudo tee /etc/systemd/system/magma-licence.service >/dev/null <<'EOF'
[Unit]
Description=The deployer's Magma licence setup (zab: workshop/ami/README.md)
Wants=network-online.target
After=network-online.target
Before=systemd-user-sessions.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/magma-licence-setup
RemainAfterExit=yes
TimeoutStartSec=120
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable magma-licence.service >/dev/null 2>&1
    # Once per boot, as at boot: on a second run of build.sh the unit is active and stays so.
    if ! sudo systemctl start magma-licence.service; then
      echo "the Magma licence setup failed: sudo journalctl -u magma-licence.service" >&2
      exit 1
    fi
  else
    echo "no MAGMA_LICENCE_SETUP: Magma runs only where its licence file alone suffices"
  fi
fi

# --- 9. SageMath through conda ----------------------------------------------------------------
# conda-forge has sage 10.9 builds for linux-64 and python 3.12 (api.anaconda.org, 2026-10-02).
if [ "${WITH_SAGE:-0}" = "1" ]; then
  log "SageMath $SAGE_VERSION via conda"
  if ! as_agent test -x "$AGENT_HOME/miniforge/bin/conda"; then
    as_agent bash -c 'curl -fsSL https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh -o /tmp/miniforge.sh && bash /tmp/miniforge.sh -b -p ~/miniforge && rm /tmp/miniforge.sh'
  fi
  # 10.9's `sage --version` prints just "10.9" (older ones "SageMath version 10.x, …"; #474)
  if ! as_agent bash -c "~/miniforge/envs/sage/bin/sage --version 2>/dev/null | grep -qE '(^| )$SAGE_VERSION(,|\$)'"; then
    as_agent bash -c "~/miniforge/bin/conda create -y -q -n sage -c conda-forge 'sage=$SAGE_VERSION' 'python=$SAGE_PYTHON'"
  fi
  # ~/.local/bin/sage is files/bin/sage, which puts the environment's bin first on PATH as
  # `conda activate` does: conda's launcher doesn't, and Sage's giac(), r() and gap() then find
  # no program or a system copy (issue #474, review on #480). It replaces the old symlink.
  as_agent rm -f "$AGENT_HOME/.local/bin/sage"
  as_agent install -m 0755 "$FILES/bin/sage" "$AGENT_HOME/.local/bin/sage"
fi

# --- 10. Lean 4 and Mathlib (PM's AMI v2 list, item 2) ----------------------------------------
# elan v4.2.4 (release tarball, sha256 below), Lean v4.34.1 (the latest stable, commit 5045d005)
# and a Lake project ~/lean/workspace on Mathlib's tag v4.34.1 (d13f23b7). `lake new … math`
# writes `rev = "v4.34.1"` and fetches the cache itself; `cache get` is retried for mirror
# failures; `lake build Mathlib` then writes the few trace files the cache lacks, without which
# comparator's read-only sandboxed build fails with "permission denied" (rehearsed on
# the development machine: 127 s from scratch, .lake 7.7 GB, ~/.cache/mathlib 449 MB, kept for offline
# `cache get` in new projects at the same rev).
ELAN_VERSION=v4.2.4
ELAN_SHA256=42b94d4244e8353142c456ec0e4ca6528fd898a6c604d4059f494e706e431f63
LEAN_TOOLCHAIN=leanprover/lean4:v4.34.1
LEAN_COMMIT=5045d0056413266e57c625dcd7c365b10e377c52
MATHLIB_TAG=v4.34.1
as_lean() { as_agent env PATH="$AGENT_HOME/.elan/bin:$AGENT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }
log "Lean $LEAN_TOOLCHAIN, Mathlib $MATHLIB_TAG"
if ! as_agent test -x "$AGENT_HOME/.elan/bin/lean"; then
  tmp=$(mktemp -d)
  chmod 0755 "$tmp"
  fetch_checked "$ELAN_SHA256" "$tmp/elan.tar.gz" \
    "https://github.com/leanprover/elan/releases/download/$ELAN_VERSION/elan-x86_64-unknown-linux-gnu.tar.gz"
  tar -xzf "$tmp/elan.tar.gz" -C "$tmp"
  as_agent "$tmp/elan-init" -y --no-modify-path --default-toolchain "$LEAN_TOOLCHAIN"
  rm -rf "$tmp"
fi
# elan was installed with --no-modify-path: the participant's shells get ~/.elan/bin the way its
# installer would add it, in ~/.profile (login shells, `ssh`, `bash -lc`) and ~/.bashrc
# (interactive shells; Ubuntu's returns early for non-interactive ones). The bridge's services
# get it from push_config's service_path_prepend (codex review on #79).
for rc in .profile .bashrc; do
  as_agent bash -c 'grep -q "\.elan/bin" ~/"$1" 2>/dev/null \
    || printf "\n# Lean (elan)\nexport PATH=\"\$HOME/.elan/bin:\$PATH\"\n" >> ~/"$1"' _ "$rc"
done
# elan downloads the toolchain without a checksum: check the commit the binary reports instead.
as_lean lean --version | grep -q "commit $LEAN_COMMIT" \
  || { echo "lean is not $LEAN_TOOLCHAIN ($LEAN_COMMIT)" >&2; exit 1; }
# `lake new` runs `cache get` itself; if the mirror fails there, the project exists and the retry
# loop below takes over.
as_lean bash -c 'mkdir -p ~/lean && cd ~/lean && { [ -d workspace ] || lake "+$1" new workspace math \
  || [ -f workspace/lakefile.toml ]; }' _ "$LEAN_TOOLCHAIN"
as_lean bash -c 'cd ~/lean/workspace && grep -q "rev = \"$1\"" lakefile.toml' _ "$MATHLIB_TAG" \
  || { echo "~/lean/workspace does not pin Mathlib $MATHLIB_TAG" >&2; exit 1; }
as_lean bash -c 'cd ~/lean/workspace && for i in 1 2 3 4 5; do lake exe cache get >/dev/null && exit 0; echo "cache get failed ($i), retrying" >&2; sleep 20; done; exit 1'
as_lean bash -c 'cd ~/lean/workspace && lake build >/dev/null && lake build Mathlib >/dev/null'
# One shared, read-only Mathlib (issue #205): the workspace's packages are renamed to
# /opt/lean/packages/v4.34.1, still agent's (git refuses another user's repositories, and Lake
# runs git in each) but with every write bit off, and linked back; /opt/lean/template/v4.34.1
# is written. Every Lean project links to them through lean-project: megabytes and seconds per
# topic instead of 8 GB and minutes. `push_config.py --lean-shared` does the same on kept VMs.
sudo install -m 0755 "$FILES/bin/lean-project" /usr/local/bin/lean-project
sudo /usr/bin/python3 /usr/local/bin/lean-project --install "$AGENT_HOME/lean/workspace"

# --- 11. comparator, landrun, lean4export, nanoda (PM's AMI v2 list, item 4) -----------------
# Root-owned binaries in /usr/local/lib/comparator, used through /usr/local/bin/comparator-run
# (files/bin). Pins: comparator d03acab (its last commit on Lean v4.34.x; master has moved to
# v4.35.0-rc3), built with lean-toolchain set to the workspace's v4.34.1, and lean4export from
# its manifest (076e8e5, tag v4.34.0, which builds under v4.34.1): lean4export must match the
# project's Lean to the patch release, or it fails with "incompatible header". landrun main
# 811cfff (0.1.18, what comparator's README names), built with Go 1.27.1; nanoda 3a24072, built
# with Rust 1.92.0 (25 s, 1.3 MB). Go, Rust and the sources are build-only and removed. landrun
# targets Landlock ABI 9 (Linux 7.1); comparator already passes `--best-effort`, so no wrapper
# is needed on older kernels. comparator's own suite runs with the real landrun below.
GO_VERSION=1.27.1
GO_SHA256=63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445
LANDRUN_COMMIT=811cfff51ceaf3d9843708aa6d22e9b84ccac8b4
COMPARATOR_COMMIT=d03acab154d269c06e60e4de7e4cc85deebff94b
RUSTUP_SHA256=dda7234360b7f578ca8b0ddcb80145646fa61a67c1720a5abc7051b35c9fcb71  # rustup-init 1.29.1
RUST_VERSION=1.92.0
NANODA_COMMIT=3a2407216ee84a75f9e1aead6803d0578be06ae7
LIB=/usr/local/lib/comparator
log "comparator $COMPARATOR_COMMIT"
# $LIB/COMMIT records the comparator commit built (its version in tools.json), written only
# after the suite below passed; another pin rebuilds (review on #480).
if ! [ -x "$LIB/comparator" ] || ! [ -x "$LIB/landrun" ] || ! [ -x "$LIB/lean4export" ] || ! [ -x "$LIB/nanoda_bin" ] \
  || [ "$(cat "$LIB/COMMIT" 2>/dev/null)" != "$COMPARATOR_COMMIT" ]; then
  sudo install -d -m 0755 "$LIB"
  work=$(mktemp -d)
  fetch_checked "$GO_SHA256" "$work/go.tar.gz" "https://go.dev/dl/go$GO_VERSION.linux-amd64.tar.gz"
  tar -xzf "$work/go.tar.gz" -C "$work"
  git clone -q https://github.com/Zouuup/landrun "$work/landrun"
  git -C "$work/landrun" checkout -q "$LANDRUN_COMMIT"
  ( cd "$work/landrun" && GOTOOLCHAIN=local GOFLAGS=-mod=readonly GOPATH="$work/gopath" \
      GOCACHE="$work/gocache" "$work/go/bin/go" build -trimpath -o "$work/landrun.bin" ./cmd/landrun )
  sudo install -m 0755 "$work/landrun.bin" "$LIB/landrun"
  fetch_checked "$RUSTUP_SHA256" "$work/rustup-init" \
    "https://static.rust-lang.org/rustup/archive/1.29.1/x86_64-unknown-linux-gnu/rustup-init"
  chmod +x "$work/rustup-init"
  RUSTUP_HOME="$work/rustup" CARGO_HOME="$work/cargo" "$work/rustup-init" -y -q --no-modify-path \
    --profile minimal --default-toolchain "$RUST_VERSION" >/dev/null
  git clone -q https://github.com/ammkrn/nanoda_lib "$work/nanoda"
  git -C "$work/nanoda" checkout -q "$NANODA_COMMIT"
  ( cd "$work/nanoda" && RUSTUP_HOME="$work/rustup" CARGO_HOME="$work/cargo" \
      "$work/cargo/bin/cargo" build --release --locked -q )
  sudo install -m 0755 "$work/nanoda/target/release/nanoda_bin" "$LIB/nanoda_bin"
  chmod -R u+w "$work"  # Go's module cache is read-only
  rm -rf "$work"
  as_lean bash -c 'set -e; src=~/lean/.comparator-src; rm -rf "$src"
    git clone -q https://github.com/leanprover/comparator "$src"; git -C "$src" checkout -q "$1"
    echo "$2" > "$src/lean-toolchain"; cd "$src" && lake build lean4export comparator >/dev/null' \
    _ "$COMPARATOR_COMMIT" "$LEAN_TOOLCHAIN"
  sudo install -m 0755 "$AGENT_HOME/lean/.comparator-src/.lake/build/bin/comparator" \
    "$AGENT_HOME/lean/.comparator-src/.lake/packages/lean4export/.lake/build/bin/lean4export" "$LIB/"
  # comparator's own suite with the real landrun, lean4export and nanoda on this kernel (the
  # fleet's): 20/20 on the development machine (Linux 7.0, Landlock ABI 8) in 38 s.
  out=$(as_lean env PATH="$LIB:$AGENT_HOME/.elan/bin:/usr/bin:/bin" \
    bash -c 'cd ~/lean/.comparator-src && lean --run runtests.lean' 2>&1) || true
  printf '%s\n' "$out" | tail -n 5
  grep -q "All tests passed" <<<"$out" || { echo "comparator's test suite failed" >&2; exit 1; }
  as_agent rm -rf "$AGENT_HOME/lean/.comparator-src"
  echo "$COMPARATOR_COMMIT" | sudo tee "$LIB/COMMIT" >/dev/null
fi
sudo install -m 0755 "$FILES/bin/comparator-run" /usr/local/bin/comparator-run
echo "kernel $(uname -r), Landlock ABI $(python3 -c 'import ctypes; print(ctypes.CDLL(None).syscall(444, None, 0, 1))')"

# --- 11b. Julia and Oscar (AMI v3, Drew Sun 20:20 on #36) -----------------------------------
# Julia under /opt/julia-<version>, linked from /usr/local/bin (on every PATH, the services'
# too). Oscar in agent's default environment, ~/.julia/environments/v<major.minor>, which plain
# `julia` loads from any directory: Project.toml and Manifest.toml are committed (files/julia/,
# Oscar pinned), so every package version is exact. Precompiled here, so that the first
# `using Oscar` on a VM loads from the cache in seconds instead of compiling for many minutes.
# The package images are compiled for the builder's CPU, the same Zen 4 as the fleet (m7a).
# HomotopyContinuation.jl 2.22.4 is the environment's second package (issue #474): added with
# Pkg's PRESERVE_ALL, so the 157 packages Oscar's pin resolved stay exactly as they were (104
# added); Oscar's own HomotopyContinuationExt is compiled with them.
log "Julia $JULIA_VERSION, Oscar $OSCAR_VERSION, HomotopyContinuation"
JULIA_DIR=/opt/julia-$JULIA_VERSION
if [ ! -x "$JULIA_DIR/bin/julia" ]; then
  tmp=$(mktemp -d)
  fetch_checked "$JULIA_SHA256" "$tmp/julia.tar.gz" \
    "https://julialang-s3.julialang.org/bin/linux/x64/${JULIA_VERSION%.*}/julia-$JULIA_VERSION-linux-x86_64.tar.gz"
  sudo tar -xzf "$tmp/julia.tar.gz" -C /opt
  rm -rf "$tmp"
fi
sudo ln -sfn "$JULIA_DIR/bin/julia" /usr/local/bin/julia
JULIA_ENV="$AGENT_HOME/.julia/environments/v${JULIA_VERSION%.*}"
as_agent install -d "$JULIA_ENV"
as_agent install -m 0644 "$FILES/julia/Project.toml" "$FILES/julia/Manifest.toml" "$JULIA_ENV"/
start=$SECONDS
as_agent /usr/local/bin/julia -e 'using Pkg; Pkg.instantiate(); Pkg.precompile()'
as_agent /usr/local/bin/julia -e 'using Oscar' </dev/null
as_agent /usr/local/bin/julia -e 'using Oscar, HomotopyContinuation' </dev/null
echo "Julia/Oscar step: $((SECONDS - start)) s, depot $(sudo du -sh "$AGENT_HOME/.julia" | cut -f1)"

# --- 11c. certbot for IP address certificates (refserve, rc6; Drew Sun 20:20, item 5) -------
# In its own venv, from hash-pinned requirements, so that push_config never installs it on 40 VMs.
log "certbot $CERTBOT_VERSION"
if ! /opt/certbot/bin/certbot --version 2>/dev/null | grep -q "$CERTBOT_VERSION"; then
  sudo "$AGENT_HOME/.local/bin/uv" venv -q --python 3.12 /opt/certbot
  sudo env VIRTUAL_ENV=/opt/certbot "$AGENT_HOME/.local/bin/uv" pip install -q --require-hashes \
    -r "$FILES/certbot-requirements.txt"
fi

# --- 12. instructions for both agents (PM's AMI v2 list, item 5) -----------------------------
# Claude reads ~/.claude/CLAUDE.md under `claude -p` (zab passes no --bare or --setting-sources;
# the default account leaves CLAUDE_CONFIG_DIR unset), Codex reads $CODEX_HOME/AGENTS.md
# (codex-rs codex-home/src/instructions at rust-v0.157.1). One text for both (files/).
log "agent instructions"
sudo install -d -o "$AGENT" -g "$AGENT" -m 0755 "$AGENT_HOME/.claude"
sudo install -o "$AGENT" -g "$AGENT" -m 0644 "$FILES/agent-instructions.md" "$AGENT_HOME/.claude/CLAUDE.md"
sudo install -o "$AGENT" -g "$AGENT" -m 0644 "$FILES/agent-instructions.md" "$AGENT_HOME/.codex/AGENTS.md"

# --- 13. smoke test (workshop/ami/README.md) ---------------------------------------------------
log "smoke test"
fail=0
check() { if "$@" >/dev/null 2>&1; then echo "ok   $*"; else echo "FAIL $*"; fail=1; fi; }
warn() { if "$@" >/dev/null 2>&1; then echo "ok   $*"; else echo "WARN $*"; fi; }  # best effort
# Without -f, so that a gprc (Ubuntu's /etc/gprc, a stray ~/.gprc) shows: a 60x60 identity
# matrix must come back whole, unwrapped in colour codes, without the `[+++]` cut-off marker.
GP_PLAIN='out=$(echo "matid(60)" | gp -q 2>/dev/null); n=$(printf "%s\n" "$out" | wc -l)
[ "$n" -ge 60 ] && ! printf "%s" "$out" | grep -q "\[+++\]" &&
! printf "%s" "$out" | grep -q "$(printf "\033")"'
check bash -c "test \"\$(gp --version-short)\" = '$PARI_VERSION'"
check bash -c 'test "$(command -v gp)" = /usr/local/bin/gp'            # 2.17.4 first on PATH
check as_agent bash -lc 'test "$(command -v gp)" = /usr/local/bin/gp'   # for agent's login shell too
check bash -c 'test "$(gap -q <<<"Print(LoadPackage(\"alnuth\"), \"\\n\");" 2>/dev/null)" = true'  # needs /usr/bin/gp
check bash -c 'test "$(gp -q -f <<<"print(2+2)" 2>/dev/null)" = 4'
check bash -c "$GP_PLAIN"                                                  # no gprc in the way
check as_agent bash -lc "$GP_PLAIN"
check bash -c 'test "$(gp -q -f <<<"print(ellinit(\"11a1\").a6)" 2>/dev/null)" = -20'                      # elldata
check bash -c 'test "$(gp -q -f <<<"print(type(polgalois(x^8-2)))" 2>/dev/null)" = t_VEC'                 # galdata
check bash -c 'test "$(gp -q -f <<<"print(type(ellsea(ellinit([1,0,0,-1,0],nextprime(10^30)))))" 2>/dev/null)" = t_INT' # seadata (SEA)
check bash -c 'test "$(gp -q -f <<<"print(type(galoisgetpol(10,1)))" 2>/dev/null)" = t_VEC'               # galpol
check bash -c 'test "$(gp -q -f <<<"print(type(nflist(\"A5\",[1,2000])))" 2>/dev/null)" = t_VEC'          # nflistdata
check bash -c 'test "$(gap -q <<<"Print(Size(SmallGroup(8,3)), \"\\n\");" 2>/dev/null)" = 8'
check as_agent bash -c "$AGENT_HOME/.local/bin/codex --version | grep -q '$CODEX_VERSION'"
check as_agent bash -c "$AGENT_HOME/.local/bin/claude --version | grep -q '$CLAUDE_VERSION'"
check as_agent "$AGENT_HOME/.local/bin/zab" --version
check as_agent "$AGENT_HOME/venv/bin/python" -c "import cypari2"
check sudo -u "$AGENT" sudo -n true
check as_agent grep -q '^check_for_update_on_startup = false' "$codexrc"
check as_agent grep -q '^\[mcp_servers\.lmfdb\]' "$codexrc"
check as_agent bash -c "$AGENT_HOME/.local/bin/claude doctor </dev/null 2>&1 | grep -q 'Auto-updates: disabled'"
check as_agent bash -c "$AGENT_HOME/.local/bin/claude mcp get lmfdb 2>/dev/null | grep -q '$LMFDB_MCP_URL'"
if [ -n "$MAGMA_TARBALL" ]; then
  if [ -n "$MAGMA_LICENCE_SETUP" ]; then check systemctl is-active magma-licence.service; fi
  # A computation that comes back is what shows the licence accepted, however the host meets it.
  if as_agent bash -c 'test "$(echo "print 2+2;" | timeout 120 magma -b 2>/dev/null | tr -d "[:space:]")" = 4'; then
    echo "ok   magma -b runs print 2+2"
  else
    echo "FAIL magma -b does not print 4 for print 2+2: is its licence accepted on this host? (workshop/ami/README.md)"
    fail=1
  fi
fi
if [ "${WITH_SAGE:-0}" = "1" ]; then
  check as_agent bash -c "test \"\$($AGENT_HOME/.local/bin/sage -c 'print(EllipticCurve(\"11a1\").rank())' 2>/dev/null)\" = 0"
fi
# AMI v2 (PM's list on #36, item 7). Codex: the package layout and the code-mode host.
check as_agent test -x "$CODEX_DIR/bin/codex-code-mode-host"
check as_agent "$CODEX_DIR/bin/codex-code-mode-host" --help
check as_agent bash -c "test \"\$(readlink -f $AGENT_HOME/.local/bin/codex)\" = '$CODEX_DIR/bin/codex'"
check as_agent grep -q '^\[mcp_servers\.lean-lsp\]' "$codexrc"
warn as_agent bash -c 'timeout 60 codex plugin list </dev/null 2>/dev/null | grep -q "lean4@lean4-skills"'
# Claude: lean-lsp connects, the lean4 plugin is installed and enabled.
check as_agent bash -c "$AGENT_HOME/.local/bin/claude mcp get lean-lsp 2>/dev/null | grep -q 'Status: .*Connected'"
check as_agent bash -c "$AGENT_HOME/.local/bin/claude plugin list --json </dev/null 2>/dev/null | jq -e '.[] | select(.id == \"lean4@lean4-skills\" and .enabled)'"
check as_agent test -s "$AGENT_HOME/.claude/CLAUDE.md"
check as_agent test -s "$AGENT_HOME/.codex/AGENTS.md"
# The service PATH push_config renders (item 6): python3 from the venv, lean and lake from elan.
SVC_PATH="$AGENT_HOME/venv/bin:$AGENT_HOME/.elan/bin:$AGENT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
# The participant's own shells: a fresh login shell and an interactive one find Lean too.
check as_agent bash -lc 'command -v lean && command -v lake && command -v comparator-run'
check as_agent bash -ic 'command -v lean && command -v lake' </dev/null
check as_agent env -i HOME="$AGENT_HOME" PATH="$SVC_PATH" bash -c "test \"\$(command -v python3)\" = $AGENT_HOME/venv/bin/python3 && for c in lean lake latexmk comparator-run; do command -v \$c || exit 1; done"
# LaTeX: amsart, tikz-cd, xy, biblatex with biber, cleveref, \mathbb, \mathfrak, \mathscr.
TEX_SMOKE='d=$(mktemp -d) && cp /usr/local/share/workshop/smoke/smoke.tex /usr/local/share/workshop/smoke/smoke.bib "$d" && cd "$d" &&
timeout 300 latexmk -pdf -interaction=nonstopmode -halt-on-error smoke.tex && test -s smoke.pdf &&
grep -q Serre smoke.bbl && ! grep -q "There were undefined references" smoke.log && rm -rf "$d"'
check as_agent bash -c "$TEX_SMOKE"
# Lean: Mathlib imports from the cache (seconds; a rebuild would take hours), one theorem, #eval.
# Lean's exit status counts, not only the #eval line (a theorem error still prints 1024).
check as_lean bash -c 'out=$(cd ~/lean/workspace && timeout 120 lake env lean /usr/local/share/workshop/smoke/Smoke.lean) && grep -qx 1024 <<<"$out"'
# comparator through the wrapper: a sorry'd challenge and its proof, accepted (exit 0).
CMP_SMOKE='d=$(mktemp -d) && cp -r /usr/local/share/workshop/smoke/comparator/. "$d" && cd "$d" &&
timeout 300 comparator-run config.json && cd / && rm -rf "$d"'
check as_lean bash -c "$CMP_SMOKE"
# The shared Mathlib (issue #205): the workspace links it, and a new project's Mathlib file and
# a Mathlib comparator challenge go through it.
check as_agent test -L "$AGENT_HOME/lean/workspace/.lake/packages"
check as_lean lean-project --check --comparator --max-seconds 120
# AMI v3: Julia on the services' PATH, Oscar precompiled (no "Precompiling" on load), Hecke works:
# the class number of Q(sqrt(-5)) is 2. certbot with IP address support.
check bash -c "test \"\$(julia --version)\" = 'julia version $JULIA_VERSION'"
check as_agent env -i HOME="$AGENT_HOME" PATH="$SVC_PATH" bash -c 'test "$(command -v julia)" = /usr/local/bin/julia'
# Oscar's progress output on stderr ends in a carriage return, which `2>&1` puts in front of
# the result ("\r2", first v3 build): strip it before comparing.
OSCAR_SMOKE='out=$(timeout 600 julia -e "using Oscar; Qx, x = polynomial_ring(QQ, :x); K, a = number_field(x^2 + 5); println(order(class_group(K)[1]))" 2>&1) && out=$(tr -d "\r" <<<"$out") && test "$(tail -n1 <<<"$out")" = 2 && ! grep -q Precompiling <<<"$out"'
check as_agent bash -c "$OSCAR_SMOKE"
# Issue #474: HomotopyContinuation and Oscar's extension for it load from the cache as well.
HC_SMOKE='out=$(timeout 900 julia -e "using Oscar, HomotopyContinuation; println(Base.get_extension(Oscar, :HomotopyContinuationExt) !== nothing)" 2>&1) && out=$(tr -d "\r" <<<"$out") && test "$(tail -n1 <<<"$out")" = true && ! grep -q Precompiling <<<"$out"'
check as_agent bash -c "$HC_SMOKE"
check bash -c "/opt/certbot/bin/certbot --version 2>&1 | grep -q '$CERTBOT_VERSION' && /opt/certbot/bin/certbot --help all | grep -q -- --ip-address"
check bash -c 'jq -e ".disableClaudeAiConnectors == true and .env.DISABLE_AUTOUPDATER == \"1\"" /etc/claude-code/managed-settings.json'
# The tools manifest (issue #474), last: every tool of files/tools.toml, its version and a small
# computation, run as agent with the services' PATH (as the agents run them), and only if all
# pass /etc/zab/tools.json with the versions seen. `!tools` and the agents' instructions read it.
have=""
if [ -n "$MAGMA_TARBALL" ]; then have=magma; fi
if [ "${WITH_SAGE:-0}" = "1" ]; then have="$have${have:+,}sage"; fi
start=$SECONDS
if tools_json=$(as_agent env -i HOME="$AGENT_HOME" USER="$AGENT" LOGNAME="$AGENT" SHELL=/bin/bash \
    LANG=C.UTF-8 PATH="$SVC_PATH" /usr/bin/python3 "$FILES/bin/tools_manifest.py" build \
    --source "$FILES/tools.toml" --image "$IMAGE_NAME" --have "$have"); then
  sudo install -d -m 0755 /etc/zab
  printf '%s\n' "$tools_json" | sudo tee /etc/zab/tools.json >/dev/null
  sudo chmod 0644 /etc/zab/tools.json
  echo "ok   /etc/zab/tools.json: $(jq '.tools | length' /etc/zab/tools.json) tools ($((SECONDS - start)) s)"
else
  echo "FAIL the tools manifest (the FAIL lines above)"
  fail=1
fi
if [ "$fail" -ne 0 ]; then echo "smoke test FAILED" >&2; exit 1; fi
# --- 14. housekeeping, last: nothing below may run claude or write the machine id again ------
# Instance metadata: launch.py sets HttpTokens=required (IMDSv2, ../aws/README.md); nothing
# here depends on IMDS.
sudo rm -f /home/ubuntu/.bash_history "$AGENT_HOME/.bash_history"
# Image hygiene: package caches, the journal, build leftovers (PM note on #56).
$APT clean
as_agent "$AGENT_HOME/.local/bin/uv" cache clean -q 2>/dev/null || true
if as_agent test -x "$AGENT_HOME/miniforge/bin/conda"; then
  as_agent "$AGENT_HOME/miniforge/bin/conda" clean -a -y -q >/dev/null 2>&1 || true
fi
sudo rm -rf /tmp/pari-src.* /tmp/zab-wheel.* "$AGENT_HOME/.cache/pip"
sudo journalctl --rotate --vacuum-time=1s >/dev/null 2>&1 || true
# The smoke test ran claude as agent, which writes fresh machine/user ids into ~/.claude.json:
# drop them again (PM review on #56), so that no two VMs share them.
if as_agent test -f "$AGENT_HOME/.claude.json"; then
  as_agent bash -c 'jq "del(.machineID, .userID)" ~/.claude.json > ~/.claude.json.tmp && mv ~/.claude.json.tmp ~/.claude.json'
fi
# So that each instance runs its first-boot user data and gets its own /etc/machine-id. SSH host
# keys are regenerated per instance by cloud-init (ssh_deletekeys); build_ami.py --smoke checks.
sudo cloud-init clean --logs --machine-id

echo
echo "AMI build complete. Now create the image (workshop/ami/README.md or build_ami.py)."
