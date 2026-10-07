# Raspberry Pi stand-in for SCV Water deployment testing
#
# Purpose: exercise the real deployment path end to end --
#   Flutter App -> deploy_service (/deploy) -> paramiko SSH -> bash script
# -- without owning real hardware.
#
# What is faithful to a real Pi:
#   - a `pi` user with /home/pi as home
#   - sshd reachable on a forwarded port, key-only auth
#   - systemd running (PID 1), so `systemctl enable` actually works
#   - i2c-tools / spi-tools, /dev/i2c-* stubs
#   - arm64 userland, so ARM-only wheels (RPi.GPIO, smbus2) install natively
#
# What is NOT faithful (and cannot be):
#   - the 40-pin header. There is no GPIO controller, so a real YF-S201 or
#     BME280 will not be detected. The deployment script completes; the driver
#     will report "no device found" at runtime. That is the honest limit of
#     this substitute, and it is why wiring correctness still needs real HW.
#
# The deploy script runs `sudo apt-get` and writes /etc/systemd/system. Inside the
# container `pi` is root, so sudo is a no-op passthrough.

FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        systemd systemd-sysv dbus python3 python3-venv python3-pip \
        openssh-server sudo ca-certificates curl procps \
        i2c-tools spi-tools \
    && rm -rf /var/lib/apt/lists/*

# The `pi` account a real Raspberry Pi OS ships with.
RUN useradd -m -s /bin/bash pi \
    && usermod -aG sudo,dialout,i2c,spi,video pi \
    && mkdir -p /home/pi/scv_water_sensors \
    && chown -R pi:pi /home/pi \
    && echo 'pi ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/90-pi \
    && chmod 0440 /etc/sudoers.d/90-pi

# Key-only auth, mirroring how the deploy service connects (no password ever
# crosses the App).
RUN mkdir -p /run/sshd /root/.ssh /home/pi/.ssh \
    && chmod 700 /root/.ssh /home/pi/.ssh \
    && ssh-keygen -A \
    && printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\n\
KbdInteractiveAuthentication no\nUsePAM no\n\
X11Forwarding no\nPrintMotd no\nAcceptEnv LANG LC_*\n' \
        > /etc/ssh/sshd_config.d/10-pi.conf \
    && sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config \
    && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config

COPY entrypoint.sh /usr/local/sbin/pi-entrypoint
RUN chmod +x /usr/local/sbin/pi-entrypoint

VOLUME ["/sys", "/dev"]
EXPOSE 22

ENTRYPOINT ["/usr/local/sbin/pi-entrypoint"]
CMD ["/sbin/init"]
