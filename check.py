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
    server.start_recording(f"{folder}/video.mov")
    time.sleep(2)
    summary = server.stop_recording()
    assert Path(folder, "video.mov").stat().st_size > 0, summary
    print(f"files: screenshot {shot.stat().st_size // 1024} KB, video {summary.split('(')[1].rstrip(')')}")
print("OK")
