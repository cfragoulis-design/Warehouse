# RAWLOGIC Transfer Agent installer. Interactive Windows user, DPAPI token,
# scheduled task. Does not touch the HPRT print agent.
[CmdletBinding()]
param(
    [string]$BaseUrl = '',
    [string]$SqlServer = '',
    [string]$SqlDatabase = '',
    [string]$InboxPath = 'C:\RAWLOGIC\109-INBOX',
    [string]$WindowsAccount = ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
)

$ErrorActionPreference = 'Stop'
$installRoot = 'C:\ProgramData\RawLogic\TransferAgent'
$taskName = 'RAWLOGIC Transfer Agent'
$shortcutName = 'Ενδοδιακινήσεις 109.lnk'
$currentAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name

try {
    if (-not $BaseUrl) { $BaseUrl = Read-Host 'Warehouse HTTPS address' }
    [Uri]$parsedBaseUrl = $null
    if (-not [Uri]::TryCreate($BaseUrl.Trim(), [UriKind]::Absolute, [ref]$parsedBaseUrl) -or $parsedBaseUrl.Scheme -ne 'https') {
        throw 'Η διεύθυνση πρέπει να είναι έγκυρο HTTPS URL.'
    }
    if (-not $SqlServer) { $SqlServer = Read-Host 'Galaxy SQL Server name (Windows auth, read-only)' }
    if (-not $SqlDatabase) { $SqlDatabase = Read-Host 'Galaxy database name' }
    if ([string]::IsNullOrWhiteSpace($SqlServer) -or $SqlServer -match '[;''"\r\n]' -or [string]::IsNullOrWhiteSpace($SqlDatabase) -or $SqlDatabase -match '[;''"\r\n]') {
        throw 'Ο SQL Server ή η βάση δεν είναι έγκυρα.'
    }
    if ([string]::IsNullOrWhiteSpace($InboxPath)) { throw 'Ο φάκελος εισερχομένων είναι κενός.' }
    if ($WindowsAccount -cne $currentAccount) {
        throw 'Για ασφαλή αποθήκευση DPAPI, ο Windows λογαριασμός πρέπει να είναι ο τρέχων συνδεδεμένος χρήστης.'
    }

    $sourceFiles = @(
        'TransferAgent.ps1',
        'TransferAgent.Status.ps1',
        'PACKAGE-MANIFEST.json'
    )
    foreach ($file in $sourceFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $file) -PathType Leaf)) { throw "Λείπει το αρχείο $file από το πακέτο." }
    }

    $normalizedBaseUrl = $parsedBaseUrl.AbsoluteUri.TrimEnd('/')
    $tokenPath = Join-Path $installRoot 'agent-token.dpapi'
    $tokenOriginPath = Join-Path $installRoot 'agent-token-origin.txt'
    $serializedToken = ''
    $tokenOrigin = ''
    if (Test-Path -LiteralPath $tokenOriginPath -PathType Leaf) {
        $tokenOrigin = (Get-Content -LiteralPath $tokenOriginPath -Raw -Encoding UTF8).TrimStart([char]0xFEFF).TrimEnd('/')
    }
    if ($tokenOrigin -ceq $normalizedBaseUrl -and (Test-Path -LiteralPath $tokenPath -PathType Leaf)) {
        try {
            $serializedToken = (Get-Content -LiteralPath $tokenPath -Raw -Encoding UTF8).TrimStart([char]0xFEFF).Trim()
            $existingToken = ConvertTo-SecureString -String $serializedToken
            if ($existingToken.Length -lt 16) { throw 'Existing token is invalid.' }
            $existingToken.Dispose()
        }
        catch { $serializedToken = '' }
    }
    if (-not $serializedToken) {
        $token = Read-Host 'Transfer agent token (stored once with Windows DPAPI)' -AsSecureString
        if ($null -eq $token -or $token.Length -lt 16) { throw 'Το token δεν είναι έγκυρο.' }
        $serializedToken = $token | ConvertFrom-SecureString
    }

    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $existingTask) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }

    [IO.Directory]::CreateDirectory($installRoot) | Out-Null
    [IO.Directory]::CreateDirectory($InboxPath) | Out-Null
    foreach ($file in $sourceFiles) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $installRoot $file) -Force
    }

    $utf8WithoutBom = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($tokenPath, $serializedToken, $utf8WithoutBom)
    [IO.File]::WriteAllText($tokenOriginPath, $normalizedBaseUrl, $utf8WithoutBom)
    $config = [ordered]@{
        base_url = $normalizedBaseUrl
        poll_seconds = 10
        items_every_seconds = 900
        inbox_path = $InboxPath
        delivery_mode = 'file'
        sql_server = $SqlServer.Trim()
        sql_database = $SqlDatabase.Trim()
        token_file = 'agent-token.dpapi'
        desktop_notify = $true
    }
    $configJson = $config | ConvertTo-Json
    [IO.File]::WriteAllText((Join-Path $installRoot 'config.json'), $configJson, $utf8WithoutBom)
    [IO.File]::WriteAllText((Join-Path $installRoot 'installed-version.txt'), '1.0.0', $utf8WithoutBom)

    $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $agent = Join-Path $installRoot 'TransferAgent.ps1'
    $configPath = Join-Path $installRoot 'config.json'
    $arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -ConfigPath "{1}"' -f $agent, $configPath
    $action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments -WorkingDirectory $installRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $WindowsAccount
    $principal = New-ScheduledTaskPrincipal -UserId $WindowsAccount -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName

    $desktopPath = [Environment]::GetFolderPath('Desktop')
    $shortcutPath = Join-Path $desktopPath $shortcutName
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $InboxPath
    $shortcut.WorkingDirectory = $InboxPath
    $shortcut.Description = 'Ενδοδιακινήσεις 109'
    $shortcut.Save()

    Write-Host ''
    Write-Host 'Η εγκατάσταση ολοκληρώθηκε.' -ForegroundColor Green
    Write-Host "Install: $installRoot"
    Write-Host "Inbox: $InboxPath"
    Write-Host 'Task: RAWLOGIC Transfer Agent (Interactive, Limited)'
    Write-Host 'Ο agent των ετικετών δεν άλλαξε.'
    Write-Host 'Created by Christos Fragoulis'
}
catch {
    Write-Host ''
    Write-Host ('Η εγκατάσταση απέτυχε: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Δεν αποθηκεύτηκε εμφανές token. Διορθώστε το θέμα και ξανατρέξτε το SETUP.'
    Read-Host 'Πατήστε Enter για κλείσιμο' | Out-Null
    exit 1
}
