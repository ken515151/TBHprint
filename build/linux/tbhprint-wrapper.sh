#!/bin/sh
# Installed to /usr/bin/tbhprint. A thin wrapper so shop staff (and the
# desktop entries) just type/run "tbhprint" - the real interpreter is the
# venv's own, which has tbhprint and its pinned deps installed and can
# see the system's tkinter/Pillow/GI via --system-site-packages.
#
# Group hand-off (2026-09-02, Mint desktop): postinst adds the person who
# installed the package to the "tbhprint" group so the tray / CLI can reach
# the agent's control socket (0660 tbhprint:tbhprint) - but a group added
# while you are logged in only takes effect after you log out and back in,
# and the "log out and back in" hint postinst prints goes to a terminal
# that Mint's graphical package installer never shows. Result on a fresh
# install: the tray autostarts, says "Agent not running", and nothing
# explains why. So: when the caller is listed as a member of the group in
# /etc/group but the running session does not carry it, re-exec through
# `sg tbhprint` (no password for listed members) and carry on as if they
# had re-logged. Root and already-in-group callers fall straight through.
PY=/opt/tbhprint/venv/bin/python

needs_sg() {
    [ "$(id -u)" -ne 0 ] || return 1
    id -Gn 2>/dev/null | tr ' ' '\n' | grep -qx tbhprint && return 1
    command -v sg >/dev/null 2>&1 || return 1
    getent group tbhprint | cut -d: -f4 | tr ',' '\n' | grep -qx "$(id -un)"
}

if needs_sg; then
    # sg takes ONE command string: quote every argument for /bin/sh so
    # names with spaces ("--name 'Front desk PC'") survive the hand-off.
    cmd="exec $PY -m tbhprint"
    for arg in "$@"; do
        quoted=$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")
        cmd="$cmd '$quoted'"
    done
    exec sg tbhprint -c "$cmd"
fi

exec "$PY" -m tbhprint "$@"
