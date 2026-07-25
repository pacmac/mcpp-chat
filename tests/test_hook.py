"""Hook tests: what a session is told, and the guarantees around failure."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import MODULE_DIR, call

HOOK = MODULE_DIR / "hook.py"


def run_hook(event: str, cwd: Path, db: Path, payload: dict | None = None) -> tuple[int, str]:
    body = dict(payload or {})
    body.setdefault("cwd", str(cwd))
    env = {**os.environ, "MCPP_CHAT_DB": str(db)}
    proc = subprocess.run(
        [sys.executable, str(HOOK), "--event", event],
        input=json.dumps(body), capture_output=True, text=True, env=env,
    )
    return proc.returncode, proc.stdout.strip()


@pytest.fixture()
def chatting(tool, repos, tmp_path):
    """node-dash has asked mesh-gw a question and is waiting."""
    gw, dash = repos
    call(tool, "chat_whoami", {}, gw)          # both repos must exist as parties
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "gw--dash", "parties": ["mesh-gw"]}, dash)
    call(tool, "chat_ask", {
        "title": "Is the chunk header enum closed?",
        "body": "I need FRAME_AUX; can your generator emit it?",
        "slug": "chunk-api",
    }, dash)
    return gw, dash, tmp_path / "chat.db"


def test_silent_when_repo_never_chatted(tool, tmp_path, repos):
    gw, _dash = repos
    call(tool, "chat_whoami", {}, gw)          # db exists, but no channels
    stranger = tmp_path / "unrelated-repo"
    stranger.mkdir()
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        rc, out = run_hook(event, stranger, tmp_path / "chat.db")
        assert rc == 0 and out == "", f"{event} should be silent"


def test_silent_when_nothing_pending(tool, repos, tmp_path):
    gw, dash = repos
    call(tool, "chat_whoami", {}, dash)
    call(tool, "chat_channel_new", {"name": "link", "parties": ["mesh-gw"]}, dash)
    rc, out = run_hook("Stop", gw, tmp_path / "chat.db")
    assert rc == 0 and out == ""


def test_missing_database_is_silent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    rc, out = run_hook("Stop", repo, tmp_path / "nonexistent.db")
    assert rc == 0 and out == ""


def test_corrupt_database_is_silent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    junk = tmp_path / "junk.db"
    junk.write_text("this is not a database", encoding="utf-8")
    rc, out = run_hook("Stop", repo, junk)
    assert rc == 0 and out == ""


def test_malformed_stdin_is_silent(tmp_path):
    env = {**os.environ, "MCPP_CHAT_DB": str(tmp_path / "chat.db")}
    proc = subprocess.run(
        [sys.executable, str(HOOK), "--event", "Stop"],
        input="{not json at all", capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_session_start_reports_pending_work(chatting):
    gw, _dash, db = chatting
    rc, out = run_hook("SessionStart", gw, db)
    assert rc == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "mesh-gw" in ctx
    assert "chunk-api" in ctx and "needs your ack" in ctx
    assert "FRAME_AUX" in ctx           # the peer's actual words, not just a count


def test_user_prompt_submit_uses_its_own_event_name(chatting):
    gw, _dash, db = chatting
    _rc, out = run_hook("UserPromptSubmit", gw, db)
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_stop_blocks_and_says_what_to_do(chatting):
    gw, _dash, db = chatting
    rc, out = run_hook("Stop", gw, db)
    assert rc == 0
    data = json.loads(out)
    assert data["decision"] == "block"
    assert "chunk-api" in data["reason"]
    assert "chat_inbox" in data["reason"] and "chat_ack" in data["reason"]
    assert "own repo" in data["reason"]


def test_stop_does_not_block_twice(chatting):
    gw, _dash, db = chatting
    rc, out = run_hook("Stop", gw, db, payload={"stop_hook_active": True})
    assert rc == 0 and out == ""


def test_hook_never_advances_the_cursor(tool, chatting):
    """Showing a message must not consume it — the agent still has to read it."""
    gw, _dash, db = chatting
    run_hook("SessionStart", gw, db)
    run_hook("Stop", gw, db)
    read = call(tool, "chat_read", {}, gw)
    assert any(m.get("slug") == "chunk-api" for m in read["result"]["messages"])


def test_hook_writes_nothing_at_all(tool, chatting):
    gw, _dash, db = chatting
    before = db.read_bytes()
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        run_hook(event, gw, db)
    assert db.read_bytes() == before


def test_author_is_not_told_to_ack_their_own_item(chatting):
    _gw, dash, db = chatting
    rc, out = run_hook("Stop", dash, db)
    # node-dash raised it, so nothing is waiting on node-dash yet.
    assert rc == 0 and out == ""


def test_prune_ok_is_surfaced_after_resolve(tool, chatting):
    gw, dash, db = chatting
    call(tool, "chat_ack", {"item": "chunk-api"}, gw)
    call(tool, "chat_reply", {"item": "chunk-api", "text": "no, it is open"}, gw)
    call(tool, "chat_resolve", {"item": "chunk-api"}, dash)
    call(tool, "chat_read", {}, gw)                     # clear unread chatter
    rc, out = run_hook("Stop", gw, db)
    assert rc == 0
    assert "prune-ok" in json.loads(out)["reason"]


def test_unknown_event_prints_plain_text(chatting):
    gw, _dash, db = chatting
    rc, out = run_hook("SomethingElse", gw, db)
    assert rc == 0 and "chunk-api" in out and not out.startswith("{")
