param (
    [string]$Target = "allert@fonie2.local"
)

Write-Host "Deploying Pi code to $Target..." -ForegroundColor Cyan
scp $PSScriptRoot\..\app.py "${Target}:~/rfid-player/app.py"
scp -r $PSScriptRoot\..\templates "${Target}:~/rfid-player/"
ssh $Target "cd ~/rfid-player && sudo systemctl restart fonie"
Write-Host "Pi deployed and restarted" -ForegroundColor Green
