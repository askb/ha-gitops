#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Anil Belur
"""GitOps Config Backup — PR-gated GitHub backup for Home Assistant config.

Workflow (never pushes to the base branch after bootstrap):
  1. Stash any local changes
  2. pull --rebase from origin/<base> to sync upstream changes (merged PRs)
  3. Re-apply stashed changes
  4. If the live config drifted, commit to a dated backup branch + open a PR

Status file (.gitops_backup_status, gitignored) format: <status>:<detail>:<extra>

Stdlib only: the image ships python3 and git, nothing else.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MAX_LOG_LINES = 200
PR_FILE_LIST_LIMIT = 50

# Secrets, credentials, and the app's own runtime metadata — must never be
# committed. Shared by the exclude writer and the untracker. Backup/editor
# copies of secrets.yaml (`secrets.yaml.bak-2026…`, `*.orig`) are the most
# common way a real secret slips into a PR, so they are covered too.
NEVER_COMMIT_PATTERNS = """\
secrets.yaml
secrets.yaml.*
!secrets.yaml.example
!secrets.yaml.sample
!secrets.yaml.template
!secrets.yaml.dist
*.bak
*.bak-*
*.bak.*
*.orig
*.secret
*.key
*.pem
*.token
*-key.bin
esphome/.device-builder*
.google.token
.git-credentials
.cloud/
.storage/
zigbee2mqtt/secret.yaml
.gitops_backup_status
gitops_backup.log
"""

# Content-level backstop for secrets living in files the path patterns above
# do not catch (an inline `password: hunter2`, a pasted PEM block, a GitHub
# PAT in a shell script…). Files matching are left out of the backup commit.
CREDENTIAL_DETECTION_RULES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private key block", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained PAT", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{80,}\b")),
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Slack token", re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("JWT / HA token", re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    (
        "inline credential",
        # key: literal — skips HA `!secret name`, templates, env refs, and
        # placeholders. Only plain literals of 8+ chars trip it.
        re.compile(
            rb"(?im)^\s*[\"']?(?:password|passwd|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|secret[_-]?key|private[_-]?key|bearer)[\"']?\s*[:=]\s*[\"']?(?!!secret\b|\{\{|\$\{?[A-Z_]|<|changeme|your[_-]|example|redacted|xxx|\*\*\*)[^\s\"'#]{8,}"
        ),
    ),
)
CONTENT_SCAN_MAX_BYTES = 1_000_000

SEED_GITIGNORE = """\
# Secrets and credentials — never commit
secrets.yaml
secrets.yaml.*
!secrets.yaml.example
!secrets.yaml.sample
!secrets.yaml.template
!secrets.yaml.dist
*.bak
*.bak-*
*.bak.*
*.orig
*.secret
*.pem
*.key
*.token
*-key.bin
esphome/.device-builder*
.google.token
.git-credentials
.cloud/
.storage/
zigbee2mqtt/secret.yaml

# Runtime state, databases, logs
*.db
*.db-shm
*.db-wal
*.log
*.log.*
ip_bans.yaml
known_devices.yaml
home-assistant_v2.*
OZW_Log.txt
.uuid
.ha_run.lock

# This app's runtime metadata (committing it creates a PR feedback loop)
.gitops_backup_status
gitops_backup.log

# Misc
__pycache__/
deps/
tts/
backups/
"""

EXCLUDE_BEGIN = "# >>> gitops-backup managed >>>"
EXCLUDE_END = "# <<< gitops-backup managed <<<"


@dataclass(frozen=True)
class Config:
    """Add-on options plus the derived GitHub endpoints."""

    github_repo: str
    github_token: str
    base_branch: str
    branch_prefix: str
    dry_run: bool
    commit_name: str
    commit_email: str
    signoff: bool
    apply_after_pull: str
    config_dir: Path
    api_url: str

    @classmethod
    def load(cls, options_path: Path, config_dir: Path) -> Config:
        opts = json.loads(options_path.read_text())
        return cls(
            github_repo=opts.get("github_repo") or "",
            github_token=opts.get("github_token") or "",
            base_branch=opts.get("base_branch") or "main",
            branch_prefix=opts.get("branch_prefix") or "auto-backup",
            dry_run=bool(opts.get("dry_run", False)),
            commit_name=opts.get("commit_name") or "HA GitOps Backup",
            commit_email=opts.get("commit_email") or "gitops-backup@users.noreply.github.com",
            signoff=bool(opts.get("signoff", True)),
            apply_after_pull=opts.get("apply_after_pull") or "reload",
            config_dir=config_dir,
            api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )

    def __post_init__(self) -> None:
        if not self.api_url.startswith(("https://", "http://")):
            raise ValueError(f"GITHUB_API_URL must be http(s): {self.api_url!r}")

    @property
    def remote_url(self) -> str:
        return f"https://github.com/{self.github_repo}.git"

    @property
    def api(self) -> str:
        return f"{self.api_url}/repos/{self.github_repo}"

    @property
    def log_file(self) -> Path:
        return self.config_dir / "gitops_backup.log"

    @property
    def status_file(self) -> Path:
        return self.config_dir / ".gitops_backup_status"


class Backup:
    """One backup run against ``cfg.config_dir``."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Token-less remote: the token is never written to .git/config
        # (readable by every app with config access and captured in HA full
        # backups). Each network call gets it via a one-shot http.extraheader.
        auth = base64.b64encode(f"x-access-token:{cfg.github_token}".encode()).decode()
        self._auth_cfg = ["-c", f"http.extraheader=AUTHORIZATION: basic {auth}"]

    # -- primitives -----------------------------------------------------

    def git(
        self, *args: str, auth: bool = False, check: bool = True, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["git", *(self._auth_cfg if auth else []), *args]
        return subprocess.run(
            cmd, cwd=self.cfg.config_dir, text=True, input=stdin, capture_output=True, check=check
        )

    def out(self, *args: str, **kw: object) -> str:
        return self.git(*args, **kw).stdout.strip()  # type: ignore[arg-type]

    def ok(self, *args: str, auth: bool = False) -> bool:
        return self.git(*args, auth=auth, check=False).returncode == 0

    def log(self, msg: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} - {msg}"
        print(line, flush=True)
        with self.cfg.log_file.open("a") as fh:
            fh.write(line + "\n")

    def write_status(self, status: str, detail: str, extra: str = "") -> None:
        self.cfg.status_file.write_text(f"{status}:{detail}:{extra}\n")
        self.log(f"Status: {status}:{detail}:{extra}")

    def rotate_log(self) -> None:
        if self.cfg.log_file.is_file():
            lines = self.cfg.log_file.read_text().splitlines(keepends=True)
            self.cfg.log_file.write_text("".join(lines[-MAX_LOG_LINES:]))

    def api_call(self, method: str, path: str, body: dict[str, str] | None = None) -> object | None:
        """GitHub REST call; None on any HTTP/network error (callers fail soft)."""
        req = urllib.request.Request(  # noqa: S310 - scheme checked in Config
            self.cfg.api + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"token {self.cfg.github_token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - scheme checked
                data: object = json.load(resp)
                return data
        except (urllib.error.URLError, OSError, ValueError):
            return None

    # -- HA integration -------------------------------------------------

    def ha_apply(self, mode: str) -> None:
        """Apply pulled changes to the running HA: reload|restart|off."""
        service = {"reload": "homeassistant/reload_all", "restart": "homeassistant/restart"}.get(mode)
        if service is None:
            return
        token = os.environ.get("SUPERVISOR_TOKEN")
        name = service.split("/", 1)[1]
        if not token:
            self.log(
                f"⚠ apply_after_pull={mode} but SUPERVISOR_TOKEN unset; skipping — needs homeassistant_api"
            )
            return
        req = urllib.request.Request(
            f"http://supervisor/core/api/services/{service}",
            method="POST",
            data=b"{}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60):  # noqa: S310 - fixed http://supervisor
                pass
            self.log(f"✅ Applied pulled changes: homeassistant.{name}")
        except (urllib.error.URLError, OSError):
            self.log(f"⚠ Failed to call homeassistant.{name}; apply manually in HA")

    # -- secret hygiene -------------------------------------------------

    def seed_gitignore(self) -> None:
        gi = self.cfg.config_dir / ".gitignore"
        if not gi.exists():
            self.log("Seeding recommended Home Assistant .gitignore")
            gi.write_text(SEED_GITIGNORE)

    def write_secret_excludes(self) -> None:
        """Write never-commit patterns to .git/info/exclude (local, un-shared).

        Keeps secrets + app metadata out of backups even when migrating a repo
        that already has its own .gitignore. Called before the stash so
        untracked secrets are never swept in. Idempotent: replaces our block.
        """
        git_dir = self.cfg.config_dir / ".git"
        if not git_dir.is_dir():
            return
        exclude = git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        kept: list[str] = []
        skipping = False
        if exclude.exists():
            for line in exclude.read_text().splitlines():
                if line == EXCLUDE_BEGIN:
                    skipping = True
                elif line == EXCLUDE_END:
                    skipping = False
                elif not skipping:
                    kept.append(line)
        block = f"{EXCLUDE_BEGIN}\n{NEVER_COMMIT_PATTERNS}{EXCLUDE_END}\n"
        exclude.write_text("".join(f"{line}\n" for line in kept) + block)

    def untrack_secrets(self) -> None:
        """Stop tracking any previously-committed secret/credential.

        Called AFTER the upstream sync (not before the stash — a staged deletion
        would be lost in stash/pop) so the removal counts as drift and flows
        out through the next backup PR. Uses git's own matcher so negations
        are honoured exactly like in .gitignore.
        """
        if not (self.cfg.config_dir / ".git").is_dir():
            return
        with tempfile.NamedTemporaryFile("w", suffix=".patterns", delete=False) as fh:
            fh.write(NEVER_COMMIT_PATTERNS)
            patfile = fh.name
        try:
            listed = self.out("ls-files", "-z", "-c", "-i", f"--exclude-from={patfile}")
        finally:
            os.unlink(patfile)
        for path in filter(None, listed.split("\0")):
            self.log(f"⚠ Untracking previously-committed sensitive path: {path}")
            self.git("rm", "-q", "--cached", "--", path)

    # -- repo lifecycle -------------------------------------------------

    def bootstrap_if_needed(self) -> bool:
        """Return True if bootstrap ran (caller should stop)."""
        cfg = self.cfg
        if (cfg.config_dir / ".git").is_dir():
            return False
        self.log(f"No git repo in {cfg.config_dir} — bootstrapping")
        self.log(
            "⚠ This app versions DECLARATIVE config only. It does NOT back up "
            "secrets.yaml, .storage/ (UI-configured integrations, logins, "
            "device/entity registries, dashboards), the database, or app data — "
            "those are excluded on purpose. For a 100% restore you MUST also run "
            "HA full backups (e.g. the Google Drive Backup app). See this app's "
            "DOCS → 'What is backed up where'."
        )
        if cfg.dry_run:
            self.seed_gitignore()
            self.log(f"DRY RUN: would git init, commit initial import, push to {cfg.base_branch}")
            self.write_status("success", "dry_run", "bootstrap")
            return True
        self.git("init", "-q", "-b", cfg.base_branch)
        self.seed_gitignore()
        self.git("add", "-A")
        self.git(
            "commit",
            "-q",
            *(["-s"] if cfg.signoff else []),
            "-m",
            "chore: initial Home Assistant config import",
        )
        self.git("remote", "add", "origin", cfg.remote_url)
        # Push base branch only on bootstrap; every later change goes through a PR
        self.git("push", "-q", "-u", "origin", cfg.base_branch, auth=True)
        self.write_status("success", "bootstrap", f"initial import pushed to {cfg.base_branch}")
        self.log("Bootstrap complete")
        return True

    def cleanup_branch(self) -> None:
        """Point HEAD back at the base branch WITHOUT touching the working tree.

        A plain `git checkout` would rewrite /config to the pre-backup base
        branch, rolling the live config back until the next run pulls the merged
        PR (and losing the drift for good if the PR is never merged). The drift
        stays as uncommitted changes and is reconciled by the stash/pull/pop on
        the next run.
        """
        current = self.out("branch", "--show-current", check=False)
        if current and current != self.cfg.base_branch:
            self.ok("symbolic-ref", "HEAD", f"refs/heads/{self.cfg.base_branch}")
            self.ok("reset", "-q")

    # -- PR helpers -----------------------------------------------------

    def find_open_backup_pr(self) -> str:
        """Head branch of the newest open PR from this app, or '' (→ open a new PR)."""
        owner = self.cfg.github_repo.split("/", 1)[0]
        prefix = f"{self.cfg.branch_prefix}/"
        prs = self.api_call(
            "GET", f"/pulls?state=open&base={self.cfg.base_branch}&sort=created&direction=desc&per_page=100"
        )
        if not isinstance(prs, list):
            return ""
        for pr in prs:
            try:
                head = pr["head"]
                if head["repo"]["owner"]["login"] == owner and head["ref"].startswith(prefix):
                    return str(head["ref"])
            except (KeyError, TypeError):
                continue
        return ""

    def create_pull_request(self, branch: str, title: str, body: str) -> str:
        resp = self.api_call(
            "POST", "/pulls", {"title": title, "body": body, "head": branch, "base": self.cfg.base_branch}
        )
        return str(resp.get("html_url") or "") if isinstance(resp, dict) else ""

    def pr_file_list(self) -> str:
        """Staged changes as a bullet list, capped for the commit body / PR."""
        rows = [r for r in self.out("diff", "--cached", "--name-status").splitlines() if r]
        lines = [f"- {' '.join(r.split(maxsplit=2)[:2])}" for r in rows[:PR_FILE_LIST_LIMIT]]
        if len(rows) > PR_FILE_LIST_LIMIT:
            lines.append(f"- … and {len(rows) - PR_FILE_LIST_LIMIT} more")
        return "\n".join(lines)

    def scan_staged_for_secrets(self) -> list[str]:
        """Unstage staged files whose *content* looks like a secret.

        Returns the paths held back. Path-based exclusion (NEVER_COMMIT_PATTERNS)
        runs first; this catches what it cannot know about — a PEM pasted into
        a shell script, an inline `password:` literal, a token in a YAML file.
        """
        held: list[str] = []
        staged = self.out("diff", "--cached", "--name-only", "--diff-filter=AM", "-z")
        for rel in filter(None, staged.split("\0")):
            path = self.cfg.config_dir / rel
            try:
                if path.stat().st_size > CONTENT_SCAN_MAX_BYTES:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8000]:
                continue  # binary; path patterns cover key blobs
            rule_idx = next(
                (i for i, (_, rx) in enumerate(CREDENTIAL_DETECTION_RULES) if rx.search(data)), -1
            )
            if rule_idx < 0:
                continue
            # Only the rule *label* and path are logged — never the match.
            label = CREDENTIAL_DETECTION_RULES[rule_idx][0]
            self.log(f"⚠ Secret-like content ({label}) in {rel} — held back from backup")
            self.git("reset", "-q", "--", rel)
            held.append(rel)
        return held

    # -- main flow ------------------------------------------------------

    def run(self) -> int:
        cfg = self.cfg
        if not cfg.github_repo or not cfg.github_token:
            self.write_status("error", "config", "github_repo and github_token are required")
            return 1

        self.git("config", "--global", "--add", "safe.directory", str(cfg.config_dir))
        self.git("config", "--global", "user.name", cfg.commit_name)
        self.git("config", "--global", "user.email", cfg.commit_email)

        if self.bootstrap_if_needed():
            self.rotate_log()
            return 0
        self.git("remote", "set-url", "origin", cfg.remote_url)
        self.seed_gitignore()
        self.write_secret_excludes()
        try:
            return self._sync_and_backup()
        finally:
            self.cleanup_branch()
            self.rotate_log()

    def _sync_and_backup(self) -> int:
        cfg = self.cfg
        # Unmerged index entries left by an older version after a stash conflict
        # make every git write fail. Drop them; the working tree is untouched.
        if self.out("ls-files", "-u"):
            self.log("Clearing unmerged index entries left by an earlier stash conflict")
            self.git("reset", "-q")

        # 1–2. Stash local drift, sync upstream (merged PRs flow back here)
        stashed = bool(self.out("status", "--porcelain"))
        if stashed:
            self.git("stash", "-q", "--include-untracked")
        pre_sync = self.out("rev-parse", "HEAD")
        if not self.ok("pull", "-q", "--rebase", "origin", cfg.base_branch, auth=True):
            self.ok("rebase", "--abort")
            if stashed:
                self.ok("stash", "pop", "-q")
            self.write_status("error", "pull_rebase_failed", f"resolve manually in {cfg.config_dir}")
            return 1
        post_sync = self.out("rev-parse", "HEAD")

        # 3. Re-apply drift
        if stashed and not self.ok("stash", "pop", "-q"):
            self.ok("checkout", "-q", "--theirs", ".")
            # checkout --theirs only rewrites the files: the index still holds
            # the conflict, which blocks the next run's stash. Unstage everything
            # so the kept live files are plain drift for the backup below.
            self.git("reset", "-q")
            self.ok("stash", "drop", "-q")
            self.write_status("warning", "stash_conflict", "kept live config where conflicting")

        # 3b. Upstream advanced (a merged PR came down) → apply it to running HA.
        if pre_sync != post_sync and not cfg.dry_run:
            self.log(f"Upstream sync: {pre_sync[:7]} → {post_sync[:7]} — applying ({cfg.apply_after_pull})")
            self.ha_apply(cfg.apply_after_pull)

        self.untrack_secrets()

        # 4. Anything to back up?
        porcelain = self.out("status", "--porcelain", "-uall")
        if not porcelain:
            self.write_status("success", "no_changes", "")
            self.log("No changes to back up")
            return 0
        changed_count = len(porcelain.splitlines())
        branch_name = f"{cfg.branch_prefix}/{datetime.now():%Y-%m-%d-%H%M%S}"

        if cfg.dry_run:
            self.log(f"DRY RUN: {changed_count} changed file(s) would go to {branch_name}:")
            self.log(porcelain)
            self.write_status("success", "dry_run", f"{changed_count} files")
            return 0

        # Build the backup commit from the index without switching branches, so
        # the live config is never touched. If an earlier backup PR is still
        # open, stack the new drift onto it instead of opening a near-identical
        # PR every run.
        self.git("add", "-A")
        held = self.scan_staged_for_secrets()
        changed_count -= len(held)
        if not self.out("diff", "--cached", "--name-only"):
            self.write_status("warning", "secrets_held", ",".join(held))
            self.log("Nothing left to back up after holding back secret-like files")
            return 0
        tree = self.out("write-tree")
        files = self.pr_file_list()
        if held:
            files += "\n\n⚠ Held back (secret-like content — fix with `!secret` or add to .gitignore):\n"
            files += "\n".join(f"- {h}" for h in held)
        stamp = f"{datetime.now():%Y-%m-%d %H:%M}"
        msg = f"chore: auto backup {stamp} ({changed_count} files)\n\n{files}"
        if cfg.signoff:
            msg += f"\n\nSigned-off-by: {cfg.commit_name} <{cfg.commit_email}>"

        existing_branch = self.find_open_backup_pr()
        parents = ["-p", "HEAD"]
        if existing_branch and self.ok("fetch", "-q", "origin", f"refs/heads/{existing_branch}", auth=True):
            existing_head = self.out("rev-parse", "FETCH_HEAD")
            if self.out("rev-parse", f"{existing_head}^{{tree}}") == tree:
                self.git("reset", "-q")
                self.write_status("success", "pr_open_unchanged", existing_branch)
                self.log(f"No new drift since open PR on {existing_branch}")
                return 0
            branch_name = existing_branch
            parents = ["-p", existing_head]
            # Base advanced since that PR was opened → make it a merge so the
            # PR diff stays limited to the drift.
            if not self.ok("merge-base", "--is-ancestor", "HEAD", existing_head):
                parents += ["-p", "HEAD"]
        else:
            existing_branch = ""

        new_commit = self.out("commit-tree", tree, *parents, stdin=msg + "\n")
        self.git("update-ref", f"refs/heads/{branch_name}", new_commit)
        self.git("reset", "-q")
        self.git("push", "-q", "origin", f"refs/heads/{branch_name}:refs/heads/{branch_name}", auth=True)

        if branch_name == existing_branch:
            self.write_status("success", "pr_updated", branch_name)
            self.log(f"✅ Backup complete — pushed {changed_count} file(s) onto open PR branch {branch_name}")
            return 0

        pr_url = self.create_pull_request(
            branch_name,
            f"chore: auto backup {stamp}",
            f"Automated Home Assistant config backup — {changed_count} changed file(s). "
            f"Opened by the GitOps Config Backup app; review and merge.\n\n{files}",
        )
        if pr_url:
            self.write_status("success", "pr_created", pr_url)
            self.log(f"✅ Backup complete — PR opened: {pr_url}")
        else:
            self.write_status("warning", "branch_pushed", branch_name)
            self.log("WARNING: branch pushed but PR creation failed")
        return 0


def main() -> int:
    cfg = Config.load(
        Path(os.environ.get("OPTIONS", "/data/options.json")),
        Path(os.environ.get("CONFIG_DIR", "/homeassistant")),
    )
    os.chdir(cfg.config_dir)
    try:
        return Backup(cfg).run()
    except subprocess.CalledProcessError as err:
        # Mirror `set -e`: an unexpected git failure aborts the run loudly.
        sys.stderr.write(f"git {' '.join(err.cmd[1:3])} failed: {err.stderr}\n")
        return err.returncode or 1


if __name__ == "__main__":
    sys.exit(main())
