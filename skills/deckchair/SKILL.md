---
name: deckchair
description: Drive the macOS desktop through the Deckchair MCP tools (mcp__deckchair__*): screenshots, mouse clicks, drags, scrolling, typing, key combos, saving screenshots to files and recording the screen to video. Use when the user asks to operate a desktop app outside the browser, work in an app inside a Remote Desktop (RDP / Windows App) session such as an ERP, check something on screen, or record a screen video or screenshots as evidence, and the deckchair MCP server is connected.
---

# Deckchair: drive the macOS desktop

The tools act on the user's real screen with the user's real mouse and keyboard. There is no
sandbox and no undo. Act like a careful human operator.

## Rules for agents

- **Look first.** Start with `screenshot`. Every action tool returns a screenshot taken 0.4 s after
  the action; read it before the next step. Call `screenshot` again only to wait for something slow
  (an animation, a report loading, RDP lag).
- **Coordinates are image pixels.** Click the centre of the target as it appears in the latest
  screenshot. Never reuse coordinates from an older screenshot after the layout changed.
- **Bring the app to the front first.** Another window (the terminal) may hold focus. Click the app's
  Dock icon, then check the screenshot shows it in front.
- **Stop on abort.** An error with `ABORTED BY USER` means the user pushed the mouse into a corner.
  Stop all desktop actions at once, take no screenshot, and ask the user how to proceed.
- **Lock error.** "Deckchair is already in use by another process" means another session drives the
  desktop. Tell the user the PID from the message; do not kill it yourself.
- **Never type secrets.** No passwords, tokens or card numbers. The user signs in to RDP and apps.
- **Confirm before irreversible actions.** Before Save / Add / Post / Send / Delete / Approve, or any
  button that changes data, tell the user exactly what you will do and wait for a yes. Reading,
  opening reports and navigating need no confirmation.
- **Say what you saw.** Report values as read from the screen; if text is too small to read, take a
  `save_screenshot` and look at the full-resolution file, or zoom the app, instead of guessing.

## Keyboard

- `type_text` types into the focused field. Click the field first.
- To replace a field's text: click at its end, `key("shift+home")`, then `type_text`. `ctrl+a`
  does not select all in every app (SAP Business One ignores it).
- `key` takes `+`-joined combos: `cmd+tab`, `cmd+space`, `shift+home`, `ctrl+alt+delete`, `enter`,
  `esc`, `tab`, `pageup`, `f5`.

## Remote Desktop (Windows App)

- Clicks, menus, scroll, ASCII text and modifier combos work.
- `type_text` types ASCII only inside RDP. Cyrillic or other non-ASCII text arrives as `a`: ask the
  user to type it.
- If scrolling moves too little, click the scrollbar track instead.
- RDP redraws slowly: if the returned screenshot shows the old state, call `screenshot` once more.

## Evidence: screenshots and video

**Video or screenshots?** A flow (click → dialog → result, filter → rows) needs a video. A static
result (a value in a report, a field's look) needs 1–3 screenshots, each showing one state. Something
the screen never shows (a DB field, an API response) needs neither: say so and name the check that fits.

### Screenshots

- `save_screenshot(path)` writes the full-resolution `.png` / `.jpg` (2× on Retina). Never scale it
  down: crop only, the text must stay readable.
- Read the saved file to check the result is in the frame.

### Video

- Name it `~/Downloads/<ticket>-<what>.mp4`, `<what>` a few kebab-case words naming the check
  (`DA-20535-bank-balance-report.mp4`). Without a ticket, just `<what>.mp4`. One video per check; a
  re-record of the same check overwrites its file.
- **Rehearse first.** Run the scenario once without recording (reset any data it changed). The video
  shows every miss.
- Pass `region` to `start_recording` to record only the app window (for RDP: the Windows App window
  without the macOS menu bar and Dock). Take it from a screenshot: `[x, y, width, height]`.
- While recording, the mouse glides to each target and pauses 0.7 s before and after the action, so a
  viewer can follow. Don't add your own waits for that; wait only for slow loads, 1–3 s.
- Re-check coordinates in the returned screenshot before each click: dialogs and toasts shift layouts.
- Hold the final state ~2 s before `stop_recording`, so the result stays on screen.
- `stop_recording()` encodes the video: still stretches play 3× faster (`mpdecimate`, `max=2`, so a
  short toast stays visible), H.264, up to 1280 px wide. Without ffmpeg it keeps the raw `.mov`.
- Always stop a recording you started, also after an error or an abort.

### Check the video before reporting it

1. Read the contact sheet `stop_recording` returns (1 tile = the seconds it states). Every step of
   the scenario must be in a tile, with no missing step and no long run of identical tiles.
2. The duration must be plausible: far shorter than the scenario means a broken recording.
3. Report the path, duration, size and what the video shows step by step.
