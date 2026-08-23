[CmdletBinding()]
param(
    [string]$BaseUrl = '',
    [string]$PrinterName = '',
    [string]$WindowsAccount = ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
)

$ErrorActionPreference = 'Stop'
$installRoot = 'C:\ProgramData\Sklavounos\WarehouseHprtAgent'
$taskName = 'Sklavounos Warehouse HPRT Agent'
$currentAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name

try {
    if (-not $BaseUrl) { $BaseUrl = Read-Host 'Warehouse HTTPS address' }
    [Uri]$parsedBaseUrl = $null
    if (-not [Uri]::TryCreate($BaseUrl.Trim(), [UriKind]::Absolute, [ref]$parsedBaseUrl) -or $parsedBaseUrl.Scheme -ne 'https') {
        throw 'Η διεύθυνση Warehouse πρέπει να είναι έγκυρο HTTPS URL.'
    }
    if (-not $PrinterName) {
        $hprtPrinters = @(
            Get-Printer -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like '*HPRT*' -or $_.DriverName -like '*HPRT*' }
        )
        if ($hprtPrinters.Count -eq 1) { $PrinterName = $hprtPrinters[0].Name }
        else { $PrinterName = Read-Host 'Exact HPRT printer name from Windows' }
    }
    if ([string]::IsNullOrWhiteSpace($PrinterName)) { throw 'Δεν επιλέχθηκε εκτυπωτής HPRT.' }
    if ($WindowsAccount -cne $currentAccount) {
        throw 'Για ασφαλή αποθήκευση DPAPI, ο Windows λογαριασμός πρέπει να είναι ο τρέχων συνδεδεμένος χρήστης.'
    }
    $sourceFiles = @('WarehouseHprtAgent.ps1', 'HprtLpq80Print.ps1')
    foreach ($file in $sourceFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $file) -PathType Leaf)) { throw "Λείπει το αρχείο $file από το πακέτο." }
    }

    $tokenPath = Join-Path $installRoot 'agent-token.dpapi'
    $serializedToken = ''
    if (Test-Path -LiteralPath $tokenPath -PathType Leaf) {
        try {
            $serializedToken = (Get-Content -LiteralPath $tokenPath -Raw -Encoding UTF8).TrimStart([char]0xFEFF).Trim()
            $existingToken = ConvertTo-SecureString -String $serializedToken
            if ($existingToken.Length -lt 8) { throw 'Existing token is invalid.' }
            $existingToken.Dispose()
        }
        catch { $serializedToken = '' }
    }
    if (-not $serializedToken) {
        $token = Read-Host 'Warehouse WORKSHOP agent token (stored once with Windows DPAPI)' -AsSecureString
        if ($null -eq $token -or $token.Length -lt 8) { throw 'Το token δεν είναι έγκυρο.' }
        $serializedToken = $token | ConvertFrom-SecureString
    }

    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $existingTask) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }

    [IO.Directory]::CreateDirectory($installRoot) | Out-Null
    foreach ($file in $sourceFiles) {
        $destination = Join-Path $installRoot $file
        $copied = $false
        for ($attempt = 1; $attempt -le 10 -and -not $copied; $attempt++) {
            try {
                Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $destination -Force
                $copied = $true
            }
            catch {
                if ($attempt -eq 10) { throw }
                Start-Sleep -Milliseconds 500
            }
        }
    }

    $utf8WithoutBom = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($tokenPath, $serializedToken, $utf8WithoutBom)
    $config = [ordered]@{
        base_url = $parsedBaseUrl.AbsoluteUri.TrimEnd('/')
        station = 'WORKSHOP'
        printer_name = $PrinterName.Trim()
        poll_seconds = 5
        token_file = 'agent-token.dpapi'
    }
    $config | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $installRoot 'config.json') -Encoding UTF8 -Force

    $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $agent = Join-Path $installRoot 'WarehouseHprtAgent.ps1'
    $configPath = Join-Path $installRoot 'config.json'
    $arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -ConfigPath "{1}"' -f $agent, $configPath
    $action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments -WorkingDirectory $installRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $WindowsAccount
    $principal = New-ScheduledTaskPrincipal -UserId $WindowsAccount -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName

    Write-Host ''
    Write-Host 'Η εγκατάσταση ολοκληρώθηκε.' -ForegroundColor Green
    Write-Host "Printer: $PrinterName"
    Write-Host 'Agent: RUNNING — οι HPRT ετικέτες θα εκτυπώνονται αυτόματα από το Stock.'
    Write-Host 'Created by Christos Fragoulis'
}
catch {
    Write-Host ''
    Write-Host ('Η εγκατάσταση απέτυχε: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Δεν αποθηκεύτηκε εμφανές token. Διορθώστε το θέμα και ξανατρέξτε το SETUP.'
    Read-Host 'Πατήστε Enter για κλείσιμο' | Out-Null
    exit 1
}
