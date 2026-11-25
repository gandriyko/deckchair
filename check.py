# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2", "pyobjc-framework-Quartz"]
# ///
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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
print(f"screenshot {size[0]}x{size[1]}, {len(jpeg) // 1024} KB, {elapsed_ms:.0f} ms")

original = server._mouse_position()
width, height = server._screen_size()
for corner in [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]:
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

with tempfile.TemporaryDirectory() as folder:
    shot = Path(server.save_screenshot(f"{folder}/shots/screen.png"))
    assert shot.stat().st_size > 0
    print(f"save_screenshot: {shot.stat().st_size // 1024} KB")

    video = Path(folder, "video.mp4")
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
        [server._video_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
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
