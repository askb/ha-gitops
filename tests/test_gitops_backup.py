# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Anil Belur
"""End-to-end tests for gitops_backup.py against a local bare origin and a
fake GitHub REST API (stdlib http.server). Complements tests/self_check.sh,
which covers the paths that need no API (bootstrap, drift, conflicts,
secrets hygiene); here we exercise PR creation and de-duplication.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

APP = Path(__file__).resolve().parents[1] / "gitops_backup" / "rootfs" / "app" / "gitops_backup.py"
spec = importlib.util.spec_from_file_location("gitops_backup", APP)
assert spec and spec.loader
gb: Any = importlib.util.module_from_spec(spec)
sys.modules["gitops_backup"] = gb
spec.loader.exec_module(gb)


class FakeGitHub(HTTPServer):
    """Records POST /pulls and serves the configured open PR list."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.open_prs: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.url = f"http://127.0.0.1:{self.server_port}"


class _Handler(BaseHTTPRequestHandler):
    server: FakeGitHub

    def log_message(self, *_: object) -> None:  # silence
        pass

    def _send(self, code: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.startswith("/repos/local/test/pulls"):
            self._send(200, self.server.open_prs)
        else:
            self._send(404, {"message": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.server.created.append(payload)
        self._send(201, {"html_url": f"https://github.com/local/test/pull/{len(self.server.created)}"})


@pytest.fixture
def github() -> Any:
    srv = FakeGitHub()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


@pytest.fixture
def env(tmp_path: Path, github: FakeGitHub) -> dict[str, str]:
    """Isolated HOME, config dir, options.json and a bare origin."""
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(tmp_path / "origin.git")], check=True)
    e = {
        **os.environ,
        "HOME": str(tmp_path),
        "OPTIONS": str(tmp_path / "data" / "options.json"),
        "CONFIG_DIR": str(tmp_path / "config"),
        "GITHUB_API_URL": github.url,
    }
    e.pop("SUPERVISOR_TOKEN", None)
    subprocess.run(
        [
            "git",
            "config",
            "--global",
            f"url.{tmp_path}/origin.git.insteadOf",
            "https://github.com/local/test.git",
        ],
        env=e,
        check=True,
    )
    write_options(e)
    (tmp_path / "config" / "configuration.yaml").write_text("a: 1\n")
    return e


def write_options(e: dict[str, str], **override: object) -> None:
    opts: dict[str, object] = {
        "github_repo": "local/test",
        "github_token": "test-token",
        "base_branch": "main",
        "branch_prefix": "auto-backup",
        "dry_run": False,
        "commit_name": "Test",
        "commit_email": "test@localhost",
        "signoff": True,
        **override,
    }
    Path(e["OPTIONS"]).write_text(json.dumps(opts))


def run(e: dict[str, str]) -> int:
    return subprocess.run([sys.executable, str(APP)], env=e, capture_output=True).returncode


def status(e: dict[str, str]) -> str:
    return (Path(e["CONFIG_DIR"]) / ".gitops_backup_status").read_text().strip()


def git(e: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", "-C", e["CONFIG_DIR"], *args], env=e, capture_output=True, text=True, check=True
    ).stdout.strip()


# -- end-to-end -------------------------------------------------------------


def test_drift_opens_pr(env: dict[str, str], github: FakeGitHub) -> None:
    assert run(env) == 0 and status(env).startswith("success:bootstrap:")
    Path(env["CONFIG_DIR"], "automations.yaml").write_text("[]\n")

    assert run(env) == 0
    assert status(env) == "success:pr_created:https://github.com/local/test/pull/1"
    (pr,) = github.created
    assert pr["base"] == "main" and pr["head"].startswith("auto-backup/")
    assert "- A automations.yaml" in pr["body"]
    assert "— 1 changed file(s)" in pr["body"]
    assert git(env, "branch", "--show-current") == "main", "HEAD must stay on base"
    assert "Signed-off-by: Test <test@localhost>" in git(env, "log", "-1", "--format=%B", pr["head"])


def test_open_pr_is_reused_not_duplicated(env: dict[str, str], github: FakeGitHub) -> None:
    run(env)
    Path(env["CONFIG_DIR"], "automations.yaml").write_text("[]\n")
    run(env)
    branch = github.created[0]["head"]
    github.open_prs = [{"head": {"ref": branch, "repo": {"owner": {"login": "local"}}}}]

    # same drift again → nothing to do
    assert run(env) == 0 and status(env) == f"success:pr_open_unchanged:{branch}"

    # more drift → stacked onto the open PR branch, no second PR
    Path(env["CONFIG_DIR"], "scripts.yaml").write_text("{}\n")
    assert run(env) == 0 and status(env) == f"success:pr_updated:{branch}"
    assert len(github.created) == 1
    assert git(env, "rev-list", "--count", f"main..{branch}") == "2"


def test_new_dir_with_secret_counts_files_and_skips_key(env: dict[str, str], github: FakeGitHub) -> None:
    run(env)
    d = Path(env["CONFIG_DIR"], "esphome")
    d.mkdir()
    (d / "garden.yaml").write_text("esphome: {}\n")
    (d / "secrets.yaml").write_text("wifi: hunter2\n")
    (d / ".device-builder-peer-link-key.bin").write_bytes(b"\0" * 32)
    (d / ".device-builder.json").write_text("{}\n")
    (d / "other.yaml").write_text("{}\n")

    assert run(env) == 0 and status(env).startswith("success:pr_created:")
    (pr,) = github.created
    assert "— 2 changed file(s)" in pr["body"], "untracked dirs must be counted per file"
    assert "garden.yaml" in pr["body"] and "other.yaml" in pr["body"]
    for leaked in ("secrets.yaml", "peer-link-key", ".device-builder.json"):
        assert leaked not in pr["body"]
    tracked = git(env, "ls-tree", "-r", "--name-only", pr["head"])
    assert "esphome/garden.yaml" in tracked and "key.bin" not in tracked and "secrets" not in tracked


def test_foreign_open_pr_is_ignored(env: dict[str, str], github: FakeGitHub) -> None:
    run(env)
    github.open_prs = [{"head": {"ref": "auto-backup/x", "repo": {"owner": {"login": "someone-else"}}}}]
    Path(env["CONFIG_DIR"], "automations.yaml").write_text("[]\n")
    assert run(env) == 0 and status(env).startswith("success:pr_created:")


def test_missing_token_is_config_error(env: dict[str, str]) -> None:
    write_options(env, github_token="")
    assert run(env) == 1
    assert status(env) == "error:config:github_repo and github_token are required"


# -- unit -------------------------------------------------------------------


def test_pr_file_list_caps_at_limit(tmp_path: Path) -> None:
    cfg = gb.Config(
        github_repo="o/r", github_token="t", base_branch="main", branch_prefix="p", dry_run=False,
        commit_name="n", commit_email="e", signoff=False, apply_after_pull="off",
        config_dir=tmp_path, api_url="https://api.github.com",
    )  # fmt: skip
    b = gb.Backup(cfg)
    b.git("init", "-q")
    for i in range(gb.PR_FILE_LIST_LIMIT + 3):
        (tmp_path / f"f{i:03}.yaml").write_text("x\n")
    b.git("add", "-A")
    lines = b.pr_file_list().splitlines()
    assert len(lines) == gb.PR_FILE_LIST_LIMIT + 1
    assert lines[0] == "- A f000.yaml" and lines[-1] == "- … and 3 more"


def test_api_url_scheme_is_enforced(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="http"):
        gb.Config(
            github_repo="o/r", github_token="t", base_branch="main", branch_prefix="p", dry_run=False,
            commit_name="n", commit_email="e", signoff=False, apply_after_pull="off",
            config_dir=tmp_path, api_url="file:///etc",
        )  # fmt: skip
