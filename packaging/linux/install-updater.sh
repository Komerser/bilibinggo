#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
APP=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
PORT=${1:-8182}
[[ $PORT =~ ^[0-9]{4,5}$ ]] && (( PORT >= 1024 && PORT <= 65535 )) || { echo 'Invalid dashboard port' >&2; exit 1; }
install -d -m 0750 -o root -g binggo /etc/binggo-updater /var/lib/binggo-updater
install -d -m 2770 -o root -g binggo /var/lib/binggo-updater/inbox
install -d -m 2750 -o root -g binggo /var/lib/binggo-updater/state /var/lib/binggo-updater/state/history
install -d -m 0700 -o root -g root /var/lib/binggo-updater/work
install -d -m 0755 /usr/local/libexec/binggo-updater /etc/systemd/system/binggo.service.d
for token in admin-token worker-token; do
  if [[ ! -f /etc/binggo-updater/$token ]]; then
    python3 -c 'import secrets; print(secrets.token_hex(32))' > "/etc/binggo-updater/$token"
  fi
  chown root:binggo "/etc/binggo-updater/$token"
  chmod 0640 "/etc/binggo-updater/$token"
done
printf '%s\n' "$PORT" > /etc/binggo-updater/port
install -m 0644 "$APP/src/release_channel.py" "$APP/src/update_state.py" /usr/local/libexec/binggo-updater/
install -m 0644 "$APP/packaging/linux/updater.py" /usr/local/libexec/binggo-updater/
install -m 0644 "$APP/packaging/linux/binggo-updater.service" "$APP/packaging/linux/binggo-updater.path" /etc/systemd/system/
cat > /etc/systemd/system/binggo.service.d/updater.conf <<'EOF'
[Service]
Environment=BINGGO_SYSTEMD_UPDATE=1
ReadWritePaths=/var/lib/binggo-updater/inbox
EOF
chmod 0644 /etc/systemd/system/binggo.service.d/updater.conf
systemctl daemon-reload
systemctl enable --now binggo-updater.path
