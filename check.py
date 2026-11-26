# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.10,<2", "pyobjc-framework-Quartz", "pyobjc-framework-Vision", "pyobjc-framework-ScreenCaptureKit"]
# ///
import asyncio
import json
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import AppKit
import Foundation
import Quartz
import server

server._acquire_lock()
second = subprocess.run(
    [sys.executable, "-c", "import server; server._acquire_lock()"],
    cwd=Path(__file__).parent,
    capture_output=True,
    text=True,
)
assert second.returncode != 0 and "already in use" in second.stderr, second.stderr
print("lock: a second process is refused")

original_clipboard = server._save_clipboard()
subprocess.run(["pbcopy"], input="clipboard check ✓".encode(), check=True)
saved = server._save_clipboard()
subprocess.run(["pbcopy"], input=b"overwritten", check=True)
server._restore_clipboard(saved)
restored = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
server._restore_clipboard(original_clipboard)
assert restored == "clipboard check ✓", restored
print("clipboard: saved and restored")

def render(text: str, font_px: float = 10, scale: int = 2):
    attributes = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(font_px)}
    attributed = Foundation.NSAttributedString.alloc().initWithString_attributes_(text, attributes)
    width, height = int(attributed.size().width) + 16, int(attributed.size().height) + 10
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, width * scale, height * scale, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0
    )
    rep.setSize_((width, height))
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    AppKit.NSColor.whiteColor().set()
    AppKit.NSRectFill(((0, 0), (width, height)))
    attributed.drawAtPoint_((8, 5))
    AppKit.NSGraphicsContext.restoreGraphicsState()
    return rep.CGImage(), [0, 0, width, height]


if server._homebrew_tool("tesseract"):
    hebrew_line = "עו\"ש בנק הפועלים ILS 2,151,051.55"
    hebrew_image, hebrew_tile = render(hebrew_line)
    upscaled = server._upscale(hebrew_image, 2)
    tesseract_lines = [line for box, line, point in server._tesseract_tile(upscaled, hebrew_tile, None)]
    vision_lines = [line for box, line, point in server._ocr_tile(upscaled, hebrew_tile, None)]
    assert "בנק הפועלים" in " ".join(tesseract_lines), tesseract_lines
    assert "2,151,051.55" in " ".join(tesseract_lines), tesseract_lines
    assert "בנק" not in " ".join(vision_lines), vision_lines
    found = server._tesseract_tile(upscaled, hebrew_tile, "הפועלים")
    assert found and server._inside(server._centre(found[0][2]), hebrew_tile), found

    page_image, page_tile = render("Trial Balance 383,682.71\n" + hebrew_line)
    page_image = server._upscale(page_image, 2)
    merged = server._merge_hebrew(
        server._dedupe(server._ocr_tile(page_image, page_tile, None), page_tile),
        server._dedupe(server._tesseract_tile(page_image, page_tile, None), page_tile),
    )
    merged_lines = [line for box, line, point in merged]
    assert len(merged_lines) == 2, merged_lines
    assert merged_lines[0] == "Trial Balance 383,682.71", merged_lines
    assert "בנק הפועלים" in merged_lines[1] and "2,151,051.55" in merged_lines[1], merged_lines
    print(f"hebrew: merged {merged_lines}; Vision alone gave {vision_lines} for the Hebrew line")
else:
    print("hebrew: skipped, brew install tesseract")

for char in string.ascii_letters + string.digits + string.punctuation + " \n\t":
    assert server._char_key(char) is not None, f"no keycode for {char!r}"
assert server._char_key("П") is None

server._capture_jpeg()
start = time.perf_counter()
jpeg = server._capture_jpeg()
elapsed_ms = (time.perf_counter() - start) * 1000
source = Quartz.CGImageSourceCreateWithData(jpeg, None)
image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
size = (Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image))
assert size == server._screen_size(), (size, server._screen_size())
main_api = server.last_capture_api[0]
assert main_api == "ScreenCaptureKit", main_api
legacy_image = server._capture_legacy()
assert Quartz.CGImageGetWidth(legacy_image) == Quartz.CGImageGetWidth(server._capture_native()[0])
working_sck = server._capture_sck


def broken_sck(display_id):
    raise TimeoutError("simulated ScreenCaptureKit failure")


server._capture_sck = broken_sck
fallback_image, _ = server._capture_native()
assert server.last_capture_api[0] == "CGWindowListCreateImage", server.last_capture_api
server._capture_sck = working_sck
print(f"screenshot {size[0]}x{size[1]}, {len(jpeg) // 1024} KB, {elapsed_ms:.0f} ms via {main_api}; "
      f"a failing ScreenCaptureKit falls back to {server.last_capture_api[0]} at the same size")

def log_entries() -> list[dict]:
    if not server._log_path().exists():
        return []
    return [json.loads(line) for line in server._log_path().read_text(encoding="utf-8").splitlines()]


log_start = len(log_entries())
original = server._mouse_global()
width, height = server._screen_size()
bounds = server._display_bounds()
left, top = bounds.origin.x, bounds.origin.y
right, bottom = left + bounds.size.width - 1, top + bounds.size.height - 1
for corner in [(left, top), (right, top), (left, bottom), (right, bottom)]:
    Quartz.CGWarpMouseCursorPosition(corner)
    for name, call in {
        "click": lambda: server.click(700, 500),
        "type_text": lambda: server.type_text("x"),
        "key": lambda: server.key("esc"),
    }.items():
        try:
            call()
            raise AssertionError(f"{name} not aborted at {corner}")
        except server.AbortedByUser:
            pass
Quartz.CGWarpMouseCursorPosition(original)
print("fail-safe: all four corners abort")

# _move_to and an immediate read: move() waits ~0.5 s for its screenshot, and a hand on the trackpad moves the
# cursor meanwhile
server._move_to(width // 2, height // 2)
local_x, local_y = server._mouse_position()
global_x, global_y = server._mouse_global()
scale = server._points_per_pixel()
assert abs(local_x - width // 2) <= 1 and abs(local_y - height // 2) <= 1, (local_x, local_y)
assert abs(global_x - (left + width // 2 * scale)) <= 1, (global_x, left, scale)
Quartz.CGWarpMouseCursorPosition(original)
print(f"coordinates: {int(bounds.size.width)}x{int(bounds.size.height)} pt display, {scale:.3f} pt per "
      f"screenshot pixel; move({width // 2}, {height // 2}) lands at ({global_x:.0f}, {global_y:.0f}) pt")

start = time.perf_counter()
server.wait(0.5)
waited = time.perf_counter() - start
assert waited >= 0.5, waited
new_entries = log_entries()[log_start:]
aborted = [entry for entry in new_entries if "ABORTED BY USER" in entry.get("error", "")]
assert len(aborted) == 12, len(aborted)
last = new_entries[-1]
assert last["tool"] == "wait" and last["args"] == {"seconds": 0.5} and last["result"] == "ok", last
assert {"time", "pid", "app"} <= last.keys(), last
print(f"wait: {waited:.1f} s; log: {len(aborted)} aborted calls and the wait in {server._log_path()}")

tools = {tool.name: tool.inputSchema for tool in asyncio.run(server.mcp.list_tools())}
assert set(tools["click"]["properties"]) == {"x", "y", "button", "clicks", "modifiers", "screenshot"}, tools["click"]
quiet = asyncio.run(server.mcp.call_tool("move", {"x": width // 2, "y": height // 2, "screenshot": False}))
loud = asyncio.run(server.mcp.call_tool("move", {"x": width // 2, "y": height // 2}))
quiet_types = [content.type for content in (quiet[0] if isinstance(quiet, tuple) else quiet)]
loud_types = [content.type for content in (loud[0] if isinstance(loud, tuple) else loud)]
assert quiet_types == ["text"] and loud_types == ["image"], (quiet_types, loud_types)
print("screenshot=False: an action returns 'ok' text instead of an image")
assert tools["wait"]["required"] == ["seconds"], tools["wait"]
print(f"schema: {len(tools)} tools, logged wrappers keep their parameters")

displays = server.list_displays()
assert displays.startswith("0: ") and "main, selected" in displays.splitlines()[0], displays
server.use_display(0)
try:
    server.use_display(len(displays.splitlines()))
    raise AssertionError("use_display accepted a missing display")
except ValueError:
    pass
print(f"displays: {len(displays.splitlines())} ({displays.splitlines()[0]})")

zoomed = server.zoom(100, 100, 300, 200)
source = Quartz.CGImageSourceCreateWithData(zoomed.data, None)
zoomed_image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
_, scale = server._capture_native()
zoom_size = (Quartz.CGImageGetWidth(zoomed_image), Quartz.CGImageGetHeight(zoomed_image))
assert abs(zoom_size[0] - 300 * scale) <= 2 and abs(zoom_size[1] - 200 * scale) <= 2, (zoom_size, scale)
print(f"zoom: 300x200 region -> {zoom_size[0]}x{zoom_size[1]} px")

previous_app = server._frontmost_app()
server.activate_app("Finder")
assert server._frontmost_app() == "com.apple.finder", server._frontmost_app()
finder = next(window for window in server._on_screen_windows()
              if window["kCGWindowOwnerName"] == "Finder" and window["kCGWindowLayer"] == 0)
frame = finder["kCGWindowBounds"]
scale = server._points_per_pixel()
finder_region = [int((frame["X"] - left) / scale) + 20, int((frame["Y"] - top) / scale) + 20, 200, 100]
def finder_pixels(capture) -> bytes:
    image = capture()
    crop = server._crop(image, Quartz.CGImageGetWidth(image) / width, finder_region)
    return bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(crop)))
seen = {"sck": finder_pixels(lambda: server._capture_native()[0]), "legacy": finder_pixels(server._capture_legacy)}
real_blocked = server.BLOCKED_APPS
server.BLOCKED_APPS = real_blocked | {"Finder"}
assert finder_pixels(lambda: server._capture_native()[0]) != seen["sck"], "ScreenCaptureKit showed a blocked app"
assert finder_pixels(server._capture_legacy) != seen["legacy"], "CGWindowListCreateImage showed a blocked app"
for name, call in {
    "activate_app": lambda: server.activate_app("Finder", screenshot=False),
    "key": lambda: server.key("esc", screenshot=False),
    "start_recording": lambda: server.start_recording(f"{tempfile.gettempdir()}/blocked.mp4"),
}.items():
    try:
        call()
        raise AssertionError(f"{name} acted on a blocked app")
    except PermissionError:
        pass
server.BLOCKED_APPS = real_blocked
print("blocked apps: hidden from both capture APIs; activate_app, key and start_recording refuse")
server.ALLOWED_APPS = {"Finder"}
server._check_app()  # by name; the menu bar has no app window and counts as the front app
server._check_app((width // 2, 2))
server.ALLOWED_APPS = {"com.example.nothing"}
try:
    server.key("esc", screenshot=False)
    raise AssertionError("key acted in an app outside DECKCHAIR_APPS")
except PermissionError as error:
    assert "Finder (com.apple.finder)" in str(error), error
server.ALLOWED_APPS = set()
print("DECKCHAIR_APPS: the front app by name passes, an app outside the list is refused")
start = time.perf_counter()
menu = server.find_text("Finder", region=[0, 0, width, 40])
ocr_ms = (time.perf_counter() - start) * 1000
assert "at (" in menu, menu
menu_y = int(menu.split("at (")[1].split(",")[1].split(")")[0])
assert menu_y < 40, menu
start = time.perf_counter()
everything = server.find_text()
full_screen_ms = (time.perf_counter() - start) * 1000
if server._homebrew_tool("tesseract"):
    start = time.perf_counter()
    server.find_text(hebrew=True)
    hebrew_ms = (time.perf_counter() - start) * 1000
    print(f"find_text(hebrew=True) on the full screen: {hebrew_ms:.0f} ms")
assert everything.count("\n") >= 5, everything
server.activate_app(previous_app)
assert server._frontmost_app() == previous_app, server._frontmost_app()
print(f"activate_app: Finder and back; find_text: menu title found in {ocr_ms:.0f} ms, "
      f"{everything.count(chr(10)) + 1} lines on the full screen in {full_screen_ms:.0f} ms")

with tempfile.TemporaryDirectory() as folder:
    shot = Path(server.save_screenshot(f"{folder}/shots/screen.png"))
    assert shot.stat().st_size > 0
    print(f"save_screenshot: {shot.stat().st_size // 1024} KB")

    video = Path(folder, "video.mp4")
    if server._blocked_pids():
        print("video: SKIPPED, a password manager window is on screen (recordings refuse); close it to test video")
    else:
        wall_start = time.monotonic()
        server.start_recording(str(video), region=[200, 200, 800, 450])
        time.sleep(4)
        glide_start = time.monotonic()
        server.move(600, 400)
        glide_seconds = time.monotonic() - glide_start
        server.move(300, 300)
        time.sleep(1)
        summary = server.stop_recording()
        wall_seconds = time.monotonic() - wall_start
        assert glide_seconds >= server.GLIDE_SECONDS + server.VISIBLE_PAUSE_SECONDS, glide_seconds
        assert not Path(folder, "video.raw.mov").exists(), "raw recording left behind"
        probe = subprocess.run(
            [server._homebrew_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        codec, width, height = probe.split(",")
        video_seconds = server._video_seconds(video)
        assert codec == "h264" and int(width) == 1280, probe
        assert video_seconds < wall_seconds * 0.8, (video_seconds, wall_seconds)
        sheet = Path(summary.split("Contact sheet: ")[1].split(" (")[0])
        assert sheet.stat().st_size > 0
        print(
            f"video: {codec} {width}x{height}, {video_seconds:.1f} s from {wall_seconds:.1f} s recorded, "
            f"move while recording {glide_seconds:.1f} s, contact sheet ok"
        )
print("OK")
