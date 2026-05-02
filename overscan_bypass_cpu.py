#!/usr/bin/env python3
"""
Overscan Bypass (CPU)
Captures the source monitor via mss (CPU), scales it with Pygame's software
renderer, and blits it fullscreen on the destination monitor.
No GPU/DXGI/OpenGL required.

Usage:
  python overscan_bypass_cpu.py [--src 0] [--dst 1] [--scale 0.9] [--fps 60]
  python overscan_bypass_cpu.py --list
"""

import os
import sys
import json
import argparse
import ctypes
import ctypes.wintypes as wt
import threading
import time

import pygame
from pygame.locals import DOUBLEBUF, NOFRAME
import mss


# ── Monitor enumeration ────────────────────────────────────────────────────────

def get_monitors():
    monitors = []

    def _callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
        r = lprcMonitor.contents
        monitors.append({
            'left':   r.left,
            'top':    r.top,
            'right':  r.right,
            'bottom': r.bottom,
            'width':  r.right  - r.left,
            'height': r.bottom - r.top,
        })
        return True

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM
    )
    ctypes.windll.user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_callback), 0)
    return monitors


# ── Cursor rendering ───────────────────────────────────────────────────────────

# Arrow polygon pixel offsets from hotspot (screen-space, Y-down)
_ARROW = [(0, 0), (0, 15), (4, 10), (7, 17), (10, 15), (7, 9), (12, 9)]


def _get_cursor_pos():
    pt = wt.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _draw_cursor_pygame(screen, abs_cx, abs_cy, src_mon, scale, offset_x, offset_y):
    """Draw the cursor arrow using pygame.draw. Skips if cursor is off src monitor."""
    src_cx = abs_cx - src_mon['left']
    src_cy = abs_cy - src_mon['top']
    if not (0 <= src_cx < src_mon['width'] and 0 <= src_cy < src_mon['height']):
        return
    dst_cx = src_cx * scale + offset_x
    dst_cy = src_cy * scale + offset_y
    pts = [(int(x + dst_cx), int(y + dst_cy)) for x, y in _ARROW]
    pygame.draw.polygon(screen, (255, 255, 255), pts)
    pygame.draw.polygon(screen, (0, 0, 0), pts, 1)


# ── Persistent config ─────────────────────────────────────────────────────────

_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'overscan_config.json')


def _load_scale():
    try:
        with open(_CONFIG_FILE) as f:
            return float(json.load(f)['scale'])
    except (OSError, KeyError, ValueError, TypeError):
        return None


def _save_scale(scale):
    try:
        with open(_CONFIG_FILE, 'w') as f:
            json.dump({'scale': scale}, f)
    except OSError as e:
        print(f"Warning: could not save scale: {e}", file=sys.stderr)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Mirror a monitor to another with overscan-compensating scale (CPU renderer).'
    )
    parser.add_argument('--src',   type=int,   default=0,    help='Source monitor index (default: 0)')
    parser.add_argument('--dst',   type=int,   default=1,    help='Destination monitor index (default: 1)')
    parser.add_argument('--scale', type=float, default=None, help='Content scale (default: fit source into dest)')
    parser.add_argument('--fps',   type=int,   default=60,   help='Target FPS (default: 60)')
    parser.add_argument('--list',  action='store_true',      help='List monitors and exit')
    args = parser.parse_args()
    if args.fps < 1:
        sys.exit("Error: --fps must be at least 1")

    ctypes.windll.shcore.SetProcessDpiAwareness(2)

    monitors = get_monitors()
    print(f"Detected {len(monitors)} monitor(s):")
    for i, m in enumerate(monitors):
        print(f"  [{i}] {m['width']}x{m['height']} at ({m['left']}, {m['top']})")

    if args.list:
        return

    if not (0 <= args.src < len(monitors)):
        sys.exit(f"Error: --src {args.src} is out of range (have {len(monitors)} monitors)")
    if not (0 <= args.dst < len(monitors)):
        sys.exit(f"Error: --dst {args.dst} is out of range (have {len(monitors)} monitors)")
    if args.src == args.dst:
        sys.exit("Error: --src and --dst must be different monitors")

    src_mon   = monitors[args.src]
    dst_mon   = monitors[args.dst]
    fit_scale   = min(dst_mon['width'] / src_mon['width'], dst_mon['height'] / src_mon['height'])
    saved_scale = _load_scale() if args.scale is None else None
    scale       = max(0.1, args.scale if args.scale is not None else
                               (saved_scale if saved_scale is not None else fit_scale))

    scaled_w = int(src_mon['width']  * scale)
    scaled_h = int(src_mon['height'] * scale)
    offset_x = (dst_mon['width']  - scaled_w) // 2
    offset_y = (dst_mon['height'] - scaled_h) // 2

    print(f"\nSource [{args.src}]: {src_mon['width']}x{src_mon['height']}")
    print(f"Dest   [{args.dst}]: {dst_mon['width']}x{dst_mon['height']}")
    print(f"Scale:  {scale:.1%}  →  {scaled_w}x{scaled_h} centered at ({offset_x}, {offset_y})")
    print("Hotkeys: +/= scale up  |  - scale down  |  ESC quit\n")

    os.environ['SDL_VIDEO_WINDOW_POS'] = f"{dst_mon['left']},{dst_mon['top']}"

    pygame.init()
    screen = pygame.display.set_mode(
        (dst_mon['width'], dst_mon['height']),
        DOUBLEBUF | NOFRAME,
    )
    pygame.display.set_caption(f'Overscan Bypass (CPU)  {scale:.1%}')

    # mss capture region in absolute screen coordinates
    capture_rect = {
        'left':   src_mon['left'],
        'top':    src_mon['top'],
        'width':  src_mon['width'],
        'height': src_mon['height'],
    }

    # ── Background capture thread ─────────────────────────────────────────────
    latest_frame = [None]   # raw RGB bytes from last grab
    frame_lock   = threading.Lock()
    stop_event   = threading.Event()

    def _capture_loop():
        with mss.mss() as sct:
            while not stop_event.is_set():
                try:
                    shot = sct.grab(capture_rect)
                    # .rgb is a memoryview; bytes() copies it for thread-safe handoff
                    rgb = bytes(shot.rgb)
                    with frame_lock:
                        latest_frame[0] = rgb
                except Exception as e:
                    print(f"Capture error: {e}", file=sys.stderr)
                    time.sleep(0.1)

    capture_thread = threading.Thread(target=_capture_loop, daemon=True)
    capture_thread.start()

    SCALE_STEP = 0.025

    def recompute_layout(s):
        sw = int(src_mon['width']  * s)
        sh = int(src_mon['height'] * s)
        ox = (dst_mon['width']  - sw) // 2
        oy = (dst_mon['height'] - sh) // 2
        return sw, sh, ox, oy

    clock = pygame.time.Clock()

    try:
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        return
                    elif event.key in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
                        scale = round(scale + SCALE_STEP, 3)
                        scaled_w, scaled_h, offset_x, offset_y = recompute_layout(scale)
                        _save_scale(scale)
                        pygame.display.set_caption(f'Overscan Bypass (CPU)  {scale:.1%}')
                        print(f"Scale: {scale:.1%}  →  {scaled_w}x{scaled_h}")
                    elif event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                        scale = max(0.1, round(scale - SCALE_STEP, 3))
                        scaled_w, scaled_h, offset_x, offset_y = recompute_layout(scale)
                        _save_scale(scale)
                        pygame.display.set_caption(f'Overscan Bypass (CPU)  {scale:.1%}')
                        print(f"Scale: {scale:.1%}  →  {scaled_w}x{scaled_h}")

            with frame_lock:
                frame_data = latest_frame[0]

            if frame_data is None:
                clock.tick(args.fps)
                continue

            screen.fill((0, 0, 0))
            src_surface    = pygame.image.frombuffer(
                frame_data, (src_mon['width'], src_mon['height']), 'RGB'
            )
            scaled_surface = pygame.transform.smoothscale(src_surface, (scaled_w, scaled_h))
            screen.blit(scaled_surface, (offset_x, offset_y))

            cx, cy = _get_cursor_pos()
            _draw_cursor_pygame(screen, cx, cy, src_mon, scale, offset_x, offset_y)

            pygame.display.flip()
            clock.tick(args.fps)

    finally:
        stop_event.set()
        try:
            hwnd = pygame.display.get_wm_info().get('window')
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE — instant visual close
        except Exception:
            pass
        capture_thread.join(timeout=2.0)
        if capture_thread.is_alive():
            print("Warning: capture thread did not exit cleanly.", file=sys.stderr)
        pygame.quit()


if __name__ == '__main__':
    main()
