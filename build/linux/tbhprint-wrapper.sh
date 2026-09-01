#!/bin/sh
# Installed to /usr/bin/tbhprint. A thin wrapper so shop staff (and the
# desktop entries) just type/run "tbhprint" - the real interpreter is the
# venv's own, which has tbhprint and its pinned deps installed and can
# see the system's tkinter/Pillow/GI via --system-site-packages.
exec /opt/tbhprint/venv/bin/python -m tbhprint "$@"
