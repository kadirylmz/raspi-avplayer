#!/usr/bin/env bash
# Pi'ye sabit IP verir (NetworkManager = Bookworm/Trixie, yoksa dhcpcd).
#   sudo ./scripts/set-static-ip.sh                      -> şu anki IP'yi sabitler (otomatik)
#   sudo ./scripts/set-static-ip.sh 192.168.1.210        -> belirtilen IP (prefix/gateway otomatik)
#   sudo ./scripts/set-static-ip.sh 192.168.1.210/24 192.168.1.1 [dns]
#   sudo ./scripts/set-static-ip.sh --dhcp               -> tekrar otomatik IP (DHCP)
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "root gerekli: sudo $0"; exit 1; }

DEV="$(ip route show default | awk '{print $5; exit}')"
[[ -n "$DEV" ]] || { echo "Aktif ağ arayüzü/varsayılan ağ geçidi bulunamadı. Önce ağa bağlanın."; exit 1; }
CUR="$(ip -4 -o addr show dev "$DEV" scope global | awk '{print $4; exit}')"
GW_CUR="$(ip route show default dev "$DEV" | awk '{print $3; exit}')"

MODE=static
if [[ "${1:-}" == "--dhcp" ]]; then MODE=dhcp; fi
REQ="${1:-${CUR}}"
GW="${2:-$GW_CUR}"
DNS="${3:-$GW}"
[[ "$REQ" == */* ]] || REQ="$REQ/${CUR#*/}"       # prefix verilmediyse mevcut olanı kullan
IP="${REQ%/*}"

if [[ $MODE == static && "$IP" != "${CUR%/*}" ]]; then
  if ping -c1 -W1 "$IP" >/dev/null 2>&1; then
    echo "HATA: $IP ağda başka bir cihaz tarafından kullanılıyor. Başka IP seçin."; exit 1
  fi
fi

if command -v nmcli >/dev/null && systemctl is-active --quiet NetworkManager; then
  CON="$(nmcli -g GENERAL.CONNECTION device show "$DEV")"
  [[ -n "$CON" ]] || { echo "NetworkManager bağlantısı bulunamadı"; exit 1; }
  if [[ $MODE == dhcp ]]; then
    nmcli con mod "$CON" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns ""
    echo "'$CON' → DHCP"
  else
    nmcli con mod "$CON" ipv4.method manual ipv4.addresses "$REQ" ipv4.gateway "$GW" ipv4.dns "$DNS" ipv4.ignore-auto-dns yes
    echo "'$CON' ($DEV) → sabit IP $REQ  ağ geçidi $GW"
  fi
  # SSH oturumu kopacağı için uygulama arka planda ve gecikmeli yapılır
  nohup bash -c "sleep 3; nmcli con up '$CON'" >/dev/null 2>&1 &
elif [[ -f /etc/dhcpcd.conf ]]; then
  sed -i '/# avplayer-static-begin/,/# avplayer-static-end/d' /etc/dhcpcd.conf
  if [[ $MODE == static ]]; then
    cat >>/etc/dhcpcd.conf <<EOF
# avplayer-static-begin
interface $DEV
static ip_address=$REQ
static routers=$GW
static domain_name_servers=$DNS
# avplayer-static-end
EOF
  fi
  nohup bash -c "sleep 3; systemctl restart dhcpcd" >/dev/null 2>&1 &
  echo "dhcpcd yapılandırıldı: $MODE $REQ"
else
  echo "Desteklenen ağ yöneticisi (NetworkManager/dhcpcd) bulunamadı"; exit 1
fi

[[ $MODE == static ]] && echo "Birkaç saniye içinde yeni adres: http://$IP  (SSH bağlantısı kopabilir, yeni IP ile bağlanın)"
