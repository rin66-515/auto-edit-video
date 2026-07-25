Set-Location $PSScriptRoot\..
$dockerReady = $false
docker info *> $null
if ($LASTEXITCODE -eq 0) {
    $dockerReady = $true
}

if (-not $dockerReady) {
    $dockerDesktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path -LiteralPath $dockerDesktop)) {
        throw "未找到 Docker Desktop：$dockerDesktop"
    }
    Write-Host "正在启动 Docker Desktop，请稍候……"
    Start-Process -FilePath $dockerDesktop
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Seconds 2
        docker info *> $null
        if ($LASTEXITCODE -eq 0) {
            $dockerReady = $true
            break
        }
    }
}

if (-not $dockerReady) {
    throw 'Docker Desktop 在120秒内未准备完成，请打开 Docker Desktop 后重试。'
}

docker compose up -d app audio asr visual
if ($LASTEXITCODE -ne 0) {
    throw 'Vlog 容器启动失败，请运行 scripts\status.ps1 查看状态。'
}

$servicesReady = $false
for ($attempt = 0; $attempt -lt 90; $attempt++) {
    Start-Sleep -Seconds 2
    try {
        $health = Invoke-RestMethod http://127.0.0.1:4380/api/health -TimeoutSec 5
        $offline = @($health.workers | Where-Object { -not $_.online })
        if ($health.ok -and $offline.Count -eq 0) {
            $servicesReady = $true
            break
        }
    } catch {
        # 审核页或工作器仍在启动，继续等待。
    }
}

if (-not $servicesReady) {
    docker compose ps
    throw 'Vlog 工作器未在180秒内全部上线，请运行 scripts\status.ps1 查看状态。'
}

Write-Host "Vlog全部工作器已上线。"
Write-Host "Vlog 审核台: http://127.0.0.1:4380"
