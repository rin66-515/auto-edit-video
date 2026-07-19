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

docker compose up -d --build
Write-Host "Vlog 审核台: http://127.0.0.1:4380"
