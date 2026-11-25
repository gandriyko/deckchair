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

## Evidence

- `save_screenshot(path)` writes a full-resolution `.png` / `.jpg` and returns its path.
- `start_recording(path)` records the screen to a `.mov` with clicks shown; `stop_recording()` returns
  the path, length and size. Always stop a recording you started, also after an error.
- Use absolute paths the user can open, for example in the project or `~/Desktop`.
