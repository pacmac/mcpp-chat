"""The mcpp front end over the board.

The test that matters is test_party_comes_from_workspace_not_cwd. Under mcpp the
process cwd is the SERVER's, fixed at startup, so a party read from cwd would be
the same wrong repo for every session on the host.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(tmp_path):
    """Fresh copies of both modules, bound to a throwaway board."""
    for name in ("xsession_board", "mcpptool"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("mcpptool", REPO / "mcpptool.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mcpptool"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tool(tmp_path, monkeypatch):
    monkeypatch.setenv("XSESSION_DIR", str(tmp_path / "board"))
    monkeypatch.delenv("XSESSION_PARTY", raising=False)
    return _load(tmp_path)


@pytest.fixture()
def repos(tmp_path):
    """Two workspace directories, so the party is a real directory name."""
    gw, dash = tmp_path / "gw", tmp_path / "dash"
    gw.mkdir()
    dash.mkdir()
    return {"gw": {"workspace_dir": str(gw)}, "dash": {"workspace_dir": str(dash)}}


def call(tool, name, args, ctx):
    return tool.execute(name, args, ctx)


@pytest.fixture()
def link(tool, repos):
    """A channel with gw and dash seated, created the way an agent would."""
    r = call(tool, "xs_channel_init", {"channel": "gw--dash", "parties": ["dash"]}, repos["gw"])
    assert r["success"], r
    assert r["result"]["parties"] == ["dash", "gw"]
    return "gw--dash"


# ── Identity ──

def test_party_comes_from_workspace_not_cwd(tool, repos, link, tmp_path, monkeypatch):
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    r = call(tool, "xs_say", {"text": "hello"}, repos["gw"])

    assert r["success"], r
    assert r["result"]["from"] == "gw"


def test_explicit_party_wins(tool, repos, link):
    r = call(tool, "xs_say", {"text": "hi", "party": "dash"}, repos["gw"])
    assert r["result"]["from"] == "dash"


def test_env_party_overrides_workspace(tool, repos, link, monkeypatch):
    monkeypatch.setenv("XSESSION_PARTY", "dash")
    r = call(tool, "xs_say", {"text": "hi"}, repos["gw"])
    assert r["result"]["from"] == "dash"


# ── Items ──

def test_ask_then_peer_reply_moves_open_to_ack(tool, repos, link):
    raised = call(tool, "xs_ask", {"title": "VERIFY the wire schema", "slug": "wire"}, repos["gw"])
    assert raised["result"]["status"] == "OPEN"

    answered = call(tool, "xs_reply", {"slug": "wire", "text": "v4, bumped today"}, repos["dash"])

    assert answered["success"], answered
    assert answered["result"]["status"] == "ACK"


def test_only_the_raiser_may_resolve(tool, repos, link):
    call(tool, "xs_ask", {"title": "GAP no cursor", "slug": "cursor"}, repos["gw"])

    refused = call(tool, "xs_resolve", {"slug": "cursor"}, repos["dash"])

    assert refused["success"] is False
    assert "only gw raised" in refused["error"]

    assert call(tool, "xs_resolve", {"slug": "cursor"}, repos["gw"])["result"]["status"] == "DONE"


def test_prune_ok_is_only_ever_for_yourself(tool, repos, link):
    call(tool, "xs_ask", {"title": "TIDY the log", "slug": "log"}, repos["gw"])
    call(tool, "xs_resolve", {"slug": "log"}, repos["gw"])          # raiser prune-oks itself

    assert call(tool, "xs_archive", {}, repos["gw"])["result"]["archived"] == []

    r = call(tool, "xs_prune_ok", {"slug": "log"}, repos["dash"])
    assert r["result"]["unanimous"] is True

    assert call(tool, "xs_archive", {}, repos["gw"])["result"]["archived"] == ["gw--dash:log"]


def test_reply_to_unknown_item_is_an_error_not_a_traceback(tool, repos, link):
    r = call(tool, "xs_reply", {"slug": "nope", "text": "?"}, repos["dash"])
    assert r["success"] is False
    assert r["error"] == "no item [nope]"


# ── Reading ──

def test_read_consumes_and_peek_does_not(tool, repos, link):
    call(tool, "xs_say", {"text": "one"}, repos["gw"])

    peeked = call(tool, "xs_read", {"peek": True}, repos["dash"])
    assert peeked["result"]["count"] == 1

    first = call(tool, "xs_read", {}, repos["dash"])
    assert first["result"]["count"] == 1
    assert "one" in first["display"]

    second = call(tool, "xs_read", {}, repos["dash"])
    assert second["result"]["count"] == 0
    assert second["display"] == "nothing new."


def test_you_never_read_your_own_post(tool, repos, link):
    call(tool, "xs_say", {"text": "mine"}, repos["gw"])
    assert call(tool, "xs_read", {}, repos["gw"])["result"]["count"] == 0


# ── Two channels ──

def test_inbox_covers_every_channel_and_posting_demands_one(tool, repos, link, tmp_path):
    other = tmp_path / "radio"
    other.mkdir()
    call(tool, "xs_channel_init", {"channel": "gw--radio", "parties": ["radio"]}, repos["gw"])

    call(tool, "xs_say", {"text": "from dash"}, repos["dash"])
    call(tool, "xs_say", {"text": "from radio"}, {"workspace_dir": str(other)})

    box = call(tool, "xs_inbox", {}, repos["gw"])
    assert box["result"]["unread_total"] == 2
    assert sorted(c["channel"] for c in box["result"]["channels"]) == ["gw--dash", "gw--radio"]

    ambiguous = call(tool, "xs_say", {"text": "which one?"}, repos["gw"])
    assert ambiguous["success"] is False
    assert "say which channel" in ambiguous["error"]

    ok = call(tool, "xs_say", {"text": "this one", "channel": "gw--radio"}, repos["gw"])
    assert ok["success"] and ok["result"]["channel"] == "gw--radio"


def test_inbox_reports_what_waits_on_me(tool, repos, link):
    call(tool, "xs_ask", {"title": "DECIDE who owns retries", "slug": "retries"}, repos["gw"])

    box = call(tool, "xs_inbox", {}, repos["dash"])["result"]

    assert [w["item"] for w in box["waiting_on_me"]] == ["retries"]
    assert box["waiting_on_peer"] == []


# ── Seating ──

def test_unseated_party_is_told_so(tool, repos):
    """inbox answers, read refuses — the engine's own split, not a tidier one.

    An unseated party asking what is waiting has asked a fair question and gets
    the answer plus how to fix it. Anything that would consume or post has
    nowhere to do it and says so.
    """
    box = call(tool, "xs_inbox", {}, repos["gw"])
    assert box["success"] is True
    assert box["result"]["channels"] == []
    assert "in no channels" in box["display"]
    # ...and is told how to fix it in tools, not in shell. fmt_inbox is shared
    # with the CLI, so this substitution is the tool's job.
    assert "xs_channel_init" in box["display"]
    assert "xs init" not in box["display"]

    for name in ("xs_read", "xs_archive"):
        r = call(tool, name, {}, repos["gw"])
        assert r["success"] is False, name
        assert "not seated in any channel" in r["error"]


def test_channel_that_is_not_yours_cannot_be_posted_to(tool, repos, link, tmp_path):
    stranger = tmp_path / "stranger"
    stranger.mkdir()

    r = call(tool, "xs_say", {"text": "hello", "channel": "gw--dash"},
             {"workspace_dir": str(stranger)})

    assert r["success"] is False
    assert "not seated" in r["error"]


def test_join_seats_you_in_an_existing_channel(tool, repos, link, tmp_path):
    third = tmp_path / "third"
    third.mkdir()
    ctx = {"workspace_dir": str(third)}

    assert call(tool, "xs_channel_join", {"channel": "gw--dash"}, ctx)["success"]
    assert call(tool, "xs_say", {"text": "in now"}, ctx)["success"]

    missing = call(tool, "xs_channel_join", {"channel": "no-such"}, ctx)
    assert missing["success"] is False


# ── Contract ──

def test_every_declared_tool_has_a_handler(tool):
    import yaml

    declared = {t["name"] for t in yaml.safe_load((REPO / "tool.yaml").read_text())["tools"]}
    assert declared == set(tool.HANDLERS)


def test_unknown_tool_is_refused(tool, repos):
    r = call(tool, "xs_wait", {}, repos["gw"])
    assert r["success"] is False
    assert "Unknown tool" in r["error"]


def test_get_info_reports_the_party_and_where_to_listen(tool, repos, link):
    info = tool.get_info(repos["gw"])

    assert info["party"] == "gw"
    assert info["channels"] == ["gw--dash"]
    assert "BACKGROUND" in info["listening"]
    assert os.path.isabs(str(info["board_dir"]))
