Set-Location $PSScriptRoot\..
docker compose ps
$health = Invoke-RestMethod http://127.0.0.1:4380/api/health
$health
$health.workers | Format-Table worker,stage,online,last_seen -AutoSize
