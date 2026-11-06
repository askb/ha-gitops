# HA GitOps — Home Assistant App

**PR-gated GitHub backup for your Home Assistant config.** Nothing force-pushes,
every change is a reviewable pull request, and CI validates your config against
pinned Home Assistant versions *before* it can merge.

[![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Faskb%2Fha-gitops)

[![GitHub Release](https://img.shields.io/github/v/release/askb/ha-gitops?label=release)](https://github.com/askb/ha-gitops/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/askb/ha-gitops/total?label=downloads)](https://github.com/askb/ha-gitops/releases)
[![License](https://img.shields.io/github/license/askb/ha-gitops)](LICENSE)

## Why another config-backup app?

Most config-sync tools upload your files straight onto a branch — some even
force-push. That gives you a *copy*, not *control*. This app treats your
config like production infrastructure:

| | Direct-push sync tools | GitOps Config Backup |
|---|---|---|
| Changes land on main | Immediately, sometimes force-pushed | **Never** — every drift becomes a PR |
| Review before accept | No | **Yes** — diff, comment, merge (or close) |
| CI validation | No | **Yes** — HA version-matrix check **+ smoke boot** (config must actually start) gate the merge |
| Bidirectional | Usually one-way upload | **Yes** — merged PRs flow back on the next run |
| Secrets safety | gitignore defaults | gitignore defaults **+ CI runs with stub secrets only** |

## How it works

```mermaid
flowchart LR
    subgraph HA["🏠 Home Assistant box"]
        CFG[("/config")]
        ADDON["GitOps Backup app<br/>two-way sync, every run:<br/>⬇ pull merged → ⬆ push drift"]
        CFG -- "① local edits<br/>(UI, HA upgrades)" --> ADDON
        ADDON -- "⑤ merged changes<br/>applied to /config" --> CFG
    end

    subgraph GH["☁️ GitHub"]
        BR["auto-backup/… branch"]
        PR{{"Pull Request"}}
        subgraph CI["CI gate — must pass"]
            LINT["yamllint"]
            CHECK["HA config check<br/>pinned + stable matrix"]
            BOOT["HA smoke boot<br/>container must start"]
        end
        MAIN[("main")]
    end

    YOU(("👤 You / dev<br/>author & review"))

    ADDON -- "② drift → branch + PR" --> BR
    BR --> PR
    YOU -- "② or: submit config<br/>change PR yourself" --> PR
    PR --> LINT & CHECK & BOOT
    LINT & CHECK & BOOT --> YOU
    YOU -- "③ merge" --> MAIN
    MAIN -- "④ pull --rebase<br/>on the next run" --> ADDON

    classDef box fill:#1c3a5e,stroke:#7fb3ff,color:#ffffff
    classDef addon fill:#0d7a5f,stroke:#34c39a,color:#ffffff
    classDef gate fill:#7a3b0d,stroke:#ffb066,color:#ffffff
    classDef human fill:#5e1c4a,stroke:#ff7fd4,color:#ffffff
    classDef store fill:#333d3a,stroke:#9aa5a1,color:#ffffff
    class ADDON addon
    class LINT,CHECK,BOOT gate
    class YOU human
    class CFG,MAIN,BR,PR store
```

**The loop, numbered:** ① you edit via the HA UI (or an upgrade changes files) →
② the app turns that drift into a PR — or you open a config PR yourself →
③ CI gates it, you merge → ④ the app's next run pulls merged `main` →
⑤ and applies it to `/config`. Both directions go through the same run:
it **pulls first, then pushes drift**, so box and repo converge on `main`.

1. **Daily (configurable) run** on your Home Assistant box:
   stash local drift → `pull --rebase` from `origin/main` (picks up PRs you
   merged) → re-apply → if the live config changed, commit to a dated
   `auto-backup/…` branch and **open a PR** via the GitHub API.
2. **CI on the PR** (template workflows included): yamllint + full
   `frenck/action-home-assistant` validation against a pinned HA version (with
   a `stable` early-warning leg) **+ a smoke boot** — the pinned HA Core
   container starts with your config and must answer HTTP with no
   `Invalid config` — upgrade breakage surfaces in CI, not on your Pi.
3. **You merge** (from your phone, if you like). The next app run rebases the
   merged state back onto the box.

## Install

1. Add this repository to your App store (badge above), install
   **GitOps Config Backup**.
2. Create a GitHub repo (private recommended) and a fine-grained PAT with
   *Contents: read/write* and *Pull requests: read/write* on it.
3. Configure the app:

```yaml
github_repo: you/your-ha-config
github_token: github_pat_…
base_branch: main
branch_prefix: auto-backup
interval_hours: 24
run_at_start: false
dry_run: true        # start with a dry run, check the log, then set false
signoff: true
```

4. Copy `template/workflows/validate-ha-config.yaml` into
   `.github/workflows/` of your config repo, and add a stub line for every
   `!secret` key you use (CI never sees real secrets).
5. (Recommended) Protect `main` in the repo settings: require the
   *HA Config Validation* check.

First run bootstraps the git repo in `/config` (seeding a Home Assistant
`.gitignore` covering `secrets.yaml`, `.storage/`, databases, logs) and pushes
the initial import; every run after that only ever opens PRs.

## Status sensor (optional)

The app writes `.gitops_backup_status` (`status:detail:extra`) to your config
dir. Expose it in HA:

```yaml
command_line:
  - sensor:
      name: GitOps Backup Status
      command: "cat /config/.gitops_backup_status 2>/dev/null || echo 'unknown'"
      scan_interval: 3600
```

## Deploying merged changes back to the box

Merged PRs are pulled into `/config` on the next scheduled run and applied
to the running Home Assistant according to `apply_after_pull` (`reload`
by default, `restart` for `configuration.yaml` / `custom_components/`
changes). To apply immediately, restart the app with `run_at_start: true`.

Do **not** `git reset --hard` in `/config` by hand: any UI edit made since
the last backup is uncommitted drift and would be wiped.

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `github_repo` | — | `owner/repo` to back up into (required) |
| `github_token` | — | Fine-grained PAT (required) |
| `base_branch` | `main` | Branch PRs target |
| `branch_prefix` | `auto-backup` | Prefix for backup branches |
| `interval_hours` | `24` | Hours between runs (1–168) |
| `run_at_start` | `false` | Also run when the app starts |
| `dry_run` | `false` | Log what would change; push nothing |
| `commit_name` / `commit_email` | see config | Commit author |
| `signoff` | `true` | Add `Signed-off-by` (DCO) to commits |
| `apply_after_pull` | `reload` | Apply merged PRs to the running HA: `reload` / `restart` / `off` |

## Security notes

- The token lives in the app options (Supervisor-encrypted), never in git
  and never in `.git/config` — it is passed per network call only, so other
  apps with config access and HA full backups never see it.
- `secrets.yaml` **and any copy of it** (`secrets.yaml.bak-*`, `*.orig`,
  `*.bak`, `zigbee2mqtt/secret.yaml`), `.storage/`, `.cloud/`, keys/tokens,
  databases, logs, and the app's own status/log files are excluded on
  every run via `.git/info/exclude` — not just the seeded `.gitignore` — and
  anything a previous run already committed is untracked automatically.
  (Committing status files creates a PR-per-day feedback loop — learned the
  hard way.)
- While a backup PR is open, new drift is pushed onto that same PR instead of
  opening another one each day.
- CI validates with **stub** secrets; your real `secrets.yaml` never leaves the box.
- Nothing in this app ever force-pushes or writes to your base branch after
  the initial bootstrap import.
- **This is change tracking, not disaster recovery** — pair it with HA's
  native encrypted backups + the Google Drive Backup app for full system
  state (`secrets.yaml`, `.storage/`, databases, apps). See the app
  DOCS for the full "what is backed up where" table and where secrets belong.

## Contributing

Lint locally with the same checks CI runs:

```bash
pip install pre-commit && pre-commit install   # once
pre-commit run --all-files
bash tests/self_check.sh
```

## License

Apache-2.0
