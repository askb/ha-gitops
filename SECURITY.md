<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2026 Anil Belur -->
# Security policy

## Supported versions

Only the latest release in the App store is supported. Update via
Settings → Apps → GitOps Config Backup.

## Reporting a vulnerability

Use GitHub's private reporting:
<https://github.com/askb/ha-gitops/security/advisories/new>. Please do
not open a public issue for anything that could expose a user's GitHub
token or `secrets.yaml`. You should hear back within 7 days.

## What the app protects

- The GitHub token is read from Supervisor-encrypted options and passed
  per git/API call via `http.extraheader`; it is never written to
  `.git/config` or the log.
- `secrets.yaml` (and copies), `.storage/`, `.cloud/`, keys, tokens and
  the ESPHome Device Builder state are excluded through
  `.git/info/exclude` on every run and untracked if already committed.
  See `gitops_backup/DOCS.md` for the full list.

## Automated checks

CI runs under harden-runner with SHA-pinned actions; zizmor audits the
workflows on every PR, CodeQL scans the Python and workflow code, Trivy
scans the built image, and OpenSSF Scorecard runs weekly. Results are in
the repository's Security tab.
