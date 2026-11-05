#!/bin/bash
# export.sh: a participant's take-home archive (workshop/admin/README.md §7.2).
#
# Run on the VM as `agent` (or by an admin with `sudo -u agent -H bash export.sh`). Creates
# ~/export-<slug>.tar.gz containing ~/work (with .git and uncommitted changes),
# ~/.codex/sessions, ~/.claude/projects and the agents' instructions (~/.codex/AGENTS.md with
# the participant's own section, ~/.claude/CLAUDE.md), without build trees (.lake, __pycache__),
# and EXCLUDES every credential: ~/.codex/auth.json,
# ~/.claude/.credentials.json and ~/.config/zab/ (CONTRIBUTING.md, rule 2). The archive is checked
# afterwards and deleted if a credential path slipped in. <slug> comes from
# /etc/zab-participant.json, written at first boot (workshop/ami/README.md).
set -euo pipefail

participant_json="${ZAB_PARTICIPANT_JSON:-/etc/zab-participant.json}"  # override only for tests
# the slug; records from first boots before it (rc3 and earlier) name the GitHub login
slug=$(python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(d.get("slug") or d.get("github", "").lower())' "$participant_json")
case "$slug" in
  *[!a-z0-9-]*|"") echo "export.sh: bad slug in /etc/zab-participant.json" >&2; exit 1 ;;
esac

out="$HOME/export-${slug}.tar.gz"
cd "$HOME"

members=()
# the agents' instructions too, with the participant's own section (PM on issue #36, Mon 19:00)
for m in work .codex/sessions .claude/projects .codex/AGENTS.md .claude/CLAUDE.md; do
  [ -e "$m" ] && members+=("$m")
done
if [ "${#members[@]}" -eq 0 ]; then
  echo "export.sh: nothing to export under $HOME" >&2
  exit 1
fi

# Build trees are left out: each Lean project's .lake holds a full Mathlib (about 8 GB, rebuilt
# from the local cache with `lake exe cache get`), and topic worktrees each have their own (PM,
# Sat Oct 10: one participant's archive would have been about 40 GB)
tar --create --gzip --file "$out" \
  --exclude='.lake' \
  --exclude='__pycache__' \
  --exclude='.codex/auth.json' \
  --exclude='.claude/.credentials.json' \
  --exclude='.config/zab' \
  --exclude='.config/zab/*' \
  --exclude='*/.zab/inbox/*' \
  -- "${members[@]}"

# Belt and braces: refuse to leave an archive that names a credential file.
if tar --list --gzip --file "$out" | grep -E '(^|/)(auth\.json|\.credentials\.json|[^/]*\.zuliprc)$' >/dev/null; then
  rm -f -- "$out"
  echo "export.sh: credential path found in the archive; deleted it" >&2
  exit 1
fi

echo "wrote $out ($(du -h -- "$out" | cut -f1)); an organizer fetches it with (workshop/admin/README.md §7.2):"
echo "  ssh ubuntu@<ip> -- sudo -u agent cat -- $out > export-${slug}.tar.gz"
