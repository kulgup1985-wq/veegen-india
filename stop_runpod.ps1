$ErrorActionPreference = "Stop"

$configPath = Join-Path $PSScriptRoot "runpod_config.ps1"
if (Test-Path $configPath) {
    . $configPath
}

if (-not $env:RUNPOD_API_KEY -or -not $env:RUNPOD_POD_ID) {
    throw "Set RUNPOD_API_KEY and RUNPOD_POD_ID in runpod_config.ps1 first."
}

Invoke-WebRequest `
    -Uri "https://rest.runpod.io/v1/pods/$($env:RUNPOD_POD_ID)/stop" `
    -Method POST `
    -Headers @{ Authorization = "Bearer $($env:RUNPOD_API_KEY)" } `
    -UseBasicParsing | Out-Null

Write-Host "Stop request sent for RunPod pod $($env:RUNPOD_POD_ID)."
