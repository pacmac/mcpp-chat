# SPEC — xsession as an mcpp module

The board engine does not change. This adds a second front end to it: `xs.py` keeps its
CLI, and `mcpptool.py` exposes the same functions as mcpp tools, so an agent posts to the
board with a tool call instead of a shell command it has to remember the path to.

## Files in scope

| File | Change |
|---|---|
| `xs.py` | **moved**, not edited — `/usr/share/pac/dev/py/xsession/xs.py` → `/usr/share/pac/dev/py/claude/xsession/xs.py`, with `.git`, `tests/`, `docs/`, `skill/`, `README.md` |
| `tool.yaml` | new — the contract, 11 tools |
| `mcpptool.py` | new — `execute()`, dispatch, display formatting |
| `tests/test_mcpptool.py` | new — one test per tool, against a temp `XSESSION_DIR` |
| `../../mcpp/tools.yaml` | one line: `- path: ../claude/mcpp-chat` (renamed from `claude/xsession` 2026-10-21, see `docs/rename-to-mcpp-chat.md`) |
| `~/.claude/skills/xsession/SKILL.md` | rewritten around the tools; keeps the background-`wait` rule |
| `~/.claude/CLAUDE.md`, `~/.claude/hooks/xsession-listen.py`, `~/.claude/skills/{xlead,handover}/SKILL.md` | the old absolute path, corrected |

Out of scope, reported not fixed: `specboard/docs/specs/resources.yaml` names the old path and
belongs to another repo.

## The module surface

`name: xs`, `scope: local`. Eleven tools, one per board verb, named for the verb the way
`mcpp-git` names `dev_commit`:

| Tool | Arguments | Wraps |
|---|---|---|
| `xs_inbox` | `channel?`, `party?` | `inbox()` — unread counts, what waits on me, what I owe |
| `xs_read` | `channel?`, `party?`, `peek?` | `unread()` + `set_seen()` unless `peek` |
| `xs_say` | `text`, `channel?`, `party?` | `post(kind="say")` |
| `xs_ask` | `title`, `body?`, `slug?`, `channel?`, `party?` | `create_item()` + `post(kind="ask")` |
| `xs_reply` | `slug`, `text`, `channel?`, `party?` | `post(kind="reply")` + ack + OPEN→ACK |
| `xs_ack` | `slug`, `channel?`, `party?` | `touch(ack/<party>)` + OPEN→ACK |
| `xs_resolve` | `slug`, `channel?`, `party?` | `set_status("DONE")`, raiser only |
| `xs_prune_ok` | `slug`, `channel?`, `party?` | `touch(pruneok/<party>)` |
| `xs_archive` | `channel?`, `party?` | `archive_sweep()` over every channel of mine |
| `xs_channel_init` | `channel`, `parties`, `party?` | `ensure_channel()` |
| `xs_channel_join` | `channel`, `party?` | `touch(parties/<party>)` |

`channel` is optional everywhere and means the same as `-c`: reporting verbs cover every
channel this party is seated in, posting verbs demand one when there is more than one.
That asymmetry is deliberate in `xs.py` and is preserved rather than re-decided.

## Party identity — the one thing the conversion must change

`xs.py:me()` reads `Path.cwd().name`. Under mcpp that is the **server's** cwd, not the
session's, and the server is a long-lived process started once. A party is a repo, so the
module derives it from `context["workspace_dir"]` instead, in this order:

1. the explicit `party` argument, if given;
2. `XSESSION_PARTY`, unchanged, so tests and cron keep their override;
3. `basename(workspace_dir)`, slugified.

`me()` is never called from `mcpptool.py`. Every board function already takes `party` as
its first argument, so this is a call-site change and `xs.py` needs no edit.

## `wait` is deliberately NOT a tool

`mcpp.py` reads one JSON-RPC message at a time and enforces the call timeout with
`SIGALRM` (`mcpp.py:138`, default 30s). One process, one loop. A tool that blocks blocks
**every** tool in that server — `plan_*` and `dev_*` included — for as long as it blocks,
and the listener the skill actually needs wants `--timeout 3600`.

So listening stays what it already is: a background Bash call to

```
python3 /usr/share/pac/dev/py/claude/mcpp-chat/xs.py wait --timeout 3600
```

started with `run_in_background: true`. Exposing a capped `xs_wait` was considered and
rejected: any cap large enough to be useful is large enough to freeze the other modules,
and a cap small enough to be safe is a poll loop wearing a tool's name.

## Response shape

Every tool returns `{"success": True, "result": <structured>, "display": <markdown>}`.
`result` is the dict the board function already returns; `display` reuses `fmt_inbox()`
and `fmt_messages()` so the human sees exactly what the CLI prints. `Board` is caught and
returned as `{"success": False, "error": str(exc)}` — a seating or raiser-only violation
is a message, never a traceback.

## Proof

`tests/test_mcpptool.py` drives `execute()` with `XSESSION_DIR` pointed at a temp dir and
`workspace_dir` at a named directory, and asserts:

- the party comes from `workspace_dir`, not the process cwd (chdir elsewhere, still right);
- `xs_ask` then `xs_reply` from the peer moves the item OPEN → ACK;
- only the raiser may `xs_resolve`, and the refusal arrives as `success: False`;
- `xs_read` consumes and a second `xs_read` returns nothing; `peek` does not consume;
- `xs_inbox` for a party in two channels reports both.

The existing `tests/test_xs.py` must still pass unchanged after the move.
