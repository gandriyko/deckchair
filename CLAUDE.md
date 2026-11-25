# Deckchair

macOS desktop control (mouse, keyboard, screenshots, screen recording) as a stdio MCP server.
Everything is in `server.py`; `check.py` is the self-check; `skills/deckchair/SKILL.md` is the agent
skill; `README.md` is the user guide. UI text, docs and code comments are English.

## Git

Work, commit and push straight on `main`. Never create branches (no feature branches, no PRs)
unless asked. The remote is `git@github-gandriyko:gandriyko/deckchair.git` (SSH host alias).

## Rules

- One file, two dependencies: `mcp<2` (`mcp` 2.x renamed FastMCP) and `pyobjc-framework-Quartz`,
  declared inline (PEP 723) and run with `uv run --script`. No `pyproject.toml`, no pyautogui, no Pillow.
- After every change run `uv run --script check.py`. It takes the lock: stop a session that uses
  Deckchair first. Then the user reconnects the server in `/mcp`; a newly added server needs a new session.
- Every new tool or changed tool behaviour goes into `README.md` (Tools table) and
  `skills/deckchair/SKILL.md` in the same commit.
- Every action tool calls `_check_abort()` first (fail-safe + lock) and returns `_after_action()`.
  `type_text` checks before each character.

## Gotchas (each one cost a debugging round)

- **RDP keyboard.** Windows App reads the key code, not the Unicode string, and checks modifier
  state through the HID system state and the left/right device bits. Keep all three: events from
  `EVENT_SOURCE` (`kCGEventSourceStateHIDSystemState`), modifiers as `kCGEventFlagsChanged` with
  `NX_DEVICEL*KEYMASK` bits in `MODIFIERS`, and a real key code plus the Unicode string per character.
- **pyautogui-style modifiers fail.** A modifier key-down without flags on the event never reaches
  `cmd+tab` / `cmd+space`.
- **Screenshot stall.** `CGWindowListCreateImage` waits 30 s (and `CGDisplayCreateImage` fails after 5 s)
  while another live process of the same Python binary has captured the screen. The lock prevents it
  between Deckchair processes. `screencapture` (the CLI) is not affected; files use it.
- **Coordinates.** Screenshots are scaled to the display size in points, so image pixels equal
  `CGEvent` coordinates on Retina.
- **Scroll in RDP.** One `kCGScrollEventUnitLine` event per wheel tick from `EVENT_SOURCE`: 10 ticks
  move an RDP grid ~160 px. pyautogui's scroll moved it ~16 px.
- **Focus.** A terminal multiplexer (Herdr) can take focus after file edits; the agent re-activates
  the target app (Dock click) before acting.
