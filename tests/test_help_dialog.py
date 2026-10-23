"""Operator help is informational, bounded and independent of app lifecycle."""
import json
import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from gnb_player import __version__, help_dialog


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def frozen_manifest(monkeypatch, tmp_path, payload):
    executable = tmp_path / 'PlaguesBroadcastStation'
    executable.touch()
    manifest = tmp_path / '.pbs-release.json'
    manifest.write_bytes(payload)
    monkeypatch.setattr(help_dialog.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(help_dialog.sys, 'executable', str(executable))
    return manifest


def test_help_has_runtime_version_save_workflow_and_actual_locations(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(help_dialog.sys, 'frozen', False, raising=False)
    data_dir = tmp_path / 'station data'
    dialog = help_dialog.HelpDialog(data_dir=data_dir)
    try:
        content = dialog.text.toPlainText()
        assert f'Running app version: {__version__}' in content
        assert 'Build identity: unrecorded (source or unpackaged run).' in content
        assert str(data_dir / 'session.json') in content
        assert str(data_dir / 'lyrics-cache' / 'lyrics' / '<audio SHA256>.json') in content
        assert 'without a filename picker' in content
        assert 'Export lyrics… is a separate action' in content
        assert 'A saved draft is not approval' in content
        assert 'Approve for airing do not publish lyrics to a website' in content
        assert 'no automatic website or AzuraCast synchronization' in content
        assert 'independent' in content and 'conflict' in content and 'older window' in content
        assert dialog.text.isReadOnly() and not dialog.isModal()
        assert dialog.objectName() == 'help_dialog'
        assert dialog.text.objectName() == 'help_text'
        assert dialog.findChild(QPushButton, 'help_close').text() == 'Close'
        assert not data_dir.exists()
    finally:
        dialog.close()


def test_help_preserves_literal_tilde_in_the_host_data_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(help_dialog.sys, 'frozen', False, raising=False)
    host_data_dir = Path('~') / 'station'
    content = help_dialog._help_text(host_data_dir)
    actual_location = host_data_dir.resolve()
    assert actual_location == tmp_path / '~' / 'station'
    assert 'Station data directory: ' + str(actual_location) in content
    assert str(actual_location / 'session.json') in content
    assert str(actual_location / 'lyrics-cache' / 'lyrics' / '<audio SHA256>.json') in content
    assert not host_data_dir.exists()


def test_frozen_manifest_reports_only_recorded_identity(qt_app, tmp_path, monkeypatch):
    manifest = {
        'schema': 1, 'app_id': 'plagues-broadcast-station',
        'version': __version__, 'release': 'fixture-release',
        'source_commit': 'a' * 40, 'source_tree_sha256': 'b' * 64,
        'bundle_sha256': 'c' * 64, 'source_dirty': True,
        'unrelated_private_field': 'PRIVATE_VALUE_MUST_NOT_APPEAR',
    }
    path = frozen_manifest(monkeypatch, tmp_path, json.dumps(manifest).encode())
    before = path.read_bytes()
    dialog = help_dialog.HelpDialog(data_dir=tmp_path / 'data')
    try:
        content = dialog.text.toPlainText()
        assert 'Release ID: fixture-release' in content
        assert 'Source commit: ' + 'a' * 40 in content
        assert 'Source tree SHA-256: ' + 'b' * 64 in content
        assert 'Package content SHA-256: ' + 'c' * 64 in content
        assert 'Build recorded uncommitted source changes: yes' in content
        assert 'not a fresh integrity check' in content
        assert 'PRIVATE_VALUE_MUST_NOT_APPEAR' not in content
        assert path.read_bytes() == before
    finally:
        dialog.close()


@pytest.mark.parametrize('payload', [b'broken', b'[]', b'{}', b'\xff',
    b'{"schema":true,"app_id":"plagues-broadcast-station"}',
    b'{"schema":1,"app_id":"another-app"}'])
def test_invalid_manifest_uses_honest_fallback(monkeypatch, tmp_path, payload):
    frozen_manifest(monkeypatch, tmp_path, payload)
    assert 'Build identity: unrecorded' in help_dialog._build_details()


def test_missing_oversized_and_linked_manifests_are_not_read(monkeypatch, tmp_path):
    path = frozen_manifest(monkeypatch, tmp_path, b'{}')
    monkeypatch.setattr(help_dialog, 'MAX_RELEASE_MANIFEST_BYTES', 16)
    path.write_bytes(b' ' * 17)
    assert help_dialog._release_manifest() is None
    path.unlink()
    assert help_dialog._release_manifest() is None
    if hasattr(os, 'symlink'):
        target = tmp_path / 'private.json'
        target.write_text('{"private":"do not read"}')
        try:
            path.symlink_to(target)
        except OSError:
            return
        assert help_dialog._release_manifest() is None


def test_manifest_version_does_not_replace_running_version(monkeypatch, tmp_path):
    manifest = {'schema': 1, 'app_id': 'plagues-broadcast-station',
                'version': 'fixture-different-version', 'release': 'fixture-release'}
    frozen_manifest(monkeypatch, tmp_path, json.dumps(manifest).encode())
    details = help_dialog._build_details()
    assert 'Manifest version: fixture-different-version' in details
    assert 'this window is running ' + __version__ in details


@pytest.mark.parametrize('action', ['close', 'escape'])
def test_help_close_and_escape_keep_parent_open(qt_app, tmp_path, action):
    parent = QWidget()
    parent.show()
    dialog = help_dialog.HelpDialog(parent, data_dir=tmp_path / 'data')
    dialog.show()
    qt_app.processEvents()
    assert parent.isVisible() and dialog.isVisible()
    if action == 'close':
        dialog.findChild(QPushButton, 'help_close').click()
    else:
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
    qt_app.processEvents()
    assert not dialog.isVisible() and parent.isVisible()
    dialog.show()
    qt_app.processEvents()
    assert dialog.isVisible() and parent.isVisible()
    dialog.close()
    parent.close()
