# Changelog

## Unreleased

- Docs: "add-on" → "app" in all user-facing text, following Home
  Assistant's rename. Platform identifiers (`io.hass.type=addon`, linter
  action, badge URLs, CI job/check names) are unchanged.

## 0.4.1

- Fix: `secrets.yaml.*` in 0.4.0 also matched committed templates such as
  `secrets.yaml.example`, untracking them and putting their deletion into
  the backup PR. `secrets.yaml.{example,sample,template,dist}` are now
  explicitly allowed; `*.bak.*` (e.g. `entity_registry.bak.<ts>` registry
  dumps) is excluded. The untracker now uses git's own matcher so negations
  behave exactly as in `.gitignore`.

## 0.4.0

- Security: backup/editor copies of `secrets.yaml` (`secrets.yaml.*`, `*.bak`,
  `*.bak-*`, `*.orig`, `*.secret`) and `zigbee2mqtt/secret.yaml` are now on the
  never-commit list. A `secrets.yaml.bak-<date>` left by a manual edit was
  enough to put every real secret into a backup PR.
- Security: the GitHub token is no longer stored in `/config/.git/config`
  (readable by any app with config access and included in HA full
  backups). The remote is token-less; auth is passed per network call via
  `http.extraheader`. Existing installs are scrubbed on the next run.
- Fix: one open backup PR at a time. While a PR is unmerged, new drift is
  pushed onto its branch (`pr_updated`) and unchanged drift is a no-op
  (`pr_open_unchanged`) instead of opening a near-identical PR every run
  (a consequence of 0.3.1 keeping drift on disk until merged).
- Change: the backup commit is built from the index (`write-tree` /
  `commit-tree`) — the app never switches branches in `/config` any more.
- Change: commit and PR bodies list the changed files.
- Change: `GITHUB_API_URL` is honoured (GitHub Enterprise / tests).
- Template: required HA gate bumped from 2026.3 to 2026.10.

## 0.3.1

- Fix: after pushing a backup branch the app no longer rolls the live
  config back. Returning to the base branch used `git checkout`, which
  rewrote `/config` to the pre-backup base branch: every backed-up change
  (UI edits, HACS updates) vanished from disk until the next run pulled the
  merged PR, edits made in that window hit stale files (the source of
  `stash_conflict` / `pull_rebase_failed`), and if the PR was never merged
  the drift was lost for good. HEAD now moves back without touching the
  working tree, so the drift stays uncommitted and is reconciled on the next
  run.
- Fix: after a `stash_conflict` the app kept the live files but left the
  conflict in the git index, so every later run failed with `needs merge` /
  `could not write index` and no backup or upstream sync happened again
  until someone ran `git reset` by hand. The index is now cleared right after
  the conflict, and a repo already stuck that way heals on the next run.
  The working tree (the live config) is never touched.

## 0.3.0

- Feature: `apply_after_pull` — after a merged PR is pulled into `/config`, the
  app now applies it to the running Home Assistant instead of leaving files
  on disk unloaded. Default `reload` calls `homeassistant.reload_all` (no
  restart); `restart` restarts HA (for `configuration.yaml` integration or
  `custom_components/` changes); `off` keeps the previous behaviour. Only fires
  when the sync actually advanced `HEAD`, never in `dry_run`. Requires the
  app's Home Assistant API access (now enabled via `homeassistant_api`).

## 0.2.1

- Security: the never-commit guarantee (secrets, credentials, `.storage/`,
  app runtime files) now holds for **migrated** repos too, not just fresh
  ones. Patterns are enforced via `.git/info/exclude` on every run, and any
  secret a prior run already committed is untracked (the removal flows out
  through the next backup PR). Previously a repo that arrived with its own
  `.gitignore` lacking these entries could leak an OAuth token or key.

## 0.2.0

- Docs: make the two-pillar backup model explicit — config repo (this app)
  + encrypted HA full backup (e.g. Google Drive Backup) = 100% restorable;
  neither alone is enough. Sharpened the "what is backed up where" table
  (names exactly what `.storage/` holds and what lives outside `/config`).
- Security: seed `.gitignore` now also excludes `*.token` / `.google.token`
  so OAuth tokens (e.g. Google Calendar) never land in the repo.
- First bootstrap now logs a disaster-recovery warning: this app versions
  declarative config only; run HA full backups for a complete restore.

## 0.1.1

- Fix: keep the app's own runtime files (`.gitops_backup_status`,
  `gitops_backup.log`) out of backups when migrating a repo that already has a
  `.gitignore` — they were previously swept into backup PRs and risked a
  status-file PR feedback loop. Now excluded via `.git/info/exclude` on every
  run, and untracked if a prior run committed them.

## 0.1.0

- Initial release: PR-gated backup (stash → rebase → drift → branch → PR),
  dry-run mode, DCO sign-off, seeded HA `.gitignore`, status file for a
  `command_line` sensor, template CI workflow for the config repo.
