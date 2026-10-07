#!/usr/bin/env bash
# Start (or restart) the Raspberry Pi stand-in container used to exercise the
# real SSH deployment path.
#
#   ./run.sh up      build + start, print the connection details
#   ./run.sh down    stop and remove
#   ./run.sh logs    tail the container log
#   ./run.sh test    run the deploy service against it end to end
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="${PI_SIM_NAME:-scv-pi-sim}"
PORT="${PI_SIM_SSH_PORT:-2222}"
IMAGE="${PI_SIM_IMAGE:-scv-pi-sim:latest}"
KEY="${PI_SIM_KEY:-$HOME/.ssh/id_rsa}"
export SSH_PUBKEY="${SSH_PUBKEY:-$(cat "${KEY}.pub" 2>/dev/null || true)}"

if [[ -z "${SSH_PUBKEY}" ]]; then
    echo "error: no public key at ${KEY}.pub -- set PI_SIM_KEY or create one." >&2
    exit 1
fi

up() {
    echo "==> building ${IMAGE}"
    docker build -t "${IMAGE}" "${HERE}"

    docker rm -f "${NAME}" >/dev/null 2>&1 || true

    echo "==> starting ${NAME} (ssh on 127.0.0.1:${PORT})"
    docker run -d --name "${NAME}" \
        --privileged \
        -p "127.0.0.1:${PORT}:22" \
        -e SSH_PUBKEY="${SSH_PUBKEY}" \
        --tmpfs /run \
        --tmpfs /run/lock \
        -v /sys:/sys:ro \
        "${IMAGE}" /sbin/init >/dev/null

    echo "==> waiting for sshd"
    for _ in $(seq 1 60); do
        if docker exec "${NAME}" systemctl is-active --quiet ssh; then
            echo "==> sshd is up"
            break
        fi
        sleep 2
    done

    cat <<EOF

  container : ${NAME}
  ssh       : pi@127.0.0.1 -p ${PORT}
  key       : ${KEY}

  In the App, enter the host as 127.0.0.1 and port ${PORT}.
  (DeployService currently hard-codes port 22; see down() notes in README.)
EOF
}

down() {
    docker rm -f "${NAME}" >/dev/null 2>&1 || true
    echo "==> removed ${NAME}"
}

case "${1:-up}" in
    up) up ;;
    down) down ;;
    logs) docker logs -f "${NAME}" ;;
    *)
        echo "usage: $0 {up|down|logs}" >&2
        exit 2
        ;;
esac
