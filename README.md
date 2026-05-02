# Overscan Bypass

Captures a source monitor and re-displays it on a destination monitor with the image scaled down to fit inside the overscanned visible area — giving you access to every pixel without touching the TV or display's menu.

---

## The problem

Overscan is a holdover from CRT television manufacturing tolerances: sets were built to slightly over-magnify the picture so that the raw, unclean edges of the analog signal would fall outside the visible tube area. Modern flat-panel TVs kept the behaviour by default so that DVD and broadcast content — which was mastered with this in mind — still looked correct.

The result for PC users: the display physically shows only a cropped window of whatever signal you send it. On a 1080p panel with 5% overscan, roughly 54 pixels are lost on every edge. Task bars get clipped. Window chrome disappears. You can't reach corners with the mouse.

The right fix is to turn overscan off, but many displays make this difficult or impossible:

- Budget and mid-range TVs often bury the setting under an obscure label ("Just Scan", "Screen Fit", "1:1 Pixel", "Dot by Dot") that only appears for certain input sources or resolutions.
- Some displays expose no setting at all and simply apply a fixed zoom in hardware.
- Others nominally support EDID — the handshake protocol where the monitor tells the GPU its capabilities — but ignore the underscan flag that graphics drivers send.
- Firmware updates and input-mode changes can silently re-enable overscan, requiring you to dig through menus again.

Overscan Bypass works around all of these: it pre-shrinks the image in software so the overscanned portion is blank black border, and the content you care about lands entirely within the visible area.

---

## How it works

```
┌─────────────────────────────────────┐
│  Source monitor (captured)          │
│  e.g. your primary desktop          │
└─────────────────────────────────────┘
            │  capture
            ▼
┌─────────────────────────────────────┐
│  Scale down (e.g. 90%)              │
│  Centre in destination resolution   │
└─────────────────────────────────────┘
            │  render
            ▼
┌─────────────────────────────────────┐
│  Destination monitor (fullscreen)   │
│  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │  ← overscan border (eaten by display)
│  ░░ ┌────────────────────────┐ ░░  │
│  ░░ │   your desktop content │ ░░  │  ← visible content, fully inside
│  ░░ └────────────────────────┘ ░░  │
│  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │
└─────────────────────────────────────┘
```

1. **Capture** — A background thread continuously grabs frames from the source monitor.
   - The GPU version uses **DXGI Desktop Duplication** via `dxcam` — a DirectX 11 API that lets applications receive the composed desktop image directly from the GPU with minimal overhead and no visible capture indicator.
   - The CPU version uses **`mss`**, which reads the screen through the GDI (Windows Graphics Device Interface) — no GPU or DirectX required, useful when DXGI is unavailable or disabled.

2. **Scale and letterbox** — Each frame is scaled down to fit within the destination monitor's resolution, leaving a black border. The default scale is computed automatically: `min(dst_width / src_width, dst_height / src_height)`. You can tune it at runtime with hotkeys.

3. **Render fullscreen** —
   - The GPU version uploads the captured frame to an OpenGL texture and draws it as a textured quad covering the letterbox region. GPU bilinear filtering handles the scale with no CPU cost.
   - The CPU version uses `pygame.transform.smoothscale` to resize the frame in software, then blits the result onto a Pygame surface.

4. **Cursor overlay** — Since the window sits on top of the destination monitor and the user's real cursor is operating on the source monitor, the app reads the cursor position via the Windows API and draws a matching arrow polygon on top of the rendered frame, scaled and offset to match the content region.

---

## Versions

| | `overscan_bypass.py` | `overscan_bypass_cpu.py` |
|---|---|---|
| Capture | DXGI (`dxcam`) | GDI (`mss`) |
| Render | OpenGL | Pygame software |
| Dependencies | `dxcam`, `PyOpenGL`, `numpy`, `pygame` | `mss`, `pygame` |
| GPU required | Yes (DirectX 11) | No |
| Performance | Best — GPU handles scaling | Good up to ~1440p@60; drop to 30fps for 4K |
| Stability | DXGI can crash on driver events; auto-recovers | mss is stable; simple error restart |

Use the GPU version if you can — it's lower latency and uses negligible CPU. Use the CPU version if your system doesn't support DXGI Desktop Duplication (older drivers, some VMs, or Remote Desktop sessions where DXGI is disabled).

---

## Installation

```
pip install -r requirements.txt         # GPU version
pip install -r requirements_cpu.txt     # CPU version
```

Python 3.8+ on Windows is required. Both versions use Windows-only APIs for monitor enumeration and cursor position.

---

## Usage

```
python overscan_bypass.py [--src 0] [--dst 1] [--scale 0.9] [--fps 60]
python overscan_bypass.py --list
```

```
python overscan_bypass_cpu.py [--src 0] [--dst 1] [--scale 0.9] [--fps 60]
python overscan_bypass_cpu.py --list
```

| Argument | Default | Description |
|---|---|---|
| `--src` | `0` | Index of the monitor to capture (your desktop) |
| `--dst` | `1` | Index of the overscanning monitor to display on |
| `--scale` | auto | Content scale factor (0.1–1.0). Defaults to the largest value that fits the source inside the destination. |
| `--fps` | `60` | Target capture and render framerate |
| `--list` | — | Print detected monitors and exit |

### Hotkeys (while running)

| Key | Action |
|---|---|
| `+` / `=` / numpad `+` | Increase scale by 2.5% |
| `-` / numpad `-` | Decrease scale by 2.5% |
| `ESC` | Quit |

### Finding the right scale

Run with `--list` first to confirm which monitor index is which. Then launch without `--scale` — the app will auto-fit the source into the destination. If content is still clipped, adjust the scale with the `+`/`-` hotkeys until everything is visible. The app saves your scale automatically and restores it on the next run.

---

## Limitations

- **Windows only.** Monitor enumeration, cursor position, and DXGI capture all use Win32 APIs.
- **The destination monitor runs as a fullscreen borderless window.** Other windows can still be moved on top of it, but in practice you'd dedicate the TV/display to this tool.
- **The source monitor is mirrored, not extended.** The tool captures and re-displays; it doesn't reposition the Windows virtual desktop. Run your content on the source monitor and let the bypass mirror it.
- **No HDR.** Both capture paths return SDR RGB. HDR content is tone-mapped by the OS before capture.
- **DRM-protected content will not capture correctly.** Services like Netflix, Disney+, and other Widevine/PlayReady protected streams render video into hardware-protected buffers that are intentionally inaccessible to capture APIs. The video region will appear black or corrupted while unprotected UI elements (browser chrome, overlays) remain visible. This is enforced at the OS and driver level and cannot be worked around.
