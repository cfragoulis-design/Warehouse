[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'config.json'),
    [switch]$RunOnce
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-AgentLog {
    param([Parameter(Mandatory)][string]$Message)
    $logPath = Join-Path $PSScriptRoot 'warehouse-hprt-agent.log'
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
        [Parameter(Mandatory)][ValidateSet('STARTING', 'CONNECTED', 'PRINTING', 'ERROR')][string]$State,
        [ValidateSet('STARTING', 'WAITING', 'ACTIVE', 'ERROR')][string]$QueueState = 'WAITING',
        [int]$CurrentJobId = 0,
        [string]$LastError = '',
        [switch]$ContactSucceeded,
        [switch]$PrintSucceeded
    )
    try {
        $path = Join-Path $PSScriptRoot 'agent-status.json'
        $existing = $null
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            try { $existing = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json }
            catch { $existing = $null }
        }
        $now = [DateTimeOffset]::Now.ToString('o')
        $lastContact = if ($ContactSucceeded) { $now } elseif ($existing) { [string]$existing.last_contact } else { '' }
        $lastPrint = if ($PrintSucceeded) { $now } elseif ($existing) { [string]$existing.last_print } else { '' }
        $status = [ordered]@{
            schema_version = 1
            station = 'WORKSHOP'
            state = $State
            queue_state = $QueueState
            current_job_id = if ($CurrentJobId -gt 0) { $CurrentJobId } else { $null }
            last_contact = $lastContact
            last_print = $lastPrint
            last_error = $LastError
            updated_at = $now
        }
        $json = $status | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($path, $json, (New-Object Text.UTF8Encoding($false)))
    }
    catch { }
}

function Save-PrintHistoryEvent {
    param(
        [Parameter(Mandatory)][int]$JobId,
        [Parameter(Mandatory)][string]$Profile,
        [Parameter(Mandatory)][string]$Product,
        [Parameter(Mandatory)][int]$Copies
    )
    $path = Join-Path $PSScriptRoot 'print-history.jsonl'
    $event = [ordered]@{
        timestamp = [DateTimeOffset]::Now.ToString('o')
        job_id = $JobId
        profile = $Profile
        product = $Product
        copies = $Copies
        result = 'PRINTED'
    }
    $line = ($event | ConvertTo-Json -Compress) + [Environment]::NewLine
    [IO.File]::AppendAllText($path, $line, (New-Object Text.UTF8Encoding($false)))
    $lines = @(Get-Content -LiteralPath $path -Encoding UTF8)
    if ($lines.Count -gt 200) {
        $trimmed = (@($lines | Select-Object -Last 200) -join [Environment]::NewLine) + [Environment]::NewLine
        [IO.File]::WriteAllText($path, $trimmed, (New-Object Text.UTF8Encoding($false)))
    }
}

function Read-ExactConfig {
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw 'Agent configuration is missing.' }
    try { $value = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'Agent configuration is invalid.' }
    foreach ($name in @('base_url', 'station', 'printer_name', 'poll_seconds', 'token_file')) {
        if ($value.PSObject.Properties.Name -notcontains $name) { throw 'Agent configuration is incomplete.' }
    }
    $base = [Uri]([string]$value.base_url).TrimEnd('/')
    if ($base.Scheme -ne 'https' -or -not $base.Host) { throw 'Agent base URL must use HTTPS.' }
    if ([string]$value.station -cne 'WORKSHOP') { throw 'HPRT Agent station must be WORKSHOP.' }
    $poll = [int]$value.poll_seconds
    if ($poll -lt 2 -or $poll -gt 60) { throw 'Agent polling interval is invalid.' }
    $printer = ([string]$value.printer_name).Trim()
    if (-not $printer -or $printer.Length -gt 255) { throw 'Agent printer name is invalid.' }
    $tokenPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ([string]$value.token_file)))
    if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) { throw 'Agent token is missing.' }
    return [pscustomobject]@{
        BaseUrl = $base.AbsoluteUri.TrimEnd('/')
        Station = 'WORKSHOP'
        PrinterName = $printer
        PollSeconds = $poll
        TokenPath = $tokenPath
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
    }
}

function ConvertTo-Base64UrlUtf8 {
    param([Parameter(Mandatory)][string]$Value)
    $bytes = (New-Object Text.UTF8Encoding($false, $true)).GetBytes($Value)
    if ($bytes.Length -gt 12000) { throw 'Dynamic label payload is too large.' }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Test-JobAlreadyPrinted {
    param([Parameter(Mandatory)][int]$JobId)
    $journal = Join-Path $PSScriptRoot 'printed-job-ids.log'
    if (-not (Test-Path -LiteralPath $journal -PathType Leaf)) { return $false }
    return @(Get-Content -LiteralPath $journal -Encoding ASCII -Tail 1000) -contains ([string]$JobId)
}

function Save-PrintedJobId {
    param([Parameter(Mandatory)][int]$JobId)
    $journal = Join-Path $PSScriptRoot 'printed-job-ids.log'
    Add-Content -LiteralPath $journal -Encoding ASCII -Value ([string]$JobId)
    $lines = @(Get-Content -LiteralPath $journal -Encoding ASCII)
    if ($lines.Count -gt 1000) {
        @($lines | Select-Object -Last 1000) | Set-Content -LiteralPath $journal -Encoding ASCII
    }
}

function Invoke-AgentRequest {
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'POST')][string]$Method,
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Token,
        [string]$ClaimToken = '',
        [hashtable]$Body = $null
    )
    $headers = @{ 'x-agent-token' = $Token }
    if ($ClaimToken) { $headers['x-print-claim-token'] = $ClaimToken }
    $parameters = @{
        Method = $Method
        Uri = $Uri
        Headers = $headers
        TimeoutSec = 20
        ErrorAction = 'Stop'
    }
    if ($null -ne $Body) { $parameters.Body = $Body }
    return Invoke-RestMethod @parameters
}

function Invoke-HprtRender {
    param(
        [Parameter(Mandatory)][object]$Payload,
        [Parameter(Mandatory)][int]$Copies,
        [Parameter(Mandatory)][string]$PrinterName
    )
    if ($Copies -lt 1 -or $Copies -gt 50) { throw 'HPRT_PAYLOAD_INVALID' }
    try {
        $json = $Payload | ConvertTo-Json -Depth 12 -Compress
        $encoded = ConvertTo-Base64UrlUtf8 -Value $json
    }
    catch {
        if ([string]$_.Exception.Message -match 'payload is too large') { throw 'HPRT_PAYLOAD_TOO_LARGE' }
        throw 'HPRT_PAYLOAD_INVALID'
    }
    $renderer = Join-Path $PSScriptRoot 'HprtLpq80Print.ps1'
    if (-not (Test-Path -LiteralPath $renderer -PathType Leaf)) { throw 'HPRT_RUNTIME_MISSING' }
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf)) { throw 'HPRT_RUNTIME_MISSING' }

    # Windows PowerShell turns stderr from a native child into ErrorRecords.
    # With the Agent's fail-fast preference those records used to interrupt this
    # function before we could classify the real renderer failure.  Continue is
    # scoped only to the child capture and the original preference is restored.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $rendererOutput = @(& $windowsPowerShell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $renderer -PayloadBase64Url $encoded -Copies $Copies -PrinterName $PrinterName 2>&1)
        $rendererExitCode = $LASTEXITCODE
    }
    catch { throw 'HPRT_RUNTIME_FAILED' }
    finally { $ErrorActionPreference = $previousErrorActionPreference }

    if ($null -eq $rendererExitCode) { throw 'HPRT_RUNTIME_FAILED' }
    if ($rendererExitCode -ne 0) {
        $rendererText = (($rendererOutput | ForEach-Object { [string]$_ }) -join ' ')
        $category = 'HPRT_RENDER_FAILED'
        if ($rendererText -match 'does not fit|Nutrition declaration is too large') { $category = 'LABEL_CONTENT_TOO_LARGE' }
        elseif ($rendererText -match 'payload is too large') { $category = 'HPRT_PAYLOAD_TOO_LARGE' }
        elseif ($rendererText -match 'payload is not valid JSON|Invalid dynamic label payload|field is invalid|Unsupported dynamic label schema|Wrong dynamic printer profile|Unsupported dynamic label profile|Approval number must contain') { $category = 'HPRT_PAYLOAD_INVALID' }
        elseif ($rendererText -match 'printer was not found|Configured HPRT printer was not found') { $category = 'HPRT_PRINTER_NOT_FOUND' }
        elseif ($rendererText -match 'print document could not start|print page could not start') { $category = 'HPRT_SPOOLER_FAILED' }
        elseif ($rendererText -match 'complete label payload') { $category = 'HPRT_WRITE_INCOMPLETE' }
        throw $category
    }
}

function Invoke-OnePoll {
    param([Parameter(Mandatory)][object]$Config, [Parameter(Mandatory)][string]$Token)
    $nextUri = '{0}/api/print-jobs/next?station={1}' -f $Config.BaseUrl, $Config.Station
    $response = Invoke-AgentRequest -Method GET -Uri $nextUri -Token $Token
    if ($null -eq $response -or [bool]$response.ok -ne $true) { throw 'Print queue response is invalid.' }
    if ($null -eq $response.job) {
        Write-AgentState -State CONNECTED -QueueState WAITING -ContactSucceeded
        return $false
    }

    $job = $response.job
    $jobId = [int]$job.id
    $claimToken = ([string]$job.claim_token).Trim()
    if ($jobId -le 0 -or -not $claimToken -or $claimToken.Length -gt 256) { throw 'Print claim is invalid.' }
    if ([string]$job.target_station -cne $Config.Station) { throw 'Print station does not match.' }
    if ([string]$job.label_key -notin @('HPRT_EFET_UNIFIED_50', 'HPRT_EFET_INTERNAL_80', 'HPRT_EFET_DISTRIBUTION_80')) { throw 'Print job is not an HPRT dynamic label.' }
    if ($null -eq $job.render_payload) { throw 'Print job has no render payload.' }

    Write-AgentState -State PRINTING -QueueState ACTIVE -CurrentJobId $jobId -ContactSucceeded

    if (-not (Test-JobAlreadyPrinted -JobId $jobId)) {
        try {
            Invoke-HprtRender -Payload $job.render_payload -Copies ([int]$job.copies) -PrinterName $Config.PrinterName
        }
        catch {
            $failureCategory = ([string]$_.Exception.Message).Trim()
            if ($failureCategory -notin @('LABEL_CONTENT_TOO_LARGE', 'HPRT_PAYLOAD_TOO_LARGE', 'HPRT_PAYLOAD_INVALID', 'HPRT_PRINTER_NOT_FOUND', 'HPRT_SPOOLER_FAILED', 'HPRT_WRITE_INCOMPLETE', 'HPRT_RENDER_FAILED', 'HPRT_RUNTIME_MISSING', 'HPRT_RUNTIME_FAILED')) {
                $failureCategory = 'HPRT_PRINT_FAILED'
            }
            try {
                $failUri = '{0}/api/print-jobs/{1}/fail?station={2}' -f $Config.BaseUrl, $jobId, $Config.Station
                [void](Invoke-AgentRequest -Method POST -Uri $failUri -Token $Token -ClaimToken $claimToken -Body @{ error_message = $failureCategory })
            }
            catch { }
            Write-AgentLog -Message ('FAILED job={0} category={1}' -f $jobId, $failureCategory)
            Write-AgentState -State ERROR -QueueState ERROR -CurrentJobId $jobId -LastError $failureCategory -ContactSucceeded
            return $true
        }
        try { Save-PrintedJobId -JobId $jobId }
        catch { Write-AgentLog -Message ('JOURNAL_FAILED job={0}' -f $jobId) }
        try {
            $profile = ([string]$job.render_payload.profile).Trim()
            $product = ([string]$job.render_payload.product.legal_name).Trim()
            if (-not $product) { $product = 'Δυναμική ετικέτα' }
            Save-PrintHistoryEvent -JobId $jobId -Profile $profile -Product $product -Copies ([int]$job.copies)
        }
        catch { Write-AgentLog -Message ('HISTORY_FAILED job={0}' -f $jobId) }
        Write-AgentLog -Message ('PRINTED job={0} copies={1}' -f $jobId, [int]$job.copies)
        Write-AgentState -State CONNECTED -QueueState WAITING -PrintSucceeded -ContactSucceeded
    }

    try {
        $doneUri = '{0}/api/print-jobs/{1}/done?station={2}' -f $Config.BaseUrl, $jobId, $Config.Station
        $done = Invoke-AgentRequest -Method POST -Uri $doneUri -Token $Token -ClaimToken $claimToken
        if ($null -eq $done -or [bool]$done.ok -ne $true) { throw 'Print completion response is invalid.' }
    }
    catch {
        Write-AgentLog -Message ('COMPLETION_UNCONFIRMED job={0}' -f $jobId)
        Write-AgentState -State ERROR -QueueState ERROR -CurrentJobId $jobId -LastError 'COMPLETION_UNCONFIRMED' -ContactSucceeded
        return $true
    }
    Write-AgentState -State CONNECTED -QueueState WAITING -ContactSucceeded
    return $true
}

$config = Read-ExactConfig
$token = Read-DpapiToken -Path $config.TokenPath
Write-AgentLog -Message 'STARTED station=WORKSHOP renderer=HPRT_LPQ80_BITMAP_50X70'
Write-AgentState -State STARTING -QueueState STARTING

do {
    try { [void](Invoke-OnePoll -Config $config -Token $token) }
    catch {
        Write-AgentLog -Message 'POLL_FAILED category=CONNECTION_OR_RESPONSE'
        Write-AgentState -State ERROR -QueueState ERROR -LastError 'CONNECTION_OR_RESPONSE'
        if ($RunOnce) { throw }
    }
    if (-not $RunOnce) { Start-Sleep -Seconds $config.PollSeconds }
} while (-not $RunOnce)

exit 0
