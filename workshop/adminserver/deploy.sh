#!/usr/bin/env bash
# Install or upgrade the zab admin server from a release tag that a deployer signed
# (docs/ADMIN.md §1.6, §1.7; workshop/adminserver/README.md). Run it on your laptop, from the tag
# you deploy (to review, from the last tag you signed: never a copy you haven't reviewed):
#
#   deploy.sh --review COMMIT --etc DIR [--since TAG]          the diff to read before signing
#   deploy.sh --host HOST --etc DIR --bootstrap --secrets DIR vX.Y.Z        the first install
#   deploy.sh --host HOST --etc DIR [--since TAG] [--downgrade] vX.Y.Z      an upgrade
#   deploy.sh --host HOST --etc DIR --wheel vX.Y.Z      a fleet wheel for !rollout, built here
#
# --etc DIR holds your copies of the server's admin.toml, allowed_signers, backup_recipients and
# break-glass/*.pub; --secrets DIR, its secrets. --repo DIR is the clone with the tags (default:
# the one this script is in), --out DIR where the reviewed diff and its review are written
# (default: .).
#
# Both halves make the four checks of §1.6: the tag is annotated and signed by a key in
# allowed_signers, its own name is the one requested, the name matches admin.toml's `release`
# pattern, and it is not older than the running release unless --downgrade says so. The server
# half (`--server`) is this script as the tag holds it, run by root through sudo; it installs
# the release at its final path, built by `zab-build`, and switches `current` atomically.
# ZAB_DEPLOY_SSH, ZAB_DEPLOY_PREFIX and ZAB_DEPLOY_SYSTEMCTL exist for the offline tests (README).
set -euo pipefail
export LC_ALL=C
umask 077

SERVER_DIR=workshop/adminserver # its files are what root runs and installs
LIST=$SERVER_DIR/release-paths
PRIV_LIST=$SERVER_DIR/privileged-paths # what Drew reads himself (D31, D32): --review shows it first
EMPTY_TREE=4b825dc642cb6eb9a060e54bf8d69288fbee4904
VERSION_RE='^v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})(rc(0|[1-9][0-9]{0,5}))?$'
FINAL_RE='^v[0-9]+\.[0-9]+\.[0-9]+$'        # zab.admin.config.RELEASE_PATTERNS["final"]
RC_RE='^v[0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)?$' # zab.admin.config.RELEASE_PATTERNS["rc"]
PATH_RE='^[A-Za-z0-9_][A-Za-z0-9._+-]*(/[A-Za-z0-9_][A-Za-z0-9._+-]*)*$'
FILE_RE='^[A-Za-z0-9_.][A-Za-z0-9._@+-]*(/[A-Za-z0-9_.][A-Za-z0-9._@+-]*)*$' # each file of a release path
HEX_RE='^[0-9a-f]{40}([0-9a-f]{24})?$'
SHA_RE='^[0-9a-f]{64}$'
HOST_RE='^[A-Za-z0-9_][A-Za-z0-9._@-]*$'
# uv on the server, the version VERIFY V9 was checked with: its release tarballs' SHA-256.
UV_VERSION=0.11.24
UV_SHA256_aarch64=e22c66d36a0098b17cff80a8647e0b8c58202af899d4e9eb820fc7ad126435a1
UV_SHA256_x86_64=5ce1ad074a78f96c5c8122088bb85a12eb282195bc1453151a48762e4fc31fed
PACKAGES=(python3 git caddy openssh-client age nftables curl)
# The units this release runs: every deploy enables and restarts them, in this order. The
# sockets first: the starter's service, socket-activated, is stopped, so that the next request
# starts the new release's (`restart`); the archive check's (#374) starts an instance per
# connection, which ends with it, so the next one runs the new release with nothing to stop.
# Then the config helper's path unit (ADMIN §2.9), whose oneshot runs only on a request; the bot,
# which hosts the runner (PR 10); the console's collector and the console (ADMIN §4, #489); and
# the waker after the bot (#348).
SERVICES=(zab-admin-starter.socket zab-archive-check.socket zab-admin-config.path zab-admin-bot.service
  zab-admin-collector.service zab-console.service zab-waker.service)
PER_CONNECTION=" zab-archive-check.socket " # Accept=yes: no service of its own to stop

die() {
  printf 'deploy.sh: %s\n' "$*" >&2
  exit 1
}
note() { printf '%s\n' "$*" >&2; }
# stops_service UNIT: UNIT is a socket whose one service is stopped at a restart (SERVICES).
stops_service() { [[ $1 == *.socket && $PER_CONNECTION != *" $1 "* ]]; }
# git with neither the user's nor the system's configuration, no replace refs, and pathspecs
# taken literally.
g() {
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1 GIT_LITERAL_PATHSPECS=1 \
    git -C "$REPO" "$@"
}
sha256() {
  if command -v sha256sum >/dev/null; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi
}
# What a terminal would hide shows as ^X, <U+XXXX> or <XX>: C0 and C1 controls, format characters
# (bidi overrides among them), every default-ignorable code point (zero widths, Hangul fillers,
# variation selectors, tag characters), private use and surrogates, line and paragraph
# separators, any space but the ASCII one and tab, and bytes that are not UTF-8. So `ok`, U+3164,
# ` = True` (U+3164 is a valid Python identifier character) shows as `ok<U+3164> = True`.
sanitize() {
  if command -v perl >/dev/null; then
    perl -ne 'BEGIN { binmode STDOUT, ":utf8" }
      s/([\x80-\xff])/sprintf("<%02X>", ord $1)/ge unless utf8::decode($_);
      s/([\x00-\x08\x0b-\x1f\x7f])/sprintf("^%c", ord($1) ^ 64)/ge;
      s/([\x{80}-\x{9f}]|\p{Cf}|\p{Co}|\p{Cs}|\p{Default_Ignorable_Code_Point}|\p{Zl}|\p{Zp}|[\x{115F}\x{1160}\x{3164}\x{FFA0}\x{FE00}-\x{FE0F}\x{E0100}-\x{E01EF}]|[^\S\t\n ])/sprintf("<U+%04X>", ord $1)/ge;
      print'
  else
    cat -v
  fi
}

# --- the checks (both halves) ----------------------------------------------------------------

# admin.toml's top-level `release`: "final" unless it says "rc" (read up to the first table).
release_kind() {
  local value
  value=$(awk '/^[ \t]*\[/ { exit }
    /^[ \t]*release[ \t]*=/ { n++; sub(/^[ \t]*release[ \t]*=[ \t]*/, ""); sub(/[ \t]*(#.*)?$/, ""); v = $0 }
    END { if (n > 1) print "(twice)"; else if (n) print v; else print "\"final\"" }' "$1") ||
    die "$1: unreadable"
  case $value in
    '"final"' | "'final'") echo final ;;
    '"rc"' | "'rc'") echo rc ;;
    *) die "$1: release must be \"final\" or \"rc\", not $value" ;;
  esac
}

# older A B: true when release A comes before B (vX.Y.ZrcN before vX.Y.Z).
older() {
  local a b i
  read -r -a a <<<"$(vkey "$1")"
  read -r -a b <<<"$(vkey "$2")"
  for i in 0 1 2 3 4; do
    if ((a[i] != b[i])); then ((a[i] < b[i])) && return 0 || return 1; fi
  done
  return 1
}
vkey() {
  [[ $1 =~ $VERSION_RE ]] || die "$1: not a release name"
  if [ -n "${BASH_REMATCH[4]}" ]; then
    echo "${BASH_REMATCH[1]} ${BASH_REMATCH[2]} ${BASH_REMATCH[3]} 0 ${BASH_REMATCH[5]}"
  else
    echo "${BASH_REMATCH[1]} ${BASH_REMATCH[2]} ${BASH_REMATCH[3]} 1 0"
  fi
}

# verify_tag NAME SIGNERS [KIND]: refs/tags/NAME is an annotated tag of a commit, named NAME in
# the tag object itself, signed with an SSH key of SIGNERS, and (with KIND) allowed by admin.toml's
# release pattern. Sets COMMIT. Everything after the lookup goes by object ID, never by name.
verify_tag() {
  local v=$1 signers=$2 kind=${3:-} re obj text header out
  [[ $v =~ $VERSION_RE ]] || die "$v: not a release name (vX.Y.Z or vX.Y.ZrcN); a branch is never deployed"
  if [ -n "$kind" ]; then
    re=$FINAL_RE
    [ "$kind" = final ] || re=$RC_RE
    [[ $v =~ $re ]] || die "$v: a release candidate, and admin.toml has release = \"$kind\""
  fi
  [ -s "$signers" ] || die "$signers: no allowed signers"
  [[ $signers == /* ]] || die "$signers: not an absolute path" # git -C would resolve it elsewhere
  obj=$(g rev-parse --verify --quiet "refs/tags/$v") || die "$v: no such tag (a branch is never deployed)"
  [ "$(g cat-file -t "$obj")" = tag ] || die "$v: a lightweight tag, which has no signature"
  text=$(g cat-file tag "$obj")
  header=${text%%$'\n\n'*}
  [ "$(sed -n 's/^tag //p' <<<"$header")" = "$v" ] ||
    die "refs/tags/$v holds a tag object named otherwise: a renamed ref is refused"
  [ "$(sed -n 's/^type //p' <<<"$header")" = commit ] || die "$v: not a tag of a commit"
  COMMIT=$(sed -n 's/^object //p' <<<"$header")
  [[ $COMMIT =~ $HEX_RE ]] || die "$v: no commit"
  [[ $text == *$'\n-----BEGIN SSH SIGNATURE-----\n'* ]] || die "$v: not signed with an SSH key"
  if ! out=$(g -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$signers" -c gpg.ssh.program=ssh-keygen \
    -c gpg.program=false -c gpg.x509.program=false verify-tag "$obj" 2>&1) ||
    [[ $out != *'Good "git" signature for '* ]]; then
    die "$v: not signed by a key in $signers: $out"
  fi
  text=$(g show "$COMMIT:pyproject.toml") || die "$v: no pyproject.toml"
  [[ $'\n'$text$'\n' == *$'\nversion = "'"${v#v}"$'"\n'* ]] || die "$v: pyproject.toml has another version"
}

# list_paths COMMIT FILE: FILE's paths, as that commit holds it, into LISTED: plain relative
# paths of the commit, one per line; blank lines and # comments are skipped.
list_paths() {
  local text line
  text=$(g show "$1:$2" 2>/dev/null) || die "$1 has no $2"
  LISTED=()
  while IFS= read -r line; do
    case $line in '' | '#'*) continue ;; esac
    [[ $line =~ $PATH_RE ]] || die "$2: not a plain relative path: $line"
    g cat-file -e "$1:$line" 2>/dev/null || die "$2: $line is not in the release"
    LISTED+=("$line")
  done <<EOF
$text
EOF
}

# covers PATH LISTED…: PATH is one of LISTED or lies under one.
covers() {
  local p=$1 q
  shift
  for q; do case $p in "$q" | "$q"/*) return 0 ;; esac; done
  return 1
}

# release_paths COMMIT: the paths of LIST, as that commit holds it, into PATHS. They must cover
# all of workshop/adminserver: deploy.sh, which root runs, the units and rules root installs, and
# LIST itself are always in the reviewed diff. Every file name under them matches FILE_RE, so
# nothing a terminal would hide is in a name (git quotes any other), and none is code Python
# loads in place of a source file or at start-up: no __pycache__/, *.pyc, *.so or *.pth (#436).
release_paths() {
  local need names name
  list_paths "$1" "$LIST"
  PATHS=(${LISTED[@]+"${LISTED[@]}"})
  covers "$SERVER_DIR" ${PATHS[@]+"${PATHS[@]}"} ||
    die "$LIST must list workshop/adminserver (or workshop), deploy.sh included"
  for need in pyproject.toml uv.lock; do
    case " ${PATHS[*]} " in *" $need "*) ;; *) die "$LIST does not list $need" ;; esac
  done
  names=$(g ls-tree -r --name-only --full-tree "$1" -- "${PATHS[@]}") || die "$1: no tree"
  while IFS= read -r name; do
    [[ $name =~ $FILE_RE ]] || die "$name: a file name of other characters than [A-Za-z0-9._@+-]"
    case /$name/ in
      */./* | */../* | */__pycache__/* | *.pyc/ | *.so/ | *.pth/) die "$name: not allowed in a release path" ;;
    esac
  done <<<"$names"
}

# priv_list COMMIT: PRIV_LIST's paths at COMMIT into LISTED, or workshop/adminserver alone for a
# commit without one (before #432), or nothing for none.
priv_list() {
  LISTED=()
  [ -n "$1" ] && [ "$1" != "$EMPTY_TREE" ] || return 0
  LISTED=("$SERVER_DIR")
  ! g cat-file -e "$1:$PRIV_LIST" 2>/dev/null || list_paths "$1" "$PRIV_LIST"
}

# privileged_paths BASE COMMIT: COMMIT's privileged paths into PRIV, BASE's into PRIV_BASE, and
# both into PRIV_ALL, which marks a file P (a path that leaves the list is still read once).
# COMMIT's are release paths or under one (release_paths COMMIT ran first), workshop/adminserver
# among them, so every file there is privileged; a commit without PRIV_LIST after a base with one
# is refused.
privileged_paths() {
  local p
  priv_list "$1"
  PRIV_BASE=(${LISTED[@]+"${LISTED[@]}"})
  if [ -n "$1" ] && g cat-file -e "$1:$PRIV_LIST" 2>/dev/null && ! g cat-file -e "$2:$PRIV_LIST" 2>/dev/null; then
    die "$PRIV_LIST: the base has one, $2 not"
  fi
  priv_list "$2"
  PRIV=(${LISTED[@]+"${LISTED[@]}"})
  covers "$SERVER_DIR" ${PRIV[@]+"${PRIV[@]}"} || die "$PRIV_LIST must list $SERVER_DIR"
  for p in "${PRIV[@]}"; do covers "$p" "${PATHS[@]}" || die "$PRIV_LIST: $p is not a release path"; done
  PRIV_ALL=("${PRIV[@]}" ${PRIV_BASE[@]+"${PRIV_BASE[@]}"})
}

# no_hidden_code: no file of the release paths at COMMIT outside PRIV is binary (#436): what the
# review lists without showing could be code (a pyc, an ELF), so it is refused instead.
no_hidden_code() {
  local id path
  g ls-tree -r --full-tree "$COMMIT" -- "${PATHS[@]}" >"$TMP/files" || die "$COMMIT: no tree"
  while read -r _ _ id path; do # MODE TYPE ID PATH
    covers "$path" "${PRIV[@]}" || ! binary "$id" || die "$path: a binary file outside $PRIV_LIST"
  done <"$TMP/files"
}

# What the server installs, by blob: `git ls-tree` of the release paths at COMMIT.
tree_of() { g ls-tree -r --full-tree "$COMMIT" -- "${PATHS[@]}"; }

# Plumbing, text always (-a), no textconv or external diff: no .gitattributes can hide a change.
DIFF=(diff-tree -r -p -a --no-color --no-ext-diff --no-textconv --no-renames --full-index
  --diff-algorithm=myers --indent-heuristic)

# reviewed_diff BASE COMMIT: the release paths' changes since BASE, and whole every path that
# LIST did not hold at BASE, itself or under one it held (all of them when BASE is empty). Its
# SHA-256 is what the deploy records (§1.6). Leaves COMMIT's paths in PATHS, and splits them into
# COMMON, diffed against BASE, and ADDED, against the empty tree.
reviewed_diff() {
  local base=$1 commit=$2 p
  local -a old=()
  COMMON=() ADDED=()
  if [ -n "$base" ] && g cat-file -e "$base:$LIST" 2>/dev/null; then
    release_paths "$base"
    old=("${PATHS[@]}")
  fi
  release_paths "$commit"
  for p in "${PATHS[@]}"; do
    if covers "$p" ${old[@]+"${old[@]}"}; then COMMON+=("$p"); else ADDED+=("$p"); fi
  done
  [ ${#COMMON[@]} -eq 0 ] || g "${DIFF[@]}" "$base" "$commit" -- "${COMMON[@]}"
  [ ${#ADDED[@]} -eq 0 ] || g "${DIFF[@]}" "$EMPTY_TREE" "$commit" -- "${ADDED[@]}"
}

# changed FROM PATH…: append each file of `diff-tree FROM COMMIT -- PATH…` to the arrays F_FROM,
# F_STATUS (A, M, D or T), F_BLOB (the new blob, the old one for a deletion), F_ADD and F_DEL
# (its lines added and removed, text always) and F_PATH, in the diff's order.
changed() {
  local from=$1 meta path old new status n=${#F_PATH[@]} rec
  shift
  [ $# -gt 0 ] || return 0
  if ! g diff-tree -r -z --raw --no-renames --full-index "$from" "$COMMIT" -- "$@" >"$TMP/raw" ||
    ! g diff-tree -r -z --numstat -a --no-renames "$from" "$COMMIT" -- "$@" >"$TMP/numstat"; then
    die "git diff-tree failed"
  fi
  while IFS= read -r -d '' meta && IFS= read -r -d '' path; do
    read -r _ _ old new status <<<"$meta" # :MODE MODE OLD NEW STATUS
    [ "$status" != D ] || new=$old
    F_FROM+=("$from") F_STATUS+=("$status") F_BLOB+=("$new") F_PATH+=("$path")
  done <"$TMP/raw"
  while IFS= read -r -d '' rec; do
    [ "${rec#*$'\t'*$'\t'}" = "${F_PATH[n]:-}" ] || die "git diff-tree: --raw and --numstat differ"
    F_ADD+=("${rec%%$'\t'*}") rec=${rec#*$'\t'} n=$((n + 1))
    F_DEL+=("${rec%%$'\t'*}")
  done <"$TMP/numstat"
  [ "$n" = ${#F_PATH[@]} ] || die "git diff-tree: --raw and --numstat differ"
}

# binary BLOB: a NUL byte in its first 8000 bytes, git's own test, whatever .gitattributes say.
binary() {
  local n
  n=$(g cat-file blob "$1" | head -c 8000 | tr -dc '\000' | wc -c) || true
  [ "$((n))" -gt 0 ]
}

# uv.lock's packages, for a reader: each changed one, its versions and source, with what a reader
# would check flagged (display only; python3 3.11 or newer, for tomllib). Every name, version,
# source and field from the lock is printed as JSON, so that none can make a line of its own.
LOCK_PY='import json, sys, tomllib
def load(path):  # the lock-level fields, and each name with every entry of it (a lock may hold
    text, found = open(path, "rb").read().decode(), {}  # several versions of one)
    doc = tomllib.loads(text) if text else {}
    for k in doc.get("package", []):
        found.setdefault(k["name"], []).append(k)
    return {key: v for key, v in doc.items() if key != "package"}, found
def source(k):
    s = k.get("source", {})
    return s.get("registry") or ("editable " + s["editable"] if "editable" in s else str(s))
def files(k):
    return sorted((f.get("url", ""), f.get("hash", "")) for f in [k.get("sdist"), *k.get("wheels", [])] if f)
def variants(ks):
    return {(k.get("version", "?"), source(k)): files(k) for k in ks}
def flat(value, at=""):  # {"a": {"b": x}} -> {"a.b": x}
    if not isinstance(value, dict):
        return {at: value}
    return {key: v for n, sub in value.items() for key, v in flat(sub, f"{at}.{n}" if at else n).items()}
def meta(k):  # what else an entry holds: dependencies, markers, groups, metadata
    return flat({key: v for key, v in k.items() if key not in ("name", "version", "source", "sdist", "wheels")})
def fields(a, b, at):  # each field changed, a list by its items
    for key in sorted(a.keys() | b.keys()):
        x, y = a.get(key), b.get(key)
        if x == y:
            continue
        if isinstance(x, list) or isinstance(y, list):
            xs, ys = [json.dumps(i, sort_keys=True) for i in x or []], [json.dumps(i, sort_keys=True) for i in y or []]
            for sign, items, others in (("-", xs, ys), ("+", ys, xs)):
                for item in items:
                    if item not in others:
                        print(f"{at}{json.dumps(key)}: {sign}{item}")
        else:
            print(f"{at}{json.dumps(key)}: {json.dumps(x)} -> {json.dumps(y)}")
(top_old, old), (top_new, new) = load(sys.argv[1]), load(sys.argv[2])
if top_old:
    fields(flat(top_old), flat(top_new), "  (the lock) ")
for name in sorted(old.keys() | new.keys()):
    a, b = variants(old.get(name, [])), variants(new.get(name, []))
    if old.get(name) == new.get(name):
        continue
    ks = new.get(name) or old[name]
    va = ", ".join(json.dumps(v) for v, _ in sorted(a)) or "(new)"
    vb = ", ".join(json.dumps(v) for v, _ in sorted(b)) or "(removed)"
    flags = [why for why, bad in (
        ("SAME VERSION, OTHER FILES", any(a.get(key, f) != f for key, f in b.items())),
        ("NOT FROM PyPI", any(source(k) not in ("https://pypi.org/simple", "editable .") for k in ks)),
        ("A FILE NOT ON files.pythonhosted.org", any(
            not u.startswith("https://files.pythonhosted.org/") for k in ks for u, _ in files(k))),
    ) if bad]
    sources = ", ".join(json.dumps(s) for s in sorted({source(k) for k in ks}))
    print(f"  {json.dumps(name)} {va} -> {vb}  {sources}" + "".join(f"  ** {why}" for why in flags))
    was = {(k.get("version", "?"), source(k)): meta(k) for k in old.get(name, [])}
    now = {(k.get("version", "?"), source(k)): meta(k) for k in new.get(name, [])}
    if not (was and now):
        continue  # a package new or gone: its line above; its dependencies are packages too
    if len(was) == len(now) == 1:  # one entry each side, whatever its version (the project)
        pairs = [(next(iter(was)), next(iter(now)))]
    else:  # each variant with its own, and a variant new or gone against nothing
        pairs = [(key, key) for key in sorted(was.keys() & now.keys())]
        pairs += [(key, None) for key in sorted(was.keys() - now.keys())]
        pairs += [(None, key) for key in sorted(now.keys() - was.keys())]
    for x, y in pairs:
        fields(was.get(x, {}), now.get(y, {}), f"      {json.dumps((y or x)[0])} ")'

# build-constraints.txt's pins, for a reader: each changed one, its versions, with other hashes
# under an unchanged version flagged (display only).
PINS_PY='import re, sys
def load(path):  # each requirement, every line of one name kept, and every hash given it, on
    pins, entry = {}, None  # its own line or the ones after
    for line in open(path, encoding="utf-8"):
        rest = re.sub(r"--hash=\S+|\\\s*$", "", re.sub(r"(^|\s)#.*$", "", line)).strip()
        if rest:  # a requirement; one other than NAME==VERSION is shown as written
            pin = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", rest)
            entry = (pin.group(2) if pin else "(as written)", [])
            pins.setdefault(pin.group(1).lower() if pin else rest, []).append(entry)
        if entry is not None:
            entry[1].extend(re.findall(r"--hash=(\S+)", line))
    return {key: sorted((v, sorted(h)) for v, h in entries) for key, entries in pins.items()}
old, new = load(sys.argv[1]), load(sys.argv[2])
for name in sorted(old.keys() | new.keys()):
    a, b = old.get(name, []), new.get(name, [])
    if a == b:
        continue
    va, vb = ", ".join(v for v, _ in a) or "(new)", ", ".join(v for v, _ in b) or "(removed)"
    flag = "  ** SAME VERSION, OTHER HASHES" if a and b and va == vb else ""
    print(f"  {name} {va} -> {vb}{flag}")'

# as_data FILE FROM: uv.lock or build-constraints.txt as D32 has it read, FROM..COMMIT: by
# package or by pin, never its hashes, which the tests check and the SHA-256 covers. Fails
# closed: without that summary (no python3 with tomllib, a file it can't read, its error on
# stderr), the file's raw diff is shown in its place, and counted in SHOWN.
as_data() {
  local py=$LOCK_PY what="each package changed (name, versions, source)" n
  [ "$1" = uv.lock ] || py=$PINS_PY what="each pin changed (name, versions)"
  [ "$2" != "$EMPTY_TREE" ] || what=${what/each/every} what=${what/ changed/}
  g show "$2:$1" >"$TMP/data.old" 2>/dev/null || : >"$TMP/data.old"
  g show "$COMMIT:$1" >"$TMP/data.new" 2>/dev/null || : >"$TMP/data.new"
  if python3 -I -c "$py" "$TMP/data.old" "$TMP/data.new" >"$TMP/data.out"; then
    printf '\n%s, %s; not its hashes, which the tests check:\n' "$1" "$what"
    cat -- "$TMP/data.out"
    [ "$2" = "$EMPTY_TREE" ] || printf '  (its raw diff is in the .diff file, which the SHA-256 covers)\n'
  else
    printf '\n%s: no summary (python3 failed; its error went to stderr): its raw diff instead.\n' "$1"
    g "${DIFF[@]}" "$2" "$COMMIT" -- "$1" >"$TMP/data.diff"
    cat -- "$TMP/data.diff"
    n=$(grep -a -c '^diff --git ' "$TMP/data.diff") || true
    SHOWN=$((SHOWN + n))
  fi
}

# review BASE BASE_NAME NAME: what the reviewed diff shows, as paged (§1.6, D31): one line per
# file (P if privileged, its status, its lines added and removed, text or binary, its path); then
# the privileged files' diff, then whole each file privileged at COMMIT but not at BASE (display
# only), then the rest's diff. uv.lock and build-constraints.txt, when privileged, show as their
# packages and pins (as_data, D32), never their hashes. Each file's diff is the reviewed diff's;
# a deleted binary outside the privileged paths is listed, not shown. After reviewed_diff and
# privileged_paths. Names the privileged files changed in CHANGED_PRIV and the newly privileged
# ones in NEWLY; counts in SECTIONS the reviewed diff's per-file sections it lists (two for a
# type change), in SHOWN those it shows.
review() {
  local base=${1:-$EMPTY_TREE} i flag kind a d s add=0 del=0 padd=0 pdel=0 names name
  local -a pb=() pe=() rb=() re=() bins=() newly=() data=() data_from=()
  local -A whole=()
  F_FROM=() F_STATUS=() F_BLOB=() F_ADD=() F_DEL=() F_PATH=() CHANGED_PRIV=() NEWLY=() SECTIONS=0 SHOWN=0
  changed "$base" ${COMMON[@]+"${COMMON[@]}"}
  changed "$EMPTY_TREE" ${ADDED[@]+"${ADDED[@]}"}
  printf 'zab admin release review: %s..%s (%s)\n' "$2" "$3" "$COMMIT"
  printf 'P: privileged (%s): read its diff yourself (DECISIONS D31, D32).\n' "$PRIV_LIST"
  printf 'A: shown whole (new since %s, or to %s), M: changed, D: removed, T: type changed.\n\n' \
    "$2" "$LIST"
  for i in "${!F_PATH[@]}"; do
    flag=' ' kind=text a=+${F_ADD[i]} d=-${F_DEL[i]} s=1
    [ "$a" != +- ] || a=- d=- F_ADD[i]=0 F_DEL[i]=0 # numstat's counts of a binary file
    [ "${F_STATUS[i]}" != T ] || s=2                 # git shows a deletion and an addition
    SECTIONS=$((SECTIONS + s)) SHOWN=$((SHOWN + s))
    ! binary "${F_BLOB[i]}" || kind=binary
    [ "${F_STATUS[i]}" != A ] || whole[${F_PATH[i]}]=1
    if covers "${F_PATH[i]}" "${PRIV_ALL[@]}"; then
      flag=P padd=$((padd + F_ADD[i])) pdel=$((pdel + F_DEL[i]))
      CHANGED_PRIV+=("${F_STATUS[i]} ${F_PATH[i]}")
      if [[ ${F_PATH[i]} == uv.lock || ${F_PATH[i]} == build-constraints.txt ]]; then
        data+=("${F_PATH[i]}") data_from+=("${F_FROM[i]}") SHOWN=$((SHOWN - s))
      elif [ "${F_FROM[i]}" = "$base" ]; then pb+=("${F_PATH[i]}"); else pe+=("${F_PATH[i]}"); fi
    elif [ $kind = binary ]; then
      bins+=("${F_PATH[i]} (blob ${F_BLOB[i]})") SHOWN=$((SHOWN - s))
    elif [ "${F_FROM[i]}" = "$base" ]; then rb+=("${F_PATH[i]}"); else re+=("${F_PATH[i]}"); fi
    add=$((add + F_ADD[i])) del=$((del + F_DEL[i]))
    printf '%s %s %7s %7s  %-6s  %s\n' "$flag" "${F_STATUS[i]}" "$a" "$d" $kind "${F_PATH[i]}"
  done
  printf '\n%d files, +%d -%d; %d privileged, +%d -%d.\n' ${#F_PATH[@]} $add $del ${#CHANGED_PRIV[@]} $padd $pdel
  printf '\n==== The privileged files (%d): read every line ====\n' ${#CHANGED_PRIV[@]}
  [ ${#pb[@]} -eq 0 ] || g "${DIFF[@]}" "$base" "$COMMIT" -- "${pb[@]}"
  [ ${#pe[@]} -eq 0 ] || g "${DIFF[@]}" "$EMPTY_TREE" "$COMMIT" -- "${pe[@]}"
  for i in "${!data[@]}"; do as_data "${data[i]}" "${data_from[i]}"; done
  names=$(g ls-tree -r --name-only --full-tree "$COMMIT" -- "${PRIV[@]}") || die "$COMMIT: no tree"
  while IFS= read -r name; do
    [ -z "$name" ] || [ -n "${whole[$name]:-}" ] || covers "$name" ${PRIV_BASE[@]+"${PRIV_BASE[@]}"} ||
      NEWLY+=("$name")
  done <<<"$names"
  printf '\n==== Newly privileged since %s (%d): read them whole once ====\n' "$2" ${#NEWLY[@]}
  # Whole, but what is read as data (D32): uv.lock by package, build-constraints.txt by pin.
  for name in ${NEWLY[@]+"${NEWLY[@]}"}; do
    case $name in uv.lock | build-constraints.txt) ;; *) newly+=("$name") ;; esac
  done
  [ ${#newly[@]} -eq 0 ] || g "${DIFF[@]}" "$EMPTY_TREE" "$COMMIT" -- "${newly[@]}"
  SHOWN=$((SHOWN + ${#newly[@]}))
  for name in build-constraints.txt uv.lock; do
    [[ " ${NEWLY[*]} " != *" $name "* ]] || as_data "$name" "$EMPTY_TREE"
  done
  printf '\n==== The rest (%d files) ====\n' $((${#F_PATH[@]} - ${#CHANGED_PRIV[@]}))
  [ ${#rb[@]} -eq 0 ] || g "${DIFF[@]}" "$base" "$COMMIT" -- "${rb[@]}"
  [ ${#re[@]} -eq 0 ] || g "${DIFF[@]}" "$EMPTY_TREE" "$COMMIT" -- "${re[@]}"
  for i in ${bins[@]+"${bins[@]}"}; do printf 'Binary, not shown: %s\n' "$i"; done
}

# --- the laptop half -------------------------------------------------------------------------

remote() { "$SSH" -- "$HOST" "$1"; }

# REMOTE_TMP is set only once it has matched remote_tmp_ok: no other value ever reaches `rm`.
laptop_cleanup() {
  if [ -n "${REMOTE_TMP:-}" ] && remote_tmp_ok "$REMOTE_TMP"; then remote "rm -rf -- $REMOTE_TMP" || true; fi
  rm -rf -- "${TMP:-/nonexistent}"
}
# The server's temporary directory: exactly what `mktemp -d /tmp/zab-deploy.XXXXXXXXXX` makes.
remote_tmp_ok() {
  local name=${1#"$RP/tmp/zab-deploy."}
  [[ $1 == "$RP/tmp/zab-deploy.$name" && $name =~ ^[A-Za-z0-9]{10}$ ]]
}

# present_diff BASE_NAME BASE_COMMIT NAME: write and hash the reviewed diff, then show the review
# made from it, which is written beside it as shown. The review shows every file of the diff.
present_diff() {
  local file=$OUT/zab-admin-${1:-empty}..$3.diff f n m
  reviewed_diff "$2" "$COMMIT" >"$file"
  DIFF_SHA=$(sha256 <"$file")
  MANIFEST=$(tree_of | sha256)
  privileged_paths "$2" "$COMMIT"
  no_hidden_code
  review "$2" "${1:-the empty tree}" "$3" >"$TMP/review"
  n=$(grep -a -c '^diff --git ' "$file") || true
  m=$(grep -a -c '^diff --git ' "$TMP/review") || true
  [[ $((n)) == "$SECTIONS" && $((m)) == "$SHOWN" ]] ||
    die "the review lists $SECTIONS of the diff's $((n)) file sections, and shows $((m)) of $SHOWN"
  sanitize <"$TMP/review" >"${file%.diff}.review"
  if [ -t 1 ] && command -v less >/dev/null; then
    # No input preprocessor (a lesspipe in LESSOPEN), shell escape or editor: secure mode.
    env -u LESSOPEN -u LESSCLOSE LESSSECURE=1 LESS='' less -- "${file%.diff}.review" ||
      true # quitting the pager early is not a refusal
  else
    cat -- "${file%.diff}.review"
  fi
  note "The reviewed diff, ${1:-the empty tree}..$3, is $file"
  note "  SHA-256 $DIFF_SHA; release tree $MANIFEST (${#PATHS[@]} paths of $LIST)"
  note "  The review, as shown, is ${file%.diff}.review: ${#F_PATH[@]} files, ${#CHANGED_PRIV[@]} privileged"
  for f in ${CHANGED_PRIV[@]+"${CHANGED_PRIV[@]}"}; do note "    P $f"; done
  [ ${#NEWLY[@]} -eq 0 ] || note "  Newly privileged, shown whole once: ${NEWLY[*]}"
}

confirm() {
  local answer
  printf 'Type %s to %s: ' "$V" "$1" >&2
  read -r answer || die "not confirmed"
  [ "$answer" = "$V" ] || die "not confirmed"
}

# run_server PAYLOAD ARG…: copy PAYLOAD over SSH, then run its deploy.sh as root.
run_server() {
  local payload=$1 tty='' t
  shift
  t=$(remote "mktemp -d $RP/tmp/zab-deploy.XXXXXXXXXX") || die "$HOST: no temporary directory"
  remote_tmp_ok "$t" || die "$HOST: unexpected answer to mktemp (a login banner?): $t"
  REMOTE_TMP=$t
  (cd "$payload" && COPYFILE_DISABLE=1 tar -cf - .) | remote "tar --warning=no-unknown-keyword -xf - -C $REMOTE_TMP" ||
    die "$HOST: could not copy the release"
  [ ! -t 0 ] || tty=-t
  "$SSH" ${tty:+"$tty"} -- "$HOST" "sudo bash $REMOTE_TMP/deploy.sh --server $REMOTE_TMP $*"
}

# build_config: COMMIT's pyproject.toml makes the build run hatchling and nothing of the tree's
# (#436): hatchling.build and no backend-path, no [tool.hatch] but the wheel's packages, nothing
# dynamic, so no build hook, metadata hook or version source, no [tool.uv] (its sources would
# replace the pinned backend; uv build also gets --no-sources); and the commit has no hatch.toml.
build_config() {
  local out
  ! g cat-file -e "$COMMIT:hatch.toml" 2>/dev/null || die "$V has a hatch.toml, which hatchling would obey"
  out=$(g show "$COMMIT:pyproject.toml" | python3 -I -c 'import sys, tomllib
d = tomllib.loads(sys.stdin.read())
b, p, h = d.get("build-system", {}), d.get("project", {}), d.get("tool", {}).get("hatch")
bad = [f"build-system.{k}" for k in b if k not in ("requires", "build-backend")]
bad += [] if b.get("build-backend") == "hatchling.build" else ["build-system.build-backend"]
bad += ["project.dynamic"] if "dynamic" in p else []
bad += [] if h in (None, {"build": {"targets": {"wheel": {"packages": ["src/zab"]}}}}) else ["tool.hatch"]
bad += ["tool.uv"] if "uv" in d.get("tool", {}) else []  # [tool.uv.sources] would steer the build
print(" ".join(bad))' 2>&1) || die "--wheel checks pyproject.toml with python3 3.11 or newer: $out"
  [ -z "$out" ] || die "$V: pyproject.toml configures the build beyond hatchling's own: $out"
}

build_wheel() {
  local src=$TMP/src dist=$TMP/dist var f
  local -a pass=() tree=(pyproject.toml build-constraints.txt src/zab)
  mkdir "$src" "$dist" "$TMP/tmp"
  case $(uv --version 2>/dev/null) in
    "uv $UV_VERSION "*) ;;
    *) note "Note: VERIFY V9 checked uv $UV_VERSION; this is $(uv --version 2>/dev/null || echo 'no uv')." ;;
  esac
  g cat-file -e "$COMMIT:build-constraints.txt" 2>/dev/null || die "$V has no build-constraints.txt"
  build_config
  # Only what the wheel holds and its metadata names, so nothing else of the tree can steer it.
  for f in README.md LICENSE .gitignore; do ! g cat-file -e "$COMMIT:$f" 2>/dev/null || tree+=("$f"); done
  g archive --format=tar "$COMMIT" -- "${tree[@]}" | tar -xf - -C "$src"
  # The tree's .gitattributes apply to `git archive` here (export-subst could write a hook into
  # pyproject.toml): what was unpacked must be the blobs checked above, as on the server.
  check_tree "wheel build's" "$src" "${tree[@]}"
  for var in UV_CACHE_DIR UV_DEFAULT_INDEX UV_OFFLINE; do
    [ -z "${!var:-}" ] || pass+=("$var=${!var}")
  done
  (cd "$src" && env -i PATH="$PATH" HOME="$HOME" TMPDIR="$TMP/tmp" UV_NO_CONFIG=1 ${pass[@]+"${pass[@]}"} \
    uv build -q --wheel --no-sources --build-constraints build-constraints.txt --require-hashes -o "$dist") ||
    die "$V: the wheel did not build with the pinned backend"
  WHEEL=zulip_agent_bridge-${V#v}-py3-none-any.whl
  set -- "$dist"/*.whl
  [[ $# -eq 1 && $1 == "$dist/$WHEEL" ]] || die "expected $WHEEL, got: ${*##*/}"
  cp "$dist/$WHEEL" "$TMP/payload/$WHEEL"
}

laptop_main() {
  local review='' since='' etc='' secrets='' bootstrap=0 downgrade=0 wheel=0 running base=''
  local base_commit='' f
  OUT=. HOST='' REPO='' V='' TMP='' REMOTE_TMP=''
  SSH=${ZAB_DEPLOY_SSH:-ssh}
  RP=${ZAB_DEPLOY_PREFIX:-} # tests only: the server's paths under a temporary root
  while [ $# -gt 0 ]; do
    case $1 in
      --review | --since | --etc | --secrets | --host | --repo | --out) [ $# -ge 2 ] || die "$1 needs a value" ;;
    esac
    case $1 in
      --review) review=$2 && shift ;;
      --since) since=$2 && shift ;;
      --etc) etc=$2 && shift ;;
      --secrets) secrets=$2 && shift ;;
      --host) HOST=$2 && shift ;;
      --repo) REPO=$2 && shift ;;
      --out) OUT=$2 && shift ;;
      --bootstrap) bootstrap=1 ;;
      --downgrade) downgrade=1 ;;
      --wheel) wheel=1 ;;
      -*) die "unknown option $1" ;;
      *) [ -z "$V" ] || die "one release at a time" && V=$1 ;;
    esac
    shift
  done
  [ -n "$REPO" ] || REPO=$(cd "$(dirname "$0")" && git rev-parse --show-toplevel) || die "--repo DIR"
  REPO=$(cd "$REPO" && pwd) || die "--repo $REPO: no such directory" # check_tree runs git from elsewhere
  [[ -n $etc && -f $etc/admin.toml && -f $etc/allowed_signers ]] ||
    die "--etc DIR, with your copies of the server's admin.toml and allowed_signers"
  [ -d "$OUT" ] || die "--out $OUT: no such directory"
  # Absolute from here on: git runs with -C REPO, and resolves a relative path from there.
  etc=$(cd "$etc" && pwd) OUT=$(cd "$OUT" && pwd)
  [ -z "$secrets" ] || [ ! -d "$secrets" ] || secrets=$(cd "$secrets" && pwd)
  TMP=$(mktemp -d)
  trap laptop_cleanup EXIT
  if [ -n "$review" ]; then
    [ -z "$V$HOST" ] || die "--review takes a commit, not a release or a host"
    base_commit=
    if [ -n "$since" ]; then
      verify_tag "$since" "$etc/allowed_signers"
      base_commit=$COMMIT
    fi
    COMMIT=$(g rev-parse --verify --quiet "$review^{commit}") || die "$review: no such commit"
    present_diff "$since" "$base_commit" "${COMMIT:0:12}"
    # The commands to paste, in any directory (#468): the tag is the version of the commit's
    # pyproject.toml, as a deploy requires (verify_tag), made in the clone read here.
    local tag repo
    tag=$(g show "$COMMIT:pyproject.toml" 2>/dev/null | sed -n 's/^version = "\(.*\)"$/v\1/p') || true
    printf -v repo %q "$REPO"
    if [[ ! $tag =~ $VERSION_RE ]]; then
      note "No tag command: $COMMIT's pyproject.toml has no single version = \"X.Y.Z\" line"
    elif g rev-parse --verify --quiet "refs/tags/$tag" >/dev/null; then
      note "No tag command: $REPO already has a tag $tag"
    else
      note "Sign it once reviewed, then verify and push it:"
      note "  git -C $repo tag -s $tag -m \"zab $tag\" $COMMIT"
      note "  git -C $repo tag -v $tag"
      note "  git -C $repo push origin $tag"
    fi
    return
  fi
  [ -n "$V" ] || die "name the release: deploy.sh --host HOST --etc DIR vX.Y.Z"
  [[ $HOST =~ $HOST_RE ]] || die "--host HOST"
  local kind commit
  kind=$(release_kind "$etc/admin.toml")
  verify_tag "$V" "$etc/allowed_signers" "$kind"
  commit=$COMMIT
  release_paths "$commit"
  g show "$commit:workshop/adminserver/deploy.sh" | cmp -s - "$0" ||
    note "Note: this deploy.sh is not $V's. The server runs $V's; run $V's here too."
  mkdir "$TMP/payload"
  g show "$commit:workshop/adminserver/deploy.sh" >"$TMP/payload/deploy.sh"
  g bundle create -q "$TMP/payload/release.bundle" "refs/tags/$V"
  if [ "$wheel" = 1 ]; then
    [[ $bootstrap$downgrade == 00 && -z $since ]] || die "--wheel goes alone"
    build_wheel
    MANIFEST=$(tree_of | sha256)
    DIFF_SHA=$(sha256 <"$TMP/payload/$WHEEL")
    note "Built $WHEEL with the pinned hatchling: SHA-256 $DIFF_SHA."
    confirm "place it on $HOST for !rollout"
    run_server "$TMP/payload" --version "$V" --commit "$commit" --manifest "$MANIFEST" \
      --wheel "$WHEEL" --wheel-sha256 "$DIFF_SHA"
    return
  fi
  running=$(remote "if [ -L $RP/opt/zab-admin/current ]; then readlink $RP/opt/zab-admin/current; else echo none; fi") ||
    die "$HOST: unreachable"
  if [ "$running" = none ]; then
    running=
    [ "$bootstrap" = 1 ] || die "$HOST runs no release: --bootstrap first"
    [[ -n $secrets && -d $secrets ]] || die "--bootstrap needs --secrets DIR"
    [ -f "$etc/backup_recipients" ] || die "$etc/backup_recipients: the deployers' backup keys (§1.7)"
  else
    [ "$bootstrap" = 0 ] || die "$HOST already runs $running: deploy without --bootstrap"
    [[ $running == "$RP/opt/zab-admin/releases/"* ]] || die "$HOST: current is $running"
    running=${running##*/}
    [[ $running =~ $VERSION_RE ]] || die "$HOST runs $running, not a release"
    [ "$V" != "$running" ] || die "$HOST already runs $V"
    ! older "$V" "$running" || [ "$downgrade" = 1 ] || die "$V is older than $running, which $HOST runs: --downgrade"
  fi
  base=${since:-$running}
  if [ -n "$base" ]; then
    verify_tag "$base" "$etc/allowed_signers"
    base_commit=$COMMIT
  fi
  COMMIT=$commit
  present_diff "$base" "$base_commit" "$V"
  if [ -n "$running" ]; then
    for f in admin.toml allowed_signers backup_recipients; do
      remote "cat $RP/etc/zab-admin/$f" >"$TMP/server-$f" 2>/dev/null || : >"$TMP/server-$f"
      [ -f "$etc/$f" ] && cmp -s "$etc/$f" "$TMP/server-$f" && continue
      note "The server's $f differs from $etc/$f (- server, + yours):"
      diff -u "$TMP/server-$f" "$etc/$f" 2>/dev/null | sanitize >&2 || true
    done
  else
    mkdir -p "$TMP/payload/etc/break-glass"
    cp "$etc/admin.toml" "$etc/allowed_signers" "$etc/backup_recipients" "$TMP/payload/etc/"
    [ ! -d "$etc/break-glass" ] || cp -R "$etc/break-glass/." "$TMP/payload/etc/break-glass/"
    cp -R "$secrets" "$TMP/payload/secrets"
  fi
  confirm "deploy it to $HOST${running:+, which runs $running}"
  local -a flags=()
  [ "$bootstrap" = 0 ] || flags+=(--bootstrap)
  [ "$downgrade" = 0 ] || flags+=(--downgrade)
  run_server "$TMP/payload" --version "$V" --commit "$commit" --manifest "$MANIFEST" \
    --base "${base:--}" --diff-sha256 "$DIFF_SHA" ${flags[@]+"${flags[@]}"}
}

# --- the server half (root) ------------------------------------------------------------------

# Privileged system steps, skipped under ZAB_DEPLOY_PREFIX (the offline tests).
priv() { if [ -n "$PREFIX" ]; then note "(test) skipped: $*"; else "$@"; fi; }
own() { [ -n "$PREFIX" ] || chown "$@"; }
# as_user USER CMD…: CMD as USER, with a clean environment, from "/", in a session of its own
# (no controlling terminal), without the deploy lock's fd 9, and with no_new_privs. stdin is
# /dev/null unless piped.
as_user() {
  local user=$1
  shift
  if [ -n "$PREFIX" ]; then
    setsid -w setpriv --no-new-privs \
      env -i PATH="$PATH" HOME=/nonexistent LANG=C.UTF-8 ${PASS[@]+"${PASS[@]}"} "$@" 9>&-
  else
    # no_new_privs once it is USER: nothing it runs gains privileges through a setuid binary
    setsid -w runuser -u "$user" -- setpriv --no-new-privs \
      env -i PATH=/usr/bin:/bin HOME=/nonexistent LANG=C.UTF-8 "$@" 9>&-
  fi
}
# The release's own admin.toml loader, as zab-build: `python -m zab.admin.config WHAT`.
admin_config() { as_user zab-build "$REL/.venv/bin/python" -I -B -m zab.admin.config --config "$ETC/admin.toml" "$@"; }

# No zab-build process outlives a step: none is left to change a tree that root then checks,
# takes back or removes, or to hold a file open in it.
end_build_processes() {
  local i
  [ -z "$PREFIX" ] || return 0
  for i in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -u zab-build >/dev/null || return 0
    pkill -KILL -u zab-build || true
    sleep 0.2
  done
  die "zab-build processes survive SIGKILL"
}

server_cleanup() {
  if [ -n "${BUILDING:-}" ]; then
    end_build_processes
    rm -rf -- "$BUILDING"
  fi
  rm -rf -- "${WORK:-/nonexistent}"
}

# uv, pinned by version and by its tarball's SHA-256, reinstalled whenever either changes.
install_uv() {
  local arch sum t
  arch=$(uname -m)
  case $arch in
    aarch64) sum=$UV_SHA256_aarch64 ;;
    x86_64) sum=$UV_SHA256_x86_64 ;;
    *) die "no pinned uv for $arch" ;;
  esac
  if [ "$(cat "$OPT/uv/tarball.sha256" 2>/dev/null)" = "$sum" ]; then
    case $("$UV" --version 2>/dev/null) in "uv $UV_VERSION "*) return ;; esac
  fi
  t=$WORK/uv
  mkdir "$t"
  curl -fsSL --proto '=https' -o "$t/uv.tar.gz" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$arch-unknown-linux-gnu.tar.gz"
  [ "$(sha256 <"$t/uv.tar.gz")" = "$sum" ] || die "uv $UV_VERSION: wrong SHA-256"
  tar -xzf "$t/uv.tar.gz" -C "$t" --no-same-owner
  install -d -m 0755 "$OPT" "$OPT/uv"
  install -m 0755 "$t/uv-$arch-unknown-linux-gnu/uv" "$UV.new"
  mv -f -T -- "$UV.new" "$UV"
  printf '%s\n' "$sum" >"$OPT/uv/tarball.sha256"
  chmod 0644 "$OPT/uv/tarball.sha256"
}

# The payload, copied into root's own 0700 WORK before anything reads it, and refused if it holds
# anything but plain files and directories; every later step reads the copy. The secrets then
# leave the SSH user's staging area at once, removed as that user.
take_payload() {
  local staging=$PAYLOAD who=${SUDO_USER:-}
  mkdir "$WORK/payload"
  cp -RP -- "$staging/." "$WORK/payload/"
  [ -z "$(find "$WORK/payload" ! -type f ! -type d -print -quit)" ] ||
    die "the payload holds something other than files and directories"
  PAYLOAD=$WORK/payload
  [ -d "$staging/secrets" ] || return 0
  if [ -z "$PREFIX" ] && [[ $who =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
    runuser -u "$who" -- rm -rf -- "$staging/secrets" || note "Remove $staging/secrets by hand."
  else
    rm -rf -- "$staging/secrets"
  fi
}

# The deployer's copies of the non-secret files of /etc/zab-admin (bootstrap).
install_etc() {
  local f
  install -d -m 0755 "$ETC" "$ETC/break-glass"
  for f in admin.toml allowed_signers backup_recipients; do
    install -m 0644 "$PAYLOAD/etc/$f" "$ETC/$f"
  done
  for f in "$PAYLOAD"/etc/break-glass/*.pub; do
    [ ! -f "$f" ] || install -m 0644 "$f" "$ETC/break-glass/"
  done
}

# install_files SRC: the server's own files from a release's workshop/adminserver (README):
# users, directories, the IMDS rule and the nftables files that keep it through every flush
# (§1.2). Its units are `install_units`'. Nothing is enabled or restarted here.
install_files() {
  local src=$1 f type path mode rest
  # The users and the rule first: nftables.service's drop-in includes imds.nft, which names the
  # users, so a drop-in installed ahead of them would keep the host's own firewall from loading.
  install -D -m 0644 "$src/sysusers.conf" "$PREFIX/etc/sysusers.d/zab-admin.conf"
  priv systemd-sysusers /etc/sysusers.d/zab-admin.conf
  # zab-build may leave no crontab or at job behind to outlive end_build_processes. (With an
  # allow file present, the deny file is ignored and zab-build, not listed there, is refused.)
  for f in "$PREFIX/etc/cron.deny" "$PREFIX/etc/at.deny"; do
    [ -e "$f" ] || install -D -m 0644 /dev/null "$f"
    grep -qx zab-build "$f" && continue
    [ ! -s "$f" ] || [ -z "$(tail -c1 "$f")" ] || printf '\n' >>"$f" # end the last name's line
    printf 'zab-build\n' >>"$f"
  done
  install -D -m 0644 "$src/tmpfiles.conf" "$PREFIX/etc/tmpfiles.d/zab-admin.conf"
  priv systemd-tmpfiles --create /etc/tmpfiles.d/zab-admin.conf
  if [ -n "$PREFIX" ]; then
    while read -r type path mode rest; do
      [ "$type" != d ] || install -d -m "$mode" "$PREFIX$path"
    done <"$src/tmpfiles.conf"
  fi
  for f in imds.nft nftables-imds.nft nftables-stop-imds.nft; do
    install -D -m 0644 "$src/$f" "$ETC/$f"
  done
}

# install_units SRC: the units, sockets and drop-ins of a release's workshop/adminserver, then a
# daemon-reload. After install_files, whose users and rule nftables.service's drop-in names. On
# an upgrade only once `current` is the new release (#348): a deploy that fails before leaves
# the running release's units in place, and loaded.
install_units() {
  local src=$1 f d
  # Each failure returns: after the switch this runs under `||`, where set -e is off.
  for f in "$src"/*.service "$src"/*.socket "$src"/*.path; do
    install -D -m 0644 "$f" "$SYSTEMD/${f##*/}" || return 1
  done
  for d in "$src"/*.service.d; do
    for f in "$d"/*.conf; do install -D -m 0644 "$f" "$SYSTEMD/${d##*/}/${f##*/}" || return 1; done
  done
  priv systemctl daemon-reload
}

# units_failed SRC: the units of SRC, the release now current and recorded, failed to install.
# Like a unit that fails to start, that is an operational fault, and a rerun of $V is refused,
# so name the commands that finish the job: the same files, the reload, then what `restart`
# does (#350's review).
units_failed() {
  local src=$1 f d s
  local -a enable=("${SERVICES[@]}") again=(zab-admin-imds.service)
  [ -z "$CONSOLE_HOST" ] || enable=(caddy.service "${enable[@]}")
  again+=("${enable[@]}")
  note "$V is installed and $OPT/current points to it, but its units were not installed (above)."
  note "Fix the cause, then run on the server:"
  for f in "$src"/*.service "$src"/*.socket "$src"/*.path; do note "  sudo install -D -m 0644 $f $SYSTEMD/${f##*/}"; done
  for d in "$src"/*.service.d; do
    for f in "$d"/*.conf; do note "  sudo install -D -m 0644 $f $SYSTEMD/${d##*/}/${f##*/}"; done
  done
  note "  sudo systemctl daemon-reload"
  note "  sudo systemctl enable ${enable[*]}"
  note "  sudo systemctl restart ${again[*]}"
  for s in "${SERVICES[@]}"; do
    ! stops_service "$s" || note "  sudo systemctl stop ${s%.socket}.service"
  done
  exit 1
}

imds_active() {
  [ -z "$PREFIX" ] || return 0
  if ! systemctl is-active --quiet zab-admin-imds.service || ! nft list table inet zab_imds >/dev/null; then
    die "zab-admin-imds.service is not active: the console and Caddy could reach the role"
  fi
}

# build REL: the release at its final path, as zab-build: the tree from git, checked against the
# commit blob by blob before anything runs; third-party wheels from uv.lock with their hashes and
# no build; a .pth naming REL/src; the self-check. Then, with no zab-build process left, root
# takes the tree back and checks it again.
build() {
  local rel=$1 py=$1/.venv/bin/python site
  local -a env=(UV_NO_CONFIG=1 UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR="$rel/.uv-cache")
  end_build_processes
  if [ -e "$rel" ] || [ -L "$rel" ]; then
    note "Removing the earlier, unused install of $V."
    rm -rf -- "$rel"
  fi
  install -d -m 0755 "$OPT/releases"
  mkdir -m 0755 "$rel"
  BUILDING=$rel
  own zab-build:zab-build "$rel"
  g archive --format=tar "$COMMIT" -- "${PATHS[@]}" |
    as_user zab-build tar -xf - -C "$rel" --no-same-owner --no-same-permissions
  end_build_processes
  check_tree unpacked "$rel" "${PATHS[@]}"
  as_user zab-build env "${env[@]}" "$UV" export -q --frozen --only-group admin --no-emit-project \
    --no-header --format requirements.txt --project "$rel" -o "$rel/.requirements.txt"
  as_user zab-build env "${env[@]}" "$UV" venv -q --python "$PYTHON" "$rel/.venv"
  as_user zab-build env "${env[@]}" "$UV" pip install -q --python "$py" --require-hashes --no-deps \
    --no-build -r "$rel/.requirements.txt" || die "$V: the admin group's wheels did not install"
  site=$(as_user zab-build "$py" -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
  [[ $site == "$rel/.venv/"* ]] || die "unexpected site-packages $site"
  printf '%s\n' "$rel/src" | as_user zab-build tee "$site/zab-release.pth" >/dev/null
  as_user zab-build "$py" -I -B -c 'import sys, zab; sys.exit(zab.__file__ != sys.argv[1])' \
    "$rel/src/zab/__init__.py" || die "$V: the venv does not import the release's own src/"
  # The release's own self-check, always: admin.toml with the strict loader, and the imports the
  # units need (the starter's boto3 among them), from this tree and venv only.
  as_user zab-build "$py" -I -B -m zab.admin selfcheck --config "$ETC/admin.toml" ||
    die "$V: selfcheck failed (above), on $ETC/admin.toml"
  # The waker's (#348): every module of its closure, the lazy ones included, from this tree.
  as_user zab-build "$py" -I -B -m zab.waker selfcheck ||
    die "$V: the waker's modules did not import (above)"
  # The archive check's (#374), as its unit runs it: a small archive re-packed in memory.
  as_user zab-build "$py" -I -S -B "$rel/workshop/admin/archive_check.py" selfcheck ||
    die "$V: the archive check's selfcheck failed (above)"
  as_user zab-build rm -rf -- "$rel/.uv-cache"
  # Root acts on the tree only once no zab-build process can change it: the top first, then the
  # rest, without following links (-P, the default of -R).
  end_build_processes
  own root:root "$rel"
  own -R -P root:root "$rel"
  chmod -R u=rwX,go=rX "$rel"
  check_tree built "$rel" "${PATHS[@]}"
}

# check_tree WHICH DIR PATH…: the files under DIR, but the venv, are exactly the blobs and modes the
# commit gives PATH…, nothing more and nothing less, no link, no other kind of file. Run right
# after every unpacking, before anything there is installed or run, and again after the build.
# The repository's info/attributes keeps `git archive` from converting anything; this checks it.
check_tree() {
  local which=$1 dir=$2 want got f mode
  shift 2
  want=$(g ls-tree -r --full-tree "$COMMIT" -- "$@" | awk -F'\t' '{ split($1, m, " "); print m[1], m[3], $2 }' | sort)
  got=$(cd "$dir" && find . \( -path ./.venv -o -path ./.requirements.txt \) -prune -o ! -type d -print |
    sort | while IFS= read -r f; do
      f=${f#./}
      if [ -L "$f" ] || [ ! -f "$f" ]; then echo "not a file: $f"; continue; fi
      mode=100644
      [ ! -x "$f" ] || mode=100755
      echo "$mode $(g hash-object --no-filters "$dir/$f") $f"
    done | sort)
  [ "$want" = "$got" ] || die "the $which tree differs from $V's blobs: $(diff <(echo "$want") <(echo "$got") | head -5)"
}

# Point current at REL: a new symlink renamed over the old one, so there is always a current.
switch() {
  rm -f -- "$OPT/current.new"
  ln -s "$1" "$OPT/current.new"
  mv -T -- "$OPT/current.new" "$OPT/current"
  BUILDING=
}

# The server's files and the Caddyfile, with the console's host, validated before it replaces the
# old one: from the release being installed, REL.
install_system() {
  local src=$REL/workshop/adminserver
  install_files "$src"
  CONSOLE_HOST=$(admin_config console-host)
  if [ -n "$CONSOLE_HOST" ]; then
    [[ $CONSOLE_HOST =~ ^[a-z0-9.-]+(:[0-9]{1,5})?$ ]] || die "console host $CONSOLE_HOST"
    install -d -m 0755 "$CADDY"
    sed "s/<CONSOLE_HOST>/$CONSOLE_HOST/g" "$src/Caddyfile" >"$CADDY/Caddyfile.new"
    chmod 0644 "$CADDY/Caddyfile.new"
    priv caddy validate --config "$CADDY/Caddyfile.new" --adapter caddyfile
    mv -f -- "$CADDY/Caddyfile.new" "$CADDY/Caddyfile"
  else
    note "admin.toml has no console_url: Caddy is left as it is."
  fi
}

# The secrets named by admin.toml, and the two every server has, from --secrets (bootstrap).
install_secrets() {
  local path rel f paths
  local -a want=(provisioner.zuliprc github-token)
  paths=$(admin_config zuliprcs)
  for path in $paths; do
    [[ $path == /etc/zab-admin/secrets/* && ${path#/etc/zab-admin/secrets/} =~ $PATH_RE ]] ||
      die "admin.toml: zuliprc $path"
    want+=("${path#/etc/zab-admin/secrets/}")
  done
  for rel in "${want[@]}"; do
    [ -f "$PAYLOAD/secrets/$rel" ] || die "--secrets has no $rel"
  done
  for rel in "${want[@]}"; do
    install -d -m 0750 "$(dirname "$ETC/secrets/$rel")"
    own root:zab-admin "$(dirname "$ETC/secrets/$rel")"
    install -m 0640 "$PAYLOAD/secrets/$rel" "$ETC/secrets/$rel"
    own root:zab-admin "$ETC/secrets/$rel"
  done
  (cd "$PAYLOAD/secrets" && find . -type f) | while IFS= read -r f; do
    case " ${want[*]} " in *" ${f#./} "*) ;; *) note "Ignored: --secrets has ${f#./}, which nothing names." ;; esac
  done
}

# The fleet key, generated here and never copied off (§5), and one EC2 key pair from it per
# workshop, tagged with its scope at creation, imported by zab-admin with the role (bootstrap).
fleet_key() {
  local key=$VAR/.ssh/id_ed25519 name region key_name tags pairs t
  local -a args
  as_user zab-admin test -e "$key" ||
    as_user zab-admin ssh-keygen -q -t ed25519 -N '' -C "zab-admin@$(hostname -s)" -f "$key"
  pairs=$(admin_config key-pairs)
  while read -r name region key_name tags; do
    [ -n "$name" ] || continue
    args=(--region "$region" --key-name "$key_name" --public-key "$key.pub")
    for t in $tags; do args+=(--tag "$t"); done
    if [ -n "$PREFIX" ]; then
      note "(test) skipped: import_key_pair.py ${args[*]}"
    else
      # -E -s, not -I: the script imports aws_common from its own (root-owned) directory
      as_user zab-admin "$REL/.venv/bin/python" -E -s -B "$REL/workshop/aws/import_key_pair.py" \
        "${args[@]}" || die "$name: the key pair was not imported (README: importing a key pair)"
    fi
  done <<<"$pairs"
}

# systemctl, for the restarts. The tests may name a stub (ZAB_DEPLOY_SYSTEMCTL, prefix mode only).
unit_ctl() {
  if [ -z "$PREFIX" ]; then
    systemctl "$@"
  elif [ -n "${ZAB_DEPLOY_SYSTEMCTL:-}" ]; then
    "$ZAB_DEPLOY_SYSTEMCTL" "$@"
  else
    note "(test) skipped: systemctl $*"
  fi
}

# Restart the IMDS rule, Caddy (with a console host) and SERVICES. Every unit is tried; those
# that don't start are named, with the command that retries them, and the run fails. The
# release is installed and current by then: what remains is to fix the cause and restart.
restart() {
  local s
  local -a units=(zab-admin-imds.service) failed=()
  [ -z "$CONSOLE_HOST" ] || units+=(caddy.service)
  units+=("${SERVICES[@]}")
  for s in "${units[@]}"; do
    if { [ "$s" = zab-admin-imds.service ] || unit_ctl enable "$s"; } && unit_ctl restart "$s" &&
      { ! stops_service "$s" || unit_ctl stop "${s%.socket}.service"; }; then
      continue
    fi
    failed+=("$s")
  done
  if [ ${#failed[@]} -gt 0 ]; then
    note "$V is installed and $OPT/current points to it, but these units did not start: ${failed[*]}"
    note "Find the cause (journalctl -u ${failed[0]}), fix it, then run on the server:"
    note "  sudo systemctl restart ${failed[*]}"
    exit 1
  fi
  imds_active
}

# One line per change in /opt/zab-admin/deploys.jsonl (root 0644); the bot copies it into the
# audit log's chain as an `upgrade` record (PR 9). Every value has passed a pattern.
record() {
  local who=${SUDO_USER:-root}
  [[ $who =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || who=unknown
  printf '{"ts":"%s","event":"%s","version":"%s","commit":"%s","tree":"%s","deployer":"%s"%s}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$V" "$COMMIT" "$MANIFEST" "$who" "$2" >>"$OPT/deploys.jsonl"
  chmod 0644 "$OPT/deploys.jsonl"
}

server_main() {
  local commit='' manifest='' base=- diff=- bootstrap=0 downgrade=0 wheel='' wheel_sha='' running=''
  local kind f
  V='' PAYLOAD='' BUILDING='' WORK='' CONSOLE_HOST='' REL=''
  while [ $# -gt 0 ]; do
    case $1 in
      --bootstrap) bootstrap=1 && shift && continue ;;
      --downgrade) downgrade=1 && shift && continue ;;
    esac
    [ $# -ge 2 ] || die "$1 needs a value"
    case $1 in
      --server) PAYLOAD=$2 ;;
      --version) V=$2 ;;
      --commit) commit=$2 ;;
      --manifest) manifest=$2 ;;
      --base) base=$2 ;;
      --diff-sha256) diff=$2 ;;
      --wheel) wheel=$2 ;;
      --wheel-sha256) wheel_sha=$2 ;;
      *) die "unknown option $1" ;;
    esac
    shift 2
  done
  [[ $V =~ $VERSION_RE && $commit =~ $HEX_RE && $manifest =~ $SHA_RE ]] || die "bad arguments"
  [[ ($base == - || $base =~ $VERSION_RE) && ($diff == - || $diff =~ $SHA_RE) ]] || die "bad arguments"
  [[ -d $PAYLOAD && -f $PAYLOAD/release.bundle ]] || die "no payload"
  PAYLOAD=$(cd "$PAYLOAD" && pwd) # absolute: the server half works from "/"
  PREFIX=${ZAB_DEPLOY_PREFIX:-}
  [[ -z $PREFIX || $PREFIX == /* ]] || die "ZAB_DEPLOY_PREFIX must be absolute"
  OPT=$PREFIX/opt/zab-admin ETC=$PREFIX/etc/zab-admin VAR=$PREFIX/var/lib/zab-admin
  SYSTEMD=$PREFIX/etc/systemd/system CADDY=$PREFIX/etc/caddy
  UV=$OPT/uv/uv PYTHON=/usr/bin/python3
  PASS=()
  if [ -n "$PREFIX" ]; then
    [ "$(id -u)" != 0 ] || die "ZAB_DEPLOY_PREFIX is for the unprivileged tests"
    UV=$(command -v uv) PYTHON=${ZAB_DEPLOY_PYTHON:-$(command -v python3)}
    for f in UV_DEFAULT_INDEX UV_OFFLINE; do [ -z "${!f:-}" ] || PASS+=("$f=${!f}"); done
  else
    [ "$(id -u)" = 0 ] || die "the server half runs as root"
  fi
  # The self-check below guards against running the wrong copy of deploy.sh, not against a
  # compromised deployer account: bash is already reading this user-writable file.
  SELF=$(cd "$(dirname "$0")" && pwd -P)/${0##*/}
  cd /
  exec </dev/null # nothing here reads the terminal, so no child gets it
  shopt -s nullglob
  mkdir -p "$PREFIX/run"
  exec 9>"$PREFIX/run/zab-admin-deploy.lock"
  flock -n 9 || die "another deploy is running"
  trap server_cleanup EXIT
  WORK=$(mktemp -d)
  mkdir "$WORK/home"
  export HOME=$WORK/home XDG_CONFIG_HOME=$WORK/home # no root dotfile reaches git or anything else
  take_payload
  if [ "$bootstrap" = 1 ]; then
    if [ -e "$OPT/current" ] || [ -L "$OPT/current" ]; then die "already bootstrapped"; fi
    priv env DEBIAN_FRONTEND=noninteractive apt-get -q update
    priv env DEBIAN_FRONTEND=noninteractive apt-get -q -y install "${PACKAGES[@]}"
    install_etc
  fi
  REPO=$WORK/repo
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git init -q --bare --template= "$REPO"
  mkdir -p "$REPO/info"
  # Above every .gitattributes of the tree: `git archive` then writes each blob as committed.
  echo '* -export-subst -export-ignore -ident -text -eol -filter -working-tree-encoding' >"$REPO/info/attributes"
  g fetch -q "$PAYLOAD/release.bundle" "refs/tags/$V:refs/tags/$V"
  # fetch.fsckObjects doesn't apply to a bundle (git 2.43): check every object it brought.
  g -c fsck.hasDotdot=error fsck --strict --no-dangling --no-progress >/dev/null 2>&1 ||
    die "$V: the bundle holds malformed objects"
  kind=$(release_kind "$ETC/admin.toml")
  verify_tag "$V" "$ETC/allowed_signers" "$kind"
  [ "$COMMIT" = "$commit" ] || die "$V is $COMMIT here, $commit on the laptop"
  release_paths "$COMMIT"
  MANIFEST=$(tree_of | sha256)
  [ "$MANIFEST" = "$manifest" ] || die "$V: the release tree differs from the laptop's"
  g show "$COMMIT:workshop/adminserver/deploy.sh" | cmp -s - "$SELF" || die "this deploy.sh is not $V's"
  if [ -n "$wheel" ]; then
    [[ $wheel == "zulip_agent_bridge-${V#v}-py3-none-any.whl" && $wheel_sha =~ $SHA_RE ]] || die "bad wheel"
    [ "$(sha256 <"$PAYLOAD/$wheel")" = "$wheel_sha" ] || die "$wheel: wrong SHA-256"
    # releases/wheels is zab-admin's: it places the wheel itself, and root never works there by
    # name. A version's wheel is never replaced.
    f=$VAR/releases/wheels/$wheel
    if as_user zab-admin test -e "$f" || as_user zab-admin test -L "$f"; then
      as_user zab-admin cmp -s - "$f" <"$PAYLOAD/$wheel" || die "$wheel is there with other content"
      note "$wheel is already there."
      return
    fi
    as_user zab-admin tee "$f.new" <"$PAYLOAD/$wheel" >/dev/null
    as_user zab-admin chmod 0640 "$f.new"
    as_user zab-admin mv -f -T -- "$f.new" "$f"
    record wheel ",\"wheel\":\"$wheel\",\"sha256\":\"$wheel_sha\""
    note "Placed $wheel for !rollout."
    return
  fi
  [ -n "$PREFIX" ] || install_uv
  if [ -L "$OPT/current" ]; then
    running=$(readlink "$OPT/current")
    running=${running##*/}
    [[ $running =~ $VERSION_RE ]] || die "current is $running, not a release"
    [ "$bootstrap" = 0 ] || die "already bootstrapped"
    [ "$V" != "$running" ] || die "$V is running"
    ! older "$V" "$running" || [ "$downgrade" = 1 ] || die "$V is older than $running: --downgrade"
  else
    [ "$bootstrap" = 1 ] || die "no release is running: --bootstrap"
    # Users, directories and the IMDS rule first, which the build and every unit rely on: from
    # the commit's blobs, checked before root installs or starts any of it.
    mkdir "$WORK/base"
    g archive --format=tar "$COMMIT" -- workshop/adminserver | tar -xf - -C "$WORK/base" --no-same-owner
    check_tree unpacked "$WORK/base" workshop/adminserver
    install_files "$WORK/base/workshop/adminserver"
    install_units "$WORK/base/workshop/adminserver" # the IMDS rule's, before the build
    priv systemctl enable --now zab-admin-imds.service
  fi
  imds_active
  REL=$OPT/releases/$V
  build "$REL"
  # Every step before `current` moves: a failure removes REL and leaves `current` as it was (none,
  # for a bootstrap), and the running release's units as they were, so the same command can run
  # again. Each step is idempotent. The new release's units are installed and loaded only after
  # the switch, just before the restart (#348).
  install_system
  if [ "$bootstrap" = 1 ]; then
    install_secrets
    fleet_key
  fi
  switch "$REL"
  note "$OPT/current is $V."
  # The record goes with the switch, before the restart: it always names the release `current`
  # points to. A unit that then fails to start is an operational fault, retried by hand (#338).
  record "$([ "$bootstrap" = 1 ] && echo bootstrap || echo upgrade)" \
    ",\"from\":\"${running:--}\",\"base\":\"$base\",\"diff_sha256\":\"$diff\",\"downgrade\":$([ "$downgrade" = 1 ] && echo true || echo false)"
  install_units "$REL/workshop/adminserver" || units_failed "$REL/workshop/adminserver"
  restart
}

((BASH_VERSINFO[0] * 100 + BASH_VERSINFO[1] >= 404)) || die "bash 4.4 or newer, please (macOS: brew install bash)"
if [ "${1:-}" = --server ]; then server_main "$@"; else laptop_main "$@"; fi
