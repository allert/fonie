param (
    [string]$Target = "fonie-esp32.local"
)

Write-Host "Compiling ESP32 Firmware..." -ForegroundColor Cyan
Set-Location -Path "$PSScriptRoot\..\firmware\esp32"
pio run -e esp32c3_ota
if ($LASTEXITCODE -ne 0) {
    Write-Host "Compilation failed." -ForegroundColor Red
    exit 1
}

Write-Host "Flashing ESP32 over Wi-Fi (ArduinoOTA)..." -ForegroundColor Cyan
pio run -e esp32c3_ota -t upload --upload-port $Target
if ($LASTEXITCODE -ne 0) {
    Write-Host "Flashing failed." -ForegroundColor Red
    exit 1
}

Write-Host "ESP32 deployed successfully!" -ForegroundColor Green
