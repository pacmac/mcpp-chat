"""Protocol tests: identity, channels, cursors, and the item handshake."""

from __future__ import annotations

import pytest


# ── Identity ──

def test_party_is_the_repo(mods, conn, repos):
    _config, _db, chat = mods
    a, _b = repos
    party = chat.party_for(conn, str(a))
    assert party["name"] == "mesh-gw"
    assert party["workspace_path"] == str(a)


def test_party_is_stable_across_calls(mods, conn, repos):
    _config, _db, chat = mods
    a, _b = repos
    first = chat.party_for(conn, str(a))
    second = chat.party_for(conn, str(a))
    assert first["id"] == second["id"]
    assert conn.execute("SELECT COUNT(*) AS n FROM parties").fetchone()["n"] == 1


def test_same_basename_different_path_is_disambiguated(mods, conn, tmp_path):
    _config, _db, chat = mods
    one = tmp_path / "x" / "node-dash"
    two = tmp_path / "y" / "node-dash"
    one.mkdir(parents=True)
    two.mkdir(parents=True)
    a = chat.party_for(conn, str(one))
    b = chat.party_for(conn, str(two))
    assert a["name"] == "node-dash"
    assert b["name"] == "node-dash~2"
    assert a["id"] != b["id"]


# ── Channels ──

def test_channel_requires_a_valid_name(mods, conn, parties):
    _config, _db, chat = mods
    pa, _pb = parties
    with pytest.raises(chat.ChatError):
        chat.create_channel(conn, pa, "Not A Slug")


def test_channel_new_seats_named_parties(mods, conn, parties):
    _config, _db, chat = mods
    pa, pb = parties
    summary = chat.create_channel(conn, pa, "link", None, [pb["name"]])
    assert set(summary["parties"]) == {"mesh-gw", "node-dash"}


def test_unknown_party_is_rejected_with_guidance(mods, conn, parties):
    _config, _db, chat = mods
    pa, _pb = parties
    with pytest.raises(chat.ChatError) as exc:
        chat.create_channel(conn, pa, "link", None, ["ghost-repo"])
    assert "unknown parties" in str(exc.value)


def test_resolve_channel_needs_disambiguation(mods, conn, parties):
    _config, _db, chat = mods
    pa, pb = parties
    chat.create_channel(conn, pa, "one", None, [pb["name"]])
    chat.create_channel(conn, pa, "two", None, [pb["name"]])
    with pytest.raises(chat.ChatError) as exc:
        chat.resolve_channel(conn, pa, None)
    assert "several channels" in str(exc.value)


def test_resolve_channel_single_is_implicit(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, _pb = parties
    assert chat.resolve_channel(conn, pa, None)["name"] == "link"


def test_posting_to_a_channel_you_are_not_in_is_refused(mods, conn, parties, repos, tmp_path):
    _config, _db, chat = mods
    pa, pb = parties
    chat.create_channel(conn, pa, "private", None, [])
    third = tmp_path / "rotator"
    third.mkdir()
    pc = chat.party_for(conn, str(third))
    with pytest.raises(chat.ChatError) as exc:
        chat.say(conn, pc, "let me in", "private")
    assert "not in" in str(exc.value)


# ── Chatter and cursors ──

def test_say_and_read_advances_cursor(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.say(conn, pa, "gateway is up", channel)
    first = chat.read(conn, pb, channel)
    assert [m["body"] for m in first["messages"]] == ["gateway is up"]
    again = chat.read(conn, pb, channel)
    assert again["messages"] == []


def test_peek_does_not_advance_cursor(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.say(conn, pa, "peek me", channel)
    chat.read(conn, pb, channel, peek=True)
    assert len(chat.read(conn, pb, channel)["messages"]) == 1


def test_author_does_not_see_own_post_as_unread(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, _pb = parties
    chat.say(conn, pa, "mine", channel)
    assert chat.inbox(conn, pa)["unread_total"] == 0


def test_read_limit_reports_more(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    for i in range(5):
        chat.say(conn, pa, f"m{i}", channel)
    page = chat.read(conn, pb, channel, limit=2)
    assert len(page["messages"]) == 2 and page["more"] == 3


# ── The item handshake ──

def test_ask_creates_open_item_with_author_pre_acked(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, _pb = parties
    item = chat.ask(conn, pa, "Is the enum closed?", "I need a new frame type.", channel)
    assert item["status"] == "open"
    assert item["slug"] == "is-the-enum-closed"
    assert set(item["consumers"]) == {"mesh-gw", "node-dash"}
    assert item["awaiting_ack_from"] == ["node-dash"]


def test_ask_needs_a_second_party(mods, conn, parties):
    _config, _db, chat = mods
    pa, _pb = parties
    chat.create_channel(conn, pa, "solo", None, [])
    with pytest.raises(chat.ChatError) as exc:
        chat.ask(conn, pa, "anyone?", "body", "solo")
    assert "no other party" in str(exc.value)


def test_ask_requires_a_body(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, _pb = parties
    with pytest.raises(chat.ChatError):
        chat.ask(conn, pa, "title only", "", channel)


def test_slug_collision_is_suffixed(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, _pb = parties
    one = chat.ask(conn, pa, "same title", "b", channel)
    two = chat.ask(conn, pa, "same title", "b", channel)
    assert one["slug"] == "same-title" and two["slug"] == "same-title-2"


def test_ack_moves_open_to_ack(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q")
    state = chat.ack(conn, pb, "q")
    assert state["status"] == "ack"
    assert state["awaiting_ack_from"] == []


def test_non_consumer_cannot_ack(mods, conn, parties, channel, tmp_path):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q", consumers=[pb["name"]])
    third = tmp_path / "rotator"
    third.mkdir()
    pc = chat.party_for(conn, str(third))
    chat.join_channel(conn, pc, channel)
    with pytest.raises(chat.ChatError) as exc:
        chat.ack(conn, pc, "q")
    assert "not a consumer" in str(exc.value)


def test_reply_to_open_item_auto_acks(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q")
    out = chat.reply(conn, pb, "q", "I own the generator; adding it now.")
    assert out["auto_acked"] is True
    assert chat._item_state(conn, _item_id(conn, "q"))["status"] == "ack"


def test_answerer_cannot_resolve_without_answered_flag(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q")
    chat.ack(conn, pb, "q")
    with pytest.raises(chat.ChatError) as exc:
        chat.resolve_item(conn, pb, "q")
    assert "only the raiser confirms satisfaction" in str(exc.value)


def test_answerer_may_resolve_when_answered(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q")
    chat.ack(conn, pb, "q")
    state = chat.resolve_item(conn, pb, "q", answered=True)
    assert state["status"] == "done"


def test_consumer_must_ack_before_resolving(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q")
    with pytest.raises(chat.ChatError) as exc:
        chat.resolve_item(conn, pb, "q", answered=True)
    assert "ack it first" in str(exc.value)


def test_raiser_resolves_freely(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q")
    assert chat.resolve_item(conn, pa, "q")["status"] == "done"


def test_prune_needs_done_and_unanimous(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q")

    # Gate 1: not done yet -> prune-ok recorded but nothing archived.
    state = chat.prune_ok(conn, pb, "q")
    assert state["archived_now"] is False

    chat.resolve_item(conn, pa, "q")
    # Gate 2: done, but the raiser has not prune-ok'd.
    live = chat.read(conn, pa, channel, since=0, peek=True)
    assert any(m.get("slug") == "q" for m in live["messages"])

    final = chat.prune_ok(conn, pa, "q")
    assert final["archived_now"] is True


def test_archived_item_leaves_the_live_channel_but_is_kept(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q")
    chat.reply(conn, pb, "q", "answered")
    chat.resolve_item(conn, pa, "q")
    chat.prune_ok(conn, pa, "q")
    chat.prune_ok(conn, pb, "q")

    live = chat.read(conn, pa, channel, since=0, peek=True)
    assert not any(m.get("slug") == "q" for m in live["messages"])

    history = chat.read(conn, pa, channel, since=0, peek=True, archived=True)
    assert any(m.get("slug") == "q" for m in history["messages"])
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE slug = 'q'"
    ).fetchone()["n"] == 1


def test_prune_ok_is_self_only(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q")
    chat.prune_ok(conn, pa, "q")
    rows = conn.execute(
        "SELECT p.name, ic.prune_ok_at FROM item_consumers ic JOIN parties p ON p.id = ic.party_id "
        "WHERE ic.message_id = ?", (_item_id(conn, "q"),)
    ).fetchall()
    stamped = {r["name"]: bool(r["prune_ok_at"]) for r in rows}
    assert stamped == {"mesh-gw": True, "node-dash": False}


def test_announce_is_done_but_still_shown_once(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    item = chat.ask(conn, pa, "FYI: rebooting", "gateway restart in 5m", channel, announce=True)
    assert item["status"] == "done"
    box = chat.inbox(conn, pb)
    assert [i["needs"] for i in box["waiting_on_me"]] == ["your prune-ok (chat_prune_ok)"]


# ── Inbox routing ──

def test_inbox_routes_work_to_the_right_side(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q")

    peer_box = chat.inbox(conn, pb)
    assert len(peer_box["waiting_on_me"]) == 1
    assert "your ack" in peer_box["waiting_on_me"][0]["needs"]

    author_box = chat.inbox(conn, pa)
    assert author_box["waiting_on_me"] == []
    assert author_box["waiting_on_peer"][0]["waiting_on"] == ["node-dash"]


def test_inbox_asks_author_to_resolve_after_ack(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q")
    chat.ack(conn, pb, "q")
    needs = [i["needs"] for i in chat.inbox(conn, pa)["waiting_on_me"]]
    assert any("your resolve" in n for n in needs)


def test_pending_count_tracks_work_for_me(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    assert chat.pending_count(conn, pb) == 0
    chat.ask(conn, pa, "q", "body", channel, slug="q")
    assert chat.pending_count(conn, pb) > 0


def test_leaving_keeps_existing_obligations(mods, conn, parties, channel):
    _config, _db, chat = mods
    pa, pb = parties
    chat.ask(conn, pa, "q", "body", channel, slug="q")
    chat.leave_channel(conn, pb, channel)
    chat.resolve_item(conn, pa, "q")
    chat.prune_ok(conn, pa, "q")
    # pb is gone but still owed a prune-ok, so the item stays live.
    assert conn.execute(
        "SELECT is_archived FROM messages WHERE slug = 'q'"
    ).fetchone()["is_archived"] == 0


def _item_id(conn, slug: str) -> int:
    return int(conn.execute("SELECT id FROM messages WHERE slug = ?", (slug,)).fetchone()["id"])
