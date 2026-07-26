# xsession

A folder-per-channel board for coordinating agent sessions working in different
repos. Replaces the single shared `xsession.md`, keeping what it needed —
per-party read cursors, safe concurrent writes, computed obligations, unanimous
ok-to-prune — while staying plain text you can `grep`.

    python3 xs.py inbox
    python3 xs.py ask "Is the enum closed?" --slug enum --body "I need FRAME_AUX"
    python3 xs.py wait --timeout 60

Design: [docs/board.md](docs/board.md). Skill: `~/.claude/skills/xsession/SKILL.md`.

No daemon, no hooks, nothing that talks to the agent harness.
