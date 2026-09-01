from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "windows-vitest-groups.ps1"
GROUPS = (
    "components-core",
    "components-chat",
    "components-interview",
    "components-offer",
    "components-support",
    "features",
    "layout",
    "lib",
    "services",
    "theme",
)


def _powershell_available() -> bool:
    return shutil.which("powershell.exe") is not None


def _run_gate(
    result_dir: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    script: Path = SCRIPT,
    repository_root: Path | str | None = ROOT,
) -> subprocess.CompletedProcess[str]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *arguments,
        "-ResultDir",
        str(result_dir),
    ]
    if repository_root is not None:
        command.extend(("-RepositoryRoot", str(repository_root)))
    return subprocess.run(
        command,
        cwd=repository_root if repository_root is not None else ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _make_gate_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository_root = tmp_path / "fixture-repository"
    web_root = repository_root / "web"
    source_root = web_root / "src"
    script = repository_root / "scripts" / SCRIPT.name
    script.parent.mkdir(parents=True)
    source_root.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    for relative_path in (
        "components/core.test.ts",
        "components/chatpanel/chat.test.ts",
        "components/interview/interview.test.ts",
        "components/offer/offer.test.ts",
        "components/k/support.test.ts",
        "features/features.test.ts",
        "layout/layout.test.ts",
        "lib/lib.test.ts",
        "services/services.test.ts",
        "theme/theme.test.ts",
    ):
        test_file = source_root / relative_path
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("it('fixture passes', () => expect(true).toBe(true));\n", encoding="utf-8")
    (source_root / "main.tsx").write_text("export {};\n", encoding="utf-8")
    for name in ("package.json", "package-lock.json", "tsconfig.json", "tsconfig.node.json", "vite.config.ts"):
        (web_root / name).write_text(f"fixture: {name}\n", encoding="utf-8")
    return repository_root, script, web_root


def _fake_npm_environment(tmp_path: Path, manifest_path: Path, web_root: Path) -> dict[str, str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_runner = fake_bin / "fake-vitest.ps1"
    fake_runner.write_text(
        """
param()
$allArguments = @($args)
$outputArgument = $allArguments | Where-Object { $_ -like '--outputFile=*' } | Select-Object -First 1
$outputPath = $outputArgument.Substring('--outputFile='.Length)
if ($env:FAKE_VITEST_FAIL -eq '1') { exit 17 }
if ($env:FAKE_VITEST_NO_REPORT -eq '1') {
    Write-Output 'fake Vitest intentionally omitted its report'
    exit 23
}
$manifest = Get-Content $env:FAKE_VITEST_MANIFEST -Raw -Encoding utf8 | ConvertFrom-Json
$files = @($manifest.groups | ForEach-Object { $_.PSObject.Properties[$env:FAKE_VITEST_GROUP].Value })
if ($env:FAKE_VITEST_OMIT_FILE -eq '1') { $files = @() }
$results = @($files | ForEach-Object {
    @{
        name = Join-Path $env:FAKE_VITEST_WEB_ROOT $_
        assertionResults = @(@{ fullName = 'fake passes'; status = 'passed' })
    }
})
@{
    success = $true
    numPendingTests = 0
    numTodoTests = 0
    numTotalTests = $results.Count
    testResults = $results
} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $outputPath -Encoding utf8
""".strip(),
        encoding="utf-8",
    )
    (fake_bin / "npm.cmd").write_text(
        '@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0fake-vitest.ps1" %*\r\nexit /b %ERRORLEVEL%\r\n',
        encoding="ascii",
    )
    environment = os.environ.copy()
    environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]
    environment["FAKE_VITEST_MANIFEST"] = str(manifest_path)
    environment["FAKE_VITEST_WEB_ROOT"] = str(web_root)
    return environment


@pytest.mark.skipif(not _powershell_available(), reason="Windows PowerShell is required")
def test_frontend_group_rejects_missing_expected_file(tmp_path: Path) -> None:
    repository_root, script, web_root = _make_gate_fixture(tmp_path)
    result_dir = tmp_path / "results"
    collected = _run_gate(result_dir, "-Collect", script=script, repository_root=repository_root)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    environment = _fake_npm_environment(tmp_path, result_dir / "frontend-manifest.json", web_root)
    environment["FAKE_VITEST_GROUP"] = "theme"
    environment["FAKE_VITEST_OMIT_FILE"] = "1"

    run = _run_gate(result_dir, "-Group", "theme", env=environment, script=script, repository_root=repository_root)

    assert run.returncode != 0


@pytest.mark.skipif(not _powershell_available(), reason="Windows PowerShell is required")
def test_frontend_gate_accepts_repository_root_with_trailing_separator(tmp_path: Path) -> None:
    repository_root, script, _ = _make_gate_fixture(tmp_path)
    result_dir = tmp_path / "results"
    collected = _run_gate(
        result_dir,
        "-Collect",
        script=script,
        repository_root=f"{repository_root}{os.sep}",
    )

    assert collected.returncode == 0, collected.stdout + collected.stderr
    manifest = json.loads((result_dir / "frontend-manifest.json").read_text(encoding="utf-8-sig"))
    assert all(file_path.startswith("src\\") for file_path in manifest["files"])


@pytest.mark.skipif(not _powershell_available(), reason="Windows PowerShell is required")
def test_frontend_gate_defaults_repository_root_from_script_location(tmp_path: Path) -> None:
    repository_root, script, _ = _make_gate_fixture(tmp_path)
    result_dir = tmp_path / "results"
    collected = _run_gate(result_dir, "-Collect", script=script, repository_root=None)

    assert collected.returncode == 0, collected.stdout + collected.stderr


@pytest.mark.skipif(not _powershell_available(), reason="Windows PowerShell is required")
def test_frontend_gate_rejects_adjacent_prefix_result_root(tmp_path: Path) -> None:
    repository_root, script, web_root = _make_gate_fixture(tmp_path)
    result_dir = tmp_path / "results"
    collected = _run_gate(result_dir, "-Collect", script=script, repository_root=repository_root)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    environment = _fake_npm_environment(tmp_path, result_dir / "frontend-manifest.json", web_root)
    environment["FAKE_VITEST_GROUP"] = "theme"
    environment["FAKE_VITEST_WEB_ROOT"] = str(web_root.parent / "web-evil")

    run = _run_gate(
        result_dir,
        "-Group",
        "theme",
        env=environment,
        script=script,
        repository_root=repository_root,
    )

    assert run.returncode != 0
    assert "outside repository root" in (run.stdout + run.stderr)


@pytest.mark.skipif(not _powershell_available(), reason="Windows PowerShell is required")
def test_frontend_aggregate_removes_stale_success_before_validation(tmp_path: Path) -> None:
    repository_root, script, _ = _make_gate_fixture(tmp_path)
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    aggregate = result_dir / "frontend.aggregate.json"
    aggregate.write_text('{"status":"completed"}', encoding="utf-8")

    run = _run_gate(result_dir, "-Aggregate", script=script, repository_root=repository_root)

    assert run.returncode != 0
    assert not aggregate.exists()


@pytest.mark.skipif(not _powershell_available(), reason="Windows PowerShell is required")
def test_frontend_aggregate_rejects_results_from_previous_manifest(tmp_path: Path) -> None:
    repository_root, script, web_root = _make_gate_fixture(tmp_path)
    result_dir = tmp_path / "results"
    collected = _run_gate(result_dir, "-Collect", script=script, repository_root=repository_root)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    manifest_path = result_dir / "frontend-manifest.json"
    environment = _fake_npm_environment(tmp_path, manifest_path, web_root)

    for group in GROUPS:
        environment["FAKE_VITEST_GROUP"] = group
        run = _run_gate(result_dir, "-Group", group, env=environment, script=script, repository_root=repository_root)
        assert run.returncode == 0, run.stdout + run.stderr

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    manifest["source_hash"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    aggregate = _run_gate(result_dir, "-Aggregate", env=environment, script=script, repository_root=repository_root)

    assert aggregate.returncode != 0


@pytest.mark.skipif(not _powershell_available(), reason="Windows PowerShell is required")
def test_frontend_group_does_not_reuse_old_result_after_failed_run(tmp_path: Path) -> None:
    repository_root, script, web_root = _make_gate_fixture(tmp_path)
    result_dir = tmp_path / "results"
    collected = _run_gate(result_dir, "-Collect", script=script, repository_root=repository_root)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    environment = _fake_npm_environment(tmp_path, result_dir / "frontend-manifest.json", web_root)
    environment["FAKE_VITEST_GROUP"] = "theme"

    first = _run_gate(result_dir, "-Group", "theme", env=environment, script=script, repository_root=repository_root)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (result_dir / "theme.results.json").exists()
    assert (result_dir / "theme.complete.json").exists()

    environment["FAKE_VITEST_FAIL"] = "1"
    second = _run_gate(result_dir, "-Group", "theme", env=environment, script=script, repository_root=repository_root)

    assert second.returncode != 0
    assert not (result_dir / "theme.results.json").exists()
    assert not (result_dir / "theme.complete.json").exists()


@pytest.mark.skipif(not _powershell_available(), reason="Windows PowerShell is required")
def test_frontend_group_preserves_diagnostics_when_vitest_omits_report(tmp_path: Path) -> None:
    repository_root, script, web_root = _make_gate_fixture(tmp_path)
    result_dir = tmp_path / "results"
    collected = _run_gate(result_dir, "-Collect", script=script, repository_root=repository_root)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    environment = _fake_npm_environment(tmp_path, result_dir / "frontend-manifest.json", web_root)
    environment["FAKE_VITEST_GROUP"] = "theme"
    environment["FAKE_VITEST_NO_REPORT"] = "1"

    run = _run_gate(result_dir, "-Group", "theme", env=environment, script=script, repository_root=repository_root)

    output = run.stdout + run.stderr
    run_log = result_dir / "theme.run.txt"
    assert run.returncode != 0
    assert "theme did not produce a Vitest JSON report (exit code 23)" in output
    assert run_log.exists()
    assert "fake Vitest intentionally omitted its report" in run_log.read_text(encoding="utf-16")


@pytest.mark.skipif(not _powershell_available(), reason="Windows PowerShell is required")
def test_frontend_aggregate_rejects_new_test_file_after_group_runs(tmp_path: Path) -> None:
    repository_root, script, web_root = _make_gate_fixture(tmp_path)
    result_dir = tmp_path / "results"
    collected = _run_gate(result_dir, "-Collect", script=script, repository_root=repository_root)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    environment = _fake_npm_environment(tmp_path, result_dir / "frontend-manifest.json", web_root)
    for group in GROUPS:
        environment["FAKE_VITEST_GROUP"] = group
        run = _run_gate(result_dir, "-Group", group, env=environment, script=script, repository_root=repository_root)
        assert run.returncode == 0, run.stdout + run.stderr

    added_test = web_root / "src" / "__frontend_gate_probe__.test.ts"
    try:
        added_test.write_text("it('probe', () => expect(true).toBe(true));\n", encoding="utf-8")
        aggregate = _run_gate(result_dir, "-Aggregate", env=environment, script=script, repository_root=repository_root)
        assert aggregate.returncode != 0
    finally:
        added_test.unlink(missing_ok=True)


@pytest.mark.skipif(not _powershell_available(), reason="Windows PowerShell is required")
def test_frontend_aggregate_rejects_production_source_change_after_group_runs(tmp_path: Path) -> None:
    repository_root, script, web_root = _make_gate_fixture(tmp_path)
    result_dir = tmp_path / "results"
    collected = _run_gate(result_dir, "-Collect", script=script, repository_root=repository_root)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    environment = _fake_npm_environment(tmp_path, result_dir / "frontend-manifest.json", web_root)
    for group in GROUPS:
        environment["FAKE_VITEST_GROUP"] = group
        run = _run_gate(result_dir, "-Group", group, env=environment, script=script, repository_root=repository_root)
        assert run.returncode == 0, run.stdout + run.stderr

    production_source = web_root / "src" / "main.tsx"
    original = production_source.read_bytes()
    try:
        production_source.write_bytes(original + b"\n// temporary frontend gate probe\n")
        aggregate = _run_gate(result_dir, "-Aggregate", env=environment, script=script, repository_root=repository_root)
        assert aggregate.returncode != 0
    finally:
        production_source.write_bytes(original)
