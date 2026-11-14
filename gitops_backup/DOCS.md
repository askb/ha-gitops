# GitOps Config Backup

PR-gated GitHub backup for your Home Assistant config.

## What you need

- A **GitHub account** (free is fine)
- A **repository** for your config — private recommended
- A **fine-grained personal access token (PAT)** scoped to that one repo

## Step-by-step setup

### 1. Create the repository

github.com → **+** → **New repository** → name it (e.g. `my-ha-config`) →
**Private** → Create. Don't add a README — the app bootstraps the content.

### 2. Create the token

1. github.com → your avatar → **Settings** → **Developer settings** →
   **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
2. **Token name**: `ha-gitops` · **Expiration**: 1 year (set a calendar
   reminder — the app status will show `error` when it expires)
3. **Repository access**: *Only select repositories* → pick the repo from step 1
4. **Permissions → Repository permissions**:
   - **Contents**: Read and write
   - **Pull requests**: Read and write
   - everything else: No access
5. Generate, copy the `github_pat_…` value — you won't see it again

> The token can only touch that single repo. Even if it leaked, your other
> repos and account are untouchable — that's why fine-grained beats classic PATs.

### 3. Configure the app

Settings → Apps → GitOps Config Backup → **Configuration**:

```yaml
github_repo: yourname/my-ha-config
github_token: github_pat_XXXXXXXX
dry_run: true        # first run: log only, push nothing
```

Start the app, then check `gitops_backup.log` in your config folder to see
what *would* be committed. Happy? Set `dry_run: false` and restart. The first
real run pushes the initial import; every run after that only opens PRs.

The token is stored in the app options by the Supervisor — it never goes
into git, and the seeded `.gitignore` keeps `secrets.yaml` and other sensitive
files out of the repo entirely.

### 4. Add the CI gate (recommended)

Copy `template/workflows/validate-ha-config.yaml` from this repository into
`.github/workflows/` of **your config repo**, and add a stub line for every
`!secret` key you use (CI never sees real secrets). Then protect `main`:
repo → Settings → Branches → require the *HA Config Validation* and
*HA Smoke Boot* checks.

## What is backed up where (best practice)

This app is **change tracking for your config**, not disaster recovery.
Run both layers:

| Data | This app (GitHub) | HA Backup + Google Drive |
|------|----------------------|--------------------------|
| Config YAML (automations, dashboards, …) | ✅ versioned, reviewable diffs | ✅ inside the archive |
| `secrets.yaml` | ❌ **never** (gitignored) | ✅ inside the **encrypted** archive |
| `.storage/` — UI-added **integrations**, **logins/tokens**, **device/entity/area registries**, **UI (Lovelace) dashboards**, helpers | ❌ never (holds secrets) | ✅ **most restore-critical item** |
| Credentials: `*.token`, `.google.token`, `*.key`, `*.pem`, `*-key.bin`, `esphome/.device-builder*`, `.cloud/` | ❌ never | ✅ |
| Databases, history, logs | ❌ never | ✅ (recorder DB, if selected) |
| App configs/data, TLS certs (`/ssl`) — live **outside** `/config` | ❌ app can't see them | ✅ |
| Media, camera recordings | ❌ | usually excluded — external drive |

> **The 100% guarantee: config repo + encrypted full backup. Neither alone is
> enough.** Restore from the git repo *only* and you'd get your YAML back but
> have to re-do by hand every UI-added integration, all logins/tokens, device
> and entity names, UI dashboards, `secrets.yaml`, and every app — because
> those live in `.storage/`, `secrets.yaml`, or outside `/config`. That's why a
> full backup (Pillar 2) is mandatory, not optional.

**Recommended stack:**

```text
GitHub (this app)      → config changes: who/what/when, PR review, CI gate
HA auto backup (daily)    → full system state, encrypted, on the box
 └─ Google Drive Backup   → off-site copies of those archives
Password manager          → the three keys to the kingdom (below)
```

### Where secrets belong

- `secrets.yaml` lives **only on the box** — referenced via `!secret` in your
  config, excluded from git by the seeded `.gitignore`, and recovered from the
  encrypted HA backup, never from GitHub.
- Store in your **password manager** (never in the repo, never in a README):
  1. the HA **backup encryption password** — without it your off-site backups
     are unrestorable,
  2. the **GitHub PAT** for this app,
  3. your HA admin credentials.
- CI validates PRs with **stub** secrets (step 4) — real values never leave
  the box.

**Disaster recovery order:** reinstall HAOS → restore the encrypted HA backup
(brings back `secrets.yaml`, `.storage`, apps) → your config repo is then
already live on the box and git history simply resumes.

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `github_repo` | — | `owner/repo` to back up into (required) |
| `github_token` | — | Fine-grained PAT (required) |
| `base_branch` | `main` | Branch PRs target |
| `branch_prefix` | `auto-backup` | Prefix for backup branches |
| `interval_hours` | `24` | Hours between runs (1–168). A merged PR is pulled at the next run, so this is also the longest it takes to reach Home Assistant; set `1` if you merge often. |
| `run_at_start` | `false` | Also run when the app starts |
| `dry_run` | `false` | Log what would change; push nothing |
| `commit_name` / `commit_email` | see defaults | Commit author identity. Set a valid (or GitHub noreply) address if the DCO app is enabled on the repo — `Signed-off-by` uses it. |
| `signoff` | `true` | Add `Signed-off-by` (DCO) to commits |
| `apply_after_pull` | `reload` | After a merged PR is pulled into `/config`, apply it to the running HA: `reload` calls `homeassistant.reload_all` (automations, scripts, scenes, templates, `input_*`) and then refreshes YAML dashboards in every open browser session with no restart; `restart` calls `homeassistant.restart` (use when merged changes touch `configuration.yaml` integrations or `custom_components/`, which are not hot-reloadable); `off` disables it. Only fires when the sync actually advanced `HEAD`, never in `dry_run`. |

## Dashboards

YAML-mode dashboards (`lovelace: dashboards:` entries with `mode: yaml`)
are plain files, so they are backed up and a merged change deploys with no
restart: after the pull the app asks Home Assistant for each YAML
dashboard once, which makes it re-read the file and tell every open
browser session to update.

Dashboards you edit in the UI are **not** covered. Home Assistant keeps
them in `.storage/`, which is excluded (it also holds tokens and device
registries), and loads them only at startup. Change those in the UI, or
convert a dashboard to YAML mode if you want it reviewed and deployed
through pull requests.

## What is committed

Everything in your config folder **except** the seeded `.gitignore` exclusions:
`secrets.yaml` and any copy of it (`secrets.yaml.*` except
`.example/.sample/.template/.dist`, `*.bak`, `*.bak-*`, `*.bak.*`,
`*.orig`, `*.secret`, `zigbee2mqtt/secret.yaml`), `.storage/`, `.cloud/`,
databases, logs, `*.key`, `*.pem`, `*-key.bin`, `*.token`, `.google.token`,
`esphome/.device-builder*` (ESPHome Device Builder peer key/state),
`known_devices.yaml`, `ip_bans.yaml`, and the app's own status/log files.
The secret patterns are also enforced through `.git/info/exclude` on every
run, so they hold for migrated repos with their own `.gitignore` too.

### Content scan (secret backstop)

Path rules cannot know about a token pasted into a shell script or a
`password: hunter2` literal in a YAML file, so every staged text file is
also scanned for secret-looking **content** before the commit is built:
private-key blocks, GitHub/AWS/Slack/Google tokens, JWTs (HA long-lived
tokens), and `password|api_key|*_token|*_secret: <literal>` lines. A file
that trips a rule is **held back** from that backup (never deleted from
`/config`) and listed in the PR body under "Held back"; fix it with
`!secret` and it ships on the next run. If *only* such files changed the
status is `warning:secrets_held:<files>`. `!secret x`, `{{ templates }}`,
`${ENV}` and placeholder values are not matched.

## Backup PRs

One open backup PR at a time: if the previous PR has not been merged yet,
new drift is committed on top of its branch (status `pr_updated`); if nothing
changed since, the run is a no-op (`pr_open_unchanged`). Commit and PR
bodies list the changed files.

## Status in Home Assistant

```yaml
command_line:
  - sensor:
      name: GitOps Backup Status
      command: "cat /config/.gitops_backup_status 2>/dev/null || echo 'unknown'"
      scan_interval: 3600
```

Values: `success:pr_created:<url>` · `success:no_changes:` ·
`success:no_changes:applied <sha>` (a merged PR was pulled and applied) ·
`success:dry_run:<n files>` · `warning:…` · `error:…`
