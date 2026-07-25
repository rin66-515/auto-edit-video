param(
    [Parameter(Mandatory = $true)][int]$ProjectId,
    [Parameter(Mandatory = $true)][int]$ScheduledExportId,
    [Parameter(Mandatory = $true)][int]$WaitForExportId,
    [int]$PollSeconds = 60,
    [string]$BaseUrl = 'http://127.0.0.1:4380'
)

Set-Location (Join-Path $PSScriptRoot '..')
$appRestarted = $false

function Write-WatcherLog([string]$Message) {
    Write-Output ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
}

while ($true) {
    try {
        $project = Invoke-RestMethod "$BaseUrl/api/projects/$ProjectId" -TimeoutSec 15
        $target = @($project.exports | Where-Object { [int]$_.id -eq $ScheduledExportId }) | Select-Object -First 1
        $waitFor = @($project.exports | Where-Object { [int]$_.id -eq $WaitForExportId }) | Select-Object -First 1
        if (-not $target) { throw "Scheduled export $ScheduledExportId no longer exists." }
        if (-not $waitFor) { throw "Preceding export $WaitForExportId no longer exists." }

        if ($target.status -in @('rendering', 'review_ready', 'approved')) {
            Write-WatcherLog "Scheduled export is already $($target.status); watcher finished."
            exit 0
        }

        if ($target.status -eq 'render_requested') {
            if ($project.control.desired_state -eq 'stopped') {
                Invoke-RestMethod "$BaseUrl/api/projects/$ProjectId/control/start" -Method Post -TimeoutSec 30 | Out-Null
                Write-WatcherLog "Scheduled export released and project started."
            }
            exit 0
        }

        if ($target.status -ne 'scheduled') { throw "Unexpected scheduled export state: $($target.status)" }
        $predecessorReady = $waitFor.status -in @('review_ready', 'approved')
        $projectStopped = $project.control.desired_state -in @('stopped', 'paused')
        if ($predecessorReady -and $projectStopped) {
            if (-not $appRestarted) {
                Write-WatcherLog 'Preceding export completed. Restarting app service to load the short-edit upgrade.'
                docker compose restart app | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'docker compose restart app failed.' }
                $healthy = $false
                for ($attempt = 0; $attempt -lt 60; $attempt++) {
                    Start-Sleep -Seconds 2
                    try {
                        $health = Invoke-RestMethod "$BaseUrl/api/health" -TimeoutSec 5
                        if ($health.ok) { $healthy = $true; break }
                    } catch {}
                }
                if (-not $healthy) { throw 'App service did not become healthy within 120 seconds.' }
                $appRestarted = $true
            }
            Invoke-RestMethod "$BaseUrl/api/exports/$ScheduledExportId/release-scheduled" -Method Post -TimeoutSec 30 | Out-Null
            Invoke-RestMethod "$BaseUrl/api/projects/$ProjectId/control/start" -Method Post -TimeoutSec 30 | Out-Null
            Write-WatcherLog "Export $ScheduledExportId released and started after export $WaitForExportId."
            exit 0
        }

        Write-WatcherLog "Waiting: predecessor=$($waitFor.status), project_control=$($project.control.desired_state)."
    } catch {
        Write-WatcherLog "Watcher retry after error: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds ([Math]::Max(15, $PollSeconds))
}
