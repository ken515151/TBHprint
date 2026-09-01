"""Tray icons drawn with Pillow - no image assets to ship or lose.

A rounded square "printer" with a paper sheet, tinted by state so the
desk can read the agent's health from the tray without opening anything:

  green   connected (realtime)        amber  degraded (polling) / paused
  red     error / unpaired-but-tried  grey   unpaired / starting
"""

from __future__ import annotations

from PIL import Image, ImageDraw

STATE_COLOURS = {
    "connected": (46, 160, 67),
    "degraded": (230, 160, 11),
    "paused": (230, 160, 11),
    "error": (200, 50, 50),
    "disconnected": (200, 50, 50),
    "unpaired": (130, 130, 130),
    "starting": (130, 130, 130),
}


def colour_for(state: str) -> tuple[int, int, int]:
    return STATE_COLOURS.get(state, STATE_COLOURS["starting"])


def render(state: str, size: int = 64, badge: int = 0) -> Image.Image:
    """Icon image for a daemon state; `badge` > 0 draws a small count dot
    (active jobs) so a stuck queue is visible at a glance."""
    colour = colour_for(state)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    s = size
    # printer body
    draw.rounded_rectangle((s * 0.08, s * 0.38, s * 0.92, s * 0.80), radius=s * 0.10, fill=colour)
    # paper sheet sticking out of the top
    draw.rectangle((s * 0.26, s * 0.14, s * 0.74, s * 0.46), fill=(255, 255, 255), outline=colour, width=max(1, s // 32))
    # output slot
    draw.rectangle((s * 0.22, s * 0.62, s * 0.78, s * 0.70), fill=(255, 255, 255))
    # status lamp
    draw.ellipse((s * 0.74, s * 0.46, s * 0.84, s * 0.56), fill=(255, 255, 255))
    if badge > 0:
        r = s * 0.20
        draw.ellipse((s - 2 * r, 0, s, 2 * r), fill=(20, 20, 20))
        text = str(min(badge, 9))
        draw.text((s - r - s * 0.06, r - s * 0.12), text, fill=(255, 255, 255))
    return image
