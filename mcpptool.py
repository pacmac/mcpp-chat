"""MCP entry point for the xsession board.

A second front end over `xs.py`, which keeps its CLI unchanged. Every board
function already takes the acting party as its first argument, so nothing here
edits the engine — it decides who the party is, dispatches, and formats.

Who the party is, is the one thing the conversion had to change. `xs.me()`
reads `Path.cwd().name`, which under mcpp is the SERVER's cwd: one long-lived
process, started once, in whatever directory the harness happened to be in. A
party is a repo, so the party here comes from `context["workspace_dir"]` and
`xs.me()` is never called.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Callable

_pkg_dir = Path(__file__).resolve().parent


def _board():
    """Import xs.py from this directory, once."""
    mod_name = "xsession_board"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _pkg_dir / "xs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Identity ──

def _party(xs, arguments: dict[str, Any], context: dict[str, Any] | None) -> str:
    """Explicit argument, then XSESSION_PARTY, then the workspace directory name.

    Never the process cwd: see the module docstring.
    """
    explicit = (arguments.get("party") or "").strip()
    if explicit:
        return xs.slugify(explicit)
    env = os.environ.get("XSESSION_PARTY", "").strip()
    if env:
        return xs.slugify(env)
    workspace = (context or {}).get("workspace_dir") or "."
    return xs.slugify(Path(workspace).resolve().name, "unnamed")


def _one_channel(xs, party: str, arguments: dict[str, Any]) -> str:
    """The single channel a posting command targets — seating is checked."""
    return xs.resolve_channel(party, (arguments.get("channel") or "").strip() or None)


def _many_channels(xs, party: str, arguments: dict[str, Any]) -> list[str]:
    """Every channel a reporting command covers. A party with two peers has two."""
    return xs.target_channels(party, (arguments.get("channel") or "").strip() or None)


# ── Handlers ──

def _inbox(xs, party: str, args: dict[str, Any]) -> dict[str, Any]:
    channel = (args.get("channel") or "").strip()
    box = xs.inbox(party, xs.resolve_channel(party, channel) if channel else None)
    display = xs.fmt_inbox(box)
    if not box["channels"]:
        # fmt_inbox is shared with the CLI and ends this case with `xs init ...`.
        # An agent reading that in a tool response is being told to shell out.
        display = display.replace("(xs init <name> --parties a,b)",
                                  "(xs_channel_init: channel, parties)")
    return {"success": True, "result": box, "display": display}


def _read(xs, party: str, args: dict[str, Any]) -> dict[str, Any]:
    peek = bool(args.get("peek"))
    out, lines, total = [], [], 0
    for ch in _many_channels(xs, party, args):
        new = xs.unread(ch, party)
        if not new:
            continue
        total += len(new)
        out.append({"channel": ch, "messages": new})
        lines.append(f"== {ch}")
        lines.append(xs.fmt_messages(new))
        if not peek:
            xs.set_seen(ch, party, max(m["seq"] for m in new))
    return {
        "success": True,
        "result": {"count": total, "peeked": peek, "channels": out},
        "display": "\n".join(lines) if lines else "nothing new.",
    }


def _say(xs, party: str, args: dict[str, Any]) -> dict[str, Any]:
    ch = _one_channel(xs, party, args)
    r = xs.post(ch, party, "say", args["text"])
    return {"success": True, "result": {**r, "channel": ch},
            "display": f"posted #{r['seq']} to {ch}"}


def _ask(xs, party: str, args: dict[str, Any]) -> dict[str, Any]:
    ch = _one_channel(xs, party, args)
    title = args["title"]
    slug = xs.slugify(args.get("slug") or title)
    xs.create_item(ch, party, slug, title)
    r = xs.post(ch, party, "ask", args.get("body") or title, slug=slug, title=title)
    return {"success": True, "result": {**r, "channel": ch, "status": "OPEN"},
            "display": f"raised [{slug}] as #{r['seq']} — OPEN"}


def _reply(xs, party: str, args: dict[str, Any]) -> dict[str, Any]:
    ch = _one_channel(xs, party, args)
    slug = args["slug"]
    if not xs.item_state(ch, slug):
        raise xs.Board(f"no item [{slug}]")
    r = xs.post(ch, party, "reply", args["text"], slug=xs.slugify(slug))
    # Answering is having it in hand; claiming so separately is noise.
    xs.touch(xs.item_dir(ch, slug) / "ack" / party)
    st = xs.item_state(ch, slug)
    if st and st["status"] == "OPEN":
        xs.set_status(ch, slug, "ACK")
    st = xs.item_state(ch, slug)
    return {"success": True, "result": {**r, "channel": ch, "status": st["status"]},
            "display": f"replied to [{slug}] as #{r['seq']}"}


def _ack(xs, party: str, args: dict[str, Any]) -> dict[str, Any]:
    ch = _one_channel(xs, party, args)
    slug = args["slug"]
    st = xs.item_state(ch, slug)
    if not st:
        raise xs.Board(f"no item [{slug}]")
    fresh = xs.touch(xs.item_dir(ch, slug) / "ack" / party)
    if st["status"] == "OPEN":
        xs.set_status(ch, slug, "ACK")
    return {"success": True,
            "result": {"channel": ch, "item": slug, "status": "ACK", "already": not fresh},
            "display": f"[{slug}] acked by {party}" + ("" if fresh else " (already)")}


def _resolve(xs, party: str, args: dict[str, Any]) -> dict[str, Any]:
    ch = _one_channel(xs, party, args)
    slug = args["slug"]
    st = xs.item_state(ch, slug)
    if not st:
        raise xs.Board(f"no item [{slug}]")
    if st["raiser"] != party:
        raise xs.Board(f"only {st['raiser']} raised [{slug}] and only they close it")
    xs.set_status(ch, slug, "DONE")
    xs.touch(xs.item_dir(ch, slug) / "pruneok" / party)
    return {"success": True, "result": {"channel": ch, "item": slug, "status": "DONE"},
            "display": f"[{slug}] DONE"}


def _prune_ok(xs, party: str, args: dict[str, Any]) -> dict[str, Any]:
    ch = _one_channel(xs, party, args)
    slug = args["slug"]
    if not xs.item_state(ch, slug):
        raise xs.Board(f"no item [{slug}]")
    xs.touch(xs.item_dir(ch, slug) / "pruneok" / party)
    st = xs.item_state(ch, slug)
    missing = sorted(xs.seats(ch) - st["pruneoks"])
    return {"success": True,
            "result": {"channel": ch, "item": slug, "waiting_on": missing,
                       "unanimous": not missing},
            "display": f"[{slug}] ok-to-prune by {party}" +
                       (f" — still waiting on {', '.join(missing)}" if missing else " — unanimous")}


def _archive(xs, party: str, args: dict[str, Any]) -> dict[str, Any]:
    moved = []
    for ch in _many_channels(xs, party, args):
        moved += [f"{ch}:{s}" for s in xs.archive_sweep(ch)]
    return {"success": True, "result": {"archived": moved},
            "display": "archived: " + (", ".join(moved) if moved else "nothing eligible")}


def _channel_init(xs, party: str, args: dict[str, Any]) -> dict[str, Any]:
    channel = args["channel"]
    raw = args.get("parties") or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    xs.ensure_channel(channel, [party] + [p for p in raw if str(p).strip()])
    seated = sorted(xs.seats(channel))
    return {"success": True,
            "result": {"channel": xs.slugify(channel), "parties": seated},
            "display": f"channel `{xs.slugify(channel)}` — seated: {', '.join(seated)}"}


def _channel_join(xs, party: str, args: dict[str, Any]) -> dict[str, Any]:
    channel = args["channel"]
    if not xs.channel_dir(channel).is_dir():
        raise xs.Board(f"no channel {channel!r}")
    xs.touch(xs.channel_dir(channel) / "parties" / party)
    seated = sorted(xs.seats(channel))
    return {"success": True,
            "result": {"channel": xs.slugify(channel), "parties": seated},
            "display": f"joined `{xs.slugify(channel)}` — seated: {', '.join(seated)}"}


HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "xs_inbox": _inbox,
    "xs_read": _read,
    "xs_say": _say,
    "xs_ask": _ask,
    "xs_reply": _reply,
    "xs_ack": _ack,
    "xs_resolve": _resolve,
    "xs_prune_ok": _prune_ok,
    "xs_archive": _archive,
    "xs_channel_init": _channel_init,
    "xs_channel_join": _channel_join,
}


# ── mcpp contract ──

def execute(tool_name: str, arguments: dict[str, Any],
            context: dict[str, Any] | None = None) -> dict[str, Any]:
    handler = HANDLERS.get(tool_name)
    if not handler:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}

    xs = _board()
    try:
        return handler(xs, _party(xs, arguments or {}, context), arguments or {})
    except xs.Board as exc:
        # Not seated, not the raiser, no such item: a message, never a traceback.
        return {"success": False, "error": str(exc)}
    except Exception as exc:                                  # pragma: no cover
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def get_info(context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Who this repo is on the board, and where the board lives."""
    xs = _board()
    party = _party(xs, {}, context)
    return {
        "party": party,
        "board_dir": str(xs.root()),
        "channels": xs.my_channels(party),
        "params": {
            "channel": {"values": xs.my_channels(party) or None, "default": None},
            "party": {"values": None, "default": party},
        },
        "listening": (
            "`wait` is not a tool. mcpp serves one call at a time, so a blocking tool "
            "would freeze every other module for as long as it blocks. To listen, run "
            f"`python3 {_pkg_dir / 'xs.py'} wait --timeout 3600` as a BACKGROUND shell "
            "command, one at a time."
        ),
    }
