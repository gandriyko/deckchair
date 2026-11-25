# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2", "pyobjc-framework-Quartz"]
# ///
import atexit
import fcntl
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import Foundation
import Quartz
from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("deckchair")

SETTLE_SECONDS = 0.4
KEY_DELAY_SECONDS = 0.03
CORNER_TOLERANCE = 1
LOCK_PATH = Path(tempfile.gettempdir()) / "deckchair.lock"
held_lock = []

# RDP/VM clients read modifier state from the HID system state; events without this source don't update it
EVENT_SOURCE = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)

# US ANSI virtual keycodes
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
    "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "`": 50, " ": 49, "\n": 36, "\t": 48,
}
SHIFTED = {
    "~": "`", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9",
    ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".",
    "?": "/",
}
NAMED_KEYS = {
    "enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51, "delete": 117, "esc": 53,
    "escape": 53, "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "left": 123, "right": 124,
    "down": 125, "up": 126, "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
# Device-dependent left-key bits (NX_DEVICEL*KEYMASK): RDP clients use them to tell left/right modifiers apart
MODIFIERS = {
    "cmd": (55, Quartz.kCGEventFlagMaskCommand | 0x08),
    "command": (55, Quartz.kCGEventFlagMaskCommand | 0x08),
    "shift": (56, Quartz.kCGEventFlagMaskShift | 0x02),
    "alt": (58, Quartz.kCGEventFlagMaskAlternate | 0x20),
    "option": (58, Quartz.kCGEventFlagMaskAlternate | 0x20),
    "ctrl": (59, Quartz.kCGEventFlagMaskControl | 0x01),
    "control": (59, Quartz.kCGEventFlagMaskControl | 0x01),
}
MODIFIER_KEYCODES = {keycode for keycode, flag in MODIFIERS.values()}
MOUSE_BUTTONS = {
    "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft),
    "right": (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight),
    "middle": (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp, Quartz.kCGMouseButtonCenter),
}


class AbortedByUser(Exception):
    pass


def _screen_size() -> tuple[int, int]:
    bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return int(bounds.size.width), int(bounds.size.height)


def _mouse_position() -> tuple[float, float]:
    location = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return location.x, location.y


def _acquire_lock() -> None:
    # One desktop controller at a time: two processes would fight over the mouse, and a second process that has
    # captured the screen makes every legacy Quartz capture in the first one stall for 30 s.
    # Taken on first use, not at startup, because every Claude session spawns this server; released on exit.
    if held_lock:
        return
    handle = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        owner = handle.read().strip() or "unknown"
        handle.close()
        raise RuntimeError(
            f"Deckchair is already in use by another process (PID {owner}). Only one session may control the "
            f"desktop. Close that Claude Code session or stop the process: kill {owner}"
        )
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    held_lock.append(handle)


def _check_abort() -> None:
    _acquire_lock()
    x, y = _mouse_position()
    width, height = _screen_size()
    at_x_edge = x <= CORNER_TOLERANCE or x >= width - 1 - CORNER_TOLERANCE
    at_y_edge = y <= CORNER_TOLERANCE or y >= height - 1 - CORNER_TOLERANCE
    if at_x_edge and at_y_edge:
        raise AbortedByUser(
            "ABORTED BY USER: the mouse is in a screen corner. Stop all desktop actions and ask the user how to proceed."
        )


def _capture_jpeg() -> bytes:
    width, height = _screen_size()
    bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    # ponytail: deprecated CGWindowListCreateImage (~25 ms). It stalls 30 s while another process of the same Python
    # binary has captured the screen; the lock prevents that between Deckchair processes. Move to ScreenCaptureKit
    # if a macOS release removes the API, or fall back to the screencapture CLI (~160 ms).
    image = Quartz.CGWindowListCreateImage(
        bounds, Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault
    )
    if image is None:
        raise RuntimeError("Screen capture failed: grant Screen Recording to the terminal app and restart it.")
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(), Quartz.kCGImageAlphaNoneSkipLast
    )
    Quartz.CGContextSetInterpolationQuality(context, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)
    scaled = Quartz.CGBitmapContextCreateImage(context)
    data = Foundation.NSMutableData.data()
    destination = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    Quartz.CGImageDestinationAddImage(
        destination, scaled, {Quartz.kCGImageDestinationLossyCompressionQuality: 0.7}
    )
    Quartz.CGImageDestinationFinalize(destination)
    return bytes(data)


def _after_action() -> Image:
    # ponytail: fixed wait for the UI to redraw; raise it if RDP screenshots lag behind actions
    time.sleep(SETTLE_SECONDS)
    return screenshot()


def _post_mouse(event_type: int, x: float, y: float, button: int, click_state: int = 1) -> None:
    event = Quartz.CGEventCreateMouseEvent(EVENT_SOURCE, event_type, (x, y), button)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, click_state)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _move_to(x: float, y: float) -> None:
    _post_mouse(Quartz.kCGEventMouseMoved, x, y, Quartz.kCGMouseButtonLeft)
    time.sleep(0.05)


def _post_key(keycode: int, is_down: bool, flags: int, char: str | None = None) -> None:
    event = Quartz.CGEventCreateKeyboardEvent(EVENT_SOURCE, keycode, is_down)
    Quartz.CGEventSetFlags(event, flags)
    if keycode in MODIFIER_KEYCODES:
        Quartz.CGEventSetType(event, Quartz.kCGEventFlagsChanged)
    if char:
        Quartz.CGEventKeyboardSetUnicodeString(event, len(char.encode("utf-16-le")) // 2, char)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    time.sleep(KEY_DELAY_SECONDS)


def _press(keycode: int, modifiers: list[tuple[int, int]], char: str | None = None) -> None:
    flags = 0
    for modifier_keycode, flag in modifiers:
        flags |= flag
        _post_key(modifier_keycode, True, flags)
    _post_key(keycode, True, flags, char)
    _post_key(keycode, False, flags, char)
    for modifier_keycode, flag in reversed(modifiers):
        flags &= ~flag
        _post_key(modifier_keycode, False, flags)


def _char_key(char: str) -> tuple[int, bool] | None:
    if char in KEYCODES:
        return KEYCODES[char], False
    if char in SHIFTED:
        return KEYCODES[SHIFTED[char]], True
    if char.isascii() and char.isupper():
        return KEYCODES[char.lower()], True
    return None


@mcp.tool()
def screenshot() -> Image:
    """Screenshot of the main display. Image pixel coordinates equal click coordinates.
    Every action tool already returns a screenshot taken after the action; call this only to look without acting."""
    _acquire_lock()
    return Image(data=_capture_jpeg(), format="jpeg")


@mcp.tool()
def click(x: int, y: int, button: str = "left", clicks: int = 1) -> Image:
    """Click at (x, y). button: left | right | middle. clicks=2 for double click."""
    _check_abort()
    down, up, button_code = MOUSE_BUTTONS[button]
    _move_to(x, y)
    for click_state in range(1, clicks + 1):
        _post_mouse(down, x, y, button_code, click_state)
        _post_mouse(up, x, y, button_code, click_state)
        time.sleep(0.05)
    return _after_action()


@mcp.tool()
def move(x: int, y: int) -> Image:
    """Move the mouse to (x, y) without clicking (hover)."""
    _check_abort()
    _move_to(x, y)
    return _after_action()


@mcp.tool()
def drag(from_x: int, from_y: int, to_x: int, to_y: int) -> Image:
    """Drag with the left button from one point to another."""
    _check_abort()
    _move_to(from_x, from_y)
    _post_mouse(Quartz.kCGEventLeftMouseDown, from_x, from_y, Quartz.kCGMouseButtonLeft)
    steps = 20
    for step in range(1, steps + 1):
        x = from_x + (to_x - from_x) * step / steps
        y = from_y + (to_y - from_y) * step / steps
        _post_mouse(Quartz.kCGEventLeftMouseDragged, x, y, Quartz.kCGMouseButtonLeft)
        time.sleep(0.02)
    _post_mouse(Quartz.kCGEventLeftMouseUp, to_x, to_y, Quartz.kCGMouseButtonLeft)
    return _after_action()


@mcp.tool()
def scroll(x: int, y: int, amount: int) -> Image:
    """Scroll at (x, y) by `amount` wheel ticks. Positive scrolls up, negative scrolls down."""
    _check_abort()
    _move_to(x, y)
    direction = 1 if amount > 0 else -1
    for tick in range(abs(amount)):
        event = Quartz.CGEventCreateScrollWheelEvent(EVENT_SOURCE, Quartz.kCGScrollEventUnitLine, 1, direction)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.02)
    return _after_action()


@mcp.tool()
def type_text(text: str) -> Image:
    """Type text into the focused field. ASCII works everywhere, including RDP; non-ASCII (Cyrillic) works only in
    macOS apps."""
    shift = [MODIFIERS["shift"]]
    for char in text:
        _check_abort()
        key = _char_key(char)
        if key is None:
            # ponytail: keycode 0 + Unicode string; macOS apps read the string, RDP reads only the keycode and
            # types 'a'. Paste via clipboard if Cyrillic inside RDP is needed.
            _press(0, [], char)
        else:
            keycode, needs_shift = key
            _press(keycode, shift if needs_shift else [], char)
    return _after_action()


@mcp.tool()
def key(keys: str) -> Image:
    """Press a key or combo, e.g. 'enter', 'tab', 'esc', 'cmd+tab', 'cmd+space', 'shift+home', 'ctrl+alt+delete'."""
    _check_abort()
    *modifier_names, name = keys.lower().split("+")
    modifiers = [MODIFIERS[modifier] for modifier in modifier_names]
    keycode = NAMED_KEYS.get(name, KEYCODES.get(name))
    if keycode is None:
        raise ValueError(f"Unknown key: {name}")
    _press(keycode, modifiers)
    return _after_action()


def _output_path(path: str, suffixes: tuple[str, ...]) -> Path:
    target = Path(path).expanduser().resolve()
    if target.suffix.lower() not in suffixes:
        raise ValueError(f"Path must end with one of {', '.join(suffixes)}: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


@mcp.tool()
def save_screenshot(path: str) -> str:
    """Save a full-resolution screenshot of the main display to a file (.png or .jpg) and return its path.
    Use it to keep evidence for the user; use `screenshot` to look at the screen yourself."""
    _acquire_lock()
    target = _output_path(path, (".png", ".jpg", ".jpeg"))
    image_format = "png" if target.suffix.lower() == ".png" else "jpg"
    subprocess.run(["screencapture", "-x", "-m", "-t", image_format, str(target)], check=True, timeout=15)
    return str(target)


recording = {}


@mcp.tool()
def start_recording(path: str) -> str:
    """Start recording the main display to a .mov file, with mouse clicks shown. Stop it with `stop_recording`."""
    _acquire_lock()
    if recording:
        raise RuntimeError(f"Already recording to {recording['path']}. Call stop_recording first.")
    target = _output_path(path, (".mov",))
    process = subprocess.Popen(
        ["screencapture", "-v", "-x", "-k", "-m", str(target)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    time.sleep(1)
    if process.poll() is not None:
        raise RuntimeError(f"Recording failed to start: {process.stderr.read().decode().strip()}")
    recording.update(process=process, path=target, started=time.monotonic())
    return f"Recording to {target}"


@mcp.tool()
def stop_recording() -> str:
    """Stop the recording started by `start_recording` and return the file path, duration and size."""
    if not recording:
        raise RuntimeError("Not recording.")
    process, target, started = recording["process"], recording["path"], recording["started"]
    recording.clear()
    process.send_signal(signal.SIGINT)
    process.wait(timeout=30)
    if not target.exists():
        raise RuntimeError(f"Recording stopped but no file was written: {target}")
    seconds = time.monotonic() - started
    return f"{target} ({seconds:.0f} s, {target.stat().st_size // 1024} KB)"


@atexit.register
def _stop_recording_on_exit() -> None:
    if recording:
        recording["process"].send_signal(signal.SIGINT)
        recording["process"].wait(timeout=30)


if __name__ == "__main__":
    mcp.run()
