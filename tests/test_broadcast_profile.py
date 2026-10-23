"""Native credentials are always fake here; never contact the user's keyring."""
import importlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gnb_player.broadcast_profile import BroadcastProfileStore, ProfileError, StreamProfile, SERVICE, native_backend


class FakeCredentials:
    def __init__(self):
        self.values = {}
        self.fail = None
        self.calls = []

    def _check(self, operation):
        self.calls.append(operation)
        if operation == self.fail:
            raise RuntimeError('NATIVE-EXCEPTION-SENTINEL rtmps://private.invalid/SECRET')

    def get_password(self, service, user):
        self._check('get')
        return self.values.get((service, user))

    def set_password(self, service, user, value):
        self._check('set')
        self.values[service, user] = value

    def delete_password(self, service, user):
        self._check('delete')
        self.values.pop((service, user), None)


@pytest.fixture
def store(tmp_path):
    backend = FakeCredentials()
    return BroadcastProfileStore(tmp_path / 'data with spaces', backend_factory=lambda: backend), backend


def profile(key='PRIVATE-KEY', destination='rtmps://user:password@host.invalid/app/FULL-URL-KEY?token=QUERY-SECRET'):
    return StreamProfile(destination, key, 1, 6500)


def test_fresh_start_is_lazy_and_restart_restores_full_protected_profile(store):
    saved, backend = store
    assert saved.load() is None and backend.calls == []
    value = profile()
    saved.save(value)
    marker=json.loads(saved.marker.read_text())
    assert set(marker)=={'schema','active_slot','credential_revision'} and marker['schema']==2 and marker['active_slot']==0
    assert len(marker['credential_revision'])==64
    assert value == BroadcastProfileStore(saved.data_dir, backend_factory=lambda: backend).load()
    assert 'PRIVATE' not in repr(value) and 'host.invalid' not in repr(value)
    plain = ''.join(p.read_text() for p in saved.data_dir.rglob('*') if p.is_file())
    for secret in ('PRIVATE-KEY', 'password', 'FULL-URL-KEY', 'QUERY-SECRET', 'host.invalid'):
        assert secret not in plain
    saved.save(profile('NEW-KEY'))
    assert json.loads(saved.marker.read_text())['active_slot'] == 1
    assert saved.load().stream_key == 'NEW-KEY'
    assert len(backend.values) == 2


@pytest.mark.parametrize('operation', ['get', 'set'])
def test_failed_update_retains_previous_selection_and_safe_error(store, operation):
    saved, backend = store
    saved.save(profile())
    marker = saved.marker.read_bytes()
    backend.fail = operation
    with pytest.raises(ProfileError) as caught:
        saved.save(profile('NEW-KEY'))
    assert 'SENTINEL' not in str(caught.value) and 'SECRET' not in str(caught.value)
    assert saved.marker.read_bytes() == marker
    backend.fail = None
    assert saved.load() == profile()


def test_failed_marker_write_keeps_prior_active_and_orphan_is_forgettable(store):
    saved, backend = store
    saved.save(profile())
    marker = saved.marker.read_bytes()
    def fail(*args): raise OSError('SECRET')
    saved._write_marker = fail
    with pytest.raises(ProfileError): saved.save(profile('SECOND'))
    assert saved.marker.read_bytes() == marker and saved.load() == profile()
    saved.forget()
    assert not saved.marker.exists() and not backend.values
    with pytest.raises(ProfileError): saved.save(profile('ORPHAN'))
    assert not saved.marker.exists() and backend.values
    saved.forget()  # Also cleans a failed first save with no marker.
    assert not backend.values
    saved.forget()  # Idempotent.


def test_bad_readback_or_interrupted_update_does_not_select_replacement(store):
    saved, backend = store
    saved.save(profile())
    marker = saved.marker.read_bytes()
    original = backend.get_password
    backend.get_password = lambda service,user:'different protected blob'
    with pytest.raises(ProfileError):saved.save(profile('UNVERIFIED'))
    assert saved.marker.read_bytes()==marker
    def interrupted(*args):raise KeyboardInterrupt('fixture only')
    backend.get_password = interrupted
    with pytest.raises(KeyboardInterrupt):saved.save(profile('INTERRUPTED'))
    assert saved.marker.read_bytes()==marker
    backend.get_password = original
    assert saved.load()==profile()  # Lock released, original selected.


def test_namespace_follows_actual_directory_including_a_literal_tilde(tmp_path,monkeypatch):
    from pathlib import Path
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path,'expanduser',lambda path:tmp_path.joinpath('simulated-home',*path.parts[1:])
                        if path.parts and path.parts[0]=='~' else path)
    backend=FakeCredentials()
    literal=BroadcastProfileStore('~/state',backend_factory=lambda:backend)
    alias=BroadcastProfileStore(tmp_path/'~'/'state',backend_factory=lambda:backend)
    expanded=BroadcastProfileStore(Path('~/state').expanduser(),backend_factory=lambda:backend)
    assert literal._account==alias._account and literal._account!=expanded._account
    literal.save(profile())
    assert alias.load()==profile() and not expanded.marker.exists()


@pytest.mark.parametrize('payload', [None, '{truncated', json.dumps({'schema': True}), profile()._encode('0'*64).replace('rtmps:', 'file:')])
def test_missing_or_corrupt_active_never_falls_back_to_previous_slot(store, payload):
    saved, backend = store
    saved.save(profile('OLD'))
    saved.save(profile('NEW'))
    account = saved._service(1), saved._username(1)
    if payload is None: backend.values.pop(account)
    else: backend.values[account] = payload
    with pytest.raises(ProfileError): saved.load()


@pytest.mark.parametrize('operation', ['get', 'delete'])
def test_forget_failure_is_truthful_and_other_data_dir_is_untouched(store, tmp_path, operation):
    saved, backend = store
    other = BroadcastProfileStore(tmp_path / 'other', backend_factory=lambda: backend)
    saved.save(profile()); other.save(profile('OTHER'))
    other_values = {key: value for key, value in backend.values.items() if key[1].startswith(other._account)}
    backend.fail = operation
    with pytest.raises(ProfileError) as caught: saved.forget()
    assert 'SENTINEL' not in str(caught.value)
    assert saved.marker.exists()
    backend.fail = None
    saved.forget()
    assert backend.values == other_values and other.load().stream_key == 'OTHER'


def test_forget_marker_removal_failure_does_not_claim_complete(store, monkeypatch):
    saved, backend = store
    saved.save(profile())
    original = type(saved.marker).unlink
    def fail(path, *args, **kwargs):
        if path == saved.marker: raise OSError('PRIVATE')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(saved.marker), 'unlink', fail)
    with pytest.raises(ProfileError) as caught: saved.forget()
    assert caught.value.credentials_deleted and saved.marker.exists() and not backend.values
    monkeypatch.setattr(type(saved.marker), 'unlink', original)
    saved.forget()
    assert not saved.marker.exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX subprocess lock, Windows branch separately mocked')
def test_cross_process_lock_refuses_interleaved_save_and_forget(store):
    saved, backend = store
    saved.save(profile())
    child_code = '''import sys
from pathlib import Path
from gnb_player.broadcast_profile import BroadcastProfileStore, StreamProfile, ProfileError
class Forbidden:
 def __call__(self): raise AssertionError('backend reached while locked')
s=BroadcastProfileStore(Path(sys.argv[1]),backend_factory=Forbidden())
for fn in (lambda:s.save(StreamProfile('rtmps://fake.invalid/app','FAKE')),s.forget):
 try:fn()
 except ProfileError as error:
  assert 'progress' in str(error)
 else:raise AssertionError('interleaved transaction permitted')
'''
    with saved._lock():
        result = subprocess.run([sys.executable, '-c', child_code, str(saved.data_dir)], capture_output=True, text=True,
                                env={**os.environ, 'PYTHONPATH':str(__import__('pathlib').Path(importlib.import_module('gnb_player').__path__[0]).parent)}, timeout=10)
    assert result.returncode == 0, result.stderr
    assert saved.load() == profile()


def test_windows_lock_uses_same_file_and_releases_on_failure(store, monkeypatch):
    saved, _ = store
    calls = []
    fake = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=lambda fd, kind, length:calls.append((kind,length)))
    monkeypatch.setitem(sys.modules,'msvcrt',fake)
    with patch('gnb_player.broadcast_profile.os.name','nt'):
        with pytest.raises(RuntimeError):
            with saved._lock():raise RuntimeError('fixture')
    assert calls == [(1,1),(2,1)]
    assert (saved.data_dir/'.broadcast-profile.lock').exists()


@pytest.mark.parametrize('platform,module,name', [('linux','keyring.backends.SecretService','Keyring'),('win32','keyring.backends.Windows','WinVaultKeyring')])
def test_backend_allowlist_uses_direct_native_class_only(platform,module,name,monkeypatch):
    sentinel = SimpleNamespace()
    monkeypatch.setitem(sys.modules,module,SimpleNamespace(**{name:lambda:sentinel}))
    monkeypatch.setattr(sys,'platform',platform)
    assert native_backend() is sentinel


def test_backend_absence_and_locked_restore_never_echo_native_error(store,monkeypatch):
    monkeypatch.setattr(sys,'platform','unsupported')
    with pytest.raises(ProfileError):native_backend()
    saved, backend = store
    saved.save(profile()); backend.fail='get'
    with pytest.raises(ProfileError) as caught:saved.load()
    assert 'NATIVE-EXCEPTION' not in str(caught.value) and 'SECRET' not in str(caught.value)


@pytest.mark.parametrize('destination', ['file:///tmp/key','rtmps://host.invalid/a#SECRET','rtmp://host.invalid:bad/app','rtmps://host.invalid/a b'])
def test_invalid_destination_is_not_saved_or_echoed(store,destination):
    saved, backend = store
    with pytest.raises(ProfileError) as caught:saved.save(profile(destination=destination))
    assert destination not in str(caught.value) and not backend.calls


def test_unknown_marker_or_symlink_is_not_overwritten(store,tmp_path):
    saved, backend = store
    saved.data_dir.mkdir()
    saved.marker.write_text('{"schema":3,"active_slot":0}')
    for fn in (saved.load,lambda:saved.save(profile()),saved.forget):
        with pytest.raises(ProfileError):fn()
    assert not backend.calls
    saved.marker.unlink(); outside=tmp_path/'keep'; outside.write_text('unchanged'); saved.marker.symlink_to(outside)
    with pytest.raises(ProfileError):saved.save(profile())
    assert outside.read_text()=='unchanged' and not backend.calls


@pytest.mark.parametrize('marker',['{truncated','{"schema":2,"active_slot":"broken"}'])
def test_explicit_forget_recovers_corruption_without_touching_other_namespace(store,tmp_path,marker):
    saved,backend=store
    other=BroadcastProfileStore(tmp_path/'other data',backend_factory=lambda:backend)
    saved.save(profile());other.save(profile('OTHER'))
    saved.marker.write_text(marker)
    with pytest.raises(ProfileError):saved.load()
    with pytest.raises(ProfileError):saved.save(profile('UNSAVED'))
    saved.forget()
    assert not saved.marker.exists() and other.load().stream_key=='OTHER'
    assert all(service.startswith(other._service(0).rsplit('.',1)[0]) for service,_ in backend.values)


def test_real_winvault_backend_with_fake_native_api_refuses_stale_compound_copy(tmp_path,monkeypatch):
    """Exercise keyring's actual Windows primary/compound behavior on Linux."""
    import copy
    from pathlib import Path
    import types
    import keyring
    class WinError(Exception):
        def __init__(self,func):self.winerror=1168;self.funcname=func
    class CredAPI:
        __name__='fake_win32cred'
        CRED_TYPE_GENERIC=1;CRED_PERSIST_ENTERPRISE=3;CRED_PERSIST_LOCAL_MACHINE=2
        def __init__(self):self.values={};self.writes=[]
        def CredRead(self,Type,TargetName):
            if TargetName not in self.values:raise WinError('CredRead')
            return copy.deepcopy(self.values[TargetName])
        def CredWrite(self,credential,flags):
            assert credential['Persist']==self.CRED_PERSIST_LOCAL_MACHINE
            credential=copy.deepcopy(credential);credential['CredentialBlob']=credential['CredentialBlob'].encode('utf-16')
            self.values[credential['TargetName']]=credential;self.writes.append(credential['TargetName'])
        def CredDelete(self,Type,TargetName):
            if TargetName not in self.values:raise WinError('CredDelete')
            del self.values[TargetName]
    api=CredAPI();win32=types.ModuleType('win32ctypes.pywin32');win32.pywintypes=SimpleNamespace(error=WinError);win32.win32cred=api
    package=types.ModuleType('win32ctypes');package.pywin32=win32
    monkeypatch.setitem(sys.modules,'win32ctypes',package);monkeypatch.setitem(sys.modules,'win32ctypes.pywin32',win32)
    name='keyring.backends.Windows'
    spec=importlib.util.spec_from_file_location(name,Path(keyring.__file__).parent/'backends/Windows.py')
    module=importlib.util.module_from_spec(spec);monkeypatch.setitem(sys.modules,name,module);spec.loader.exec_module(module)
    monkeypatch.setenv('KEYRING_PROPERTY_PERSIST','enterprise')
    def selected():
        with patch('gnb_player.broadcast_profile.sys.platform','win32'):return native_backend()
    saved=BroadcastProfileStore(tmp_path/'one',backend_factory=selected)
    other=BroadcastProfileStore(tmp_path/'two',backend_factory=selected)
    saved.save(profile('FIRST'));saved.save(profile('SECOND'));saved.save(profile('THIRD'))
    other.save(profile('OTHER'))
    assert len({saved._service(0),saved._service(1),other._service(0),other._service(1)})==4
    active=json.loads(saved.marker.read_text())['active_slot'];service=saved._service(active)
    # Real WinVault saves a stale compound copy on replacement even for the
    # same username. Simulate loss of its newer primary credential.
    assert selected().get_password(service,saved._username(active))==api.values[service]['CredentialBlob'].decode('utf-16')
    del api.values[service]
    assert 'FIRST' in selected().get_password(service,saved._username(active))
    with pytest.raises(ProfileError):saved.load()  # Revision token mismatches.
    saved.forget()
    assert other.load().stream_key=='OTHER'
    assert not any(saved._account in target for target in api.values)
