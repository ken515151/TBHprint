"""Generate build/windows dist assets from tbhprint.applet.icons at build time.

Run with the *build machine's* Python (`py -3`), which needs Pillow - this
is NOT run inside the bundled runtime, it just draws a couple of files
that get copied into it. Kept as a separate script (rather than inline in
build.ps1) so it can be unit-tested / run by hand and so build.ps1 does
not need a multi-line embedded Python string.

Usage:
    py -3 gen_icon.py <repo_root> <out_ico_path>
"""

from __future__ import annotations

import sys
from pathlib import Path

ICO_SIZES = (16, 32, 48, 256)
# Neutral grey - this is the *application* icon (Start Menu, uninstaller,
# taskbar-while-loading), not a live status indicator, so we use the same
# "starting/unpaired" grey tkinter.icons already defines for "no signal
# yet" rather than inventing a new colour just for this file.
ICON_STATE = "starting"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: gen_icon.py <repo_root> <out_ico_path>", file=sys.stderr)
        return 2
    repo_root, out_path = Path(argv[1]), Path(argv[2])
    sys.path.insert(0, str(repo_root))
    from tbhprint.applet import icons  # noqa: E402  (path set above)

    largest = icons.render(ICON_STATE, size=max(ICO_SIZES))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    largest.save(out_path, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"wrote {out_path} ({', '.join(str(s) for s in ICO_SIZES)}px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
