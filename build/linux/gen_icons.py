"""Generate the hicolor icon theme PNGs from tbhprint.applet.icons.

Run with the build machine's python3 (Pillow comes from the distro package
python3-pil there, same as it does on an installed machine - see
docs/DISTRIBUTION_DESIGN.md section 6, "Pillow comes from the distro").

Usage:
    python3 gen_icons.py <repo_root> <out_dir>

Writes <out_dir>/<size>x<size>/tbhprint.png for each size in ICON_SIZES -
i.e. ready to copy straight into a hicolor icon theme tree at
usr/share/icons/hicolor/<size>x<size>/apps/tbhprint.png.
"""

from __future__ import annotations

import sys
from pathlib import Path

ICON_SIZES = (16, 32, 48, 64, 128, 256)
# Same reasoning as build/windows/gen_icon.py: this is the static
# application icon, not a live status dot, so use the neutral grey.
ICON_STATE = "starting"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: gen_icons.py <repo_root> <out_dir>", file=sys.stderr)
        return 2
    repo_root, out_dir = Path(argv[1]), Path(argv[2])
    sys.path.insert(0, str(repo_root))
    from tbhprint.applet import icons  # noqa: E402  (path set above)

    for size in ICON_SIZES:
        image = icons.render(ICON_STATE, size=size)
        dest_dir = out_dir / f"{size}x{size}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "tbhprint.png"
        image.save(dest, format="PNG")
        print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
