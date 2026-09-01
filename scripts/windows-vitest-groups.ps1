param(
    [ValidateSet('components-core', 'components-chat', 'components-interview', 'components-offer', 'components-support', 'features', 'layout', 'lib', 'services', 'theme')]
    [string]$Group,
    [Parameter(Mandatory = $true)]
    [string]$ResultDir,
    [string]$RepositoryRoot,
    [switch]$Collect,
    [switch]$Aggregate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    Split-Path -Parent $PSScriptRoot
} else {
    [System.IO.Path]::GetFullPath($RepositoryRoot)
}
$repoRootVolume = [System.IO.Path]::GetPathRoot($repoRoot)
if ($repoRoot.Length -gt $repoRootVolume.Length) {
    $repoRoot = $repoRoot.TrimEnd('\', '/')
}
$webRoot = Join-Path $repoRoot 'web'
$resolvedResultDir = [System.IO.Path]::GetFullPath($ResultDir)
New-Item -ItemType Directory -Force -Path $resolvedResultDir | Out-Null
Set-Location $webRoot

$groupNames = @('components-core', 'components-chat', 'components-interview', 'components-offer', 'components-support', 'features', 'layout', 'lib', 'services', 'theme')
$manifestPath = Join-Path $resolvedResultDir 'frontend-manifest.json'
$aggregatePath = Join-Path $resolvedResultDir 'frontend.aggregate.json'

if (Test-Path -LiteralPath $aggregatePath) {
    Remove-Item -LiteralPath $aggregatePath -Force
}

function Get-TestFiles {
    @(
        Get-ChildItem -Path (Join-Path $webRoot 'src') -Recurse -File |
            Where-Object { $_.Name -match '\.(test|spec)\.[cm]?[jt]sx?$' } |
            ForEach-Object {
                Get-RelativePathWithinRoot $_.FullName $webRoot
            } |
            Sort-Object
    )
}

function Get-GroupForFile([string]$File) {
    $parts = $File.Replace('/', '\').Split('\')
    if ($parts.Count -lt 3 -or $parts[0] -ne 'src') {
        throw "Frontend test is outside src/<group>: $File"
    }
    $normalized = $File.Replace('/', '\').ToLowerInvariant()
    if ($normalized.StartsWith('src\components\chatpanel\')) {
        $group = 'components-chat'
    } elseif ($normalized.StartsWith('src\components\k')) {
        $group = 'components-support'
    } elseif ($normalized.StartsWith('src\components\authgate\') -or $normalized.StartsWith('src\components\ui\')) {
        $group = 'components-support'
    } elseif ($normalized -match '^src\\components\\.*(offer|negotiation)') {
        $group = 'components-offer'
    } elseif ($normalized -match '^src\\components\\.*(interview|mock|reviewform)') {
        $group = 'components-interview'
    } elseif ($parts[1] -eq 'components') {
        $group = 'components-core'
    } else {
        $group = $parts[1]
    }
    if ($groupNames -notcontains $group) {
        throw "Frontend test belongs to an unconfigured group: $File"
    }
    return $group
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

function Get-TextSha256([string]$Value) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

function Get-RelativePathWithinRoot([string]$Path, [string]$Root) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $fullRoot = [System.IO.Path]::GetFullPath($Root)
    $rootVolume = [System.IO.Path]::GetPathRoot($fullRoot)
    if ($fullRoot.Length -gt $rootVolume.Length) {
        $fullRoot = $fullRoot.TrimEnd('\', '/')
    }
    $rootPrefix = $fullRoot
    if (-not ($rootPrefix.EndsWith('\') -or $rootPrefix.EndsWith('/'))) {
        $rootPrefix += '\'
    }
    if (-not $fullPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside repository root: $Path"
    }
    $fullPath.Substring($rootPrefix.Length).Replace('/', '\\')
}

function Get-RepoRelativePath([string]$Path) {
    Get-RelativePathWithinRoot $Path $repoRoot
}

function Get-FingerprintFiles {
    $files = @(
        Get-ChildItem -Path (Join-Path $webRoot 'src') -Recurse -File |
            ForEach-Object { Get-RepoRelativePath $_.FullName }
    )
    $webMetadataNames = @(
        'package.json',
        'package-lock.json',
        'pnpm-lock.yaml',
        'yarn.lock',
        'bun.lockb',
        'tsconfig.json',
        'tsconfig.node.json'
    )
    $files += @(
        Get-ChildItem -Path $webRoot -File -Force |
            Where-Object {
                $webMetadataNames -contains $_.Name -or
                $_.Name -match '(^|\.)(config|lock)(\.|$)' -or
                $_.Name -like 'tsconfig*.json'
            } |
            ForEach-Object { Get-RepoRelativePath $_.FullName }
    )
    $files += Get-RepoRelativePath $PSCommandPath
    @($files | Sort-Object -Unique)
}

function Get-SourceHash([string[]]$Files) {
    $lines = @($Files | Sort-Object | ForEach-Object {
        $path = Join-Path $repoRoot $_
        "$_|$(Get-Sha256 $path)"
    })
    Get-TextSha256 ($lines -join "`n")
}

function Write-Manifest {
    $files = @(Get-TestFiles)
    if ($files.Count -eq 0) { throw 'No frontend test files were collected' }
    $duplicates = @($files | Group-Object | Where-Object Count -gt 1)
    if ($duplicates.Count -gt 0) {
        throw "Frontend collection contains duplicate files: $($duplicates.Name -join ', ')"
    }
    $groups = [ordered]@{}
    foreach ($name in $groupNames) {
        $groups[$name] = @($files | Where-Object { (Get-GroupForFile $_) -eq $name })
        if (@($groups[$name]).Count -eq 0) { throw "Frontend group $name has no test files" }
    }
    $fingerprintFiles = @(Get-FingerprintFiles)
    if ($fingerprintFiles.Count -eq 0) { throw 'No frontend fingerprint files were found' }
    [ordered]@{
        manifest_version = 1
        collected_at_utc = [DateTime]::UtcNow.ToString('o')
        file_count = $files.Count
        fingerprint_file_count = $fingerprintFiles.Count
        fingerprint_files = $fingerprintFiles
        source_hash = Get-SourceHash $fingerprintFiles
        files = $files
        groups = $groups
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding utf8
    Write-Host "Frontend manifest collected: $($files.Count) test files"
}

function Get-Manifest {
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        throw "Frontend manifest is missing: $manifestPath"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    if ($manifest.manifest_version -ne 1) {
        throw 'Unsupported frontend manifest version'
    }
    $files = @($manifest.files | ForEach-Object { [string]$_ })
    $duplicates = @($files | Group-Object | Where-Object Count -gt 1)
    if ($duplicates.Count -gt 0) {
        throw "Frontend manifest contains duplicate files: $($duplicates.Name -join ', ')"
    }
    if ([int]$manifest.file_count -ne $files.Count) {
        throw 'Frontend manifest file_count does not match files'
    }
    $currentFiles = @(Get-TestFiles)
    $testFileDifferences = @(Compare-Object -ReferenceObject @($files | Sort-Object) -DifferenceObject @($currentFiles | Sort-Object))
    if ($testFileDifferences.Count -gt 0) {
        $differenceNames = @($testFileDifferences | ForEach-Object { [string]$_.InputObject })
        throw "Frontend test file set changed since collection: $($differenceNames -join ', ')"
    }
    $fingerprintFiles = @($manifest.fingerprint_files | ForEach-Object { [string]$_ })
    if ([int]$manifest.fingerprint_file_count -ne $fingerprintFiles.Count) {
        throw 'Frontend manifest fingerprint_file_count does not match fingerprint_files'
    }
    $fingerprintDuplicates = @($fingerprintFiles | Group-Object | Where-Object Count -gt 1)
    if ($fingerprintDuplicates.Count -gt 0) {
        throw "Frontend manifest contains duplicate fingerprint files: $($fingerprintDuplicates.Name -join ', ')"
    }
    $currentFingerprintFiles = @(Get-FingerprintFiles)
    $fingerprintDifferences = @(Compare-Object -ReferenceObject @($fingerprintFiles | Sort-Object) -DifferenceObject @($currentFingerprintFiles | Sort-Object))
    if ($fingerprintDifferences.Count -gt 0) {
        $differenceNames = @($fingerprintDifferences | ForEach-Object { [string]$_.InputObject })
        throw "Frontend fingerprint file set changed since collection: $($differenceNames -join ', ')"
    }
    if ([string]::IsNullOrWhiteSpace([string]$manifest.source_hash)) {
        throw 'Frontend manifest source_hash is missing'
    }
    if ([string]$manifest.source_hash -ne (Get-SourceHash $currentFingerprintFiles)) {
        throw 'Frontend manifest source_hash does not match frontend sources or gate configuration'
    }
    [pscustomobject]@{
        file_count = [int]$manifest.file_count
        fingerprint_file_count = [int]$manifest.fingerprint_file_count
        source_hash = [string]$manifest.source_hash
        files = $files
    }
}

function Get-ReportFiles([object]$Report) {
    @($Report.testResults | ForEach-Object {
        $resultPath = [System.IO.Path]::GetFullPath([string]$_.name)
        Get-RelativePathWithinRoot $resultPath $webRoot
    })
}

function Get-AssertionRecords([object]$Report, [string[]]$ExpectedFiles) {
    $expected = @{}
    foreach ($file in $ExpectedFiles) { $expected[$file] = $true }
    $actualFiles = @(Get-ReportFiles $Report)
    $actualDuplicates = @($actualFiles | Group-Object | Where-Object Count -gt 1)
    if ($actualDuplicates.Count -gt 0) {
        throw "Vitest returned duplicate test files: $($actualDuplicates.Name -join ', ')"
    }
    $fileDifferences = @(Compare-Object -ReferenceObject @($ExpectedFiles | Sort-Object) -DifferenceObject @($actualFiles | Sort-Object))
    if ($fileDifferences.Count -gt 0) {
        $differenceNames = @($fileDifferences | ForEach-Object { [string]$_.InputObject })
        throw "Vitest file set does not match expected files: $($differenceNames -join ', ')"
    }
    $records = @()
    foreach ($result in @($Report.testResults)) {
        $resultPath = [System.IO.Path]::GetFullPath([string]$result.name)
        $relative = $resultPath.Substring($webRoot.Length + 1).Replace('/', '\')
        if (-not $expected.ContainsKey($relative)) {
            throw "Vitest returned an unexpected test file: $relative"
        }
        foreach ($assertion in @($result.assertionResults)) {
            $fullName = [string]$assertion.fullName
            if ([string]::IsNullOrWhiteSpace($fullName)) { throw "Vitest returned an unnamed assertion in $relative" }
            $records += [pscustomobject]@{
                id = "$relative::$fullName"
                file = $relative
                full_name = $fullName
                status = [string]$assertion.status
            }
        }
    }
    return $records
}

function Invoke-FrontendGroup([string]$Name) {
    $manifest = Get-Manifest
    $files = @($manifest.files | Where-Object { (Get-GroupForFile $_) -eq $Name })
    if ($files.Count -eq 0) { throw "Frontend group $Name has no files" }
    $jsonPath = Join-Path $resolvedResultDir "$Name.results.json"
    $runPath = Join-Path $resolvedResultDir "$Name.run.txt"
    $markerPath = Join-Path $resolvedResultDir "$Name.complete.json"
    Remove-Item -LiteralPath $markerPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $jsonPath -Force -ErrorAction SilentlyContinue

    $arguments = @(
        'test', '--', '--run', '--pool=forks', '--maxWorkers=1', '--minWorkers=1',
        '--no-file-parallelism', '--reporter=json', "--outputFile=$jsonPath"
    ) + $files
    [IO.File]::WriteAllText($runPath, '', [Text.UTF8Encoding]::new($false))
    $null = & npm.cmd @arguments 2>&1 | Tee-Object -FilePath $runPath
    $exitCode = $LASTEXITCODE
    if (-not (Test-Path -LiteralPath $jsonPath)) {
        throw "$Name did not produce a Vitest JSON report (exit code $exitCode). Child process output was preserved at $runPath"
    }
    $report = Get-Content -LiteralPath $jsonPath -Raw -Encoding utf8 | ConvertFrom-Json
    $records = @(Get-AssertionRecords $report $files)
    $duplicates = @($records | Group-Object -Property id | Where-Object Count -gt 1)
    if ($duplicates.Count -gt 0) { throw "$Name contains duplicate test ids" }
    if ($exitCode -ne 0 -or $report.success -ne $true) { throw "$Name Vitest failed with exit code $exitCode" }
    if ([int]$report.numPendingTests -ne 0 -or [int]$report.numTodoTests -ne 0) {
        throw "$Name contains skipped or todo tests"
    }
    if ($records.Count -ne [int]$report.numTotalTests) {
        throw "$Name assertion count does not match Vitest report"
    }
    [ordered]@{
        marker_version = 1
        status = 'completed'
        group = $Name
        exit_code = $exitCode
        file_count = $files.Count
        test_count = $records.Count
        manifest_sha256 = Get-Sha256 $manifestPath
        source_sha256 = $manifest.source_hash
        result_sha256 = Get-Sha256 $jsonPath
    } | ConvertTo-Json | Set-Content -LiteralPath $markerPath -Encoding utf8
    Write-Host "$Name completed: $($files.Count) files, $($records.Count) tests"
}

function Invoke-Aggregate {
    $manifest = Get-Manifest
    $files = @($manifest.files)
    $allRecords = New-Object System.Collections.Generic.List[object]
    $seenFiles = @{}
    foreach ($name in $groupNames) {
        $jsonPath = Join-Path $resolvedResultDir "$name.results.json"
        $markerPath = Join-Path $resolvedResultDir "$name.complete.json"
        if (-not (Test-Path -LiteralPath $jsonPath) -or -not (Test-Path -LiteralPath $markerPath)) {
            throw "$name completion result is missing"
        }
        $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding utf8 | ConvertFrom-Json
        if ($marker.marker_version -ne 1 -or $marker.status -ne 'completed' -or $marker.group -ne $name -or [int]$marker.exit_code -ne 0) {
            throw "$name completion marker is not successful"
        }
        if ($marker.manifest_sha256 -ne (Get-Sha256 $manifestPath) -or $marker.source_sha256 -ne $manifest.source_hash) {
            throw "$name completion marker is from a different manifest or source set"
        }
        if ($marker.result_sha256 -ne (Get-Sha256 $jsonPath)) { throw "$name result hash mismatches marker" }
        $expected = @($files | Where-Object { (Get-GroupForFile $_) -eq $name })
        if ([int]$marker.file_count -ne $expected.Count) { throw "$name completion marker file_count mismatches manifest" }
        $report = Get-Content -LiteralPath $jsonPath -Raw -Encoding utf8 | ConvertFrom-Json
        $records = @(Get-AssertionRecords $report $expected)
        if ($records.Count -ne [int]$marker.test_count -or $records.Count -ne [int]$report.numTotalTests) {
            throw "$name test count mismatches completion marker"
        }
        foreach ($file in @(Get-ReportFiles $report)) {
            if ($seenFiles.ContainsKey($file)) { $seenFiles[$file] += 1 } else { $seenFiles[$file] = 1 }
        }
        foreach ($record in $records) { $allRecords.Add($record) }
    }
    $duplicateTests = @($allRecords | Group-Object -Property id | Where-Object Count -gt 1)
    if ($duplicateTests.Count -gt 0) { throw 'Frontend group coverage contains duplicate test ids' }
    foreach ($file in $files) {
        if ($seenFiles[$file] -ne 1) { throw "Frontend file coverage is incomplete or duplicated: $file" }
    }
    [ordered]@{
        status = 'completed'
        group_count = $groupNames.Count
        file_count = $files.Count
        test_count = $allRecords.Count
        groups = $groupNames
        test_ids_sha256 = Get-TextSha256 (($allRecords | ForEach-Object { $_.id }) -join "`n")
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $aggregatePath -Encoding utf8
    Write-Host "All frontend groups passed: $($allRecords.Count) tests across $($files.Count) files"
}

if ($Collect) {
    if ($Aggregate -or $Group) { throw '-Collect cannot be combined with -Group or -Aggregate' }
    Write-Manifest
} elseif ($Aggregate) {
    Invoke-Aggregate
} elseif ($Group) {
    Invoke-FrontendGroup $Group
} else {
    throw 'Use -Collect, -Group, or -Aggregate'
}
