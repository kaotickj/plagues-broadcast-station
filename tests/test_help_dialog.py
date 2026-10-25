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


def test_topics_cover_setup_broadcast_profiles_and_models_without_external_access(qt_app, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(help_dialog.QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()))
    dialog = help_dialog.HelpDialog(data_dir=tmp_path / 'station')
    try:
        for topic, expected in [('setup', 'without installing lyric models'), ('models', 'Validate and use existing models'),
                                ('broadcast', '28,500 kbps is 28.5 Mbps'), ('profiles', 'Remember stream key in OS keyring'),
                                ('troubleshooting', 'locked or missing keyring'), ('version', __version__)]:
            dialog.show_topic(topic)
            assert expected in dialog.text.toPlainText()
        assert opened == []
        assert not (tmp_path / 'station').exists()
        dialog.show_topic('unknown')
        assert dialog.topics.currentData() == 'all'
        assert 'Running app version:' in dialog.text.toPlainText()
    finally:
        dialog.close()


def test_help_search_supports_next_wrap_and_missing_results(qt_app, tmp_path):
    dialog = help_dialog.HelpDialog(data_dir=tmp_path)
    try:
        dialog.show_topic('profiles')
        dialog.find.setText('profile')
        dialog.find_next()
        first = dialog.text.textCursor().selectionStart()
        assert dialog.text.textCursor().selectedText().lower() == 'profile'
        dialog.find_next()
        assert dialog.text.textCursor().selectionStart() > first
        cursor = dialog.text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        dialog.text.setTextCursor(cursor)
        dialog.find_next()
        assert dialog.text.textCursor().selectionStart() == first
        dialog.find.setText('NO_SUCH_TERM_IN_BUNDLED_HELP')
        dialog.find_next()
        assert 'No match' in dialog.search_status.text()
        dialog.find.clear()
        dialog.find_next()
        assert 'Enter a word' in dialog.search_status.text()
    finally:
        dialog.close()


@pytest.mark.parametrize('opened', [True, False])
def test_only_explicit_help_link_opens_fixed_official_url(qt_app, tmp_path, monkeypatch, opened):
    urls = []
    def open_url(url):
        urls.append(url.toString())
        return opened
    monkeypatch.setattr(help_dialog.QDesktopServices, 'openUrl', open_url)
    dialog = help_dialog.HelpDialog(data_dir=tmp_path)
    try:
        assert urls == []
        dialog.youtube_help.click()
        assert urls == ['https://support.google.com/youtube/answer/2853702?hl=en']
        assert ('Opened official' if opened else 'Could not open a browser') in dialog.search_status.text()
    finally:
        dialog.close()


def test_ctrl_f_focuses_search_and_escape_preserves_parent(qt_app, tmp_path):
    parent = QWidget()
    dialog = help_dialog.HelpDialog(parent, data_dir=tmp_path)
    parent.show(); dialog.show(); dialog.activateWindow(); qt_app.processEvents()
    try:
        dialog.text.setFocus()
        QTest.keyClick(dialog, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        qt_app.processEvents()
        assert dialog.find.hasFocus()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        qt_app.processEvents()
        assert not dialog.isVisible() and parent.isVisible()
    finally:
        dialog.close(); parent.close()


def test_enter_in_search_advances_one_match_without_activating_other_actions(qt_app, tmp_path, monkeypatch):
    urls = []
    monkeypatch.setattr(help_dialog.QDesktopServices, 'openUrl', lambda url: urls.append(url.toString()))
    dialog = help_dialog.HelpDialog(data_dir=tmp_path, topic='profiles')
    try:
        dialog.show(); dialog.activateWindow(); qt_app.processEvents()
        dialog.find.setText('profile')
        dialog.find.setFocus()
        QTest.keyClick(dialog.find, Qt.Key.Key_Return)
        qt_app.processEvents()
        first = dialog.text.textCursor().selectionStart()
        assert first == dialog.text.toPlainText().lower().index('profile')
        QTest.keyClick(dialog.find, Qt.Key.Key_Return)
        qt_app.processEvents()
        assert dialog.text.textCursor().selectionStart() == dialog.text.toPlainText().lower().index('profile', first + 1)
        assert dialog.isVisible() and urls == []
    finally:
        dialog.close()


def test_topic_headings_in_paths_or_release_names_do_not_split_topics(qt_app, tmp_path, monkeypatch):
    frozen_manifest(monkeypatch, tmp_path, json.dumps({
        'schema': 1, 'app_id': 'plagues-broadcast-station', 'release': 'BASIC USE',
    }).encode())
    location = tmp_path / 'WEBSITE AND RADIO STORES'
    dialog = help_dialog.HelpDialog(data_dir=location, topic='storage')
    try:
        assert str(location / 'session.json') in dialog.text.toPlainText()
        assert 'Existing native JSON' in dialog.text.toPlainText()
        dialog.show_topic('version')
        assert 'Release ID: BASIC USE' in dialog.text.toPlainText()
        dialog.show_topic('website')
        assert dialog.text.toPlainText().startswith('WEBSITE AND RADIO STORES')
        assert 'no automatic website' in dialog.text.toPlainText()
    finally:
        dialog.close()


def test_recovery_help_explains_separate_untimed_save_and_platform_limit(qt_app,tmp_path):
    dialog=help_dialog.HelpDialog(data_dir=tmp_path/'unused station')
    try:
        dialog.show_topic('recovery')
        text=dialog.text.toPlainText()
        assert 'FAILED RECOGNITION DRAFTS' in text
        assert 'Save draft text' in text and 'does not align' in text
        assert 'Program Output' in text and 'Linux' in text and 'Cancel keeps the buffer' in text
        assert not (tmp_path/'unused station').exists()
    finally:
        dialog.close()


def test_development_candidate_channel_matches_version_and_package_metadata(qt_app,tmp_path):
    import tomllib
    project=Path(__file__).resolve().parents[1]/'pyproject.toml'
    assert tomllib.loads(project.read_text())['project']['version']==__version__
    assert '.dev' in __version__
    dialog=help_dialog.HelpDialog(data_dir=tmp_path/'unused station')
    try:
        text=dialog.text.toPlainText()
        assert 'Running app version: '+__version__ in text
        assert 'development controlled-test candidate; not a stable release' in text
        assert not (tmp_path/'unused station').exists()
    finally:
        dialog.close()
