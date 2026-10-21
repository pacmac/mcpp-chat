# TIDY rename the folder to claude/mcpp-chat

The GitHub repo is `pacmac/mcpp-chat`; the folder was `claude/xsession`. One name for
the code: the folder becomes `/usr/share/pac/dev/py/claude/mcpp-chat`.

**Unchanged on purpose:** the skill name `xsession`, the `xs_*` tool names, the `xs`
module name in `tool.yaml`, and the board at `~/.xsession`. Every peer session's
instructions use those; renaming them buys nothing.

## Files in scope

| file | change |
|---|---|
| `../../mcpp/tools.yaml` | `- path: ../claude/xsession` → `../claude/mcpp-chat` |
| `~/.claude/CLAUDE.md` | listener command path |
| `~/.claude/hooks/xsession-listen.py` | `XS` constant and docstring path |
| `~/.claude/skills/xlead/SKILL.md` | CLI path |
| `~/.claude/skills/handover/SKILL.md` | CLI path |
| `~/.claude/skills/xsession/SKILL.md` | symlink target |
| `README.md`, `skill/SKILL.md`, `SPEC.md` | paths in this repo |

## Compatibility

`claude/xsession` stays as a symlink to `claude/mcpp-chat`, so running mcpp processes,
live peer sessions and historical docs in other repos (node-dash QA notes, rotator
HANDOVER) keep resolving. Remove it once no session still uses the old path.

## Not touched

`~/.claude/xsession*.md` are the deprecated flat-file channels and refer to themselves,
not to this folder.
