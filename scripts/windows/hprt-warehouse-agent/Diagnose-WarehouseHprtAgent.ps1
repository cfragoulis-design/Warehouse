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
$configuredPrinter = ''
try {
    $configuredPrinter = [string]((Get-Content -LiteralPath (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json).printer_name)
}
catch { }
$printerFound = $configuredPrinter -and (Get-Printer -Name $configuredPrinter -ErrorAction SilentlyContinue)
Write-Host ('Configured printer: ' + $(if ($configuredPrinter) { $configuredPrinter } else { 'MISSING' }))
Write-Host ('Printer available: ' + $(if ($printerFound) { 'YES' } else { 'NO' }))
Write-Host ''
Write-Host 'Τελευταίες εγγραφές (χωρίς token ή περιεχόμενο ετικέτας):'
$log = Join-Path $root 'warehouse-hprt-agent.log'
if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Tail 12 } else { Write-Host '-' }
Write-Host ''
Write-Host 'Created by Christos Fragoulis'
