# Deckchair

macOS desktop control (mouse, keyboard, screenshots, screen recording) as a stdio MCP server.
Everything is in `server.py`; `check.py` is the self-check; `skills/deckchair/SKILL.md` is the agent
skill; `README.md` is the user guide. UI text, docs and code comments are English.

## Git

Work, commit and push straight on `main`. Never create branches (no feature branches, no PRs)
unless asked. The remote is `git@github-gandriyko:gandriyko/deckchair.git` (SSH host alias).

## Rules

- One file, four dependencies: `mcp<2` (`mcp` 2.x renamed FastMCP), `pyobjc-framework-Quartz`,
  `pyobjc-framework-Vision` and `pyobjc-framework-ScreenCaptureKit`, declared inline (PEP 723) and run with `uv run --script`. No `pyproject.toml`, no pyautogui, no Pillow.
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
- **Tool output.** Tools are `@mcp.tool(structured_output=False)` (needs `mcp>=1.10`): a return type of
  `Image | str` otherwise fails pydantic output-schema generation at import, and plain text beats the
  `{"result": ...}` wrapper. Acting tools take `screenshot: bool = True`; inside them the name shadows
  the `screenshot` tool, so they call `_after_action(screenshot)` / `_finish(screenshot)`, never `screenshot()`.
- **pyautogui-style modifiers fail.** A modifier key-down without flags on the event never reaches
  `cmd+tab` / `cmd+space`.
- **Capture API.** ScreenCaptureKit (`SCScreenshotManager`, ~37 ms with pixels read; ~200 ms the first
  time in a process), falling back to the deprecated `CGWindowListCreateImage` (~30 ms with pixels; it
  returns in 12 ms and renders lazily). SCK is async: `_sck_wait` waits on a `threading.Event`, the
  handler runs on a background queue. `SCShareableContent` (~50 ms) is cached per display.
- **Screenshot stall.** While another live process of the same Python binary has captured the screen,
  SCK doesn't answer at all and `CGWindowListCreateImage` waits 30 s (`CGDisplayCreateImage` fails after
  5 s). The lock prevents it between Deckchair processes; stop the session's server before running
  `check.py` or a test script. `screencapture` (the CLI) is not affected; files use it.
- **Coordinates are screenshot pixels.** A screenshot is the display in points, shrunk to
  `SCREENSHOT_MAX_EDGE` (1568) on the long edge: Claude Code shrinks wider images before the model sees
  them (a 2304 px screenshot showed at 2000, every click missed by 15%). `_points_per_pixel()` scales
  back: `_post_mouse` multiplies and adds the selected display's origin (negative x left of the main),
  `_mouse_position` reverses it, `_capture_native` returns native pixels per screenshot pixel for crops.
  `CGWarpMouseCursorPosition` and `_mouse_global` stay in global points.
- **OCR needs small tiles of large text.** Vision shrinks a big image and loses small text: on a
  3840x2160 pt display the full screen gave 9 lines, its lower half 70. `find_text` runs on ~400 px tiles
  (40 px overlap), each upscaled to at least 4 native pixels per screenshot pixel (a 6 px Dock tooltip was
  found only upscaled), and grown to 1:8 if thin (a 1568x40 strip gave nothing). Longest lines win over
  tile-edge fragments; lines centred outside the request are dropped. Full screen: 360 lines in ~1.8 s.
  `minimumTextHeight` is already 0 by default; changing it doesn't help.
- **Displays.** `_displays()` puts the main display first; `screencapture -D` numbers them the same way
  (checked with a 5K main display and the built-in one). The fail-safe checks the corners of every display.
- **Scroll in RDP.** One `kCGScrollEventUnitLine` event per wheel tick from `EVENT_SOURCE`: 10 ticks
  move an RDP grid ~160 px. pyautogui's scroll moved it ~16 px.
- **Video frames.** `screencapture -v` writes a frame only when the screen changes (variable rate,
  120 Hz time base). Normalise to `fps=15` before `mpdecimate`, or `setpts` plays it at the wrong speed.
  Never drop `max=2` from `mpdecimate`: without it still frames vanish and toasts only flash. The video
  rules come from the `browser-qa` skill (`~/.claude/skills/browser-qa/SKILL.md`, section 6).
- **Focus.** A terminal multiplexer (Herdr) can take focus after file edits; the agent re-activates
  the target app with `activate_app` before acting.
