#!/bin/sh
# Installed to /usr/lib/tbhprint/apply-update.sh; run as root by
# tbhprint-update.service (itself triggered by tbhprint-update.path
# noticing /var/lib/tbhprint/update/requested). See
# docs/DISTRIBUTION_DESIGN.md section 4.
#
# Why re-verify the sha256 here, a second time: the daemon (unprivileged,
# tbhprint user) already checked it once before writing the marker file,
# but this script is the point a mismatched/tampered .deb would get
# installed AS ROOT - so it checks again itself rather than trusting that
# first check.
set -eu

UPDATE_DIR=/var/lib/tbhprint/update
MARKER="$UPDATE_DIR/requested"

[ -f "$MARKER" ] || exit 0

DEB=$(ls -t "$UPDATE_DIR"/*.deb 2>/dev/null | head -n1 || true)
if [ -z "$DEB" ] || [ ! -f "$DEB.sha256" ]; then
    echo "tbhprint-update: no .deb/.sha256 pair in $UPDATE_DIR - clearing the request and doing nothing" >&2
    rm -f "$MARKER"
    exit 0
fi

# Clear the request BEFORE attempting the install: the path unit re-fires
# whenever the marker exists and this service is idle, so a failing
# apt-get/dpkg would otherwise loop forever. A failed .deb is kept beside
# its .sha256 for diagnosis; the agent's next 6-hourly cycle stages it
# again if the server still offers that version.
rm -f "$MARKER"

cd "$UPDATE_DIR"
if ! sha256sum -c "$(basename "$DEB").sha256" --status; then
    echo "tbhprint-update: sha256 mismatch for $DEB - refusing to install it" >&2
    rm -f "$DEB" "$DEB.sha256"
    exit 1
fi

if ! apt-get install -y "$DEB"; then
    echo "tbhprint-update: apt-get install failed, falling back to dpkg -i" >&2
    dpkg -i "$DEB"
fi

systemctl restart tbhprint.service || true
