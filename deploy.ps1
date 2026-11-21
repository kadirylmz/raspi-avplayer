<#
 Windows'tan tek komutla Pi'ye kurulum.
 Kullanım (PowerShell):
   .\deploy.ps1 -PiIP 192.168.1.190
   .\deploy.ps1 -PiIP 192.168.1.190 -User pi -StaticIP      # kurulumda IP'yi de sabitler
   .\deploy.ps1 -PiIP 192.168.1.190 -Update                 # sadece kodu güncelle (yeniden kurar)
#>
param(
  [Parameter(Mandatory = $true)][string]$PiIP,
  [string]$User = "pi",
  [switch]$StaticIP,
  [switch]$Enable4kp60,
  [switch]$Update
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = "$User@$PiIP"

foreach ($c in "ssh", "scp") {
  if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { throw "$c bulunamadı. Ayarlar > Uygulamalar > Ek Özellikler > 'OpenSSH İstemcisi' kurun." }
}

Write-Host "`n[1/3] Dosyalar $target adresine paketlenip tek seferde kopyalanıyor..." -ForegroundColor Cyan
$tarFile = Join-Path $env:TEMP "avplayer_deploy.tar.gz"
if (Test-Path $tarFile) { Remove-Item $tarFile -Force }
Push-Location $here
tar -czf $tarFile app systemd scripts install.sh uninstall.sh requirements.txt README.md
Pop-Location
scp $tarFile "${target}:~/avplayer_deploy.tar.gz"
Remove-Item $tarFile -Force

$flags = ""
if ($StaticIP)    { $flags += " --static-ip auto" }
if ($Enable4kp60) { $flags += " --enable-4kp60" }

Write-Host "`n[2/3] Pi üzerinde kurulum/güncelleme uygulanıyor..." -ForegroundColor Cyan
if ($Update) {
  $remote = "sudo tar -xzf ~/avplayer_deploy.tar.gz -C /opt/avplayer && rm -f ~/avplayer_deploy.tar.gz && sudo rm -f /run/avplayer/standby_bg.png && sudo find /opt/avplayer/app -name '*.pyc' -delete && (/opt/avplayer/venv/bin/python -c 'import PIL' 2>/dev/null || sudo /opt/avplayer/venv/bin/pip install Pillow -q || sudo apt-get install -y python3-pil) && sudo install -m 644 /opt/avplayer/systemd/*.service /etc/systemd/system/ && BOOT=/boot/firmware; [ -d `$BOOT ] || BOOT=/boot; grep -q '^hdmi_force_hotplug=1' `$BOOT/config.txt 2>/dev/null || printf '\nhdmi_force_hotplug=1\ndisable_splash=1\n' | sudo tee -a `$BOOT/config.txt >/dev/null; for opt in fsck.mode=auto fsck.repair=yes quiet loglevel=0 systemd.show_status=false; do grep -q `$opt `$BOOT/cmdline.txt 2>/dev/null || sudo sed -i `"1 s/`$/ `$opt/`" `$BOOT/cmdline.txt; done; for svc in museum-agent wayvnc wayvnc-control vncserver-x11-serviced display-manager lightdm bluetooth hciuart cups cups-browsed ModemManager triggerhappy; do sudo systemctl disable --now `$svc 2>/dev/null || true; done; sudo systemctl mask museum-agent.service wayvnc.service wayvnc-control.service 2>/dev/null || true; sudo systemctl reset-failed 2>/dev/null || true; sudo clear >/dev/tty1 2>/dev/null || true; sudo iw dev wlan0 set power_save off 2>/dev/null || true; sudo systemctl daemon-reload && sudo systemctl enable avplayer-web.service avplayer-player.service && sudo systemctl restart avplayer-web.service avplayer-player.service && sync && sleep 2 && sudo journalctl -u avplayer-player.service -n 15 --no-pager"
} else {
  $remote = "rm -rf ~/avplayer && mkdir -p ~/avplayer && tar -xzf ~/avplayer_deploy.tar.gz -C ~/avplayer && rm -f ~/avplayer_deploy.tar.gz && cd ~/avplayer && sed -i 's/\r$//' install.sh uninstall.sh scripts/*.sh && chmod +x install.sh uninstall.sh scripts/*.sh && sudo ./install.sh$flags"
}
ssh -t $target $remote

Write-Host "`n[3/3] Bitti. Panel: http://$PiIP" -ForegroundColor Green
Write-Host "Konsol/DRM ayarları için Pi'yi bir kez yeniden başlatın:  ssh -t $target 'sudo reboot'" -ForegroundColor Yellow
Start-Process "http://$PiIP"
