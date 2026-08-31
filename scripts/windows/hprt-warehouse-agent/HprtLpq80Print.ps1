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

function Get-LabelLayoutSpecification {
    $specification = [ordered]@{
        title_font_px = @(27, 17, 32)
        title_height_px = @(42, 34, 56)
        legal_name_font_px = @(14, 9, 20)
        legal_name_height_px = @(29, 20, 44)
        ingredients_font_px = @(13, 9, 18)
        ingredients_height_px = @(52, 32, 76)
        allergens_font_px = @(14, 10, 18)
        allergens_height_px = @(31, 22, 48)
        allergens_gap_after_px = @(3, 0, 12)
        nutrition_heading_font_px = @(12, 9, 16)
        nutrition_heading_height_px = @(19, 15, 28)
        nutrition_cell_font_px = @(11, 8, 14)
        nutrition_row_height_px = @(22, 18, 32)
        nutrition_gap_after_px = @(4, 0, 12)
        dates_font_px = @(13, 9, 16)
        dates_height_px = @(24, 18, 34)
        lot_font_px = @(12, 8, 15)
        lot_height_px = @(23, 16, 32)
        source_lot_font_px = @(11, 8, 14)
        source_lot_height_px = @(20, 14, 30)
        storage_font_px = @(13, 9, 16)
        storage_height_px = @(28, 18, 44)
        origin_font_px = @(11, 8, 14)
        origin_height_px = @(21, 16, 32)
        usage_font_px = @(11, 8, 14)
        usage_height_px = @(33, 18, 50)
        footer_caption_font_px = @(10, 8, 12)
        footer_name_font_px = @(13, 9, 16)
        footer_address_font_px = @(10, 8, 12)
        approval_country_font_px = @(12, 10, 14)
        approval_number_font_px = @(14, 9, 18)
        approval_suffix_font_px = @(11, 9, 14)
    }
    return ,$specification
}

function Get-CanonicalLabelLayoutDefaults {
    $specification = Get-LabelLayoutSpecification
    $settings = [ordered]@{}
    foreach ($name in $specification.Keys) {
        $settings[$name] = [int]$specification[$name][0]
    }
    return ,$settings
}

function ConvertTo-StrictLayoutInteger {
    param(
        [Parameter(Mandatory)][object]$Value,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][int64]$Minimum,
        [Parameter(Mandatory)][int64]$Maximum
    )
    $isInteger = (
        $Value -is [byte] -or $Value -is [sbyte] -or
        $Value -is [int16] -or $Value -is [uint16] -or
        $Value -is [int32] -or $Value -is [uint32] -or
        $Value -is [int64] -or $Value -is [uint64]
    )
    if (-not $isInteger) { throw "Label layout setting $Name must be an integer." }
    try { $number = [int64]$Value }
    catch { throw "Label layout setting $Name is invalid." }
    if ($number -lt $Minimum -or $number -gt $Maximum) {
        throw "Label layout setting $Name is outside the allowed range."
    }
    return [int]$number
}

function Get-CanonicalSettingsSha256 {
    param([Parameter(Mandatory)][Collections.Specialized.OrderedDictionary]$Settings)
    $names = [string[]]@($Settings.Keys | ForEach-Object { [string]$_ })
    [Array]::Sort($names, [StringComparer]::Ordinal)
    $pairs = New-Object Collections.Generic.List[string]
    foreach ($name in $names) {
        $pairs.Add(('"{0}":{1}' -f $name, [int]$Settings[$name]))
    }
    $canonicalJson = '{' + ($pairs -join ',') + '}'
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash((New-Object Text.UTF8Encoding($false, $true)).GetBytes($canonicalJson))
        return (($digest | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally { $sha.Dispose() }
}

function Resolve-LabelLayout {
    param(
        [Parameter(Mandatory)][object]$Payload,
        [Parameter(Mandatory)][int]$SchemaVersion
    )
    $defaults = Get-CanonicalLabelLayoutDefaults
    if ($SchemaVersion -ne 6 -and $SchemaVersion -ne 7) { return [pscustomobject]$defaults }
    $schemaLabel = "Schema $SchemaVersion"

    $layout = $Payload.layout
    if ($null -eq $layout -or $layout -isnot [pscustomobject]) {
        throw "$schemaLabel label layout is missing or invalid."
    }
    $requiredLayoutFields = @('contract_version', 'version_id', 'settings_sha256', 'settings')
    $layoutFields = @($layout.PSObject.Properties.Name)
    foreach ($name in $layoutFields) {
        if ($requiredLayoutFields -cnotcontains $name) { throw "Unknown schema $SchemaVersion label layout field: $name." }
    }
    foreach ($name in $requiredLayoutFields) {
        if ($layoutFields -cnotcontains $name) { throw "$schemaLabel label layout field is missing: $name." }
    }

    $contractVersion = ConvertTo-StrictLayoutInteger -Value $layout.contract_version -Name 'contract_version' -Minimum 1 -Maximum 1
    [void]$contractVersion
    $versionId = ConvertTo-StrictLayoutInteger -Value $layout.version_id -Name 'version_id' -Minimum 1 -Maximum ([int]::MaxValue)
    [void]$versionId

    $settingsObject = $layout.settings
    if ($null -eq $settingsObject -or $settingsObject -isnot [pscustomobject]) {
        throw "$schemaLabel label layout settings are missing or invalid."
    }
    $specification = Get-LabelLayoutSpecification
    $providedNames = @($settingsObject.PSObject.Properties.Name)
    foreach ($name in $providedNames) {
        if (-not $specification.Contains($name)) { throw "Unknown label layout setting: $name." }
    }
    foreach ($name in $specification.Keys) {
        if ($providedNames -cnotcontains $name) { throw "Label layout setting is missing: $name." }
    }
    if ($providedNames.Count -ne $specification.Count) { throw "$schemaLabel label layout settings are incomplete." }

    $settings = [ordered]@{}
    foreach ($name in $specification.Keys) {
        $range = $specification[$name]
        $settings[$name] = ConvertTo-StrictLayoutInteger -Value $settingsObject.$name -Name $name -Minimum ([int64]$range[1]) -Maximum ([int64]$range[2])
    }
    $claimedHash = (Get-LabelText -Value $layout.settings_sha256 -Maximum 64).ToLowerInvariant()
    if ($claimedHash -notmatch '^[0-9a-f]{64}$') { throw "$schemaLabel label layout hash is invalid." }
    $actualHash = Get-CanonicalSettingsSha256 -Settings $settings
    if (-not [string]::Equals($claimedHash, $actualHash, [StringComparison]::Ordinal)) {
        throw "$schemaLabel label layout hash does not match its settings."
    }
    return [pscustomobject]$settings
}

function ConvertTo-StrictLabelContentText {
    param(
        [Parameter(Mandatory)][object]$Value,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][int]$Maximum
    )
    if ($Value -isnot [string]) { throw "Label content $Name must be text." }
    $text = Get-LabelText -Value $Value -Maximum $Maximum
    if (-not $text) { throw "Label content $Name is required." }
    $normalized = $text.Normalize([Text.NormalizationForm]::FormC)
    if (-not [string]::Equals($text, $normalized, [StringComparison]::Ordinal)) {
        throw "Label content $Name must use canonical Unicode."
    }
    $bidiControls = @(0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069)
    foreach ($character in $text.ToCharArray()) {
        $code = [int][char]$character
        $category = [Globalization.CharUnicodeInfo]::GetUnicodeCategory($character)
        if (
            [char]::IsControl($character) -or
            [char]::IsSurrogate($character) -or
            $category -eq [Globalization.UnicodeCategory]::Format -or
            $bidiControls -contains $code
        ) {
            throw "Label content $Name contains a forbidden control character."
        }
    }
    return $text
}

function ConvertTo-CanonicalJsonStringLiteral {
    param([Parameter(Mandatory)][string]$Value)
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

function Get-CanonicalLabelContentSha256 {
    param([Parameter(Mandatory)][Collections.Specialized.OrderedDictionary]$Content)
    $properties = @(
        ('"company_address":' + (ConvertTo-CanonicalJsonStringLiteral -Value ([string]$Content.company_address)))
        ('"company_name":' + (ConvertTo-CanonicalJsonStringLiteral -Value ([string]$Content.company_name)))
        ('"footer_caption":' + (ConvertTo-CanonicalJsonStringLiteral -Value ([string]$Content.footer_caption)))
        ('"logo_asset_id":' + (ConvertTo-CanonicalJsonStringLiteral -Value ([string]$Content.logo_asset_id)))
    )
    $canonicalJson = '{' + ($properties -join ',') + '}'
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash((New-Object Text.UTF8Encoding($false, $true)).GetBytes($canonicalJson))
        return (($digest | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally { $sha.Dispose() }
}

function Resolve-LabelContent {
    param(
        [Parameter(Mandatory)][object]$Payload,
        [Parameter(Mandatory)][int]$SchemaVersion
    )
    if ($SchemaVersion -ne 7) {
        return [pscustomobject][ordered]@{
            footer_caption = 'Παρασκευάζεται και συσκευάζεται από:'
            company_name = Get-LabelText -Value $Payload.business.name -Maximum 255
            company_address = Get-LabelText -Value $Payload.business.address -Maximum 500
            logo_asset_id = 'NONE'
        }
    }

    $snapshot = $Payload.label_content
    if ($null -eq $snapshot -or $snapshot -isnot [pscustomobject]) {
        throw 'Schema 7 label content snapshot is missing or invalid.'
    }
    $requiredSnapshotFields = @('contract_version', 'version_id', 'content_sha256', 'content')
    $snapshotFields = @($snapshot.PSObject.Properties.Name)
    foreach ($name in $snapshotFields) {
        if ($requiredSnapshotFields -cnotcontains $name) { throw "Unknown schema 7 label content snapshot field: $name." }
    }
    foreach ($name in $requiredSnapshotFields) {
        if ($snapshotFields -cnotcontains $name) { throw "Schema 7 label content snapshot field is missing: $name." }
    }
    [void](ConvertTo-StrictLayoutInteger -Value $snapshot.contract_version -Name 'label_content.contract_version' -Minimum 1 -Maximum 1)
    [void](ConvertTo-StrictLayoutInteger -Value $snapshot.version_id -Name 'label_content.version_id' -Minimum 1 -Maximum ([int]::MaxValue))

    $rawContent = $snapshot.content
    if ($null -eq $rawContent -or $rawContent -isnot [pscustomobject]) {
        throw 'Schema 7 label content is missing or invalid.'
    }
    $requiredContentFields = @('footer_caption', 'company_name', 'company_address', 'logo_asset_id')
    $contentFields = @($rawContent.PSObject.Properties.Name)
    foreach ($name in $contentFields) {
        if ($requiredContentFields -cnotcontains $name) { throw "Unknown schema 7 label content field: $name." }
    }
    foreach ($name in $requiredContentFields) {
        if ($contentFields -cnotcontains $name) { throw "Schema 7 label content field is missing: $name." }
    }
    if ($contentFields.Count -ne $requiredContentFields.Count) { throw 'Schema 7 label content is incomplete.' }

    $content = [ordered]@{
        footer_caption = ConvertTo-StrictLabelContentText -Value $rawContent.footer_caption -Name 'footer_caption' -Maximum 120
        company_name = ConvertTo-StrictLabelContentText -Value $rawContent.company_name -Name 'company_name' -Maximum 255
        company_address = ConvertTo-StrictLabelContentText -Value $rawContent.company_address -Name 'company_address' -Maximum 500
        logo_asset_id = ConvertTo-StrictLabelContentText -Value $rawContent.logo_asset_id -Name 'logo_asset_id' -Maximum 32
    }
    if ($content.logo_asset_id -cne 'NONE' -and $content.logo_asset_id -cne 'SKLAVOUNOS_MARK') {
        throw 'Schema 7 logo_asset_id is not approved.'
    }
    $claimedHash = (Get-LabelText -Value $snapshot.content_sha256 -Maximum 64).ToLowerInvariant()
    if ($claimedHash -notmatch '^[0-9a-f]{64}$') { throw 'Schema 7 label content hash is invalid.' }
    $actualHash = Get-CanonicalLabelContentSha256 -Content $content
    if (-not [string]::Equals($claimedHash, $actualHash, [StringComparison]::Ordinal)) {
        throw 'Schema 7 label content hash does not match its content.'
    }
    return [pscustomobject]$content
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
    $format.Trimming = [Drawing.StringTrimming]::EllipsisWord
    if ($NoWrap) { $format.FormatFlags = [Drawing.StringFormatFlags]::NoWrap }
    try {
        for ($size = $MaximumFontPixels; $size -ge $MinimumFontPixels; $size--) {
            $font = New-Object Drawing.Font('Arial', [single]$size, $Style, [Drawing.GraphicsUnit]::Pixel)
            try {
                $measured = $Graphics.MeasureString($value, $font, [int]$Rectangle.Width, $format)
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
        [Parameter(Mandatory)][int]$Y,
        [Parameter(Mandatory)][object]$Layout
    )
    $text = (Get-LabelText -Value $Nutrition) -replace '^\s*Ανά\s+100\s*g\s*:\s*', ''
    if (-not $text) { return 0 }
    $entries = @($text -split ',\s+(?=[^0-9])' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($entries.Count -gt 8) { throw 'Nutrition declaration is too large for the 50x70 label.' }
    Add-LabelText -Graphics $Graphics -Text 'ΔΙΑΤΡΟΦΙΚΗ ΔΗΛΩΣΗ ΑΝΑ 100 g' -Rectangle (New-Object Drawing.RectangleF(14, $Y, 372, $Layout.nutrition_heading_height_px)) -MaximumFontPixels $Layout.nutrition_heading_font_px -MinimumFontPixels 9 -Style Bold -NoWrap
    $rows = [Math]::Ceiling($entries.Count / 2.0)
    $cellWidth = 186
    $cellHeight = [int]$Layout.nutrition_row_height_px
    $pen = New-Object Drawing.Pen([Drawing.Color]::Black, 1)
    try {
        for ($i = 0; $i -lt $entries.Count; $i++) {
            $column = $i % 2
            $row = [Math]::Floor($i / 2)
            $rect = New-Object Drawing.RectangleF((14 + ($column * $cellWidth)), ($Y + $Layout.nutrition_heading_height_px + ($row * $cellHeight)), $cellWidth, $cellHeight)
            $Graphics.DrawRectangle($pen, [single]$rect.X, [single]$rect.Y, [single]$rect.Width, [single]$rect.Height)
            $inner = New-Object Drawing.RectangleF(($rect.X + 4), $rect.Y, ($rect.Width - 8), $rect.Height)
            Add-LabelText -Graphics $Graphics -Text $entries[$i] -Rectangle $inner -MaximumFontPixels $Layout.nutrition_cell_font_px -MinimumFontPixels 8 -Alignment Center -NoWrap
        }
    }
    finally { $pen.Dispose() }
    return [int]($Layout.nutrition_heading_height_px + ($rows * $cellHeight))
}

function Add-ApprovedCompanyLogo {
    param(
        [Parameter(Mandatory)][Drawing.Graphics]$Graphics,
        [Parameter(Mandatory)][string]$AssetId
    )
    if ($AssetId -ceq 'NONE') { return $false }
    if ($AssetId -cne 'SKLAVOUNOS_MARK') { throw 'Company logo asset is not approved.' }

    $assetPath = Join-Path $PSScriptRoot 'company-logo-sklavounos.png'
    if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf)) {
        throw 'Approved company logo asset is missing from the Agent package.'
    }
    $expectedHash = '41633fd9bf9fc15c885c1c6b39ddfb9211c85a330bf07bc4465c1de3d357eeff'
    $assetSha = [Security.Cryptography.SHA256]::Create()
    try {
        $actualHash = (($assetSha.ComputeHash([IO.File]::ReadAllBytes($assetPath)) | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally { $assetSha.Dispose() }
    if (-not [string]::Equals($actualHash, $expectedHash, [StringComparison]::Ordinal)) {
        throw 'Approved company logo asset failed integrity verification.'
    }

    $source = [Drawing.Image]::FromFile([IO.Path]::GetFullPath($assetPath))
    try {
        if ($source.Width -ne 1188 -or $source.Height -ne 1018) {
            throw 'Approved company logo dimensions are invalid.'
        }
        $box = New-Object Drawing.RectangleF(17, 478, 50, 64)
        $scale = [Math]::Min($box.Width / $source.Width, $box.Height / $source.Height)
        $width = [single]($source.Width * $scale)
        $height = [single]($source.Height * $scale)
        $destination = New-Object Drawing.RectangleF(
            [single]($box.X + (($box.Width - $width) / 2)),
            [single]($box.Y + (($box.Height - $height) / 2)),
            $width,
            $height
        )
        $Graphics.DrawImage($source, $destination)
    }
    finally { $source.Dispose() }
    return $true
}

function Add-ApprovalOval {
    param(
        [Parameter(Mandatory)][Drawing.Graphics]$Graphics,
        [Parameter(Mandatory)][string]$ApprovalNumber,
        [Parameter(Mandatory)][int]$Y,
        [Parameter(Mandatory)][object]$Layout
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
    Add-LabelText -Graphics $Graphics -Text $country -Rectangle (New-Object Drawing.RectangleF(310, ($Y + 7), 68, 17)) -MaximumFontPixels $Layout.approval_country_font_px -MinimumFontPixels 10 -Style Bold -NoWrap
    Add-LabelText -Graphics $Graphics -Text $number -Rectangle (New-Object Drawing.RectangleF(308, ($Y + 23), 72, 25)) -MaximumFontPixels $Layout.approval_number_font_px -MinimumFontPixels 9 -Style Bold -NoWrap
    Add-LabelText -Graphics $Graphics -Text $suffix -Rectangle (New-Object Drawing.RectangleF(310, ($Y + 47), 68, 16)) -MaximumFontPixels $Layout.approval_suffix_font_px -MinimumFontPixels 9 -Style Bold -NoWrap
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

function Save-MonochromePreviewPng {
    param(
        [Parameter(Mandatory)][byte[]]$Pixels,
        [Parameter(Mandatory)][ValidateRange(1, 4096)][int]$Width,
        [Parameter(Mandatory)][ValidateRange(1, 4096)][int]$Height,
        [Parameter(Mandatory)][string]$Path
    )
    $bytesPerRow = [int][Math]::Ceiling($Width / 8.0)
    if ($Pixels.Length -ne ($bytesPerRow * $Height)) { throw 'Monochrome preview data is invalid.' }

    # A 1-bpp Windows bitmap row is padded to a 4-byte boundary and stored
    # bottom-up.  Build that bitmap directly from the exact TSPL raster bytes,
    # then let GDI+ encode it as PNG.  No anti-aliased source pixels survive.
    $bitmapRowBytes = [int]([Math]::Ceiling($bytesPerRow / 4.0) * 4)
    $pixelDataSize = $bitmapRowBytes * $Height
    $pixelOffset = 14 + 40 + 8
    $fileSize = $pixelOffset + $pixelDataSize
    $stream = New-Object IO.MemoryStream
    $writer = New-Object IO.BinaryWriter($stream, [Text.Encoding]::ASCII, $true)
    try {
        $writer.Write([byte][char]'B')
        $writer.Write([byte][char]'M')
        $writer.Write([int]$fileSize)
        $writer.Write([int16]0)
        $writer.Write([int16]0)
        $writer.Write([int]$pixelOffset)
        $writer.Write([int]40)
        $writer.Write([int]$Width)
        $writer.Write([int]$Height)
        $writer.Write([int16]1)
        $writer.Write([int16]1)
        $writer.Write([int]0)
        $writer.Write([int]$pixelDataSize)
        $writer.Write([int]7992)
        $writer.Write([int]7992)
        $writer.Write([int]2)
        $writer.Write([int]2)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([byte]255)
        $writer.Write([byte]255)
        $writer.Write([byte]255)
        $writer.Write([byte]0)
        $padding = New-Object byte[] ($bitmapRowBytes - $bytesPerRow)
        for ($y = $Height - 1; $y -ge 0; $y--) {
            $writer.Write($Pixels, ($y * $bytesPerRow), $bytesPerRow)
            if ($padding.Length -gt 0) { $writer.Write($padding) }
        }
        $writer.Flush()
        $stream.Position = 0
        $source = [Drawing.Image]::FromStream($stream)
        try {
            $preview = New-Object Drawing.Bitmap($source)
            try { $preview.Save([IO.Path]::GetFullPath($Path), [Drawing.Imaging.ImageFormat]::Png) }
            finally { $preview.Dispose() }
        }
        finally { $source.Dispose() }
    }
    finally {
        $writer.Dispose()
        $stream.Dispose()
    }
}

function New-UnifiedLabelBitmap {
    param([Parameter(Mandatory)][object]$Payload)
    $schemaVersion = [int]$Payload.schema_version
    if ($schemaVersion -ne 3 -and $schemaVersion -ne 4 -and $schemaVersion -ne 5 -and $schemaVersion -ne 6 -and $schemaVersion -ne 7) { throw 'Unsupported dynamic label schema.' }
    if ([string]$Payload.printer_profile -cne 'HPRT_LPQ80_BITMAP_50X70') { throw 'Wrong dynamic printer profile.' }
    if (([string]$Payload.profile).Trim().ToUpperInvariant() -cne 'DISTRIBUTION') { throw 'Unsupported dynamic label profile.' }
    $layout = Resolve-LabelLayout -Payload $Payload -SchemaVersion $schemaVersion
    $labelContent = Resolve-LabelContent -Payload $Payload -SchemaVersion $schemaVersion
    if ($schemaVersion -eq 7 -and [int]$Payload.layout.version_id -ne [int]$Payload.label_content.version_id) {
        throw 'Schema 7 layout and label content must use the same immutable version.'
    }

    $displayName = Get-LabelText -Value $Payload.product.display_name -Maximum 255
    $legalName = Get-LabelText -Value $Payload.product.legal_name -Maximum 500
    $unit = if ($schemaVersion -ge 4) { (Get-LabelText -Value $Payload.product.unit -Maximum 8).Trim().ToLowerInvariant() } else { '' }
    $plainTraceability = if ($schemaVersion -eq 4) {
        [bool]$Payload.product.plain_piece
    }
    elseif ($schemaVersion -ge 5) {
        [bool]$Payload.product.plain_traceability
    }
    else {
        $false
    }
    $singleIngredient = [bool]$Payload.product.single_ingredient
    $ingredientText = Get-LabelText -Value $Payload.product.ingredients
    $allergenText = Get-LabelText -Value $Payload.product.allergens
    $nutritionText = Get-LabelText -Value $Payload.product.nutrition
    $nutritionExempt = [bool]$Payload.product.nutrition_exempt
    if ($schemaVersion -eq 4 -and $plainTraceability -and $unit -cne 'pcs') {
        throw 'Schema 4 plain piece labels require unit pcs.'
    }
    if ($schemaVersion -ge 5 -and $plainTraceability -and $unit -cne 'pcs' -and $unit -cne 'box' -and $unit -cne 'tray') {
        throw 'Plain traceability labels require unit pcs, box, or tray.'
    }
    if (-not $plainTraceability -and -not $singleIngredient -and -not $ingredientText) { throw 'Ingredients are required.' }
    if (-not $plainTraceability -and -not $allergenText) { throw 'Allergen declaration is required.' }
    if (-not $nutritionText -and -not $nutritionExempt) { throw 'Nutrition declaration or documented exemption is required.' }
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
        Add-LabelText -Graphics $graphics -Text $displayName -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, $layout.title_height_px)) -MaximumFontPixels $layout.title_font_px -MinimumFontPixels 17 -Style Bold
        $y += $layout.title_height_px
        Add-LabelText -Graphics $graphics -Text $legalName -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, $layout.legal_name_height_px)) -MaximumFontPixels $layout.legal_name_font_px -MinimumFontPixels 9
        $y += $layout.legal_name_height_px

        if ($ingredientText -and (-not $singleIngredient -or $plainTraceability)) {
            $ingredients = 'Συστατικά: ' + $ingredientText
            Add-LabelText -Graphics $graphics -Text $ingredients -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, $layout.ingredients_height_px)) -MaximumFontPixels $layout.ingredients_font_px -MinimumFontPixels 9
            $y += $layout.ingredients_height_px
        }
        if ($allergenText) {
            $allergens = 'ΑΛΛΕΡΓΙΟΓΟΝΑ: ' + $allergenText
            Add-LabelText -Graphics $graphics -Text $allergens -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, $layout.allergens_height_px)) -MaximumFontPixels $layout.allergens_font_px -MinimumFontPixels 10 -Style Bold
            $y += $layout.allergens_height_px + $layout.allergens_gap_after_px
        }

        $nutritionHeight = 0
        if ($nutritionText) {
            $nutritionHeight = Add-NutritionTable -Graphics $graphics -Nutrition $nutritionText -Y $y -Layout $layout
        }
        if ($nutritionHeight -gt 0) { $y += $nutritionHeight + $layout.nutrition_gap_after_px }

        $dates = 'ΠΑΡΑΓΩΓΗ: {0}     ΑΝΑΛΩΣΗ ΕΩΣ: {1}' -f (Get-LabelText $Payload.traceability.production_date 16), (Get-LabelText $Payload.traceability.use_by_date 16)
        Add-LabelText -Graphics $graphics -Text $dates -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, $layout.dates_height_px)) -MaximumFontPixels $layout.dates_font_px -MinimumFontPixels 9 -Style Bold -NoWrap
        $y += $layout.dates_height_px
        $lotLine = 'LOT: {0}' -f (Get-LabelText $Payload.traceability.internal_lot 64)
        Add-LabelText -Graphics $graphics -Text $lotLine -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, $layout.lot_height_px)) -MaximumFontPixels $layout.lot_font_px -MinimumFontPixels 8 -NoWrap
        $y += $layout.lot_height_px
        $source = Get-LabelText $Payload.traceability.source_lot 96
        if ($source) {
            Add-LabelText -Graphics $graphics -Text ("ΠΑΡΤΙΔΑ ΠΗΓΗΣ: $source") -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, $layout.source_lot_height_px)) -MaximumFontPixels $layout.source_lot_font_px -MinimumFontPixels 8 -NoWrap
            $y += $layout.source_lot_height_px
        }
        Add-LabelText -Graphics $graphics -Text (Get-LabelText $Payload.storage 255) -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, $layout.storage_height_px)) -MaximumFontPixels $layout.storage_font_px -MinimumFontPixels 9 -Style Bold
        $y += $layout.storage_height_px
        $origin = 'ΠΡΟΕΛΕΥΣΗ: ' + (Get-LabelText $Payload.product.origin 255)
        Add-LabelText -Graphics $graphics -Text $origin -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, $layout.origin_height_px)) -MaximumFontPixels $layout.origin_font_px -MinimumFontPixels 8 -NoWrap
        $y += $layout.origin_height_px
        $usage = Get-LabelText $Payload.product.usage_instructions 500
        if ($usage) {
            Add-LabelText -Graphics $graphics -Text $usage -Rectangle (New-Object Drawing.RectangleF(14, $y, 372, $layout.usage_height_px)) -MaximumFontPixels $layout.usage_font_px -MinimumFontPixels 8
            $y += $layout.usage_height_px
        }
        if ($y -gt 449) { throw 'Dynamic label content does not fit the 50x70 layout.' }

        $separator = New-Object Drawing.Pen([Drawing.Color]::Black, 1)
        try { $graphics.DrawLine($separator, 14, 452, 386, 452) }
        finally { $separator.Dispose() }
        Add-LabelText -Graphics $graphics -Text $labelContent.footer_caption -Rectangle (New-Object Drawing.RectangleF(14, 456, 278, 18)) -MaximumFontPixels $layout.footer_caption_font_px -MinimumFontPixels 8 -NoWrap
        $hasCompanyLogo = Add-ApprovedCompanyLogo -Graphics $graphics -AssetId $labelContent.logo_asset_id
        $footerTextX = if ($hasCompanyLogo) { 72 } else { 14 }
        $footerTextWidth = if ($hasCompanyLogo) { 220 } else { 278 }
        Add-LabelText -Graphics $graphics -Text $labelContent.company_name -Rectangle (New-Object Drawing.RectangleF($footerTextX, 473, $footerTextWidth, 31)) -MaximumFontPixels $layout.footer_name_font_px -MinimumFontPixels 9 -Style Bold
        Add-LabelText -Graphics $graphics -Text $labelContent.company_address -Rectangle (New-Object Drawing.RectangleF($footerTextX, 503, $footerTextWidth, 43)) -MaximumFontPixels $layout.footer_address_font_px -MinimumFontPixels 8
        Add-ApprovalOval -Graphics $graphics -ApprovalNumber (Get-LabelText $Payload.business.approval_number 128) -Y 470 -Layout $layout
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
        $pixels = Convert-BitmapToMonochromeBytes -Bitmap $bitmap
    }
    finally { $bitmap.Dispose() }

    if ($PreviewPath) {
        Save-MonochromePreviewPng -Pixels $pixels -Width 400 -Height 560 -Path $PreviewPath
    }

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
