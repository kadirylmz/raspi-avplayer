#!/usr/bin/env bash
# Kaldırma: sudo ./uninstall.sh [--purge-data]
set -e
[[ $EUID -eq 0 ]] || { echo "root gerekli"; exit 1; }
systemctl disable --now avplayer-player.service avplayer-web.service 2>/dev/null || true
rm -f /etc/systemd/system/avplayer-*.service /etc/avplayer.env
systemctl daemon-reload
rm -rf /opt/avplayer
[[ "${1:-}" == "--purge-data" ]] && rm -rf /var/lib/avplayer && echo "Veriler silindi"
echo "Kaldırıldı. (Masaüstü için: sudo systemctl set-default graphical.target)"
