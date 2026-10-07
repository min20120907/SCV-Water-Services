#!/usr/bin/env bash
# Entrypoint for the Raspberry Pi stand-in container.
#
# Installs the caller's public SSH key for the `pi` user, then hands off to
# systemd so `systemctl enable` inside the deployment script behaves normally.
set -euo pipefail

install_key() {
    local key="${1:-}"
    local user="${2:-pi}"
    local home="/home/${user}"

    if [[ -z "${key}" ]]; then
        echo "[pi-sim] no SSH_PUBKEY provided; generate one or the deploy service cannot connect" >&2
        return 0
    fi

    install -d -m 700 -o "${user}" -g "${user}" "${home}/.ssh"
    printf '%s\n' "${key}" > "${home}/.ssh/authorized_keys"
    chown "${user}:${user}" "${home}/.ssh/authorized_keys"
    chmod 600 "${home}/.ssh/authorized_keys"
    echo "[pi-sim] installed SSH key for ${user}"
}

install_key "${SSH_PUBKEY:-}" pi
install_root_key() {
    local key="${1:-}"
    [[ -z "${key}" ]] && return 0
    printf '%s\n' "${key}" > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    echo "[pi-sim] installed SSH key for root"
}
install_root_key "${SSH_PUBKEY_ROOT:-}"

# I2C/SPI bus stubs so i2cdetect / spidev tools find something plausible.
# The real bus controllers are absent -- these are only so the deployment
# script's tool installs and invocations do not error out.
if [[ ! -e /dev/i2c-1 ]]; then
    mknod /dev/i2c-1 c 89 1 2>/dev/null || true
fi
if [[ ! -e /dev/spidev0.0 ]]; then
    mknod /dev/spidev0.0 c 153 0 2>/dev/null || true
fi

# Keep systemd happy inside a container.
mkdir -p /run/systemd/system
# Container stdout is not a tty; systemd would otherwise refuse to start units.
export SYSTEMD_IGNORE_CHROOT=1
export container=docker

echo "[pi-sim] handing off to systemd: $*"
exec "$@"
