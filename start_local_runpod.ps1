$ErrorActionPreference = "Stop"

$configPath = Join-Path $PSScriptRoot "runpod_config.ps1"
if (Test-Path $configPath) {
    . $configPath
}

if (-not $env:RUNPOD_LIPSYNC_URL) {
    $env:RUNPOD_LIPSYNC_URL = "https://j7i5xg4b9sk4lk-8000.proxy.runpod.net"
}

function Test-RunPodReady {
    param([string] $BaseUrl)

    try {
        $response = Invoke-WebRequest -Uri "$BaseUrl/faces" -UseBasicParsing -TimeoutSec 10
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (-not (Test-RunPodReady -BaseUrl $env:RUNPOD_LIPSYNC_URL)) {
    if ($env:RUNPOD_API_KEY -and $env:RUNPOD_POD_ID) {
        Write-Host "RunPod is not ready. Sending start request for pod $($env:RUNPOD_POD_ID)..."
        Invoke-WebRequest `
            -Uri "https://rest.runpod.io/v1/pods/$($env:RUNPOD_POD_ID)/start" `
            -Method POST `
            -Headers @{ Authorization = "Bearer $($env:RUNPOD_API_KEY)" } `
            -UseBasicParsing | Out-Null

        Write-Host "Waiting for RunPod HTTP service..."
        for ($i = 1; $i -le 60; $i++) {
            if (Test-RunPodReady -BaseUrl $env:RUNPOD_LIPSYNC_URL) {
                Write-Host "RunPod is ready."
                break
            }
            Start-Sleep -Seconds 10
        }
    } else {
        Write-Warning "RunPod is not ready, and RUNPOD_API_KEY/RUNPOD_POD_ID are not configured."
    }
}

& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\app.py"
