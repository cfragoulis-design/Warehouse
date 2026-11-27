# RAWLOGIC Transfer Agent 1.0.0
# Outbound-only poller. Reads one fixed SELECT from Galaxy with the current
# Windows account, pushes the item list when it changed, and writes 109 CSV
# files into the inbox. It never writes to Galaxy.
[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'config.json'),
    [switch]$DryRun,
    [switch]$RunOnce,
    [string]$SqlServer = '',
    [string]$SqlDatabase = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$script:AgentVersion = '1.0.0'

# RAWLOGIC_ITEM_QUERY_BEGIN
$script:ItemQuery = @'
SELECT
    item.GXCODE AS code,
    item.GXDESCRIPTION AS item_name,
    item.GXACTIVE AS is_active,
    CONVERT(nvarchar(64), item.GXITEMID) AS galaxy_item_id,
    COALESCE(CONVERT(nvarchar(40), unit.GXCODE), N'') AS unit_code,
    COALESCE(CONVERT(nvarchar(80), unit.GXDESCRIPTION), N'') AS unit_name
FROM GXITEMCOMPANYPROP AS item
LEFT JOIN GXMEASUREMENTUNIT AS unit
    ON unit.GXID = item.GXAMEUNID
'@
# RAWLOGIC_ITEM_QUERY_END

function Write-AgentLog {
    param([Parameter(Mandatory)][string]$Message)
    if ($DryRun) { return }
    $logPath = Join-Path $PSScriptRoot 'transfer-agent.log'
    try {
        if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 1048576) {
            Move-Item -LiteralPath $logPath -Destination ($logPath + '.previous') -Force
        }
        Add-Content -LiteralPath $logPath -Encoding UTF8 -Value ('{0:o} {1}' -f [DateTimeOffset]::Now, $Message)
    }
    catch { }
}

function Write-AgentState {
    param(
        [Parameter(Mandatory)][string]$State,
        [string]$LastError = '',
        [string]$LastFile = '',
        [int]$ItemCount = -1
    )
    if ($DryRun) { return }
    try {
        $path = Join-Path $PSScriptRoot 'agent-status.json'
        $existing = $null
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            try { $existing = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json }
            catch { $existing = $null }
        }
        $now = [DateTimeOffset]::Now.ToString('o')
        $status = [ordered]@{
            schema_version = 1
            product = 'RAWLOGIC Transfer Agent'
            version = $script:AgentVersion
            state = $State
            last_contact = $now
            last_file = $(if ($LastFile) { $LastFile } elseif ($existing) { [string]$existing.last_file } else { '' })
            last_error = $LastError
            item_count = $(if ($ItemCount -ge 0) { $ItemCount } elseif ($existing) { $existing.item_count } else { $null })
            delivery_mode = 'file'
            updated_at = $now
        }
        $json = $status | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($path, $json, (New-Object Text.UTF8Encoding($false)))
    }
    catch { }
}

function Test-SafeSqlTarget {
    param([Parameter(Mandatory)][string]$Value, [Parameter(Mandatory)][string]$Label)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Length -gt 128 -or $Value -match '[;''"\r\n]') {
        throw "$Label is invalid."
    }
}

function Read-ExactConfig {
    if ($DryRun -and -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        Test-SafeSqlTarget -Value $SqlServer -Label 'SQL server'
        Test-SafeSqlTarget -Value $SqlDatabase -Label 'SQL database'
        return [pscustomobject]@{
            BaseUrl = ''
            PollSeconds = 10
            ItemsEverySeconds = 900
            InboxPath = ''
            DeliveryMode = 'file'
            SqlServer = $SqlServer.Trim()
            SqlDatabase = $SqlDatabase.Trim()
            TokenPath = ''
            DesktopNotify = $false
        }
    }
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw 'Agent configuration is missing.' }
    try { $value = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'Agent configuration is invalid.' }
    foreach ($name in @('base_url', 'poll_seconds', 'items_every_seconds', 'inbox_path', 'delivery_mode', 'sql_server', 'sql_database', 'token_file')) {
        if ($value.PSObject.Properties.Name -notcontains $name) { throw 'Agent configuration is incomplete.' }
    }
    $server = if ($SqlServer) { $SqlServer.Trim() } else { ([string]$value.sql_server).Trim() }
    $database = if ($SqlDatabase) { $SqlDatabase.Trim() } else { ([string]$value.sql_database).Trim() }
    Test-SafeSqlTarget -Value $server -Label 'SQL server'
    Test-SafeSqlTarget -Value $database -Label 'SQL database'
    $mode = ([string]$value.delivery_mode).Trim().ToLowerInvariant()
    if ($mode -notin @('file', 'api')) { throw 'Agent delivery mode is invalid.' }
    $poll = [int]$value.poll_seconds
    if ($poll -lt 5 -or $poll -gt 60) { throw 'Agent polling interval is invalid.' }
    $itemsEvery = [int]$value.items_every_seconds
    if ($itemsEvery -lt 60 -or $itemsEvery -gt 86400) { throw 'Agent item interval is invalid.' }
    $notify = $true
    if ($value.PSObject.Properties.Name -contains 'desktop_notify') { $notify = [bool]$value.desktop_notify }
    $tokenPath = ''
    $baseUrl = ''
    $inbox = ''
    if (-not $DryRun) {
        $base = [Uri]([string]$value.base_url).TrimEnd('/')
        if ($base.Scheme -ne 'https' -or -not $base.Host) { throw 'Agent base URL must use HTTPS.' }
        $baseUrl = $base.AbsoluteUri.TrimEnd('/')
        $inbox = [IO.Path]::GetFullPath(([string]$value.inbox_path).Trim())
        if ([string]::IsNullOrWhiteSpace($inbox)) { throw 'Agent inbox path is invalid.' }
        $tokenPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ([string]$value.token_file)))
        if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) { throw 'Agent token is missing.' }
    }
    return [pscustomobject]@{
        BaseUrl = $baseUrl
        PollSeconds = $poll
        ItemsEverySeconds = $itemsEvery
        InboxPath = $inbox
        DeliveryMode = $mode
        SqlServer = $server
        SqlDatabase = $database
        TokenPath = $tokenPath
        DesktopNotify = $notify
    }
}

function Read-DpapiToken {
    param([Parameter(Mandatory)][string]$Path)
    $serialized = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    $serialized = $serialized.TrimStart([char]0xFEFF).Trim()
    $secure = ConvertTo-SecureString -String $serialized
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if ([string]::IsNullOrWhiteSpace($plain) -or $plain.Length -gt 4096) { throw 'Agent token is invalid.' }
        return $plain
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $secure.Dispose()
    }
}

function Get-UnitKind {
    param([string]$UnitCode, [string]$UnitName)
    $text = (($UnitCode + ' ' + $UnitName).ToLowerInvariant())
    if ($text -match 'κιλ|χιλ|kg|kgr|kilo') { return 'kg' }
    if ($text -match 'τεμ|τεμαχ|τμχ|pcs|piece|tem') { return 'piece' }
    return 'unknown'
}

function Invoke-GalaxyItemQuery {
    param([Parameter(Mandatory)]$Config)
    Add-Type -AssemblyName System.Data
    $builder = New-Object System.Data.SqlClient.SqlConnectionStringBuilder
    $builder['Data Source'] = $Config.SqlServer
    $builder['Initial Catalog'] = $Config.SqlDatabase
    $builder['Integrated Security'] = $true
    $builder['ApplicationIntent'] = 'ReadOnly'
    $builder['Connect Timeout'] = 15
    $builder['Application Name'] = 'RAWLOGIC Transfer Agent'
    $connection = New-Object System.Data.SqlClient.SqlConnection($builder.ConnectionString)
    $command = $connection.CreateCommand()
    $command.CommandText = $script:ItemQuery
    $command.CommandTimeout = 60
    $command.CommandType = [System.Data.CommandType]::Text
    $rows = New-Object System.Collections.Generic.List[object]
    try {
        $connection.Open()
        $reader = $command.ExecuteReader()
        try {
            while ($reader.Read()) {
                $activeValue = $reader['is_active']
                $active = $false
                if ($activeValue -is [bool]) { $active = [bool]$activeValue }
                elseif ($null -ne $activeValue -and "$activeValue" -match '^(1|true)$') { $active = $true }
                $code = ([string]$reader['code']).Trim()
                $name = ([string]$reader['item_name']).Trim()
                if (-not $code -or -not $name) { continue }
                $rows.Add([pscustomobject]@{
                    code = $code
                    name = $name
                    is_active = $active
                    galaxy_item_id = ([string]$reader['galaxy_item_id']).Trim()
                    unit_code = ([string]$reader['unit_code']).Trim()
                    unit_name = ([string]$reader['unit_name']).Trim()
                }) | Out-Null
            }
        }
        finally { $reader.Close() }
    }
    finally {
        $connection.Close()
    }
    return $rows
}

function Get-LocalFingerprint {
    param($Rows)
    $lines = foreach ($row in @($Rows | Sort-Object code)) {
        '{0}|{1}|{2}|{3}|{4}|{5}' -f $row.code, $row.name, ([int]$row.is_active), $row.galaxy_item_id, $row.unit_code, $row.unit_name
    }
    $text = ($lines -join "`n")
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Invoke-AgentRequest {
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'POST')][string]$Method,
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Token,
        [string]$Body = ''
    )
    $headers = @{ 'x-agent-token' = $Token; 'Accept' = 'application/json' }
    $parameters = @{
        Method = $Method
        Uri = $Uri
        Headers = $headers
        TimeoutSec = 30
        ErrorAction = 'Stop'
    }
    if ($Body) {
        $parameters.Body = $Body
        $parameters.ContentType = 'application/json; charset=utf-8'
    }
    return Invoke-RestMethod @parameters
}

function Send-ItemSnapshot {
    param($Config, [string]$Token, $Rows)
    # Build the array by hand. Windows PowerShell 5.1 unwraps a one-item array.
    $pieces = New-Object System.Collections.Generic.List[string]
    foreach ($row in @($Rows)) {
        $one = [ordered]@{
            code = $row.code
            name = $row.name
            unit_code = $row.unit_code
            unit_name = $row.unit_name
            is_active = [bool]$row.is_active
            galaxy_item_id = $row.galaxy_item_id
        } | ConvertTo-Json -Depth 4 -Compress
        $pieces.Add($one) | Out-Null
    }
    $json = '{"items":[' + ($pieces -join ',') + ']}'
    $uri = '{0}/api/transfer-agent/items' -f $Config.BaseUrl
    return Invoke-AgentRequest -Method POST -Uri $uri -Token $Token -Body $json
}

function Show-DryRun {
    param($Rows)
    $kg = 0
    $pieces = 0
    $unknown = New-Object System.Collections.Generic.List[string]
    foreach ($row in $Rows) {
        if (-not $row.is_active) { continue }
        $kind = Get-UnitKind -UnitCode $row.unit_code -UnitName $row.unit_name
        if ($kind -eq 'kg') { $kg += 1 }
        elseif ($kind -eq 'piece') { $pieces += 1 }
        else {
            $label = ('{0} {1}' -f $row.unit_code, $row.unit_name).Trim()
            if ($unknown.Count -lt 8 -and -not $unknown.Contains($label)) { $unknown.Add($label) }
        }
    }
    Write-Output 'RAWLOGIC Transfer Agent DRY-RUN'
    Write-Output ('items: {0}' -f @($Rows).Count)
    Write-Output ('active_kg: {0}' -f $kg)
    Write-Output ('active_pieces: {0}' -f $pieces)
    Write-Output ('unknown_units: {0}' -f $unknown.Count)
    if ($unknown.Count -gt 0) { Write-Output ('unknown_unit_samples: {0}' -f ($unknown -join ' | ')) }
    Write-Output 'no files written'
    Write-Output 'no data sent'
}

function Write-InboxCsv {
    param(
        [Parameter(Mandatory)][string]$Inbox,
        [Parameter(Mandatory)][string]$Filename,
        [Parameter(Mandatory)][byte[]]$CsvBytes,
        [Parameter(Mandatory)][string]$ExpectedSha
    )
    if ($Filename -notmatch '^109_[0-9]{4}_[0-9]{4}(_[0-9]+)?\.csv$') { throw 'CSV filename is invalid.' }
    if ($CsvBytes.Length -ge 3 -and $CsvBytes[0] -eq 0xEF -and $CsvBytes[1] -eq 0xBB -and $CsvBytes[2] -eq 0xBF) {
        throw 'CSV contains a BOM.'
    }
    [IO.Directory]::CreateDirectory($Inbox) | Out-Null
    $finalPath = Join-Path $Inbox $Filename
    if (Test-Path -LiteralPath $finalPath -PathType Leaf) {
        $existing = (Get-FileHash -LiteralPath $finalPath -Algorithm SHA256).Hash
        if ($existing -eq $ExpectedSha) { return 'exists-same' }
        throw 'CSV already exists with different contents. It was left untouched.'
    }
    $temp = Join-Path $Inbox ('.{0}.{1}.partial' -f $Filename, ([guid]::NewGuid().ToString('N')))
    try {
        [IO.File]::WriteAllBytes($temp, $CsvBytes)
        $actual = (Get-FileHash -LiteralPath $temp -Algorithm SHA256).Hash
        if ($actual -ne $ExpectedSha) { throw 'CSV sha256 does not match.' }
        if (Test-Path -LiteralPath $finalPath -PathType Leaf) { throw 'CSV appeared while it was being written.' }
        [IO.File]::Move($temp, $finalPath)
    }
    catch {
        if (Test-Path -LiteralPath $temp -PathType Leaf) {
            Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        }
        throw
    }
    return 'written'
}

function Show-DesktopNotice {
    param([string]$Filename)
    try { [System.Media.SystemSounds]::Asterisk.Play() } catch { }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon = [System.Drawing.SystemIcons]::Information
        $icon.Visible = $true
        $icon.ShowBalloonTip(8000, 'Ενδοδιακίνηση 109', ('Νέο αρχείο ' + $Filename), [System.Windows.Forms.ToolTipIcon]::Info)
        Start-Sleep -Seconds 2
        $icon.Dispose()
    }
    catch { }
}

function Invoke-OnePoll {
    param($Config, [string]$Token)
    $now = Get-Date
    $stampPath = Join-Path $PSScriptRoot 'items-sync-at.txt'
    $localHashPath = Join-Path $PSScriptRoot 'items-fingerprint.txt'
    $serverHashPath = Join-Path $PSScriptRoot 'items-server-hash.txt'
    $due = $true
    if (Test-Path -LiteralPath $stampPath -PathType Leaf) {
        try {
            $previous = [DateTimeOffset]::Parse((Get-Content -LiteralPath $stampPath -Raw -Encoding UTF8).Trim())
            if (($now - $previous.LocalDateTime).TotalSeconds -lt $Config.ItemsEverySeconds) { $due = $false }
        }
        catch { $due = $true }
    }
    if ($due) {
        $rows = Invoke-GalaxyItemQuery -Config $Config
        $localHash = Get-LocalFingerprint -Rows $rows
        $savedLocal = ''
        if (Test-Path -LiteralPath $localHashPath -PathType Leaf) {
            $savedLocal = (Get-Content -LiteralPath $localHashPath -Raw -Encoding UTF8).Trim()
        }
        if ($savedLocal -and $savedLocal -eq $localHash) {
            $savedServer = ''
            if (Test-Path -LiteralPath $serverHashPath -PathType Leaf) {
                $savedServer = (Get-Content -LiteralPath $serverHashPath -Raw -Encoding UTF8).Trim()
            }
            $beatBody = @{ items_hash = $savedServer; item_count = @($rows).Count } | ConvertTo-Json -Compress
            $beat = Invoke-AgentRequest -Method POST -Uri ('{0}/api/transfer-agent/heartbeat' -f $Config.BaseUrl) -Token $Token -Body $beatBody
            if (-not $beat.fresh) { Remove-Item -LiteralPath $localHashPath -Force -ErrorAction SilentlyContinue }
        }
        else {
            $result = Send-ItemSnapshot -Config $Config -Token $Token -Rows $rows
            if (-not $result.ok) { throw 'Item list was rejected.' }
            [IO.File]::WriteAllText($localHashPath, $localHash, (New-Object Text.UTF8Encoding($false)))
            [IO.File]::WriteAllText($serverHashPath, ([string]$result.content_hash), (New-Object Text.UTF8Encoding($false)))
            Write-AgentLog -Message ('ITEMS count={0} changed={1}' -f $result.count, $result.changed)
            Write-AgentState -State 'CONNECTED' -ItemCount ([int]$result.count)
        }
        [IO.File]::WriteAllText($stampPath, ([DateTimeOffset]::Now.ToString('o')), (New-Object Text.UTF8Encoding($false)))
    }

    if ($Config.DeliveryMode -ne 'file') {
        if (-not $script:ApiModeNoted) {
            Write-AgentLog -Message 'API delivery is not built. File delivery is the only mode in this version.'
            $script:ApiModeNoted = $true
        }
        Write-AgentState -State 'CONNECTED' -LastError 'delivery mode api is not available'
        return
    }

    $next = Invoke-AgentRequest -Method GET -Uri ('{0}/api/transfer-agent/next' -f $Config.BaseUrl) -Token $Token
    if ($null -eq $next.batch) {
        Write-AgentState -State 'CONNECTED'
        return
    }
    $batch = $next.batch
    if ([string]$batch.delivery_mode -ne 'file') {
        Write-AgentLog -Message ('SKIP batch={0} mode={1}' -f $batch.id, $batch.delivery_mode)
        return
    }
    try {
        $csvBytes = [Convert]::FromBase64String([string]$batch.csv_base64)
        $written = Write-InboxCsv -Inbox $Config.InboxPath -Filename ([string]$batch.filename) -CsvBytes $csvBytes -ExpectedSha (([string]$batch.sha256).ToUpperInvariant())
        $ackBody = @{ sha256 = [string]$batch.sha256; filename = [string]$batch.filename } | ConvertTo-Json -Compress
        [void](Invoke-AgentRequest -Method POST -Uri ('{0}/api/transfer-agent/{1}/delivered' -f $Config.BaseUrl, $batch.id) -Token $Token -Body $ackBody)
        Write-AgentLog -Message ('DELIVERED file={0} result={1}' -f $batch.filename, $written)
        Write-AgentState -State 'CONNECTED' -LastFile ([string]$batch.filename)
        if ($written -eq 'written' -and $Config.DesktopNotify) { Show-DesktopNotice -Filename ([string]$batch.filename) }
    }
    catch {
        $message = ([string]$_.Exception.Message)
        if ($message.Length -gt 200) { $message = $message.Substring(0, 200) }
        try {
            $failBody = @{ error = $message } | ConvertTo-Json -Compress
            [void](Invoke-AgentRequest -Method POST -Uri ('{0}/api/transfer-agent/{1}/failed' -f $Config.BaseUrl, $batch.id) -Token $Token -Body $failBody)
        }
        catch { }
        Write-AgentLog -Message ('FAILED file={0} error={1}' -f $batch.filename, $message)
        Write-AgentState -State 'ERROR' -LastError $message
    }
}

$config = Read-ExactConfig
if ($DryRun) {
    $rows = Invoke-GalaxyItemQuery -Config $config
    Show-DryRun -Rows $rows
    exit 0
}

$token = Read-DpapiToken -Path $config.TokenPath
Write-AgentLog -Message ('START version={0} mode={1}' -f $script:AgentVersion, $config.DeliveryMode)
Write-AgentState -State 'STARTING'
while ($true) {
    try { Invoke-OnePoll -Config $config -Token $token }
    catch {
        Write-AgentLog -Message ('POLL_ERROR ' + $_.Exception.Message)
        Write-AgentState -State 'ERROR' -LastError ([string]$_.Exception.Message)
    }
    if ($RunOnce) { break }
    Start-Sleep -Seconds $config.PollSeconds
}
