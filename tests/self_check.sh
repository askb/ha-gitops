#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Anil Belur
##############################################################################
# Self-check for gitops_backup.sh — runs entirely locally (no GitHub calls):
#   1. bootstrap dry-run seeds .gitignore and reports status
#   2. real bootstrap pushes the initial import to a local bare "origin"
#   3. drift on a later run produces a backup branch (PR call fails soft,
#      status = warning:branch_pushed)
##############################################################################
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${HERE}/../gitops_backup/rootfs/app/gitops_backup.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/config" "$TMP/data"
git init -q --bare "$TMP/origin.git"

options() { # dry_run
  cat > "$TMP/data/options.json" <<EOF
{"github_repo":"local/test","github_token":"test-token","base_branch":"main",
 "branch_prefix":"auto-backup","dry_run":$1,
 "commit_name":"Test","commit_email":"test@localhost","signoff":true}
EOF
}

run() {
  OPTIONS="$TMP/data/options.json" CONFIG_DIR="$TMP/config" HOME="$TMP" \
    bash "$SCRIPT" >/dev/null 2>&1 || true
}

status() { cat "$TMP/config/.gitops_backup_status"; }

echo "config file content" > "$TMP/config/configuration.yaml"
echo "secret: real" > "$TMP/config/secrets.yaml"

# 1. dry-run bootstrap
options true
run
[ -f "$TMP/config/.gitignore" ] || { echo "FAIL: .gitignore not seeded"; exit 1; }
grep -q "^secrets.yaml$" "$TMP/config/.gitignore" || { echo "FAIL: secrets.yaml not ignored"; exit 1; }
[ "$(status)" = "success:dry_run:bootstrap" ] || { echo "FAIL: dry-run status = $(status)"; exit 1; }

# 2. real bootstrap against local bare origin (remote URL rewritten via insteadOf)
options false
HOME="$TMP" git config --global "url.$TMP/origin.git.insteadOf" \
  "https://x-access-token:test-token@github.com/local/test.git"
run
git -C "$TMP/config" log --oneline -1 | grep -q "initial" || { echo "FAIL: no bootstrap commit"; exit 1; }
git -c safe.bareRepository=all -C "$TMP/origin.git" rev-parse main >/dev/null || { echo "FAIL: main not pushed"; exit 1; }
# secrets must not be committed
if git -C "$TMP/config" ls-files | grep -q secrets.yaml; then
  echo "FAIL: secrets.yaml committed"; exit 1
fi

# 3. drift -> backup branch (PR API unreachable => warning:branch_pushed)
echo "changed" >> "$TMP/config/configuration.yaml"
echo "new file" > "$TMP/config/new_automation.yaml"
run
git -c safe.bareRepository=all -C "$TMP/origin.git" branch | grep -q "auto-backup/" || { echo "FAIL: no backup branch pushed"; exit 1; }
status | grep -q "warning:branch_pushed" || { echo "FAIL: drift status = $(status)"; exit 1; }
git -C "$TMP/config" branch --show-current | grep -q "^main$" || { echo "FAIL: not back on main"; exit 1; }
# Returning to main must not roll the live config back to the old main
grep -q "^changed$" "$TMP/config/configuration.yaml" \
  || { echo "FAIL: live config rolled back after backup"; exit 1; }
[ -f "$TMP/config/new_automation.yaml" ] \
  || { echo "FAIL: new live file deleted after backup"; exit 1; }

# 3b. The backup PR gets merged upstream; the next run pulls it and must find
#     no drift, no conflict, and the live files unchanged.
backup=$(git -c safe.bareRepository=all -C "$TMP/origin.git" branch \
  | grep -o 'auto-backup/[^ ]*' | tail -1)
git clone -q -b main "$TMP/origin.git" "$TMP/merge"
git -C "$TMP/merge" -c user.name=T -c user.email=t@localhost \
  merge -q --no-ff -m "Merge backup PR" "origin/$backup"
git -C "$TMP/merge" push -q origin main
run
[ "$(status)" = "success:no_changes:" ] || { echo "FAIL: post-merge status = $(status)"; exit 1; }
grep -q "^changed$" "$TMP/config/configuration.yaml" \
  || { echo "FAIL: live config lost after merged-PR sync"; exit 1; }
[ "$(git -C "$TMP/config" rev-parse HEAD)" = "$(git -C "$TMP/merge" rev-parse HEAD)" ] \
  || { echo "FAIL: main not synced to the merged PR"; exit 1; }
[ -z "$(git -C "$TMP/config" stash list)" ] || { echo "FAIL: stash left behind"; exit 1; }

# 4. Migration case: repo already had its own .gitignore lacking the addon's
#    entries, and was never excluded. The addon's own status file + log AND any
#    secrets/credentials must be kept out of the backup — even an already-
#    committed secret must be untracked (regression test for seed_gitignore
#    skip-when-.gitignore-exists + secret-leak on migrated repos).
sleep 1  # ensure a distinct auto-backup/<timestamp> branch name
rm -f "$TMP/config/.git/info/exclude"                 # never-excluded repo
printf 'secrets.yaml\n' > "$TMP/config/.gitignore"    # user ignore, no addon/token entries
HOME="$TMP" git -C "$TMP/config" add .gitignore
HOME="$TMP" git -C "$TMP/config" commit -q -m "user gitignore"
echo "success:no_changes:" > "$TMP/config/.gitops_backup_status"
echo "log line" > "$TMP/config/gitops_backup.log"
echo "ya29.oauth-secret" > "$TMP/config/.google.token"  # untracked secret, not in user ignore
echo "PRIVATE KEY" > "$TMP/config/leaked.key"           # secret already committed by a prior run
HOME="$TMP" git -C "$TMP/config" add -f leaked.key
HOME="$TMP" git -C "$TMP/config" commit -q -m "oops: committed a key"
echo "more drift" >> "$TMP/config/configuration.yaml"
run
newbranch=$(git -c safe.bareRepository=all -C "$TMP/origin.git" branch \
  | grep -o 'auto-backup/[^ ]*' | tail -1)
tree=$(git -c safe.bareRepository=all -C "$TMP/origin.git" ls-tree -r --name-only "$newbranch")
if echo "$tree" | grep -qE '^\.gitops_backup_status$|^gitops_backup\.log$'; then
  echo "FAIL: addon runtime files committed to backup branch"; exit 1
fi
if echo "$tree" | grep -qE '^\.google\.token$|^leaked\.key$|^secrets\.yaml$'; then
  echo "FAIL: a secret/credential was committed to the backup branch"; exit 1
fi
echo "$tree" | grep -q "^configuration.yaml$" || { echo "FAIL: real drift not backed up"; exit 1; }

# 5. Static wiring checks for apply_after_pull (no HA/Supervisor available here)
grep -q '"homeassistant_api": true' "${HERE}/../gitops_backup/config.json" \
  || { echo "FAIL: homeassistant_api not enabled in config.json"; exit 1; }
grep -q 'apply_after_pull' "${HERE}/../gitops_backup/config.json" \
  || { echo "FAIL: apply_after_pull option missing from config.json"; exit 1; }
grep -q 'homeassistant/reload_all' "$SCRIPT" \
  || { echo "FAIL: reload_all call missing from script"; exit 1; }

echo "OK: all self-checks passed"
