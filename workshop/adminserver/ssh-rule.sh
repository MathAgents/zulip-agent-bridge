#!/usr/bin/env bash
# Admit this laptop's address alone to a server's port 22, in one command (#674): BOOTSTRAP
# §3.1's refresh for the admin server, the Open and Close of workshop/files/README.md, "SSH to
# the files host", for the files host. Run it on your laptop, from a reviewed checkout, in
# BOOTSTRAP §2.1's terminal, never on a machine where coding agents run:
#
#   ssh-rule.sh --save-defaults --profile P --region R --key FILE
#                                       once: the values below, kept in ~/.config/zab/ssh-rule.conf
#   ssh-rule.sh --record-home           at home: record this network's address, as BOOTSTRAP §0's
#                                       HOMEIP, in ~/.config/zab/homeip, and nothing else
#   ssh-rule.sh                         from another network: the admin server's group admits
#                                       this network's /32 alone on port 22
#   ssh-rule.sh --files-host            the same for the files host's group
#   ssh-rule.sh --close [--files-host]  no rule for port 22 at all; from any network
#
#   --dry-run          look everything up and check it, say what would change, change nothing
#   --profile P        the AWS profile of the account's owner, --region R the group's region
#   --key FILE         the dedicated key, whose fingerprint the agent must hold, alone
#                      (BOOTSTRAP §2.1); each of the three overrides the saved one
#   --group-name NAME  the admin server's group by another name than zab-admin, still with the
#                      zab-admin tag, such as a test server's. Not with --files-host
#   --home-unknown     no home address to refuse, for a run away from home with none recorded
#
# There are no built-in values: with neither a flag nor a saved value for the profile and the
# region (and, to open, the key), the run stops and says what to give.
#
# The address comes from checkip.amazonaws.com. It is refused when it is the one recorded as
# home, a network an agent machine shares (BOOTSTRAP §3.1), and the run refuses to start when
# none is recorded, unless the file says `none` or --home-unknown is given. A record over 30
# days old gets a warning: record it again whenever home's address may have changed. The home
# address is written to that one file, never printed, and never written anywhere else.
#
# Then, as the runbook's block: every ingress rule for port 22 alone (22 to 22, whatever its
# source) is revoked, and one for this /32 authorized; each prints AWS's `True`. Last, the
# group's rules are read back, and must be exactly 443 and 80 from 0.0.0.0/0 and 22 from this
# /32, with no other source (after --close: 443 and 80 alone). Every rule ID is checked before
# the first revoke. Any refusal or mismatch ends with a `stopped: …` line and exit code 1; a
# usage error, 2. A failure after a revoke says that port 22 may admit nothing now: running
# this again then opens it.
#
# The home address and the key are checked first, before any AWS call: the key since the SSH
# step that follows needs it. A script can't start an agent for the shell that runs it: when
# there is none, or it lacks the key, the run stops and prints the commands to paste, and you
# run it again.
set -euo pipefail
export LC_ALL=C AWS_PAGER=""
umask 077

PROFILE="" REGION="" KEY=""
CONF=$HOME/.config/zab/ssh-rule.conf # --save-defaults: profile=…, region=…, key=…
HOMEFILE=$HOME/.config/zab/homeip
HOME_DAYS=30 # a home address recorded longer ago than this gets a warning
CHECKIP=https://checkip.amazonaws.com
ADMIN_GROUP=zab-admin        # BOOTSTRAP §3.1: the group's name, and a tag key it carries
GROUP=$ADMIN_GROUP           # the name looked up: --group-name's, or that one
FILES_INSTANCE=zab-central-files # workshop/files/README.md §1: the files host's Name tag
ANY=0.0.0.0/0
# shellcheck disable=SC2016 # JMESPath's literals are in backticks, for the AWS CLI
READBACK='SecurityGroups[0].IpPermissions[].[IpProtocol,FromPort,ToPort,join(`","`,IpRanges[].CidrIp || `[]`),join(`","`,Ipv6Ranges[].CidrIpv6 || `[]`),join(`","`,UserIdGroupPairs[].GroupId || `[]`),join(`","`,PrefixListIds[].PrefixListId || `[]`)]'
# shellcheck disable=SC2016
RULES='SecurityGroupRules[?!IsEgress && FromPort==`22` && ToPort==`22`].SecurityGroupRuleId'

stop() {
  printf 'stopped: %s\n' "$*" >&2
  exit 1
}
# synopsis: the header's commands and options
synopsis() { sed -n '/^#   ssh-rule.sh --save-defaults/,/^#   --home-unknown/s/^# \{0,1\}//p' "$0"; }
usage() {
  printf 'ssh-rule.sh: %s\n' "$*" >&2
  synopsis >&2
  exit 2
}
warn() { printf 'warning: %s\n' "$*" >&2; }

((BASH_VERSINFO[0] * 100 + BASH_VERSINFO[1] >= 404)) || stop "bash 4.4 or newer, please (macOS: brew install bash)"

files=0 close=0 dry=0 record=0 unknown=0 save=0 valued=0 named=0
while (($#)); do
  case $1 in
  --files-host) files=1 ;;
  --close) close=1 ;;
  --dry-run) dry=1 ;;
  --record-home) record=1 ;;
  --home-unknown) unknown=1 ;;
  --save-defaults) save=1 ;;
  --profile | --region | --key | --group-name)
    if (($# < 2)) || [[ -z $2 || $2 == -* ]]; then
      usage "$1 needs a value"
    fi
    case $1 in
    --profile) PROFILE=$2 ;;
    --region) REGION=$2 ;;
    --key) KEY=$2 ;;
    --group-name) GROUP=$2 named=1 ;;
    esac
    valued=1
    shift
    ;;
  -h | --help)
    synopsis
    exit 0
    ;;
  *) usage "unknown argument: $1" ;;
  esac
  shift
done
((record + close + save <= 1)) || usage "--record-home, --save-defaults and --close: one at a time"
((!record || !(files + unknown + valued))) || usage "--record-home records the address alone: no other option but --dry-run"
((!save || !(files + unknown + dry))) || usage "--save-defaults takes --profile, --region and --key alone"
((!close || !unknown)) || usage "--close checks no address: --home-unknown has nothing to skip"
((!named || !(files + save))) || usage "--group-name names the admin server's group: not with --files-host or --save-defaults"

PROFILE_RE='^[A-Za-z0-9_.+=@-]+$'
REGION_RE='^[a-z]{2}(-[a-z]+)+-[0-9]$'
GROUP_RE='^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$'
[[ -z $PROFILE || $PROFILE =~ $PROFILE_RE ]] || usage "--profile: not a profile name: $PROFILE"
[[ -z $REGION || $REGION =~ $REGION_RE ]] || usage "--region: not a region: $REGION"
[[ $GROUP =~ $GROUP_RE ]] || usage "--group-name: not a group name ([A-Za-z0-9._-]): $GROUP"

if ((save)); then
  [[ -n $PROFILE && -n $REGION && -n $KEY ]] || usage "--save-defaults needs --profile, --region and --key"
  [[ $KEY != \~/* ]] || KEY=$HOME/${KEY#\~/}
  [[ $KEY == /* ]] || KEY=$PWD/$KEY
  [[ $KEY != *[[:space:]]* ]] || usage "--key: a path without blanks, please"
  [[ -r $KEY.pub ]] || stop "no $KEY.pub: --key names the dedicated key's file, with its .pub beside it (BOOTSTRAP §2.1)"
  mkdir -p "${CONF%/*}"
  chmod 700 "${CONF%/*}"
  printf 'profile=%s\nregion=%s\nkey=%s\n' "$PROFILE" "$REGION" "$KEY" >"$CONF.new"
  mv -f "$CONF.new" "$CONF"
  echo "saved, in $CONF: profile $PROFILE, region $REGION, key $KEY"
  exit 0
fi

# saved NAME: the value saved for NAME by --save-defaults, or nothing. The file is read, never
# sourced, and each value is checked as a flag's is.
saved() {
  local line
  [[ -f $CONF ]] || return 0
  while IFS= read -r line || [[ -n $line ]]; do
    if [[ $line == "$1="* ]]; then
      printf '%s\n' "${line#*=}"
      return 0
    fi
  done <"$CONF"
}
if ((!record)); then
  [[ -n $PROFILE ]] || PROFILE=$(saved profile)
  [[ -n $REGION ]] || REGION=$(saved region)
  [[ -n $KEY ]] || KEY=$(saved key)
  [[ -z $PROFILE || $PROFILE =~ $PROFILE_RE ]] || stop "$CONF: profile is not a profile name: $PROFILE"
  [[ -z $REGION || $REGION =~ $REGION_RE ]] || stop "$CONF: region is not a region: $REGION"
  missing=()
  [[ -n $PROFILE ]] || missing+=(--profile)
  [[ -n $REGION ]] || missing+=(--region)
  ((close)) || [[ -n $KEY ]] || missing+=(--key)
  ((${#missing[@]} == 0)) || stop "no ${missing[*]}: give them, or save them once with" \
    "ssh-rule.sh --save-defaults --profile P --region R --key FILE (the account owner's profile, the server's region, the dedicated key)"
fi

# ipv4 TEXT: one dotted quad, each part at most 255, without leading zeros, and not 0.0.0.0.
ipv4() {
  local part
  [[ $1 =~ ^((0|[1-9][0-9]{0,2})\.){3}(0|[1-9][0-9]{0,2})$ && $1 != 0.0.0.0 ]] || return 1
  for part in ${1//./ }; do
    ((part <= 255)) || return 1
  done
}

# this_address: this network's public IPv4, as checkip sees it, with /32. Only the line's end
# and the blanks around the answer are dropped: a blank or a line break inside it refuses it.
this_address() {
  local out
  out=$(curl -fsS --max-time 20 "$CHECKIP") || stop "checkip ($CHECKIP) didn't answer: try again"
  out=${out%$'\r'}
  out=${out#"${out%%[![:blank:]]*}"}
  out=${out%"${out##*[![:blank:]]}"}
  if [[ $out == *[[:space:]]* ]] || ! ipv4 "$out"; then
    stop "checkip's answer is not one address: $(printf '%q' "${out:0:60}")"
  fi
  printf '%s/32\n' "$out"
}

if ((record)); then
  NEW=$(this_address)
  if ((dry)); then
    echo "DRY RUN: would record this network's address as home, in $HOMEFILE"
    exit 0
  fi
  mkdir -p "${HOMEFILE%/*}"
  chmod 700 "${HOMEFILE%/*}"
  printf '%s\n' "$NEW" >"$HOMEFILE.new"
  mv -f "$HOMEFILE.new" "$HOMEFILE"
  echo "recorded: this network's address, as home, in $HOMEFILE (not printed)"
  echo "From here, run ssh-rule.sh from another network: a phone's hotspot, a VPN, your office."
  exit 0
fi

# check_home: refuse this network if it is home's (BOOTSTRAP §0, §3.1).
check_home() {
  local home=""
  if ((unknown)); then
    warn "no home address checked (--home-unknown): run this only from a network that no machine running coding agents shares"
    return
  fi
  [[ -f $HOMEFILE ]] || stop "no home address in $HOMEFILE. At home, run: ssh-rule.sh --record-home." \
    "Away from home with none recorded, or sharing no network with an agent machine: --home-unknown"
  IFS= read -r home <"$HOMEFILE" || true
  if [[ $home == none ]]; then
    warn "$HOMEFILE says none: no home address checked, so run this only from a network that no machine running coding agents shares"
  elif [[ $home =~ ^[0-9.]+/32$ ]] && ipv4 "${home%/32}"; then
    [[ $NEW != "$home" ]] || stop "this network is home's, a network an agent machine shares." \
      "Switch to another network (a phone's hotspot), then run this again"
    if [[ -n $(find "$HOMEFILE" -mtime +"$HOME_DAYS" -print) ]]; then
      warn "home's address was recorded over $HOME_DAYS days ago: if it may have changed, run ssh-rule.sh --record-home at home"
    fi
    echo "home: not this network ($HOMEFILE)"
  else
    stop "$HOMEFILE holds neither one address/32 nor none: run ssh-rule.sh --record-home at home"
  fi
}

# check_key: the agent of this terminal holds the dedicated key, and only it (BOOTSTRAP §2.1).
check_key() {
  local fp listed rc=0 load
  [[ $KEY != \~/* ]] || KEY=$HOME/${KEY#\~/} # a saved file's ~/, as the shell would
  [[ -r $KEY.pub ]] || stop "no $KEY.pub: --key names the dedicated key's file, with its .pub beside it (BOOTSTRAP §2.1)"
  read -r _ fp _ < <(ssh-keygen -l -f "$KEY.pub") || true
  [[ $fp == SHA256:* ]] || stop "ssh-keygen read no fingerprint from $KEY.pub"
  listed=$(ssh-add -l 2>/dev/null) || rc=$?
  printf -v load 'ssh-add -t 8h %q' "$KEY"
  # shellcheck disable=SC2016 # printed for the caller's shell to run, not expanded here
  local start='  eval "$(ssh-agent -s)"'
  if ((rc == 2)); then
    stop "this terminal has no ssh agent, and the SSH step after this needs the key in one.
Paste these, then run this again (ssh-add asks for the passphrase):
$start
  $load"
  elif ((rc != 0)) || ! grep -qF " $fp " <<<"$listed"; then
    stop "the agent doesn't hold $KEY ($fp).
Paste this, then run this again (it asks for the passphrase):
  $load"
  elif (($(grep -c . <<<"$listed") != 1)); then
    stop "the agent holds other keys besides $KEY: BOOTSTRAP §2.1's terminal has an agent of its own, with the dedicated key alone.
In a new terminal, paste these, then run this again (ssh-add asks for the passphrase):
$start
  $load"
  fi
  echo "key: $fp, alone in the agent"
}

ERR=$(mktemp)
trap 'rm -f "$ERR"' EXIT
# ec2 ARGS…: `aws ec2 ARGS…` with the profile and region; its output, or a stop with its error.
ec2() {
  local out
  out=$(aws --profile "$PROFILE" --region "$REGION" ec2 "$@" 2>"$ERR") ||
    stop "aws ec2 $1 failed: $(head -c 400 "$ERR")"
  printf '%s\n' "$out"
}

# check_rules TEXT EXPECTED…: the read-back's lines, trailing blanks dropped, are EXPECTED's.
check_rules() {
  local got want
  got=$(sed -e 's/[[:space:]]*$//' -e '/^$/d' <<<"$1" | sort)
  shift
  want=$(printf '%s\n' "$@" | sort)
  [[ $got == "$want" ]]
}

((dry)) && echo "DRY RUN: nothing changes"
# first what this laptop can tell alone, so that a refusal makes no AWS call
expected=("tcp	443	443	$ANY" "tcp	80	80	$ANY")
if ((!close)); then
  NEW=$(this_address)
  check_home # before the address is printed: it may be home's
  echo "address: $NEW (checkip)"
  check_key
  expected+=("tcp	22	22	$NEW")
fi
if ((files)); then
  what="the files host's group"
  desc="admin"
  FI=$(ec2 describe-instances \
    --filters "Name=tag:Name,Values=$FILES_INSTANCE" Name=instance-state-name,Values=running \
    --query 'Reservations[].Instances[].InstanceId' --output text)
  [[ $FI =~ ^i-[0-9a-f]+$ ]] || stop "not one running instance named $FILES_INSTANCE: '${FI:0:80}'"
  SG=$(ec2 describe-instances --instance-ids "$FI" \
    --query 'Reservations[].Instances[].SecurityGroups[].GroupId' --output text)
else
  what="the admin server's group"
  ((!named)) || what="the admin server's group $GROUP"
  desc="deployer"
  SG=$(ec2 describe-security-groups \
    --filters "Name=group-name,Values=$GROUP" "Name=tag-key,Values=$ADMIN_GROUP" \
    --query 'SecurityGroups[].GroupId' --output text)
fi
[[ $SG =~ ^sg-[0-9a-f]+$ ]] || stop "not one group for $what: '${SG:0:80}'"
echo "group: $SG, $what (profile $PROFILE, region $REGION)"

# into a variable first: a failure in a `for` list's substitution would not stop the script;
# and every ID checked before the first revoke, so that a refusal changes nothing
ids=$(ec2 describe-security-group-rules --filters "Name=group-id,Values=$SG" --query "$RULES" --output text)
for R in $ids; do
  [[ $R =~ ^sgr-[0-9a-f]+$ ]] || stop "not a rule ID: '${R:0:80}'"
done
closed="port 22 may admit nothing now: run this again, or BOOTSTRAP §3.1's block"
for R in $ids; do
  if ((dry)); then
    echo "would revoke $R"
  else
    said=$(ec2 revoke-security-group-ingress --group-id "$SG" --security-group-rule-ids "$R" \
      --query Return --output text) || stop "$closed"
    echo "revoke $R: $said"
    [[ $said == True ]] || stop "AWS didn't revoke $R: $closed"
  fi
done
if ((!close)); then
  if ((dry)); then
    echo "would authorize tcp 22 from $NEW"
  else
    said=$(ec2 authorize-security-group-ingress --group-id "$SG" \
      --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$NEW,Description=$desc}]" \
      --query Return --output text) || stop "$closed"
    echo "authorize tcp 22 from $NEW: $said"
    [[ $said == True ]] || stop "AWS didn't authorize $NEW: $closed"
  fi
fi

rules=$(ec2 describe-security-groups --group-ids "$SG" --query "$READBACK" --output text)
echo "rules (protocol, ports, then IPv4 ranges, IPv6 ranges, groups and prefix lists):"
printf '%s\n' "$rules" | sed -e '/^[[:space:]]*$/d' -e 's/^/  /'
if ((dry)); then
  # what the run would leave: the rules that aren't for port 22 alone, of any protocol, as
  # RULES selects them, as they are, and the new one for 22
  rules=$(grep -vE $'^[^\t]*\t22\t22(\t|$)' <<<"$rules" || true)
  ((close)) || rules+=$'\n'"tcp	22	22	$NEW"
fi
want="443 and 80 from $ANY"
((close)) || want+=", 22 from $NEW alone"
if ! check_rules "$rules" "${expected[@]}"; then
  ((dry)) && stop "after the run the group would not hold just $want: look at the rules above first (BOOTSTRAP §3.1)"
  stop "the group doesn't hold just $want: remove the others by hand before you connect (BOOTSTRAP §3.1)"
fi
if ((dry)); then
  echo "ok: the run would leave $want"
else
  echo "ok: $want"
fi
