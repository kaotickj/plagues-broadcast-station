"""Scoped RC10 hotfix verification; generated releases only, no activation."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import rc10_hotfix as hotfix
import linux_installer as generic


def digest(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def release(tmp_path):
    bundle = tmp_path / 'retained rc10'
    guard = bundle / '_internal/gnb_player/extraction_backend/lyrics_review.py'
    guard.parent.mkdir(parents=True)
    binary = bundle / 'PlaguesBroadcastStation'
    binary.write_bytes(b'generated executable; never launched')
    binary.chmod(0o755)
    guard.write_bytes(b'original neighbour review\n')
    guard.chmod(0o644)
    other = bundle / '_internal/unchanged.bin'
    other.write_bytes(b'original unchanged native contents')
    other.chmod(0o644)
    files = generic.inventory(bundle)
    manifest = {'schema': 1, 'app_id': generic.APP_ID, 'version': '0.2.4rc10',
                'release': 'rc10-fixture', 'source_commit': 'a' * 40,
                'source_tree_sha256': 'b' * 64, 'source_dirty': False,
                'platform': 'Linux', 'architecture': 'fixture', 'files': files,
                'bundle_sha256': digest(json.dumps(files, sort_keys=True).encode())}
    raw = (json.dumps(manifest, indent=2) + '\n').encode()
    path = bundle / '.pbs-release.json'
    path.write_bytes(raw); path.chmod(0o644)
    approved = b'approved exact neighbour guard\n'
    policy = hotfix.Policy(release=manifest['release'], version=manifest['version'],
                           source_commit=manifest['source_commit'],
                           original_manifest_sha256=digest(raw), binary_sha256=digest(binary.read_bytes()),
                           guard_path=guard.relative_to(bundle).as_posix(),
                           original_guard_sha256=digest(guard.read_bytes()), approved_guard_sha256=digest(approved),
                           directory_mode=0o755)
    guard.write_bytes(approved)
    for directory in (bundle, *(p for p in bundle.rglob('*') if p.is_dir())):
        directory.chmod(0o755)
    return bundle, policy, raw


def all_bytes(bundle):
    return {p.relative_to(bundle).as_posix(): (p.read_bytes(), p.stat().st_mode & 0o7777)
            for p in bundle.rglob('*') if p.is_file() and not p.is_symlink()}


def test_exact_patch_verifies_read_only_and_does_not_weaken_normal_verification(release):
    bundle, policy, raw = release
    before = all_bytes(bundle)
    with pytest.raises(ValueError, match='Release verification failed'):
        generic.verify_release(bundle)
    verified = hotfix.verify_rc10_hotfix(bundle, policy=policy)
    assert verified.bundle == bundle
    assert verified.original_manifest_raw == raw
    assert verified.manifest['files'][policy.guard_path]['sha256'] == policy.original_guard_sha256
    assert verified.patched_manifest['files'][policy.guard_path]['sha256'] == policy.approved_guard_sha256
    assert verified.inventory == verified.patched_manifest['files']
    assert verified.receipt['unknown_differences'] == 'refused'
    assert verified.receipt['mode_pins']['provenance'].startswith('explicit readiness baseline')
    assert all_bytes(bundle) == before
    # Callers cannot mutate the verified cached original through a returned dict.
    detached = verified.manifest
    detached['files'][policy.guard_path]['sha256'] = 'c' * 64
    assert verified.manifest['files'][policy.guard_path]['sha256'] == policy.original_guard_sha256
    with pytest.raises(ValueError):
        generic.verify_release(bundle)


@pytest.mark.parametrize('mutation', ['other-content', 'binary', 'guard', 'unpatched', 'manifest-bytes',
                                     'missing', 'extra-file', 'empty-directory', 'file-mode',
                                     'guard-executable', 'directory-mode', 'manifest-mode'])
def test_unknown_differences_refused_without_any_writes(release, mutation):
    bundle, policy, raw = release
    guard = bundle / policy.guard_path
    if mutation == 'other-content': (bundle / '_internal/unchanged.bin').write_bytes(b'unknown native change')
    elif mutation == 'binary': (bundle / 'PlaguesBroadcastStation').write_bytes(b'other executable')
    elif mutation == 'guard': guard.write_bytes(b'almost approved patch plus unknown edit')
    elif mutation == 'unpatched': guard.write_bytes(b'original neighbour review\n')
    elif mutation == 'manifest-bytes': (bundle / '.pbs-release.json').write_bytes(raw + b' ')
    elif mutation == 'missing': (bundle / '_internal/unchanged.bin').unlink()
    elif mutation == 'extra-file': (bundle / 'unknown.py').write_bytes(b'unknown code')
    elif mutation == 'empty-directory': (bundle / 'unexpected').mkdir()
    elif mutation == 'file-mode': (bundle / '_internal/unchanged.bin').chmod(0o664)
    elif mutation == 'guard-executable': guard.chmod(0o755)
    elif mutation == 'directory-mode': (bundle / '_internal').chmod(0o775)
    elif mutation == 'manifest-mode': (bundle / '.pbs-release.json').chmod(0o600)
    before = all_bytes(bundle)
    with pytest.raises(hotfix.HotfixError): hotfix.verify_rc10_hotfix(bundle, policy=policy)
    assert all_bytes(bundle) == before


@pytest.mark.parametrize('target', ['bundle', 'ancestor', 'manifest', 'guard', 'file', 'directory', 'extra'])
def test_symlinks_never_bypass_scoped_verification(release, tmp_path, target):
    bundle, policy, _ = release
    tested = bundle
    if target == 'bundle':
        tested = tmp_path / 'linked release'; tested.symlink_to(bundle, target_is_directory=True)
    elif target == 'ancestor':
        linked = tmp_path / 'linked parent'; linked.symlink_to(bundle.parent, target_is_directory=True)
        tested = linked / bundle.name
    else:
        path = {'manifest': bundle / '.pbs-release.json', 'guard': bundle / policy.guard_path,
                'file': bundle / '_internal/unchanged.bin', 'directory': bundle / '_internal',
                'extra': bundle / 'new-link'}[target]
        if target == 'extra': path.symlink_to('PlaguesBroadcastStation')
        else:
            saved = tmp_path / ('saved-' + target)
            path.rename(saved)
            path.symlink_to(saved, target_is_directory=target == 'directory')
    with pytest.raises(hotfix.HotfixError): hotfix.verify_rc10_hotfix(tested, policy=policy)


def test_declared_empty_directory_is_exactly_pinned(release):
    bundle, policy, _ = release
    (bundle / 'empty').mkdir()
    (bundle / 'empty').chmod(0o755)
    pinned = replace(policy, empty_directories=('empty',))
    assert hotfix.verify_rc10_hotfix(bundle, policy=pinned)
    (bundle / 'empty/unknown').mkdir()
    with pytest.raises(hotfix.HotfixError): hotfix.verify_rc10_hotfix(bundle, policy=pinned)


def test_policy_rejects_unpinned_and_malformed_overrides(release):
    _, policy, _ = release
    for changes in ({'guard_path': '../other'}, {'guard_path': 'PlaguesBroadcastStation'},
                    {'approved_guard_sha256': policy.original_guard_sha256}, {'directory_mode': 0o1777},
                    {'binary_sha256': 'wrong'}, {'empty_directories': ['not-a-tuple']}):
        with pytest.raises(hotfix.HotfixError): replace(policy, **changes)


def test_bundled_policy_is_hash_pinned_and_tampering_fails_before_bundle_read(tmp_path, monkeypatch):
    policy = tmp_path / 'policy.json'
    policy.write_bytes(hotfix.POLICY_PATH.read_bytes() + b' ')
    monkeypatch.setattr(hotfix, 'POLICY_PATH', policy)
    with pytest.raises(hotfix.HotfixError, match='policy changed'):
        hotfix.load_default_policy()
    with pytest.raises(hotfix.HotfixError, match='policy changed'):
        hotfix.verify_rc10_hotfix(tmp_path / 'never-opened')


def test_cli_has_no_arbitrary_policy_or_patch_hash_option(release):
    bundle, _, _ = release
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/rc10_hotfix.py'), str(bundle),
                             '--policy', 'made-up.json'], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and 'unrecognized arguments' in result.stderr
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/rc10_hotfix.py'), str(bundle)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and 'pinned baseline' in result.stderr


def test_audit_separates_original_derived_and_receipt_create_only(release, tmp_path):
    bundle, policy, raw = release
    before = all_bytes(bundle)
    audit = tmp_path / 'audit'
    verified = hotfix.capture_verified_hotfix(bundle, audit, policy=policy)
    assert set(p.name for p in audit.iterdir()) == {'original-manifest.json', 'patched-manifest.json', 'receipt.json'}
    assert (audit / 'original-manifest.json').read_bytes() == raw
    assert json.loads((audit / 'patched-manifest.json').read_bytes()) == verified.patched_manifest
    assert json.loads((audit / 'receipt.json').read_bytes()) == verified.receipt
    assert audit.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in audit.iterdir())
    captured = all_bytes(audit)
    assert hotfix.capture_verified_hotfix(bundle, audit, policy=policy).receipt == verified.receipt
    assert all_bytes(audit) == captured and all_bytes(bundle) == before


def test_interrupted_audit_retains_original_without_false_completion_and_retry_works(release, tmp_path, monkeypatch):
    bundle, policy, raw = release
    audit = tmp_path / 'interrupted audit'
    writer = hotfix._write_create_only
    def fail_second(fd, name, value):
        if name == 'patched-manifest.json': raise OSError('simulated interrupted evidence write')
        return writer(fd, name, value)
    monkeypatch.setattr(hotfix, '_write_create_only', fail_second)
    with pytest.raises(hotfix.HotfixError, match='did not finish'):
        hotfix.capture_verified_hotfix(bundle, audit, policy=policy)
    assert (audit / 'original-manifest.json').read_bytes() == raw
    assert not (audit / 'receipt.json').exists()
    assert not list(audit.glob('*.tmp'))
    monkeypatch.setattr(hotfix, '_write_create_only', writer)
    hotfix.capture_verified_hotfix(bundle, audit, policy=policy)
    assert (audit / 'receipt.json').is_file()


@pytest.mark.parametrize('tamper', ['different-file', 'extra-file', 'linked-file', 'wrong-file-mode', 'wrong-directory-mode'])
def test_audit_never_overwrites_conflicting_evidence(release, tmp_path, tamper):
    bundle, policy, _ = release
    audit = tmp_path / 'audit'
    hotfix.capture_verified_hotfix(bundle, audit, policy=policy)
    path = audit / 'original-manifest.json'
    if tamper == 'different-file': path.write_bytes(b'preserved other evidence')
    elif tamper == 'extra-file': (audit / 'unknown.json').write_bytes(b'preserved')
    elif tamper == 'linked-file':
        outside = tmp_path / 'outside';outside.write_bytes(path.read_bytes());path.unlink();path.symlink_to(outside)
    elif tamper == 'wrong-file-mode': path.chmod(0o644)
    else: audit.chmod(0o755)
    before = all_bytes(audit)
    with pytest.raises(hotfix.HotfixError): hotfix.capture_verified_hotfix(bundle, audit, policy=policy)
    assert all_bytes(audit) == before


def test_capture_cannot_write_inside_or_above_retained_release(release):
    bundle, policy, _ = release
    verified = hotfix.verify_rc10_hotfix(bundle, policy=policy)
    for audit in (bundle, bundle / 'audit', bundle.parent):
        with pytest.raises(hotfix.HotfixError, match='separate'):
            hotfix.capture_hotfix_evidence(verified, audit)


def test_changed_release_during_audit_does_not_publish_receipt(release, tmp_path, monkeypatch):
    bundle, policy, _ = release
    audit = tmp_path / 'audit'
    writer = hotfix._write_create_only
    def mutate(fd, name, raw):
        result = writer(fd, name, raw)
        if name == 'patched-manifest.json': (bundle / '_internal/unchanged.bin').write_bytes(b'late unknown change')
        return result
    monkeypatch.setattr(hotfix, '_write_create_only', mutate)
    with pytest.raises(hotfix.HotfixError): hotfix.capture_verified_hotfix(bundle, audit, policy=policy)
    assert not (audit / 'receipt.json').exists()


def test_verification_result_is_not_an_activation_or_process_operation(release, monkeypatch):
    bundle, policy, _ = release
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=AssertionError('No process operations')))
    assert hotfix.verify_rc10_hotfix(bundle, policy=policy)


def test_file_hashed_early_cannot_change_unnoticed_later(release,monkeypatch):
    bundle,policy,_=release
    original=hotfix._read
    seen=False
    def change_earlier(fd,name,**kwargs):
        nonlocal seen
        result=original(fd,name,**kwargs)
        if name=='lyrics_review.py' and not seen:
            seen=True
            (bundle/'PlaguesBroadcastStation').write_bytes(b'unknown change after its earlier hash')
        return result
    monkeypatch.setattr(hotfix,'_read',change_earlier)
    with pytest.raises(hotfix.HotfixError,match='previously hashed'):
        hotfix.verify_rc10_hotfix(bundle,policy=policy)


def test_private_release_root_mode_is_pinned_separately_from_descendants(release):
    bundle,policy,_=release
    bundle.chmod(0o700)
    pinned=replace(policy,root_directory_mode=0o700)
    verified=hotfix.verify_rc10_hotfix(bundle,policy=pinned)
    assert verified.receipt['mode_pins']['root_directory']==0o700
    assert verified.receipt['mode_pins']['directory']==0o755
    bundle.chmod(0o755)
    with pytest.raises(hotfix.HotfixError,match='directory mode'):
        hotfix.verify_rc10_hotfix(bundle,policy=pinned)
