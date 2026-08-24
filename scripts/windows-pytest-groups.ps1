param(
    [ValidateSet('agent', 'domain', 'knowledge', 'proposals', 'misc')]
    [string]$Group,
    [Parameter(Mandatory = $true)]
    [string]$ResultDir,
    [switch]$Aggregate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
New-Item -ItemType Directory -Force -Path $ResultDir | Out-Null

function Get-NodeIds([object[]]$Output) {
    @($Output | ForEach-Object {
        $line = ([string]$_).Trim()
        if ($line -match '^(tests[\\/].+::.+)$') { $Matches[1].Replace('/', '\') }
    } | Where-Object { $_ })
}

function Get-OrdinalDuplicates([string[]]$Values) {
    $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $duplicates = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($value in $Values) {
        if (-not $seen.Add($value)) { [void]$duplicates.Add($value) }
    }
    @($duplicates)
}

function Test-OrdinalSetEqual([string[]]$Left, [string[]]$Right) {
    $values = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($value in $Left) { [void]$values.Add($value) }
    $values.SetEquals($Right)
}

function Get-TestFiles {
    @(
        Get-ChildItem -Path (Join-Path $repoRoot 'tests') -Recurse -File -Filter 'test_*.py' |
            ForEach-Object { $_.FullName.Substring($repoRoot.Length + 1).Replace('/', '\') } |
            Sort-Object -Unique
    )
}

function Get-Groups([string[]]$TestFiles) {
    $groups = [ordered]@{
        agent = @($TestFiles | Where-Object { $_ -match '\\test_(ai_|chat_|config|settings_api|auth_api|cli)' })
        domain = @($TestFiles | Where-Object { $_ -match '\\test_(applications|events|notes|resumes|offers|questions|jd_resume_ai_api|module_workflows)' })
        knowledge = @($TestFiles | Where-Object { $_ -match '\\test_(knowledge|ki)' })
        proposals = @($TestFiles | Where-Object { $_ -match '\\test_(opportunity_fit|interview|material|evidence|smoke)' })
        misc = @()
    }
    $assigned = @($groups.Values | ForEach-Object { $_ })
    $groups.misc = @($TestFiles | Where-Object { $assigned -notcontains $_ })
    $assigned = @($groups.Values | ForEach-Object { $_ })
    $unassigned = @($TestFiles | Where-Object { $assigned -notcontains $_ })
    if ($unassigned.Count -gt 0) { throw "unassigned test files: $($unassigned -join ', ')" }
    $groups
}

function Get-AllowedSkips {
    $reason = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String('5b2T5YmN546v5aKD5rKh5pyJ5Yib5bu656ym5Y+36ZO+5o6l55qE5p2D6ZmQ')
    )
    $allowed = [System.Collections.Generic.Dictionary[string,string]]::new(
        [StringComparer]::Ordinal
    )
    $allowed.Add(
        'tests\test_knowledge_ingest_integrity.py::test_failed_commit_cleanup_does_not_follow_symlink',
        $reason
    )
    $allowed.Add(
        'tests\test_knowledge_reset.py::test_cli_rejects_knowledge_root_symlink_with_external_sentinels',
        $reason
    )
    $allowed.Add(
        'tests\test_knowledge_reset.py::test_cli_rejects_legacy_reset_root_symlink_with_external_sentinels',
        $reason
    )
    $allowed.Add(
        'tests\test_knowledge_reset.py::test_cli_does_not_follow_nested_escape_symlink',
        $reason
    )
    return ,$allowed
}

function Get-SkipsFromJunit([string]$Path) {
    [xml]$report = Get-Content -LiteralPath $Path -Raw -Encoding utf8
    foreach ($testcase in @($report.testsuites.testsuite.testcase)) {
        $skippedProperty = $testcase.PSObject.Properties['skipped']
        if ($null -eq $skippedProperty -or $null -eq $skippedProperty.Value) { continue }
        $module = [string]$testcase.classname
        if ($module -notmatch '^tests\.(.+)$') { throw "cannot map JUnit testcase module: $module" }
        $relative = $Matches[1].Replace('.', '\')
        [pscustomobject]@{
            NodeId = "tests\$relative.py::$([string]$testcase.name)"
            Reason = [string]$testcase.skipped.message
        }
    }
}

function Assert-AllowedSkips([string]$Path, [string]$Name) {
    $allowed = Get-AllowedSkips
    foreach ($skip in @(Get-SkipsFromJunit $Path)) {
        if (
            -not $allowed.ContainsKey($skip.NodeId) -or
            -not [string]::Equals(
                $allowed[$skip.NodeId],
                $skip.Reason,
                [StringComparison]::Ordinal
            )
        ) {
            throw "$Name has an unexpected skip: $($skip.NodeId) [$($skip.Reason)]"
        }
    }
}

function Get-Sha256([string]$Path) {
    $stream = [IO.File]::OpenRead([IO.Path]::GetFullPath($Path))
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream)) -replace '-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Get-JunitSummary([string]$Path) {
    [xml]$report = Get-Content -LiteralPath $Path -Raw -Encoding utf8
    $suite = @($report.testsuites.testsuite) | Select-Object -First 1
    if ($null -eq $suite) { throw "JUnit report has no testsuite: $Path" }
    [pscustomobject]@{
        tests = [int]$suite.tests
        failures = [int]$suite.failures
        errors = [int]$suite.errors
        skipped = [int]$suite.skipped
    }
}

function Invoke-Group([string]$Name, [string[]]$Files) {
    if ($Files.Count -eq 0) { throw "pytest group $Name has no files" }
    $collectPath = Join-Path $ResultDir "$Name.collect.txt"
    $junitPath = Join-Path $ResultDir "$Name.junit.xml"
    $runPath = Join-Path $ResultDir "$Name.run.txt"
    $markerPath = Join-Path $ResultDir "$Name.complete.json"
    if (Test-Path -LiteralPath $markerPath) {
        Remove-Item -LiteralPath $markerPath -Force
    }
    $collectOutput = @(& uv run pytest --collect-only -q --disable-warnings @Files 2>&1 | Tee-Object -FilePath $collectPath)
    $collectExit = $LASTEXITCODE
    if ($collectExit -ne 0) { throw "$Name collection failed with exit code $collectExit" }
    $nodes = @(Get-NodeIds $collectOutput)
    $duplicates = @(Get-OrdinalDuplicates $nodes)
    if ($duplicates.Count -gt 0) { throw "$Name collection contains duplicate node ids: $($duplicates -join ', ')" }
    if ($nodes.Count -eq 0) { throw "$Name collection returned no tests" }

    $null = & uv run pytest -q -rs --disable-warnings "--junitxml=$junitPath" @Files 2>&1 | Tee-Object -FilePath $runPath
    $runExit = $LASTEXITCODE
    if (-not (Test-Path -LiteralPath $junitPath)) { throw "$Name did not produce JUnit" }
    Assert-AllowedSkips $junitPath $Name
    $summary = Get-JunitSummary $junitPath
    if ($runExit -ne 0) { throw "$Name pytest failed with exit code $runExit" }
    $marker = [ordered]@{
        marker_version = 1
        status = 'completed'
        group = $Name
        exit_code = 0
        collected_count = [int]$nodes.Count
        test_count = $summary.tests
        failures = $summary.failures
        errors = $summary.errors
        skipped = $summary.skipped
        collect_sha256 = Get-Sha256 $collectPath
        junit_sha256 = Get-Sha256 $junitPath
    }
    $marker | ConvertTo-Json | Set-Content -LiteralPath $markerPath -Encoding utf8
    Write-Host "$Name completed: $($nodes.Count) collected, $($summary.tests) tests, $($summary.skipped) allowed skips"
}

function Invoke-Aggregate {
    $manifestPath = Join-Path $ResultDir 'full-manifest.txt'
    if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'full-manifest.txt is missing' }
    $manifest = @(Get-NodeIds (Get-Content -LiteralPath $manifestPath -Encoding utf8))
    $manifestDuplicates = @(Get-OrdinalDuplicates $manifest)
    if ($manifestDuplicates.Count -gt 0) { throw 'full manifest contains duplicate node ids' }
    $all = [System.Collections.Generic.List[string]]::new()
    foreach ($name in @('agent', 'domain', 'knowledge', 'proposals', 'misc')) {
        $collectPath = Join-Path $ResultDir "$name.collect.txt"
        $junitPath = Join-Path $ResultDir "$name.junit.xml"
        $markerPath = Join-Path $ResultDir "$name.complete.json"
        foreach ($path in @($collectPath, $junitPath)) {
            if (-not (Test-Path -LiteralPath $path)) { throw "$name result is missing: $path" }
        }
        if (-not (Test-Path -LiteralPath $markerPath)) {
            throw "$name completion marker is missing: $markerPath"
        }
        $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding utf8 | ConvertFrom-Json
        if ($marker.marker_version -ne 1 -or $marker.status -ne 'completed' -or $marker.group -ne $name -or [int]$marker.exit_code -ne 0) {
            throw "$name completion marker is not successful"
        }
        if ($marker.collect_sha256 -ne (Get-Sha256 $collectPath) -or $marker.junit_sha256 -ne (Get-Sha256 $junitPath)) {
            throw "$name completion marker does not match persisted results"
        }
        $nodes = @(Get-NodeIds (Get-Content -LiteralPath $collectPath -Encoding utf8))
        $duplicates = @(Get-OrdinalDuplicates $nodes)
        if ($duplicates.Count -gt 0) { throw "$name aggregate input contains duplicate node ids" }
        if ([int]$marker.collected_count -ne $nodes.Count) { throw "$name collected count mismatches marker" }
        $summary = Get-JunitSummary $junitPath
        Assert-AllowedSkips $junitPath $name
        if ([int]$marker.test_count -ne $summary.tests) { throw "$name test count mismatches marker" }
        if ([int]$marker.failures -ne $summary.failures) { throw "$name failure count mismatches marker" }
        if ([int]$marker.errors -ne $summary.errors) { throw "$name error count mismatches marker" }
        if ([int]$marker.skipped -ne $summary.skipped) { throw "$name skip count mismatches marker" }
        foreach ($node in $nodes) { $all.Add($node) }
    }
    $duplicates = @(Get-OrdinalDuplicates @($all))
    if ($duplicates.Count -gt 0) { throw "pytest group coverage contains duplicate node ids: $($duplicates -join ', ')" }
    if (-not (Test-OrdinalSetEqual $manifest @($all))) {
        throw 'pytest group coverage differs from full manifest'
    }
    Write-Host "All pytest groups passed; coverage matches $($manifest.Count) tests."
}

if ($Aggregate) {
    Invoke-Aggregate
} else {
    if (-not $Group) { throw '-Group is required unless -Aggregate is used' }
    $groups = Get-Groups (Get-TestFiles)
    Invoke-Group $Group @($groups[$Group])
}
