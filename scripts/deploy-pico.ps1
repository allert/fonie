param (
    [string]$Target = "allert@fonie2.local"
)

Write-Host "Compiling Pico Firmware..." -ForegroundColor Cyan
Set-Location -Path "$PSScriptRoot\..\firmware\pico"
pio run -e pico
if ($LASTEXITCODE -ne 0) {
    Write-Host "Compilation failed." -ForegroundColor Red
    exit 1
}

$firmwareBin = "$PSScriptRoot\..\firmware\pico\.pio\build\pico\firmware.bin"
if (-Not (Test-Path $firmwareBin)) {
    Write-Host "firmware.bin not found!" -ForegroundColor Red
    exit 1
}

Write-Host "Uploading firmware.bin and python flasher to Pi..." -ForegroundColor Cyan
scp $firmwareBin "${Target}:~/rfid-player/firmware.bin"
scp "$PSScriptRoot\pico_uart_flash.py" "${Target}:~/rfid-player/"

Write-Host "Flashing Pico over UART..." -ForegroundColor Cyan
ssh $Target 'cd ~/rfid-player && sudo apt install -y python3-serial && python3 pico_uart_flash.py firmware.bin'

Write-Host "Pico deployed and restarted" -ForegroundColor Green
