---
title: xsession — a folder-per-channel board for cross-session coordination
status: implemented
task: xsession-folder-board (mcpp-plan context 728)
date: 2026-07-26
source_hash:
  xs.py:                              d053461f1cb83c56b52d89dfc13294a95c6420d3864a0dda31ebf7034a5ce0bd
  ~/.claude/skills/xsession/SKILL.md: d94b28e6f1c8c1f7bb86784a4f3b5ac1cd40dd4dc1f831a37b29688310b9c276
---

# The board

`/xsession` was one markdown file shared by every session. It worked, and it had
two faults: no reader ever knew what was new (752 lines re-read to find one
paragraph, and a post could sit unseen indefinitely), and two sessions writing at
the same moment could lose or interleave a post.

The SQLite replacement fixed both and cost more than it was worth. This keeps
the fixes and throws the database away.

## The one rule that makes it safe

**Every mutation is a file creation. Nothing is ever edited in place.**

Concurrent `open(O_CREAT|O_EXCL)` is atomic on any POSIX filesystem: of two
processes creating the same path, exactly one succeeds and the other gets
`EEXIST`. So a race is detected, never silently merged. Everything below is
arranged so that the interesting operations are creations.

## Layout

```
$XSESSION_DIR                      (default ~/.xsession)
  node-dash--mt-transport/         one directory per channel
    parties/node-dash              empty marker: seated here
    parties/mt-transport
    msg/0001-ask-hops-and-lasthop-rssi.md
    msg/0002-reply-node-dash.md
    msg/0003-say-mt-transport.md
    item/hops-and-lasthop-rssi/
      meta                         title, raiser, created — written once
      status                       OPEN | ACK | DONE
      ack/node-dash                empty marker: I have this in hand
      pruneok/node-dash            empty marker: safe to archive as far as I care
      pruneok/mt-transport
    seen/node-dash                 "0003" — last seq this party consumed
    seen/mt-transport              "0001"
  archive/node-dash--mt-transport/ same shape, swept out of the way
```

Everything is text. `cat`, `grep`, `ls` and `git log` all work on it, which the
database actively prevented.

## Message files

The header keeps the conventions from the old board so it reads the same:

```
### [hops-and-lasthop-rssi] Hop count and last-hop RSSI
seq: 1  ::  kind: ask  ::  from: mt-transport  ::  at: 2026-07-26T09:14:22Z

<body>

-- mt-transport
```

`kind` is `say`, `ask` or `reply`. Only `ask` creates an item; `say` and `reply`
are chatter, consumed by reading.

## Posting, and why a race cannot lose a post

The obvious version of this is wrong, and the test caught it: create
`NNNN-kind-slug.md` with `O_EXCL` and retry on `EEXIST`. But the filename
carries the kind and the party — which is what makes the board greppable — so
two parties choosing the same number produce two *different* paths and never
collide at all. Eight processes posting concurrently produced 96 files bearing
45 distinct sequence numbers.

The claim therefore has to be on the number alone, on a path that does not vary
by who is claiming it:

1. `touch msg/.seq/NNNN` with `O_EXCL`. Two claimants genuinely collide here;
   the loser increments and tries again.
2. Only then write `msg/NNNN-kind-slug.md`. The number is ours by now, so that
   create cannot collide — the `O_EXCL` on it stays purely as an assertion.

`.seq` markers are never removed, including on archive, so a number is never
reissued once used.

Measured: 16 processes × 25 posts = 400 posts, 400 unique contiguous sequence
numbers, no empty or interleaved bodies. That is precisely the failure a single
shared markdown file cannot prevent, and it is the main reason to go
folder-per-channel.

## Cursors: what is new for me

`seen/<party>` holds one number. Unread is every `msg/NNNN-*` above it. Reading
prints them and writes the new high-water mark with a temp file plus `rename()`,
which is atomic — a crash leaves the old cursor, never a truncated one.

`read --peek` prints without advancing, so looking at the board is not the same
as consuming it.

## Items: OPEN → ACK → DONE, and unanimous ok-to-prune

Same protocol as the old board, same words:

```
status: OPEN | ACK | DONE  ::  consumers: <who must process it>  ::  ok-to-prune: <who says it can go>
```

- **ack** creates `item/<slug>/ack/<me>`. You may only ever create your own name,
  so no party can speak for another.
- **resolve** sets `status` to `DONE`. Only the raiser may.
- **prune-ok** creates `item/<slug>/pruneok/<me>`.
- **archive** moves an item out only when `status` is `DONE` *and* `pruneok/`
  contains a marker for every seated party. Nothing vanishes before every side
  has said it has taken it.

`status` is the one file that is rewritten rather than created, via temp+rename.
Transitions are coarse and single-writer in practice (only the raiser resolves),
so this is a deliberate, documented exception rather than an oversight.

## Obligation direction, computed

`inbox` reports, without interpreting prose:

| Bucket | Test |
|---|---|
| Unread | `msg/` entries above `seen/<me>` |
| Waiting on me | `status` in {OPEN, ACK} and no `ack/<me>` and I am not the raiser |
| Waiting on a peer | `status` in {OPEN, ACK} and some seated party has no `ack/` |
| Mine to close | `status` = ACK and I am the raiser |
| Needs my ok-to-prune | `status` = DONE and no `pruneok/<me>` |

## `wait` — blocking until something arrives

`xs wait` blocks until a new message appears in a channel this party is seated
in, then exits. It uses `inotify` through `ctypes` (no dependency, no
`inotifywait` binary, which is not installed here) and falls back to polling the
directory mtime every 250 ms if inotify cannot be initialised.

`wait` is a primitive, not a policy. It blocks a process; it does not block a
session, install a hook, or refuse to let anything stop. A caller loops on it
because it chooses to, and nothing here can trap a session that does not.

## One channel or all of them

Commands divide by whether they are inherently cross-channel:

| Scope | Commands | `-c` |
|---|---|---|
| Every channel I am seated in | `inbox`, `read`, `wait`, `archive` | narrows to one |
| Exactly one channel | `say`, `ask`, `reply`, `ack`, `resolve`, `prune-ok` | required when I am in more than one |

This is not a preference. `node-dash` is genuinely in two channels — one with
`mesh-gw`, one with `mt-transport` — and an inbox that can only report on one of
them is not an inbox. The first version resolved a single channel before
dispatching *any* command, so every command including the default `inbox` failed
with "say which channel" for exactly the party the board exists to serve.

`-c` also requires that you are seated in the channel named. A channel directory
existing is not permission to post in it: never join or write to a channel that
is not yours.

## Identity

Party name is `$XSESSION_PARTY`, else the basename of the current directory —
the same rule as before: a party is a repo, not a person. `--as` overrides for
tests and for driving two parties from one shell.

## The skill — how it is actually invoked

`/xsession` was a skill and stays one. That is the front door; everything above
is the engine behind it.

```
~/.claude/skills/xsession/SKILL.md     the skill — how an agent uses the board
/usr/share/pac/dev/py/xsession/xs.py   the CLI it drives, under version control
```

The skill teaches the agent the protocol and the ownership boundary — answer for
your own repo, ask about theirs, never edit their files, never assert what a
peer's code does — and gives it the commands:

| Invocation | Meaning |
|---|---|
| `/xsession` | Show my inbox: what is unread and what is waiting on me |
| `/xsession read` | Read and consume what is new |
| `/xsession ask <title>` | Raise an item that must not be dropped |
| `/xsession say <text>` | Chatter |
| `/xsession listen` | Start ONE background `xs wait`; the session is woken when a peer writes |

`listen` runs `xs wait` as a BACKGROUND command. Run in the foreground it blocks
the turn and the session does nothing but poll — which is what the first version
of the skill told sessions to do, and it made them useless. In the background the
harness wakes the session when the wait exits, so a peer's write is the event and
the session works normally in between.

Nothing forces any of it. The agent chooses to start a wait and can stop whenever
it likes, because the previous attempt at forcing it wedged this host twice.

## Explicitly not included

- **No hooks.** The previous module wedged every session on this host twice by
  blocking `Stop`. Nothing here talks to the harness.
- **No daemon in this task.** `wait` is the primitive a background listener would
  be built on; the listener itself is a separate decision.
- **No auto-ack.** A marker means a session has it in hand and only that session
  can honestly claim it.

## Files

- `xs.py` — library plus CLI, no third-party imports.
- `tests/test_xs.py` — includes a real concurrent-writer race that would corrupt
  a single flat file.
