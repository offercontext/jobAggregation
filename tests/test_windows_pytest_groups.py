from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
GROUPS = ("agent", "domain", "knowledge", "proposals", "misc")


def _powershell() -> str:
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell is required for the Windows pytest gate tests")
    return executable


def _write_group_results(result_dir: Path, *, marker_overrides: dict[str, object] | None = None) -> None:
    marker_overrides = marker_overrides or {}
    manifest_lines: list[str] = []
    for index, group in enumerate(GROUPS):
        node_id = f"tests/test_{group}.py::test_{group}"
        manifest_lines.append(node_id)
        collect_path = result_dir / f"{group}.collect.txt"
        junit_path = result_dir / f"{group}.junit.xml"
        collect_path.write_text(node_id + "\n", encoding="utf-8")
        junit_path.write_text(
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
            f"<testsuites><testsuite name=\"{group}\" tests=\"1\" failures=\"0\" "
            "errors=\"0\" skipped=\"0\">"
            f"<testcase classname=\"tests.test_{group}\" name=\"test_{group}\"/>"
            "</testsuite></testsuites>",
            encoding="utf-8",
        )
        marker = {
            "marker_version": 1,
            "status": "completed",
            "group": group,
            "exit_code": 0,
            "collected_count": 1,
            "test_count": 1,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "collect_sha256": hashlib.sha256(collect_path.read_bytes()).hexdigest(),
            "junit_sha256": hashlib.sha256(junit_path.read_bytes()).hexdigest(),
        }
        if group == "misc":
            marker.update(marker_overrides)
        (result_dir / f"{group}.complete.json").write_text(
            json.dumps(marker), encoding="utf-8"
        )
    (result_dir / "full-manifest.txt").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")


def _aggregate(result_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "windows-pytest-groups.ps1"),
            "-Aggregate",
            "-ResultDir",
            str(result_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_pytest_group_aggregate_requires_every_completion_marker(tmp_path: Path) -> None:
    _write_group_results(tmp_path)
    (tmp_path / "misc.complete.json").unlink()

    result = _aggregate(tmp_path)

    assert result.returncode != 0
    assert "completion marker" in (result.stdout + result.stderr)


def test_pytest_group_aggregate_rejects_marker_summary_mismatch(tmp_path: Path) -> None:
    _write_group_results(tmp_path, marker_overrides={"test_count": 2})

    result = _aggregate(tmp_path)

    assert result.returncode != 0
    assert "test count" in (result.stdout + result.stderr).lower()


def test_pytest_group_aggregate_accepts_only_completed_matching_markers(tmp_path: Path) -> None:
    _write_group_results(tmp_path)

    result = _aggregate(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "coverage matches 5 tests" in result.stdout


def test_pytest_group_aggregate_accepts_windows_backslash_manifest_node_ids(tmp_path: Path) -> None:
    _write_group_results(tmp_path)
    manifest_path = tmp_path / "full-manifest.txt"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace("/", "\\"),
        encoding="utf-8",
    )

    result = _aggregate(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "coverage matches 5 tests" in result.stdout


def test_pytest_group_aggregate_treats_parameter_node_ids_as_case_sensitive(
    tmp_path: Path,
) -> None:
    _write_group_results(tmp_path)
    lower = "tests/test_misc.py::test_misc[1e-7]"
    upper = "tests/test_misc.py::test_misc[1E-7]"
    collect_path = tmp_path / "misc.collect.txt"
    junit_path = tmp_path / "misc.junit.xml"
    collect_path.write_text(f"{lower}\n{upper}\n", encoding="utf-8")
    junit_path.write_text(
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
        "<testsuites><testsuite name=\"misc\" tests=\"2\" failures=\"0\" "
        "errors=\"0\" skipped=\"0\">"
        "<testcase classname=\"tests.test_misc\" name=\"test_misc[1e-7]\"/>"
        "<testcase classname=\"tests.test_misc\" name=\"test_misc[1E-7]\"/>"
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    marker_path = tmp_path / "misc.complete.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker.update(
        {
            "collected_count": 2,
            "test_count": 2,
            "collect_sha256": hashlib.sha256(collect_path.read_bytes()).hexdigest(),
            "junit_sha256": hashlib.sha256(junit_path.read_bytes()).hexdigest(),
        }
    )
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    manifest_path = tmp_path / "full-manifest.txt"
    manifest = manifest_path.read_text(encoding="utf-8").replace(
        "tests/test_misc.py::test_misc", f"{lower}\n{upper}"
    )
    manifest_path.write_text(manifest, encoding="utf-8")

    result = _aggregate(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "coverage matches 6 tests" in result.stdout
