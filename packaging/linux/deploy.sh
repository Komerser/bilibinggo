#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"
case "${1:-help}" in
  install) exec bash app/packaging/linux/install.sh ;;
  upgrade)
    [[ $EUID == 0 ]] || { echo 'Run upgrade with sudo' >&2; exit 1; }
    sha256sum --quiet -c FILES.sha256
    bash app/packaging/linux/install-updater.sh "${2:-8182}"
    exec /usr/bin/python3 -I /usr/local/libexec/binggo-updater/updater.py --bootstrap "$ROOT"
    ;;
  docker)
    command -v docker >/dev/null || { echo 'Install Docker Engine and Compose plugin first (see README).'; exit 1; }
    docker compose version >/dev/null
    [[ $EUID == 0 ]] || { echo 'Run: sudo bash deploy.sh docker' >&2; exit 1; }
    install -d -m 0700 -o 10001 -g 10001 user-data
    docker compose build
    docker compose up -d --wait --wait-timeout 90
    echo 'Binggo is responding at 127.0.0.1:8182.'
    ;;
  backup)
    [[ $EUID == 0 ]] || { echo 'Run backup with sudo'; exit 1; }
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    mkdir -p backups
    if systemctl is-active --quiet binggo.service; then
      systemctl stop binggo.service
      trap 'systemctl start binggo.service' EXIT
      /opt/binggo/venv/bin/python /opt/binggo/app/packaging/linux/backup.py /var/lib/binggo "$ROOT/backups/$stamp"
      systemctl start binggo.service
      trap - EXIT
    elif command -v docker >/dev/null && [[ -n $(docker compose ps --status running -q binggo 2>/dev/null) ]]; then
      docker compose stop binggo
      trap 'docker compose start binggo' EXIT
      docker compose run --rm --no-deps --user 0 --cap-add DAC_OVERRIDE --entrypoint python -v "$ROOT/backups:/backup" binggo /app/packaging/linux/backup.py /var/lib/binggo "/backup/$stamp"
      docker compose start binggo
      trap - EXIT
    else
      echo 'No running deployment found. Backup aborted.' >&2; exit 1
    fi
    tar -czf "backups/binggo-data-$stamp.tar.gz" -C backups "$stamp"
    echo "Backup complete: $ROOT/backups/binggo-data-$stamp.tar.gz"
    ;;
  *) echo 'Usage: sudo bash deploy.sh {install|upgrade [port]|docker|backup}' ;;
esac
