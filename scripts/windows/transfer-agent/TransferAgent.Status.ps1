[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\ProgramData\RawLogic\TransferAgent'
)

$ErrorActionPreference = 'Stop'
$statusPath = Join-Path $InstallRoot 'agent-status.json'
Write-Host 'RAWLOGIC Transfer Agent'
if (-not (Test-Path -LiteralPath $statusPath -PathType Leaf)) {
    Write-Host 'Δεν υπάρχει ακόμα κατάσταση. Έλεγξε ότι η εργασία «RAWLOGIC Transfer Agent» τρέχει.'
    exit 1
}
$status = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Host ('Έκδοση: {0}' -f $status.version)
Write-Host ('Κατάσταση: {0}' -f $status.state)
Write-Host ('Τελευταία επαφή: {0}' -f $status.last_contact)
Write-Host ('Τελευταίο αρχείο: {0}' -f $status.last_file)
if ($status.last_error) { Write-Host ('Σφάλμα: {0}' -f $status.last_error) }
Write-Host 'Ο agent δεν σβήνει αρχεία και δεν γράφει στο Galaxy.'
