# AI Trader v2 - Windows boot WSL autostart setup
# Run as Administrator in PowerShell

$TaskName = "AI-Trader-WSL-Autostart"
$Description = "Start WSL Ubuntu and AI Trader bot on Windows boot"

# Remove existing task
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[OK] Removed existing task" -ForegroundColor Yellow
}

# Trigger: on system startup
$Trigger = New-ScheduledTaskTrigger -AtStartup

# Action: start WSL and ensure ai-trader service is running
$Action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d Ubuntu -- /bin/bash -c `"sleep 5 && systemctl is-active ai-trader.service || sudo systemctl start ai-trader.service`""

# Settings
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

# Run as SYSTEM (no login required)
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

# Register
Register-ScheduledTask -TaskName $TaskName -Description $Description -Trigger $Trigger -Action $Action -Settings $Settings -Principal $Principal

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " AI Trader WSL Autostart registered!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Task: $TaskName"
Write-Host "  Trigger: Windows startup"
Write-Host "  Action: WSL Ubuntu -> ai-trader.service"
Write-Host ""
Write-Host "  Check:  Get-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "  Remove: Unregister-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host ""
