param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string[]]$SourceJson,

    [string[]]$ProductJson = @(),
    [string]$AccountId = 'account1',
    [string]$AccountName = 'account1',
    [string]$HistoryAccount = '',
    [switch]$RequireCategory,
    [switch]$ExternalSelection,
    [switch]$NoPush,
    [switch]$DryRun
)

# Merge only verified note reservations/posts, commit in an isolated worktree,
# push the current HEAD to origin/main, then prove the ASINs exist remotely.

$ErrorActionPreference = 'Stop'
if ($ExternalSelection -and $RequireCategory) {
    throw 'ExternalSelection and RequireCategory cannot be combined'
}
# The Node reservation runner consumes ASIN_SYNC_RESULT as UTF-8 JSON.
# Windows PowerShell 5.1 otherwise emits Japanese category names in CP932,
# which corrupts the success marker when the runner decodes stdout as UTF-8.
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ScriptRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ScriptRoot)) {
    $ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$RepoDir = Split-Path -Parent $ScriptRoot
if ([string]::IsNullOrWhiteSpace($HistoryAccount)) { $HistoryAccount = $AccountId }
if ($HistoryAccount -notmatch '^account([1-9]|1[0-9]|20)$') {
    throw "HistoryAccount must be account1..account20: $HistoryAccount"
}

function Resolve-InputFiles {
    param([string[]]$Paths, [string]$Label)
    $resolved = @()
    foreach ($item in $Paths) {
        if ([string]::IsNullOrWhiteSpace($item)) { continue }
        if (-not (Test-Path -LiteralPath $item -PathType Leaf)) {
            throw "$Label file not found: $item"
        }
        $resolved += (Resolve-Path -LiteralPath $item).Path
    }
    return @($resolved)
}

function Invoke-Git {
    param([string]$Directory, [string[]]$Arguments, [switch]$Capture)
    $previousErrorActionPreference = $ErrorActionPreference
    $previousConsoleOutputEncoding = [Console]::OutputEncoding
    try {
        # Native git writes harmless warnings (for example LF/CRLF notices) to
        # stderr. With the script-wide Stop policy those warnings became
        # terminating PowerShell errors before LASTEXITCODE could be checked.
        # Windows PowerShell 5.1 otherwise decodes UTF-8 JSON from `git show`
        # with the active OEM code page and corrupts Japanese category names.
        $ErrorActionPreference = 'Continue'
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        $value = & git -C $Directory @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        [Console]::OutputEncoding = $previousConsoleOutputEncoding
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) { throw "git $($Arguments -join ' ') failed: $value" }
    if ($Capture) { return ($value -join "`n") }
    foreach ($line in @($value)) { Write-Host $line }
}

function Invoke-CoreSync {
    param([string]$WorkingRepo, [string[]]$Sources, [string[]]$Products, [switch]$CoreDryRun)
    $core = Join-Path $WorkingRepo 'scripts\sync_asin_history.py'
    if (-not (Test-Path -LiteralPath $core -PathType Leaf)) { throw "sync core not found: $core" }
    $arguments = @(
        $core,
        '--repo-dir', $WorkingRepo,
        '--account', $HistoryAccount,
        '--account-name', $AccountName
    )
    foreach ($source in $Sources) { $arguments += @('--source-json', $source) }
    foreach ($product in $Products) { $arguments += @('--product-json', $product) }
    if ($RequireCategory) { $arguments += '--require-category' }
    if ($ExternalSelection) { $arguments += '--external-selection' }
    if ($CoreDryRun) { $arguments += '--dry-run' }
    $output = & python @arguments 2>&1
    $exitCode = $LASTEXITCODE
    foreach ($line in $output) { Write-Host $line }
    if ($exitCode -ne 0) { throw "ASIN sync validation failed (exit $exitCode)" }
    $marker = @($output | Where-Object { [string]$_ -like 'ASIN_SYNC_RESULT=*' } | Select-Object -Last 1)
    if ($marker.Count -ne 1) { throw 'ASIN sync result marker was not emitted' }
    return ([string]$marker[0]).Substring('ASIN_SYNC_RESULT='.Length) | ConvertFrom-Json
}

function Test-RemoteLedger {
    param([string]$BaseRepo, $Summary)
    if ($ExternalSelection -and (
        $Summary.external_selection -ne $true -or
        $Summary.rotation_applicable -ne $false -or
        $Summary.rotation_changed -ne $false -or
        @($Summary.rotation_matched_asins).Count -ne 0 -or
        $Summary.rotation_warning -or
        $Summary.rotation_skip_reason -ne 'external_selection'
    )) {
        throw 'external selection sync did not preserve the category cursor contract'
    }
    Invoke-Git -Directory $BaseRepo -Arguments @('fetch', '--quiet', 'origin', 'main')
    $historyRel = "data/$HistoryAccount/asin_history.json"
    $remoteText = Invoke-Git -Directory $BaseRepo -Arguments @('show', "origin/main:$historyRel") -Capture
    $remote = $remoteText | ConvertFrom-Json
    foreach ($event in @($Summary.accepted_events)) {
        $eventAsin = ([string]$event.asin).Trim().ToUpperInvariant()
        $eventDate = [string]$event.event_date
        $eventAccount = [string]$event.account_id
        $eventCategory = [string]$event.category
        $matches = @($remote.posted | Where-Object {
            $entry = $_
            $entryAsin = ([string]$entry.asin).Trim().ToUpperInvariant()
            $entryStatus = ([string]$entry.status).Trim().ToLowerInvariant()
            $entryDates = @()
            foreach ($name in @('posted_at', 'reserved_at')) {
                $match = [regex]::Match([string]$entry.$name, '^(\d{4}-\d{2}-\d{2})')
                if ($match.Success) { $entryDates += $match.Groups[1].Value }
            }
            $entryDate = if ($entryDates.Count -gt 0) { @($entryDates | Sort-Object)[-1] } else { '' }
            $entryAsin -eq $eventAsin -and
                $entryStatus -in @('posted', 'published', 'reserved', 'scheduled') -and
                ([string]$entry.account_id) -eq $eventAccount -and
                $entryDate -eq $eventDate -and
                ([string]::IsNullOrWhiteSpace($eventCategory) -or ([string]$entry.category) -eq $eventCategory)
        })
        if ($matches.Count -lt 1) {
            throw "remote event verification failed: $eventAsin|$eventDate|$eventAccount|$eventCategory"
        }
    }

    if (@($Summary.rotation_matched_asins).Count -gt 0 -and -not $Summary.rotation_warning) {
        $rotationRel = "data/$HistoryAccount/category_rotation.json"
        $remoteRotationText = Invoke-Git -Directory $BaseRepo -Arguments @('show', "origin/main:$rotationRel") -Capture
        $remoteRotation = $remoteRotationText | ConvertFrom-Json
        foreach ($name in @('last_asin', 'last_category_name', 'last_category_position', 'next_category_name', 'next_category_position', 'last_event_at')) {
            if ([string]$remoteRotation.$name -ne [string]$Summary.rotation_state.$name) {
                throw "remote category rotation verification failed: $name expected=$($Summary.rotation_state.$name) actual=$($remoteRotation.$name)"
            }
        }
    }
}

$sources = Resolve-InputFiles -Paths $SourceJson -Label 'SourceJson'
$products = Resolve-InputFiles -Paths $ProductJson -Label 'ProductJson'
if ($sources.Count -eq 0) { throw 'At least one SourceJson is required' }

if ($DryRun) {
    $summary = Invoke-CoreSync -WorkingRepo $RepoDir -Sources $sources -Products $products -CoreDryRun
    Write-Host ('ASIN_SYNC_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 20))
    exit 0
}

if ($NoPush) {
    $summary = Invoke-CoreSync -WorkingRepo $RepoDir -Sources $sources -Products $products
    $paths = @("data/$HistoryAccount/asin_history.json")
    $rotationPath = "data/$HistoryAccount/category_rotation.json"
    if (-not $ExternalSelection -and (Test-Path -LiteralPath (Join-Path $RepoDir $rotationPath))) { $paths += $rotationPath }
    Invoke-Git -Directory $RepoDir -Arguments (@('add', '--') + $paths)
    $staged = & git -C $RepoDir diff --staged --name-only
    if ($LASTEXITCODE -ne 0) { throw 'git staged inspection failed' }
    $unexpected = @($staged | Where-Object { $_ -notin $paths })
    if ($unexpected.Count -gt 0) { throw "unrelated staged files detected: $($unexpected -join ',')" }
    Write-Host ('ASIN_SYNC_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 20))
    exit 0
}

$mutexName = 'Global\PinefieldVerifiedAsinSync'
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$lockTaken = $false
try {
    $lockTaken = $mutex.WaitOne([TimeSpan]::FromMinutes(5))
    if (-not $lockTaken) { throw 'timed out waiting for ASIN sync lock' }

    $pushed = $false
    $lastError = $null
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("pinefield-asin-sync-" + [guid]::NewGuid().ToString('N'))
        try {
            Invoke-Git -Directory $RepoDir -Arguments @('fetch', '--quiet', 'origin', 'main')
            Invoke-Git -Directory $RepoDir -Arguments @('worktree', 'add', '--quiet', '--detach', $tempRoot, 'origin/main')
            $summary = Invoke-CoreSync -WorkingRepo $tempRoot -Sources $sources -Products $products
            $paths = @("data/$HistoryAccount/asin_history.json")
            $rotationPath = "data/$HistoryAccount/category_rotation.json"
            if (-not $ExternalSelection -and (Test-Path -LiteralPath (Join-Path $tempRoot $rotationPath))) { $paths += $rotationPath }
            Invoke-Git -Directory $tempRoot -Arguments (@('add', '--') + $paths)

            $staged = & git -C $tempRoot diff --staged --name-only
            if ($LASTEXITCODE -ne 0) { throw 'git staged inspection failed' }
            $unexpected = @($staged | Where-Object { $_ -notin $paths })
            if ($unexpected.Count -gt 0) { throw "unrelated staged files detected: $($unexpected -join ',')" }
            & git -C $tempRoot diff --staged --quiet
            $hasChanges = $LASTEXITCODE -ne 0
            if ($hasChanges) {
                $message = "ver6.0 Record verified ASINs $HistoryAccount " + (Get-Date).ToString('yyyy-MM-dd HH:mm')
                Invoke-Git -Directory $tempRoot -Arguments @('commit', '--quiet', '-m', $message)
                & git -C $tempRoot push origin HEAD:main
                if ($LASTEXITCODE -ne 0) { throw 'git push origin HEAD:main failed' }
            }

            Test-RemoteLedger -BaseRepo $RepoDir -Summary $summary
            $summary | Add-Member -NotePropertyName remote_verified -NotePropertyValue $true -Force
            $summary | Add-Member -NotePropertyName pushed -NotePropertyValue $hasChanges -Force
            Write-Host ('ASIN_SYNC_RESULT=' + ($summary | ConvertTo-Json -Compress -Depth 20))
            $pushed = $true
            break
        } catch {
            $lastError = $_
            Write-Warning "ASIN sync attempt $attempt failed: $($_.Exception.Message)"
            if ($_.Exception.Message -match 'ASIN_SYNC_ERROR=|validation failed|file not found|category config not found') {
                break
            }
        } finally {
            if (Test-Path -LiteralPath $tempRoot) {
                & git -C $RepoDir worktree remove --force $tempRoot 2>$null
                if (Test-Path -LiteralPath $tempRoot) {
                    $resolvedTemp = [System.IO.Path]::GetFullPath($tempRoot)
                    $systemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
                    if (-not $resolvedTemp.StartsWith($systemTemp, [System.StringComparison]::OrdinalIgnoreCase) -or
                        -not ([System.IO.Path]::GetFileName($resolvedTemp)).StartsWith('pinefield-asin-sync-')) {
                        throw "refusing to remove unexpected worktree path: $resolvedTemp"
                    }
                    Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
                }
            }
            & git -C $RepoDir worktree prune 2>$null
        }
        if (-not $pushed -and $attempt -lt 4) { Start-Sleep -Seconds 2 }
    }
    if (-not $pushed) { throw "ASIN sync failed after automatic attempts: $($lastError.Exception.Message)" }
} finally {
    if ($lockTaken) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
