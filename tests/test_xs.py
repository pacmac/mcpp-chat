"""xsession board tests.

The one that matters is test_concurrent_writers_lose_nothing: it is the failure
a single shared markdown file cannot prevent, and the reason this is a directory.
"""

from __future__ import annotations

import importlib.util
import multiprocessing
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("xs", REPO / "xs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["xs"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def xs(tmp_path, monkeypatch):
    monkeypatch.setenv("XSESSION_DIR", str(tmp_path / "board"))
    return _load()


@pytest.fixture()
def link(xs):
    """A channel with two parties seated: gw and dash."""
    xs.ensure_channel("gw--dash", ["gw", "dash"])
    return "gw--dash"


def run(args, tmp_path, party):
    return subprocess.run(
        [sys.executable, str(REPO / "xs.py"), "--as", party, *args],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "XSESSION_DIR": str(tmp_path / "board")},
    )


# ── Cursors ──

def test_unread_is_a_number_comparison_not_a_reread(xs, link):
    for i in range(5):
        xs.post(link, "dash", "say", f"message {i}")
    assert len(xs.unread(link, "gw")) == 5
    xs.set_seen(link, "gw", 3)
    assert [m["seq"] for m in xs.unread(link, "gw")] == [4, 5]


def test_my_own_posts_are_never_unread_to_me(xs, link):
    xs.post(link, "gw", "say", "mine")
    assert xs.unread(link, "gw") == []
    assert len(xs.unread(link, "dash")) == 1


def test_peek_does_not_consume(xs, link, tmp_path):
    xs.post(link, "dash", "say", "hello")
    r = run(["read", "--peek"], tmp_path, "gw")
    assert "hello" in r.stdout
    assert len(xs.unread(link, "gw")) == 1, "peek must not advance the cursor"
    run(["read"], tmp_path, "gw")
    assert xs.unread(link, "gw") == []


def test_cursor_survives_a_partial_write(xs, link):
    """write_atomic must never leave a truncated cursor."""
    xs.post(link, "dash", "say", "one")
    xs.set_seen(link, "gw", 1)
    f = xs.channel_dir(link) / "seen" / "gw"
    assert f.read_text().strip() == "1"
    assert not list(f.parent.glob(".*tmp*")), "no temp file left behind"


# ── Concurrency: the reason this is a directory ──

def _hammer(args):
    board, channel, party, n = args
    import os
    os.environ["XSESSION_DIR"] = board
    mod = _load()
    for i in range(n):
        mod.post(channel, party, "say", f"{party} {i}")


def test_concurrent_writers_lose_nothing(xs, link, tmp_path):
    """Eight processes posting at once: every post survives, sequences unique.

    A shared markdown file would interleave or clobber here. O_EXCL turns the
    race into a retry.
    """
    board = str(tmp_path / "board")
    procs, per = 8, 12
    with multiprocessing.Pool(procs) as pool:
        pool.map(_hammer, [(board, link, f"p{i}", per) for i in range(procs)])

    msgs = xs.messages(link)
    seqs = [m["seq"] for m in msgs]
    assert len(msgs) == procs * per, f"lost posts: {len(msgs)} of {procs * per}"
    assert len(set(seqs)) == len(seqs), "duplicate sequence numbers"
    assert sorted(seqs) == list(range(1, procs * per + 1)), "gaps in the sequence"
    for m in msgs:
        assert m["body"], "a message came back empty — interleaved write"


def test_a_marker_can_only_be_created_once(xs, link):
    d = xs.item_dir(link, "thing") / "ack" / "gw"
    assert xs.touch(d) is True
    assert xs.touch(d) is False, "second create must report it already existed"


# ── Item lifecycle ──

def test_full_handshake(xs, link, tmp_path):
    assert run(["ask", "Is the enum closed?", "--slug", "enum", "--body", "need FRAME_AUX"],
               tmp_path, "dash").returncode == 0
    assert xs.item_state(link, "enum")["status"] == "OPEN"

    run(["reply", "enum", "open — adding it"], tmp_path, "gw")
    st = xs.item_state(link, "enum")
    assert st["status"] == "ACK" and "gw" in st["acks"], "replying is having it in hand"

    r = run(["resolve", "enum"], tmp_path, "gw")
    assert r.returncode == 2 and "only dash" in r.stderr, "only the raiser closes it"

    assert run(["resolve", "enum"], tmp_path, "dash").returncode == 0
    assert xs.item_state(link, "enum")["status"] == "DONE"


def test_archive_needs_everyone(xs, link, tmp_path):
    run(["ask", "Q", "--slug", "q"], tmp_path, "dash")
    run(["reply", "q", "A"], tmp_path, "gw")
    run(["resolve", "q"], tmp_path, "dash")

    assert xs.archive_sweep(link) == [], "one party's ok is not unanimous"
    run(["prune-ok", "q"], tmp_path, "gw")
    assert xs.archive_sweep(link) == ["q"]
    assert xs.item_state(link, "q") is None
    assert xs.item_state(link, "q", archived=True)["status"] == "DONE", "history kept"


def test_nobody_can_prune_ok_for_a_peer(xs, link, tmp_path):
    run(["ask", "Q", "--slug", "q"], tmp_path, "dash")
    run(["resolve", "q"], tmp_path, "dash")
    run(["prune-ok", "q"], tmp_path, "dash")
    st = xs.item_state(link, "q")
    assert st["pruneoks"] == {"dash"}, "a party may only ever mark its own name"


# ── Obligations ──

def test_inbox_computes_direction(xs, link):
    xs.create_item(link, "dash", "q", "A question")
    xs.post(link, "dash", "ask", "body", slug="q", title="A question")

    gw = xs.inbox("gw")
    assert [r["item"] for r in gw["waiting_on_me"]] == ["q"]
    assert gw["unread_total"] == 1

    dash = xs.inbox("dash")
    assert dash["waiting_on_me"] == [], "the raiser does not owe themselves an ack"
    assert [r["item"] for r in dash["waiting_on_peer"]] == ["q"]


def test_done_item_asks_only_for_prune_ok(xs, link):
    xs.create_item(link, "dash", "q", "A question")
    xs.set_status(link, "q", "DONE")
    box = xs.inbox("gw")
    assert [r["item"] for r in box["needs_my_prune_ok"]] == ["q"]
    assert box["waiting_on_me"] == []


# ── Waiting ──

def test_wait_returns_at_once_when_something_is_already_there(xs, link):
    xs.post(link, "dash", "say", "already here")
    r = xs.wait("gw", timeout=5)
    assert r["arrived"] and r["via"] == "immediate"


def test_wait_times_out_quietly_on_silence(xs, link):
    start = time.monotonic()
    r = xs.wait("gw", timeout=1)
    assert r["arrived"] is False
    assert 0.9 <= time.monotonic() - start < 4


def _delayed_post(board, channel):
    import os, time
    os.environ["XSESSION_DIR"] = board
    time.sleep(0.6)
    _load().post(channel, "dash", "say", "late arrival")


def test_wait_wakes_when_a_peer_writes(xs, link, tmp_path):
    p = multiprocessing.Process(target=_delayed_post, args=(str(tmp_path / "board"), link))
    p.start()
    try:
        r = xs.wait("gw", timeout=10)
        assert r["arrived"] is True
        assert r["waited"] < 5, "should wake on the event, not sit out the timeout"
        assert r["via"] == "inotify", (
            "silently degraded to polling — the inotify path is broken and the "
            "fallback hid it, which is exactly how the wrong IN_NONBLOCK constant "
            "shipped once already")
    finally:
        p.join()


def test_poll_fallback_works_when_inotify_is_unavailable(xs, link, monkeypatch, tmp_path):
    """Any inotify trouble must degrade, never fail."""
    monkeypatch.setattr(xs, "_inotify_wait",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no inotify")))
    p = multiprocessing.Process(target=_delayed_post, args=(str(tmp_path / "board"), link))
    p.start()
    try:
        r = xs.wait("gw", timeout=10)
        assert r["arrived"] is True and r["via"] == "poll"
    finally:
        p.join()


# ── Safety ──

def test_nothing_here_touches_the_harness(xs):
    src = (REPO / "xs.py").read_text(encoding="utf-8")
    for forbidden in ("stop_hook_active", '"block"', "hookSpecificOutput", "settings.json"):
        assert forbidden not in src, f"{forbidden} must never appear in this module"


# ── More than one channel ──
#
# node-dash is really seated in two channels, one per peer. Everything below was
# broken for exactly that party: main() resolved a single channel before
# dispatching any command, so even the default inbox refused to run.

@pytest.fixture()
def two_channels(xs):
    xs.ensure_channel("dash--gw", ["dash", "gw"])
    xs.ensure_channel("dash--transport", ["dash", "transport"])
    return "dash--gw", "dash--transport"


def test_inbox_covers_every_channel(xs, two_channels, tmp_path):
    gw_ch, tr_ch = two_channels
    xs.post(gw_ch, "gw", "say", "from gw")
    xs.post(tr_ch, "transport", "say", "from transport")

    r = run(["inbox"], tmp_path, "dash")
    assert r.returncode == 0, f"inbox must not demand a channel: {r.stderr}"
    assert gw_ch in r.stdout and tr_ch in r.stdout
    assert xs.inbox("dash")["unread_total"] == 2


def test_read_consumes_across_channels(xs, two_channels, tmp_path):
    gw_ch, tr_ch = two_channels
    xs.post(gw_ch, "gw", "say", "alpha")
    xs.post(tr_ch, "transport", "say", "beta")

    r = run(["read"], tmp_path, "dash")
    assert r.returncode == 0
    assert "alpha" in r.stdout and "beta" in r.stdout
    assert xs.unread(gw_ch, "dash") == [] and xs.unread(tr_ch, "dash") == []


def test_wait_watches_every_channel(xs, two_channels):
    _gw_ch, tr_ch = two_channels
    xs.post(tr_ch, "transport", "say", "only in the second channel")
    r = xs.wait("dash", timeout=3)
    assert r["arrived"] is True, "a peer in either channel must wake the wait"


def test_archive_sweeps_every_channel(xs, two_channels, tmp_path):
    gw_ch, tr_ch = two_channels
    for ch, peer in ((gw_ch, "gw"), (tr_ch, "transport")):
        xs.create_item(ch, "dash", "q", "Q")
        xs.set_status(ch, "q", "DONE")
        xs.touch(xs.item_dir(ch, "q") / "pruneok" / "dash")
        xs.touch(xs.item_dir(ch, "q") / "pruneok" / peer)
    r = run(["archive"], tmp_path, "dash")
    assert f"{gw_ch}:q" in r.stdout and f"{tr_ch}:q" in r.stdout


def test_posting_still_demands_a_channel_when_ambiguous(xs, two_channels, tmp_path):
    r = run(["say", "which one?"], tmp_path, "dash")
    assert r.returncode == 2 and "-c" in r.stderr, "a post must not guess its destination"

    r = run(["-c", "dash--gw", "say", "this one"], tmp_path, "dash")
    assert r.returncode == 0


def test_cannot_reach_a_channel_i_am_not_seated_in(xs, two_channels, tmp_path):
    """A channel directory existing is not permission to write in it."""
    xs.ensure_channel("gw--transport", ["gw", "transport"])
    r = run(["-c", "gw--transport", "say", "not mine"], tmp_path, "dash")
    assert r.returncode == 2 and "not seated" in r.stderr
    assert xs.messages("gw--transport") == []
