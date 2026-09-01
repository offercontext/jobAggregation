param(
  [switch]$ValidateAudit,
  [string]$AuditPath,
  [string]$BrowserAuditPath,
  [string]$ExpectedBaseUrl,
  [string]$ExpectedTargetId,
  [string]$ExpectedSessionId,
  [int]$AuditorExitCode = 0,
  [switch]$ValidateProviderEgress,
  [string]$ProviderAuditPath,
  [string]$ProviderAllowlistPath,
  [switch]$ValidateProviderConfig,
  [string]$ProviderConfigPath,
  [switch]$ValidateAttemptPersistence,
  [string]$StoryAttemptAuditPath,
  [switch]$ValidateScreenshotMatrix,
  [string]$ScreenshotDirectory,
  [string]$ScreenshotManifestPath,
  [string]$CompletionSignalPath,
  [string]$SessionStatePath,
  [string]$CleanupAuditPath,
  [switch]$ForceAuditorStartupCleanupFailureForTest,
  [int]$ForceChromiumStartupExitFailuresForTest = 0,
  [int]$ForceChromiumStartupTimeoutFailuresForTest = 0,
  [int]$ForceChromiumStartupLateReadyFailuresForTest = 0,
  [int]$ChromiumStartupDeadlineMillisecondsForTest = 0,
  [switch]$ForceChromiumStartupCleanupFailureForTest
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http

if ($ForceChromiumStartupExitFailuresForTest -lt 0 -or $ForceChromiumStartupExitFailuresForTest -gt 3) {
  throw 'ForceChromiumStartupExitFailuresForTest must be between zero and three.'
}
if ($ForceChromiumStartupTimeoutFailuresForTest -lt 0 -or $ForceChromiumStartupTimeoutFailuresForTest -gt 3) {
  throw 'ForceChromiumStartupTimeoutFailuresForTest must be between zero and three.'
}
if ($ForceChromiumStartupLateReadyFailuresForTest -lt 0 -or $ForceChromiumStartupLateReadyFailuresForTest -gt 3) {
  throw 'ForceChromiumStartupLateReadyFailuresForTest must be between zero and three.'
}
$chromiumStartupFaultModeCount = @(
  $ForceChromiumStartupExitFailuresForTest,
  $ForceChromiumStartupTimeoutFailuresForTest,
  $ForceChromiumStartupLateReadyFailuresForTest
).Where({ $_ -gt 0 }).Count
if ($chromiumStartupFaultModeCount -gt 1) {
  throw 'Only one Chromium startup fault mode can be enabled per test run.'
}
if ($ChromiumStartupDeadlineMillisecondsForTest -ne 0 -and (
  $ChromiumStartupDeadlineMillisecondsForTest -lt 100 -or
  $ChromiumStartupDeadlineMillisecondsForTest -gt 5000
)) {
  throw 'ChromiumStartupDeadlineMillisecondsForTest must be zero or between 100 and 5000.'
}
if ($ChromiumStartupDeadlineMillisecondsForTest -gt 0 -and $ForceChromiumStartupLateReadyFailuresForTest -eq 0) {
  throw 'ChromiumStartupDeadlineMillisecondsForTest requires ForceChromiumStartupLateReadyFailuresForTest.'
}

$repo = Split-Path -Parent $PSScriptRoot
$sourceData = if ($env:OFFERPILOT_DATA) { $env:OFFERPILOT_DATA } else { Join-Path $HOME '.offerpilot' }
$tempData = Join-Path ([IO.Path]::GetTempPath()) ('offerpilot-interview-story-' + [Guid]::NewGuid().ToString('N'))
$browserProfile = $null
$browserAudit = Join-Path $tempData 'browser-network.jsonl'
$browserStop = Join-Path $tempData 'browser-network.stop'
$browserReady = Join-Path $tempData 'browser-network.ready'
$auditorStdout = Join-Path $tempData 'browser-network-auditor.stdout.log'
$auditorStderr = Join-Path $tempData 'browser-network-auditor.stderr.log'
$providerAudit = Join-Path $tempData 'provider-egress.jsonl'
$providerAllowlist = Join-Path $tempData 'provider-allowlist.json'
$server = $null
$proxy = $null
$browser = $null
$browserStartupAttempt = $null
$auditor = $null
$previousData = $env:OFFERPILOT_DATA
$previousHttpProxy = $env:HTTP_PROXY
$previousHttpsProxy = $env:HTTPS_PROXY
$previousNoProxy = $env:NO_PROXY
$projectPython = Join-Path $repo '.venv\Scripts\python.exe'
$projectOc = Join-Path $repo '.venv\Scripts\oc.exe'

function Get-FreePort {
  $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
  try { $listener.Start(); return ([Net.IPEndPoint]$listener.LocalEndpoint).Port }
  finally { $listener.Stop() }
}

function Get-DistinctFreePort([System.Collections.Generic.HashSet[int]]$reservedPorts) {
  for ($attempt = 0; $attempt -lt 100; $attempt++) {
    $candidate = Get-FreePort
    if ($reservedPorts.Add($candidate)) { return $candidate }
  }
  throw 'Could not allocate a distinct local port for Chromium CDP.'
}

function Assert-ExitCode([string]$label) {
  if ($LASTEXITCODE -ne 0) { throw "$label failed with exit code $LASTEXITCODE." }
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

function Stop-Tree([object]$process, [string]$label = 'local process') {
  if ($null -eq $process) { return }
  $processId = [int]$process.Id
  $children = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $_.ParentProcessId -eq $processId })
  foreach ($child in $children) { Stop-Tree ([pscustomobject]@{ Id = $child.ProcessId }) "$label child" }
  $running = Get-Process -Id $processId -ErrorAction SilentlyContinue
  if ($null -ne $running) { Stop-Process -Id $processId -Force -ErrorAction Stop }
  $deadline = [DateTime]::UtcNow.AddSeconds(15)
  while ($null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
    if ([DateTime]::UtcNow -ge $deadline) { throw "$label process $processId did not exit during cleanup." }
    Start-Sleep -Milliseconds 100
  }
}

function Remove-IsolatedTempData {
  $lastError = $null
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    try {
      if (Test-Path -LiteralPath $tempData) {
        Remove-Item -LiteralPath $tempData -Recurse -Force -ErrorAction Stop
      }
      if (-not (Test-Path -LiteralPath $tempData)) { return }
    } catch {
      $lastError = $_
    }
    Start-Sleep -Milliseconds 150
  }
  if ($null -ne $lastError) { throw "Isolated browser acceptance data cleanup failed: $($lastError.Exception.Message)" }
  throw 'Isolated browser acceptance data cleanup failed.'
}

function Invoke-IsolatedPython([string]$label, [string]$code) {
  if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Project Python runtime is missing.'
  }
  $scriptPath = Join-Path $tempData ("$label-" + [Guid]::NewGuid().ToString('N') + '.py')
  $stdoutPath = "$scriptPath.stdout"
  $stderrPath = "$scriptPath.stderr"
  [IO.File]::WriteAllText($scriptPath, $code, [Text.UTF8Encoding]::new($false))
  try {
    $process = Start-Process -FilePath $projectPython -WorkingDirectory $repo -ArgumentList @($scriptPath) -PassThru -Wait -NoNewWindow -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    $output = @(
      if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath }
      if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath }
    )
    $exitCode = $process.ExitCode
    if ($exitCode -ne 0) { throw "$label failed with exit code ${exitCode}: $($output -join [Environment]::NewLine)" }
    return @($output)
  } finally {
    Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stdoutPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
  }
}

function Wait-ForHttpReady([object]$process, [string]$uri, [string]$label, [int]$attempts = 60) {
  for ($i = 0; $i -lt $attempts; $i++) {
    if ($process.HasExited) { throw "$label exited before readiness." }
    try {
      $response = Invoke-RestMethod -Uri $uri -TimeoutSec 2
      if ($null -ne $response) { return $response }
    } catch { }
    Start-Sleep -Milliseconds 500
  }
  throw "$label did not become ready."
}

function Get-ProcessDiagnostic([string]$stdoutPath, [string]$stderrPath) {
  $lines = @()
  foreach ($path in @($stdoutPath, $stderrPath)) {
    if (Test-Path -LiteralPath $path) { $lines += @(Get-Content -LiteralPath $path -ErrorAction SilentlyContinue) }
  }
  $text = ($lines -join [Environment]::NewLine).Trim()
  if ([string]::IsNullOrWhiteSpace($text)) { return 'no local diagnostic output' }
  $start = [Math]::Max(0, $text.Length - 4096)
  return $text.Substring($start)
}

function Wait-ForChromiumCdpReady([object]$process, [string]$uri, [int]$deadlineMilliseconds) {
  $deadline = [DateTime]::UtcNow.AddMilliseconds($deadlineMilliseconds)
  $handler = [Net.Http.HttpClientHandler]::new()
  $handler.UseProxy = $false
  $client = [Net.Http.HttpClient]::new($handler)
  try {
    while ($true) {
      $now = [DateTime]::UtcNow
      if ($now -ge $deadline) {
        return [pscustomobject]@{ Outcome = 'timed_out'; DeadlineMilliseconds = $deadlineMilliseconds }
      }
      $process.Refresh()
      if ($process.HasExited) {
        return [pscustomobject]@{ Outcome = 'exited'; DeadlineMilliseconds = $deadlineMilliseconds }
      }

      $remainingMilliseconds = [int][Math]::Max(1, [Math]::Ceiling(($deadline - $now).TotalMilliseconds))
      $cancellation = [Threading.CancellationTokenSource]::new()
      $response = $null
      try {
        $cancellation.CancelAfter($remainingMilliseconds)
        $response = $client.GetAsync($uri, $cancellation.Token).GetAwaiter().GetResult()
        if ([DateTime]::UtcNow -ge $deadline) {
          return [pscustomobject]@{ Outcome = 'timed_out'; DeadlineMilliseconds = $deadlineMilliseconds }
        }
        $response.EnsureSuccessStatusCode()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if ([DateTime]::UtcNow -ge $deadline) {
          return [pscustomobject]@{ Outcome = 'timed_out'; DeadlineMilliseconds = $deadlineMilliseconds }
        }
        $version = $body | ConvertFrom-Json -ErrorAction Stop
        if ([DateTime]::UtcNow -ge $deadline) {
          return [pscustomobject]@{ Outcome = 'timed_out'; DeadlineMilliseconds = $deadlineMilliseconds }
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$version.webSocketDebuggerUrl)) {
          return [pscustomobject]@{ Outcome = 'ready'; DeadlineMilliseconds = $deadlineMilliseconds }
        }
      } catch {
        if ([DateTime]::UtcNow -ge $deadline) {
          return [pscustomobject]@{ Outcome = 'timed_out'; DeadlineMilliseconds = $deadlineMilliseconds }
        }
      } finally {
        if ($null -ne $response) { $response.Dispose() }
        $cancellation.Dispose()
      }

      $sleepMilliseconds = [int][Math]::Min(
        250,
        [Math]::Max(1, [Math]::Ceiling(($deadline - [DateTime]::UtcNow).TotalMilliseconds))
      )
      if ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds $sleepMilliseconds
      }
    }
  } finally {
    $client.Dispose()
  }
}

function Remove-ChromiumAttemptProfile([string]$profile) {
  if (Test-Path -LiteralPath $profile) {
    Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction Stop
  }
  if (Test-Path -LiteralPath $profile) {
    throw "Chromium startup profile still exists after cleanup: $profile"
  }
}

function Start-DedicatedChromium([string]$chromium, [int[]]$reservedPortValues) {
  $reservedPorts = [System.Collections.Generic.HashSet[int]]::new()
  foreach ($reservedPort in $reservedPortValues) { [void]$reservedPorts.Add($reservedPort) }
  $attemptDiagnostics = [System.Collections.Generic.List[string]]::new()

  for ($attempt = 1; $attempt -le 3; $attempt++) {
    $deadlineMilliseconds = if (
      $attempt -le $ForceChromiumStartupLateReadyFailuresForTest -and
      $ChromiumStartupDeadlineMillisecondsForTest -gt 0
    ) {
      $ChromiumStartupDeadlineMillisecondsForTest
    } else {
      20000
    }
    $deadlineSecondsLabel = ($deadlineMilliseconds / 1000.0).ToString(
      '0.###',
      [Globalization.CultureInfo]::InvariantCulture
    )
    $attemptPort = Get-DistinctFreePort $reservedPorts
    $attemptProfile = Join-Path $tempData "browser-profile-attempt-$attempt"
    $attemptStdout = Join-Path $tempData "chromium-startup-attempt-$attempt.stdout.log"
    $attemptStderr = Join-Path $tempData "chromium-startup-attempt-$attempt.stderr.log"
    New-Item -ItemType Directory -Force -Path $attemptProfile | Out-Null
    $process = $null
    $outcome = 'launch_error'
    $launchDiagnostic = $null
    $observedExitCode = $null
    $readinessElapsedMilliseconds = 0
    $lateReadyStubReady = $null
    try {
      if ($attempt -le $ForceChromiumStartupExitFailuresForTest) {
        $faultScript = Join-Path $tempData "forced-chromium-startup-exit-$attempt.py"
        [IO.File]::WriteAllText(
          $faultScript,
          "import sys, time`nprint('forced Chromium startup exit', file=sys.stderr, flush=True)`ntime.sleep(0.25)`nraise SystemExit(73)`n",
          [Text.UTF8Encoding]::new($false)
        )
        $process = Start-Process -FilePath $projectPython -WorkingDirectory $repo -WindowStyle Hidden -PassThru -ArgumentList @($faultScript) -RedirectStandardOutput $attemptStdout -RedirectStandardError $attemptStderr
      } elseif ($attempt -le $ForceChromiumStartupTimeoutFailuresForTest) {
        $faultScript = Join-Path $tempData "forced-chromium-startup-timeout-$attempt.py"
        [IO.File]::WriteAllText(
          $faultScript,
          "import sys, time`nprint('forced Chromium startup timeout', file=sys.stderr, flush=True)`ntime.sleep(60)`n",
          [Text.UTF8Encoding]::new($false)
        )
        $process = Start-Process -FilePath $projectPython -WorkingDirectory $repo -WindowStyle Hidden -PassThru -ArgumentList @($faultScript) -RedirectStandardOutput $attemptStdout -RedirectStandardError $attemptStderr
      } elseif ($attempt -le $ForceChromiumStartupLateReadyFailuresForTest) {
        $faultScript = Join-Path $tempData "forced-chromium-startup-late-ready-$attempt.py"
        $lateReadyStubReady = Join-Path $tempData "forced-chromium-startup-late-ready-$attempt.ready"
        [IO.File]::WriteAllText(
          $faultScript,
          @'
import http.server
import json
import pathlib
import sys
import time


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        print("forced Chromium late-ready CDP stub accepted request", file=sys.stderr, flush=True)
        time.sleep(0.35)
        payload = json.dumps(
            {"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/late"}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


server = http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
pathlib.Path(sys.argv[2]).write_text("ready", encoding="utf-8")
print("forced Chromium late-ready CDP stub listening", file=sys.stderr, flush=True)
server.handle_request()
'@,
          [Text.UTF8Encoding]::new($false)
        )
        $process = Start-Process -FilePath $projectPython -WorkingDirectory $repo -WindowStyle Hidden -PassThru -ArgumentList @($faultScript, [string]$attemptPort, $lateReadyStubReady) -RedirectStandardOutput $attemptStdout -RedirectStandardError $attemptStderr
      } else {
        $process = Start-Process -FilePath $chromium -PassThru -ArgumentList @(
          "--remote-debugging-port=$attemptPort",
          "--user-data-dir=$attemptProfile",
          '--no-first-run',
          '--no-default-browser-check',
          '--remote-allow-origins=*',
          '--window-size=1455,1200',
          '--force-color-profile=srgb',
          '--enable-logging=stderr',
          'about:blank'
        ) -RedirectStandardOutput $attemptStdout -RedirectStandardError $attemptStderr
      }
      [void]$process.Handle
      $script:browserStartupAttempt = $process
      if ($null -ne $lateReadyStubReady) {
        $stubDeadline = [DateTime]::UtcNow.AddSeconds(5)
        while (-not (Test-Path -LiteralPath $lateReadyStubReady)) {
          $process.Refresh()
          if ($process.HasExited) { throw 'Forced Chromium late-ready CDP stub exited before listening.' }
          if ([DateTime]::UtcNow -ge $stubDeadline) { throw 'Forced Chromium late-ready CDP stub did not begin listening.' }
          Start-Sleep -Milliseconds 10
        }
      }
      $readinessStopwatch = [Diagnostics.Stopwatch]::StartNew()
      $readiness = Wait-ForChromiumCdpReady $process "http://127.0.0.1:$attemptPort/json/version" $deadlineMilliseconds
      $readinessStopwatch.Stop()
      $readinessElapsedMilliseconds = [int]$readinessStopwatch.ElapsedMilliseconds
      $outcome = [string]$readiness.Outcome
      if ($outcome -eq 'exited') {
        $process.WaitForExit()
        $process.Refresh()
        $observedExitCode = $process.ExitCode
      }
      if ($outcome -eq 'ready') {
        foreach ($diagnostic in $attemptDiagnostics) {
          Write-Host "Dedicated Chromium startup retry diagnostic: $diagnostic"
        }
        return [pscustomobject]@{
          Process = $process
          Port = $attemptPort
          Profile = $attemptProfile
          Attempt = $attempt
        }
      }
    } catch {
      $outcome = 'launch_error'
      $launchDiagnostic = $_.Exception.Message
    }

    $exitCode = 'not_available'
    if ($null -ne $observedExitCode) {
      $exitCode = [string]$observedExitCode
    }
    $localDiagnostic = Get-ProcessDiagnostic $attemptStdout $attemptStderr
    if (-not [string]::IsNullOrWhiteSpace($launchDiagnostic)) {
      $localDiagnostic = "$launchDiagnostic | $localDiagnostic"
    }
    try {
      if ($null -ne $process) { Stop-Tree $process "Chromium startup attempt $attempt" }
      if ($null -eq $observedExitCode -and $null -ne $process) {
        $process.Refresh()
        if ($process.HasExited) {
          $process.WaitForExit()
          $exitCode = [string]$process.ExitCode
        }
      }
      if ($ForceChromiumStartupCleanupFailureForTest -and $attempt -eq 1) {
        throw "Forced Chromium startup cleanup failure for test after attempt $attempt."
      }
      Remove-ChromiumAttemptProfile $attemptProfile
    } catch {
      $cleanupError = $_.Exception.Message
      if ($exitCode -eq 'not_available' -and $null -ne $process) {
        try {
          $process.Refresh()
          if ($process.HasExited) {
            $process.WaitForExit()
            $exitCode = [string]$process.ExitCode
          }
        } catch { }
      }
      $failedCleanupDiagnostic = "attempt=$attempt; outcome=$outcome; port=$attemptPort; profile=$attemptProfile; exit_code=$exitCode; deadline_seconds=$deadlineSecondsLabel; deadline_milliseconds=$deadlineMilliseconds; readiness_elapsed_ms=$readinessElapsedMilliseconds; cleanup=failed; diagnostic=$localDiagnostic; cleanup_error=$cleanupError"
      [Console]::Error.WriteLine("Dedicated Chromium startup cleanup diagnostic: $failedCleanupDiagnostic")
      throw "Chromium startup attempt $attempt cleanup failed; refusing another launch."
    }
    $script:browserStartupAttempt = $null

    $attemptDiagnostics.Add(
      "attempt=$attempt; outcome=$outcome; port=$attemptPort; profile=$attemptProfile; exit_code=$exitCode; deadline_seconds=$deadlineSecondsLabel; deadline_milliseconds=$deadlineMilliseconds; readiness_elapsed_ms=$readinessElapsedMilliseconds; cleanup=complete; diagnostic=$localDiagnostic"
    )
  }

  foreach ($diagnostic in $attemptDiagnostics) {
    [Console]::Error.WriteLine("Dedicated Chromium startup failure diagnostic: $diagnostic")
  }
  throw 'Dedicated Chromium CDP endpoint did not become ready after three bounded attempts.'
}

function Start-BrowserAuditor([string]$cdpUrl, [string]$expectedUrl, [ref]$trackedAuditor) {
  $lastDiagnostic = 'no local diagnostic output'
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    Remove-Item -LiteralPath $browserReady, $auditorStdout, $auditorStderr -Force -ErrorAction SilentlyContinue
    $process = Start-Process -FilePath $projectPython -WorkingDirectory $repo -WindowStyle Hidden -PassThru -ArgumentList @('scripts/browser-network-audit.py', '--debugging-url', $cdpUrl, '--expected-url', $expectedUrl, '--audit', $browserAudit, '--stop-file', $browserStop, '--ready-file', $browserReady) -RedirectStandardOutput $auditorStdout -RedirectStandardError $auditorStderr
    $trackedAuditor.Value = $process
    if ($ForceAuditorStartupCleanupFailureForTest) {
      throw 'Forced browser auditor startup cleanup failure.'
    }
    for ($wait = 0; $wait -lt 40; $wait++) {
      if (Test-Path -LiteralPath $browserReady) {
        try {
          $identity = Get-Content -LiteralPath $browserReady -Raw | ConvertFrom-Json
          $targetId = [string]$identity.target_id
          $sessionId = [string]$identity.session_id
          if ([string]::IsNullOrWhiteSpace($targetId) -or [string]::IsNullOrWhiteSpace($sessionId)) {
            throw 'ready identity is incomplete'
          }
          return [pscustomobject]@{ Process = $process; TargetId = $targetId; SessionId = $sessionId }
        } catch {
          $lastDiagnostic = 'Browser auditor ready file did not contain a dedicated target/session identity.'
          break
        }
      }
      if ($process.HasExited) {
        $lastDiagnostic = Get-ProcessDiagnostic $auditorStdout $auditorStderr
        break
      }
      Start-Sleep -Milliseconds 500
    }
    if (-not $process.HasExited) {
      $lastDiagnostic = 'Browser auditor did not complete its Network ready handshake.'
      Stop-Tree $process 'browser auditor startup'
    }
  }
  throw "Browser auditor did not become ready after three bounded local-CDP attempts: $lastDiagnostic"
}

function Find-Chromium {
  $paths = @(
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
  )
  foreach ($path in $paths) {
    if ($path -and (Test-Path -LiteralPath $path)) { return $path }
  }
  throw 'No local Chromium browser was found for the isolated CDP acceptance.'
}

function Get-ProviderEndpoints([string]$configPath) {
  if (-not (Test-Path -LiteralPath $configPath)) { throw 'Provider config is missing.' }
  $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
  $byId = @{}
  $configuredProviderCount = 0
  foreach ($provider in @($config.providers)) {
    if ($provider.id) {
      $configuredProviderCount += 1
      $byId[[string]$provider.id] = $provider
    }
  }
  if ($byId.Count -eq 0 -and $config.base_url) {
    $id = if ($config.active_provider_id) { [string]$config.active_provider_id } else { 'default' }
    $byId[$id] = [pscustomobject]@{ id = $id; enabled = $true; base_url = [string]$config.base_url }
  }
  $ids = @()
  if ($configuredProviderCount -gt 0 -and [string]::IsNullOrWhiteSpace([string]$config.active_provider_id)) {
    throw 'Configured providers require active_provider_id.'
  }
  if ($config.active_provider_id) { $ids += [string]$config.active_provider_id }
  if ($config.fallback_provider_ids) { $ids += @($config.fallback_provider_ids | ForEach-Object { [string]$_ }) }
  if ($config.fallback_provider_id) { $ids += [string]$config.fallback_provider_id }
  if ($ids.Count -eq 0 -and $byId.Count -eq 1 -and $config.base_url) { $ids = @($byId.Keys) }
  if ($ids.Count -eq 0) { throw 'Configured providers require active_provider_id.' }
  $seen = @{}
  $result = @()
  foreach ($id in $ids) {
    if (-not $byId.ContainsKey($id)) { throw "Configured Provider id '$id' is missing." }
    $provider = $byId[$id]
    if (-not $provider.enabled -or -not $provider.base_url) {
      if ($id -eq [string]$config.active_provider_id) { throw 'Configured active Provider is disabled or has no endpoint.' }
      continue
    }
    $uri = [Uri]$provider.base_url
    $port = if ($uri.IsDefaultPort) { if ($uri.Scheme -eq 'https') { 443 } else { 80 } } else { $uri.Port }
    $tuple = "$($uri.Scheme)://$($uri.Host):$port"
    if (-not $seen.ContainsKey($tuple)) {
      $seen[$tuple] = $true
      $result += [pscustomobject]@{ Scheme = $uri.Scheme; Host = $uri.Host; Port = $port; Tuple = $tuple }
    }
  }
  if ($result.Count -eq 0) { throw 'No enabled Provider endpoint is configured.' }
  return $result
}

function Get-ForbiddenDomainSnapshot {
  $env:INTERVIEW_STORY_HARNESS_DB = Join-Path $tempData 'data.db'
  $code = @'
import hashlib, json, os, sqlite3
db = sqlite3.connect(os.environ["INTERVIEW_STORY_HARNESS_DB"])
allowed = {
  "interview_stories", "interview_story_versions", "interview_story_version_evidence_links",
  "interview_story_user_assertions", "interview_story_proposal_attempts"
}
tables = [row[0] for row in db.execute("select name from sqlite_master where type='table'") if row[0] not in allowed and not row[0].startswith("sqlite_") and not row[0].startswith("knowledge_evidence_fts_")]
result = {}
for name in sorted(tables):
    rows = db.execute(f"select * from {name} order by rowid").fetchall()
    data = json.dumps(rows, ensure_ascii=False, default=str, separators=(",", ":"))
    result[name] = {"count": len(rows), "sha256": hashlib.sha256(data.encode("utf-8")).hexdigest()}
print(json.dumps(result, separators=(",", ":")))
'@
  $raw = Invoke-IsolatedPython 'forbidden-domain-snapshot' $code
  return (($raw -join '').Trim() | ConvertFrom-Json)
}

function Assert-ForbiddenDomainsUnchanged($before, $after) {
  $names = @($before.PSObject.Properties.Name + $after.PSObject.Properties.Name | Sort-Object -Unique)
  foreach ($name in $names) {
    $left = $before.$name
    $right = $after.$name
    if ($null -eq $left -or $null -eq $right -or [int]$left.count -ne [int]$right.count -or [string]$left.sha256 -ne [string]$right.sha256) {
      throw "Unexpected non-Story write in $name."
    }
  }
}

function Seed-StoryContext {
  $env:INTERVIEW_STORY_HARNESS_DB = Join-Path $tempData 'data.db'
  $code = @'
import json, os
from datetime import datetime, timezone
from pathlib import Path
from offerpilot.db import session_factory_for_data_dir
from offerpilot.models import Application, ApplicationEvent, InterviewNote, Resume, MockInterviewAttempt, MockInterviewTurn
data_dir = Path(os.environ["INTERVIEW_STORY_HARNESS_DB"]).parent
factory = session_factory_for_data_dir(data_dir)
try:
  with factory() as s:
    app = Application(company_name="\u661f\u4e91\u6570\u636e", position_name="\u540e\u7aef\u5de5\u7a0b\u5e08", status="interview", source="smoke")
    s.add(app); s.flush()
    event = ApplicationEvent(application_id=app.id, event_type="interview", subtype="technical", scheduled_at=datetime.now(timezone.utc), duration_minutes=45, status="done")
    resume = Resume(name="\u7b71\u54f2", title="\u540e\u7aef\u5de5\u7a0b\u5e08\u7b80\u5386", content_json=json.dumps({"\u9879\u76ee": {"\u5185\u5bb9": "\u8d1f\u8d23\u5ef6\u8fdf\u6392\u67e5\u548c\u98ce\u9669\u540c\u6b65"}}, ensure_ascii=False))
    s.add_all([event, resume]); s.flush()
    note = InterviewNote(application_id=app.id, application_event_id=event.id, company="\u661f\u4e91\u6570\u636e", position="\u540e\u7aef\u5de5\u7a0b\u5e08", questions="\u5982\u4f55\u6392\u67e5\u7ebf\u4e0a\u5ef6\u8fdf\uff1f", self_reflection="\u6211\u5148\u786e\u8ba4\u6307\u6807\uff0c\u518d\u540c\u6b65\u98ce\u9669\u3002")
    attempt = MockInterviewAttempt(application_id=app.id, event_id=event.id, resume_id=resume.id, idempotency_key="story-browser-mock-attempt", input_snapshot_json="{}", source_fingerprint="browser-mock", attempt_status="feedback_ready", transcript_fingerprint="browser-transcript", completed_at=datetime.now(timezone.utc))
    s.add_all([note, attempt]); s.flush()
    s.add(MockInterviewTurn(attempt_id=attempt.id, turn_no=1, question_idempotency_key="story-browser-question", turn_idempotency_key="story-browser-answer", question_text="\u8bf7\u4ecb\u7ecd\u4e00\u6b21\u95ee\u9898\u6392\u67e5\u3002", answer_text="\u6211\u901a\u8fc7\u5206\u6bb5\u5b9a\u4f4d\u89e3\u51b3\u4e86\u5ef6\u8fdf\u3002", turn_status="answered"))
    s.commit()
    print(json.dumps({"application_id": app.id, "event_id": event.id, "note_id": note.id, "resume_id": resume.id, "mock_attempt_id": attempt.id}, separators=(",", ":")))
finally:
  factory.kw["bind"].dispose()
'@
  $raw = Invoke-IsolatedPython 'story-context-seed' $code
  return (($raw -join '').Trim() | ConvertFrom-Json)
}

function Read-BrowserRecords([string]$path = $browserAudit) {
  if (-not (Test-Path -LiteralPath $path)) { throw 'Browser audit output is missing.' }
  return @(Get-Content -LiteralPath $path | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
}

function Read-StoryAttemptAudit([string]$path) {
  if ([string]::IsNullOrWhiteSpace($path) -or -not (Test-Path -LiteralPath $path)) {
    throw 'Persisted Story attempt audit output is missing.'
  }
  return @(Get-Content -LiteralPath $path -Raw | ConvertFrom-Json)
}

function Get-PersistedStoryAttempts {
  $env:INTERVIEW_STORY_HARNESS_DB = Join-Path $tempData 'data.db'
  $code = @'
import json, os, sqlite3
db = sqlite3.connect(os.environ["INTERVIEW_STORY_HARNESS_DB"])
rows = db.execute("""
    select id, entrypoint, attempt_status, repair_count, confirmed_story_id, confirmed_story_version_id
    from interview_story_proposal_attempts
    order by id
""").fetchall()
print(json.dumps([
    {
        "id": row[0],
        "entrypoint": row[1],
        "attempt_status": row[2],
        "repair_count": row[3],
        "confirmed_story_id": row[4],
        "confirmed_story_version_id": row[5],
    }
    for row in rows
], separators=(",", ":")))
'@
  $raw = Invoke-IsolatedPython 'story-attempt-persistence-audit' $code
  return @(($raw -join '').Trim() | ConvertFrom-Json)
}

function Get-RecordProperty($record, [string]$name) {
  $property = $record.PSObject.Properties[$name]
  if ($null -eq $property) { return $null }
  return $property.Value
}

function Assert-AuditorSucceeded([object]$process) {
  if (-not [bool]$process.HasExited) { throw 'Browser auditor did not stop cleanly.' }
  if ([int]$process.ExitCode -ne 0) { throw "Browser auditor failed with exit code $($process.ExitCode)." }
}

function Assert-RecordFromDedicatedTarget([object]$record, [string]$targetId, [string]$sessionId) {
  if ([string]::IsNullOrWhiteSpace($targetId) -and [string]::IsNullOrWhiteSpace($sessionId)) { return }
  if ([string]::IsNullOrWhiteSpace($targetId) -or [string]::IsNullOrWhiteSpace($sessionId)) {
    throw 'Story audit requires both the dedicated CDP target and session identities.'
  }
  if ((Get-RecordProperty $record 'target_id') -ne $targetId -or (Get-RecordProperty $record 'session_id') -ne $sessionId) {
    throw 'Story flow record is not bound to the dedicated CDP target/session.'
  }
}

function Get-StoryAttemptResponse([object[]]$records, [string]$entrypoint, [string]$url, [string]$targetId = '', [string]$sessionId = '') {
  $matches = @()
  for ($index = 0; $index -lt $records.Count; $index++) {
    $record = $records[$index]
    $context = Get-RecordProperty $record 'request_context'
    if (
      $record.kind -eq 'browser_response' -and $record.method -eq 'POST' -and
      $record.url -eq $url -and
      $null -ne $context -and
      $context.entrypoint -eq $entrypoint -and
      $record.response_body_status -eq 'captured' -and
      (Get-RecordProperty $record 'response_proposal_id') -is [int] -and
      $record.response_status -in @(200, 201)
    ) {
      Assert-RecordFromDedicatedTarget $record $targetId $sessionId
      $matches += [pscustomobject]@{ Index = $index; Record = $record }
    }
  }
  if ($matches.Count -eq 0) {
    throw "Browser did not capture a ready $entrypoint Story proposal response."
  }
  return $matches[-1]
}

function Get-StoryProviderFlowWindows([object[]]$records, [string]$baseUrl, [string]$targetId = '', [string]$sessionId = '') {
  $flows = @{}
  foreach ($entrypoint in @('ui', 'pilot')) {
    $url = if ($entrypoint -eq 'ui') { "$baseUrl/api/interview-story-proposals" } else { "$baseUrl/api/pilot/interview-story-proposals" }
    $requests = @()
    for ($index = 0; $index -lt $records.Count; $index++) {
      $record = $records[$index]
      if (
        $record.kind -eq 'browser_request' -and $record.method -eq 'POST' -and $record.url -eq $url -and
        $null -ne (Get-RecordProperty $record 'request_context') -and $record.request_context.entrypoint -eq $entrypoint
      ) {
        Assert-RecordFromDedicatedTarget $record $targetId $sessionId
        $requests += [pscustomobject]@{ Index = $index; Record = $record }
      }
    }
    if ($requests.Count -lt 1 -or $requests.Count -gt 2) {
      throw "Browser must capture one request or one bounded provider-error replay for $entrypoint."
    }
    if ($requests.Count -eq 2) {
      $firstContext = $requests[0].Record.request_context
      foreach ($request in $requests) {
        $context = $request.Record.request_context
        if (
          [string]::IsNullOrWhiteSpace([string]$context.idempotency_key_sha256) -or
          [string]::IsNullOrWhiteSpace([string]$context.payload_sha256) -or
          $context.idempotency_key_sha256 -ne $firstContext.idempotency_key_sha256 -or
          $context.payload_sha256 -ne $firstContext.payload_sha256
        ) {
          throw "Browser $entrypoint replay did not preserve the original idempotency key and frozen input."
        }
      }
      $retryResponses = @()
      for ($index = $requests[0].Index + 1; $index -lt $requests[1].Index; $index++) {
        $record = $records[$index]
        $context = Get-RecordProperty $record 'request_context'
        if (
          $record.kind -eq 'browser_response' -and $record.url -eq $url -and
          $null -ne $context -and $context.entrypoint -eq $entrypoint -and
          $record.response_status -eq 502 -and $record.response_body_status -eq 'captured'
        ) {
          $retryResponses += $record
        }
      }
      if ($retryResponses.Count -ne 1 -or $retryResponses[0].response_error_code -ne 'story_provider_error') {
        throw "Browser $entrypoint replay must follow exactly one story_provider_error response; deterministic failures cannot be replayed."
      }
      $retryAttemptId = Get-RecordProperty $retryResponses[0] 'response_proposal_id'
      if ($retryAttemptId -isnot [int]) {
        throw "Browser $entrypoint provider-error replay response is missing its Attempt identity."
      }
      $readyAttempt = Get-StoryAttemptResponse $records $entrypoint $url $targetId $sessionId
      if ([int]$readyAttempt.Record.response_proposal_id -ne [int]$retryAttemptId) {
        throw "Browser $entrypoint provider-error replay did not reuse the original Attempt identity."
      }
    }
    $response = Get-StoryAttemptResponse $records $entrypoint $url $targetId $sessionId
    if ($response.Index -le $requests[-1].Index) { throw "Browser $entrypoint ready response predates its final request." }
    $requestTimestamp = Get-RecordProperty $requests[0].Record 'observed_at_ns'
    $responseTimestamp = Get-RecordProperty $response.Record 'observed_at_ns'
    foreach ($timestamp in @($requestTimestamp, $responseTimestamp)) {
      if ($null -eq $timestamp -or [int64]$timestamp -le 0) {
        throw 'Story browser flow is missing request-scoped audit timestamps.'
      }
    }
    if ([int64]$requestTimestamp -gt [int64]$responseTimestamp) {
      throw 'Story browser flow audit timestamps are out of order.'
    }
    $flows[$entrypoint] = [pscustomobject]@{
      request_ns = [int64]$requestTimestamp
      response_ns = [int64]$responseTimestamp
      attempt_id = [int]$response.Record.response_proposal_id
      browser_replay_count = $requests.Count - 1
      target_id = $targetId
      session_id = $sessionId
    }
  }
  return [pscustomobject]@{ ui = $flows['ui']; pilot = $flows['pilot'] }
}

function Assert-StoryAttemptPersistence([object]$flows, [object[]]$attempts) {
  if ($null -eq $flows -or $null -eq $attempts -or $attempts.Count -ne 2) {
    throw 'Story attempt persistence audit must contain exactly the UI and Pilot attempts.'
  }
  $confirmedStories = @($attempts | ForEach-Object { Get-RecordProperty $_ 'confirmed_story_id' } | Where-Object { $null -ne $_ } | Sort-Object -Unique)
  $confirmedVersions = @($attempts | ForEach-Object { Get-RecordProperty $_ 'confirmed_story_version_id' } | Where-Object { $null -ne $_ } | Sort-Object -Unique)
  if ($confirmedStories.Count -ne 2 -or $confirmedVersions.Count -ne 2) {
    throw 'UI and Pilot must persist distinct confirmed Story and Version identities.'
  }
  $matched = @{}
  foreach ($entrypoint in @('ui', 'pilot')) {
    $flow = $flows.$entrypoint
    $attempt = @($attempts | Where-Object { (Get-RecordProperty $_ 'id') -eq $flow.attempt_id })
    if ($attempt.Count -ne 1) { throw "Browser $entrypoint flow is not mapped to one persisted Story Attempt." }
    $row = $attempt[0]
    if (
      (Get-RecordProperty $row 'entrypoint') -ne $entrypoint -or
      (Get-RecordProperty $row 'attempt_status') -ne 'confirmed' -or
      (Get-RecordProperty $row 'confirmed_story_id') -ne $flow.story_id -or
      (Get-RecordProperty $row 'confirmed_story_version_id') -ne $flow.story_version_id
    ) {
      throw "Browser $entrypoint flow does not match its persisted confirmed Story Attempt."
    }
    $repairCount = Get-RecordProperty $row 'repair_count'
    if ($repairCount -isnot [int] -or $repairCount -lt 0 -or $repairCount -gt 1) {
      throw "Browser $entrypoint persisted repair_count is invalid."
    }
    $matched[$entrypoint] = $row
  }
  if ($flows.ui.story_id -eq $flows.pilot.story_id -or $flows.ui.story_version_id -eq $flows.pilot.story_version_id) {
    throw 'UI and Pilot must persist distinct confirmed Story and Version identities.'
  }
  return [pscustomobject]@{ ui = $matched['ui']; pilot = $matched['pilot'] }
}

function Assert-ProviderEgress([object[]]$providers, [string]$auditPath = $providerAudit, [object]$flows, [object]$attempts) {
  if (-not (Test-Path -LiteralPath $auditPath)) {
    throw 'Provider egress audit output is missing.'
  }
  $allowed = @{}
  foreach ($provider in $providers) { $allowed[$provider.Tuple] = $true }
  $records = @(Get-Content -LiteralPath $auditPath | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
  $allConnections = @($records | Where-Object { $_.kind -eq 'provider_proxy_connect' })
  if (@($allConnections | Where-Object { $_.status -notin @('connected', 'rejected') }).Count -gt 0) {
    throw 'Provider egress audit contains an unknown proxy connection status.'
  }
  # `rejected` means the loopback proxy blocked a non-allowlisted CONNECT
  # before it reached the network (for example optional library telemetry).
  # Only a successful CONNECT is Provider egress and must be allowlisted and
  # correlated to one of the two Story flows.
  $connections = @($allConnections | Where-Object { $_.status -eq 'connected' })
  foreach ($connection in $connections) {
    $tuple = "$($connection.scheme)://$($connection.host):$($connection.port)"
    if (-not $allowed.ContainsKey($tuple)) {
      throw 'Provider egress was outside the configured candidate allowlist.'
    }
  }
  if ($null -eq $flows -or $null -eq $attempts) { throw 'Provider egress audit requires correlated UI/Pilot flows and persisted attempts.' }
  $windows = @(
    [pscustomobject]@{ entrypoint = 'ui'; start_ns = $flows.ui.request_ns; end_ns = $flows.ui.response_ns },
    [pscustomobject]@{ entrypoint = 'pilot'; start_ns = $flows.pilot.request_ns; end_ns = $flows.pilot.response_ns }
  )
  $counts = @{ ui = 0; pilot = 0 }
  foreach ($connection in $connections) {
    $timestamp = Get-RecordProperty $connection 'observed_at_ns'
    if ($null -eq $timestamp -or [int64]$timestamp -le 0) {
      throw 'Provider egress connection is missing its audit timestamp.'
    }
    $matches = @($windows | Where-Object { [int64]$timestamp -ge [int64]$_.start_ns -and [int64]$timestamp -le [int64]$_.end_ns })
    if ($matches.Count -ne 1) {
      throw 'Provider egress connection could not be correlated to exactly one UI or Pilot Story request.'
    }
    $counts[[string]$matches[0].entrypoint] += 1
  }
  foreach ($entrypoint in @('ui', 'pilot')) {
    $repairCount = Get-RecordProperty $attempts.$entrypoint 'repair_count'
    $browserReplayCount = Get-RecordProperty $flows.$entrypoint 'browser_replay_count'
    if ($browserReplayCount -isnot [int] -or $browserReplayCount -lt 0 -or $browserReplayCount -gt 1) {
      throw "Story $entrypoint browser replay count is invalid."
    }
    # CONNECT counts transport tunnels, not model calls. One model call can
    # reconnect, while an initial call and its bounded format repair can share
    # one keep-alive tunnel. Persisted repair_count plus the browser-proven
    # replay count are the call audit; the proxy proves destination and timing.
    if ($counts[$entrypoint] -lt 1) {
      throw "Story $entrypoint Provider egress was not observed in its request window."
    }
  }
}

function Assert-StoryBrowserSequence([object[]]$records, [string]$baseUrl, [string]$targetId = '', [string]$sessionId = '') {
  if (-not [string]::IsNullOrWhiteSpace($targetId) -or -not [string]::IsNullOrWhiteSpace($sessionId)) {
    foreach ($record in $records) { Assert-RecordFromDedicatedTarget $record $targetId $sessionId }
  }
  $origin = [Uri]$baseUrl
  $foreign = @($records | Where-Object {
    $rawUrl = Get-RecordProperty $_ 'url'
    if ($rawUrl -isnot [string] -or [string]::IsNullOrWhiteSpace($rawUrl)) { return $false }
    $uri = [Uri]$rawUrl
    $uri.Scheme -ne $origin.Scheme -or $uri.Host -ne $origin.Host -or $uri.Port -ne $origin.Port
  })
  if ($foreign.Count -gt 0) { throw 'Browser accessed a non-local URL.' }
  $chatWrites = @($records | Where-Object {
    $_.kind -eq 'browser_request' -and $_.method -eq 'POST' -and
    $_.url -in @("$baseUrl/api/chat", "$baseUrl/api/chat/confirm")
  })
  if ($chatWrites.Count -gt 0) { throw 'Pilot Story entry must not create chat writes.' }
  $uiPosts = @($records | Where-Object { $_.kind -eq 'browser_request' -and $_.method -eq 'POST' -and $_.url -eq "$baseUrl/api/interview-story-proposals" })
  $pilotPosts = @($records | Where-Object { $_.kind -eq 'browser_request' -and $_.method -eq 'POST' -and $_.url -eq "$baseUrl/api/pilot/interview-story-proposals" })
  if ($uiPosts.Count -lt 1 -or $uiPosts.Count -gt 2 -or $pilotPosts.Count -lt 1 -or $pilotPosts.Count -gt 2) { throw 'Browser did not execute one UI and one Pilot Story proposal sequence, each with at most one provider-error replay.' }
  $uiPostIndex = -1
  $pilotPostIndex = -1
  $sourceReadIndexes = @()
  $libraryReadIndexes = @()
  for ($index = 0; $index -lt $records.Count; $index++) {
    $record = $records[$index]
    if (
      $record.kind -ne 'browser_response' -or
      $record.method -ne 'GET' -or
      $record.response_status -lt 200 -or
      $record.response_status -ge 300 -or
      $record.response_body_status -ne 'captured'
    ) { continue }
    $uri = [Uri][string]$record.url
    if ($uri.AbsolutePath -eq '/api/interview-story-sources') { $sourceReadIndexes += $index }
    if ($uri.AbsolutePath -eq '/api/interview-stories') { $libraryReadIndexes += $index }
  }
  for ($index = 0; $index -lt $records.Count; $index++) {
    $record = $records[$index]
    if ($record.kind -ne 'browser_request' -or $record.method -ne 'POST') { continue }
    if ($record.url -eq "$baseUrl/api/interview-story-proposals") { $uiPostIndex = $index }
    if ($record.url -eq "$baseUrl/api/pilot/interview-story-proposals") { $pilotPostIndex = $index }
  }
  $uiResponse = Get-StoryAttemptResponse $records 'ui' "$baseUrl/api/interview-story-proposals" $targetId $sessionId
  $pilotResponse = Get-StoryAttemptResponse $records 'pilot' "$baseUrl/api/pilot/interview-story-proposals" $targetId $sessionId
  $attemptIds = @([int]$uiResponse.Record.response_proposal_id, [int]$pilotResponse.Record.response_proposal_id)
  if ($attemptIds[0] -eq $attemptIds[1]) { throw 'UI and Pilot did not receive distinct Story attempts.' }
  $keys = @($uiPosts + $pilotPosts | ForEach-Object { $_.request_context.idempotency_key_sha256 } | Where-Object { $_ } | Sort-Object -Unique)
  if ($keys.Count -ne 2) { throw 'UI and Pilot did not use exactly two distinct Story idempotency keys.' }
  $flowIndexes = @()
  foreach ($attemptId in $attemptIds) {
    $confirm = @()
    for ($index = 0; $index -lt $records.Count; $index++) {
      $record = $records[$index]
      if (
        $record.kind -eq 'browser_response' -and
        $record.url -eq "$baseUrl/api/interview-story-proposals/$attemptId/confirm" -and
        $record.response_body_status -eq 'captured' -and
        $record.response_status -in @(200, 201) -and
        (Get-RecordProperty $record 'response_story_id') -is [int] -and
        (Get-RecordProperty $record 'response_story_version_id') -is [int]
      ) {
        Assert-RecordFromDedicatedTarget $record $targetId $sessionId
        $confirm += [pscustomobject]@{ Index = $index; Record = $record }
      }
    }
    if ($confirm.Count -ne 1) { throw "Browser did not confirm Story attempt $attemptId exactly once." }
    $latestConfirm = $confirm[-1]
    # The production library reopens a confirmed Story through its aggregate GET.
    # Its response embeds the current immutable Version; require that embedded ID
    # to equal the Version returned by confirmation rather than inventing a
    # version-detail request that the UI does not make.
    $historyUrl = "$baseUrl/api/interview-stories/$($latestConfirm.Record.response_story_id)"
    $history = @()
    for ($index = $latestConfirm.Index + 1; $index -lt $records.Count; $index++) {
      $record = $records[$index]
      if (
        $record.kind -eq 'browser_response' -and
        $record.method -eq 'GET' -and
        $record.url -eq $historyUrl -and
        $record.response_status -eq 200 -and
        $record.response_body_status -eq 'captured' -and
        (Get-RecordProperty $record 'response_story_id') -eq $latestConfirm.Record.response_story_id -and
        (Get-RecordProperty $record 'response_story_current_version_id') -eq $latestConfirm.Record.response_story_version_id
      ) {
        Assert-RecordFromDedicatedTarget $record $targetId $sessionId
        $history += [pscustomobject]@{ Index = $index; Record = $record }
      }
    }
    if ($history.Count -ne 1) { throw "Browser did not reopen confirmed Story version for attempt $attemptId exactly once." }
    $proposalIndex = if ($attemptId -eq $attemptIds[0]) { $uiPostIndex } else { $pilotPostIndex }
    $flowIndexes += [pscustomobject]@{ ProposalIndex = $proposalIndex; ConfirmIndex = $latestConfirm.Index; HistoryIndex = $history[-1].Index }
  }
  $uiFlow = $flowIndexes[0]
  $pilotFlow = $flowIndexes[1]
  if (@($sourceReadIndexes | Where-Object { $_ -lt $uiFlow.ProposalIndex }).Count -eq 0) { throw 'Browser did not open the UI source picker before its proposal.' }
  if (@($libraryReadIndexes | Where-Object { $_ -lt $uiFlow.ProposalIndex }).Count -eq 0) { throw 'Browser did not read the Story library before the UI proposal.' }
  if (@($sourceReadIndexes | Where-Object { $_ -gt $uiFlow.HistoryIndex -and $_ -lt $pilotFlow.ProposalIndex }).Count -eq 0) { throw 'Browser did not open the Pilot source picker after the UI history flow.' }
  if ($uiFlow.ConfirmIndex -le $uiFlow.ProposalIndex -or $pilotFlow.ConfirmIndex -le $pilotFlow.ProposalIndex) {
    throw 'Story confirmation did not occur after proposal generation.'
  }
  $providerFlows = Get-StoryProviderFlowWindows $records $baseUrl $targetId $sessionId
  $uiConfirmRecord = $records[$uiFlow.ConfirmIndex]
  $pilotConfirmRecord = $records[$pilotFlow.ConfirmIndex]
  $providerFlows.ui | Add-Member -NotePropertyName story_id -NotePropertyValue ([int]$uiConfirmRecord.response_story_id) -Force
  $providerFlows.ui | Add-Member -NotePropertyName story_version_id -NotePropertyValue ([int]$uiConfirmRecord.response_story_version_id) -Force
  $providerFlows.pilot | Add-Member -NotePropertyName story_id -NotePropertyValue ([int]$pilotConfirmRecord.response_story_id) -Force
  $providerFlows.pilot | Add-Member -NotePropertyName story_version_id -NotePropertyValue ([int]$pilotConfirmRecord.response_story_version_id) -Force
  return $providerFlows
}

function Assert-StoryBrowserInteractions([object[]]$records, [string]$targetId, [string]$sessionId) {
  $matches = @($records | Where-Object { $_.kind -eq 'browser_story_interactions' })
  if (-not [string]::IsNullOrWhiteSpace($targetId) -or -not [string]::IsNullOrWhiteSpace($sessionId)) {
    $matches = @($matches | Where-Object {
      $_.target_id -eq $targetId -and $_.session_id -eq $sessionId
    })
  }
  if ($matches.Count -ne 1) { throw 'Browser did not retain one dedicated Story interaction audit record.' }
  $steps = @($matches[0].steps)
  $required = @('ui-library', 'ui-source-picker', 'ui-generate', 'ui-confirm', 'pilot-entry', 'pilot-source-picker', 'pilot-generate', 'pilot-confirm')
  $lastIndex = -1
  foreach ($step in $required) {
    $index = [Array]::IndexOf([string[]]$steps, $step)
    if ($index -lt 0) { throw "Browser did not execute required Story interaction: $step" }
    if ($index -le $lastIndex) { throw 'Browser Story interactions were not recorded in the required UI-then-Pilot order.' }
    $lastIndex = $index
  }
}

function Assert-StoryScreenshotMatrix([string]$directory, [string]$manifestPath) {
  if ([string]::IsNullOrWhiteSpace($directory) -or -not (Test-Path -LiteralPath $directory)) {
    throw 'ScreenshotDirectory is required and must exist.'
  }
  $required = @(
    '01-story-library.png',
    '02-source-picker.png',
    '03-source-preview.png',
    '04-generated-draft.png',
    '05-confirmation.png',
    '06-history.png',
    '07-source-changed.png',
    '08-pilot-entry.png',
    '09-pilot-source-choice.png',
    '10-pilot-history.png'
  )
  Add-Type -AssemblyName System.Drawing
  $matrix = @()
  foreach ($name in $required) {
    $path = Join-Path $directory $name
    if (-not (Test-Path -LiteralPath $path)) { throw "Required Story screenshot is missing: $name" }
    $image = $null
    try {
      $image = [System.Drawing.Image]::FromFile($path)
      if ($image.Width -lt 1440 -or $image.Height -lt 900 -or $image.Height -gt 1400) {
        throw "Story screenshot must be a single wide viewport (1440x900 through 1400px tall): $name"
      }
      $matrix += [pscustomobject]@{
        file = $name
        width = $image.Width
        height = $image.Height
        sha256 = Get-Sha256 $path
        visual_review = 'operator-required'
      }
    } finally {
      if ($null -ne $image) { $image.Dispose() }
    }
  }
  $output = if ([string]::IsNullOrWhiteSpace($manifestPath)) { Join-Path $directory 'story-screenshot-matrix.json' } else { $manifestPath }
  [IO.File]::WriteAllText(
    $output,
    ($matrix | ConvertTo-Json -Depth 3),
    [Text.UTF8Encoding]::new($false)
  )
  return $matrix
}

if ($ValidateAudit) {
  if ([string]::IsNullOrWhiteSpace($AuditPath) -or [string]::IsNullOrWhiteSpace($ExpectedBaseUrl)) {
    throw 'Audit validation requires AuditPath and ExpectedBaseUrl.'
  }
  Assert-AuditorSucceeded ([pscustomobject]@{ HasExited = $true; ExitCode = $AuditorExitCode })
  $records = Read-BrowserRecords $AuditPath
  [void](Assert-StoryBrowserSequence $records $ExpectedBaseUrl $ExpectedTargetId $ExpectedSessionId)
  Assert-StoryBrowserInteractions $records $ExpectedTargetId $ExpectedSessionId
  exit 0
}

if ($ValidateProviderConfig) {
  if ([string]::IsNullOrWhiteSpace($ProviderConfigPath)) { throw 'ProviderConfigPath is required.' }
  [void](Get-ProviderEndpoints $ProviderConfigPath)
  exit 0
}

if ($ValidateProviderEgress) {
  if ([string]::IsNullOrWhiteSpace($ProviderAuditPath) -or [string]::IsNullOrWhiteSpace($ProviderAllowlistPath) -or [string]::IsNullOrWhiteSpace($BrowserAuditPath) -or [string]::IsNullOrWhiteSpace($StoryAttemptAuditPath) -or [string]::IsNullOrWhiteSpace($ExpectedBaseUrl) -or [string]::IsNullOrWhiteSpace($ExpectedTargetId) -or [string]::IsNullOrWhiteSpace($ExpectedSessionId)) {
    throw 'Provider egress validation requires ProviderAuditPath, ProviderAllowlistPath, BrowserAuditPath, StoryAttemptAuditPath, ExpectedBaseUrl, ExpectedTargetId, and ExpectedSessionId.'
  }
  $providers = @(Get-Content -LiteralPath $ProviderAllowlistPath -Raw | ConvertFrom-Json)
  $records = Read-BrowserRecords $BrowserAuditPath
  $flows = Assert-StoryBrowserSequence $records $ExpectedBaseUrl $ExpectedTargetId $ExpectedSessionId
  Assert-StoryBrowserInteractions $records $ExpectedTargetId $ExpectedSessionId
  $attempts = Assert-StoryAttemptPersistence $flows (Read-StoryAttemptAudit $StoryAttemptAuditPath)
  Assert-ProviderEgress $providers $ProviderAuditPath $flows $attempts
  exit 0
}

if ($ValidateAttemptPersistence) {
  if ([string]::IsNullOrWhiteSpace($BrowserAuditPath) -or [string]::IsNullOrWhiteSpace($StoryAttemptAuditPath) -or [string]::IsNullOrWhiteSpace($ExpectedBaseUrl) -or [string]::IsNullOrWhiteSpace($ExpectedTargetId) -or [string]::IsNullOrWhiteSpace($ExpectedSessionId)) {
    throw 'Attempt persistence validation requires BrowserAuditPath, StoryAttemptAuditPath, ExpectedBaseUrl, ExpectedTargetId, and ExpectedSessionId.'
  }
  $records = Read-BrowserRecords $BrowserAuditPath
  $flows = Assert-StoryBrowserSequence $records $ExpectedBaseUrl $ExpectedTargetId $ExpectedSessionId
  Assert-StoryBrowserInteractions $records $ExpectedTargetId $ExpectedSessionId
  [void](Assert-StoryAttemptPersistence $flows (Read-StoryAttemptAudit $StoryAttemptAuditPath))
  exit 0
}

if ($ValidateScreenshotMatrix) {
  Assert-StoryScreenshotMatrix $ScreenshotDirectory $ScreenshotManifestPath | Out-Null
  exit 0
}

$primaryFailure = $null
try {
  New-Item -ItemType Directory -Force -Path $tempData | Out-Null
  $configPath = Join-Path $sourceData 'config.json'
  if (-not (Test-Path -LiteralPath $configPath)) { throw 'Configured real-provider config.json is required.' }
  Copy-Item -LiteralPath $configPath -Destination (Join-Path $tempData 'config.json')
  $providers = @(Get-ProviderEndpoints (Join-Path $tempData 'config.json'))
  if (@($providers | Where-Object { $_.Scheme -ne 'https' }).Count -gt 0) { throw 'Configured Provider endpoint must use HTTPS.' }
  $providers | ConvertTo-Json -Compress | Set-Content -LiteralPath $providerAllowlist -Encoding utf8

  $port = Get-FreePort
  $proxyPort = Get-FreePort
  $baseUrl = "http://127.0.0.1:$port"
  $env:OFFERPILOT_DATA = $tempData
  $env:HTTP_PROXY = "http://127.0.0.1:$proxyPort"
  $env:HTTPS_PROXY = "http://127.0.0.1:$proxyPort"
  $env:NO_PROXY = '127.0.0.1,localhost'
  if (-not (Test-Path -LiteralPath $projectOc)) { throw 'Project OfferPilot CLI runtime is missing.' }
  $proxy = Start-Process -FilePath $projectPython -WorkingDirectory $repo -WindowStyle Hidden -PassThru -ArgumentList @('scripts/provider-egress-proxy.py', '--port', $proxyPort, '--audit', $providerAudit, '--expected-endpoints-file', $providerAllowlist)
  $server = Start-Process -FilePath $projectOc -WorkingDirectory $repo -WindowStyle Hidden -PassThru -ArgumentList @('start', '--port', $port)
  Wait-ForHttpReady $server "$baseUrl/api/health" 'Isolated service' | Out-Null
  $seed = Seed-StoryContext
  $baseline = Get-ForbiddenDomainSnapshot
  $chromium = Find-Chromium
  $chromiumHandle = Start-DedicatedChromium $chromium @($port, $proxyPort)
  $browser = $chromiumHandle.Process
  $browserProfile = $chromiumHandle.Profile
  $cdpPort = $chromiumHandle.Port
  $browserStartupAttempt = $null
  $auditorHandle = Start-BrowserAuditor "http://127.0.0.1:$cdpPort" $baseUrl ([ref]$auditor)
  $auditor = $auditorHandle.Process
  if (-not [string]::IsNullOrWhiteSpace($SessionStatePath)) {
    $sessionState = [ordered]@{
      base_url = $baseUrl
      cdp_url = "http://127.0.0.1:$cdpPort"
      auditor_target_id = $auditorHandle.TargetId
      auditor_session_id = $auditorHandle.SessionId
      completion_signal_path = $CompletionSignalPath
      temp_data_path = $tempData
      browser_profile = $browserProfile
      chromium_startup_attempt = $chromiumHandle.Attempt
    } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($SessionStatePath, $sessionState, [Text.UTF8Encoding]::new($false))
  }
  Write-Host 'Dedicated browser target is ready in light mode at 1455x1200.'
  Write-Host 'Complete UI Story flow, then Pilot Story flow in the same target. Do not open another tab.'
  Write-Host 'Before each proposal, open the Story library and its source picker. Use selected seed note sources, edit a draft, confirm each Story Version, and reopen history.'
  Write-Host 'Save the ten reviewed light-mode 1455x1200 screenshots to ScreenshotDirectory before completing this run.'
  Write-Host 'Press Enter only after both flows and history reads have completed.'
  while ($true) {
    if (-not [string]::IsNullOrWhiteSpace($CompletionSignalPath) -and (Test-Path -LiteralPath $CompletionSignalPath)) { break }
    if ($server.HasExited) { throw 'Isolated service exited during browser acceptance.' }
    if ($browser.HasExited) { throw 'Dedicated browser exited during browser acceptance.' }
    if ($auditor.HasExited) { throw "Browser auditor exited during browser acceptance: $(Get-ProcessDiagnostic $auditorStdout $auditorStderr)" }
    if ([string]::IsNullOrWhiteSpace($CompletionSignalPath)) {
      try {
        if ([Console]::KeyAvailable) {
          $key = [Console]::ReadKey($true)
          if ($key.Key -eq [ConsoleKey]::Enter) { break }
        }
      } catch {
        throw 'Browser acceptance requires interactive input or CompletionSignalPath.'
      }
    }
    Start-Sleep -Milliseconds 250
  }
  New-Item -ItemType File -Force -Path $browserStop | Out-Null
  $auditor.WaitForExit(15000)
  Assert-AuditorSucceeded $auditor
  $records = Read-BrowserRecords
  $flows = Assert-StoryBrowserSequence $records $baseUrl $auditorHandle.TargetId $auditorHandle.SessionId
  Assert-StoryBrowserInteractions $records $auditorHandle.TargetId $auditorHandle.SessionId
  $attempts = Assert-StoryAttemptPersistence $flows (Get-PersistedStoryAttempts)
  Assert-ProviderEgress $providers $providerAudit $flows $attempts
  Assert-ForbiddenDomainsUnchanged $baseline (Get-ForbiddenDomainSnapshot)
  Assert-StoryScreenshotMatrix $ScreenshotDirectory $ScreenshotManifestPath | Out-Null
  $env:INTERVIEW_STORY_HARNESS_DB = Join-Path $tempData 'data.db'
  $verify = @'
import os, sqlite3
db = sqlite3.connect(os.environ["INTERVIEW_STORY_HARNESS_DB"])
rows = db.execute("select id, entrypoint, attempt_status, confirmed_story_id, confirmed_story_version_id from interview_story_proposal_attempts order by id").fetchall()
if len(rows) != 2 or {row[1] for row in rows} != {"ui", "pilot"} or any(row[2] != "confirmed" or row[3] is None or row[4] is None for row in rows):
    raise SystemExit("both UI and Pilot Story confirmations are required")
if len({row[3] for row in rows}) != 2 or len({row[4] for row in rows}) != 2:
    raise SystemExit("UI and Pilot must confirm distinct Stories and Versions")
story_count = db.execute("select count(*) from interview_stories").fetchone()[0]
version_rows = db.execute("select id from interview_story_versions order by id").fetchall()
if story_count != 2 or len(version_rows) != 2:
    raise SystemExit("browser acceptance must create exactly two Stories and two Versions")
for (version_id,) in version_rows:
    link_count = db.execute("select count(*) from interview_story_version_evidence_links where story_version_id = ?", (version_id,)).fetchone()[0]
    if link_count < 1:
        raise SystemExit("each confirmed Story Version requires persisted evidence links")
'@
  Invoke-IsolatedPython 'story-confirmation-verification' $verify | Out-Null
  Write-Host 'Story browser acceptance passed.'
} catch {
  $primaryFailure = $_
}
finally {
  $cleanupErrors = [System.Collections.Generic.List[string]]::new()
  $cleanupProcesses = [System.Collections.Generic.List[object]]::new()
  if ($null -ne $browserStop) {
    try { New-Item -ItemType File -Force -Path $browserStop -ErrorAction Stop | Out-Null }
    catch { $cleanupErrors.Add('browser auditor stop signal') }
  }
  $processesToClean = @(
    [pscustomobject]@{ Process = $auditor; Label = 'browser auditor' },
    [pscustomobject]@{ Process = $browser; Label = 'dedicated browser' },
    [pscustomobject]@{ Process = $server; Label = 'isolated service' },
    [pscustomobject]@{ Process = $proxy; Label = 'provider proxy' }
  )
  if ($null -ne $browserStartupAttempt) {
    $processesToClean = @(
      [pscustomobject]@{ Process = $browserStartupAttempt; Label = 'browser startup attempt' }
      $processesToClean
    )
  }
  foreach ($item in $processesToClean) {
    $processId = if ($null -eq $item.Process) { $null } else { [int]$item.Process.Id }
    try { Stop-Tree $item.Process $item.Label }
    catch { $cleanupErrors.Add([string]$item.Label) }
    $cleanupProcesses.Add([pscustomobject]@{
      label = [string]$item.Label
      process_id = $processId
      exited = ($null -eq $processId -or $null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue))
    })
  }
  try {
    $env:OFFERPILOT_DATA = $previousData
    $env:HTTP_PROXY = $previousHttpProxy
    $env:HTTPS_PROXY = $previousHttpsProxy
    $env:NO_PROXY = $previousNoProxy
  } catch {
    $cleanupErrors.Add('process environment restoration')
  }
  try { Remove-IsolatedTempData }
  catch { $cleanupErrors.Add('isolated acceptance data') }
  if (-not [string]::IsNullOrWhiteSpace($CleanupAuditPath)) {
    try {
      [IO.File]::WriteAllText(
        [IO.Path]::GetFullPath($CleanupAuditPath),
        (([ordered]@{ processes = @($cleanupProcesses) }) | ConvertTo-Json -Depth 3),
        [Text.UTF8Encoding]::new($false)
      )
    } catch {
      $cleanupErrors.Add('cleanup audit')
    }
  }
  if ($null -ne $primaryFailure) {
    if ($cleanupErrors.Count -gt 0) {
      [Console]::Error.WriteLine("Story browser acceptance cleanup also failed for: $($cleanupErrors -join ', ').")
    }
    throw $primaryFailure
  }
  if ($cleanupErrors.Count -gt 0) {
    throw "Story browser acceptance cleanup failed for: $($cleanupErrors -join ', ')."
  }
}
