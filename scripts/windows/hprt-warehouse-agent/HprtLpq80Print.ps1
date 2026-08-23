[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_-]{1,32768}$')][string]$PayloadBase64Url,
    [Parameter(Mandatory)][ValidateRange(1, 50)][int]$Copies,
    [Parameter(Mandatory)][ValidateLength(1, 255)][string]$PrinterName,
    [string]$DryRunOutputPath = ''
)

$ErrorActionPreference = 'Stop'

function ConvertFrom-Base64UrlUtf8 {
    param([Parameter(Mandatory)][string]$Value)
    $base64 = $Value.Replace('-', '+').Replace('_', '/')
    switch ($base64.Length % 4) {
        0 { }
        2 { $base64 += '==' }
        3 { $base64 += '=' }
        default { throw 'Invalid dynamic label payload.' }
    }
    $bytes = [Convert]::FromBase64String($base64)
    if ($bytes.Length -gt 24576) { throw 'Dynamic label payload is too large.' }
    $utf8 = New-Object Text.UTF8Encoding($false, $true)
    return $utf8.GetString($bytes)
}

function Get-LabelText {
    param([object]$Value, [int]$Maximum = 4000)
    if ($null -eq $Value) { return '' }
    $text = ([string]$Value).Trim()
    if ($text.Length -gt $Maximum -or $text.IndexOf([char]0) -ge 0) {
        throw 'Dynamic label field is invalid.'
    }
    return $text
}

function ConvertTo-TsplText {
    param([Parameter(Mandatory)][string]$Value)
    return (($Value -replace '[\x00-\x1f]', ' ') -replace '"', "'").Trim()
}

function Split-LabelLines {
    param([string]$Value, [int]$MaximumCharacters = 42)
    $clean = (ConvertTo-TsplText -Value (Get-LabelText -Value $Value))
    if (-not $clean) { return @() }
    $result = New-Object Collections.Generic.List[string]
    $current = ''
    foreach ($word in @($clean -split '\s+')) {
        if ($word.Length -gt $MaximumCharacters) {
            if ($current) { $result.Add($current); $current = '' }
            for ($offset = 0; $offset -lt $word.Length; $offset += $MaximumCharacters) {
                $result.Add($word.Substring($offset, [Math]::Min($MaximumCharacters, $word.Length - $offset)))
            }
            continue
        }
        $candidate = if ($current) { "$current $word" } else { $word }
        if ($candidate.Length -le $MaximumCharacters) {
            $current = $candidate
        } else {
            $result.Add($current)
            $current = $word
        }
    }
    if ($current) { $result.Add($current) }
    return @($result)
}

function Add-TsplWrappedText {
    param(
        [Collections.Generic.List[string]]$Commands,
        [ref]$Y,
        [string]$Prefix,
        [string]$Value,
        [int]$MaxY,
        [int]$Scale = 1
    )
    $lineHeight = if ($Scale -eq 2) { 38 } else { 25 }
    $maxCharacters = if ($Scale -eq 2) { 22 } else { 42 }
    $prefixText = Get-LabelText -Value $Prefix
    $valueText = Get-LabelText -Value $Value
    $combined = if ($prefixText) { "$prefixText $valueText" } else { $valueText }
    $lines = Split-LabelLines -Value $combined -MaximumCharacters $maxCharacters
    foreach ($line in $lines) {
        if ($Y.Value + $lineHeight -gt $MaxY) { throw 'Dynamic label content does not fit the selected profile.' }
        $safe = ConvertTo-TsplText -Value $line
        $Commands.Add(('TEXT 18,{0},"0",0,{1},{1},"{2}"' -f $Y.Value, $Scale, $safe))
        $Y.Value += $lineHeight
    }
}

function New-Lpq80TsplDocument {
    param([Parameter(Mandatory)][object]$Payload, [Parameter(Mandatory)][int]$PrintCopies)
    if ([int]$Payload.schema_version -ne 1) { throw 'Unsupported dynamic label schema.' }
    if ([string]$Payload.printer_profile -cne 'HPRT_LPQ80_TSPL_80MM') { throw 'Wrong dynamic printer profile.' }
    $profile = ([string]$Payload.profile).Trim().ToUpperInvariant()
    if ($profile -notin @('INTERNAL', 'DISTRIBUTION')) { throw 'Unsupported dynamic label profile.' }

    $heightMm = if ($profile -eq 'DISTRIBUTION') { 120 } else { 72 }
    $heightDots = $heightMm * 8
    $maxY = $heightDots - 30
    $commands = New-Object Collections.Generic.List[string]
    $commands.Add(('SIZE 80 mm,{0} mm' -f $heightMm))
    $commands.Add('GAP 2 mm,0 mm')
    $commands.Add('DIRECTION 1')
    $commands.Add('REFERENCE 0,0')
    $commands.Add('CLS')
    $commands.Add('CODEPAGE 1253')
    $commands.Add('DENSITY 10')

    $y = 14
    $title = if ($profile -eq 'DISTRIBUTION') { 'ΕΤΙΚΕΤΑ ΔΙΑΘΕΣΗΣ' } else { 'ΕΣΩΤΕΡΙΚΗ ΙΧΝΗΛΑΣΙΜΟΤΗΤΑ' }
    Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix '' -Value $title -MaxY $maxY
    $commands.Add(('BAR 18,{0},604,2' -f $y)); $y += 12
    Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix '' -Value (Get-LabelText $Payload.product.legal_name) -MaxY $maxY -Scale 2
    Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'LOT: ' -Value (Get-LabelText $Payload.traceability.internal_lot 64) -MaxY $maxY
    if (Get-LabelText $Payload.traceability.source_lot 96) {
        Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'LOT ΠΡΟΜΗΘΕΥΤΗ: ' -Value (Get-LabelText $Payload.traceability.source_lot 96) -MaxY $maxY
    }
    Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'ΠΑΡΑΣΚΕΥΗ: ' -Value (Get-LabelText $Payload.traceability.production_date 16) -MaxY $maxY
    Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'ΑΝΑΛΩΣΗ ΕΩΣ: ' -Value (Get-LabelText $Payload.traceability.use_by_date 16) -MaxY $maxY
    Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'ΣΥΝΤΗΡΗΣΗ: ' -Value (Get-LabelText $Payload.storage 255) -MaxY $maxY

    if ($profile -eq 'DISTRIBUTION') {
        Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'ΚΑΘ. ΠΟΣΟΤΗΤΑ: ' -Value (Get-LabelText $Payload.net_quantity 64) -MaxY $maxY
        if (-not [bool]$Payload.product.single_ingredient) {
            Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'ΣΥΣΤΑΤΙΚΑ: ' -Value (Get-LabelText $Payload.product.ingredients) -MaxY $maxY
        }
        Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'ΑΛΛΕΡΓΙΟΓΟΝΑ: ' -Value (Get-LabelText $Payload.product.allergens) -MaxY $maxY
        if (Get-LabelText $Payload.product.origin) {
            Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'ΠΡΟΕΛΕΥΣΗ: ' -Value (Get-LabelText $Payload.product.origin) -MaxY $maxY
        }
        if (Get-LabelText $Payload.product.usage_instructions) {
            Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'ΟΔΗΓΙΕΣ: ' -Value (Get-LabelText $Payload.product.usage_instructions) -MaxY $maxY
        }
        if (Get-LabelText $Payload.product.nutrition) {
            Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'ΔΙΑΤΡΟΦΙΚΗ ΔΗΛΩΣΗ: ' -Value (Get-LabelText $Payload.product.nutrition) -MaxY $maxY
        }
        Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix '' -Value (Get-LabelText $Payload.business.name 255) -MaxY $maxY
        Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix '' -Value (Get-LabelText $Payload.business.address 500) -MaxY $maxY
        if (Get-LabelText $Payload.business.approval_number 128) {
            Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'ΑΡ. ΕΓΚΡΙΣΗΣ: ' -Value (Get-LabelText $Payload.business.approval_number 128) -MaxY $maxY
        }
    }

    if (Get-LabelText $Payload.extra_code 64) {
        Add-TsplWrappedText -Commands $commands -Y ([ref]$y) -Prefix 'ΚΩΔ.: ' -Value (Get-LabelText $Payload.extra_code 64) -MaxY $maxY
    }
    $qrData = ConvertTo-TsplText -Value ('LOT:' + (Get-LabelText $Payload.traceability.internal_lot 64) + ';SKU:' + (Get-LabelText $Payload.product.sku 64))
    if ($y + 80 -le $maxY) {
        $commands.Add(('QRCODE 540,{0},L,4,A,0,M2,S7,"{1}"' -f $y, $qrData))
    }
    $commands.Add(('PRINT 1,{0}' -f $PrintCopies))
    $commands.Add('')

    $encoding = [Text.Encoding]::GetEncoding(1253, (New-Object Text.EncoderReplacementFallback('?')), (New-Object Text.DecoderExceptionFallback))
    return $encoding.GetBytes(($commands -join "`r`n"))
}

function Send-RawPrinterBytes {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][byte[]]$Bytes)
    if (-not ('Sklavounos.Hprt.RawPrinter' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace Sklavounos.Hprt {
  public static class RawPrinter {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Ansi)]
    public class DOCINFOA { [MarshalAs(UnmanagedType.LPStr)] public string pDocName; [MarshalAs(UnmanagedType.LPStr)] public string pOutputFile; [MarshalAs(UnmanagedType.LPStr)] public string pDataType; }
    [DllImport("winspool.drv", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool OpenPrinter(string name, out IntPtr handle, IntPtr defaults);
    [DllImport("winspool.drv", SetLastError=true)] public static extern bool ClosePrinter(IntPtr handle);
    [DllImport("winspool.drv", SetLastError=true, CharSet=CharSet.Ansi)] public static extern int StartDocPrinter(IntPtr handle, int level, [In] DOCINFOA info);
    [DllImport("winspool.drv", SetLastError=true)] public static extern bool EndDocPrinter(IntPtr handle);
    [DllImport("winspool.drv", SetLastError=true)] public static extern bool StartPagePrinter(IntPtr handle);
    [DllImport("winspool.drv", SetLastError=true)] public static extern bool EndPagePrinter(IntPtr handle);
    [DllImport("winspool.drv", SetLastError=true)] public static extern bool WritePrinter(IntPtr handle, IntPtr bytes, int count, out int written);
  }
}
'@
    }
    $handle = [IntPtr]::Zero
    $unmanaged = [IntPtr]::Zero
    $documentStarted = $false
    $pageStarted = $false
    try {
        if (-not [Sklavounos.Hprt.RawPrinter]::OpenPrinter($Name, [ref]$handle, [IntPtr]::Zero)) { throw 'HPRT printer was not found.' }
        $info = New-Object Sklavounos.Hprt.RawPrinter+DOCINFOA
        $info.pDocName = 'Sklavounos Warehouse Dynamic Label'
        $info.pDataType = 'RAW'
        if ([Sklavounos.Hprt.RawPrinter]::StartDocPrinter($handle, 1, $info) -eq 0) { throw 'HPRT print document could not start.' }
        $documentStarted = $true
        if (-not [Sklavounos.Hprt.RawPrinter]::StartPagePrinter($handle)) { throw 'HPRT print page could not start.' }
        $pageStarted = $true
        $unmanaged = [Runtime.InteropServices.Marshal]::AllocHGlobal($Bytes.Length)
        [Runtime.InteropServices.Marshal]::Copy($Bytes, 0, $unmanaged, $Bytes.Length)
        $written = 0
        if (-not [Sklavounos.Hprt.RawPrinter]::WritePrinter($handle, $unmanaged, $Bytes.Length, [ref]$written) -or $written -ne $Bytes.Length) {
            throw 'HPRT did not accept the complete label payload.'
        }
    }
    finally {
        if ($unmanaged -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::FreeHGlobal($unmanaged) }
        if ($pageStarted) { [void][Sklavounos.Hprt.RawPrinter]::EndPagePrinter($handle) }
        if ($documentStarted) { [void][Sklavounos.Hprt.RawPrinter]::EndDocPrinter($handle) }
        if ($handle -ne [IntPtr]::Zero) { [void][Sklavounos.Hprt.RawPrinter]::ClosePrinter($handle) }
    }
}

$json = ConvertFrom-Base64UrlUtf8 -Value $PayloadBase64Url
try { $payload = ConvertFrom-Json -InputObject $json -ErrorAction Stop }
catch { throw 'Dynamic label payload is not valid JSON.' }
$bytes = New-Lpq80TsplDocument -Payload $payload -PrintCopies $Copies

if ($DryRunOutputPath) {
    [IO.File]::WriteAllBytes([IO.Path]::GetFullPath($DryRunOutputPath), $bytes)
    exit 0
}

if (-not (Get-Printer -Name $PrinterName -ErrorAction SilentlyContinue)) { throw 'Configured HPRT printer was not found.' }
Send-RawPrinterBytes -Name $PrinterName -Bytes $bytes
exit 0
