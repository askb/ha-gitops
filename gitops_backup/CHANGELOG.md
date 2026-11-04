# Changelog

## 0.3.1

- Fix: after pushing a backup branch the add-on no longer rolls the live
  config back. Returning to the base branch used `git checkout`, which
  rewrote `/config` to the pre-backup base branch: every backed-up change
  (UI edits, HACS updates) vanished from disk until the next run pulled the
  merged PR, edits made in that window hit stale files (the source of
  `stash_conflict` / `pull_rebase_failed`), and if the PR was never merged
  the drift was lost for good. HEAD now moves back without touching the
  working tree, so the drift stays uncommitted and is reconciled on the next
  run.

## 0.3.0

- Feature: `apply_after_pull` — after a merged PR is pulled into `/config`, the
  add-on now applies it to the running Home Assistant instead of leaving files
  on disk unloaded. Default `reload` calls `homeassistant.reload_all` (no
  restart); `restart` restarts HA (for `configuration.yaml` integration or
  `custom_components/` changes); `off` keeps the previous behaviour. Only fires
  when the sync actually advanced `HEAD`, never in `dry_run`. Requires the
  add-on's Home Assistant API access (now enabled via `homeassistant_api`).

## 0.2.1

- Security: the never-commit guarantee (secrets, credentials, `.storage/`,
  addon runtime files) now holds for **migrated** repos too, not just fresh
  ones. Patterns are enforced via `.git/info/exclude` on every run, and any
  secret a prior run already committed is untracked (the removal flows out
  through the next backup PR). Previously a repo that arrived with its own
  `.gitignore` lacking these entries could leak an OAuth token or key.

## 0.2.0

- Docs: make the two-pillar backup model explicit — config repo (this add-on)
  + encrypted HA full backup (e.g. Google Drive Backup) = 100% restorable;
  neither alone is enough. Sharpened the "what is backed up where" table
  (names exactly what `.storage/` holds and what lives outside `/config`).
- Security: seed `.gitignore` now also excludes `*.token` / `.google.token`
  so OAuth tokens (e.g. Google Calendar) never land in the repo.
- First bootstrap now logs a disaster-recovery warning: this add-on versions
  declarative config only; run HA full backups for a complete restore.

## 0.1.1

- Fix: keep the addon's own runtime files (`.gitops_backup_status`,
  `gitops_backup.log`) out of backups when migrating a repo that already has a
  `.gitignore` — they were previously swept into backup PRs and risked a
  status-file PR feedback loop. Now excluded via `.git/info/exclude` on every
  run, and untracked if a prior run committed them.

## 0.1.0

- Initial release: PR-gated backup (stash → rebase → drift → branch → PR),
  dry-run mode, DCO sign-off, seeded HA `.gitignore`, status file for a
  `command_line` sensor, template CI workflow for the config repo.
