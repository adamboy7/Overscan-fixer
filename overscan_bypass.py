#!/usr/bin/env python3
"""
Overscan Bypass
Captures the source monitor, scales it down to fit within the visible area of
an overscanning external monitor, and displays it fullscreen on that monitor.
The real cursor is drawn on top of the preview even when the window is unfocused.

Usage:
  python overscan_bypass.py [--src 0] [--dst 1] [--scale 0.9] [--fps 60]
  python overscan_bypass.py --list
"""

import os
import sys
import json
import argparse
import ctypes
import ctypes.wintypes as wt
import threading
import time

import numpy as np
import pygame
from pygame.locals import DOUBLEBUF, OPENGL, NOFRAME
from OpenGL.GL import (
    GL_CLAMP_TO_EDGE, GL_COLOR_BUFFER_BIT, GL_LINEAR,
    GL_LINE_LOOP, GL_POLYGON, GL_QUADS, GL_RGB, GL_TEXTURE_2D,
    GL_TEXTURE_MAG_FILTER, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_WRAP_S,
    GL_TEXTURE_WRAP_T, GL_UNSIGNED_BYTE, GL_MODELVIEW, GL_PROJECTION,
    GL_DEPTH_TEST,
    glBegin, glBindTexture, glClear, glClearColor, glColor3f,
    glDeleteTextures, glDisable, glEnable, glEnd, glGenTextures,
    glLineWidth, glLoadIdentity, glMatrixMode, glOrtho, glTexCoord2f,
    glTexImage2D, glTexParameteri, glTexSubImage2D, glVertex2f,
)
import dxcam


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
_ARROW = np.array(
    [[0, 0], [0, 15], [4, 10], [7, 17], [10, 15], [7, 9], [12, 9]],
    dtype=np.float32,
)


def _get_cursor_pos():
    pt = wt.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _draw_cursor_gl(abs_cx, abs_cy, src_mon, scale, offset_x, offset_y, dst_h):
    """Draw the cursor arrow as GL geometry. Skips if cursor is off src monitor."""
    src_cx = abs_cx - src_mon['left']
    src_cy = abs_cy - src_mon['top']
    if not (0 <= src_cx < src_mon['width'] and 0 <= src_cy < src_mon['height']):
        return

    dst_cx = src_cx * scale + offset_x
    dst_cy = src_cy * scale + offset_y
    # Convert screen-space (Y-down) arrow pts to GL-space (Y-up)
    pts = _ARROW + [dst_cx, dst_cy]
    pts_gl = [(float(x), float(dst_h - y)) for x, y in pts]

    glColor3f(1.0, 1.0, 1.0)
    glBegin(GL_POLYGON)
    for x, y in pts_gl:
        glVertex2f(x, y)
    glEnd()

    glColor3f(0.0, 0.0, 0.0)
    glLineWidth(1.0)
    glBegin(GL_LINE_LOOP)
    for x, y in pts_gl:
        glVertex2f(x, y)
    glEnd()


# ── OpenGL setup ───────────────────────────────────────────────────────────────

def _init_gl(dst_w, dst_h):
    glMatrixMode(GL_PROJECTION)
    glLoadIdentity()
    # 2-D ortho: origin bottom-left, matches screen-space after Y-flip
    glOrtho(0, dst_w, 0, dst_h, -1, 1)
    glMatrixMode(GL_MODELVIEW)
    glLoadIdentity()
    glDisable(GL_DEPTH_TEST)
    glClearColor(0.0, 0.0, 0.0, 1.0)


def _create_texture(src_w, src_h):
    tex_id = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, tex_id)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    # Allocate GPU texture memory once
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, src_w, src_h, 0,
                 GL_RGB, GL_UNSIGNED_BYTE, None)
    return tex_id


def _draw_frame(tex_id, frame, src_w, src_h,
                offset_x, offset_y, scaled_w, scaled_h, dst_h):
    # Upload RGB frame — no resize, no axis swap; GPU handles scaling
    glBindTexture(GL_TEXTURE_2D, tex_id)
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, src_w, src_h,
                    GL_RGB, GL_UNSIGNED_BYTE, frame)

    # Convert letterbox region from screen-space (Y-down) to GL-space (Y-up)
    x0 = offset_x
    y0 = dst_h - offset_y - scaled_h  # GL bottom = physical bottom of letterbox
    x1 = offset_x + scaled_w
    y1 = dst_h - offset_y             # GL top    = physical top of letterbox

    # t=0 is data row 0 = top of captured image; assign to physically-top vertices (y1)
    glEnable(GL_TEXTURE_2D)
    glColor3f(1.0, 1.0, 1.0)
    glBegin(GL_QUADS)
    glTexCoord2f(0.0, 1.0); glVertex2f(x0, y0)  # physical bottom-left  → source bottom-left
    glTexCoord2f(1.0, 1.0); glVertex2f(x1, y0)  # physical bottom-right → source bottom-right
    glTexCoord2f(1.0, 0.0); glVertex2f(x1, y1)  # physical top-right    → source top-right
    glTexCoord2f(0.0, 0.0); glVertex2f(x0, y1)  # physical top-left     → source top-left
    glEnd()
    glDisable(GL_TEXTURE_2D)


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
        description='Mirror a monitor to another with overscan-compensating scale.'
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
    pygame.display.set_mode(
        (dst_mon['width'], dst_mon['height']),
        DOUBLEBUF | OPENGL | NOFRAME,
    )
    pygame.display.set_caption(f'Overscan Bypass  {scale:.1%}')

    _init_gl(dst_mon['width'], dst_mon['height'])
    tex_id = _create_texture(src_mon['width'], src_mon['height'])

    # ── Background capture thread with DXGI recovery ──────────────────────────
    # One-shot grab() mode: each call is independent so DXGI errors propagate
    # directly to our except handler instead of dying silently in dxcam's thread.
    cam_ref    = [dxcam.create(output_idx=args.src)]

    latest_frame = [None]
    frame_lock   = threading.Lock()
    stop_event   = threading.Event()

    def _recreate_camera():
        cam_ref[0] = None  # release old camera; GC frees DXGI resources
        for attempt in range(20):
            if stop_event.is_set():
                return
            try:
                cam_ref[0] = dxcam.create(output_idx=args.src)
                print("Camera recovered.")
                return
            except Exception:
                stop_event.wait(min(0.25 * (attempt + 1), 5.0))
        print("Warning: camera recovery failed after 20 attempts — display frozen.", file=sys.stderr)

    _FRAME_INTERVAL = 1.0 / args.fps
    _STALE_TIMEOUT  = 2.0

    def _capture_loop():
        last_frame_time = time.monotonic()
        while not stop_event.is_set():
            t0 = time.monotonic()

            if cam_ref[0] is None:
                _recreate_camera()
                last_frame_time = time.monotonic()
                continue

            try:
                f = cam_ref[0].grab()
            except Exception:
                _recreate_camera()
                last_frame_time = time.monotonic()
                continue

            if f is not None:
                with frame_lock:
                    latest_frame[0] = f
                last_frame_time = time.monotonic()
            elif time.monotonic() - last_frame_time > _STALE_TIMEOUT:
                _recreate_camera()
                last_frame_time = time.monotonic()

            elapsed = time.monotonic() - t0
            remaining = _FRAME_INTERVAL - elapsed
            if remaining > 0:
                stop_event.wait(remaining)

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
                        pygame.display.set_caption(f'Overscan Bypass  {scale:.1%}')
                        print(f"Scale: {scale:.1%}  →  {scaled_w}x{scaled_h}")
                    elif event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                        scale = max(0.1, round(scale - SCALE_STEP, 3))
                        scaled_w, scaled_h, offset_x, offset_y = recompute_layout(scale)
                        _save_scale(scale)
                        pygame.display.set_caption(f'Overscan Bypass  {scale:.1%}')
                        print(f"Scale: {scale:.1%}  →  {scaled_w}x{scaled_h}")

            with frame_lock:
                frame = latest_frame[0]

            if frame is None:
                clock.tick(args.fps)
                continue

            glClear(GL_COLOR_BUFFER_BIT)
            _draw_frame(tex_id, frame, src_mon['width'], src_mon['height'],
                        offset_x, offset_y, scaled_w, scaled_h, dst_mon['height'])

            cx, cy = _get_cursor_pos()
            _draw_cursor_gl(cx, cy, src_mon, scale, offset_x, offset_y, dst_mon['height'])

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
        cam_ref[0] = None  # release DXGI resources
        glDeleteTextures([tex_id])
        pygame.quit()


if __name__ == '__main__':
    main()
