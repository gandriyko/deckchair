# Deckchair

macOS desktop control (mouse, keyboard, screenshots, screen recording) as a stdio MCP server.
Everything is in `server.py`; `check.py` is the self-check; `skills/deckchair/SKILL.md` is the agent
skill; `README.md` is the user guide. UI text, docs and code comments are English.

## Git

Work, commit and push straight on `main`. Never create branches (no feature branches, no PRs)
unless asked. The remote is `git@github-gandriyko:gandriyko/deckchair.git` (SSH host alias).

## Rules

- One file, three dependencies: `mcp<2` (`mcp` 2.x renamed FastMCP), `pyobjc-framework-Quartz` and
  `pyobjc-framework-Vision`, declared inline (PEP 723) and run with `uv run --script`. No `pyproject.toml`, no pyautogui, no Pillow.
  Video encoding calls the `ffmpeg` / `ffprobe` binaries when present; everything else works without them.
- After every change run `uv run --script check.py`. It takes the lock: stop a session that uses
  Deckchair first. Then the user reconnects the server in `/mcp`; a newly added server needs a new session.
- Every new tool or changed tool behaviour goes into `README.md` (Tools table) and
  `skills/deckchair/SKILL.md` in the same commit.
- Every action tool calls `_check_abort()` first (fail-safe + lock) and returns `_after_action()`.
  `type_text` checks before each character.
- Every tool that acts gets `@_logged` under `@mcp.tool()` (the action log). Tools that only look
  (`screenshot`, `zoom`, `find_text`) don't. `check.py` writes its own calls to the real log too.

## Gotchas (each one cost a debugging round)

- **RDP keyboard.** Windows App reads the key code, not the Unicode string, and checks modifier
  state through the HID system state and the left/right device bits. Keep all three: events from
  `EVENT_SOURCE` (`kCGEventSourceStateHIDSystemState`), modifiers as `kCGEventFlagsChanged` with
  `NX_DEVICEL*KEYMASK` bits in `MODIFIERS`, and a real key code plus the Unicode string per character.
- **Non-ASCII in RDP.** Key codes can't carry Cyrillic, so `type_text` pastes through the clipboard.
  Windows App syncs the Mac clipboard to the remote side only when it becomes the active app: without
  the Finder-and-back switch every paste repeats the first text, even 2 s later. The remote fetches the
  data on paste, asynchronously: restore the clipboard 1 s after `ctrl+v`, and use one paste per call
  (several quick pastes arrive out of order).
- **Front app is stale without a run loop.** `NSWorkspace.frontmostApplication()` keeps returning the
  app that was in front at the first call: `_frontmost_app()` spins the run loop 10 ms first.
- **Vision.** `VNImageRequestHandler.initWithCGImage_options_` needs `None`, not `{}` ("key does not
  exist"). No Hebrew model (`supportedRecognitionLanguagesAndReturnError_` lists the languages). The very
  first OCR call on a Mac took 34 s; later calls take 90–230 ms.
- **pyautogui-style modifiers fail.** A modifier key-down without flags on the event never reaches
  `cmd+tab` / `cmd+space`.
- **Screenshot stall.** `CGWindowListCreateImage` waits 30 s (and `CGDisplayCreateImage` fails after 5 s)
  while another live process of the same Python binary has captured the screen. The lock prevents it
  between Deckchair processes. `screencapture` (the CLI) is not affected; files use it.
- **Coordinates.** Screenshots are scaled to the display size in points, so image pixels equal
  `CGEvent` coordinates on Retina.
- **Scroll in RDP.** One `kCGScrollEventUnitLine` event per wheel tick from `EVENT_SOURCE`: 10 ticks
  move an RDP grid ~160 px. pyautogui's scroll moved it ~16 px.
- **Video frames.** `screencapture -v` writes a frame only when the screen changes (variable rate,
  120 Hz time base). Normalise to `fps=15` before `mpdecimate`, or `setpts` plays it at the wrong speed.
  Never drop `max=2` from `mpdecimate`: without it still frames vanish and toasts only flash. The video
  rules come from the `browser-qa` skill (`~/.claude/skills/browser-qa/SKILL.md`, section 6).
- **Focus.** A terminal multiplexer (Herdr) can take focus after file edits; the agent re-activates
  the target app with `activate_app` before acting.
