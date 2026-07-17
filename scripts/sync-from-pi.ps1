param (
    [string]$Target = "allert@fonie2.local"
)

$repoDir = Resolve-Path "$PSScriptRoot\.."

Write-Host "🔄 Syncing configuration files from Pi ($Target)..." -ForegroundColor Cyan

# Download files
try {
    scp "${Target}:~/rfid-player/rfid_mappings.json" "$repoDir\rfid_mappings.json"
    Write-Host "  ✔ Synced rfid_mappings.json" -ForegroundColor Green
} catch {
    Write-Host "  ⚠️ Could not sync rfid_mappings.json" -ForegroundColor Yellow
}

try {
    scp "${Target}:~/rfid-player/settings.json" "$repoDir\settings.json"
    Write-Host "  ✔ Synced settings.json" -ForegroundColor Green
} catch {
    Write-Host "  ⚠️ Could not sync settings.json" -ForegroundColor Yellow
}

try {
    scp "${Target}:~/rfid-player/.env" "$repoDir\.env"
    Write-Host "  ✔ Synced .env" -ForegroundColor Green
} catch {
    Write-Host "  ⚠️ Could not sync .env" -ForegroundColor Yellow
}

Write-Host "✅ Sync complete!" -ForegroundColor Green
