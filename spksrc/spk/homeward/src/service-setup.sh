# Homeward service setup for spksrc (SynoCommunity).
#
# Same runtime layout as the package built by tools/build_spk.py:
#   ${SYNOPKG_PKGDEST}/env   Python virtualenv with Homeward and its dependencies
#   ${SYNOPKG_PKGDEST}/ui    the DSM app (from cross/homeward)
#   ${SYNOPKG_PKGVAR}        config.toml, state/, status.json, homeward.log (kept on upgrade)
#
# spksrc's start-stop-status and installer source this file, so the SERVICE_* and
# SVC_* variables are read there.
# shellcheck shell=sh disable=SC2034

PYTHON_DIR="/var/packages/python312/target/bin"
PATH="${SYNOPKG_PKGDEST}/env/bin:${SYNOPKG_PKGDEST}/bin:${PYTHON_DIR}:${PATH}"

# Libraries that write to ~ (cookies, caches) stay inside the package.
HOME="${SYNOPKG_PKGVAR}"
HOMEWARD_CONFIG="${SYNOPKG_PKGVAR}/config.toml"
HOMEWARD_VAR="${SYNOPKG_PKGVAR}"
export HOME HOMEWARD_CONFIG HOMEWARD_VAR

SERVICE_COMMAND="${SYNOPKG_PKGDEST}/env/bin/homeward-daemon --config ${HOMEWARD_CONFIG} --log-file ${SYNOPKG_PKGVAR}/homeward.log"
SVC_BACKGROUND=y
SVC_WRITE_PID=y
# The DSM app shows homeward.log; keep it across restarts (the daemon rotates it).
SVC_KEEP_LOG=y
SVC_CWD="${SYNOPKG_PKGVAR}"
# A running sync records its finished steps on SIGTERM before it exits.
SVC_WAIT_TIMEOUT=45

service_postinst ()
{
    install_python_virtualenv
    install_python_wheels

    mkdir -p "${SYNOPKG_PKGVAR}/state"
    if [ ! -f "${HOMEWARD_CONFIG}" ]; then
        echo "Writing the default configuration to ${HOMEWARD_CONFIG}"
        cat > "${HOMEWARD_CONFIG}" <<CONF
# Homeward configuration. Add accounts, photos backups and sync pairs in the
# Homeward app in DSM. All options: https://github.com/sebiweise/homeward/blob/master/docs/user-guide.md
state_dir = "state"

[daemon]
interval_minutes = 60
CONF
    fi
}

service_postupgrade ()
{
    "${SYNOPKG_PKGDEST}/env/bin/python3" -m homeward.api.migrate \
        || echo "Could not update ${HOMEWARD_CONFIG}; check it in the Homeward app."
}

service_preuninst ()
{
    # Upgrades also run the uninstall scripts; only a real uninstall removes var/.
    if [ "${SYNOPKG_PKG_STATUS}" = "UPGRADE" ]; then
        return
    fi
    # Copy config and state into the shared folder, so a reinstall can pick them up.
    # The synced files themselves live in shared folders and are never touched. The
    # iCloud sessions stay behind: anyone who can read the share could use them to
    # reach the Apple ID, so a reinstall asks to sign in again instead.
    if [ -n "${SHARE_PATH}" ] && [ -d "${SHARE_PATH}" ] && [ -f "${HOMEWARD_CONFIG}" ]; then
        dest="${SHARE_PATH}/.homeward-backup/$(date +%Y-%m-%d_%H%M%S)"
        echo "Saving configuration and state (without the iCloud sessions) to ${dest}"
        mkdir -p "${dest}/state" && cp -p "${HOMEWARD_CONFIG}" "${dest}/" || return
        for entry in "${SYNOPKG_PKGVAR}/state"/* "${SYNOPKG_PKGVAR}/state"/.[!.]*; do
            [ -e "${entry}" ] || continue
            case "$(basename "${entry}")" in
                sessions | sessions-pending) continue ;;
            esac
            cp -pR "${entry}" "${dest}/state/"
        done
    fi
}
