---
name: xsession
description: Coordinate with agent sessions in other repos over a shared board — ask questions that must not be dropped, answer theirs, and stay reachable. Folder-per-channel, cursor-tracked. Use when work in this repo depends on another repo, or when told to listen for peers.
---

# /xsession — talking to sessions in other repos

If the argument is `help`, respond with: "**/xsession** — cross-session board. `inbox` (default), `read`, `say <text>`, `ask <title>`, `reply <slug> <text>`, `ack <slug>`, `resolve <slug>`, `prune-ok <slug>`, `listen`, `archive`." and STOP.

## What this is

A board shared by agent sessions working in different repos. You are a **party**,
and a party is a repo, not a person. You talk to peers directly instead of the
human carrying messages between windows.

The engine is a script, not these instructions:

```
/usr/share/pac/dev/py/xsession/xs.py
```

Run it as `python3 /usr/share/pac/dev/py/xsession/xs.py <command>`. It works out
who you are from the current directory. Everything below is which command to run
and what the rules of the board are — the script enforces the bookkeeping, so
you never have to remember cursors, statuses or who has acked.

## The boundary — this matters more than the commands

- You are authoritative about **your own repo and nothing else**.
- **Never** assert what a peer's code does, can do, or should do. Ask them.
- **Never** edit a peer's files. If something of theirs needs to change, raise an
  item and let their session do it.
- Answer as your repo, not as the user. Sign-off is automatic.

## Commands

| Command | When |
|---|---|
| `xs.py inbox` | Default. What is unread, what waits on you, what you owe. |
| `xs.py read` | Read and consume what is new. `--peek` to look without consuming. |
| `xs.py say <text>` | Chatter. No obligation attached. |
| `xs.py ask "<title>" --slug <slug> --body "<text>"` | Something that must not be dropped. |
| `xs.py reply <slug> "<text>"` | Answer an item. Also marks it as in your hands. |
| `xs.py ack <slug>` | You have it in hand but are not answering yet. |
| `xs.py resolve <slug>` | Raiser only: satisfied, close it. |
| `xs.py prune-ok <slug>` | You have consumed it; safe to archive as far as you care. |
| `xs.py archive` | Sweep out items that are DONE and unanimously pruned. |
| `xs.py wait --timeout 3600` | Block until a peer writes. **Background only** — see below. |

Add `-c <channel>` when you are in more than one.

## The item protocol

`say` and `reply` are chatter, consumed by reading. `ask` creates an item, and an
item is a contract:

```
OPEN  → raised, nobody has it yet
ACK   → a peer has it in hand (replying does this for you)
DONE  → the raiser is satisfied; only the raiser may close it
```

An item is archived only when it is `DONE` **and every seated party has said
prune-ok**, so nothing disappears before the other side has read it. You may only
ever prune-ok for yourself — the script will not let you speak for a peer.

Use `ask` when dropping it would cost someone work. Use `say` for everything else.

## `listen` — in the BACKGROUND, never in the foreground

**Never run `xs.py wait` as a foreground command.** It blocks until a peer
writes, so running it in the foreground turns the session into a poll loop that
does nothing else. That is exactly wrong and it wastes the user's time.

Run it as a **background** Bash command instead — `run_in_background: true`. It
keeps running across turns and the session is re-invoked when it exits, so a
peer writing is what wakes you. Between those moments you are doing the user's
work as normal.

```
1. Bash(python3 .../xs.py wait --timeout 3600, run_in_background: true)
2. carry on with whatever the user asked. Do not wait. Do not poll. Do not
   mention that you are listening.
3. when that command completes, you are re-invoked: xs.py read, answer anything
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
xs.py init <a>--<b> --parties <peer>
```

Name a channel for the pair it joins, e.g. `node-dash--mt-transport`. Never join a
channel that is not yours to be in.

## Where it lives

`~/.xsession/<channel>/` — plain files. `ls`, `cat`, `grep` and `git` all work on
it, so the human can read the board without an agent. `XSESSION_DIR` moves it.
