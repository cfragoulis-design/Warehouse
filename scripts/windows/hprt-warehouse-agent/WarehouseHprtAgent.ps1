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
    if ($Copies -lt 1 -or $Copies -gt 50) { throw 'Print copy count is invalid.' }
    $json = $Payload | ConvertTo-Json -Depth 12 -Compress
    $encoded = ConvertTo-Base64UrlUtf8 -Value $json
    $renderer = Join-Path $PSScriptRoot 'HprtLpq80Print.ps1'
    if (-not (Test-Path -LiteralPath $renderer -PathType Leaf)) { throw 'HPRT renderer is missing.' }
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    & $windowsPowerShell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $renderer -PayloadBase64Url $encoded -Copies $Copies -PrinterName $PrinterName 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'HPRT rendering failed.' }
}

function Invoke-OnePoll {
    param([Parameter(Mandatory)][object]$Config, [Parameter(Mandatory)][string]$Token)
    $nextUri = '{0}/api/print-jobs/next?station={1}' -f $Config.BaseUrl, $Config.Station
    $response = Invoke-AgentRequest -Method GET -Uri $nextUri -Token $Token
    if ($null -eq $response -or [bool]$response.ok -ne $true) { throw 'Print queue response is invalid.' }
    if ($null -eq $response.job) { return $false }

    $job = $response.job
    $jobId = [int]$job.id
    $claimToken = ([string]$job.claim_token).Trim()
    if ($jobId -le 0 -or -not $claimToken -or $claimToken.Length -gt 256) { throw 'Print claim is invalid.' }
    if ([string]$job.target_station -cne $Config.Station) { throw 'Print station does not match.' }
    if ([string]$job.label_key -notin @('HPRT_EFET_INTERNAL_80', 'HPRT_EFET_DISTRIBUTION_80')) { throw 'Print job is not an HPRT dynamic label.' }
    if ($null -eq $job.render_payload) { throw 'Print job has no render payload.' }

    if (-not (Test-JobAlreadyPrinted -JobId $jobId)) {
        try {
            Invoke-HprtRender -Payload $job.render_payload -Copies ([int]$job.copies) -PrinterName $Config.PrinterName
        }
        catch {
            try {
                $failUri = '{0}/api/print-jobs/{1}/fail?station={2}' -f $Config.BaseUrl, $jobId, $Config.Station
                [void](Invoke-AgentRequest -Method POST -Uri $failUri -Token $Token -ClaimToken $claimToken -Body @{ error_message = 'HPRT_PRINT_FAILED' })
            }
            catch { }
            Write-AgentLog -Message ('FAILED job={0} category=HPRT_PRINT_FAILED' -f $jobId)
            return $true
        }
        try { Save-PrintedJobId -JobId $jobId }
        catch { Write-AgentLog -Message ('JOURNAL_FAILED job={0}' -f $jobId) }
        Write-AgentLog -Message ('PRINTED job={0} copies={1}' -f $jobId, [int]$job.copies)
    }

    try {
        $doneUri = '{0}/api/print-jobs/{1}/done?station={2}' -f $Config.BaseUrl, $jobId, $Config.Station
        $done = Invoke-AgentRequest -Method POST -Uri $doneUri -Token $Token -ClaimToken $claimToken
        if ($null -eq $done -or [bool]$done.ok -ne $true) { throw 'Print completion response is invalid.' }
    }
    catch {
        Write-AgentLog -Message ('COMPLETION_UNCONFIRMED job={0}' -f $jobId)
        return $true
    }
    return $true
}

$config = Read-ExactConfig
$token = Read-DpapiToken -Path $config.TokenPath
Write-AgentLog -Message 'STARTED station=WORKSHOP renderer=HPRT_LPQ80_TSPL'

do {
    try { [void](Invoke-OnePoll -Config $config -Token $token) }
    catch {
        Write-AgentLog -Message 'POLL_FAILED category=CONNECTION_OR_RESPONSE'
        if ($RunOnce) { throw }
    }
    if (-not $RunOnce) { Start-Sleep -Seconds $config.PollSeconds }
} while (-not $RunOnce)

exit 0
