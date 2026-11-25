#!/usr/bin/env python3
"""Verify candidate Linux artifacts using disposable sessions and installations."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def check_packages(previous_installer=None, report_dir=None):
    if platform.system() != "Linux":
        raise ValueError("These native checks require Linux.")
    spec = importlib.util.spec_from_file_location("installer_test_fixtures", ROOT / "tests/test_linux_installer.py")
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    case = fixtures.LinuxInstallerTests()
    case.setUp()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    package = ROOT / "artifacts" / f"PlaguesBroadcastStation-{version}-linux-{platform.machine()}.run"
    portable = package.with_name(package.stem + "-portable.tar.gz")
    manifest = json.loads(package.with_suffix(".manifest.json").read_text())
    report = {"candidate_version": version, "package_sha256": fixtures.installer.digest(package),
              "portable_sha256": fixtures.installer.digest(portable)}
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_MEDIA_BACKEND": "ffmpeg",
                   "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"}

    def command(*arguments, check=True, env=None):
        result = subprocess.run(list(map(str, arguments)), check=False, capture_output=True,
                                text=True, timeout=180, env=env or environment, cwd=case.base)
        if check and result.returncode:
            raise AssertionError((list(map(str, arguments)), result.returncode, result.stdout[-2000:], result.stderr[-2000:]))
        return result

    def unchanged():
        assert case.snapshot() == case.before, "Installer changed persisted data content or modification times."
        from gnb_player.lyrics import fingerprint, load_lyrics
        from gnb_player.lyric_review import is_approved
        assert is_approved(load_lyrics(case.approved_path), fingerprint(case.review_source))

    def gui_smoke(binary, label, extra=()):
        process = subprocess.Popen([str(binary), "--data-dir", str(case.base / (label + " GUI data")), *extra],
                                   env=environment, cwd=case.base, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(3)
            if process.poll() is not None:
                output, error = process.communicate()
                raise AssertionError((label, process.returncode, output[-2000:], error[-2000:]))
            report[label] = "stayed running offscreen for 3 seconds; only own disposable process terminated"
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    try:
        command(package, "--help")
        desktop = case.base / "Disposable XDG/applications/Station.desktop"
        command(package, "--install-dir", case.root, "--data-dir", case.data, "--desktop-file", desktop)
        launcher = case.root / "bin/plagues-broadcast-station"
        assert case.state()["current"] == manifest["release"] and case.state()["previous"] is None
        assert str(case.data) == json.loads(command(launcher, "--installer-info").stdout)["data_dir"]
        report["stable_version"] = command(launcher, "--version").stdout.strip()
        assert f"Plague's Broadcast Station {version} " in report["stable_version"]
        command(launcher, "--help")
        command(package, "--install-dir", case.root)
        assert case.state()["previous"] is None
        assert (case.root / "app-icon.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        if shutil.which("desktop-file-validate"):
            command("desktop-file-validate", desktop)
        unchanged()
        report["fresh_repeat"] = "passed; session/model/art/lyric approval and review-history bytes+mtimes unchanged"

        legacy = case.base / "Known portable station.desktop"
        original = case.legacy_entry(legacy)
        command(package, "--install-dir", case.root, "--desktop-file", legacy, "--adopt-desktop-entry")
        backups = list(case.base.glob("Known portable station.desktop.pbs-before-managed-*"))
        assert len(backups) == 1 and backups[0].read_bytes() == original
        assert "Icon=plagues-broadcast-station\n" in legacy.read_text()
        assert "Path=/chosen/existing working directory\n" in legacy.read_text()
        unchanged()
        report["legacy_adoption"] = "passed; exact backup, stable launcher and preserved icon/working directory"

        if previous_installer:
            upgrade_root = case.base / "Actual previous version upgrade"
            command(Path(previous_installer).resolve(), "--install-dir", upgrade_root, "--data-dir", case.data)
            stable = upgrade_root / "bin/plagues-broadcast-station"
            old_version = command(stable, "--version").stdout.strip()
            old_release = json.loads(command(stable, "--installer-info").stdout)["current"]["release"]
            unchanged()
            command(package, "--install-dir", upgrade_root)
            status = json.loads(command(stable, "--installer-info").stdout)
            assert status["current"]["release"] == manifest["release"] and status["previous"]["release"] == old_release
            command(stable, "--installer-rollback")
            assert command(stable, "--version").stdout.strip() == old_version
            unchanged()
            command(stable, "--installer-rollback")
            assert command(stable, "--version").stdout.strip() == report["stable_version"]
            unchanged()
            report["real_upgrade_rollback"] = {"previous_version": old_version, "previous_release": old_release,
                                               "result": "actual previous native package upgraded, rolled back and returned; all persisted bytes+mtimes unchanged"}

        damaged = case.base / "damaged installer.run"
        shutil.copyfile(package, damaged)
        with damaged.open("r+b") as handle:
            handle.seek(-1, os.SEEK_END)
            handle.truncate()
        damaged.chmod(0o755)
        refused = command(damaged, "--install-dir", case.root, check=False)
        assert refused.returncode == 1 and "checksum failed" in refused.stderr
        assert case.state()["current"] == manifest["release"]
        unchanged()
        report["damaged_package"] = "refused before activation; current selection and all persisted data preserved"

        bundle = case.root / "releases" / manifest["release"]
        assert fixtures.installer.verify_release(bundle) == manifest
        unpacked = case.base / "Portable extracted with spaces"
        unpacked.mkdir()
        fixtures.installer.extract_payload(portable, 0, fixtures.installer.digest(portable), unpacked)
        portable_bundle = unpacked / "PlaguesBroadcastStation"
        assert fixtures.installer.verify_release(portable_bundle) == manifest
        assert command(portable_bundle / "PlaguesBroadcastStation", "--version").stdout.strip() == f"Plague's Broadcast Station {version}"
        command(portable_bundle / "run-linux.sh", "--help")
        report["portable"] = "native modes/symlinks/full manifest verified; extracted executable and portable launcher passed"
        from check_credential_bundle import check_credentials
        report["credential_bundle"] = check_credentials(bundle / "PlaguesBroadcastStation", "Linux")
        report["protected_profile_preservation"] = "opaque profile marker preserved byte-for-byte and mtime; same OS user/canonical data-dir retains native namespace; no real credentials accessed"
        gui_smoke(bundle / "PlaguesBroadcastStation", "installed_operator")
        gui_smoke(bundle / "PlaguesBroadcastStation", "installed_model_setup", ("--setup-models",))
        gui_smoke(portable_bundle / "PlaguesBroadcastStation", "portable_operator")
        native_env = {**environment, "LD_LIBRARY_PATH": str(bundle / "_internal")}
        for name in ("ffmpeg", "ffprobe"):
            report[name] = command(bundle / "_internal/bin" / name, "-version", env=native_env).stdout.splitlines()[0]
        worker_check = "import sys;sys.path.insert(0,sys.argv[1]);import extract_worker;assert not any(n.startswith('PySide6') for n in sys.modules)"
        command(sys.executable, "-c", worker_check, bundle / "_internal/gnb_player")
        report["packaged_extraction_worker"] = "standalone import succeeded with copied shared-lock lyrics.py and no Qt/model initialization"
        events, result = case.base / "installer events.jsonl", case.base / "installer result.json"
        refused = command(bundle / "PlaguesBroadcastStation", "--model-install-worker", "--events-file", events,
                          "--result-file", result, check=False)
        assert refused.returncode == 1 and not result.exists()
        assert "explicit Install action" in json.loads(events.read_text().splitlines()[-1])["message"]
        report["source_manifest"] = {key: value for key, value in manifest.items() if key != "files"}
        report["model_downloads"] = "none; native model installer refused execution without an explicit install action"
        report["production_changes"] = "none; all installations, state, GUI processes and menu entries were disposable"
        destination = report_dir or ROOT / "build"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "native-package-checks.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        case.temporary.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-installer", type=Path, help="Actual earlier .run to verify a native upgrade/rollback")
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    check_packages(args.previous_installer, args.report_dir)
