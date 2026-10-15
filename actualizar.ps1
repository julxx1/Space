$ErrorActionPreference = 'Stop'
try {
  $base = $PSScriptRoot
  $tmp = Join-Path $env:TEMP 'micuarto_upd'
  if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
  New-Item -ItemType Directory -Path $tmp | Out-Null
  $zip = Join-Path $tmp 'u.zip'
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -Uri 'https://github.com/julxx1/Space/archive/refs/heads/main.zip' -OutFile $zip -UseBasicParsing -TimeoutSec 25
  Expand-Archive -Path $zip -DestinationPath $tmp -Force
  $src = Join-Path $tmp 'Space-main'
  foreach ($f in 'server.py','cutout.py','prices.py','index.html','LEEME.txt') {
    $s = Join-Path $src $f
    if (Test-Path $s) { Copy-Item $s (Join-Path $base $f) -Force }
  }
  Write-Host 'Idearium esta al dia.'
} catch {
  Write-Host 'No pude buscar actualizaciones (sin internet?). Se abre la version que ya tienes.'
}
