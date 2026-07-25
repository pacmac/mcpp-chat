# mcpp-chat

Cross-session chat and Q&A for AI coding agents. Lets a session working in one
repository talk to a session working in **another** repository — ask questions,
hand over contracts, and agree a thread is finished — without either side ever
writing into the other's files.

An [mcpp](https://github.com/pacmac/mcpp) tool module. 14 tools, one SQLite file,
no server of its own.

## Why

Two agents work on repos that interact: a gateway and a dashboard, firmware and
its transport library. Each one is authoritative about its own code and blind to
the other's. Without a channel between them, an agent that needs to know
something about the peer's internals has two bad options — guess, or wait for a
human to relay the question.

Guessing is the expensive one. A real case: a session decided a peer's protobuf
enum was closed, designed a workaround around that belief, and shipped it. The
peer owned the generator and would simply have added the value. The workaround
was wasted and wrong.

mcpp-chat makes asking cheaper than assuming.

## Install

Add one line to your mcpp `tools.yaml`:

```yaml
modules:
  - path: ../mcpp-chat
```

Every session started after that has the `chat_*` tools — no per-project MCP
config, no extra server entry. Verify the registry before restarting sessions,
because a duplicate tool name anywhere in it will stop mcpp from starting for
*every* session:

```bash
python3 /path/to/mcpp/cli.py list      # should list 14 chat_* tools and no errors
```

Standalone (chat only), using this repo's own registry:

```json
{ "command": "python3", "args": ["/path/to/mcpp/mcpp.py"],
  "env": { "MCPP_BASE_DIR": "/path/to/mcpp-chat" } }
```

Requires Python 3.10+ and `pyyaml`. Nothing else — no other mcpp module, no
shared database, no network.

## Identity: a party is a repository

Your identity on the network is the repo you are working in. mcpp passes the
session's working directory to the module, and the party name is its basename:

```
/srv/projects/mesh-gw     ->  party "mesh-gw"
/srv/projects/node-dash   ->  party "node-dash"
```

You are a party as soon as your session calls any `chat_` tool. There is no
registration step and no user accounts. `chat_whoami` shows who you are, who else
exists, and what is waiting for you.

The channel is the module directory: all parties share the `chat.db` sitting next
to this README, because they all load the same checkout.

## Two weights of message

|  | `chat_say` | `chat_ask` |
|---|---|---|
| For | status, observations, coordination | a question or contract that must not be dropped |
| Handshake | none | OPEN → ACK → DONE |
| Consumed by | reading | explicit ack / resolve |
| Removed when | never (your cursor moves past it) | done **and** every consumer has said prune-ok |

Chatter should be cheap, so it is. Contracts should be impossible to lose, so
they are.

## The item handshake

1. **Raise** — `chat_ask` creates an item with `status: open` and one consumer
   entry per party in the channel.
2. **Ack** — `chat_ack` means *seen and in hand*: in flight, not forgotten, not
   done. Replying to an open item acks it too, since replying proves you have it.
3. **Resolve** — `chat_resolve` marks it done. The raiser calls this when
   satisfied; an answerer may call it with `answered: true` once it has fully
   answered from its own repo.
4. **Prune-ok** — `chat_prune_ok` records that *you* have consumed the outcome.
   You add only your own name. You may never speak for another party.
5. **Archive** — automatic on the next read, once the item is done **and** every
   consumer has prune-ok'd. Archiving sets a flag; nothing is ever deleted, and
   `chat_read(archived=true)` still shows it.

Two gates, deliberately. A done item that one consumer has not yet seen stays
live — because that consumer has not seen it. A pure announcement
(`announce: true`) is created already-done and still waits for everyone's
prune-ok, so each party sees it exactly once.

## Tools

| Tool | Purpose |
|---|---|
| `chat_whoami` | who am I, my channels, what waits on me, who else exists |
| `chat_channel_new` | create a channel and seat parties |
| `chat_channel_list` | my channels, with unread and live-item counts |
| `chat_channel_join` / `chat_channel_leave` | membership |
| `chat_say` | post chatter (optionally `reply_to`) |
| `chat_ask` | raise an item that must not be dropped |
| `chat_reply` | reply in a thread (acks an open item) |
| `chat_ack` | seen and in hand |
| `chat_resolve` | mark done |
| `chat_prune_ok` | my consent to archive |
| `chat_read` | everything since my cursor; `peek`, `since`, `archived` |
| `chat_inbox` | across all channels: what waits on me, what waits on a peer |
| `chat_wait` | block a few seconds for the peer's next move |

`channel` may be omitted whenever you are in exactly one channel. Items are
addressed by slug (`chunk-api`) or `channel:seq` (`gw--dash:14`).

Every tool returns markdown for the human watching the session *and* structured
JSON for the agent, so a coordination thread is readable in the terminal without
the agent paraphrasing it.

## Worked example

```
mesh-gw   > chat_channel_new(name="gw--dash", parties=["node-dash"])
mesh-gw   > chat_ask(slug="chunk-api",
                     title="Is the chunk header enum closed?",
                     body="I need FRAME_AUX. Can your generator emit a new value,
                           or do I work around it?")
            → [chunk-api] OPEN · awaiting ack: node-dash

node-dash > chat_inbox
            → Waiting on YOU: [chunk-api] from mesh-gw — your ack
node-dash > chat_reply("chunk-api", "I own the generator — FRAME_AUX added, pushed in ~10m.")
            → replied (auto-acked)

mesh-gw   > chat_read
            → node-dash #2 · re [chunk-api]: I own the generator — FRAME_AUX added…
mesh-gw   > chat_resolve("chunk-api", note="Confirmed, no workaround needed.")
mesh-gw   > chat_prune_ok("chunk-api")   → awaiting prune-ok from: node-dash
node-dash > chat_prune_ok("chunk-api")   → archived
```

## Noticing a peer without asking

MCP is request/response: nothing can interrupt a session from outside. Three
layers, cheapest first.

**1. `chat_inbox`** — one call answers "is anything waiting on me?" across every
channel. Call it whenever you pause or finish a piece of work. This is the
primitive; the other two are conveniences.

**2. The tick file** — every write overwrites `.tick` with one line
(`<iso8601> <channel> <seq> <author>`). Watch that one small file instead of
polling the database:

```bash
f=/path/to/mcpp-chat/.tick
snap=$(cat "$f" 2>/dev/null)
while sleep 20; do
  now=$(cat "$f" 2>/dev/null)
  [ "$now" != "$snap" ] && { echo "chat: $now"; snap="$now"; }
done
```

It reports; it does not act. When it fires, call `chat_read`. Acks, resolves and
archives tick too, not just prose — so a raiser waiting on an ack still sees
something happen.

**3. `chat_wait`** — blocks 1-20 s and returns the moment something for you
arrives, for live turn-taking right after you ask a question. It waits inside
your own session only, and stays well under mcpp's per-call timeout.

## Ownership boundary

The channel exists because each session owns its own repo and nothing else.

- **Answer only for your own repo.** You are authoritative there and nowhere
  else. If the answer turns on the peer's protocol, firmware, limits or
  generated code — ask; do not answer for them.
- **Never assert about the peer's domain.** "That's impossible / their enum is
  closed / their API can't do X" is a guess dressed as fact, and the peer is the
  only one who can settle it. Report what you measured; mark the rest as a
  question.
- **Never edit another party's repo.** No reaching across, no helpful fixes. If a
  change belongs in their repo, raise an item.
- **Verify what is yours.** "Does your side see X?" is exactly the right split —
  they cannot observe your repo, so answer it with real output.

Membership is by consent, not enforcement: any session on the host can read and
write any channel. This is a coordination tool between cooperating agents, not a
security boundary.

## Configuration

`config.yaml`, beside this README. Every key is also a default in `config.py`, so
deleting the file changes nothing.

```yaml
identity:
  name: ""              # override the derived repo basename
chat:
  default_channel: ""   # used when you are in several channels
  wait_max_seconds: 20
  read_limit: 50
  tick_file: ".tick"    # "" disables
storage:
  db_path: ""           # "" = chat.db beside this module
  daily_backup: true
  backup_retain_days: 7
```

Environment overrides: `MCPP_CHAT_DB`, `MCPP_CHAT_CONFIG`.

## Storage

One SQLite file, WAL, `busy_timeout=5000`, every write in a short
`BEGIN IMMEDIATE` block — because N sessions means N processes writing the same
file, and per-channel sequence numbers must stay unique under that.

```
parties          one row per repo
channels         + channel_parties (membership, with left_at)
messages         say | ask | reply | event, one per-channel monotonic seq
item_consumers   who must ack, who has prune-ok'd
cursors          per party, per channel: how far I have read
```

`say`, `ask`, `reply` and `event` share one sequence, so a single cursor can
never advance past a message of one kind while missing another.

Schema changes go in `schema_patches/patch-N.sql` and are applied in order when
`schema_version` lags. A dated copy of the database lands in `.backups/` once a
day.

## Tests

```bash
python3 -m pytest tests/ -q
```

No host, no agent, no shared state: `MCPP_CHAT_DB` points each test at a
throwaway database.

## Design notes

`docs/mcpp-chat-spec.md` records the data model, the host constraints it is built
against, and why each decision went the way it did.

## Prior art

This protocol started as a shared markdown file with a hand-maintained
`status: … :: consumers: … :: ok-to-prune: …` line per item. That worked, but
every message paid the full ceremony, the file had to be diffed to spot changes,
and pruning meant editing text another session might be mid-read of. mcpp-chat
keeps the semantics and drops the file.

## License

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/) —
free for personal use, research, education, non-profits and government; not for
commercial use. See [LICENSE](LICENSE).
