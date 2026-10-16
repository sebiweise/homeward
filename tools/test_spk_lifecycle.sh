#!/bin/sh
# Smoke test for a built .spk: replays what DSM does on install, start, run,
# stop, upgrade and uninstall in a temporary /var/packages-like tree.
#
#   tools/test_spk_lifecycle.sh dist/homeward-X.Y.Z-N-x64.spk [python3.12]
#
# Only meaningful on a host whose CPU matches the package (x64 on CI).
set -eu

SPK="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
PYTHON_BIN="$(command -v "${2:-python3.12}")"
ROOT="$(mktemp -d)"
trap 'sh "${ROOT}/scripts/start-stop-status" stop >/dev/null 2>&1 || true; rm -rf "${ROOT}"' EXIT

export SYNOPKG_PKGNAME=homeward
export SYNOPKG_PKGDEST="${ROOT}/target"
export SYNOPKG_PKGVAR="${ROOT}/var"
export SYNOPKG_TEMP_LOGFILE="${ROOT}/temp.log"
export HOMEWARD_SHARE="${ROOT}/share"
export HOMEWARD_PYTHON="${PYTHON_BIN}"

step() { echo "--- $*"; }
fail() { echo "FAIL: $*"; [ -f "${SYNOPKG_PKGVAR}/homeward.log" ] && cat "${SYNOPKG_PKGVAR}/homeward.log"; exit 1; }

unpack() {
    rm -rf "${ROOT}/scripts" "${ROOT}/target"
    mkdir -p "${ROOT}/target" "${ROOT}/var" "${ROOT}/share"
    tar -xf "${SPK}" -C "${ROOT}" scripts INFO
    tar -xOf "${SPK}" package.tgz | tar -xzf - -C "${ROOT}/target"
}

script() {
    script_name=$1
    script_arg=${2:-}
    sh "${ROOT}/scripts/${script_name}" ${script_arg:+"${script_arg}"}
}

wait_for_result() {
    wanted_result=$1
    i=0
    while [ "${i}" -lt 60 ]; do
        if grep -q "\"result\": \"${wanted_result}\"" "${SYNOPKG_PKGVAR}/status.json" 2>/dev/null; then
            return 0
        fi
        i=$((i + 1))
        sleep 1
    done
    fail "no job with result ${wanted_result} in status.json"
}

step "install"
export SYNOPKG_PKG_STATUS=INSTALL
unpack
grep -q '^package="homeward"$' "${ROOT}/INFO" || fail "INFO has no package name"
script preinst
script postinst
"${SYNOPKG_PKGDEST}/env/bin/homeward" --version
"${SYNOPKG_PKGDEST}/env/bin/python" -c "import pyicloud, rich" || fail "pyicloud is not importable"
[ -f "${SYNOPKG_PKGVAR}/config.toml" ] || fail "no default config"

step "start with the default config (no pairs)"
script start-stop-status start
script start-stop-status status || fail "daemon not running"

step "add a pair and run it now"
mkdir -p "${ROOT}/share/nas" "${ROOT}/share/cloud"
echo hello > "${ROOT}/share/nas/a.txt"
cat >> "${SYNOPKG_PKGVAR}/config.toml" <<EOF

[[pair]]
name = "smoke"
local = "${ROOT}/share/nas"
remote = "${ROOT}/share/cloud"
remote_provider = "folder"
mode = "upload"
EOF
kill -USR1 "$(cat "${SYNOPKG_PKGVAR}/homeward.pid")"
wait_for_result ok
[ -f "${ROOT}/share/cloud/a.txt" ] || fail "file was not synced"

step "stop"
script start-stop-status stop
if script start-stop-status status; then fail "daemon still running"; fi

step "upgrade"
# A bidirectional pair saved before the beta flag existed must keep working.
mkdir -p "${ROOT}/share/nas2" "${ROOT}/share/cloud2"
cat >> "${SYNOPKG_PKGVAR}/config.toml" <<EOF

[[pair]]
name = "legacy"
local = "${ROOT}/share/nas2"
remote = "${ROOT}/share/cloud2"
remote_provider = "folder"
mode = "bidirectional"
EOF
export SYNOPKG_PKG_STATUS=UPGRADE
script preupgrade
script preuninst
script postuninst
unpack
script preinst
script postinst
script postupgrade
grep -q 'name = "smoke"' "${SYNOPKG_PKGVAR}/config.toml" || fail "config lost on upgrade"
grep -q 'beta = true' "${SYNOPKG_PKGVAR}/config.toml" || fail "legacy pair not migrated"
[ ! -d "${ROOT}/share/.homeward-backup" ] || fail "upgrade must not write a backup"
script start-stop-status start
script start-stop-status status || fail "daemon not running after upgrade"
script start-stop-status stop

step "uninstall"
export SYNOPKG_PKG_STATUS=UNINSTALL
mkdir -p "${SYNOPKG_PKGVAR}/state/sessions/me" "${SYNOPKG_PKGVAR}/state/sessions-pending/me"
echo cookie > "${SYNOPKG_PKGVAR}/state/sessions/me/trusted"
echo state > "${SYNOPKG_PKGVAR}/state/me.sqlite3"
script preuninst
script postuninst
ls "${ROOT}"/share/.homeward-backup/*/config.toml >/dev/null || fail "no config backup"
ls "${ROOT}"/share/.homeward-backup/*/state/me.sqlite3 >/dev/null || fail "no state backup"
! ls -d "${ROOT}"/share/.homeward-backup/*/state/sessions* 2>/dev/null || fail "sessions copied to the share"
[ -f "${ROOT}/share/cloud/a.txt" ] || fail "uninstall touched synced files"

echo "lifecycle OK"
