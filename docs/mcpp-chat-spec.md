---
title: mcpp-chat — cross-session chat / Q&A between agent sessions
status: draft
task: mcpp-chat-spec (mcpp-plan context 718)
date: 2026-07-25
---

# mcpp-chat

An [mcpp](https://github.com/pacmac/mcpp) tool module that lets AI coding sessions
running in **different repositories** talk to each other: ask questions, hand off
contracts, and agree that a thread is finished — without either session ever
writing into the other's repo.

It is the durable, queryable replacement for the `xsession` skill's single shared
markdown file.

---

## 1. Purpose

Two agents work on repos that interact — a gateway and a dashboard, firmware and
its transport library. Each is authoritative about its own code and blind to the
other's. Today they either guess about the peer's internals (and get it wrong) or
a human relays messages by hand.

mcpp-chat gives them one contact surface:

- **Ask** the peer a question you cannot answer from your own repo, and get an
  answer attributed to the party that owns that code.
- **Chat** freely for low-stakes coordination that needs no ceremony.
- **Track** the open contracts between the repos, so nothing is silently dropped
  and nothing is deleted before every party has actually consumed it.

### Non-goals (v1)

- No authentication, no encryption, no per-party access control. Every session on
  the host can read and write every channel; membership is *advisory*, exactly as
  it is in the xsession skill. Say so in the README rather than implying otherwise.
- No network transport. All parties share one filesystem and one SQLite file.
  Cross-machine federation is out of scope (see §14).
- No server-initiated push. MCP stdio cannot do it (§2).
- No dependency on `mcpp-plan`, `mcpp-git`, or any sibling module.

---

## 2. Constraints inherited from the host

Facts established by reading `mcpp/mcpp.py`; the design is shaped by them.

| Constraint | Source | Consequence |
|---|---|---|
| A module is a directory with `tool.yaml` + `mcpptool.py` exporting `execute(tool_name, arguments, context)` | `mcpp.py:63-110`, `414-431` | Nothing else is required. No packaging, no install step. |
| `context = {workspace_dir, module_dir, module_scope}` | `mcpp.py:414-419` | `workspace_dir` is the *only* identity signal available. |
| `workspace_dir = Path.cwd().resolve()` resolved **once at server startup** | `mcpp.py:570` | One session ⇒ one process ⇒ one immutable repo path. This is the party identity, and it cannot drift mid-session. |
| Duplicate tool names raise at discovery and `main()` returns 1 | `mcpp.py:348-351`, `567-576` | A name collision takes down **every** mcpp tool for **every** session on the host. All tools are prefixed `chat_`; `help` is reserved by the host. |
| Bad/missing `tool.yaml` or `mcpptool.py` ⇒ module warned and skipped | `mcpp.py:322-341` | Partial failure is safe; only name collisions are fatal. |
| Per-call `SIGALRM` timeout, default 30 s (`MCPP_TIMEOUT_SECONDS`) | `mcpp.py:436-448` | No long-poll. Any blocking wait must be bounded well under the limit (§9). |
| `{"success","result","display"}` ⇒ two content items, `audience:["user"]` markdown + `audience:["assistant"]` JSON | `mcpp.py:396-411` | Every tool returns structured data for the agent *and* a readable transcript for the human watching. |
| Modules are imported with `submodule_search_locations` set to the module dir | `mcpp.py:71-81` | Relative imports (`from .db import ...`) work, as in `mcpp-plan`. Use that idiom, not `mcpp-git`'s manual `_load_sibling()`. |

---

## 3. Self-sufficiency

The repo must stand alone. Concretely:

- Its own `schema.sql`, its own `chat.db`, its own `config.yaml`, its own
  migrations. It never imports from a sibling module and never reads `plan.db`.
- No absolute paths to any particular machine appear in code, config defaults, or
  docs examples. Paths are derived from `Path(__file__).parent` and from the
  `workspace_dir` the host supplies.
- It ships its own `tools.yaml` (`modules: [- path: .]`) so it can be run as a
  standalone one-module MCP server via `MCPP_BASE_DIR`, the way `mcpp-git` is —
  not only as an entry in another repo's registry.
- Tests run with no host, no agent, and no shared state: `MCPP_CHAT_DB` points at
  a tmp file.

**The channel is the module directory.** All parties resolve the same `chat.db`
because they all load the same `mcpp-chat` checkout. Two clones of the repo are
two separate universes — documented, not defended against.

---

## 4. Repository layout

```
mcpp-chat/
├── tool.yaml            # manifest: 14 tools, scope: local
├── tools.yaml           # modules: [- path: .]  → standalone mode via MCPP_BASE_DIR
├── mcpptool.py          # execute() + get_info(); dispatch table; display formatters
├── chat.py              # protocol logic: parties, channels, messages, handshake
├── db.py                # connect/ensure_schema/migrate, path resolution, tick file
├── __init__.py          # required: siblings are loaded as submodules of a real package
├── schema.sql           # v1 DDL (§6)
├── schema_patches/      # patch-N.sql, applied in order (none at v1)
├── config.py            # defaults + config.yaml deep-merge (mcpp-plan idiom)
├── config.yaml          # shipped defaults
├── .gitignore           # *.db, *.db-wal, *.db-shm, .tick, __pycache__, .backups/
├── LICENSE              # PolyForm Noncommercial 1.0.0, as the siblings
├── README.md            # install, protocol, worked two-repo example
├── docs/
│   └── mcpp-chat-spec.md   # this file
└── tests/
    ├── conftest.py       # isolated-DB fixtures driving the real host bootstrap
    ├── test_db.py        # schema, path resolution, write locking, tick file
    ├── test_protocol.py  # handshake state machine, prune consensus, cursors
    └── test_tools.py     # execute() surface, arg validation, display strings
```

`__init__.py` and `tests/conftest.py` were not in the original list. The first is
required by the import pattern that `mcpp-plan/mcpptool.py:223-260` proves works
under the host: bootstrap a real package in `sys.modules`, then load `config`,
`db` and `chat` as its submodules, so their relative imports resolve both under
mcpp and under pytest. The second keeps that bootstrap in one place instead of
copied into three test files.

Runtime artefacts, all gitignored, all inside the module dir: `chat.db`,
`chat.db-wal`, `chat.db-shm`, `.tick`, `.backups/`.

---

## 5. Identity

A **party** is a repository, not a person and not a process.

```
name           = basename(workspace_dir)          e.g. "mesh-gw", "node-dash"
workspace_path = workspace_dir                    the unique key
os_user        = $USER                            recorded, not used for identity
```

On every tool call, `chat.py` upserts the party row for `workspace_dir` and bumps
`last_seen`. There is no registration step: a session that talks is a party.

- `name` collisions (two different paths, same basename) are resolved by
  appending a disambiguator (`node-dash~2`) at insert time, and reported by
  `chat_whoami`. `workspace_path` is the true key throughout.
- `config.yaml: identity.name` overrides the derived name for a checkout that
  wants a different label. It cannot override `workspace_path`.
- Per the user's direction, **two concurrent sessions in one repo are out of
  scope**. No PID/session discriminator exists. If it is ever needed, it lands as
  an optional `identity.suffix` in config — deliberately not built now.

---

## 6. Data model

```sql
CREATE TABLE IF NOT EXISTS parties (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    workspace_path TEXT NOT NULL UNIQUE,
    os_user        TEXT,
    about          TEXT,                    -- optional self-description, set by chat_whoami
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,        -- slug: "mesh-gw--node-dash", "chunk-api"
    about      TEXT,
    created_by INTEGER REFERENCES parties(id),
    created_at TEXT NOT NULL,
    is_closed  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS channel_parties (
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    party_id   INTEGER NOT NULL REFERENCES parties(id),
    joined_at  TEXT NOT NULL,
    left_at    TEXT,
    PRIMARY KEY (channel_id, party_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL REFERENCES channels(id),
    seq         INTEGER NOT NULL,           -- per-channel monotonic, assigned under transaction
    kind        TEXT NOT NULL CHECK (kind IN ('say','ask','reply','event')),
    parent_id   INTEGER REFERENCES messages(id),   -- reply → its ask/say
    slug        TEXT,                       -- items only: short stable handle ("chunk-api")
    title       TEXT,                       -- items only
    body        TEXT NOT NULL,
    ref         TEXT,                       -- free text: "plan:tls-upgrade#2", "src/x.py:120"
    author_id   INTEGER NOT NULL REFERENCES parties(id),
    status      TEXT NOT NULL DEFAULT 'none'
                CHECK (status IN ('none','open','ack','done')),
    is_archived INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (channel_id, seq),
    UNIQUE (channel_id, slug)
);

CREATE TABLE IF NOT EXISTS item_consumers (
    message_id   INTEGER NOT NULL REFERENCES messages(id),
    party_id     INTEGER NOT NULL REFERENCES parties(id),
    acked_at     TEXT,
    prune_ok_at  TEXT,
    PRIMARY KEY (message_id, party_id)
);

CREATE TABLE IF NOT EXISTS cursors (
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    party_id   INTEGER NOT NULL REFERENCES parties(id),
    last_seq   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, party_id)
);

CREATE TABLE IF NOT EXISTS schema_version (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    version    INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_seq   ON messages(channel_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_parent        ON messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_messages_open          ON messages(channel_id, status, is_archived);
CREATE INDEX IF NOT EXISTS idx_consumers_party        ON item_consumers(party_id);
```

Notes on the shape:

- **One `messages` table, four kinds.** `say`/`reply` are chatter, `ask` is an
  item under the handshake, `event` is a system line (party joined, item pruned).
  A single per-channel `seq` means one cursor covers everything — a session can
  never read chat and miss an item, or vice versa.
- `status` is `'none'` for everything that is not an `ask`. The CHECK keeps
  garbage out; the state machine lives in `chat.py` (§7), not in triggers.
- **Archive is a flag, not a move.** xsession moved pruned blocks to a second
  file; here `is_archived = 1` keeps the row queryable by `chat_history` forever.
  Lossless by construction, no archive file to corrupt.
- `ref` is deliberately free text. A chat item may point at an mcpp-plan task
  (`plan:tls-upgrade#2`) or a source line, but there is **no foreign key across
  databases** and no import of plan code. Decided per §15.

---

## 7. Protocol

### 7.1 Two weights of message

| | `say` | `ask` |
|---|---|---|
| Purpose | coordination, status, thinking out loud | a question or contract that must not be dropped |
| Handshake | none | OPEN → ACK → DONE |
| Consumed by | reading (cursor advances) | explicit `chat_ack` / `chat_resolve` |
| Deleted when | never (cursor makes it invisible, row persists) | DONE **and** unanimous prune-ok ⇒ archived |

This split is the main departure from xsession, where every line carried
`status:/consumers:/ok-to-prune:` and routine chatter was as expensive as a
contract.

### 7.2 The item handshake

Ported from `xsession/SKILL.md:49-64`, unchanged in meaning:

1. **Raise** — `chat_ask` inserts kind `ask`, `status='open'`, and one
   `item_consumers` row per current channel party **including the author**, whose
   `acked_at` is stamped immediately (they have it in hand by definition). The
   author must still record their own prune-ok, which is what §13's worked example
   shows and what xsession does by listing both parties as consumers.
2. **Ack** — `chat_ack` stamps `acked_at` for the calling party and sets the item
   to `ack`. Means *seen and in hand*; not finished. A bare ack writes no prose,
   so it also emits an `event` line — otherwise the raiser has nothing to notice
   and no tick fires (§9). Replying to an open item acks it too: replying is proof
   the item is in hand, and leaving it looking unattended would be worse than the
   small liberty of stamping it.
3. **Resolve** — `chat_resolve` sets `done`. Allowed only for the **author**
   (the raiser confirms satisfaction) or a consumer that has already acked and
   passes `answered: true`; `chat.py` records which, as an `event`.
4. **Prune-ok** — `chat_prune_ok` stamps `prune_ok_at` for **the calling party
   only**. A party can never speak for another.
5. **Archive** — swept automatically at the start of any `chat_read`/`chat_inbox`:
   every item where `status='done'` and every consumer row has `prune_ok_at` gets
   `is_archived = 1` plus an `event` line. Both gates required, as in xsession.

A pure announcement is raised with `chat_ask(..., announce: true)` → inserted
directly as `done`. It still waits for every consumer's prune-ok, so everyone
sees it exactly once.

### 7.3 Membership

- `chat_channel_new(name, parties[])` creates the channel and seats the caller
  plus any named parties.
- `chat_channel_join` seats the caller; `left_at` records departure.
- Consumers of an item are frozen at raise time from the then-current party list.
  A party joining later is **not** retroactively required to prune-ok — otherwise
  a newcomer would pin every open item forever.
- Membership is advisory. `chat_say`/`chat_ask` into a channel the caller has not
  joined returns an error telling them to join, but nothing prevents joining.

---

## 8. Tool surface

14 tools, all prefixed `chat_` (§2). `scope: local`.

| Tool | Args | Does |
|---|---|---|
| `chat_whoami` | `about?` | Show/refresh this party: name, workspace path, channels, unread counts. Optionally set `about`. |
| `chat_channel_new` | `name`, `about?`, `parties?[]` | Create a channel, seat caller + named parties. |
| `chat_channel_list` | `all?` | Channels I am in (or all channels), with party list, unread count, open-item count. |
| `chat_channel_join` | `name` | Seat the caller. |
| `chat_channel_leave` | `name` | Stamp `left_at`. Existing consumer obligations survive. |
| `chat_say` | `text`, `channel?`, `reply_to?`, `ref?` | Post chatter. |
| `chat_ask` | `title`, `body`, `channel?`, `slug?`, `ref?`, `consumers?[]`, `announce?` | Raise an item (`open`, or `done` if `announce`). |
| `chat_reply` | `item`, `text` | Threaded reply to an item or a `say`. |
| `chat_ack` | `item`, `note?` | Seen and in hand. |
| `chat_resolve` | `item`, `note?`, `answered?` | Mark `done` (§7.2 rule 3). |
| `chat_prune_ok` | `item` | Add **my** prune-ok. Self only. |
| `chat_read` | `channel?`, `limit?`, `since?`, `peek?`, `archived?` | Everything after my cursor; advances it unless `peek`. `archived: true` reads the archived history instead — the only way back to pruned threads, since v1 has no separate history tool. |
| `chat_inbox` | — | Cross-channel: unread counts + every item waiting on **me** (needs my ack / my answer / my prune-ok), and what waits on a peer. |
| `chat_wait` | `seconds?`, `channel?` | Bounded blocking poll (§9). Returns as soon as something for me arrives, or on timeout. |

Ergonomics:

- `channel` is optional whenever the caller is a member of exactly one channel;
  otherwise it is required and the error names the candidates.
- `item` accepts a slug (`chunk-api`) or a `channel:seq` (`mesh-gw--node-dash:14`).
- Every tool returns `display` markdown as well as `result` JSON — the human
  watching either session sees a readable transcript without the agent
  paraphrasing it.

### The ownership boundary lives in the descriptions

`xsession/SKILL.md:131-152` is the most valuable part of the skill and the part
an agent is most likely to skip. It is therefore encoded where the agent cannot
miss it — in `tool.yaml` descriptions and in `get_info()`:

- `chat_ask.description`: "Use when the answer depends on the peer's repo — their
  protocol, firmware, limits. **Ask; do not answer for them.** Never assert what
  another party's code can or cannot do."
- `chat_resolve.description`: "Only the raiser confirms satisfaction."
- `chat_prune_ok.description`: "Adds only YOUR prune-ok. You may never speak for
  another party."
- Module `about`: "Talk to sessions in other repos. You are authoritative about
  your own repo and nothing else; never edit another party's files — raise an
  item instead."

---

## 9. Getting a peer's message noticed

MCP stdio has no server→client push, and calls are capped at ~30 s (§2). Three
layers, cheapest first:

1. **Pull — `chat_inbox`.** One call answers "is anything waiting on me?" across
   all channels. This is the primitive; a companion skill (out of scope here) can
   make an agent call it at natural pauses.
2. **Tick file — for watchers.** Every message insert — chatter, items, replies
   **and events** (ack, resolve, archive, join) — overwrites `mcpp-chat/.tick`
   with a one-line payload `<iso8601> <channel> <seq> <author>`. Ticking only on
   prose was the first thing the two-process smoke test caught: a raiser waiting
   on an ack saw nothing happen. A session can arm a
   `Monitor`/`tail -F` watcher on that single small file — no DB access, no
   polling of SQLite, no markdown diffing (xsession's `--listen` had to diff a
   growing file to avoid false alarms; a tick file makes that trivial). The
   watcher reports; the session then calls `chat_read`.
3. **Bounded block — `chat_wait`.** `seconds` clamped to 1..20 (default 10),
   polling the DB every 500 ms, returning the moment something addressed to me
   lands. Deliberately under `MCPP_TIMEOUT_SECONDS=30`, and clamped again to
   `max(1, timeout - 5)` if the module can read that env. It blocks **only the
   calling session's own mcpp process** — which is that session's choice to make.
   This is what makes real turn-taking Q&A feel live instead of polled.

---

## 10. Concurrency and durability

N sessions are N processes writing one SQLite file.

- `connect()`: `isolation_level=None`, `PRAGMA journal_mode=WAL`,
  `PRAGMA foreign_keys=ON`, `row_factory=Row` — the `mcpp-plan/db.py:18-23`
  idiom, **plus `PRAGMA busy_timeout=5000`**, which mcpp-plan omits and which is
  what actually prevents `database is locked` under multi-writer load.
- Every write is a short `BEGIN IMMEDIATE` … `COMMIT` block. `seq` is allocated
  inside that transaction (`SELECT COALESCE(MAX(seq),0)+1`), so the per-channel
  ordering is safe against two sessions posting simultaneously.
- Connections are opened per call and closed in `finally` — no long-lived handle
  held across an idle session (except inside `chat_wait`, which reopens per poll).
- `ensure_schema()` + `schema_version` + ordered `schema_patches/patch-N.sql`,
  mirroring `mcpp-plan/db.py:101-268` but **without** its trial-migration
  machinery at v1; chat data is not precious enough to justify that ceremony, and
  a daily copy into `.backups/` covers the loss case.

---

## 11. Configuration

`config.py` — `DEFAULTS` deep-merged with `config.yaml`, the `mcpp-plan/config.py:59-110`
idiom, no new invention:

```yaml
identity:
  name: ""            # override the derived repo basename; "" = derive
chat:
  default_channel: "" # used when a party is in more than one channel
  wait_max_seconds: 20
  read_limit: 50
  tick_file: ".tick"  # relative to module dir; "" disables
storage:
  db_path: ""         # "" = chat.db beside this module
  daily_backup: true
  backup_retain_days: 7
```

Env overrides for tests and unusual installs: `MCPP_CHAT_DB`, `MCPP_CHAT_CONFIG`.

---

## 12. Installation

**As an mcpp module** (normal case) — one line in the host's `tools.yaml`:

```yaml
modules:
  - path: ../mcpp-chat
```

Every session that starts thereafter has `chat_*`. No per-project MCP config, no
new server entry. Because a name collision here is fatal host-wide (§2), the
README instructs: run `python3 <mcpp>/cli.py list` **before** restarting sessions.

**Standalone** — the module's own `tools.yaml` makes it a one-module server:

```json
{ "command": "python3", "args": ["<mcpp>/mcpp.py"],
  "env": { "MCPP_BASE_DIR": "<path>/mcpp-chat" } }
```

---

## 13. Worked example (goes in README verbatim)

```
mesh-gw   > chat_channel_new(name="mesh-gw--node-dash", parties=["node-dash"])
mesh-gw   > chat_ask(slug="chunk-api", title="Is the chunk header enum closed?",
                     body="I need to add a frame type. Can your generator emit a new value,
                           or do I need a workaround on my side?")
                     → OPEN, consumers: node-dash

node-dash > chat_inbox      → "1 item waits on YOU: [chunk-api] from mesh-gw"
node-dash > chat_ack("chunk-api")
node-dash > chat_reply("chunk-api", "I own the generator — adding FRAME_AUX now, pushed in ~10m.")
mesh-gw   > chat_resolve("chunk-api", note="Confirmed, no workaround needed.")
both      > chat_prune_ok("chunk-api")   → archived on the next read
```

That is the real failure the xsession skill documents at `SKILL.md:141-145` — a
session declared a peer's protobuf enum closed, built a workaround around that
belief, and the peer simply added the value. The whole design exists to make
asking cheaper than assuming.

---

## 14. Implementation order

| Milestone | Contents | Usable? |
|---|---|---|
| **M1 — talking** | repo skeleton, `schema.sql`, `db.py`, `config.py`, `chat_whoami`, `chat_channel_new/list/join`, `chat_say`, `chat_read`, `chat_inbox` | yes: two repos can chat |
| **M2 — contracts** | `chat_ask`, `chat_reply`, `chat_ack`, `chat_resolve`, `chat_prune_ok`, archive sweep, `event` lines | yes: full handshake |
| **M3 — liveness** | tick file, `chat_wait`, `chat_channel_leave`, display polish, `get_info()` | yes |
| **M4 — ship** | README, tests, LICENSE, `.gitignore`, first commit + `pacmac/mcpp-chat` remote | — |

M1-M3 were built in one pass under task `mcpp-chat-build` rather than three
separate tasks: the milestones sequence the same file set, and a module registered
for every session on the host is worth having whole. M4 is done except the remote,
which is not this task's to create. 59 tests plus a two-process smoke test back it.

Deferred, deliberately: channel close/rename, full-text search over history,
`chat_digest` (summarise a channel), cross-machine federation, and any
`identity.suffix` for same-repo sessions.

---

## 15. Decisions taken (recorded, since they were delegated)

1. **Self-sufficient repo** — own DB, config, schema, `tools.yaml`; no sibling
   imports; no host-specific paths in code or docs. (§3, user's direction.)
2. **Party = repo, full stop** — no session discriminator, per the user's
   statement that two sessions never share a repo. (§5.)
3. **`say` vs `ask`** — chatter must not pay the consensus-prune tax. This is the
   single biggest usability change from xsession. (§7.1.)
4. **One `messages` table with a per-channel `seq`** — one cursor cannot miss a
   kind. (§6.)
5. **Archive by flag, not by file move** — lossless and queryable. (§6.)
6. **Loose plan cross-link** — a free-text `ref`, no cross-DB FK, no import of
   `mcpp-plan`. Keeps §3 true. (§6.)
7. **`chat_wait` included** — a bounded 20 s block is what makes live Q&A work;
   the 30 s host cap makes it safe, and it costs only the caller. (§9.)
8. **Ownership boundary encoded in tool descriptions**, not only in docs — it is
   the rule agents actually break. (§8.)
9. **`busy_timeout` added** where mcpp-plan omits it — multi-writer is the normal
   case here, not the exception. (§10.)

## 16. Explicitly NOT changed

- `mcpp/mcpp.py` — the host needs **no** modification; the module contract as it
  stands is sufficient.
- `mcpp/tools.yaml` — the one-line registration is an install step for the user,
  and is not performed by this task (it changes another repo, and it takes effect
  for every session on the host).
- `mcpp-plan/`, `mcpp-git/` — untouched; read only as pattern references.
- `~/.claude/skills/xsession/SKILL.md` — left in place. It is the file-based
  fallback for hosts without mcpp, and the source of this protocol. Retiring it
  is a separate decision once mcpp-chat has run for real.
