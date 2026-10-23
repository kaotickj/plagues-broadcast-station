"""Disposable installer lifecycle/security checks; no real user paths or GUI."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if sys.platform == "linux":
    sys.path.insert(0, str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location("pbs_installer", ROOT / "scripts/linux_installer.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    build_spec = importlib.util.spec_from_file_location("pbs_package_builder", ROOT / "scripts/build_linux_installer.py")
    package_builder = importlib.util.module_from_spec(build_spec)
    build_spec.loader.exec_module(package_builder)


@unittest.skipUnless(sys.platform == "linux", "Linux installer")
class LinuxInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="station installer spaces $ ' ")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'Application with spaces \\ " % $ `'
        self.data = self.base / "Existing session with spaces"
        for name, data in {"session.json": b'{"playlist":["unchanged"]}',
                           "broadcast-profile.json": b'{"schema":2,"active_slot":1,"credential_revision":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}',
                           "lyrics/song.json": b'{"qc":"approved","words":["lyrics"]}',
                           "lyrics/history/previous.json": b"previous lyrics",
                           "model-cache/model.bin": b"existing model weights",
                           "settings/models.json": b'{"python":"keep this runtime"}',
                           "artwork/cover.png": b"art", "playlists/show.m3u": b"#EXTM3U\n/keep/music.mp3\n"}.items():
            path = self.data / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        from gnb_player.lyrics import Cue, LyricsDocument, fingerprint, save_lyrics
        from gnb_player.lyric_review import document_revision, edit_document, save_review_document
        self.review_source = self.data / "recordings/reviewed recording.wav"
        self.review_source.parent.mkdir()
        self.review_source.write_bytes(b"disposable recording bound to an explicit manual approval")
        self.approved_path = self.data / "lyrics/manually-approved.json"
        review = LyricsDocument([Cue("Operator checked these words", 1, 3)], 4,
                                source_sha256=fingerprint(self.review_source),
                                quality={"status": "review", "reasons": [{"code": "fixture", "message": "Retain the original QC finding"}]},
                                metadata={"model_evidence": {"id": "preserve-this-evidence"}})
        save_lyrics(review, self.approved_path)
        approved = save_review_document(review, self.approved_path, self.review_source, document_revision(review), approve=True)
        draft = edit_document(approved, [(1, 3, "Operator edited a different draft")])
        save_review_document(draft, self.data / "lyrics/edited-draft.json", self.review_source, None)
        self.before = self.snapshot()
        self.source = (ROOT / "scripts/linux_installer.py").read_text()

    def snapshot(self):
        return {p.relative_to(self.data).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.data.rglob("*") if p.is_file()}

    def package(self, version, *, link=False, broken_binary=False):
        bundle = self.base / ("bundle-" + version)
        bundle.mkdir()
        binary = bundle / "PlaguesBroadcastStation"
        binary.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                          f"version = {version!r}\n"
                          "if sys.argv[1:] == ['--version']:\n"
                          + ("    raise SystemExit(17)\n" if broken_binary else
                             "    print(\"Plague's Broadcast Station \" + version)\n    raise SystemExit(0)\n")
                          + "open(os.environ['PBS_TEST_CAPTURE'], 'w').write(json.dumps(sys.argv[1:]))\n")
        binary.chmod(0o755)
        (bundle / "content.txt").write_text(version)
        if link:
            (bundle / "content-link.txt").symlink_to("content.txt")
        files = installer.inventory(bundle)
        manifest = {"schema": 1, "app_id": installer.APP_ID, "version": version,
                    "release": version, "architecture": platform.machine(), "platform": "Linux",
                    "source_commit": "a" * 40, "source_tree_sha256": "b" * 64, "source_dirty": False,
                    "bundle_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(), "files": files}
        (bundle / ".pbs-release.json").write_text(json.dumps(manifest))
        archive = self.base / (version + ".tar.gz")
        with tarfile.open(archive, "w:gz", dereference=False) as package:
            for path in bundle.iterdir():
                package.add(path, arcname=path.name)
        content = archive.read_bytes()
        prefix = b"#!/bin/sh\nfixture installer header\n"
        archive.write_bytes(prefix + content)
        return {"archive": str(archive), "offset": len(prefix), "sha256": hashlib.sha256(content).hexdigest(),
                "architecture": platform.machine(), "release": version}

    def run_install(self, payload, *extra):
        return installer.main(["--install-dir", str(self.root), "--data-dir", str(self.data), *extra], payload, self.source)

    def state(self):
        return json.loads((self.root / "install.json").read_text())

    def test_fresh_upgrade_reinstall_and_rollback_keep_all_data(self):
        first = self.package("1.0", link=True)
        second = self.package("2.0")
        self.assertEqual(self.run_install(first), 0)
        self.assertEqual(self.run_install(first), 0)
        self.assertIsNone(self.state()["previous"])
        self.assertEqual(installer.main(["--install-dir", str(self.root)], second, self.source), 0)
        self.assertEqual(self.state()["previous"], "1.0")
        self.assertEqual(self.run_install(second), 0)
        self.assertEqual(self.state()["previous"], "1.0")
        self.assertEqual(installer.main(["--install-dir", str(self.root), "--rollback"]), 0)
        self.assertEqual(self.state()["current"], "1.0")
        self.assertEqual(self.state()["previous"], "2.0")
        self.assertEqual(self.snapshot(), self.before)
        self.assertTrue((self.root / "releases/1.0/content-link.txt").is_symlink())
        from gnb_player.lyrics import fingerprint, load_lyrics
        from gnb_player.lyric_review import is_approved
        self.assertTrue(is_approved(load_lyrics(self.approved_path), fingerprint(self.review_source)))
        self.assertEqual(load_lyrics(self.data / "lyrics/edited-draft.json").human_review["status"], "draft")

    def test_portable_archive_preserves_native_layout_modes_links_and_manifest(self):
        self.package("portable-fixture", link=True)
        bundle = self.base / "bundle-portable-fixture"
        manifest = json.loads((bundle / ".pbs-release.json").read_text())
        archive = self.base / "portable.tar.gz"
        package_builder.write_bundle_archive(bundle, manifest, archive, prefix="PlaguesBroadcastStation/")
        with tarfile.open(archive) as package:
            binary = package.getmember("PlaguesBroadcastStation/PlaguesBroadcastStation")
            self.assertTrue(binary.mode & 0o111)
            link = package.getmember("PlaguesBroadcastStation/content-link.txt")
            self.assertTrue(link.issym())
            self.assertEqual(link.linkname, "content.txt")
            self.assertEqual(json.load(package.extractfile("PlaguesBroadcastStation/.pbs-release.json")), manifest)

    def test_stable_launcher_version_status_and_exact_arguments(self):
        self.assertEqual(self.run_install(self.package("1.0")), 0)
        launcher = self.root / "bin/plagues-broadcast-station"
        result = subprocess.run([str(launcher), "--version"], check=True, capture_output=True, text=True)
        self.assertIn("1.0; source aaaaaaaaaaaa", result.stdout)
        result = subprocess.run([str(launcher), "--installer-info"], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["data_dir"], str(self.data))
        capture = self.base / "captured arguments.json"
        profile = str(self.base / 'literal $(touch HACKED) `whoami` " %.json')
        subprocess.run([str(launcher), "--profile", profile], check=True,
                       env={**os.environ, "PBS_TEST_CAPTURE": str(capture)}, cwd=self.base)
        self.assertEqual(json.loads(capture.read_text()), ["--data-dir", str(self.data), "--profile", profile])
        self.assertFalse((self.base / "HACKED").exists())
        refused = subprocess.run([str(launcher), "--data-dir", "another session"], capture_output=True)
        self.assertEqual(refused.returncode, 1)

    def test_interruption_before_atomic_activation_keeps_current_and_retry_works(self):
        self.assertEqual(self.run_install(self.package("1.0")), 0)
        second = self.package("2.0")
        original = installer.write_json
        def fail_state(path, data):
            if path.name == "install.json":
                raise OSError("simulated interrupted activation")
            return original(path, data)
        with patch.object(installer, "write_json", side_effect=fail_state):
            self.assertEqual(self.run_install(second), 1)
        self.assertEqual(self.state()["current"], "1.0")
        self.assertFalse(list(self.root.glob(".staging-*")))
        self.assertTrue((self.root / "releases/2.0").exists())
        self.assertEqual(self.run_install(second), 0)
        self.assertEqual(self.snapshot(), self.before)

    def test_interrupted_rollback_keeps_current(self):
        self.assertEqual(self.run_install(self.package("1.0")), 0)
        self.assertEqual(self.run_install(self.package("2.0")), 0)
        with patch.object(installer, "write_json", side_effect=OSError("simulated disk error")):
            self.assertEqual(installer.main(["--install-dir", str(self.root), "--rollback"]), 1)
        self.assertEqual(self.state()["current"], "2.0")
        self.assertEqual(self.snapshot(), self.before)

    def test_abrupt_process_exit_before_activation_keeps_current(self):
        self.assertEqual(self.run_install(self.package("1.0")), 0)
        payload = self.package("2.0")
        code = ("import importlib.util,json,os,sys\n"
                "spec=importlib.util.spec_from_file_location('installer',sys.argv[1])\n"
                "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
                "m.write_json=lambda *args:os._exit(92)\n"
                "m.main(['--install-dir',sys.argv[2]],json.loads(sys.argv[3]),open(sys.argv[1]).read())\n")
        process = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts/linux_installer.py"),
                                  str(self.root), json.dumps(payload)])
        self.assertEqual(process.returncode, 92)
        self.assertEqual(self.state()["current"], "1.0")
        self.assertEqual(self.run_install(payload), 0)
        self.assertEqual(self.snapshot(), self.before)

    def test_corrupt_and_truncated_payload_do_not_activate(self):
        self.assertEqual(self.run_install(self.package("1.0")), 0)
        payload = self.package("2.0")
        archive = Path(payload["archive"])
        for content in (archive.read_bytes()[:-9], b"not a package"):
            archive.write_bytes(content)
            self.assertEqual(self.run_install(payload), 1)
            self.assertEqual(self.state()["current"], "1.0")
        self.assertEqual(self.snapshot(), self.before)

    def test_payload_hash_valid_but_invalid_tar_fails_safely(self):
        self.assertEqual(self.run_install(self.package("1.0")), 0)
        payload = self.package("2.0")
        archive = Path(payload["archive"])
        archive.write_bytes(b"not a gzip tar")
        payload["offset"] = 0
        payload["sha256"] = installer.digest(archive)
        self.assertEqual(self.run_install(payload), 1)
        self.assertEqual(self.state()["current"], "1.0")

    def test_failed_native_preflight_keeps_current(self):
        self.assertEqual(self.run_install(self.package("1.0")), 0)
        self.assertEqual(self.run_install(self.package("2.0", broken_binary=True)), 1)
        self.assertEqual(self.state()["current"], "1.0")

    def test_rejects_unknown_directory_marker_and_symlink(self):
        payload = self.package("1.0")
        self.root.mkdir()
        (self.root / "keep.txt").write_text("unknown content")
        self.assertEqual(self.run_install(payload), 1)
        self.assertEqual((self.root / "keep.txt").read_text(), "unknown content")
        (self.root / ".pbs-installation").write_text("different owner")
        self.assertEqual(self.run_install(payload), 1)
        other_root = self.base / "link root"
        other_root.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            with installer.locked_root(other_root, create=True):
                pass

    def test_refuses_different_data_path_and_nested_data(self):
        payload = self.package("1.0")
        self.assertEqual(self.run_install(payload), 0)
        self.assertEqual(installer.main(["--install-dir", str(self.root), "--data-dir", str(self.base / "other")], payload, self.source), 1)
        for root, data in ((self.base / "new root", self.base / "new root/session"),
                           (self.data / "application", self.data)):
            self.assertEqual(installer.main(["--install-dir", str(root), "--data-dir", str(data)], payload, self.source), 1)
        self.assertEqual(self.snapshot(), self.before)

    def test_first_install_requires_explicit_data_without_creating_root(self):
        self.assertEqual(installer.main(["--install-dir", str(self.root)], self.package("1.0"), self.source), 1)
        self.assertFalse(self.root.exists())

    def test_invalid_provenance_and_modified_release_refused(self):
        self.assertEqual(self.run_install(self.package("1.0")), 0)
        release = self.root / "releases/1.0"
        manifest = json.loads((release / ".pbs-release.json").read_text())
        manifest["source_commit"] = "unknown"
        (release / ".pbs-release.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "provenance"):
            installer.verify_release(release)
        manifest["source_commit"] = "a" * 40
        (release / ".pbs-release.json").write_text(json.dumps(manifest))
        (release / "content.txt").write_text("modified")
        self.assertEqual(self.run_install(self.package("2.0")), 0)
        self.assertEqual(installer.main(["--install-dir", str(self.root), "--rollback"]), 1)
        self.assertEqual(self.state()["current"], "2.0")

    def test_archive_path_link_and_special_file_escape_refused(self):
        for name, kind, link in (("../outside", tarfile.REGTYPE, ""), ("/absolute", tarfile.REGTYPE, ""),
                                  ("link", tarfile.SYMTYPE, "../outside"), ("link", tarfile.SYMTYPE, "/tmp/outside"),
                                  ("hardlink", tarfile.LNKTYPE, "outside"), ("pipe", tarfile.FIFOTYPE, "")):
            with self.subTest(name=name, kind=kind, link=link):
                archive = self.base / "unsafe.tar.gz"
                with tarfile.open(archive, "w:gz") as package:
                    entry = tarfile.TarInfo(name)
                    entry.type, entry.linkname = kind, link
                    package.addfile(entry, io.BytesIO())
                stage = self.base / "unsafe stage"
                stage.mkdir(exist_ok=True)
                with self.assertRaises(ValueError):
                    installer.extract_payload(archive, 0, installer.digest(archive), stage)
                self.assertEqual(list(stage.iterdir()), [])

    def test_nested_symlink_archive_entry_refused(self):
        archive = self.base / "unsafe.tar.gz"
        with tarfile.open(archive, "w:gz") as package:
            link = tarfile.TarInfo("link")
            link.type, link.linkname = tarfile.SYMTYPE, "safe"
            package.addfile(link)
            package.addfile(tarfile.TarInfo("link/file"), io.BytesIO())
        stage = self.base / "unsafe stage"
        stage.mkdir()
        with self.assertRaisesRegex(ValueError, "nested"):
            installer.extract_payload(archive, 0, installer.digest(archive), stage)

    def test_desktop_opt_in_stable_exec_and_refusal_of_existing_entry(self):
        desktop = self.base / "disposable XDG/applications/plagues-broadcast-station.desktop"
        first = self.package("1.0")
        second = self.package("2.0")
        self.assertEqual(self.run_install(first, "--desktop-file", str(desktop)), 0)
        original = desktop.read_bytes()
        self.assertIn(b"bin/plagues-broadcast-station", original)
        self.assertEqual(self.run_install(second, "--desktop-file", str(desktop)), 0)
        self.assertEqual(desktop.read_bytes(), original)
        if shutil.which("desktop-file-validate"):
            subprocess.run(["desktop-file-validate", str(desktop)], check=True, capture_output=True)
        if shutil.which("gio"):
            capture = self.base / "desktop captured arguments.json"
            launched = subprocess.run(["gio", "launch", str(desktop)], capture_output=True,
                                      env={**os.environ, "PBS_TEST_CAPTURE": str(capture)}, timeout=15)
            self.assertEqual(launched.returncode, 0, launched.stderr.decode(errors="replace"))
            for _ in range(40):
                if capture.exists():
                    break
                time.sleep(0.05)
            self.assertEqual(json.loads(capture.read_text()), ["--data-dir", str(self.data)])
        desktop.write_text("[Desktop Entry]\nName=Unrelated app\n")
        self.assertEqual(self.run_install(first, "--desktop-file", str(desktop)), 1)
        self.assertEqual(self.state()["current"], "2.0")
        self.assertIn("Unrelated app", desktop.read_text())

    def test_lock_and_symlink_release_ownership(self):
        payload = self.package("1.0")
        self.assertEqual(self.run_install(payload), 0)
        with installer.locked_root(self.root):
            self.assertEqual(self.run_install(payload), 1)
        (self.root / "releases/escape").symlink_to(self.data, target_is_directory=True)
        with self.assertRaises(ValueError):
            installer.release_path(self.root, "escape")
        with self.assertRaises(ValueError):
            installer.release_path(self.root, "../escape")

    def legacy_entry(self, desktop, *, name="Plague's Broadcast Station", data=None, command=None):
        executable = str(self.base / "Portable build/PlaguesBroadcastStation")
        command = command or f'"{executable}" --data-dir "{data or self.data}"'
        content = ("[Desktop Entry]\nVersion=1.0\nType=Application\n" + "Name=" + name + "\nExec=" + command +
                   "\nPath=/chosen/existing working directory\nIcon=plagues-broadcast-station\nTerminal=false\nCategories=AudioVideo;Player;\nStartupNotify=true\n")
        desktop.write_text(content)
        return content.encode()

    def test_explicit_legacy_adoption_backs_up_and_preserves_icon_working_directory(self):
        desktop = self.base / "Existing station.desktop"
        original = self.legacy_entry(desktop)
        payload = self.package("1.0")
        self.assertEqual(self.run_install(payload, "--desktop-file", str(desktop)), 1)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.run_install(payload, "--desktop-file", str(desktop), "--adopt-desktop-entry"), 0)
        backups = list(self.base.glob("Existing station.desktop.pbs-before-managed-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        current = desktop.read_text()
        self.assertIn("Icon=plagues-broadcast-station\n", current)
        self.assertIn("Path=/chosen/existing working directory\n", current)
        self.assertIn("StartupNotify=true\n", current)
        self.assertNotIn("Portable build", current)
        self.assertIn("bin/plagues-broadcast-station", current)
        self.assertEqual(self.run_install(payload, "--desktop-file", str(desktop), "--adopt-desktop-entry"), 0)
        self.assertEqual(desktop.read_text(), current)
        self.assertEqual(len(list(self.base.glob("Existing station.desktop.pbs-before-managed-*"))), 1)
        self.assertEqual(self.snapshot(), self.before)

    def test_legacy_adoption_refuses_wrong_identity_shell_or_session(self):
        payload = self.package("1.0")
        desktop = self.base / "Existing station.desktop"
        for values in ({"name": "Another app"}, {"command": 'sh -c "something"'},
                       {"data": self.base / "different session"}):
            with self.subTest(values=values):
                original = self.legacy_entry(desktop, **values)
                self.assertEqual(self.run_install(payload, "--desktop-file", str(desktop), "--adopt-desktop-entry"), 1)
                self.assertEqual(desktop.read_bytes(), original)
                self.assertFalse(list(self.base.glob("*.pbs-before-managed-*")))
                self.assertFalse(self.root.exists())

    def test_failed_desktop_migration_preserves_legacy_and_can_retry(self):
        desktop = self.base / "Existing station.desktop"
        original = self.legacy_entry(desktop)
        payload = self.package("1.0")
        with patch.object(installer, "write_desktop", side_effect=OSError("simulated desktop write failure")):
            self.assertEqual(self.run_install(payload, "--desktop-file", str(desktop), "--adopt-desktop-entry"), 1)
        self.assertEqual(self.state()["current"], "1.0")
        self.assertEqual(desktop.read_bytes(), original)
        self.assertEqual(self.run_install(payload, "--desktop-file", str(desktop), "--adopt-desktop-entry"), 0)
        self.assertEqual(self.snapshot(), self.before)

    def test_legacy_backup_is_never_overwritten(self):
        desktop = self.base / "Existing station.desktop"
        original = self.legacy_entry(desktop)
        backup = desktop.with_name(desktop.name + ".pbs-before-managed-" + hashlib.sha256(original).hexdigest()[:12])
        backup.write_bytes(b"unknown existing backup")
        self.assertEqual(self.run_install(self.package("1.0"), "--desktop-file", str(desktop), "--adopt-desktop-entry"), 1)
        self.assertEqual(backup.read_bytes(), b"unknown existing backup")
        self.assertEqual(desktop.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
