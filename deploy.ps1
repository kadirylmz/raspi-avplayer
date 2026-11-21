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

Write-Host "`n[1/3] Dosyalar $target adresine kopyalanıyor (Pi şifresi sorulacak)..." -ForegroundColor Cyan
ssh $target "rm -rf ~/avplayer && mkdir -p ~/avplayer"
foreach ($item in "app", "systemd", "scripts", "install.sh", "uninstall.sh", "requirements.txt", "README.md") {
  scp -r "$here\$item" "${target}:~/avplayer/"
}

$flags = ""
if ($StaticIP)    { $flags += " --static-ip auto" }
if ($Enable4kp60) { $flags += " --enable-4kp60" }

Write-Host "`n[2/3] Pi üzerinde kurulum başlıyor (birkaç dakika sürer, sudo şifresi sorulabilir)..." -ForegroundColor Cyan
$remote = "cd ~/avplayer && sed -i 's/\r$//' install.sh uninstall.sh scripts/*.sh && chmod +x install.sh uninstall.sh scripts/*.sh && sudo ./install.sh$flags"
ssh -t $target $remote

Write-Host "`n[3/3] Bitti. Panel: http://$PiIP" -ForegroundColor Green
Write-Host "Konsol/DRM ayarları için Pi'yi bir kez yeniden başlatın:  ssh -t $target 'sudo reboot'" -ForegroundColor Yellow
Start-Process "http://$PiIP"
