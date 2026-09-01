#!/usr/bin/env bash
# Builds dist/tbhprint_<version>_all.deb.
# docs/DISTRIBUTION_DESIGN.md section 6 is binding for the package's
# shape - read that before changing anything here.
#
# Runs itself inside `docker run ubuntu:22.04` by default - this is
# deliberate, not just a CI convenience: building against the exact base
# Ubuntu 22.04 ships is what proves the repo's stated Python 3.10 floor
# (docs section 1) rather than silently building against whatever Python
# happens to be on the maintainer's own machine.
#
# Usage:
#   build/linux/build.sh                    # re-execs itself in Docker
#   TBHPRINT_NO_DOCKER=1 build/linux/build.sh   # build directly here
#     (only if this host already looks like the target: apt, dpkg-dev,
#     python3-pip, python3-pil present - the release/CI job runs the
#     Docker path)
#   TBHPRINT_WHEELS_DIR=<dir> build/linux/build.sh
#     use already-downloaded wheels from <dir> instead of hitting PyPI
#     again (offline rebuilds; also how this repo's own verification runs
#     were done, in a sandbox whose Docker containers cannot reach PyPI
#     even though the host can - see the final build report).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# -- 0. re-exec inside Docker unless told not to --------------------------------

if [ "${TBHPRINT_IN_DOCKER:-0}" != "1" ] && [ "${TBHPRINT_NO_DOCKER:-0}" != "1" ]; then
    echo "Re-running in docker run ubuntu:22.04 (TBHPRINT_NO_DOCKER=1 to build directly on this host instead) ..."
    docker_args=(--rm -v "$REPO_ROOT:/src" -e TBHPRINT_IN_DOCKER=1 -w /src)
    if [ -n "${TBHPRINT_WHEELS_DIR:-}" ]; then
        docker_args+=(-v "${TBHPRINT_WHEELS_DIR}:/wheels-cache:ro" -e TBHPRINT_WHEELS_DIR=/wheels-cache)
    fi
    exec docker run "${docker_args[@]}" ubuntu:22.04 bash build/linux/build.sh
fi

# -- 1. build-time dependencies (dpkg-dev, pip, Pillow for icon gen) ------------

need_apt_install=()
command -v dpkg-deb >/dev/null 2>&1 || need_apt_install+=(dpkg-dev)
python3 -c "import pip" >/dev/null 2>&1 || need_apt_install+=(python3-pip)
python3 -c "import PIL" >/dev/null 2>&1 || need_apt_install+=(python3-pil)
if [ "${#need_apt_install[@]}" -gt 0 ]; then
    echo "Installing build deps: ${need_apt_install[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq "${need_apt_install[@]}"
fi
python3 --version

# -- 2. version, from pyproject.toml (never edited here - read only) -----------

# tr -dc: pyproject.toml may have CRLF line endings (this repo is authored
# on Windows) - sed's substitution only replaces the matched span, so a
# trailing \r past the closing quote survives into $VERSION unless
# stripped explicitly. A version with an embedded \r produces a package
# filename Windows tools can list but not open - the exact failure mode
# that caught this.
# pyproject.toml declares version = dynamic (attr tbhprint.__version__), so
# tbhprint/__init__.py is the single source of truth.
VERSION="$(sed -nE 's/^__version__ *= *"([^"]+)"/\1/p' "$REPO_ROOT/tbhprint/__init__.py" | head -n1 | tr -dc '0-9A-Za-z.+_-')"
if [ -z "$VERSION" ]; then
    echo "could not find version = \"...\" in pyproject.toml" >&2
    exit 1
fi
echo "TBHprint version: $VERSION"

WORK_DIR="$(mktemp -d /tmp/tbhprint-linux-build.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT
PKGROOT="$WORK_DIR/pkgroot"
DIST_DIR="$REPO_ROOT/dist"
mkdir -p "$DIST_DIR"

# -- 3. vendor the pure-python wheels -------------------------------------------

WHEELS_OUT="$PKGROOT/opt/tbhprint/wheels"
mkdir -p "$WHEELS_OUT"

if [ -n "${TBHPRINT_WHEELS_DIR:-}" ] && [ -d "${TBHPRINT_WHEELS_DIR}" ]; then
    echo "Using pre-fetched wheels from ${TBHPRINT_WHEELS_DIR}"
    cp "${TBHPRINT_WHEELS_DIR}"/*.whl "$WHEELS_OUT/"
else
    echo "Downloading pinned pure-python wheels ..."
    # --no-deps: build/linux/requirements.txt pins the full transitive
    # closure itself (see that file's own comment) - this keeps the
    # vendored set reproducible instead of "whatever pip resolves today".
    python3 -m pip download --no-deps --only-binary=:all: \
        --implementation py --abi none --platform any \
        -r "$HERE/requirements.txt" -d "$WHEELS_OUT"
fi

# Refuse to ship anything that isn't a pure-python wheel - Architecture:
# all promises the .deb runs on any CPU architecture, which is only true
# if nothing in here is arch-specific.
for whl in "$WHEELS_OUT"/*.whl; do
    case "$(basename "$whl")" in
        *-py3-none-any.whl|*-py2.py3-none-any.whl|*-py2-none-any.whl) ;;
        *)
            echo "refusing to vendor a non-pure-python wheel: $(basename "$whl")" >&2
            exit 1
            ;;
    esac
done
echo "Vendored $(ls "$WHEELS_OUT"/*.whl | wc -l) wheels."

# -- 4. the tbhprint wheel itself, built HERE, never on the shop machine -------
# First cut had postinst pip-build /opt/tbhprint/app on the target; on Ubuntu
# 22.04 pip's isolated build env under a --system-site-packages venv picked
# the distro's setuptools 59 and produced "UNKNOWN-0.0.0" (no tbhprint module,
# service crash-looping). A pre-built pure wheel removes setuptools from the
# install path entirely: postinst only ever unpacks wheels.
if [ -n "${TBHPRINT_WHEELS_DIR:-}" ] && [ -d "${TBHPRINT_WHEELS_DIR}" ]; then
    python3 -m pip install -q --no-index --find-links "$TBHPRINT_WHEELS_DIR" "setuptools>=68" wheel
else
    python3 -m pip install -q "setuptools>=68" wheel
fi
python3 -m pip wheel -q --no-deps --no-build-isolation -w "$WHEELS_OUT" "$REPO_ROOT"
ls "$WHEELS_OUT"/tbhprint-"$VERSION"-py3-none-any.whl >/dev/null || { echo "tbhprint wheel for $VERSION was not built" >&2; exit 1; }
echo "Built tbhprint-$VERSION-py3-none-any.whl."

# -- 5. icons --------------------------------------------------------------------

ICON_STAGE="$WORK_DIR/icons"
python3 "$HERE/gen_icons.py" "$REPO_ROOT" "$ICON_STAGE"
for size_dir in "$ICON_STAGE"/*/; do
    size="$(basename "$size_dir")"
    dest="$PKGROOT/usr/share/icons/hicolor/$size/apps"
    mkdir -p "$dest"
    cp "$size_dir/tbhprint.png" "$dest/tbhprint.png"
done

# -- 6. everything else: units, desktop entries, wrapper, conffile -------------

mkdir -p "$PKGROOT/usr/bin"
install -m 0755 "$HERE/tbhprint-wrapper.sh" "$PKGROOT/usr/bin/tbhprint"

mkdir -p "$PKGROOT/lib/systemd/system"
install -m 0644 "$REPO_ROOT/packaging/tbhprint.service" "$PKGROOT/lib/systemd/system/"
install -m 0644 "$REPO_ROOT/packaging/tbhprint-update.path" "$PKGROOT/lib/systemd/system/"
install -m 0644 "$REPO_ROOT/packaging/tbhprint-update.service" "$PKGROOT/lib/systemd/system/"
install -m 0644 "$REPO_ROOT/packaging/tbhprint-update-check.service" "$PKGROOT/lib/systemd/system/"
install -m 0644 "$REPO_ROOT/packaging/tbhprint-update.timer" "$PKGROOT/lib/systemd/system/"

mkdir -p "$PKGROOT/usr/lib/tbhprint"
install -m 0755 "$REPO_ROOT/packaging/tbhprint-apply-update.sh" "$PKGROOT/usr/lib/tbhprint/apply-update.sh"

mkdir -p "$PKGROOT/usr/share/applications"
install -m 0644 "$REPO_ROOT/packaging/tbhprint.desktop" "$PKGROOT/usr/share/applications/"

mkdir -p "$PKGROOT/etc/xdg/autostart"
install -m 0644 "$REPO_ROOT/packaging/tbhprint-tray.desktop" "$PKGROOT/etc/xdg/autostart/"

mkdir -p "$PKGROOT/etc/tbhprint"
install -m 0640 "$REPO_ROOT/packaging/config.example.json" "$PKGROOT/etc/tbhprint/config.json"

# -- 7. DEBIAN control area -------------------------------------------------------

mkdir -p "$PKGROOT/DEBIAN"
sed "s/__VERSION__/$VERSION/" "$HERE/debian/control" > "$PKGROOT/DEBIAN/control"
sed "s/__VERSION__/$VERSION/" "$HERE/debian/postinst" > "$PKGROOT/DEBIAN/postinst"   # pins the wheel version
chmod 755 "$PKGROOT/DEBIAN/postinst"
install -m 0755 "$HERE/debian/prerm" "$PKGROOT/DEBIAN/prerm"
install -m 0755 "$HERE/debian/postrm" "$PKGROOT/DEBIAN/postrm"
install -m 0644 "$HERE/debian/conffiles" "$PKGROOT/DEBIAN/conffiles"

# -- 8. build ----------------------------------------------------------------------

OUT_DEB="$DIST_DIR/tbhprint_${VERSION}_all.deb"
rm -f "$OUT_DEB"
dpkg-deb --build --root-owner-group "$PKGROOT" "$OUT_DEB"
dpkg-deb --info "$OUT_DEB"
echo ""
echo "Built $OUT_DEB"
