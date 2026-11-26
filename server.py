# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.10,<2", "pyobjc-framework-Quartz", "pyobjc-framework-Vision", "pyobjc-framework-ScreenCaptureKit"]
# ///
import atexit
import fcntl
import functools
import inspect
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import AppKit
import Foundation
import Quartz
import ScreenCaptureKit
import Vision
from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("deckchair")

# Longest screenshot edge in pixels: larger images are shrunk by the client before the model sees them, so the
# model's coordinates would not match; 1568 is the size Anthropic recommends for images
SCREENSHOT_MAX_EDGE = 1568
SETTLE_SECONDS = 0.4
KEY_DELAY_SECONDS = 0.03
# While recording, the mouse glides to the target and waits before and after each action, so a viewer
# can follow it (the browser-qa video rules)
GLIDE_SECONDS = 0.4
VISIBLE_PAUSE_SECONDS = 0.7
VIDEO_FPS = 15
VIDEO_MAX_WIDTH = 1280
CORNER_TOLERANCE = 1
# ponytail: Windows App / Microsoft Remote Desktop only; add the bundle IDs of other RDP / VNC clients here
RDP_APPS = {"com.microsoft.rdc.macos"}
CLIPBOARD_SYNC_SECONDS = 1.0
# Apps the agent may act in: bundle ids or names, comma-separated. Unset or empty: every app.
ALLOWED_APPS = {app.strip() for app in os.environ.get("DECKCHAIR_APPS", "").split(",") if app.strip()}
# Password managers: the agent never acts in, activates or sees them; captures hide their windows.
# Matched by bundle id or by name, so a wrong or changed bundle id is still caught by the name.
PASSWORD_MANAGERS = {
    "com.1password.1password": "1Password",
    "com.agilebits.onepassword7": "1Password 7",
    "com.apple.Passwords": "Passwords",
    "com.apple.keychainaccess": "Keychain Access",
    "com.bitwarden.desktop": "Bitwarden",
    "com.lastpass.LastPass": "LastPass",
    "com.dashlane.dashlanephonefinal": "Dashlane",
    "com.keepersecurity.passwordmanager": "Keeper Password Manager",
    "com.nordsec.nordpass": "NordPass",
    "me.proton.pass.electron": "Proton Pass",
    "in.sinew.Enpass-Desktop": "Enpass",
    "org.keepassxc.keepassxc": "KeePassXC",
    "com.markmcguill.strongbox.mac": "Strongbox",
    "com.hicknhacksoftware.MacPass": "MacPass",
    "com.siber.roboform": "RoboForm",
    "com.outercorner.Secrets": "Secrets",
}
BLOCKED_APPS = set(PASSWORD_MANAGERS) | set(PASSWORD_MANAGERS.values())
# Vision OCR on this Mac has no Hebrew: Hebrew labels are not read, digits next to them are
OCR_LANGUAGES = ["en-US", "uk-UA", "ru-RU"]
OCR_MAX_ASPECT = 8
OCR_TILE = 400
OCR_TILE_OVERLAP = 40
OCR_MIN_NATIVE_SCALE = 4
# English first: on rendered "Total <hebrew> ILS <amount>" lines eng+heb read every digit, heb+eng 50%; both read
# the Hebrew 100%. A number before Hebrew ("1100 - <hebrew>") stays weak: 42%.
TESSERACT_LANGUAGES = "eng+heb"
HEBREW = re.compile("[֐-׿]")
TESSERACT_WORKERS = 8
TESSDATA_URL ="https://github.com/tesseract-ocr/tessdata_fast/raw/main/{language}.traineddata"
LOCK_PATH = Path(tempfile.gettempdir()) / "deckchair.lock"
LOG_DIR = Path.home() / ".deckchair"
TESSDATA_DIR = LOG_DIR / "tessdata"
held_lock = []
selected_display = [Quartz.CGMainDisplayID()]
sck_displays = {}
sck_blocked = {}
last_capture_api = [""]
SCK_TIMEOUT_SECONDS = 10

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


def _displays() -> list[int]:
    """Active display IDs, the main display first: the index is what list_displays / use_display show."""
    error, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    main = Quartz.CGMainDisplayID()
    return [main] + [display for display in ids[:count] if display != main]


def _display_bounds():
    """Bounds of the selected display in global points. All tool coordinates are relative to its top-left."""
    if selected_display[0] not in _displays():
        selected_display[0] = Quartz.CGMainDisplayID()
    return Quartz.CGDisplayBounds(selected_display[0])


def _points_per_pixel() -> float:
    """Screen points per screenshot pixel. Tools speak screenshot pixels: a display larger than
    SCREENSHOT_MAX_EDGE gets a smaller screenshot (the client shrinks wider images anyway, and the model's
    coordinates then miss), and every coordinate is scaled back to points here."""
    bounds = _display_bounds()
    return max(1.0, max(bounds.size.width, bounds.size.height) / SCREENSHOT_MAX_EDGE)


def _screen_size() -> tuple[int, int]:
    """The screenshot size in pixels = the coordinate range of every tool."""
    bounds = _display_bounds()
    scale = _points_per_pixel()
    return round(bounds.size.width / scale), round(bounds.size.height / scale)


def _mouse_global() -> tuple[float, float]:
    location = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return location.x, location.y


def _mouse_position() -> tuple[float, float]:
    x, y = _mouse_global()
    bounds = _display_bounds()
    scale = _points_per_pixel()
    return (x - bounds.origin.x) / scale, (y - bounds.origin.y) / scale


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
    if recording and _blocked_pids():
        # ponytail: checked only between actions; a blocked app that opens mid-wait is in the video until then
        _discard_recording()
        raise PermissionError("NOT ALLOWED: a password manager came on screen while recording; the recording is deleted.")
    x, y = _mouse_global()
    for display in _displays():
        bounds = Quartz.CGDisplayBounds(display)
        left, top = bounds.origin.x, bounds.origin.y
        right, bottom = left + bounds.size.width - 1, top + bounds.size.height - 1
        inside = left - CORNER_TOLERANCE <= x <= right + CORNER_TOLERANCE and top - CORNER_TOLERANCE <= y <= bottom + CORNER_TOLERANCE
        at_x_edge = x <= left + CORNER_TOLERANCE or x >= right - CORNER_TOLERANCE
        at_y_edge = y <= top + CORNER_TOLERANCE or y >= bottom - CORNER_TOLERANCE
        if not (inside and at_x_edge and at_y_edge):
            continue
        raise AbortedByUser(
            "ABORTED BY USER: the mouse is in a screen corner. Stop all desktop actions and ask the user how to proceed."
        )


def _app_at(x: float, y: float):
    """The app whose normal window is topmost at screenshot point (x, y); None outside app windows."""
    bounds = _display_bounds()
    scale = _points_per_pixel()
    point_x, point_y = bounds.origin.x + x * scale, bounds.origin.y + y * scale
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    # front to back; only layer 0: the menu bar and the Dock lie above every app in full-screen windows
    for window in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID):
        frame = window["kCGWindowBounds"]
        if window["kCGWindowLayer"] == 0 and frame["X"] <= point_x < frame["X"] + frame["Width"] \
                and frame["Y"] <= point_y < frame["Y"] + frame["Height"]:
            return AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(window["kCGWindowOwnerPID"])
    return None


def _is_blocked(app) -> bool:
    return bool(app and {app.bundleIdentifier(), app.localizedName()} & BLOCKED_APPS)


def _on_screen_windows() -> list[dict]:
    """On-screen windows, front to back. Occluded windows count: a capture must not depend on window order."""
    return Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)


def _blocked_pids() -> set[int]:
    """PIDs of BLOCKED_APPS with a window on screen."""
    pids = {window["kCGWindowOwnerPID"] for window in _on_screen_windows()}
    return {pid for pid in pids if _is_blocked(AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid))}


def _refuse_blocked_on_screen(what: str) -> None:
    # the screencapture CLI can't hide a window: refuse instead
    if _blocked_pids():
        raise PermissionError(
            f"NOT ALLOWED: a password manager is on screen; {what} would show it. Ask the user to close it."
        )


def _check_app(*points: tuple[float, float]) -> None:
    """Refuse to act in BLOCKED_APPS or outside DECKCHAIR_APPS: keys go to the front app, the mouse to the window
    under each point.
    ponytail: a point outside app windows (menu bar, Dock, popup menu) counts as the front app, so the Dock is
    clickable while an allowed app is in front; match the Dock's layer-20 frame if that matters."""
    for app in [_app_at(x, y) for x, y in points] or [None]:
        app = app or _running_app(_frontmost_app())
        if _is_blocked(app):
            raise PermissionError(
                f"NOT ALLOWED: {app.localizedName()} is blocked: Deckchair never acts in it. Stop and ask the user "
                f"to close it."
            )
        if ALLOWED_APPS and not {app.bundleIdentifier(), app.localizedName()} & ALLOWED_APPS:
            raise PermissionError(
                f"NOT ALLOWED: {app.localizedName()} ({app.bundleIdentifier()}) is not in DECKCHAIR_APPS "
                f"({', '.join(sorted(ALLOWED_APPS))}). Bring an allowed app to the front with activate_app, or ask "
                f"the user to allow this one."
            )


def _log_path(day: datetime | None = None) -> Path:
    """One JSONL file per local day: "what did you do today" reads one small file, old days go as whole files."""
    return LOG_DIR / f"actions-{(day or datetime.now()).strftime('%Y-%m-%d')}.jsonl"


def _logged(tool):
    """Append every call of an acting tool to the day's log (JSONL): time, tool, arguments, front app, result or
    error. Typed text is logged too: it is the audit trail of what the agent did, e.g. in an ERP."""
    signature = inspect.signature(tool)

    @functools.wraps(tool)
    def wrapper(*args, **kwargs):
        started = datetime.now().astimezone()
        entry = {
            "time": started.isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
            "tool": tool.__name__,
            "args": dict(signature.bind(*args, **kwargs).arguments),
            "app": _frontmost_app(),
        }
        try:
            result = tool(*args, **kwargs)
            entry["result"] = result if isinstance(result, str) else "ok"
            return result
        except Exception as error:
            entry["error"] = str(error)
            raise
        finally:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with _log_path(started).open("a", encoding="utf-8") as log:
                log.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return wrapper


def _capture_native():
    """The selected display at native resolution (2x on Retina) and its scale: native pixels per screenshot pixel.
    ScreenCaptureKit (~37 ms with pixels), falling back to the deprecated CGWindowListCreateImage (~30 ms) if it
    fails. Both stall while another live process of the same Python binary has captured the screen (SCK doesn't
    answer, the legacy call waits 30 s); the lock prevents that between Deckchair processes."""
    _display_bounds()
    try:
        image = _capture_sck(selected_display[0])
        last_capture_api[0] = "ScreenCaptureKit"
    except (RuntimeError, TimeoutError, KeyError):
        image = _capture_legacy()
        last_capture_api[0] = "CGWindowListCreateImage"
    return image, Quartz.CGImageGetWidth(image) / _screen_size()[0]


def _sck_wait(call) -> tuple:
    """Call a ScreenCaptureKit method with a completion handler and wait for it. The handler runs on a background
    queue, so an Event works in this run-loop-less server."""
    done = threading.Event()
    answer = {}

    def handler(*values):
        answer["values"] = values
        done.set()

    call(handler)
    if not done.wait(SCK_TIMEOUT_SECONDS):
        raise TimeoutError(f"ScreenCaptureKit did not answer in {SCK_TIMEOUT_SECONDS} s")
    return answer["values"]


def _sck_content(display_id: int) -> tuple:
    """The SCDisplay and the SCRunningApplications to hide. SCShareableContent costs ~50 ms: cached, refetched
    when a display appears or the blocked apps on screen change."""
    pids = _blocked_pids()
    if display_id not in sck_displays or sck_blocked.get("pids") != pids:
        content, error = _sck_wait(ScreenCaptureKit.SCShareableContent.getShareableContentWithCompletionHandler_)
        if content is None:
            raise RuntimeError(f"ScreenCaptureKit: no shareable content ({error})")
        sck_displays.clear()
        sck_displays.update({display.displayID(): display for display in content.displays()})
        hidden = [app for app in content.applications() if app.processID() in pids]
        if len(hidden) < len(pids):
            sck_displays.clear()
            raise RuntimeError("ScreenCaptureKit does not list a blocked app yet")
        sck_blocked.update(pids=pids, apps=hidden)
    return sck_displays[display_id], sck_blocked["apps"]


def _capture_sck(display_id: int):
    mode = Quartz.CGDisplayCopyDisplayMode(display_id)
    display, hidden = _sck_content(display_id)
    content_filter = ScreenCaptureKit.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
        display, hidden, []
    )
    config = ScreenCaptureKit.SCStreamConfiguration.alloc().init()
    config.setWidth_(Quartz.CGDisplayModeGetPixelWidth(mode))
    config.setHeight_(Quartz.CGDisplayModeGetPixelHeight(mode))
    config.setShowsCursor_(False)
    image, error = _sck_wait(
        lambda handler: ScreenCaptureKit.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            content_filter, config, handler
        )
    )
    if image is None:
        sck_displays.clear()
        raise RuntimeError(f"ScreenCaptureKit capture failed: {error}")
    return image


def _capture_legacy():
    capture = getattr(Quartz, "CGWindowListCreateImageFromArray", None)
    # every on-screen window except the blocked apps', front to back
    blocked = _blocked_pids()
    windows = [window["kCGWindowNumber"] for window in _on_screen_windows() if window["kCGWindowOwnerPID"] not in blocked]
    image = capture and capture(_display_bounds(), windows, Quartz.kCGWindowImageDefault)
    if image is None:
        raise RuntimeError("Screen capture failed: grant Screen Recording to the terminal app and restart it.")
    return image


def _crop(image, scale: float, region: list[int]):
    x, y, width, height = region
    return Quartz.CGImageCreateWithImageInRect(image, Quartz.CGRectMake(x * scale, y * scale, width * scale, height * scale))


def _encode_image(image, uti: str, properties: dict | None = None) -> bytes:
    data = Foundation.NSMutableData.data()
    destination = Quartz.CGImageDestinationCreateWithData(data, uti, 1, None)
    Quartz.CGImageDestinationAddImage(destination, image, properties)
    Quartz.CGImageDestinationFinalize(destination)
    return bytes(data)


def _capture_jpeg() -> bytes:
    width, height = _screen_size()
    image, scale = _capture_native()
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(), Quartz.kCGImageAlphaNoneSkipLast
    )
    Quartz.CGContextSetInterpolationQuality(context, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)
    scaled = Quartz.CGBitmapContextCreateImage(context)
    return _encode_image(scaled, "public.jpeg", {Quartz.kCGImageDestinationLossyCompressionQuality: 0.7})


def _after_action(show: bool = True) -> Image | str:
    # ponytail: fixed wait for the UI to redraw; raise it if RDP screenshots lag behind actions
    time.sleep(VISIBLE_PAUSE_SECONDS if recording else SETTLE_SECONDS)
    return _finish(show)


def _finish(show: bool) -> Image | str:
    # a screenshot costs the model ~2k tokens; screenshot=False skips it for steps whose result needn't be seen
    return screenshot() if show else "ok"


def _post_mouse(event_type: int, x: float, y: float, button: int, click_state: int = 1, flags: int = 0) -> None:
    bounds = _display_bounds()
    scale = _points_per_pixel()
    point = (bounds.origin.x + x * scale, bounds.origin.y + y * scale)
    event = Quartz.CGEventCreateMouseEvent(EVENT_SOURCE, event_type, point, button)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, click_state)
    if flags:
        Quartz.CGEventSetFlags(event, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _move_to(x: float, y: float) -> None:
    if recording:
        start_x, start_y = _mouse_position()
        steps = 20
        for step in range(1, steps + 1):
            _post_mouse(
                Quartz.kCGEventMouseMoved,
                start_x + (x - start_x) * step / steps,
                start_y + (y - start_y) * step / steps,
                Quartz.kCGMouseButtonLeft,
            )
            time.sleep(GLIDE_SECONDS / steps)
        time.sleep(VISIBLE_PAUSE_SECONDS)
        return
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


@mcp.tool(structured_output=False)
def screenshot() -> Image:
    """Screenshot of the selected display (the main one unless `use_display` chose another). Image pixel
    coordinates equal click coordinates.
    Every action tool already returns a screenshot taken after the action; call this only to look without acting."""
    _acquire_lock()
    return Image(data=_capture_jpeg(), format="jpeg")


@mcp.tool(structured_output=False)
@_logged
def activate_app(name: str, screenshot: bool = True) -> Image | str:
    """Bring an app to the front, launching it if needed: by name ('Windows App', 'Sublime Text') or bundle id
    ('com.microsoft.rdc.macos'). Use it instead of clicking the Dock, e.g. after the terminal took focus.
    screenshot=False returns 'ok' instead of a screenshot (~2k tokens less)."""
    _check_abort()
    if name.lower() in {app.lower() for app in BLOCKED_APPS}:
        raise PermissionError(f"NOT ALLOWED: {name} is blocked: Deckchair never opens it.")
    flag = "-b" if "." in name and " " not in name else "-a"
    result = subprocess.run(["open", flag, name], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"Cannot open {name!r}: {result.stderr.strip()}")
    front = _running_app(_frontmost_app())
    if _is_blocked(front):
        # a name that resolves to a blocked app (an alias, a path): put it away again
        front.hide()
        raise PermissionError(f"NOT ALLOWED: {name} opened {front.localizedName()}, which is blocked; hidden again.")
    return _after_action(screenshot)


@mcp.tool(structured_output=False)
def zoom(x: int, y: int, width: int, height: int) -> Image:
    """Look closely at part of the screen: the region [x, y, width, height] (screenshot coordinates) at the
    display's native resolution, sharp enough to read small text and digits. Only for reading: its pixels are
    not click coordinates. To click what you see in it, use `find_text` or the screenshot coordinates."""
    _acquire_lock()
    image, scale = _capture_native()
    if max(width, height) * scale > SCREENSHOT_MAX_EDGE:
        limit = int(SCREENSHOT_MAX_EDGE / scale)
        raise ValueError(f"Region too large to zoom on this display: keep width and height under {limit}.")
    return Image(data=_encode_image(_crop(image, scale, [x, y, width, height]), "public.png"), format="png")


def _ocr_region(region: list[int], screen_width: int, screen_height: int) -> list[int]:
    """Vision misses text in long thin images (a 1568x40 menu-bar strip found nothing, 784x40 worked): grow the
    short side to at least 1/OCR_MAX_ASPECT of the long one, staying on screen. find_text keeps only lines whose
    centre is in the requested region."""
    x, y, width, height = region
    if width > height * OCR_MAX_ASPECT:
        height = min(screen_height, math.ceil(width / OCR_MAX_ASPECT))
        y = max(0, min(y, screen_height - height))
    elif height > width * OCR_MAX_ASPECT:
        width = min(screen_width, math.ceil(height / OCR_MAX_ASPECT))
        x = max(0, min(x, screen_width - width))
    return [x, y, width, height]


@mcp.tool(structured_output=False)
def find_text(text: str | None = None, region: list[int] | None = None, hebrew: bool = False) -> str:
    """Read the text on screen with OCR: every text line with its click point and box, in screenshot coordinates,
    top to bottom. With `text`, only lines that contain it (case-insensitive), and the point is the centre of the
    match itself: click it to press a button or menu item by its label. region: [x, y, width, height] to read only
    part of the screen. Use it to read exact numbers instead of guessing them from a screenshot.
    By default only Apple Vision runs (fast): English, Ukrainian, Russian and digits. It skips Hebrew lines and
    garbles digits next to Hebrew. If the screenshot shows Hebrew in the region, pass hebrew=True: Tesseract then
    reads the Hebrew lines (slower: ~+0.3 s for the full screen, needs `brew install tesseract`). A number right
    before Hebrew text ("1100 - <name>") is unreliable even then: read it with a region around it alone, or zoom."""
    _acquire_lock()
    image, scale = _capture_native()
    screen_width, screen_height = _screen_size()
    wanted = region or [0, 0, screen_width, screen_height]
    needle = text.lower() if text else None
    upscale = max(1.0, OCR_MIN_NATIVE_SCALE / scale)
    tiles = []
    for tile in _ocr_tiles(wanted):
        tile = _ocr_region(tile, screen_width, screen_height)
        tiles.append((tile, _upscale(_crop(image, scale, tile), upscale)))
    vision_results, tesseract_results = [], []
    # Tesseract is the slow pass (full screen: 9.9 s in sequence, 2.4 s in 8 parallel single-threaded processes);
    # Vision runs in this thread meanwhile
    with ThreadPoolExecutor(TESSERACT_WORKERS) as pool:
        futures = []
        if hebrew:
            if not _homebrew_tool("tesseract"):
                raise RuntimeError("Hebrew OCR needs Tesseract: ask the user to run `brew install tesseract`.")
            _tessdata()
            futures = [pool.submit(_tesseract_tile, tile_image, tile, needle) for tile, tile_image in tiles]
        for tile, tile_image in tiles:
            vision_results += _ocr_tile(tile_image, tile, needle)
        for future in futures:
            tesseract_results += future.result()
    found = _merge_hebrew(_dedupe(vision_results, wanted), _dedupe(tesseract_results, wanted))
    return _format_lines(found) or (f"No text matching {text!r} on screen." if text else "No text on screen.")


def _merge_hebrew(vision: list, tesseract: list) -> list:
    """Vision has no Hebrew model and garbles digits in Hebrew lines, Tesseract reads them: a Tesseract line with
    Hebrew replaces the Vision lines it overlaps; every other line stays Vision's."""
    hebrew = [result for result in tesseract if HEBREW.search(result[1])]

    def overlaps(first: list[int], second: list[int]) -> bool:
        return _inside(_centre(first), second) or _inside(_centre(second), first)

    kept = [result for result in vision if not any(overlaps(result[0], line[0]) for line in hebrew)]
    return sorted(kept + hebrew, key=lambda result: (result[0][1], result[0][0]))


def _dedupe(results: list, wanted: list[int]) -> list:
    found = []
    # longest lines first: a line cut at a tile edge is dropped in favour of the whole line from the next tile
    for line_box, line, point_box in sorted(results, key=lambda result: -len(result[1])):
        centre = _centre(line_box)
        if not _inside(centre, wanted) or any(_inside(centre, kept[0]) for kept in found):
            continue
        found.append((line_box, line, point_box))
    return sorted(found, key=lambda result: (result[0][1], result[0][0]))


def _format_lines(found: list) -> str:
    return "\n".join(f"{line!r} at {_centre(point_box)} box {point_box}" for line_box, line, point_box in found)


def _union(boxes: list[list[int]]) -> list[int]:
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[0] + box[2] for box in boxes)
    bottom = max(box[1] + box[3] for box in boxes)
    return [left, top, right - left, bottom - top]


def _tessdata() -> Path:
    """Tesseract's fast heb + eng models in ~/.deckchair/tessdata, downloaded once (0.9 MB + 4 MB)."""
    for language in ("heb", "eng"):
        model = TESSDATA_DIR / f"{language}.traineddata"
        if not model.exists():
            TESSDATA_DIR.mkdir(parents=True, exist_ok=True)
            partial = model.with_suffix(".part")
            urllib.request.urlretrieve(TESSDATA_URL.format(language=language), partial)
            partial.rename(model)
    return TESSDATA_DIR


def _tesseract_tile(image, tile: list[int], needle: str | None) -> list[tuple[list[int], str, list[int]]]:
    """Like _ocr_tile, with Tesseract: Vision has no Hebrew model. On rendered text Tesseract read Hebrew 100%,
    Vision 0%; with heb first Tesseract dropped leading digits in mixed lines ("1100 - עו"ש" -> "0 - עו"ש")."""
    tesseract = _homebrew_tool("tesseract")
    if not tesseract:
        raise RuntimeError("Hebrew OCR needs Tesseract: brew install tesseract")
    with tempfile.NamedTemporaryFile(suffix=".png") as file:
        Path(file.name).write_bytes(_encode_image(image, "public.png"))
        result = subprocess.run(
            # the "tsv" config file lives in Homebrew's tessdata/configs, not in our model folder: set it inline
            [tesseract, file.name, "stdout", "-l", TESSERACT_LANGUAGES, "--psm", "6", "-c", "tessedit_create_tsv=1"],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "TESSDATA_PREFIX": str(_tessdata()), "OMP_THREAD_LIMIT": "1"},
        )
    image_pixels_per_pixel = Quartz.CGImageGetWidth(image) / tile[2]
    lines = {}
    for row in result.stdout.splitlines()[1:]:
        fields = row.split("\t")
        if len(fields) < 12 or fields[0] != "5" or not fields[11].strip():
            continue
        left, top, width, height = (int(value) / image_pixels_per_pixel for value in fields[6:10])
        box = [round(tile[0] + left), round(tile[1] + top), round(width), round(height)]
        lines.setdefault(tuple(fields[2:5]), []).append((fields[11], box))
    found = []
    for words in lines.values():
        line = " ".join(word for word, box in words)
        line_box = point_box = _union([box for word, box in words])
        if needle:
            if needle not in line.lower():
                continue
            hits = [box for word, box in words if word.lower() in needle or needle in word.lower()]
            point_box = _union(hits) if hits else line_box
        found.append((line_box, line, point_box))
    return found


def _centre(box: list[int]) -> tuple[int, int]:
    return box[0] + box[2] // 2, box[1] + box[3] // 2


def _inside(point: tuple[int, int], box: list[int], margin: int = 2) -> bool:
    return box[0] - margin <= point[0] <= box[0] + box[2] + margin and box[1] - margin <= point[1] <= box[1] + box[3] + margin


def _ocr_tiles(region: list[int]):
    """Vision shrinks a large image and loses small text (a full 1568x882 screen: 9 lines; its lower half alone:
    70), so OCR runs on ~OCR_TILE tiles that overlap by OCR_TILE_OVERLAP."""
    x, y, width, height = region
    columns, rows = max(1, math.ceil(width / OCR_TILE)), max(1, math.ceil(height / OCR_TILE))
    tile_width, tile_height = math.ceil(width / columns), math.ceil(height / rows)
    for row in range(rows):
        for column in range(columns):
            left = max(x, x + column * tile_width - OCR_TILE_OVERLAP)
            top = max(y, y + row * tile_height - OCR_TILE_OVERLAP)
            right = min(x + width, x + (column + 1) * tile_width + OCR_TILE_OVERLAP)
            bottom = min(y + height, y + (row + 1) * tile_height + OCR_TILE_OVERLAP)
            yield [left, top, right - left, bottom - top]


def _upscale(image, factor: float):
    # Vision misses text only ~15 native pixels high (a Dock tooltip in a 300x82 region); twice the size it reads it
    if factor <= 1:
        return image
    width = int(Quartz.CGImageGetWidth(image) * factor)
    height = int(Quartz.CGImageGetHeight(image) * factor)
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(), Quartz.kCGImageAlphaNoneSkipLast
    )
    Quartz.CGContextSetInterpolationQuality(context, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)
    return Quartz.CGBitmapContextCreateImage(context)


def _ocr_tile(image, tile: list[int], needle: str | None) -> list[tuple[list[int], str, list[int]]]:
    """(line box, line, click box) in screenshot pixels for the text lines of one tile; with a needle, only lines
    containing it, and the click box is the match itself."""
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)
    request.setRecognitionLanguages_(OCR_LANGUAGES)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    success, error = handler.performRequests_error_([request], None)
    if not success:
        raise RuntimeError(f"OCR failed: {error}")
    tile_x, tile_y, tile_width, tile_height = tile

    def to_pixels(box) -> list[int]:
        # Vision boxes are normalised with the origin at the bottom left
        return [
            round(tile_x + box.origin.x * tile_width),
            round(tile_y + (1 - box.origin.y - box.size.height) * tile_height),
            round(box.size.width * tile_width),
            round(box.size.height * tile_height),
        ]

    lines = []
    for observation in request.results() or []:
        candidate = observation.topCandidates_(1)[0]
        line = candidate.string()
        line_box = point_box = to_pixels(observation.boundingBox())
        if needle:
            start = line.lower().find(needle)
            if start < 0:
                continue
            match, error = candidate.boundingBoxForRange_error_(Foundation.NSMakeRange(start, len(needle)), None)
            if match is not None:
                point_box = to_pixels(match.boundingBox())
        lines.append((line_box, line, point_box))
    return lines


@mcp.tool(structured_output=False)
@_logged
def click(
    x: int, y: int, button: str = "left", clicks: int = 1, modifiers: str = "", screenshot: bool = True
) -> Image | str:
    """Click at (x, y). button: left | right | middle. clicks=2 for double click.
    screenshot=False returns 'ok' instead of a screenshot (~2k tokens less): for steps whose result you don't need
    to see, e.g. focusing a field before typing.
    modifiers: keys held during the click, '+'-joined: 'shift' (extend a selection), 'cmd' (add to a selection on
    macOS), 'ctrl' (the same in Windows / RDP), 'alt', 'cmd+shift'."""
    _check_abort()
    _check_app((x, y))
    down, up, button_code = MOUSE_BUTTONS[button]
    held = [MODIFIERS[name] for name in modifiers.lower().split("+") if name]
    _move_to(x, y)
    flags = 0
    for modifier_keycode, flag in held:
        flags |= flag
        _post_key(modifier_keycode, True, flags)
    for click_state in range(1, clicks + 1):
        _post_mouse(down, x, y, button_code, click_state, flags)
        _post_mouse(up, x, y, button_code, click_state, flags)
        time.sleep(0.05)
    for modifier_keycode, flag in reversed(held):
        flags &= ~flag
        _post_key(modifier_keycode, False, flags)
    return _after_action(screenshot)


@mcp.tool(structured_output=False)
@_logged
def move(x: int, y: int, screenshot: bool = True) -> Image | str:
    """Move the mouse to (x, y) without clicking (hover). screenshot=False returns 'ok' instead of a screenshot."""
    _check_abort()
    _check_app((x, y))
    _move_to(x, y)
    return _after_action(screenshot)


@mcp.tool(structured_output=False)
@_logged
def drag(from_x: int, from_y: int, to_x: int, to_y: int, screenshot: bool = True) -> Image | str:
    """Drag with the left button from one point to another. screenshot=False returns 'ok' instead of a screenshot."""
    _check_abort()
    _check_app((from_x, from_y), (to_x, to_y))
    _move_to(from_x, from_y)
    _post_mouse(Quartz.kCGEventLeftMouseDown, from_x, from_y, Quartz.kCGMouseButtonLeft)
    steps = 20
    for step in range(1, steps + 1):
        x = from_x + (to_x - from_x) * step / steps
        y = from_y + (to_y - from_y) * step / steps
        _post_mouse(Quartz.kCGEventLeftMouseDragged, x, y, Quartz.kCGMouseButtonLeft)
        time.sleep(0.02)
    _post_mouse(Quartz.kCGEventLeftMouseUp, to_x, to_y, Quartz.kCGMouseButtonLeft)
    return _after_action(screenshot)


@mcp.tool(structured_output=False)
@_logged
def scroll(x: int, y: int, amount: int, screenshot: bool = True) -> Image | str:
    """Scroll at (x, y) by `amount` wheel ticks. Positive scrolls up, negative scrolls down.
    screenshot=False returns 'ok' instead of a screenshot."""
    _check_abort()
    _check_app((x, y))
    _move_to(x, y)
    direction = 1 if amount > 0 else -1
    for tick in range(abs(amount)):
        event = Quartz.CGEventCreateScrollWheelEvent(EVENT_SOURCE, Quartz.kCGScrollEventUnitLine, 1, direction)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.02)
    return _after_action(screenshot)


@mcp.tool(structured_output=False)
@_logged
def type_text(text: str, screenshot: bool = True) -> Image | str:
    """Type text into the focused field. Any text, Cyrillic included. Inside an RDP client non-ASCII text is
    pasted through the clipboard (ctrl+v on the remote side); the user's clipboard is restored afterwards.
    screenshot=False returns 'ok' instead of a screenshot (~2k tokens less), e.g. before a key('enter')."""
    _check_abort()
    _check_app()
    if _frontmost_app() in RDP_APPS and any(_char_key(char) is None for char in text):
        # one paste for the whole text: the client syncs the clipboard slowly, and a second paste soon after
        # the first one inserts the first text again
        _paste_in_rdp(text)
        return _after_action(screenshot)
    shift = [MODIFIERS["shift"]]
    for char in text:
        _check_abort()
        _check_app()  # the terminal can take focus mid-text
        key = _char_key(char)
        if key is None:
            # keycode 0 + Unicode string: macOS apps read the string
            _press(0, [], char)
        else:
            keycode, needs_shift = key
            _press(keycode, shift if needs_shift else [], char)
    return _after_action(screenshot)


def _frontmost_app() -> str | None:
    # NSWorkspace updates frontmostApplication from run-loop notifications, and this server has no run loop:
    # without a short spin it keeps returning the app that was in front at the first call
    Foundation.NSRunLoop.currentRunLoop().runUntilDate_(Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.01))
    return AppKit.NSWorkspace.sharedWorkspace().frontmostApplication().bundleIdentifier()


def _save_clipboard() -> list[dict]:
    board = AppKit.NSPasteboard.generalPasteboard()
    return [{kind: item.dataForType_(kind) for kind in item.types()} for item in board.pasteboardItems() or []]


def _restore_clipboard(saved: list[dict]) -> None:
    board = AppKit.NSPasteboard.generalPasteboard()
    board.clearContents()
    items = []
    for content in saved:
        item = AppKit.NSPasteboardItem.alloc().init()
        for kind, data in content.items():
            if data is not None:
                item.setData_forType_(data, kind)
        items.append(item)
    if items:
        board.writeObjects_(items)


def _paste_in_rdp(text: str) -> None:
    # RDP clients send key codes only, so a non-ASCII character can't be typed; the clipboard is redirected instead
    saved = _save_clipboard()
    board = AppKit.NSPasteboard.generalPasteboard()
    board.clearContents()
    board.setString_forType_(text, AppKit.NSPasteboardTypeString)
    # Windows App sends a clipboard change to the remote side only when it becomes the active app, not when the
    # clipboard changes: switch to Finder and back. The caret in the remote field stays where it was.
    rdp_app = _running_app(_frontmost_app())
    for app in [_running_app("com.apple.finder"), rdp_app]:
        app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        time.sleep(0.3)
    _press(KEYCODES["v"], [MODIFIERS["ctrl"]])
    # the remote fetches the data only on paste and asynchronously: restore too early and it pastes the old text
    time.sleep(CLIPBOARD_SYNC_SECONDS)
    _restore_clipboard(saved)


def _running_app(bundle_id: str):
    return AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)[0]


@mcp.tool(structured_output=False)
@_logged
def key(keys: str, screenshot: bool = True) -> Image | str:
    """Press a key or combo, e.g. 'enter', 'tab', 'esc', 'cmd+tab', 'cmd+space', 'shift+home', 'ctrl+alt+delete'.
    screenshot=False returns 'ok' instead of a screenshot (~2k tokens less)."""
    _check_abort()
    _check_app()
    *modifier_names, name = keys.lower().split("+")
    modifiers = [MODIFIERS[modifier] for modifier in modifier_names]
    keycode = NAMED_KEYS.get(name, KEYCODES.get(name))
    if keycode is None:
        raise ValueError(f"Unknown key: {name}")
    _press(keycode, modifiers)
    return _after_action(screenshot)


@mcp.tool(structured_output=False)
@_logged
def wait(seconds: float, screenshot: bool = True) -> Image | str:
    """Wait without acting (a report loading, RDP redrawing, holding the final state for a video), then return a
    screenshot. Up to 30 s. screenshot=False returns 'ok' instead, e.g. to hold the last frame of a video."""
    _check_abort()
    time.sleep(min(max(seconds, 0), 30))
    _check_abort()
    return _finish(screenshot)


def _output_path(path: str, suffixes: tuple[str, ...]) -> Path:
    target = Path(path).expanduser().resolve()
    if target.suffix.lower() not in suffixes:
        raise ValueError(f"Path must end with one of {', '.join(suffixes)}: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


@mcp.tool(structured_output=False)
@_logged
def save_screenshot(path: str) -> str:
    """Save a full-resolution screenshot of the selected display to a file (.png or .jpg) and return its path.
    Use it to keep evidence for the user; use `screenshot` to look at the screen yourself."""
    _acquire_lock()
    target = _output_path(path, (".png", ".jpg", ".jpeg"))
    # the same capture as `screenshot`, at native resolution: it hides BLOCKED_APPS, the screencapture CLI can't
    uti = "public.png" if target.suffix.lower() == ".png" else "public.jpeg"
    target.write_bytes(_encode_image(_capture_native()[0], uti))
    return str(target)


def _screencapture_display() -> str:
    # ponytail: screencapture numbers displays from 1 = main; the order of the others is assumed to match
    # CGGetActiveDisplayList, not verified with a second display
    _display_bounds()
    return str(_displays().index(selected_display[0]) + 1)


@mcp.tool(structured_output=False)
def list_displays() -> str:
    """The connected displays: index, size in points, position, which is main and which is selected. All tool
    coordinates are relative to the selected display; switch with `use_display`."""
    _display_bounds()
    lines = []
    for index, display in enumerate(_displays()):
        bounds = Quartz.CGDisplayBounds(display)
        notes = ["main"] if display == Quartz.CGMainDisplayID() else []
        notes += ["selected"] if display == selected_display[0] else []
        lines.append(
            f"{index}: {int(bounds.size.width)}x{int(bounds.size.height)} at "
            f"({int(bounds.origin.x)}, {int(bounds.origin.y)}){' — ' + ', '.join(notes) if notes else ''}"
        )
    return "\n".join(lines)


@mcp.tool(structured_output=False)
@_logged
def use_display(index: int) -> Image:
    """Select the display that screenshots, clicks, OCR, zoom, files and recordings work on, by its index in
    `list_displays` (0 = main). Coordinates are then relative to that display. Returns its screenshot."""
    _acquire_lock()
    displays = _displays()
    if not 0 <= index < len(displays):
        raise ValueError(f"No display {index}; connected: 0..{len(displays) - 1}")
    selected_display[0] = displays[index]
    return screenshot()


recording = {}


def _homebrew_tool(name: str) -> str | None:
    found = shutil.which(name) or f"/opt/homebrew/bin/{name}"
    return found if Path(found).exists() else None


def _video_seconds(path: Path) -> float:
    probe = subprocess.run(
        [_homebrew_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(probe.stdout.strip())


def _encode_video(raw: Path, target: Path) -> None:
    # Constant 15 fps first (screencapture writes frames only on change), then mpdecimate with max=2 keeps
    # every third frame of a still screen: pauses play 3x faster, motion stays real-time, a toast still shows.
    video_filter = (
        f"fps={VIDEO_FPS},mpdecimate=hi=64*24:lo=64*8:frac=0.2:max=2,setpts=N/{VIDEO_FPS}/TB,"
        f"scale='min({VIDEO_MAX_WIDTH},iw)':-2:flags=lanczos,fps=30"
    )
    subprocess.run(
        [
            _homebrew_tool("ffmpeg"), "-y", "-loglevel", "error", "-i", str(raw), "-vf", video_filter,
            "-c:v", "libx264", "-crf", "18", "-preset", "slow", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(target),
        ],
        check=True,
        timeout=900,
    )


def _contact_sheet(video: Path, seconds: float) -> tuple[Path, float]:
    tile_seconds = max(1.0, seconds / 64)
    tiles = max(1, math.ceil(seconds / tile_seconds))
    columns, rows = min(8, tiles), math.ceil(tiles / 8)
    sheet = Path(tempfile.gettempdir()) / f"{video.stem}-contact.jpg"
    subprocess.run(
        [
            _homebrew_tool("ffmpeg"), "-y", "-loglevel", "error", "-i", str(video),
            "-vf", f"fps=1/{tile_seconds:.3f},scale=240:-2,tile={columns}x{rows}", "-frames:v", "1", str(sheet),
        ],
        check=True,
        timeout=120,
    )
    return sheet, tile_seconds


@mcp.tool(structured_output=False)
@_logged
def start_recording(path: str, region: list[int] | None = None) -> str:
    """Start recording the screen to an .mp4 file, with mouse clicks shown. Stop it with `stop_recording`.
    region: optional [x, y, width, height] in screenshot coordinates, e.g. only the RDP window.
    While recording, the mouse glides to each target and pauses 0.7 s before and after the action."""
    _acquire_lock()
    if recording:
        raise RuntimeError(f"Already recording to {recording['path']}. Call stop_recording first.")
    target = _output_path(path, (".mp4",))
    _refuse_blocked_on_screen("a recording")
    raw = target.with_name(f"{target.stem}.raw.mov")
    command = ["screencapture", "-v", "-x", "-k"]
    if region:
        bounds = _display_bounds()
        scale = _points_per_pixel()
        x, y, width, height = (round(value * scale) for value in region)
        command += ["-R", f"{int(bounds.origin.x) + x},{int(bounds.origin.y) + y},{width},{height}"]
    else:
        command += ["-D", _screencapture_display()]
    process = subprocess.Popen(
        command + [str(raw)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    time.sleep(1)
    if process.poll() is not None:
        raise RuntimeError(f"Recording failed to start: {process.stderr.read().decode().strip()}")
    recording.update(process=process, path=target, raw=raw)
    return f"Recording to {target}"


@mcp.tool(structured_output=False)
@_logged
def stop_recording() -> str:
    """Stop the recording and encode it: duplicate still frames cut (pauses play faster), H.264, at most
    1280 px wide. Returns the video path, duration, size and a contact sheet (up to 8x8 tiles) to check every
    step is in the video. Without ffmpeg the raw .mov is kept instead."""
    if not recording:
        raise RuntimeError("Not recording.")
    process, target, raw = recording["process"], recording["path"], recording["raw"]
    recording.clear()
    process.send_signal(signal.SIGINT)
    process.wait(timeout=30)
    if not raw.exists():
        raise RuntimeError(f"Recording stopped but no file was written: {raw}")
    if not (_homebrew_tool("ffmpeg") and _homebrew_tool("ffprobe")):
        return f"ffmpeg not found (brew install ffmpeg); raw recording kept: {raw}"
    _encode_video(raw, target)
    raw.unlink()
    seconds = _video_seconds(target)
    sheet, tile_seconds = _contact_sheet(target, seconds)
    return (
        f"{target} ({seconds:.1f} s, {target.stat().st_size // 1024} KB). "
        f"Contact sheet: {sheet} (1 tile = {tile_seconds:.1f} s of video, left to right, top to bottom)"
    )


def _discard_recording() -> None:
    process, raw = recording["process"], recording["raw"]
    recording.clear()
    process.kill()
    process.wait(timeout=30)
    raw.unlink(missing_ok=True)


@atexit.register
def _stop_recording_on_exit() -> None:
    if recording:
        recording["process"].send_signal(signal.SIGINT)
        recording["process"].wait(timeout=30)


if __name__ == "__main__":
    mcp.run()
