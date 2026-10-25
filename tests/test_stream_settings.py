"""Ordinary stream settings use dummy endpoints and never touch native secrets."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from gnb_player.broadcast_profile import (
    BroadcastProfileStore, ProfileError, StreamProfile, public_destination,
)


def no_credentials():
    raise AssertionError('Ordinary profile accessed native credential storage')


def profile(destination='rtmps://stream.example.invalid/live', key='DUMMY-KEY', bitrate=28500):
    return StreamProfile(destination, key, 1, bitrate)


def test_save_reopen_repeat_and_restart_are_independent_of_keyring(tmp_path):
    data = tmp_path / 'station'
    store = BroadcastProfileStore(data, backend_factory=no_credentials)
    assert store.load_settings() is None and not data.exists()
    store.save_settings(profile())
    saved = BroadcastProfileStore(data, backend_factory=no_credentials).load_settings()
    assert saved.profile == profile(key='') and saved.remember is False
    store.save_settings(profile(bitrate=34000))
    store.save_settings(profile(bitrate=34000))
    assert store.load_settings().profile.bitrate_kbps == 34000
    payload = json.loads(store.settings_file.read_text())
    assert set(payload) == {'schema', 'destination', 'picture_index', 'bitrate_kbps', 'remember'}
    assert 'DUMMY-KEY' not in store.settings_file.read_text()
    child = '''import sys
from gnb_player.broadcast_profile import BroadcastProfileStore
store=BroadcastProfileStore(sys.argv[1],backend_factory=lambda:(_ for _ in ()).throw(AssertionError('keyring reached')))
saved=store.load_settings()
assert saved.profile.destination=='rtmps://stream.example.invalid/live'
assert saved.profile.bitrate_kbps==34000 and saved.profile.stream_key=='' and not saved.remember
'''
    result = subprocess.run([sys.executable, '-c', child, str(data)], capture_output=True, text=True,
                            env={**__import__('os').environ, 'PYTHONPATH': str(Path(__import__('gnb_player').__path__[0]).parent)}, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('url', [
    'rtmps://user:password@example.invalid/live',
    'rtmps://example.invalid/live/INLINE-KEY',
    'rtmps://example.invalid/live%2fINLINE-KEY',
    'rtmps://example.invalid/live?key=QUERY-KEY',
    'rtmps://example.invalid/INLINE-KEY',
    'rtmps://example.invalid/%49NLINE-KEY',
    'rtmps://example.invalid/custom-app',
    'rtmps://example.invalid/live%3fkey%3dQUERY-KEY',
    'rtmps://example.invalid/live%23INLINE-KEY',
    'rtmps://user%3apassword%40example.invalid/live',
])
def test_credentials_and_inline_key_urls_require_explicit_protected_storage(tmp_path, url):
    store = BroadcastProfileStore(tmp_path, backend_factory=no_credentials)
    value = profile(url)
    assert not public_destination(url)
    with pytest.raises(ProfileError) as caught:
        store.save_settings(value)
    assert 'Remember in protected storage' in str(caught.value)
    assert not store.settings_file.exists() and 'INLINE-KEY' not in str(caught.value)
    store.save_settings(value, remember=True)
    saved = store.load_settings()
    assert saved.remember is True and saved.profile.destination == ''
    assert saved.profile.stream_key == '' and saved.profile.bitrate_kbps == 28500
    text = store.settings_file.read_text()
    for secret in ['password', 'INLINE-KEY', 'QUERY-KEY', 'DUMMY-KEY', 'example.invalid']:
        assert secret not in text


@pytest.mark.parametrize('bitrate', [999, 100001, True, '28500', 28500.0])
def test_invalid_update_preserves_previous_settings(tmp_path, bitrate):
    store = BroadcastProfileStore(tmp_path, backend_factory=no_credentials)
    store.save_settings(profile())
    before = store.settings_file.read_bytes()
    with pytest.raises(ProfileError):
        store.save_settings(profile(bitrate=bitrate))
    assert store.settings_file.read_bytes() == before


def test_new_ordinary_save_supersedes_selection_of_old_protected_data_without_access(tmp_path):
    store = BroadcastProfileStore(tmp_path, backend_factory=no_credentials)
    store.save_settings(profile(), remember=True)
    assert store.load_settings().remember is True
    store.save_settings(profile(bitrate=42000), remember=False)
    assert store.load_settings().remember is False
    assert store.load_settings().profile.bitrate_kbps == 42000
    store.forget_settings()
    store.forget_settings()
    assert store.load_settings() is None


@pytest.mark.parametrize('payload', ['broken', '{}', '{"schema":2}', '{"schema":true}'])
def test_corrupt_settings_fail_with_no_keyring_or_secret_echo(tmp_path, payload):
    store = BroadcastProfileStore(tmp_path, backend_factory=no_credentials)
    store.settings_file.write_text(payload)
    with pytest.raises(ProfileError):
        store.load_settings()
    with pytest.raises(ProfileError):
        store.save_settings(profile())
    assert store.settings_file.read_text() == payload


def test_ordinary_symlink_target_is_not_read_or_replaced(tmp_path):
    store = BroadcastProfileStore(tmp_path / 'station', backend_factory=no_credentials)
    store.data_dir.mkdir()
    target = tmp_path / 'private-target.json'
    target.write_text('PRIVATE SENTINEL')
    store.settings_file.symlink_to(target)
    for action in [store.load_settings, lambda: store.save_settings(profile()), store.forget_settings]:
        with pytest.raises(ProfileError):
            action()
    assert target.read_text() == 'PRIVATE SENTINEL'


def test_failed_ordinary_write_keeps_previous_profile_and_safe_error(tmp_path, monkeypatch):
    from gnb_player import broadcast_profile
    store = BroadcastProfileStore(tmp_path, backend_factory=no_credentials)
    store.save_settings(profile())
    before = store.settings_file.read_bytes()
    def fail(*args):
        raise OSError('DUMMY-KEY rtmps://PRIVATE.invalid/PRIVATE-KEY')
    monkeypatch.setattr(broadcast_profile, 'atomic_json', fail)
    with pytest.raises(ProfileError) as caught:
        store.save_settings(profile(bitrate=42000))
    assert store.settings_file.read_bytes() == before
    assert 'PRIVATE' not in str(caught.value) and 'DUMMY-KEY' not in str(caught.value)


def test_ordinary_load_preserves_settings_even_when_protected_backend_is_missing(tmp_path):
    store = BroadcastProfileStore(tmp_path, backend_factory=no_credentials)
    store.save_settings(profile(), remember=True)
    restarted = BroadcastProfileStore(tmp_path, backend_factory=no_credentials)
    saved = restarted.load_settings()
    assert saved.remember is True and saved.profile.destination == profile().destination
    assert saved.profile.bitrate_kbps == 28500 and saved.profile.stream_key == ''
    assert not store.marker.exists()  # Ordinary settings never invent a protected marker.


@pytest.mark.parametrize('app', ['', 'live', 'live2', 'app', 'application'])
def test_only_known_static_app_paths_are_ordinary_destinations(app):
    assert public_destination('rtmps://stream.example.invalid/' + app)


@pytest.mark.parametrize('key', ['live', 'l%69ve'])
def test_known_app_name_equal_to_entered_key_is_protected_only(tmp_path, key):
    store = BroadcastProfileStore(tmp_path, backend_factory=no_credentials)
    value = profile('rtmps://example.invalid/live', key=key)
    with pytest.raises(ProfileError, match='protected storage'):
        store.save_settings(value)
    assert not store.settings_file.exists()
    store.save_settings(value, remember=True)
    assert json.loads(store.settings_file.read_text())['destination'] == ''
    assert store.load_settings().remember is True


def test_ordinary_settings_do_not_create_protected_attempt_reference(tmp_path):
    store = BroadcastProfileStore(tmp_path, backend_factory=no_credentials)
    store.save_settings(profile())
    assert store.load_settings().remember is False
    assert not store.protected_attempt_file.exists()
