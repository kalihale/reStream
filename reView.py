#!/usr/bin/env python3
"""reView: low-latency viewer for reStream with a client-side pen cursor.

Video frames are read from stdin (piped from reStream.sh). Pen input is read
directly from the tablet's wacom digitizer over a second ssh connection, so
the cursor tracks the pen at input-event rate instead of video frame rate
(the same approach rmview uses).

The cursor is drawn by the viewer, so run restream WITHOUT -c. Pipe the
decompressed framebuffer straight in (ffmpeg's CFR sync buffers rawvideo
pipes indefinitely, so don't route through reStream.sh -o pipe:1):

    ssh root@10.11.99.1 '~/restream -h 1872 -w 1404 -b 4 -f :mem: -s 2629636' \
        | lz4 -d | ./reView.py

Keys: Esc/q quit.
"""

import argparse
import os
import select
import struct
import subprocess
import sys
import threading
import time
from collections import deque

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame

# reMarkable 2 wacom digitizer range
WACOM_MAX_X = 15725  # along the short screen edge
WACOM_MAX_Y = 20951  # along the long screen edge

# struct input_event is 16 bytes on the tablet (32-bit ARM)
EVENT_FORMAT = "2IHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EV_KEY = 1
EV_ABS = 3
ABS_ALONG_WIDTH = 1   # digitizer axis along the short screen edge
ABS_ALONG_HEIGHT = 0  # digitizer axis along the long screen edge
ABS_PRESSURE = 24
BTN_TOOL_PEN = 320

TRAIL_SECONDS = 0.35

SSH_OPTIONS = [
    "-o", "ConnectTimeout=3",
    "-o", "PasswordAuthentication=no",
    "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
]


class PenTracker(threading.Thread):
    """Reads raw evdev events from the tablet and keeps the latest pen state."""

    def __init__(self, host, device, frame_size, landscape, threshold):
        super().__init__(daemon=True)
        self.host = host
        self.device = device
        self.frame_size = frame_size
        self.landscape = landscape
        self.threshold = threshold

        self.pos = None       # latest position in frame coordinates
        self.near = False     # pen in hover range
        self.pressed = False  # pen touching the screen
        self.trail = deque()  # recent positions: (x, y, timestamp)
        self._trail_lock = threading.Lock()

    def run(self):
        cmd = ["ssh", *SSH_OPTIONS, "root@%s" % self.host, "cat", self.device]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        except OSError as e:
            print("[reView] could not start ssh for pen events: %s" % e, file=sys.stderr)
            return

        wx = wy = None
        while True:
            data = proc.stdout.read(EVENT_SIZE)
            if len(data) < EVENT_SIZE:
                print("[reView] pen event stream ended", file=sys.stderr)
                return
            _, _, e_type, e_code, e_value = struct.unpack(EVENT_FORMAT, data)
            if e_type == EV_ABS:
                if e_code == ABS_ALONG_WIDTH:
                    wx = e_value
                elif e_code == ABS_ALONG_HEIGHT:
                    wy = e_value
                elif e_code == ABS_PRESSURE:
                    self.pressed = e_value > self.threshold
                if wx is not None and wy is not None and e_code in (ABS_ALONG_WIDTH, ABS_ALONG_HEIGHT):
                    pos = self._map(wx, wy)
                    self.pos = pos
                    if self.near:
                        with self._trail_lock:
                            self.trail.append((pos[0], pos[1], time.monotonic()))
            elif e_type == EV_KEY and e_code == BTN_TOOL_PEN:
                self.near = bool(e_value)
                if e_value:
                    with self._trail_lock:
                        self.trail.clear()
                    wx = wy = None  # discard stale coords; wait for a fresh pair

    def _map(self, wx, wy):
        fw, fh = self.frame_size
        if self.landscape:
            return wy / WACOM_MAX_Y * fw, wx / WACOM_MAX_X * fh
        return wx / WACOM_MAX_X * fw, (1 - wy / WACOM_MAX_Y) * fh


class FrameReader(threading.Thread):
    """Reads raw frames from stdin, keeping only the most recent one."""

    def __init__(self, frame_bytes, convert):
        super().__init__(daemon=True)
        self.frame_bytes = frame_bytes
        self.convert = convert
        self.eof = False
        self._lock = threading.Lock()
        self._latest = None

    def run(self):
        # raw os.read instead of sys.stdin.buffer: a daemon thread blocked on
        # the buffered reader's lock aborts the interpreter at shutdown.
        # SDL's X11 init puts fd 0 into non-blocking mode behind our back, so
        # force it blocking and still guard against EAGAIN with select().
        try:
            os.set_blocking(0, True)
        except OSError:
            pass
        while True:
            chunks = []
            remaining = self.frame_bytes
            while remaining:
                try:
                    chunk = os.read(0, min(remaining, 1 << 20))
                except BlockingIOError:
                    select.select([0], [], [])
                    continue
                if not chunk:
                    self.eof = True
                    return
                chunks.append(chunk)
                remaining -= len(chunk)
            converted = self.convert(b"".join(chunks))
            with self._lock:
                self._latest = converted

    def take(self):
        with self._lock:
            latest, self._latest = self._latest, None
            return latest


def make_converter(pixel_format):
    """Return (bytes_per_pixel, fn(frame_bytes) -> (buffer, pygame_format))."""
    if pixel_format == "bgra":
        return 4, lambda d: (d, "BGRA")

    import numpy as np

    if pixel_format == "gray8":
        def convert(d):
            a = np.frombuffer(d, np.uint8)
            return np.repeat(a, 3).tobytes(), "RGB"
        return 1, convert

    if pixel_format == "gray16be":
        # xochitl's gray16 output peaks at 1/8th of the value range,
        # so shifting by 5 instead of 8 restores full brightness
        def convert(d):
            v = np.frombuffer(d, ">u2")
            g = np.clip(v >> 5, 0, 255).astype(np.uint8)
            return np.repeat(g, 3).tobytes(), "RGB"
        return 2, convert

    if pixel_format == "rgb565le":
        def convert(d):
            v = np.frombuffer(d, "<u2").astype(np.uint32)
            r = ((v >> 11) & 0x1F) * 255 // 31
            g = ((v >> 5) & 0x3F) * 255 // 63
            b = (v & 0x1F) * 255 // 31
            return np.stack([r, g, b], axis=-1).astype(np.uint8).tobytes(), "RGB"
        return 2, convert

    raise ValueError("unsupported pixel format: %s" % pixel_format)


def rotate_point(x, y, fw, fh, deg):
    """Map a point in the (fw x fh) frame to its position after the frame is
    rotated clockwise by `deg` degrees, matching pygame.transform.rotate(-deg)."""
    if deg == 90:
        return fh - y, x
    if deg == 180:
        return fw - x, fh - y
    if deg == 270:
        return y, fw - x
    return x, y


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="Example: ssh root@10.11.99.1 '~/restream -h 1872 -w 1404 -b 4"
               " -f :mem: -s 2621636' | lz4 -d | ./reView.py",
    )
    parser.add_argument("--width", type=int, help="frame width in pixels (default: 1404, or 1872 with --landscape)")
    parser.add_argument("--height", type=int, help="frame height in pixels (default: 1872, or 1404 with --landscape)")
    parser.add_argument("--landscape", action="store_true", help="frames are in landscape orientation (reStream.sh -l)")
    parser.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                        help="rotate the displayed image clockwise by this many degrees. Matches"
                             " the working-tree reStream.sh: 0 = portrait (default), 90 = -l (landscape).")
    parser.add_argument("--pixel-format", default="bgra", choices=["bgra", "gray8", "gray16be", "rgb565le"],
                        help="pixel format of the incoming frames (default: bgra)")
    parser.add_argument("--source", default=os.environ.get("REMARKABLE_IP", "10.11.99.1"),
                        help="reMarkable IP address for the pen event connection")
    parser.add_argument("--event-device", default="/dev/input/event1",
                        help="wacom evdev device on the tablet (default: /dev/input/event1)")
    parser.add_argument("--pressure-threshold", type=int, default=1000,
                        help="pen pressure above this counts as touching (default: 1000)")
    parser.add_argument("--pen-color", default="red", help="hover cursor color (default: red)")
    parser.add_argument("--trail-color", default=None,
                        help="motion-trail color (default: same as --pen-color)")
    parser.add_argument("--pressed-color", default=None,
                        help="cursor color while the pen is touching (default: same as --pen-color)")
    parser.add_argument("--pen-size", type=float, default=18.0, help="cursor diameter in frame pixels (default: 18)")
    parser.add_argument("--title", default="reStream", help="window title")
    parser.add_argument("--no-pen", action="store_true", help="disable the pen cursor overlay")
    parser.add_argument("--exit-after", type=int, default=0, help="exit after N frames (for testing)")
    parser.add_argument("--screenshot", help="save the window to this PNG just before exit (for testing)")
    return parser.parse_args()


def main():
    args = parse_args()

    if sys.stdin.isatty():
        print("reView reads raw video from stdin; pipe the framebuffer into it:", file=sys.stderr)
        print("    ssh root@10.11.99.1 '~/restream -h 1872 -w 1404 -b 4 -f :mem: -s 2629636' \\", file=sys.stderr)
        print("        | lz4 -d | ./reView.py", file=sys.stderr)
        return 1

    fw = args.width or (1872 if args.landscape else 1404)
    fh = args.height or (1404 if args.landscape else 1872)

    # displayed dimensions after rotation (90/270 swap width and height)
    disp_w, disp_h = (fh, fw) if args.rotate in (90, 270) else (fw, fh)

    bytes_per_pixel, convert = make_converter(args.pixel_format)
    reader = FrameReader(fw * fh * bytes_per_pixel, convert)
    reader.start()

    pen = None
    if not args.no_pen:
        pen = PenTracker(args.source, args.event_device, (fw, fh),
                         args.landscape, args.pressure_threshold)
        pen.start()

    pygame.init()
    pygame.display.set_caption(args.title)
    init_scale = min(900 / disp_h, 1400 / disp_w, 1.0)
    window = pygame.display.set_mode((int(disp_w * init_scale), int(disp_h * init_scale)), pygame.RESIZABLE)
    clock = pygame.time.Clock()
    pen_rgb = tuple(pygame.Color(args.pen_color))[:3]
    trail_rgb = tuple(pygame.Color(args.trail_color))[:3] if args.trail_color else pen_rgb
    pressed_rgb = tuple(pygame.Color(args.pressed_color))[:3] if args.pressed_color else pen_rgb

    frame_surface = None
    scaled = None
    frames_shown = 0
    running = True

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                running = False
            elif event.type == pygame.VIDEORESIZE:
                scaled = None

        latest = reader.take()
        if latest is not None:
            buffer, pygame_format = latest
            # .convert() drops the alpha channel: xochitl writes
            # alpha < 255 in places, which would blend into the background
            frame_surface = pygame.image.frombuffer(buffer, (fw, fh), pygame_format).convert()
            if args.rotate:
                # negative angle = clockwise; 90/180/270 are exact (no interpolation)
                frame_surface = pygame.transform.rotate(frame_surface, -args.rotate)
            scaled = None
            frames_shown += 1
            if args.exit_after and frames_shown >= args.exit_after:
                running = False

        window_w, window_h = window.get_size()
        scale = min(window_w / disp_w, window_h / disp_h)
        target_w, target_h = int(disp_w * scale), int(disp_h * scale)
        offset_x, offset_y = (window_w - target_w) // 2, (window_h - target_h) // 2

        window.fill((0, 0, 0))
        if frame_surface is not None:
            if scaled is None or scaled.get_size() != (target_w, target_h):
                scaled = pygame.transform.smoothscale(frame_surface, (target_w, target_h))
            window.blit(scaled, (offset_x, offset_y))

        if pen is not None:
            now = time.monotonic()
            with pen._trail_lock:
                while pen.trail and now - pen.trail[0][2] > TRAIL_SECONDS:
                    pen.trail.popleft()
                trail_snap = list(pen.trail)

            # draw the cursor only while the pen is present, but keep drawing the
            # trail until it ages out so it fades normally after the pen lifts
            draw_cursor = pen.near and pen.pos is not None
            if trail_snap or draw_cursor:
                overlay = pygame.Surface((window_w, window_h), pygame.SRCALPHA)
                radius = max(3.0, args.pen_size * scale / 2)
                points = []
                for x, y, t in trail_snap:
                    rx, ry = rotate_point(x, y, fw, fh, args.rotate)
                    points.append((offset_x + rx * scale, offset_y + ry * scale, t))
                for (x1, y1, _), (x2, y2, t2) in zip(points, points[1:]):
                    alpha = int(180 * max(0.0, 1 - (now - t2) / TRAIL_SECONDS))
                    pygame.draw.line(overlay, (*trail_rgb, alpha), (x1, y1), (x2, y2),
                                     max(2, int(radius * 0.6)))

                if draw_cursor:
                    x, y = pen.pos
                    rx, ry = rotate_point(x, y, fw, fh, args.rotate)
                    center = (offset_x + rx * scale, offset_y + ry * scale)
                    if pen.pressed:
                        pygame.draw.circle(overlay, (*pressed_rgb, 230), center, radius)
                    else:
                        pygame.draw.circle(overlay, (*pen_rgb, 110), center, radius)
                        pygame.draw.circle(overlay, (*pen_rgb, 230), center, radius, width=2)
                window.blit(overlay, (0, 0))

        pygame.display.flip()
        if reader.eof and latest is None:
            running = False
        clock.tick(60)

    if args.screenshot:
        pygame.image.save(window, args.screenshot)

    pygame.quit()
    if pen is not None:
        # quitting the viewer doesn't reliably tear down the upstream
        # pipeline, so kill restream on the tablet like reStream.sh's trap
        subprocess.run(["ssh", *SSH_OPTIONS, "root@%s" % args.source,
                        "killall restream 2>/dev/null; true"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
