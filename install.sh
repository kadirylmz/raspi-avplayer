#!/usr/bin/env bash
# AV Player - tek komutla kurulum (Raspberry Pi OS 64-bit Bookworm/Trixie, Pi 4 / Pi 5)
#   sudo ./install.sh [--data-dir /mnt/usb/avplayer] [--port 80] [--enable-4kp60] [--keep-desktop]
set -euo pipefail

DATA_DIR=/var/lib/avplayer
PORT=80
ENABLE_4KP60=0
KEEP_DESKTOP=0
STATIC_IP=""
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/avplayer

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-dir) DATA_DIR="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --enable-4kp60) ENABLE_4KP60=1; shift;;
    --keep-desktop) KEEP_DESKTOP=1; shift;;
    --static-ip) STATIC_IP="${2:-auto}"; shift 2;;   # auto | 192.168.1.210 | 192.168.1.210/24
    *) echo "Bilinmeyen parametre: $1"; exit 2;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "root olarak çalıştırın: sudo ./install.sh"; exit 1; }
log() { echo -e "\n\033[1;34m==> $*\033[0m"; }

# ---------------------------------------------------------------- donanım kontrolü
MODEL="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
log "Cihaz: $MODEL"
case "$MODEL" in
  *"Raspberry Pi 5"*) PI=pi5;;
  *"Raspberry Pi 4"*|*"Raspberry Pi 400"*|*"Compute Module 4"*) PI=pi4;;
  *) PI=other; echo "UYARI: Pi 4/5 dışı cihaz; yazılımsal profil kullanılacak.";;
esac
[[ "$(uname -m)" == "aarch64" ]] || echo "UYARI: 64-bit OS önerilir (uname -m = $(uname -m))"

# ---------------------------------------------------------------- paketler
log "Paketler kuruluyor (internet gerekir; çalışma sırasında gerekmez)"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip ffmpeg mpv alsa-utils v4l-utils rsync || true
apt-get install -y libraspberrypi-bin 2>/dev/null || true   # vcgencmd (opsiyonel)

# ---------------------------------------------------------------- uygulama
log "Uygulama $APP_DIR içine kopyalanıyor"
mkdir -p "$APP_DIR" "$DATA_DIR"
rsync -a --delete --exclude venv --exclude __pycache__ --exclude tests "$SRC_DIR/app" "$SRC_DIR/requirements.txt" "$APP_DIR/"
[[ -d "$APP_DIR/venv" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip -q
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

cat >/etc/avplayer.env <<EOF
AVP_DATA=$DATA_DIR
AVP_RUN=/run/avplayer
AVP_PORT=$PORT
EOF

# ---------------------------------------------------------------- sistem ayarları
BOOT=/boot/firmware; [[ -d $BOOT ]] || BOOT=/boot
if [[ -f $BOOT/cmdline.txt ]]; then
  log "Konsol ayarları (ekran kararmasın, imleç/logo gizli)"
  cp -n "$BOOT/cmdline.txt" "$BOOT/cmdline.txt.avplayer.bak"
  for opt in consoleblank=0 logo.nologo vt.global_cursor_default=0; do
    grep -q "$opt" "$BOOT/cmdline.txt" || sed -i "1 s/\$/ $opt/" "$BOOT/cmdline.txt"
  done
fi
if [[ $ENABLE_4KP60 -eq 1 && $PI == pi4 && -f $BOOT/config.txt ]]; then
  log "Pi 4: 4Kp60 HDMI etkinleştiriliyor (daha fazla ısınır, iyi soğutma gerekir)"
  grep -q '^hdmi_enable_4kp60=1' "$BOOT/config.txt" || printf '\n[pi4]\nhdmi_enable_4kp60=1\n[all]\n' >>"$BOOT/config.txt"
fi
if [[ $KEEP_DESKTOP -eq 0 ]]; then
  log "Masaüstü kapatılıyor (konsol modu): mpv ekrana doğrudan DRM/KMS ile çizer"
  systemctl set-default multi-user.target
  systemctl stop display-manager.service lightdm.service gdm.service wayfire.service 2>/dev/null || true
fi
# yeniden başlatma/servis kontrolü için kullanıcı müdahalesi gerekmez; servisler root olarak çalışır.

# ---------------------------------------------------------------- servisler
log "systemd servisleri kuruluyor"
install -m 644 "$SRC_DIR/systemd/avplayer-web.service" "$SRC_DIR/systemd/avplayer-player.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable avplayer-web.service avplayer-player.service
systemctl restart avplayer-web.service
sleep 3
systemctl restart avplayer-player.service

# ---------------------------------------------------------------- otomatik donanım testi
log "Otomatik donanım ve modül testi"
set -a; . /etc/avplayer.env; set +a
( cd "$APP_DIR" && "$APP_DIR/venv/bin/python" -m app.selftest ) || echo "UYARI: bazı testler başarısız - yukarıdaki listeye bakın"

if [[ -n "$STATIC_IP" ]]; then
  log "Sabit IP ayarlanıyor ($STATIC_IP)"
  if [[ "$STATIC_IP" == auto ]]; then bash "$SRC_DIR/scripts/set-static-ip.sh"; else bash "$SRC_DIR/scripts/set-static-ip.sh" "$STATIC_IP"; fi || echo "UYARI: sabit IP ayarlanamadı"
fi
IP="${STATIC_IP%/*}"; [[ -z "$IP" || "$IP" == auto ]] && IP="$(hostname -I | awk '{print $1}')"
KEY="$(cd "$APP_DIR" && "$APP_DIR/venv/bin/python" -c 'from app import db; print(db.get("api_key"))')"
log "Kurulum tamam!"
echo "  Panel:        http://${IP:-<cihaz-ip>}$([[ $PORT == 80 ]] || echo ":$PORT")"
echo "  API anahtarı: $KEY   (çoklu cihaz yönetiminde bu cihazı eklemek için)"
echo "  Loglar:       journalctl -u avplayer-player -f   |   $DATA_DIR/logs/"
echo "  Not: Konsol/DRM değişiklikleri için bir kez 'sudo reboot' önerilir."
