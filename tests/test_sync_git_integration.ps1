$ErrorActionPreference = 'Stop'
$SourceRepo = Split-Path -Parent $PSScriptRoot
$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("pinefield-sync-test-" + [guid]::NewGuid().ToString('N'))
$Remote = Join-Path $TempRoot 'remote.git'
$Seed = Join-Path $TempRoot 'seed'
$Client = Join-Path $TempRoot 'client'

try {
    New-Item -ItemType Directory -Path $TempRoot | Out-Null
    & git init --bare --quiet $Remote
    if ($LASTEXITCODE -ne 0) { throw 'git init bare failed' }
    & git clone --quiet $Remote $Seed
    if ($LASTEXITCODE -ne 0) { throw 'git clone seed failed' }
    & git -C $Seed config user.name 'ASIN Sync Test'
    & git -C $Seed config user.email 'asin-sync-test@example.invalid'
    New-Item -ItemType Directory -Path (Join-Path $Seed 'scripts'),(Join-Path $Seed 'data\account1') | Out-Null
    Copy-Item -LiteralPath (Join-Path $SourceRepo 'scripts\sync_asin_history1.ps1') -Destination (Join-Path $Seed 'scripts\sync_asin_history1.ps1')
    Copy-Item -LiteralPath (Join-Path $SourceRepo 'scripts\sync_asin_history.py') -Destination (Join-Path $Seed 'scripts\sync_asin_history.py')
    Copy-Item -LiteralPath (Join-Path $SourceRepo 'scripts\update_category_rotation.py') -Destination (Join-Path $Seed 'scripts\update_category_rotation.py')
    @'
categories:
  - name: C1
    url: https://www.amazon.co.jp/s?rh=n%3A1001
  - name: C2
    url: https://www.amazon.co.jp/s?rh=n%3A1002
'@ | Set-Content -LiteralPath (Join-Path $Seed 'categories1.yaml') -Encoding UTF8
    @'
{
  "schema": "note-amazon-asin-history-v1",
  "updated_at": "",
  "posted": []
}
'@ | Set-Content -LiteralPath (Join-Path $Seed 'data\account1\asin_history.json') -Encoding UTF8
    & git -C $Seed add .
    & git -C $Seed commit --quiet -m 'initial test repository'
    & git -C $Seed branch -M main
    & git -C $Seed push --quiet -u origin main
    if ($LASTEXITCODE -ne 0) { throw 'seed push failed' }
    & git --git-dir=$Remote symbolic-ref HEAD refs/heads/main
    if ($LASTEXITCODE -ne 0) { throw 'remote HEAD setup failed' }

    & git clone --quiet $Remote $Client
    if ($LASTEXITCODE -ne 0) { throw 'git clone client failed' }
    & git -C $Client config user.name 'ASIN Sync Test'
    & git -C $Client config user.email 'asin-sync-test@example.invalid'
    & git -C $Client switch --quiet -c feature/local-main-must-not-be-pushed
    'must never reach remote main' | Set-Content -LiteralPath (Join-Path $Client 'FEATURE_ONLY.txt') -Encoding UTF8
    & git -C $Client add FEATURE_ONLY.txt
    & git -C $Client commit --quiet -m 'feature-only marker'
    if ($LASTEXITCODE -ne 0) { throw 'feature marker commit failed' }

    $Results = Join-Path $TempRoot 'results.json'
    $Products = Join-Path $TempRoot 'products.json'
    @'
[
  {
    "asin": "B000000001",
    "status": "reserved",
    "reserved_at": "2026-08-01T07:00:00+09:00",
    "reserved_list_confirmed": true
  },
  {
    "asin": "B000000002",
    "status": "skipped_post_error",
    "publish_at": "2026-08-01T07:30:00+09:00"
  }
]
'@ | Set-Content -LiteralPath $Results -Encoding UTF8
    @'
[
  {"asin": "B000000001", "category": "C1#1"},
  {"asin": "B000000002", "category": "C2#1"}
]
'@ | Set-Content -LiteralPath $Products -Encoding UTF8

    $Sync = Join-Path $Client 'scripts\sync_asin_history1.ps1'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Sync -SourceJson $Results -ProductJson $Products -AccountId account1 -AccountName test -RequireCategory
    if ($LASTEXITCODE -ne 0) { throw 'first sync failed' }
    $FirstRemoteHead = (& git --git-dir=$Remote rev-parse refs/heads/main).Trim()
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & git --git-dir=$Remote cat-file -e 'refs/heads/main:FEATURE_ONLY.txt' 2>$null
    $featureLeakExit = $LASTEXITCODE
    $ErrorActionPreference = $savedPreference
    if ($featureLeakExit -eq 0) { throw 'feature branch content leaked into remote main' }
    $HistoryText = & git --git-dir=$Remote show 'refs/heads/main:data/account1/asin_history.json'
    $History = ($HistoryText -join "`n") | ConvertFrom-Json
    if (@($History.posted | Where-Object asin -eq 'B000000001').Count -ne 1) { throw 'confirmed ASIN missing remotely' }
    if (@($History.posted | Where-Object asin -eq 'B000000002').Count -ne 0) { throw 'skipped ASIN was recorded' }
    $RotationText = & git --git-dir=$Remote show 'refs/heads/main:data/account1/category_rotation.json'
    $Rotation = ($RotationText -join "`n") | ConvertFrom-Json
    if ($Rotation.last_asin -ne 'B000000001' -or $Rotation.next_category_position -ne 2) { throw 'rotation remote proof failed' }

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Sync -SourceJson $Results -ProductJson $Products -AccountId account1 -AccountName test -RequireCategory
    if ($LASTEXITCODE -ne 0) { throw 'idempotent sync failed' }
    $SecondRemoteHead = (& git --git-dir=$Remote rev-parse refs/heads/main).Trim()
    if ($FirstRemoteHead -ne $SecondRemoteHead) { throw 'idempotent sync created an unnecessary commit' }

    $BadResults = Join-Path $TempRoot 'unconfirmed.json'
    @'
[
  {
    "asin": "B000000003",
    "status": "reserved",
    "reserved_at": "2026-08-01T08:00:00+09:00",
    "reserved_list_confirmed": false
  }
]
'@ | Set-Content -LiteralPath $BadResults -Encoding UTF8
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Sync -SourceJson $BadResults -AccountId account1 -AccountName test
    if ($LASTEXITCODE -eq 0) { throw 'unconfirmed reservation unexpectedly succeeded' }
    $AfterBadHead = (& git --git-dir=$Remote rev-parse refs/heads/main).Trim()
    if ($AfterBadHead -ne $SecondRemoteHead) { throw 'failed sync changed remote main' }

    Write-Host 'Git integration test passed: confirmed-only, HEAD:main, remote proof, rotation, idempotence, failure safety.'
} finally {
    if (Test-Path -LiteralPath $TempRoot) {
        $resolvedTemp = [System.IO.Path]::GetFullPath($TempRoot)
        $systemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        if (-not $resolvedTemp.StartsWith($systemTemp, [System.StringComparison]::OrdinalIgnoreCase) -or
            -not ([System.IO.Path]::GetFileName($resolvedTemp)).StartsWith('pinefield-sync-test-')) {
            throw "refusing to remove unexpected test path: $resolvedTemp"
        }
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
    }
}
