# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Anil Belur
"""End-to-end tests for gitops_backup.py against a local bare origin and a
fake GitHub REST API (stdlib http.server). Complements tests/self_check.sh,
which covers the paths that need no API (bootstrap, drift, conflicts,
secrets hygiene); here we exercise PR creation and de-duplication.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import socketserver
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


def test_base_advanced_under_open_pr_gets_merge_parent(env: dict[str, str], github: FakeGitHub) -> None:
    run(env)
    Path(env["CONFIG_DIR"], "automations.yaml").write_text("[]\n")
    run(env)
    branch = github.created[0]["head"]
    github.open_prs = [{"head": {"ref": branch, "repo": {"owner": {"login": "local"}}}}]

    # someone merges something else to main upstream
    up = Path(env["HOME"], "upstream")
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(Path(env["HOME"], "origin.git")), str(up)],
        env=env,
        check=True,
    )
    (up / "upstream.yaml").write_text("x: 1\n")
    for args in (
        ["add", "-A"],
        ["-c", "user.name=U", "-c", "user.email=u@l", "commit", "-qm", "up"],
        ["push", "-q"],
    ):
        subprocess.run(["git", "-C", str(up), *args], env=env, check=True)

    Path(env["CONFIG_DIR"], "scripts.yaml").write_text("{}\n")
    assert run(env) == 0 and status(env) == f"success:pr_updated:{branch}"
    parents = git(env, "log", "-1", "--format=%P", branch).split()
    assert len(parents) == 2, "stacked commit must merge the advanced base so the PR stays mergeable"
    assert Path(env["CONFIG_DIR"], "upstream.yaml").exists(), "upstream change pulled to the box"
    assert "upstream.yaml" in git(env, "ls-tree", "-r", "--name-only", branch)


def test_dry_run_reports_without_pushing(env: dict[str, str], github: FakeGitHub) -> None:
    run(env)
    write_options(env, dry_run=True)
    Path(env["CONFIG_DIR"], "automations.yaml").write_text("[]\n")
    assert run(env) == 0 and status(env) == "success:dry_run:1 files"
    assert github.created == []
    assert git(env, "branch", "--list", "auto-backup/*") == ""
    assert "DRY RUN: 1 changed file(s)" in Path(env["CONFIG_DIR"], "gitops_backup.log").read_text()


def test_malformed_pr_list_is_tolerated(env: dict[str, str], github: FakeGitHub) -> None:
    run(env)
    github.open_prs = [{"head": None}, {"nope": 1}, {"head": {"ref": "x"}}]
    Path(env["CONFIG_DIR"], "automations.yaml").write_text("[]\n")
    assert run(env) == 0 and status(env).startswith("success:pr_created:")


def test_unreachable_origin_exits_nonzero(env: dict[str, str]) -> None:
    subprocess.run(
        [
            "git",
            "config",
            "--global",
            "url.https://github.com/local/test.git.insteadOf",
            "https://github.com/local/test.git",
        ],
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--global", "--unset", f"url.{env['HOME']}/origin.git.insteadOf"],
        env=env,
        check=True,
    )
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run([sys.executable, str(APP)], env=env, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "failed" in proc.stderr
    assert "test-token" not in proc.stderr + proc.stdout, "token must never be echoed"


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


def test_secret_content_held_back_clean_files_ship(env: dict[str, str], github: FakeGitHub) -> None:
    run(env)
    cfg = Path(env["CONFIG_DIR"])
    (cfg / "clean.yaml").write_text("mqtt:\n  password: !secret mqtt_pw\n  user: '{{ states.x }}'\n")
    (cfg / "leaky.yaml").write_text("rest:\n  api_key: sk_live_0123456789abcdef\n")
    (cfg / "deploy.sh").write_text("#!/bin/sh\nexport GH=ghp_" + "A" * 36 + "\n")
    pem_header = (
        "-----BEGIN RSA " + "PRIVATE KEY-----"
    )  # split so pre-commit's detect-private-key ignores this file
    (cfg / "cert.yaml").write_text(f"tls: |\n  {pem_header}\n  MIIE\n")

    assert run(env) == 0 and status(env).startswith("success:pr_created:")
    (pr,) = github.created
    assert "— 1 changed file(s)" in pr["body"]
    assert "Held back" in pr["body"]
    for held in ("leaky.yaml", "deploy.sh", "cert.yaml"):
        assert f"- {held}" in pr["body"]
    tracked = git(env, "ls-tree", "-r", "--name-only", pr["head"])
    assert "clean.yaml" in tracked
    assert not any(h in tracked for h in ("leaky", "deploy", "cert"))
    assert "sk_live" not in pr["body"] and "ghp_" not in pr["body"]
    # still present in the live config — the tool never deletes user files
    assert (cfg / "leaky.yaml").exists()


def test_only_secret_files_changed_yields_warning_not_pr(env: dict[str, str], github: FakeGitHub) -> None:
    run(env)
    Path(env["CONFIG_DIR"], "token.yaml").write_text("x:\n  access_token: abcdefghijklmnop\n")
    assert run(env) == 0
    assert status(env) == "warning:secrets_held:token.yaml"
    assert github.created == []


# -- applying a merged PR to the running Home Assistant ----------------------


class FakeHA(socketserver.ThreadingTCPServer):
    """Supervisor's Core proxy: the REST service call plus the WebSocket API.

    Like Home Assistant, it only "re-reads" a YAML dashboard when asked for
    it, so the test can assert exactly which dashboards the app asked for.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _HAHandler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        self.services: list[str] = []
        self.tokens: list[str] = []
        self.fetched: list[str | None] = []
        self.pongs = 0
        self.accept_auth = True


class _HAHandler(socketserver.StreamRequestHandler):
    server: FakeHA

    def handle(self) -> None:
        request = self.rfile.readline().decode()
        headers: dict[str, str] = {}
        while line := self.rfile.readline().decode().strip():
            key, _, value = line.partition(":")
            headers[key.lower()] = value.strip()
        if headers.get("upgrade") == "websocket":
            self._websocket(headers["sec-websocket-key"])
            return
        self.rfile.read(int(headers.get("content-length", "0")))
        self.server.services.append(request.split()[1])
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n[]")

    def _send(self, payload: bytes, opcode: int = 0x1, fin: bool = True) -> None:
        n = len(payload)
        if n < 126:
            size = bytes([n])
        elif n < 65536:
            size = bytes([126]) + n.to_bytes(2, "big")
        else:
            size = bytes([127]) + n.to_bytes(8, "big")
        self.wfile.write(bytes([(0x80 if fin else 0) | opcode]) + size + payload)

    def _recv(self) -> dict[str, Any] | None:
        head = self.rfile.read(2)
        if len(head) < 2 or head[0] & 0x0F == 0x8:
            return None
        n = head[1] & 0x7F
        if n == 126:
            n = int.from_bytes(self.rfile.read(2), "big")
        mask = self.rfile.read(4)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(n)))
        if head[0] & 0x0F == 0xA:
            self.server.pongs += 1
            return self._recv()
        msg: dict[str, Any] = json.loads(data)
        return msg

    def _websocket(self, key: str) -> None:
        guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        accept = base64.b64encode(hashlib.sha1((key + guid).encode()).digest()).decode()  # noqa: S324
        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )
        self._send(b'{"type": "auth_required"}')
        auth = self._recv()
        assert auth is not None
        self.server.tokens.append(auth["access_token"])
        if not self.server.accept_auth:
            self._send(b'{"type": "auth_invalid"}')
            return
        self._send(b'{"type": "auth_ok"}')
        while msg := self._recv():
            if msg["type"] == "lovelace/dashboards/list":
                result: object = [
                    {"url_path": "lovelace-energy", "mode": "yaml"},
                    {"url_path": "garden", "mode": "storage"},
                ]
            else:
                self.server.fetched.append(msg.get("url_path"))
                result = {"views": [{"title": "x" * 70000}]}  # > 64 KiB: 64-bit frame length
            body = json.dumps({"id": msg["id"], "type": "result", "success": True, "result": result}).encode()
            self._send(b"keepalive", opcode=0x9)  # ping in the middle of the conversation
            self._send(body[:10], fin=False)  # reply split across two frames
            self._send(body[10:], opcode=0x0)


HA_TOKEN = "t" * 130  # real Supervisor tokens are > 125 bytes: needs the 16-bit frame length


@pytest.fixture
def ha(env: dict[str, str]) -> Any:
    srv = FakeHA()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env["SUPERVISOR_URL"] = srv.url
    env["SUPERVISOR_TOKEN"] = HA_TOKEN
    yield srv
    srv.shutdown()
    srv.server_close()


def merge_upstream(e: dict[str, str], name: str, content: str) -> str:
    """Land a commit on origin/main the way a merged PR does; returns its short SHA."""
    up = Path(e["HOME"], "upstream")
    if not up.exists():
        origin = str(Path(e["HOME"], "origin.git"))
        subprocess.run(["git", "clone", "-q", "-b", "main", origin, str(up)], env=e, check=True)
    (up / name).parent.mkdir(parents=True, exist_ok=True)
    (up / name).write_text(content)
    for args in (
        ["pull", "-q", "--rebase"],
        ["add", "-A"],
        ["-c", "user.name=U", "-c", "user.email=u@l", "commit", "-qm", "merged PR"],
        ["push", "-q"],
    ):
        subprocess.run(["git", "-C", str(up), *args], env=e, check=True)
    return subprocess.run(
        ["git", "-C", str(up), "rev-parse", "--short=7", "HEAD"], env=e, capture_output=True, text=True
    ).stdout.strip()


def test_merged_dashboard_change_is_pushed_to_open_sessions(env: dict[str, str], ha: FakeHA) -> None:
    run(env)
    sha = merge_upstream(env, "dashboards/energy.yaml", "title: Energy\n")

    assert run(env) == 0
    assert ha.services == ["/core/api/services/homeassistant/reload_all"]
    assert ha.tokens == [HA_TOKEN]
    assert ha.fetched == [None, "lovelace-energy"], "default + YAML dashboards only, never storage ones"
    assert ha.pongs == 3, "every ping must be answered"
    assert status(env) == f"success:no_changes:applied {sha}", "status must say a merge was applied"


def test_merge_without_yaml_changes_leaves_dashboards_alone(env: dict[str, str], ha: FakeHA) -> None:
    run(env)
    merge_upstream(env, "python_scripts/x.py", "x = 1\n")

    assert run(env) == 0
    assert ha.services == ["/core/api/services/homeassistant/reload_all"]
    assert ha.tokens == [] and ha.fetched == []


def test_dashboard_refresh_failure_does_not_fail_the_run(env: dict[str, str], ha: FakeHA) -> None:
    run(env)
    ha.accept_auth = False
    merge_upstream(env, "dashboards/energy.yaml", "title: Energy\n")

    assert run(env) == 0 and status(env).startswith("success:no_changes:applied ")
    assert ha.fetched == []
    assert "Could not refresh open dashboards" in Path(env["CONFIG_DIR"], "gitops_backup.log").read_text()


def test_no_merge_means_no_home_assistant_calls(env: dict[str, str], ha: FakeHA) -> None:
    run(env)

    assert run(env) == 0 and status(env) == "success:no_changes:"
    assert ha.services == [] and ha.tokens == []
