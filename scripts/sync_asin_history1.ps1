param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string[]]$SourceJson,

    [string]$AccountId = 'account1',
    [string]$AccountName = 'account1',
    [string]$HistoryAccount = '',

    [switch]$NoPush,
    [switch]$DryRun
)

# ============================================================
# sync_asin_history1.ps1  (ver2.1, 2026-07-05)
# Merge note posting-result JSON(s) into pinefield's canonical
# data/<account>/asin_history.json, then commit & push so the
# pinefield GitHub-side scraper can exclude them.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\sync_asin_history1.ps1 -SourceJson "C:\path\posting_result.json" -AccountId account2 -AccountName "account2"
# ============================================================

$ErrorActionPreference = 'Stop'

$ScriptRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ScriptRoot)) {
    $ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$RepoDir = Split-Path -Parent $ScriptRoot
if ([string]::IsNullOrWhiteSpace($HistoryAccount)) {
    $HistoryAccount = $AccountId
}
if ($HistoryAccount -notmatch '^account[0-9]+$') {
    throw "HistoryAccount must be like account1/account2: $HistoryAccount"
}
$HistoryJson = Join-Path $RepoDir ("data\{0}\asin_history.json" -f $HistoryAccount)
$HistoryRel = "data/$HistoryAccount/asin_history.json"
$RotationJson = Join-Path $RepoDir ("data\{0}\category_rotation.json" -f $HistoryAccount)
$RotationRel = "data/$HistoryAccount/category_rotation.json"
$RotationScript = Join-Path $ScriptRoot 'update_category_rotation.py'

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "JSON file not found: $Path"
    }
    $raw = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($Path))
    $raw = $raw.TrimStart([char]0xFEFF).Trim()
    if (-not $raw) { throw "JSON file is empty: $Path" }
    return $raw | ConvertFrom-Json
}

function Get-ArrayItems {
    param($Json)
    if ($null -eq $Json) { return @() }
    if ($Json -is [System.Array]) { return @($Json) }
    foreach ($name in @('posted', 'results', 'items', 'articles', 'data')) {
        if ($Json.PSObject.Properties.Name -contains $name) { return @($Json.$name) }
    }
    return @($Json)
}

function Find-AsinInText {
    param($Value)
    if ($null -eq $Value) { return $null }
    $text = ([string]$Value).ToUpperInvariant()
    foreach ($pattern in @('(?:/DP/|/GP/PRODUCT/)([A-Z0-9]{10})', '[?&]ASIN=([A-Z0-9]{10})', '(?<![A-Z0-9])(B0[A-Z0-9]{8})(?![A-Z0-9])')) {
        $match = [regex]::Match($text, $pattern)
        if ($match.Success) { return $match.Groups[1].Value }
    }
    return $null
}

function Get-AsinFromObject {
    param($Item)
    foreach ($name in @('asin', 'ASIN', 'product_asin', 'productAsin')) {
        if ($Item.PSObject.Properties.Name -contains $name) {
            $value = ([string]$Item.$name).Trim().ToUpperInvariant()
            if ($value -match '^[A-Z0-9]{10}$') { return $value }
        }
    }
    foreach ($name in @('main_affiliate_url', 'affiliate_url', 'amazon_url', 'url', 'body', 'result_url', 'edit_url')) {
        if ($Item.PSObject.Properties.Name -contains $name) {
            $asin = Find-AsinInText $Item.$name
            if ($asin) { return $asin }
        }
    }
    foreach ($prop in $Item.PSObject.Properties) {
        if ($prop.Value -is [string]) {
            $asin = Find-AsinInText $prop.Value
            if ($asin) { return $asin }
        }
    }
    return $null
}

function Get-PropValue {
    param($Item, [string[]]$Names)
    foreach ($name in $Names) {
        if ($Item.PSObject.Properties.Name -contains $name) {
            $value = $Item.$name
            if ($null -ne $value -and -not [string]::IsNullOrWhiteSpace([string]$value)) { return [string]$value }
        }
    }
    return $null
}

function Get-DateText {
    param($Value)
    if ($null -eq $Value) { return $null }
    $text = ([string]$Value).Trim()
    if ($text -match '^(\d{4}-\d{2}-\d{2})') { return $Matches[1] }
    return $null
}

function Convert-RowToHistoryEntry {
    param($Row, [string]$SourcePath, [int]$Index)
    $asin = Get-AsinFromObject $Row
    if (-not $asin) { return $null }
    $status = Get-PropValue $Row @('status')
    $postedAt = Get-PropValue $Row @('posted_at', 'postedAt')
    $reservedAt = Get-PropValue $Row @('reserved_at', 'reservedAt', 'scheduled_at', 'scheduledAt')
    if (-not $reservedAt) {
        $date = Get-PropValue $Row @('note_schedule_date')
        $time = Get-PropValue $Row @('note_schedule_time')
        if ($date -and $time) { $reservedAt = "$date $time" }
    }
    if (-not $postedAt -and -not $reservedAt) { return $null }
    if (-not $status) { $status = if ($reservedAt) { 'scheduled' } else { 'posted' } }
    [PSCustomObject]@{
        asin = $asin
        title = Get-PropValue $Row @('title', 'product_title')
        status = $status
        posted_at = $postedAt
        reserved_at = $reservedAt
        account_id = $AccountId
        account_name = $AccountName
        note_url = Get-PropValue $Row @('result_url', 'note_url')
        edit_url = Get-PropValue $Row @('edit_url')
        thumbnail_path = Get-PropValue $Row @('thumbnail_path')
        source_file = (Resolve-Path -LiteralPath $SourcePath).Path
        source_index = $Index
    }
}

function Get-EntryKey {
    param($Entry)
    $dateText = Get-DateText $Entry.posted_at
    if (-not $dateText) { $dateText = Get-DateText $Entry.reserved_at }
    if (-not $dateText) { $dateText = 'unknown' }
    return ('{0}|{1}|{2}' -f $Entry.asin, $dateText, $Entry.account_id)
}

function Merge-Entry {
    param($Old, $New)
    foreach ($prop in @('title', 'status', 'posted_at', 'reserved_at', 'account_id', 'account_name', 'note_url', 'edit_url', 'thumbnail_path', 'source_file', 'source_index')) {
        if ($New.PSObject.Properties.Name -contains $prop) {
            $value = $New.$prop
            if ($null -ne $value -and -not [string]::IsNullOrWhiteSpace([string]$value)) { $Old.$prop = $value }
        }
    }
    return $Old
}

# ---- load existing history ----
$existingEntries = @()
if (Test-Path -LiteralPath $HistoryJson -PathType Leaf) {
    $history = Read-JsonFile -Path $HistoryJson
    $existingEntries = @(Get-ArrayItems $history)
}

$map = [ordered]@{}
foreach ($entry in $existingEntries) {
    $asin = Get-AsinFromObject $entry
    if (-not $asin) { continue }
    if (-not ($entry.PSObject.Properties.Name -contains 'asin')) {
        $entry | Add-Member -NotePropertyName asin -NotePropertyValue $asin
    }
    $map[(Get-EntryKey $entry)] = $entry
}

$added = 0; $updated = 0; $skipped = 0
$rotationAsins = [System.Collections.Generic.List[string]]::new()
foreach ($source in $SourceJson) {
    $json = Read-JsonFile -Path $source
    $rows = @(Get-ArrayItems $json)
    for ($i = 0; $i -lt $rows.Count; $i++) {
        $entry = Convert-RowToHistoryEntry -Row $rows[$i] -SourcePath $source -Index ($i + 1)
        if ($null -eq $entry) { $skipped++; continue }
        $rotationStatus = ([string]$entry.status).Trim().ToLowerInvariant()
        if ($rotationStatus -notin @('scraped', 'draft', 'failed', 'error', 'skipped')) {
            $rotationAsins.Add([string]$entry.asin)
        }
        $key = Get-EntryKey $entry
        if ($map.Contains($key)) { $map[$key] = Merge-Entry -Old $map[$key] -New $entry; $updated++ }
        else { $map[$key] = $entry; $added++ }
    }
}

$now = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
$posted = @(
    $map.Values |
        Sort-Object `
            @{ Expression = { $d = Get-DateText $_.posted_at; if (-not $d) { $d = Get-DateText $_.reserved_at }; if ($d) { $d } else { '9999-99-99' } } },
            @{ Expression = { $_.reserved_at } },
            @{ Expression = { $_.asin } }
)

$output = [PSCustomObject]@{
    schema = 'note-amazon-asin-history-v1'
    updated_at = $now
    description = "Single source of truth for $HistoryAccount ASIN exclusion (3-day rule enforced by scraper.py at scrape time). Update via scripts/sync_asin_history1.ps1 after posting."
    posted = $posted
}

$rotationArgs = @(
    $RotationScript,
    '--account', $HistoryAccount,
    '--repo-dir', $RepoDir
)
foreach ($asin in $rotationAsins) {
    $rotationArgs += @('--asin', $asin)
}
if ($DryRun) {
    $rotationArgs += '--dry-run'
}
& python @rotationArgs
if ($LASTEXITCODE -ne 0) { throw "category rotation update failed" }

if ($DryRun) {
    Write-Host ("[DryRun] added={0} updated={1} skipped={2} total={3}" -f $added, $updated, $skipped, $posted.Count)
    exit 0
}

ConvertTo-Json -InputObject $output -Depth 20 | Set-Content -LiteralPath $HistoryJson -Encoding UTF8
Write-Host ("[sync] history written: added={0} updated={1} skipped={2} total={3}" -f $added, $updated, $skipped, $posted.Count)

# ---- commit & push so the 00:01 JST scraper sees it ----
$gitPaths = @($HistoryRel)
if (Test-Path -LiteralPath $RotationJson -PathType Leaf) {
    $gitPaths += $RotationRel
}
& git -C $RepoDir add @gitPaths
if ($LASTEXITCODE -ne 0) { throw "git add failed" }
& git -C $RepoDir diff --staged --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "[sync] no change to commit."
    exit 0
}
$msg = "Update ASIN history $HistoryAccount " + (Get-Date).ToString('yyyy-MM-dd HH:mm')
& git -C $RepoDir commit -m $msg
if ($LASTEXITCODE -ne 0) { throw "git commit failed" }

if ($NoPush) {
    Write-Host "[sync] committed locally (NoPush). Remember to push before 00:01 JST."
    exit 0
}
$pushed = $false
for ($i = 1; $i -le 3; $i++) {
    & git -C $RepoDir push origin main
    if ($LASTEXITCODE -eq 0) { $pushed = $true; break }
    Write-Host ("[sync] push failed (attempt {0}), rebasing..." -f $i)
    & git -C $RepoDir pull --rebase --autostash origin main
    Start-Sleep -Seconds 5
}
if (-not $pushed) { throw "git push failed after retries" }
Write-Host "[sync] pushed. Scraper will exclude these ASINs from the next run."
