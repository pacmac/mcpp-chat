#!/usr/bin/env python3
"""xsession — a folder-per-channel board for coordinating agent sessions.

The board this replaces was one shared markdown file. It had two faults, and
both of them lived in the fact that it was prose the agent had to interpret:
nobody could tell what was new without re-reading everything, and two sessions
editing at once could lose a post.

The fix is structural rather than remembered. Every mutation here is a file
CREATION, never an edit, because concurrent O_CREAT|O_EXCL is atomic: of two
processes creating the same path exactly one wins and the other is told. So a
race is detected instead of silently merged, and "what is new for me" is a
comparison of a directory against a number rather than a judgement call.

No third-party imports, no daemon, and nothing that talks to the agent harness.

    xs init <channel> --parties a,b     seat parties in a channel
    xs say <text>                       chatter
    xs ask <title> --slug s [--body b]  raise an item that must not be dropped
    xs reply <slug> <text>              answer an item
    xs ack <slug>                       I have this in hand
    xs resolve <slug>                   raiser only: it is finished
    xs prune-ok <slug>                  safe to archive as far as I am concerned
    xs inbox                            what is unread, what waits on me
    xs read [--peek]                    read (and consume) what is new
    xs wait [--timeout N]               block until something arrives
    xs archive                          sweep DONE + unanimously pruned items
"""

from __future__ import annotations

import argparse
import os
import re
import select
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SEQ_WIDTH = 4
POLL_SECONDS = 0.25
NAME_RE = re.compile(r"[^a-z0-9._~-]+")


# ── Identity and location ──

def root() -> Path:
    """Where the board lives. XSESSION_DIR wins so tests never touch the real one."""
    env = os.environ.get("XSESSION_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".xsession"


def slugify(text: str, fallback: str = "item") -> str:
    s = NAME_RE.sub("-", (text or "").strip().lower()).strip("-")
    return s or fallback


def me(override: str | None = None) -> str:
    """A party is a repo, not a person — the same rule the old board used."""
    if override:
        return slugify(override)
    env = os.environ.get("XSESSION_PARTY", "").strip()
    if env:
        return slugify(env)
    return slugify(Path.cwd().name, "unnamed")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Channels ──

def channel_dir(channel: str, archived: bool = False) -> Path:
    base = root() / "archive" if archived else root()
    return base / slugify(channel)


def ensure_channel(channel: str, parties: list[str]) -> Path:
    d = channel_dir(channel)
    for sub in ("msg", "item", "seen", "parties"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    for p in parties:
        touch(d / "parties" / slugify(p))
    return d


def seats(channel: str) -> set[str]:
    d = channel_dir(channel) / "parties"
    return {p.name for p in d.iterdir()} if d.is_dir() else set()


def my_channels(party: str) -> list[str]:
    if not root().is_dir():
        return []
    out = []
    for d in sorted(root().iterdir()):
        if d.name == "archive" or not d.is_dir():
            continue
        if (d / "parties" / party).exists():
            out.append(d.name)
    return out


def resolve_channel(party: str, channel: str | None) -> str:
    """The single channel a posting command targets.

    Only for commands that write to one place. Anything that reports on or waits
    for this party's traffic is inherently cross-channel and must use
    target_channels() instead — see the note there.
    """
    chans = my_channels(party)
    if channel:
        want = slugify(channel)
        if want not in chans:
            # A channel directory existing is not permission to write in it.
            where = f"you are in: {', '.join(chans)}" if chans else "you are in no channels"
            raise Board(f"{party} is not seated in {want!r} — {where}")
        return want
    if not chans:
        raise Board(f"{party} is not seated in any channel — xs init <name> --parties ...")
    if len(chans) > 1:
        raise Board(f"say which channel with -c: {', '.join(chans)}")
    return chans[0]


def target_channels(party: str, channel: str | None) -> list[str]:
    """Every channel a reporting command covers: all of mine, or just the named one.

    node-dash is genuinely seated in two channels, one per peer. An inbox that
    can only speak about one of them is not an inbox, and the first version of
    this resolved a single channel before dispatching any command — so every
    command, including the default inbox, failed for exactly the party the board
    exists to serve.
    """
    if channel:
        return [resolve_channel(party, channel)]
    chans = my_channels(party)
    if not chans:
        raise Board(f"{party} is not seated in any channel — xs init <name> --parties ...")
    return chans


class Board(Exception):
    """A user-facing problem, printed without a traceback."""


# ── Atomic primitives ──

def touch(path: Path) -> bool:
    """Create an empty marker. True if we created it, False if it already existed.

    Markers are named after the party that owns them, so a session can only ever
    create its own — nobody can ack or prune-ok on another party's behalf.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def write_atomic(path: Path, text: str) -> None:
    """Replace a file's contents without ever exposing a partial one.

    Used for the two things that genuinely are state rather than events: a
    cursor and an item's status. A crash leaves the previous value intact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def highest_seq(msg_dir: Path) -> int:
    """Highest number either claimed or written. Claims count: a claim always
    precedes its file, so ignoring them would hand the same number out twice."""
    top = 0
    for d, pattern in ((msg_dir, r"(\d+)-"), (msg_dir / ".seq", r"^(\d+)$")):
        if not d.is_dir():
            continue
        for f in d.iterdir():
            m = re.match(pattern, f.name)
            if m:
                top = max(top, int(m.group(1)))
    return top


def claim_seq(msg_dir: Path) -> int:
    """Reserve the next sequence number atomically.

    The message filename carries the kind and the slug because that makes the
    board greppable — but that also means two parties choosing the same number
    produce two DIFFERENT paths, so O_EXCL on the message file protects nothing.
    (Eight processes posting concurrently produced 96 files bearing 45 distinct
    numbers before this existed.) The claim is therefore a party-independent
    path, `.seq/NNNN`, which two claimants genuinely collide on.
    """
    (msg_dir / ".seq").mkdir(parents=True, exist_ok=True)
    for _ in range(500):
        seq = highest_seq(msg_dir) + 1
        if touch(msg_dir / ".seq" / f"{seq:0{SEQ_WIDTH}d}"):
            return seq
    raise Board("could not allocate a sequence number — too much contention")


# ── Posting ──

def post(channel: str, party: str, kind: str, body: str,
         slug: str | None = None, title: str | None = None) -> dict:
    """Append a message. Never loses a post to a concurrent writer.

    The sequence number is reserved first (see claim_seq) and is ours alone by
    the time the file is written, so two sessions posting at the same moment
    cannot overwrite each other or interleave inside one file. That is the
    failure a single shared markdown file cannot prevent.
    """
    d = channel_dir(channel)
    if not d.is_dir():
        raise Board(f"no channel {channel!r}")
    msg_dir = d / "msg"
    stamp = utc_now()

    if kind == "ask":
        slug = slug or slugify(title or body[:40])
        heading = f"### [{slug}] {title or slug}"
    elif kind == "reply":
        heading = f"### reply to [{slug}]"
    else:
        heading = f"### say from {party}"

    seq = claim_seq(msg_dir)
    path = msg_dir / f"{seq:0{SEQ_WIDTH}d}-{kind}-{slug or party}.md"
    meta = [f"seq: {seq}", f"kind: {kind}", f"from: {party}", f"at: {stamp}"]
    if slug:
        meta.append(f"slug: {slug}")
    text = f"{heading}\n{'  ::  '.join(meta)}\n\n{body.strip()}\n\n-- {party}\n"

    # The number is ours alone by now, so this create cannot collide. O_EXCL
    # stays as an assertion: if it ever fires, the claim logic is broken.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return {"seq": seq, "kind": kind, "from": party, "slug": slug, "path": str(path)}


# ── Reading ──

def parse_message(path: Path) -> dict:
    """Header is the first two lines; everything after is body. Body is never parsed."""
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    heading = lines[0] if lines else ""
    meta: dict[str, str] = {}
    if len(lines) > 1:
        for part in lines[1].split("::"):
            if ":" in part:
                k, v = part.split(":", 1)
                meta[k.strip()] = v.strip()
    body = "\n".join(lines[2:]).strip()
    m = re.match(r"(\d+)-", path.name)
    return {
        "seq": int(meta.get("seq") or (m.group(1) if m else 0)),
        "kind": meta.get("kind", "say"),
        "from": meta.get("from", "?"),
        "slug": meta.get("slug") or None,
        "at": meta.get("at", ""),
        "heading": heading.lstrip("# ").strip(),
        "body": body,
        "path": str(path),
    }


def messages(channel: str, archived: bool = False) -> list[dict]:
    d = channel_dir(channel, archived) / "msg"
    if not d.is_dir():
        return []
    return sorted((parse_message(f) for f in d.iterdir() if f.suffix == ".md"),
                  key=lambda m: m["seq"])


def seen(channel: str, party: str) -> int:
    f = channel_dir(channel) / "seen" / party
    try:
        return int(f.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def set_seen(channel: str, party: str, seq: int) -> None:
    write_atomic(channel_dir(channel) / "seen" / party, f"{seq}\n")


def unread(channel: str, party: str) -> list[dict]:
    """What is new for me. A number comparison, not a re-read of the whole board."""
    last = seen(channel, party)
    return [m for m in messages(channel) if m["seq"] > last and m["from"] != party]


# ── Items ──

def item_dir(channel: str, slug: str, archived: bool = False) -> Path:
    return channel_dir(channel, archived) / "item" / slugify(slug)


def item_state(channel: str, slug: str, archived: bool = False) -> dict | None:
    d = item_dir(channel, slug, archived)
    if not d.is_dir():
        return None
    meta: dict[str, str] = {}
    mf = d / "meta"
    if mf.exists():
        for line in mf.read_text(encoding="utf-8").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    status = (d / "status").read_text(encoding="utf-8").strip() if (d / "status").exists() else "OPEN"
    acks = {p.name for p in (d / "ack").iterdir()} if (d / "ack").is_dir() else set()
    pruneoks = {p.name for p in (d / "pruneok").iterdir()} if (d / "pruneok").is_dir() else set()
    return {
        "slug": d.name,
        "title": meta.get("title", d.name),
        "raiser": meta.get("raiser", "?"),
        "status": status,
        "acks": acks,
        "pruneoks": pruneoks,
    }


def items(channel: str, archived: bool = False) -> list[dict]:
    d = channel_dir(channel, archived) / "item"
    if not d.is_dir():
        return []
    out = [item_state(channel, p.name, archived) for p in sorted(d.iterdir()) if p.is_dir()]
    return [i for i in out if i]


def create_item(channel: str, party: str, slug: str, title: str) -> None:
    d = item_dir(channel, slug)
    if d.exists():
        raise Board(f"item [{slug}] already exists")
    (d / "ack").mkdir(parents=True, exist_ok=True)
    (d / "pruneok").mkdir(parents=True, exist_ok=True)
    write_atomic(d / "meta", f"title: {title}\nraiser: {party}\ncreated: {utc_now()}\n")
    write_atomic(d / "status", "OPEN\n")


def set_status(channel: str, slug: str, status: str) -> None:
    write_atomic(item_dir(channel, slug) / "status", f"{status}\n")


# ── Obligations ──

def inbox(party: str, channel: str | None = None) -> dict:
    """Who owes whom, computed from the directory rather than read out of prose."""
    out: dict = {"me": party, "channels": [], "unread_total": 0,
                 "waiting_on_me": [], "waiting_on_peer": [],
                 "mine_to_close": [], "needs_my_prune_ok": []}
    for ch in ([slugify(channel)] if channel else my_channels(party)):
        new = unread(ch, party)
        out["unread_total"] += len(new)
        out["channels"].append({"channel": ch, "unread": len(new),
                                "parties": sorted(seats(ch))})
        seated = seats(ch)
        for it in items(ch):
            ref = {"channel": ch, "item": it["slug"], "title": it["title"],
                   "status": it["status"], "raiser": it["raiser"]}
            if it["status"] in ("OPEN", "ACK"):
                if it["raiser"] != party and party not in it["acks"]:
                    out["waiting_on_me"].append(ref)
                missing = sorted(seated - it["acks"] - {it["raiser"]})
                if missing and it["raiser"] == party:
                    out["waiting_on_peer"].append({**ref, "waiting_on": missing})
            if it["status"] == "ACK" and it["raiser"] == party:
                out["mine_to_close"].append(ref)
            if it["status"] == "DONE" and party not in it["pruneoks"]:
                out["needs_my_prune_ok"].append(ref)
    return out


def archive_sweep(channel: str) -> list[str]:
    """Move out items that are DONE and unanimously ok-to-prune. Nothing else."""
    moved = []
    seated = seats(channel)
    for it in items(channel):
        if it["status"] != "DONE" or not seated.issubset(it["pruneoks"]):
            continue
        src = item_dir(channel, it["slug"])
        dst = channel_dir(channel, archived=True) / "item" / it["slug"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))
        msg_dst = channel_dir(channel, archived=True) / "msg"
        msg_dst.mkdir(parents=True, exist_ok=True)
        for m in messages(channel):
            if m["slug"] == it["slug"]:
                shutil.move(m["path"], str(msg_dst / Path(m["path"]).name))
        moved.append(it["slug"])
    return moved


# ── Waiting ──

def _inotify_wait(dirs: list[Path], timeout: float) -> bool:
    """Block on inotify via ctypes. True if something appeared.

    inotifywait is not installed here and a dependency is not worth it for
    forty lines. Any failure returns None so the caller falls back to polling.
    """
    import ctypes

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    fd = libc.inotify_init1(0o4000)                     # IN_NONBLOCK; 0o2000 is EINVAL
    if fd < 0:
        raise OSError(f"inotify_init1: {os.strerror(ctypes.get_errno())}")
    try:
        IN_CREATE, IN_MOVED_TO = 0x00000100, 0x00000080
        for d in dirs:
            if libc.inotify_add_watch(fd, str(d).encode(), IN_CREATE | IN_MOVED_TO) < 0:
                raise OSError(f"inotify_add_watch failed for {d}")
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return False
        os.read(fd, 4096)
        return True
    finally:
        os.close(fd)


def wait(party: str, timeout: float = 60.0, channel: str | None = None) -> dict:
    """Block until a peer writes, or the timeout expires.

    A primitive, not a policy: it blocks this process. It does not block a
    session, install a hook, or refuse to let anything stop. A caller loops on
    it because it chooses to.
    """
    chans = target_channels(party, channel)
    dirs = [channel_dir(c) / "msg" for c in chans]

    def pending() -> int:
        return sum(len(unread(c, party)) for c in chans)

    already = pending()
    if already:
        return {"arrived": True, "unread": already, "waited": 0.0, "via": "immediate"}

    started = time.monotonic()
    via = "inotify"
    try:
        _inotify_wait(dirs, timeout)
    except Exception:
        via = "poll"                                     # any inotify trouble: degrade
        deadline = started + timeout
        while time.monotonic() < deadline and not pending():
            time.sleep(POLL_SECONDS)
    waited = round(time.monotonic() - started, 2)
    n = pending()
    return {"arrived": bool(n), "unread": n, "waited": waited, "via": via}


# ── CLI ──

def fmt_inbox(box: dict) -> str:
    lines = [f"xsession — you are `{box['me']}`"]
    if not box["channels"]:
        return lines[0] + "\n  in no channels (xs init <name> --parties a,b)"
    for c in box["channels"]:
        lines.append(f"  {c['channel']}  unread {c['unread']}  ({', '.join(c['parties'])})")
    for label, key in (("Waiting on you", "waiting_on_me"),
                       ("Waiting on a peer", "waiting_on_peer"),
                       ("Yours to close", "mine_to_close"),
                       ("Needs your ok-to-prune", "needs_my_prune_ok")):
        if box[key]:
            lines.append(f"{label}:")
            for r in box[key]:
                extra = ""
                if r.get("waiting_on"):
                    extra = f" — waiting on {', '.join(r['waiting_on'])}"
                lines.append(f"  [{r['item']}] {r['title']}  ({r['status']}){extra}")
    return "\n".join(lines)


def fmt_messages(msgs: list[dict]) -> str:
    if not msgs:
        return "nothing new."
    out = []
    for m in msgs:
        out.append(f"--- #{m['seq']} {m['kind']} from {m['from']}  {m['at']}")
        out.append(m["heading"])
        out.append(m["body"])
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xs", description=__doc__.splitlines()[0])
    p.add_argument("--as", dest="as_party", help="act as this party (default: cwd name)")
    p.add_argument("-c", "--channel", help="channel, when seated in more than one")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("init"); q.add_argument("channel"); q.add_argument("--parties", default="")
    q = sub.add_parser("join"); q.add_argument("channel")
    q = sub.add_parser("say"); q.add_argument("text", nargs="+")
    q = sub.add_parser("ask"); q.add_argument("title", nargs="+")
    q.add_argument("--slug"); q.add_argument("--body", default="")
    q = sub.add_parser("reply"); q.add_argument("slug"); q.add_argument("text", nargs="+")
    q = sub.add_parser("ack"); q.add_argument("slug")
    q = sub.add_parser("resolve"); q.add_argument("slug")
    q = sub.add_parser("prune-ok"); q.add_argument("slug")
    sub.add_parser("inbox")
    q = sub.add_parser("read"); q.add_argument("--peek", action="store_true")
    q = sub.add_parser("wait"); q.add_argument("--timeout", type=float, default=60.0)
    sub.add_parser("archive")

    a = p.parse_args(argv)
    party = me(a.as_party)

    try:
        if a.cmd == "init":
            parts = [x for x in a.parties.replace(",", " ").split() if x]
            ensure_channel(a.channel, [party] + parts)
            print(f"channel `{slugify(a.channel)}` — seated: {', '.join(sorted(seats(a.channel)))}")
            return 0

        if a.cmd == "join":
            if not channel_dir(a.channel).is_dir():
                raise Board(f"no channel {a.channel!r}")
            touch(channel_dir(a.channel) / "parties" / party)
            print(f"joined `{slugify(a.channel)}`")
            return 0

        # Deliberately NOT resolving a single channel here. Only the posting
        # commands below need one; inbox / read / wait / archive cover every
        # channel this party is seated in, and forcing a choice on them is what
        # made the whole tool unusable for a party with two peers.
        if a.cmd in ("say", "ask", "reply", "ack", "resolve", "prune-ok"):
            ch = resolve_channel(party, a.channel)

        if a.cmd == "say":
            r = post(ch, party, "say", " ".join(a.text))
            print(f"posted #{r['seq']} to {ch}")

        elif a.cmd == "ask":
            title = " ".join(a.title)
            slug = slugify(a.slug or title)
            create_item(ch, party, slug, title)
            r = post(ch, party, "ask", a.body or title, slug=slug, title=title)
            print(f"raised [{slug}] as #{r['seq']} — OPEN")

        elif a.cmd == "reply":
            if not item_state(ch, a.slug):
                raise Board(f"no item [{a.slug}]")
            r = post(ch, party, "reply", " ".join(a.text), slug=slugify(a.slug))
            # Answering is having it in hand; claiming so separately is noise.
            touch(item_dir(ch, a.slug) / "ack" / party)
            st = item_state(ch, a.slug)
            if st and st["status"] == "OPEN":
                set_status(ch, a.slug, "ACK")
            print(f"replied to [{a.slug}] as #{r['seq']}")

        elif a.cmd == "ack":
            st = item_state(ch, a.slug)
            if not st:
                raise Board(f"no item [{a.slug}]")
            fresh = touch(item_dir(ch, a.slug) / "ack" / party)
            if st["status"] == "OPEN":
                set_status(ch, a.slug, "ACK")
            print(f"[{a.slug}] acked by {party}" + ("" if fresh else " (already)"))

        elif a.cmd == "resolve":
            st = item_state(ch, a.slug)
            if not st:
                raise Board(f"no item [{a.slug}]")
            if st["raiser"] != party:
                raise Board(f"only {st['raiser']} raised [{a.slug}] and only they close it")
            set_status(ch, a.slug, "DONE")
            touch(item_dir(ch, a.slug) / "pruneok" / party)
            print(f"[{a.slug}] DONE")

        elif a.cmd == "prune-ok":
            if not item_state(ch, a.slug):
                raise Board(f"no item [{a.slug}]")
            touch(item_dir(ch, a.slug) / "pruneok" / party)
            st = item_state(ch, a.slug)
            missing = sorted(seats(ch) - st["pruneoks"])
            print(f"[{a.slug}] ok-to-prune by {party}" +
                  (f" — still waiting on {', '.join(missing)}" if missing else " — unanimous"))

        elif a.cmd == "inbox":
            print(fmt_inbox(inbox(party, resolve_channel(party, a.channel) if a.channel else None)))

        elif a.cmd == "read":
            printed = False
            for c in target_channels(party, a.channel):
                new = unread(c, party)
                if not new:
                    continue
                print(f"== {c}")
                print(fmt_messages(new))
                printed = True
                if not a.peek:
                    set_seen(c, party, max(m["seq"] for m in new))
            if not printed:
                print("nothing new.")

        elif a.cmd == "wait":
            r = wait(party, a.timeout, a.channel)
            print(f"{'something arrived' if r['arrived'] else 'nothing arrived'} "
                  f"({r['unread']} unread, {r['waited']}s, via {r['via']})")

        elif a.cmd == "archive":
            moved = []
            for c in target_channels(party, a.channel):
                moved += [f"{c}:{s}" for s in archive_sweep(c)]
            print("archived: " + (", ".join(moved) if moved else "nothing eligible"))

    except Board as exc:
        print(f"xs: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
