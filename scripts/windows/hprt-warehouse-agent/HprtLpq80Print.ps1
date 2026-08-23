[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_-]{1,32768}$')][string]$PayloadBase64Url,
    [Parameter(Mandatory)][ValidateRange(1, 50)][int]$Copies,
    [Parameter(Mandatory)][ValidateLength(1, 255)][string]$PrinterName,
    [string]$DryRunOutputPath = '',
    [string]$PreviewOutputPath = ''
)

$ErrorActionPreference = 'Stop'
[void][Reflection.Assembly]::LoadWithPartialName('System.Drawing')

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

function Add-LabelText {
    param(
        [Parameter(Mandatory)][Drawing.Graphics]$Graphics,
        [Parameter(Mandatory)][string]$Text,
        [Parameter(Mandatory)][Drawing.RectangleF]$Rectangle,
        [int]$MaximumFontPixels = 16,
        [int]$MinimumFontPixels = 8,
        [Drawing.FontStyle]$Style = [Drawing.FontStyle]::Regular,
        [Drawing.StringAlignment]$Alignment = [Drawing.StringAlignment]::Center,
        [switch]$NoWrap
    )
    $value = (Get-LabelText -Value $Text) -replace '[\x00-\x08\x0b\x0c\x0e-\x1f]', ' '
    if (-not $value) { return }
    $format = New-Object Drawing.StringFormat
    $format.Alignment = $Alignment
    $format.LineAlignment = [Drawing.StringAlignment]::Center
    # Never hide legal label content with an ellipsis. Measure the complete
    # value, reduce the font until it truly fits, and fail if even the minimum
    # size does not fit the reserved rectangle.
    $format.Trimming = [Drawing.StringTrimming]::None
    if ($NoWrap) { $format.FormatFlags = [Drawing.StringFormatFlags]::NoWrap }
    try {
        for ($size = $MaximumFontPixels; $size -ge $MinimumFontPixels; $size--) {
            $font = New-Object Drawing.Font('Arial', [single]$size, $Style, [Drawing.GraphicsUnit]::Pixel)
            try {
                $measureWidth = if ($NoWrap) { [single]10000 } else { [single]$Rectangle.Width }
                $measureBounds = New-Object Drawing.SizeF($measureWidth, [single]10000)
                $measured = $Graphics.MeasureString($value, $font, $measureBounds, $format)
                if ($measured.Width -le ($Rectangle.Width + 1) -and $measured.Height -le ($Rectangle.Height + 1)) {
                    $Graphics.DrawString($value, $font, [Drawing.Brushes]::Black, $Rectangle, $format)
                    return
                }
            }
            finally { $font.Dispose() }
        }
        throw 'Dynamic label content does not fit the 50x70 layout.'
    }
    finally { $format.Dispose() }
}

function Add-NutritionTable {
    param(
        [Parameter(Mandatory)][Drawing.Graphics]$Graphics,
        [Parameter(Mandatory)][string]$Nutrition,
        [Parameter(Mandatory)][int]$Y
    )
    $text = (Get-LabelText -Value $Nutrition) -replace '^\s*Ανά\s+100\s*g\s*:\s*', ''
    if (-not $text) { return 0 }
    $entries = @($text -split ',\s+(?=[^0-9])' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($entries.Count -gt 8) { throw 'Nutrition declaration is too large for the 50x70 label.' }
    Add-LabelText -Graphics $Graphics -Text 'ΔΙΑΤΡΟΦΙΚΗ ΔΗΛΩΣΗ ΑΝΑ 100 g' -Rectangle (New-Object Drawing.RectangleF(14, $Y, 372, 19)) -MaximumFontPixels 12 -MinimumFontPixels 9 -Style Bold -NoWrap
    $rows = [Math]::Ceiling($entries.Count / 2.0)
    $cellWidth = 186
    $cellHeight = 25
    $pen = New-Object Drawing.Pen([Drawing.Color]::Black, 1)
    try {
        for ($i = 0; $i -lt $entries.Count; $i++) {
            $column = $i % 2
            $row = [Math]::Floor($i / 2)
            $isUnpairedLastEntry = (($entries.Count % 2) -eq 1) -and ($i -eq ($entries.Count - 1))
            $rectWidth = if ($isUnpairedLastEntry) { [single]372 } else { [single]$cellWidth }
            $rectX = if ($isUnpairedLastEntry) { [single]14 } else { [single](14 + ($column * $cellWidth)) }
            $rect = New-Object Drawing.RectangleF($rectX, ($Y + 19 + ($row * $cellHeight)), $rectWidth, $cellHeight)
            $Graphics.DrawRectangle($pen, [single]$rect.X, [single]$rect.Y, [single]$rect.Width, [single]$rect.Height)
            $inner = New-Object Drawing.RectangleF(($rect.X + 4), $rect.Y, ($rect.Width - 8), $rect.Height)
            # The shared text helper starts at the largest size and shrinks only
            # when the real value cannot fit. Nutrition cells remain centered.
            Add-LabelText -Graphics $Graphics -Text $entries[$i] -Rectangle $inner -MaximumFontPixels 14 -MinimumFontPixels 9 -NoWrap
        }
    }
    finally { $pen.Dispose() }
    return [int](19 + ($rows * $cellHeight))
}

function Add-ApprovalOval {
    param(
        [Parameter(Mandatory)][Drawing.Graphics]$Graphics,
        [Parameter(Mandatory)][string]$ApprovalNumber,
        [Parameter(Mandatory)][int]$Y
    )
    $raw = (Get-LabelText -Value $ApprovalNumber -Maximum 128) -replace '\s+', ' '
    $parts = @($raw.Split(' ', [StringSplitOptions]::RemoveEmptyEntries))
    $country = if ($parts.Count -ge 1) { $parts[0] } else { '' }
    $suffixIsPresent = $parts.Count -ge 3 -and $parts[-1] -match '^(CE|EC|EU)$'
    $suffix = if ($suffixIsPresent) { $parts[-1] } else { 'EU' }
    $numberEnd = if ($suffixIsPresent) { $parts.Count - 2 } else { $parts.Count - 1 }
    $numberParts = New-Object Collections.Generic.List[string]
    for ($i = 1; $i -le $numberEnd; $i++) { $numberParts.Add($parts[$i]) }
    $number = ($numberParts -join ' ').Trim()
    if (-not $country -or -not $number) { throw 'Approval number must contain a country code and establishment number.' }
    $pen = New-Object Drawing.Pen([Drawing.Color]::Black, 2)
    try { $Graphics.DrawEllipse($pen, 304, $Y, 80, 70) }
    finally { $pen.Dispose() }
    Add-LabelText -Graphics $Graphics -Text $country -Rectangle (New-Object Drawing.RectangleF(310, ($Y + 7), 68, 17)) -MaximumFontPixels 12 -MinimumFontPixels 10 -Style Bold -NoWrap
    Add-LabelText -Graphics $Graphics -Text $number -Rectangle (New-Object Drawing.RectangleF(308, ($Y + 23), 72, 25)) -MaximumFontPixels 14 -MinimumFontPixels 9 -Style Bold -NoWrap
    Add-LabelText -Graphics $Graphics -Text $suffix -Rectangle (New-Object Drawing.RectangleF(310, ($Y + 47), 68, 16)) -MaximumFontPixels 11 -MinimumFontPixels 9 -Style Bold -NoWrap
}

function Convert-BitmapToMonochromeBytes {
    param([Parameter(Mandatory)][Drawing.Bitmap]$Bitmap)
    $rect = New-Object Drawing.Rectangle(0, 0, $Bitmap.Width, $Bitmap.Height)
    $data = $Bitmap.LockBits($rect, [Drawing.Imaging.ImageLockMode]::ReadOnly, [Drawing.Imaging.PixelFormat]::Format24bppRgb)
    try {
        $stride = [Math]::Abs($data.Stride)
        $source = New-Object byte[] ($stride * $Bitmap.Height)
        [Runtime.InteropServices.Marshal]::Copy($data.Scan0, $source, 0, $source.Length)
        $bytesPerRow = [int][Math]::Ceiling($Bitmap.Width / 8.0)
        $output = New-Object byte[] ($bytesPerRow * $Bitmap.Height)
        for ($i = 0; $i -lt $output.Length; $i++) { $output[$i] = 0xFF }
        for ($y = 0; $y -lt $Bitmap.Height; $y++) {
            for ($x = 0; $x -lt $Bitmap.Width; $x++) {
                $offset = ($y * $stride) + ($x * 3)
                $blue = [int]$source[$offset]
                $green = [int]$source[$offset + 1]
                $red = [int]$source[$offset + 2]
                $luma = (($red * 299) + ($green * 587) + ($blue * 114)) / 1000
                if ($luma -lt 205) {
                    $target = ($y * $bytesPerRow) + [Math]::Floor($x / 8)
                    $mask = 0x80 -shr ($x % 8)
                    $output[$target] = [byte]($output[$target] -band (0xFF -bxor $mask))
                }
            }
        }
        return ,$output
    }
    finally { $Bitmap.UnlockBits($data) }
}

function New-UnifiedLabelBitmap {
    param([Parameter(Mandatory)][object]$Payload)
    if ([int]$Payload.schema_version -ne 3) { throw 'Unsupported dynamic label schema.' }
    if ([string]$Payload.printer_profile -cne 'HPRT_LPQ80_BITMAP_50X70') { throw 'Wrong dynamic printer profile.' }
    if (([string]$Payload.profile).Trim().ToUpperInvariant() -cne 'DISTRIBUTION') { throw 'Unsupported dynamic label profile.' }

    $displayName = Get-LabelText -Value $Payload.product.display_name -Maximum 255
    $legalName = Get-LabelText -Value $Payload.product.legal_name -Maximum 500
    if (-not $displayName) { $displayName = $legalName }
    $bitmap = New-Object Drawing.Bitmap(400, 560, [Drawing.Imaging.PixelFormat]::Format24bppRgb)
    $bitmap.SetResolution(203, 203)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.Clear([Drawing.Color]::White)
        $graphics.TextRenderingHint = [Drawing.Text.TextRenderingHint]::SingleBitPerPixelGridFit
        $graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.PixelOffsetMode = [Drawing.Drawing2D.PixelOffsetMode]::HighQuality

        $y = 7
        Add-LabelText -Graphics $graphics -Text $displayName -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, 42)) -MaximumFontPixels 30 -MinimumFontPixels 17 -Style Bold
        $y += 42
        Add-LabelText -Graphics $graphics -Text $legalName -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, 29)) -MaximumFontPixels 17 -MinimumFontPixels 9
        $y += 29

        if (-not [bool]$Payload.product.single_ingredient) {
            $ingredients = 'Συστατικά: ' + (Get-LabelText -Value $Payload.product.ingredients)
            Add-LabelText -Graphics $graphics -Text $ingredients -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, 52)) -MaximumFontPixels 16 -MinimumFontPixels 9
            $y += 52
        }
        $allergens = 'ΑΛΛΕΡΓΙΟΓΟΝΑ: ' + (Get-LabelText -Value $Payload.product.allergens)
        Add-LabelText -Graphics $graphics -Text $allergens -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, 31)) -MaximumFontPixels 17 -MinimumFontPixels 10 -Style Bold
        $y += 34

        $nutritionHeight = Add-NutritionTable -Graphics $graphics -Nutrition (Get-LabelText -Value $Payload.product.nutrition) -Y $y
        if ($nutritionHeight -gt 0) { $y += $nutritionHeight + 4 }

        $dates = 'ΠΑΡΑΓΩΓΗ: {0}     ΑΝΑΛΩΣΗ ΕΩΣ: {1}' -f (Get-LabelText $Payload.traceability.production_date 16), (Get-LabelText $Payload.traceability.use_by_date 16)
        Add-LabelText -Graphics $graphics -Text $dates -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, 24)) -MaximumFontPixels 16 -MinimumFontPixels 9 -Style Bold -NoWrap
        $y += 24
        $lotLine = 'LOT: {0}' -f (Get-LabelText $Payload.traceability.internal_lot 64)
        Add-LabelText -Graphics $graphics -Text $lotLine -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, 23)) -MaximumFontPixels 15 -MinimumFontPixels 8 -NoWrap
        $y += 23
        $source = Get-LabelText $Payload.traceability.source_lot 96
        if ($source) {
            Add-LabelText -Graphics $graphics -Text ("ΠΑΡΤΙΔΑ ΠΗΓΗΣ: $source") -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, 20)) -MaximumFontPixels 14 -MinimumFontPixels 8 -NoWrap
            $y += 20
        }
        Add-LabelText -Graphics $graphics -Text (Get-LabelText $Payload.storage 255) -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, 28)) -MaximumFontPixels 16 -MinimumFontPixels 9 -Style Bold
        $y += 28
        $origin = 'ΠΡΟΕΛΕΥΣΗ: ' + (Get-LabelText $Payload.product.origin 255)
        Add-LabelText -Graphics $graphics -Text $origin -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, 21)) -MaximumFontPixels 14 -MinimumFontPixels 8 -NoWrap
        $y += 21
        $usage = Get-LabelText $Payload.product.usage_instructions 500
        if ($usage) {
            Add-LabelText -Graphics $graphics -Text $usage -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, 33)) -MaximumFontPixels 14 -MinimumFontPixels 8
            $y += 33
        }
        if ($y -gt 449) { throw 'Dynamic label content does not fit the 50x70 layout.' }

        $separator = New-Object Drawing.Pen([Drawing.Color]::Black, 1)
        try { $graphics.DrawLine($separator, 14, 452, 386, 452) }
        finally { $separator.Dispose() }
        Add-LabelText -Graphics $graphics -Text 'Παρασκευάζεται και συσκευάζεται από:' -Rectangle (New-Object Drawing.RectangleF(14, 456, 278, 18)) -MaximumFontPixels 10 -MinimumFontPixels 8 -NoWrap
        Add-LabelText -Graphics $graphics -Text (Get-LabelText $Payload.business.name 255) -Rectangle (New-Object Drawing.RectangleF(14, 473, 278, 31)) -MaximumFontPixels 16 -MinimumFontPixels 9 -Style Bold
        Add-LabelText -Graphics $graphics -Text (Get-LabelText $Payload.business.address 500) -Rectangle (New-Object Drawing.RectangleF(14, 503, 278, 43)) -MaximumFontPixels 12 -MinimumFontPixels 8
        Add-ApprovalOval -Graphics $graphics -ApprovalNumber (Get-LabelText $Payload.business.approval_number 128) -Y 470
        return $bitmap
    }
    catch {
        $bitmap.Dispose()
        throw
    }
    finally { $graphics.Dispose() }
}

function New-Lpq80TsplDocument {
    param([Parameter(Mandatory)][object]$Payload, [Parameter(Mandatory)][int]$PrintCopies, [string]$PreviewPath = '')
    $bitmap = New-UnifiedLabelBitmap -Payload $Payload
    try {
        if ($PreviewPath) {
            $preview = [IO.Path]::GetFullPath($PreviewPath)
            $bitmap.Save($preview, [Drawing.Imaging.ImageFormat]::Png)
        }
        $pixels = Convert-BitmapToMonochromeBytes -Bitmap $bitmap
    }
    finally { $bitmap.Dispose() }

    $ascii = [Text.Encoding]::ASCII
    $header = $ascii.GetBytes("SIZE 50 mm,70 mm`r`nGAP 2 mm,0 mm`r`nDIRECTION 1`r`nREFERENCE 0,0`r`nCLS`r`nDENSITY 10`r`nBITMAP 0,0,50,560,0,")
    $suffix = $ascii.GetBytes(("`r`nPRINT 1,{0}`r`n" -f $PrintCopies))
    $stream = New-Object IO.MemoryStream
    try {
        $stream.Write($header, 0, $header.Length)
        $stream.Write($pixels, 0, $pixels.Length)
        $stream.Write($suffix, 0, $suffix.Length)
        return ,$stream.ToArray()
    }
    finally { $stream.Dispose() }
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
        $info.pDocName = 'Sklavounos Warehouse Unified Label 50x70'
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
$bytes = New-Lpq80TsplDocument -Payload $payload -PrintCopies $Copies -PreviewPath $PreviewOutputPath

if ($DryRunOutputPath) {
    [IO.File]::WriteAllBytes([IO.Path]::GetFullPath($DryRunOutputPath), $bytes)
    exit 0
}

if (-not (Get-Printer -Name $PrinterName -ErrorAction SilentlyContinue)) { throw 'Configured HPRT printer was not found.' }
Send-RawPrinterBytes -Name $PrinterName -Bytes $bytes
exit 0
