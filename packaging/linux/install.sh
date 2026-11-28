#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
if [[ $EUID != 0 ]]; then
  echo 'Run: sudo bash deploy.sh install' >&2
  exit 1
fi
if [[ ! -f /etc/os-release ]]; then echo 'Linux distribution not recognized' >&2; exit 1; fi
source /etc/os-release
if [[ ${ID:-} != ubuntu && ${ID_LIKE:-} != *debian* ]]; then
  echo 'This installer supports Ubuntu and Debian. Use Docker on other systems.' >&2
  exit 1
fi
if [[ -e /opt/binggo/app || -e /var/lib/binggo || -e /etc/systemd/system/binggo.service ]]; then
  echo 'Existing deployment found. Installation stopped to preserve its data.' >&2
  exit 1
fi
command -v systemctl >/dev/null || { echo 'systemd is required' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv ca-certificates tzdata
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required. On Ubuntu 22.04 use: sudo bash deploy.sh docker"'
getent group binggo >/dev/null || groupadd --system binggo
id binggo >/dev/null 2>&1 || useradd --system --gid binggo --home-dir /var/lib/binggo --shell /usr/sbin/nologin binggo
install -d -m 0755 /opt/binggo /opt/binggo/app
install -d -m 0700 -o binggo -g binggo /var/lib/binggo
install -d -m 0700 /etc/binggo
cp -a "$ROOT/app/." /opt/binggo/app/
python3 -m venv /opt/binggo/venv
/opt/binggo/venv/bin/python -m pip install --disable-pip-version-check -r /opt/binggo/app/packaging/linux/requirements.lock
chmod -R a+rX /opt/binggo
if [[ ! -e /etc/binggo/server.env ]]; then
  printf '%s\n' 'BINGGO_PORT=8182' 'TZ=Asia/Shanghai' > /etc/binggo/server.env
fi
install -m 0644 "$ROOT/app/packaging/linux/binggo.service" /etc/systemd/system/binggo.service
bash "$ROOT/app/packaging/linux/install-updater.sh" 8182
systemctl enable --now binggo.service
for attempt in {1..45}; do
  if /opt/binggo/venv/bin/python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8182/", timeout=2).close()' 2>/dev/null; then
    echo 'Binggo installed and responding at 127.0.0.1:8182; enabled at boot.'
    exit 0
  fi
  sleep 1
done
systemctl status binggo.service --no-pager || true
echo 'Startup check failed. Inspect: sudo journalctl -u binggo -n 100 --no-pager' >&2
exit 1
