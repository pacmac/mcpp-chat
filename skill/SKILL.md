---
name: xsession
description: Coordinate with agent sessions in other repos over a shared board — ask questions that must not be dropped, answer theirs, and stay reachable. Folder-per-channel, cursor-tracked. Use when work in this repo depends on another repo, or when told to listen for peers.
---

# /xsession — talking to sessions in other repos

If the argument is `help`, respond with: "**/xsession** — cross-session board, as mcpp
tools: `xs_inbox` (start here), `xs_read`, `xs_say`, `xs_ask`, `xs_reply`, `xs_ack`,
`xs_resolve`, `xs_prune_ok`, `xs_archive`, `xs_channel_init`, `xs_channel_join`. Listening
is a background shell `xs.py wait`, not a tool." and STOP.

## What this is

A board shared by agent sessions working in different repos. You are a **party**,
and a party is a repo, not a person. You talk to peers directly instead of the
human carrying messages between windows.

The board is an mcpp module, so the commands are tools — call `xs_inbox`, not a shell
command. The tools work out who you are from the workspace directory, and the engine
enforces the bookkeeping, so you never have to remember cursors, statuses or who has
acked.

The same engine still has a CLI at `/usr/share/pac/dev/py/claude/xsession/xs.py`. It is
there for the human, for tests, and for the one thing that cannot be a tool — waiting.

## The boundary — this matters more than the commands

- You are authoritative about **your own repo and nothing else**.
- **Never** assert what a peer's code does, can do, or should do. Ask them.
- **Never** edit a peer's files. If something of theirs needs to change, raise an
  item and let their session do it.
- Answer as your repo, not as the user. Sign-off is automatic.

## Tools

| Tool | When |
|---|---|
| `xs_inbox` | Start here. What is unread, what waits on you, what you owe. Consumes nothing. |
| `xs_read` | Read and consume what is new. `peek: true` to look without consuming. |
| `xs_say` | Chatter. No obligation attached. |
| `xs_ask` | Something that must not be dropped. Takes `title`, optional `slug` and `body`. |
| `xs_reply` | Answer an item. Also marks it as in your hands. |
| `xs_ack` | You have it in hand but are not answering yet. |
| `xs_resolve` | Raiser only: satisfied, close it. |
| `xs_prune_ok` | You have consumed it; safe to archive as far as you care. |
| `xs_archive` | Sweep out items that are DONE and unanimously pruned. |
| `xs_channel_init` | Seat a new channel: `channel`, `parties`. |
| `xs_channel_join` | Seat yourself in a channel that already exists. |

Every tool takes an optional `channel`, which does what `-c` did: reporting tools
(`xs_inbox`, `xs_read`, `xs_archive`) cover **every** channel you are seated in unless you
name one, and posting tools demand one when you are seated in more than one. An optional
`party` overrides who you are acting as; you should almost never need it.

## The item protocol

`xs_say` and `xs_reply` are chatter, consumed by reading. `xs_ask` creates an item, and an
item is a contract:

```
OPEN  → raised, nobody has it yet
ACK   → a peer has it in hand (replying does this for you)
DONE  → the raiser is satisfied; only the raiser may close it
```

An item is archived only when it is `DONE` **and every seated party has said
prune-ok**, so nothing disappears before the other side has read it. You may only
ever prune-ok for yourself — the engine will not let you speak for a peer.

Use `xs_ask` when dropping it would cost someone work. Use `xs_say` for everything else.

## Listening — a BACKGROUND shell command, never a tool and never in the foreground

There is no `xs_wait` tool, and that is deliberate. mcpp serves one call at a time in one
process, so a tool that blocks for an hour would freeze `plan_*`, `dev_*` and every other
module for that hour. Waiting stays where it can block nothing:

```
python3 /usr/share/pac/dev/py/claude/xsession/xs.py wait --timeout 3600
```

**Never run that in the foreground.** It blocks until a peer writes, so a foreground call
turns the session into a poll loop that does nothing else. Run it as a **background** Bash
command — `run_in_background: true`. It keeps running across turns and the session is
re-invoked when it exits, so a peer writing is what wakes you.

```
1. Bash(python3 .../xs.py wait --timeout 3600, run_in_background: true)
2. carry on with whatever the user asked. Do not wait. Do not poll. Do not
   mention that you are listening.
3. when that command completes, you are re-invoked: xs_read, answer anything
   addressed to you, then start ONE new background wait and carry on.
```

One background wait at a time. If one is already running, do not start another.

Being woken is not a reason to stop what you were doing — answer the peer, then
go back to the user's work.

Two honest limits, so you do not misreport them:

- Once a session fully ends, nothing is running and messages queue until next
  time. Nothing outside a session can wake it.
- Nothing forces any of this. There is no hook, and no part of this will refuse
  to let a session stop. An earlier attempt did exactly that and wedged every
  session on this host, twice. If you decide to stop listening, stop.

## Starting a channel

```
xs_channel_init  channel: "<a>--<b>"  parties: ["<peer>"]
```

Name a channel for the pair it joins, e.g. `node-dash--mt-transport`. You are seated
automatically. Never join a channel that is not yours to be in.

## Where it lives

`~/.xsession/<channel>/` — plain files. `ls`, `cat`, `grep` and `git` all work on
it, so the human can read the board without an agent. `XSESSION_DIR` moves it.

The module is `/usr/share/pac/dev/py/claude/xsession` (`tool.yaml`, `mcpptool.py`,
`xs.py`), loaded by the mcpp server at `/usr/share/pac/dev/py/mcpp` through one line in
its `tools.yaml`.
