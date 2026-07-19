Set-Location $PSScriptRoot\..
docker compose up -d --build
Write-Host "Vlog 审核台: http://127.0.0.1:4380"

