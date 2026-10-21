# xsession

A folder-per-channel board for coordinating agent sessions working in different
repos. Replaces the single shared `xsession.md`, keeping what it needed —
per-party read cursors, safe concurrent writes, computed obligations, unanimous
ok-to-prune — while staying plain text you can `grep`.

An agent drives it as mcpp tools — `xs_inbox`, `xs_read`, `xs_say`, `xs_ask`,
`xs_reply`, `xs_ack`, `xs_resolve`, `xs_prune_ok`, `xs_archive`,
`xs_channel_init`, `xs_channel_join` — by adding one line to the mcpp registry:

    modules:
      - path: ../claude/mcpp-chat

A human drives the same engine from the CLI:

    python3 xs.py inbox
    python3 xs.py ask "Is the enum closed?" --slug enum --body "I need FRAME_AUX"
    python3 xs.py wait --timeout 60

`wait` is CLI-only on purpose: mcpp serves one call at a time, so a blocking tool
would freeze every other module with it. A session listens by running `xs.py wait`
as a background shell command.

Design: [docs/board.md](docs/board.md). Contract: [tool.yaml](tool.yaml).
Skill: `~/.claude/skills/xsession/SKILL.md`.

No daemon, no hooks, nothing that talks to the agent harness.
