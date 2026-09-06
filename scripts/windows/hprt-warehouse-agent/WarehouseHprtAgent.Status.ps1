[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\ProgramData\Sklavounos\WarehouseHprtAgent',
    [string]$TaskName = 'Sklavounos Warehouse HPRT Agent',
    [switch]$RestartOnly,
    [switch]$SnapshotOnly
)

$ErrorActionPreference = 'Stop'

if ($RestartOnly) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    exit 0
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()

function Get-DisplayTime {
    param([AllowNull()][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return 'Δεν υπάρχει ακόμη' }
    try { return ([DateTimeOffset]::Parse($Value)).LocalDateTime.ToString('dd/MM/yyyy HH:mm:ss') }
    catch { return 'Καταγράφηκε' }
}

function Get-HprtPrintHistory {
    param([int]$Limit = 10)
    $path = Join-Path $InstallRoot 'print-history.jsonl'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return @() }
    $history = New-Object System.Collections.Generic.List[object]
    foreach ($line in @(Get-Content -LiteralPath $path -Encoding UTF8 -Tail 50 -ErrorAction SilentlyContinue)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try {
            $event = $line | ConvertFrom-Json -ErrorAction Stop
            $timestamp = [DateTimeOffset]::Parse([string]$event.timestamp, [Globalization.CultureInfo]::InvariantCulture)
            $profile = switch ([string]$event.profile) {
                'INTERNAL' { 'Εσωτερική 50×70' }
                'DISTRIBUTION' { 'Ενιαία 50×70' }
                default { 'Δυναμική ετικέτα' }
            }
            $history.Add([pscustomobject]@{
                Time = Get-DisplayTime -Value ([string]$event.timestamp)
                Timestamp = $timestamp.ToString('o')
                SortTimestampUtc = $timestamp.UtcDateTime.Ticks
                PrintSucceeded = ([string]$event.result -eq 'PRINTED')
                Job = "#$([int]$event.job_id)"
                Label = "$profile · $([string]$event.product)"
                Copies = [string]$event.copies
                Result = if ([string]$event.result -eq 'PRINTED') { 'Επιτυχία' } else { 'Έλεγχος' }
            })
        }
        catch { }
    }
    return @($history | Sort-Object SortTimestampUtc -Descending | Select-Object -First $Limit)
}

function Get-HprtLastPrintTime {
    param([AllowNull()][string]$StateTimestamp, [object[]]$History = @())
    $latest = $null
    foreach ($value in @($StateTimestamp) + @($History | Where-Object { $_.PrintSucceeded } | ForEach-Object { $_.Timestamp })) {
        if ([string]::IsNullOrWhiteSpace([string]$value)) { continue }
        try {
            $candidate = [DateTimeOffset]::Parse([string]$value, [Globalization.CultureInfo]::InvariantCulture)
            if ($null -eq $latest -or $candidate -gt $latest) { $latest = $candidate }
        }
        catch { }
    }
    if ($null -eq $latest) { return 'Δεν υπάρχει ακόμη' }
    return Get-DisplayTime -Value $latest.ToString('o')
}

function Get-HprtInstalledVersion {
    $path = Join-Path $InstallRoot 'PACKAGE-MANIFEST.json'
    try {
        $manifest = Get-Content -LiteralPath $path -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        $version = [string]$manifest.version
        if ($version -match '^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$') { return $version }
    }
    catch { }
    return 'Μη διαθέσιμη'
}

function Get-HprtStatusSnapshot {
    $configPath = Join-Path $InstallRoot 'config.json'
    $tokenPath = Join-Path $InstallRoot 'agent-token.dpapi'
    $agentPath = Join-Path $InstallRoot 'WarehouseHprtAgent.ps1'
    $statusPath = Join-Path $InstallRoot 'agent-status.json'
    $logPath = Join-Path $InstallRoot 'warehouse-hprt-agent.log'
    $diagnosticPath = Join-Path $InstallRoot 'Diagnose-WarehouseHprtAgent.ps1'

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue } else { $null }
    $config = $null
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        try { $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { $config = $null }
    }
    $status = $null
    if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
        try { $status = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { $status = $null }
    }

    $printerName = if ($config) { ([string]$config.printer_name).Trim() } else { '' }
    $printer = if ($printerName) { Get-Printer -Name $printerName -ErrorAction SilentlyContinue } else { $null }
    $installed = [bool](
        $config -and
        (Test-Path -LiteralPath $tokenPath -PathType Leaf) -and
        (Test-Path -LiteralPath $agentPath -PathType Leaf)
    )
    $taskRunning = [bool]($task -and [string]$task.State -eq 'Running')
    $lastContactValue = if ($status) { [string]$status.last_contact } else { '' }
    $lastContact = $null
    if ($lastContactValue) {
        try { $lastContact = [DateTimeOffset]::Parse($lastContactValue) }
        catch { $lastContact = $null }
    }
    $contactFresh = [bool]($lastContact -and ([DateTimeOffset]::Now - $lastContact).TotalSeconds -le 30)
    $state = if ($status) { [string]$status.state } else { 'STARTING' }
    $queueState = if ($status) { [string]$status.queue_state } else { 'STARTING' }
    $currentJob = if ($status -and $status.current_job_id) { "#$([int]$status.current_job_id)" } else { '—' }
    $history = @(Get-HprtPrintHistory -Limit 10)
    $lastPrint = Get-HprtLastPrintTime -StateTimestamp $(if ($status) { [string]$status.last_print } else { '' }) -History $history
    $lastError = if ($status -and -not [string]::IsNullOrWhiteSpace([string]$status.last_error)) {
        switch ([string]$status.last_error) {
            'LABEL_CONTENT_TOO_LARGE' { 'Το περιεχόμενο δεν χωρά στην ετικέτα 50×70' }
            'HPRT_PAYLOAD_TOO_LARGE' { 'Τα δεδομένα της ετικέτας είναι υπερβολικά μεγάλα' }
            'HPRT_PAYLOAD_INVALID' { 'Τα δεδομένα της ετικέτας δεν είναι έγκυρα' }
            'HPRT_PRINTER_NOT_FOUND' { 'Ο εκτυπωτής LABELS δεν βρέθηκε' }
            'HPRT_SPOOLER_FAILED' { 'Η ουρά εκτύπωσης των Windows δεν ξεκίνησε' }
            'HPRT_WRITE_INCOMPLETE' { 'Ο εκτυπωτής δεν δέχτηκε ολόκληρη την ετικέτα' }
            'HPRT_RENDER_FAILED' { 'Η δημιουργία της ετικέτας απέτυχε' }
            'HPRT_RUNTIME_MISSING' { 'Λείπει αρχείο του EFET Print Agent' }
            'HPRT_RUNTIME_FAILED' { 'Δεν ξεκίνησε ο μηχανισμός εκτύπωσης' }
            'HPRT_PRINT_FAILED' { 'Η εκτύπωση απέτυχε' }
            'COMPLETION_UNCONFIRMED' { 'Η επιβεβαίωση εκτύπωσης δεν ολοκληρώθηκε' }
            'CONNECTION_OR_RESPONSE' { 'Δεν υπάρχει επικοινωνία με το Warehouse' }
            default { 'Χρειάζεται έλεγχος' }
        }
    } else { 'Κανένα πρόσφατο σφάλμα' }
    if (-not $status -and (Test-Path -LiteralPath $logPath -PathType Leaf)) {
        $lastFailure = @(Get-Content -LiteralPath $logPath -Tail 100 -ErrorAction SilentlyContinue | Where-Object { $_ -match 'FAILED|UNCONFIRMED' } | Select-Object -Last 1)[0]
        if ($lastFailure) { $lastError = 'Υπάρχει καταγεγραμμένο σφάλμα — ανοίξτε τα διαγνωστικά' }
    }

    $queueText = switch ($queueState) {
        'ACTIVE' { 'Εκτυπώνει τώρα' }
        'WAITING' { 'Αναμονή νέας ετικέτας' }
        'ERROR' { 'Σταμάτησε για έλεγχο' }
        default { 'Εκκίνηση' }
    }

    $statusCode = 'STOPPED'
    $statusTitle = 'Ο EFET AGENT ΔΕΝ ΛΕΙΤΟΥΡΓΕΙ'
    $statusDetail = 'Πατήστε «Επανεκκίνηση Agent». Αν παραμείνει κόκκινος, ανοίξτε τα διαγνωστικά.'
    if (-not $installed) {
        $statusCode = 'NOT_INSTALLED'
        $statusTitle = 'ΔΕΝ ΕΧΕΙ ΕΓΚΑΤΑΣΤΑΘΕΙ'
        $statusDetail = 'Χρειάζεται να ολοκληρωθεί μία φορά το SETUP.'
    }
    elseif (-not $printer) {
        $statusCode = 'PRINTER_MISSING'
        $statusTitle = 'ΔΕΝ ΒΡΕΘΗΚΕ Ο ΕΚΤΥΠΩΤΗΣ'
        $statusDetail = "Ο Windows εκτυπωτής «$printerName» δεν είναι διαθέσιμος."
    }
    elseif ($taskRunning -and $contactFresh -and $state -eq 'PRINTING') {
        $statusCode = 'PRINTING'
        $statusTitle = 'ΕΚΤΥΠΩΝΕΙ ΤΩΡΑ'
        $statusDetail = "Η εργασία $currentJob στέλνεται στον HPRT LPQ80."
    }
    elseif ($taskRunning -and $contactFresh -and $state -eq 'CONNECTED') {
        $statusCode = 'HEALTHY'
        $statusTitle = 'Ο EFET AGENT ΛΕΙΤΟΥΡΓΕΙ ΚΑΝΟΝΙΚΑ'
        $statusDetail = 'Οι ετικέτες ιχνηλασιμότητας παρακολουθούνται και εκτυπώνονται αυτόματα.'
    }
    elseif ($taskRunning -and $state -eq 'ERROR') {
        $statusCode = 'ERROR'
        $statusTitle = 'ΧΡΕΙΑΖΕΤΑΙ ΕΛΕΓΧΟΣ'
        $statusDetail = $lastError
    }
    elseif ($taskRunning) {
        $statusCode = 'STARTING'
        $statusTitle = 'Ο EFET AGENT ΞΕΚΙΝΑ'
        $statusDetail = 'Η εργασία Windows λειτουργεί και περιμένουμε την πρώτη επικοινωνία.'
    }

    return [pscustomobject]@{
        Version = Get-HprtInstalledVersion
        StatusCode = $statusCode
        StatusTitle = $statusTitle
        StatusDetail = $statusDetail
        Printer = if ($printerName) { if ($printer) { "$printerName · HPRT LPQ80" } else { "$printerName · μη διαθέσιμος" } } else { 'Δεν έχει ρυθμιστεί' }
        AutoStart = if ($task) { 'Ενεργή · με τη σύνδεση στα Windows' } else { 'Δεν υπάρχει' }
        TaskState = if ($task) { [string]$task.State } else { 'Δεν υπάρχει' }
        LastContact = Get-DisplayTime -Value $lastContactValue
        Queue = $queueText
        CurrentJob = $currentJob
        LastPrint = $lastPrint
        LastError = $lastError
        LastTaskResult = if ($taskInfo) { [string]$taskInfo.LastTaskResult } else { '—' }
        PrintHistory = $history
        DiagnosticPath = $diagnosticPath
    }
}

if ($SnapshotOnly) {
    Get-HprtStatusSnapshot | ConvertTo-Json -Depth 5
    exit 0
}

$palette = @{
    Background = [Drawing.ColorTranslator]::FromHtml('#090C10')
    Header = [Drawing.ColorTranslator]::FromHtml('#0E131A')
    Card = [Drawing.ColorTranslator]::FromHtml('#141A22')
    Border = [Drawing.ColorTranslator]::FromHtml('#2A3442')
    Text = [Drawing.ColorTranslator]::FromHtml('#F4F7FB')
    Muted = [Drawing.ColorTranslator]::FromHtml('#9EABB9')
    Green = [Drawing.ColorTranslator]::FromHtml('#2DD4A3')
    Orange = [Drawing.ColorTranslator]::FromHtml('#F08A24')
    Red = [Drawing.ColorTranslator]::FromHtml('#F04438')
    Blue = [Drawing.ColorTranslator]::FromHtml('#3BA7FF')
}

$form = New-Object Windows.Forms.Form
$form.Text = 'EFET Print Agent WORKSHOP — Κατάσταση'
$form.StartPosition = 'CenterScreen'
$form.ClientSize = New-Object Drawing.Size(840, 835)
$form.MinimumSize = New-Object Drawing.Size(856, 874)
$form.BackColor = $palette.Background
$form.ForeColor = $palette.Text
$form.Font = New-Object Drawing.Font('Segoe UI', 10)

$header = New-Object Windows.Forms.Panel
$header.Location = New-Object Drawing.Point(0, 0)
$header.Size = New-Object Drawing.Size(840, 88)
$header.BackColor = $palette.Header
$header.Anchor = 'Top,Left,Right'
$form.Controls.Add($header)

$brand = New-Object Windows.Forms.Label
$brand.Text = 'SKLAVOUNOS ONE'
$brand.Location = New-Object Drawing.Point(28, 15)
$brand.Size = New-Object Drawing.Size(330, 18)
$brand.ForeColor = $palette.Orange
$brand.Font = New-Object Drawing.Font('Segoe UI Semibold', 9)
$header.Controls.Add($brand)

$creatorAsset = Join-Path $InstallRoot 'favicon-64.png'
if (Test-Path -LiteralPath $creatorAsset -PathType Leaf) {
    $creatorBytes = [IO.File]::ReadAllBytes($creatorAsset)
    $creatorStream = New-Object IO.MemoryStream(,$creatorBytes)
    try {
        $sourceImage = [Drawing.Image]::FromStream($creatorStream)
        try { $creatorImage = New-Object Drawing.Bitmap($sourceImage) }
        finally { $sourceImage.Dispose() }
    }
    finally { $creatorStream.Dispose() }
    $creatorMark = New-Object Windows.Forms.PictureBox
    $creatorMark.Location = New-Object Drawing.Point(530, 13)
    $creatorMark.Size = New-Object Drawing.Size(62, 62)
    $creatorMark.SizeMode = 'Zoom'
    $creatorMark.Image = $creatorImage
    $creatorMark.Anchor = 'Top,Right'
    $header.Controls.Add($creatorMark)
    $form.Add_FormClosed({ if ($null -ne $creatorMark.Image) { $creatorMark.Image.Dispose() } })
}

$title = New-Object Windows.Forms.Label
$title.Text = 'EFET PRINT AGENT · WORKSHOP'
$title.Location = New-Object Drawing.Point(26, 37)
$title.Size = New-Object Drawing.Size(530, 35)
$title.ForeColor = $palette.Text
$title.Font = New-Object Drawing.Font('Segoe UI Semibold', 20)
$header.Controls.Add($title)

$mediaBadge = New-Object Windows.Forms.Label
$mediaBadge.Text = 'HPRT · ΕΝΙΑΙΑ 50×70'
$mediaBadge.TextAlign = 'MiddleCenter'
$mediaBadge.Location = New-Object Drawing.Point(610, 24)
$mediaBadge.Size = New-Object Drawing.Size(200, 36)
$mediaBadge.BackColor = [Drawing.ColorTranslator]::FromHtml('#2B1D13')
$mediaBadge.ForeColor = $palette.Orange
$mediaBadge.Font = New-Object Drawing.Font('Segoe UI Semibold', 9)
$mediaBadge.Anchor = 'Top,Right'
$header.Controls.Add($mediaBadge)

$versionLabel = New-Object Windows.Forms.Label
$versionLabel.Location = New-Object Drawing.Point(610, 62)
$versionLabel.Size = New-Object Drawing.Size(200, 22)
$versionLabel.TextAlign = 'MiddleCenter'
$versionLabel.ForeColor = $palette.Muted
$versionLabel.Font = New-Object Drawing.Font('Segoe UI', 10)
$versionLabel.Anchor = 'Top,Right'
$header.Controls.Add($versionLabel)

$statusPanel = New-Object Windows.Forms.Panel
$statusPanel.Location = New-Object Drawing.Point(26, 108)
$statusPanel.Size = New-Object Drawing.Size(788, 116)
$statusPanel.BackColor = $palette.Card
$statusPanel.Anchor = 'Top,Left,Right'
$form.Controls.Add($statusPanel)

$statusDot = New-Object Windows.Forms.Label
$statusDot.Text = '●'
$statusDot.Location = New-Object Drawing.Point(22, 22)
$statusDot.Size = New-Object Drawing.Size(42, 48)
$statusDot.Font = New-Object Drawing.Font('Segoe UI', 25)
$statusPanel.Controls.Add($statusDot)

$statusTitle = New-Object Windows.Forms.Label
$statusTitle.Location = New-Object Drawing.Point(74, 21)
$statusTitle.Size = New-Object Drawing.Size(680, 31)
$statusTitle.Font = New-Object Drawing.Font('Segoe UI Semibold', 15)
$statusTitle.Anchor = 'Top,Left,Right'
$statusPanel.Controls.Add($statusTitle)

$statusDetail = New-Object Windows.Forms.Label
$statusDetail.Location = New-Object Drawing.Point(76, 56)
$statusDetail.Size = New-Object Drawing.Size(670, 45)
$statusDetail.Font = New-Object Drawing.Font('Segoe UI', 10)
$statusDetail.ForeColor = $palette.Muted
$statusDetail.Anchor = 'Top,Left,Right'
$statusPanel.Controls.Add($statusDetail)

$detailsPanel = New-Object Windows.Forms.Panel
$detailsPanel.Location = New-Object Drawing.Point(26, 242)
$detailsPanel.Size = New-Object Drawing.Size(788, 265)
$detailsPanel.BackColor = $palette.Card
$detailsPanel.Anchor = 'Top,Left,Right'
$form.Controls.Add($detailsPanel)

$detailsHeading = New-Object Windows.Forms.Label
$detailsHeading.Text = 'ΚΕΝΤΡΟ ΛΕΙΤΟΥΡΓΙΑΣ'
$detailsHeading.Location = New-Object Drawing.Point(22, 15)
$detailsHeading.Size = New-Object Drawing.Size(280, 22)
$detailsHeading.Font = New-Object Drawing.Font('Segoe UI Semibold', 10)
$detailsHeading.ForeColor = $palette.Orange
$detailsPanel.Controls.Add($detailsHeading)

$detailLabels = @{}
$rows = @(
    @('Printer', 'Εκτυπωτής'),
    @('AutoStart', 'Αυτόματη εκκίνηση'),
    @('TaskState', 'Εργασία Windows'),
    @('LastContact', 'Τελευταία επικοινωνία'),
    @('Queue', 'Ουρά εκτύπωσης'),
    @('CurrentJob', 'Τρέχουσα εργασία'),
    @('LastPrint', 'Τελευταία εκτύπωση'),
    @('LastError', 'Τελευταίο σφάλμα')
)
$rowY = 45
foreach ($row in $rows) {
    $caption = New-Object Windows.Forms.Label
    $caption.Text = $row[1]
    $caption.Location = New-Object Drawing.Point(24, $rowY)
    $caption.Size = New-Object Drawing.Size(230, 25)
    $caption.ForeColor = $palette.Muted
    $detailsPanel.Controls.Add($caption)

    $value = New-Object Windows.Forms.Label
    $value.Text = '—'
    $value.Location = New-Object Drawing.Point(260, $rowY)
    $value.Size = New-Object Drawing.Size(500, 25)
    $value.ForeColor = $palette.Text
    $value.Font = New-Object Drawing.Font('Segoe UI Semibold', 10)
    $value.AutoEllipsis = $true
    $value.Anchor = 'Top,Left,Right'
    $detailsPanel.Controls.Add($value)
    $detailLabels[$row[0]] = $value
    $rowY += 27
}

$historyPanel = New-Object Windows.Forms.Panel
$historyPanel.Location = New-Object Drawing.Point(26, 525)
$historyPanel.Size = New-Object Drawing.Size(788, 190)
$historyPanel.BackColor = $palette.Card
$historyPanel.Anchor = 'Top,Bottom,Left,Right'
$form.Controls.Add($historyPanel)

$historyHeading = New-Object Windows.Forms.Label
$historyHeading.Text = 'ΙΣΤΟΡΙΚΟ ΕΤΙΚΕΤΩΝ · ΤΕΛΕΥΤΑΙΕΣ 10'
$historyHeading.Location = New-Object Drawing.Point(22, 14)
$historyHeading.Size = New-Object Drawing.Size(430, 22)
$historyHeading.Font = New-Object Drawing.Font('Segoe UI Semibold', 10)
$historyHeading.ForeColor = $palette.Orange
$historyPanel.Controls.Add($historyHeading)

$historyGrid = New-Object Windows.Forms.DataGridView
$historyGrid.Location = New-Object Drawing.Point(22, 40)
$historyGrid.Size = New-Object Drawing.Size(744, 132)
$historyGrid.Anchor = 'Top,Bottom,Left,Right'
$historyGrid.BackgroundColor = $palette.Card
$historyGrid.BorderStyle = 'None'
$historyGrid.ReadOnly = $true
$historyGrid.AllowUserToAddRows = $false
$historyGrid.AllowUserToDeleteRows = $false
$historyGrid.AllowUserToResizeRows = $false
$historyGrid.RowHeadersVisible = $false
$historyGrid.AutoGenerateColumns = $false
$historyGrid.EnableHeadersVisualStyles = $false
$historyGrid.ColumnHeadersDefaultCellStyle.BackColor = $palette.Header
$historyGrid.ColumnHeadersDefaultCellStyle.ForeColor = $palette.Muted
$historyGrid.ColumnHeadersDefaultCellStyle.SelectionBackColor = $palette.Header
$historyGrid.DefaultCellStyle.BackColor = $palette.Card
$historyGrid.DefaultCellStyle.ForeColor = $palette.Text
$historyGrid.DefaultCellStyle.SelectionBackColor = [Drawing.ColorTranslator]::FromHtml('#40281A')
$historyGrid.DefaultCellStyle.SelectionForeColor = $palette.Text
$historyGrid.GridColor = $palette.Border
$historyGrid.CellBorderStyle = 'SingleHorizontal'
$historyGrid.SelectionMode = 'FullRowSelect'

$columns = @(
    @('Time', 'Ώρα', 160),
    @('Job', 'Job', 70),
    @('Label', 'Προϊόν / τύπος ετικέτας', 0),
    @('Copies', 'Αντίτυπα', 75),
    @('Result', 'Αποτέλεσμα', 90)
)
foreach ($columnData in $columns) {
    $column = New-Object Windows.Forms.DataGridViewTextBoxColumn
    $column.Name = $columnData[0]
    $column.HeaderText = $columnData[1]
    if ([int]$columnData[2] -eq 0) { $column.AutoSizeMode = 'Fill' }
    else { $column.Width = [int]$columnData[2] }
    [void]$historyGrid.Columns.Add($column)
}
$historyPanel.Controls.Add($historyGrid)

function New-ActionButton {
    param([string]$Text, [int]$X, [int]$Width, [Drawing.Color]$BorderColor, [Drawing.Color]$BackColor)
    $button = New-Object Windows.Forms.Button
    $button.Text = $Text
    $button.Location = New-Object Drawing.Point($X, 733)
    $button.Size = New-Object Drawing.Size($Width, 48)
    $button.FlatStyle = 'Flat'
    $button.FlatAppearance.BorderColor = $BorderColor
    $button.BackColor = $BackColor
    $button.ForeColor = $palette.Text
    $button.Anchor = 'Bottom,Left'
    $form.Controls.Add($button)
    return $button
}

$refreshButton = New-ActionButton -Text 'Ανανέωση' -X 26 -Width 150 -BorderColor $palette.Border -BackColor $palette.Card
$restartButton = New-ActionButton -Text 'Επανεκκίνηση Agent' -X 188 -Width 210 -BorderColor $palette.Orange -BackColor ([Drawing.ColorTranslator]::FromHtml('#40281A'))
$diagnosticButton = New-ActionButton -Text 'Άνοιγμα διαγνωστικών' -X 410 -Width 220 -BorderColor $palette.Border -BackColor $palette.Card
$closeButton = New-ActionButton -Text 'Κλείσιμο' -X 642 -Width 172 -BorderColor $palette.Border -BackColor $palette.Card
$closeButton.Anchor = 'Bottom,Right'
$closeButton.ForeColor = $palette.Muted

$updatedLabel = New-Object Windows.Forms.Label
$updatedLabel.Location = New-Object Drawing.Point(28, 795)
$updatedLabel.Size = New-Object Drawing.Size(380, 22)
$updatedLabel.ForeColor = $palette.Muted
$updatedLabel.Anchor = 'Bottom,Left,Right'
$form.Controls.Add($updatedLabel)

$creatorCredit = New-Object Windows.Forms.Button
$creatorCredit.Location = New-Object Drawing.Point(475, 785)
$creatorCredit.Size = New-Object Drawing.Size(338, 45)
$creatorCredit.Anchor = 'Bottom,Right'
$creatorCredit.FlatStyle = 'Flat'
$creatorCredit.FlatAppearance.BorderSize = 0
$creatorCredit.BackColor = $palette.Background
$creatorCredit.AccessibleName = 'RAW LOGIC. REAL SYSTEMS. — Created by Christos Fragoulis'
$creatorCredit.AccessibleDescription = 'Σχετικά με τον EFET Print Agent και την έκδοση εγκατάστασης'
$signaturePath = Join-Path $InstallRoot 'creator-signature.png'
if (Test-Path -LiteralPath $signaturePath -PathType Leaf) {
    $signatureStream = New-Object IO.MemoryStream(,[IO.File]::ReadAllBytes($signaturePath))
    try {
        $signatureSource = [Drawing.Image]::FromStream($signatureStream)
        try { $creatorCredit.BackgroundImage = New-Object Drawing.Bitmap($signatureSource) }
        finally { $signatureSource.Dispose() }
    }
    finally { $signatureStream.Dispose() }
    $creatorCredit.BackgroundImageLayout = 'Zoom'
    $form.Add_FormClosed({ if ($null -ne $creatorCredit.BackgroundImage) { $creatorCredit.BackgroundImage.Dispose() } })
}
else {
    $creatorCredit.Text = "RAW LOGIC. REAL SYSTEMS.`r`nCreated by Christos Fragoulis"
    $creatorCredit.Font = New-Object Drawing.Font('Segoe UI', 9)
}
$creatorCredit.Add_Click({
    $about = New-Object Windows.Forms.Form
    $about.Text = 'Σχετικά με τον EFET Print Agent'
    $about.ClientSize = New-Object Drawing.Size(440, 230)
    $about.StartPosition = 'CenterParent'
    $about.FormBorderStyle = 'FixedDialog'
    $about.MaximizeBox = $false
    $about.MinimizeBox = $false
    $about.BackColor = $palette.Background
    $about.ForeColor = $palette.Text
    $about.Font = New-Object Drawing.Font('Segoe UI', 10)
    $aboutTitle = New-Object Windows.Forms.Label
    $aboutTitle.Location = New-Object Drawing.Point(20, 18)
    $aboutTitle.Size = New-Object Drawing.Size(400, 55)
    $aboutTitle.Text = "EFET Print Agent · WORKSHOP`r`nΈκδοση εγκατάστασης: $(Get-HprtInstalledVersion)"
    $about.Controls.Add($aboutTitle)
    $aboutCredit = New-Object Windows.Forms.Label
    $aboutCredit.Location = New-Object Drawing.Point(20, 85)
    $aboutCredit.Size = New-Object Drawing.Size(400, 60)
    $aboutCredit.AccessibleName = $creatorCredit.AccessibleName
    if ($null -ne $creatorCredit.BackgroundImage) {
        $aboutCredit.BackgroundImage = $creatorCredit.BackgroundImage
        $aboutCredit.BackgroundImageLayout = 'Zoom'
    }
    else { $aboutCredit.Text = "RAW LOGIC. REAL SYSTEMS.`r`nCreated by Christos Fragoulis" }
    $about.Controls.Add($aboutCredit)
    $aboutUrl = New-Object Windows.Forms.Label
    $aboutUrl.Location = New-Object Drawing.Point(20, 153)
    $aboutUrl.Size = New-Object Drawing.Size(260, 25)
    $aboutUrl.Text = 'https://rawlogic.gr'
    $aboutUrl.ForeColor = $palette.Muted
    $about.Controls.Add($aboutUrl)
    $aboutClose = New-Object Windows.Forms.Button
    $aboutClose.Text = 'Κλείσιμο'
    $aboutClose.Location = New-Object Drawing.Point(300, 185)
    $aboutClose.Size = New-Object Drawing.Size(120, 32)
    $aboutClose.DialogResult = [Windows.Forms.DialogResult]::OK
    $about.Controls.Add($aboutClose)
    $about.AcceptButton = $aboutClose
    $about.CancelButton = $aboutClose
    try { [void]$about.ShowDialog($form) }
    finally { $aboutCredit.BackgroundImage = $null; $about.Dispose() }
})
$form.Controls.Add($creatorCredit)

$refreshAction = {
    try {
        $snapshot = Get-HprtStatusSnapshot
        $versionLabel.Text = "Έκδοση $($snapshot.Version)"
        $form.Text = "EFET Print Agent WORKSHOP — $($snapshot.Version) — Κατάσταση"
        $statusTitle.Text = $snapshot.StatusTitle
        $statusDetail.Text = $snapshot.StatusDetail
        $statusColor = switch ($snapshot.StatusCode) {
            'HEALTHY' { $palette.Green }
            'PRINTING' { $palette.Orange }
            'STARTING' { $palette.Blue }
            default { $palette.Red }
        }
        $statusDot.ForeColor = $statusColor
        $statusTitle.ForeColor = $statusColor
        foreach ($key in $detailLabels.Keys) { $detailLabels[$key].Text = [string]$snapshot.$key }
        $historyGrid.Rows.Clear()
        foreach ($entry in @($snapshot.PrintHistory)) {
            [void]$historyGrid.Rows.Add($entry.Time, $entry.Job, $entry.Label, $entry.Copies, $entry.Result)
        }
        if (@($snapshot.PrintHistory).Count -eq 0) {
            [void]$historyGrid.Rows.Add('—', '—', 'Οι επόμενες εκτυπώσεις θα εμφανιστούν εδώ.', '—', '—')
        }
        $diagnosticButton.Tag = $snapshot.DiagnosticPath
        $updatedLabel.Text = "Τελευταία ανανέωση: $((Get-Date).ToString('dd/MM/yyyy HH:mm:ss'))"
    }
    catch {
        $statusDot.ForeColor = $palette.Red
        $statusTitle.ForeColor = $palette.Red
        $statusTitle.Text = 'ΔΕΝ ΜΠΟΡΕΣΑ ΝΑ ΔΙΑΒΑΣΩ ΤΗΝ ΚΑΤΑΣΤΑΣΗ'
        $statusDetail.Text = $_.Exception.Message
    }
}

$refreshButton.Add_Click($refreshAction)
$closeButton.Add_Click({ $form.Close() })
$diagnosticButton.Add_Click({
    $path = [string]$diagnosticButton.Tag
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        [Windows.Forms.MessageBox]::Show('Τα διαγνωστικά δεν έχουν εγκατασταθεί.', 'EFET Print Agent') | Out-Null
        return
    }
    Start-Process -FilePath 'PowerShell.exe' -ArgumentList @(
        '-NoLogo', '-NoProfile', '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', "`"$path`""
    ) | Out-Null
})
$restartButton.Add_Click({
    $answer = [Windows.Forms.MessageBox]::Show(
        'Να γίνει επανεκκίνηση του EFET Print Agent;',
        'EFET Print Agent',
        [Windows.Forms.MessageBoxButtons]::YesNo,
        [Windows.Forms.MessageBoxIcon]::Question
    )
    if ($answer -ne [Windows.Forms.DialogResult]::Yes) { return }
    try {
        $arguments = @(
            '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', "`"$PSCommandPath`"", '-InstallRoot', "`"$InstallRoot`"",
            '-TaskName', "`"$TaskName`"", '-RestartOnly'
        )
        $process = Start-Process -FilePath 'PowerShell.exe' -Verb RunAs -Wait -PassThru -ArgumentList $arguments
        if ($process.ExitCode -ne 0) { throw "Η επανεκκίνηση επέστρεψε κωδικό $($process.ExitCode)." }
        Start-Sleep -Seconds 2
        & $refreshAction
    }
    catch {
        [Windows.Forms.MessageBox]::Show(
            "Η επανεκκίνηση δεν ολοκληρώθηκε.`r`n$($_.Exception.Message)",
            'EFET Print Agent',
            [Windows.Forms.MessageBoxButtons]::OK,
            [Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    }
})

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 5000
$timer.Add_Tick($refreshAction)
$form.Add_Shown({ & $refreshAction; $timer.Start() })
$form.Add_FormClosed({ $timer.Stop(); $timer.Dispose() })
[void]$form.ShowDialog()
