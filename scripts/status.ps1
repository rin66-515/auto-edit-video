Set-Location $PSScriptRoot\..
docker compose ps
Invoke-RestMethod http://127.0.0.1:4380/api/health

