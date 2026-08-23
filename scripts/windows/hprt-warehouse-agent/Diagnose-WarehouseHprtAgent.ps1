$ErrorActionPreference = 'Stop'
$root = 'C:\ProgramData\Sklavounos\WarehouseHprtAgent'
$taskName = 'Sklavounos Warehouse HPRT Agent'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$info = if ($task) { Get-ScheduledTaskInfo -TaskName $taskName } else { $null }

Write-Host 'SKLAVOUNOS WAREHOUSE HPRT AGENT' -ForegroundColor Cyan
Write-Host ('Task: ' + $(if ($task) { $task.State } else { 'NOT INSTALLED' }))
Write-Host ('Last result: ' + $(if ($info) { $info.LastTaskResult } else { '-' }))
Write-Host ('Config: ' + $(if (Test-Path -LiteralPath (Join-Path $root 'config.json')) { 'OK' } else { 'MISSING' }))
Write-Host ('DPAPI token: ' + $(if (Test-Path -LiteralPath (Join-Path $root 'agent-token.dpapi')) { 'OK' } else { 'MISSING' }))
Write-Host ('HPRT printer configured: ' + $(if (Get-Printer | Where-Object Name -Like '*HPRT*') { 'YES' } else { 'NO HPRT NAME FOUND' }))
Write-Host ''
Write-Host 'Τελευταίες εγγραφές (χωρίς token ή περιεχόμενο ετικέτας):'
$log = Join-Path $root 'warehouse-hprt-agent.log'
if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Tail 12 } else { Write-Host '-' }
Write-Host ''
Write-Host 'Created by Christos Fragoulis'
