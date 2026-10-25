#!/usr/bin/env python3
"""Explicit, journaled code selection for a qualified development candidate.

Default CLI planning is read-only. Mutations require an explicit approval and
closed-app assertion; this tool never closes apps, launches a GUI, accesses a
keyring, copies models, changes station data, or restores old user data.
Generic installer integrity verification is not modified.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

# Running the immutable helper directly must not create sibling __pycache__.
# Set this before importing any packaged readiness-tool module, without relying
# on callers to supply -B or environment flags.
sys.dont_write_bytecode = True

import linux_installer as installer
import rc10_hotfix

PLAN_VERSION = 'controlled-code-selection-1'
JOURNAL_VERSION = 'controlled-code-selection-journal-1'
MAX_METADATA_BYTES = 2 * 1024 * 1024
STATE_KEYS = {'app_id', 'schema', 'current', 'previous', 'data_dir'}
PLAN_KEYS = {'version', 'root', 'action', 'candidate_release', 'rc10_release',
             'before_b64', 'after_b64', 'before_sha256', 'after_sha256',
             'candidate_identity', 'hotfix_identity'}


class ActivationError(ValueError):
    pass


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json_raw(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ActivationError('Duplicate metadata fields were refused.')
        result[key] = value
    return result


def _decode(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    except (UnicodeError, ValueError) as error:
        raise ActivationError('Control metadata is invalid; nothing was selected.') from error


def _plain_path(value):
    path = Path(value).expanduser().absolute()
    if '..' in path.parts or any(c in str(path) for c in '\0\n\r'):
        raise ActivationError('Control paths must be plain absolute paths.')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ActivationError('Linked control paths were refused.')
    return path


def _read(path):
    path = _plain_path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_BYTES:
            raise ActivationError('Control metadata must be a bounded regular file.')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(MAX_METADATA_BYTES + 1)
        if len(raw) > MAX_METADATA_BYTES:
            raise ActivationError('Control metadata is oversized.')
        return raw
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _root(root):
    root = _plain_path(root)
    if _read(root / '.pbs-installation') != installer.MARKER.encode():
        raise ActivationError('This is not the expected managed installation root.')
    return root


def _state(root):
    raw = _read(root / 'install.json')
    state = _decode(raw)
    if (not isinstance(state, dict) or set(state) != STATE_KEYS or state['app_id'] != installer.APP_ID
            or type(state['schema']) is not int or state['schema'] != 1
            or not isinstance(state['data_dir'], str) or not Path(state['data_dir']).is_absolute()
            or any(c in state['data_dir'] for c in '\0\n\r')):
        raise ActivationError('Unknown installation state was preserved without change.')
    data = _plain_path(state['data_dir'])
    if (not data.is_dir() or data == root or data.is_relative_to(root) or root.is_relative_to(data)):
        raise ActivationError('Existing station data must remain a separate ordinary directory; no mapping was changed.')
    installer.release_path(root, state['current'])
    if state['previous'] is not None:
        installer.release_path(root, state['previous'])
    return raw, state


def _candidate(bundle):
    manifest = installer.verify_release(bundle)
    version = manifest.get('version')
    if (not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:rc\d+|\.dev\d+)(?:\.dev\d+)?', version)
            or manifest['source_dirty'] is not False):
        raise ActivationError('Controlled activation requires a clean, explicitly development/RC candidate.')
    expected = f"{version}-{manifest['source_commit'][:12]}-{manifest['bundle_sha256'][:12]}"
    if manifest.get('release') != expected:
        raise ActivationError('The development release ID must bind its version, source commit and content hash.')
    return manifest



def _candidate_modes(bundle):
    """Pin complete modes and directory names without relaxing generic checks."""
    result = {}
    for path in (bundle, *sorted(bundle.rglob('*'))):
        info = path.lstat()
        kind = ('directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode)
                else 'link' if stat.S_ISLNK(info.st_mode) else 'unsupported')
        if kind == 'unsupported':
            raise ActivationError('Unsupported candidate file type was refused.')
        result['.' if path == bundle else path.relative_to(bundle).as_posix()] = {
            'type': kind, 'mode': stat.S_IMODE(info.st_mode)}
    return result

def _candidate_identity(bundle, manifest):
    return {'release': manifest['release'], 'version': manifest['version'],
            'source_commit': manifest['source_commit'], 'bundle_sha256': manifest['bundle_sha256'],
            'manifest_sha256': _sha(_read(bundle / '.pbs-release.json')),
            'mode_inventory_sha256': _sha(_json_raw(_candidate_modes(bundle))),
            'binary_sha256': installer.digest(bundle / installer.EXECUTABLE)}


def _hotfix_identity(verified):
    return {'release': verified.manifest['release'], 'version': verified.manifest['version'],
            'source_commit': verified.manifest['source_commit'],
            'original_manifest_sha256': _sha(verified.original_manifest_raw),
            'approved_inventory_sha256': _sha(json.dumps(verified.inventory, sort_keys=True).encode())}


def _make_plan(root, action, candidate_release, expected_state_sha256, policy):
    root = _root(root)
    raw, state = _state(root)
    if expected_state_sha256 is not None and _sha(raw) != expected_state_sha256:
        raise ActivationError('Installation selection changed; make a fresh plan before continuing.')
    if action == 'activate':
        rc10_release = state['current']
    elif action == 'rollback':
        rc10_release = state['previous']
        candidate_release = state['current']
        if rc10_release is None:
            raise ActivationError('No retained RC10 selection is recorded.')
    else:
        raise ActivationError('Unknown controlled selection action.')
    if candidate_release == rc10_release:
        raise ActivationError('Candidate and retained RC10 must be distinct releases.')
    retained = installer.release_path(root, rc10_release)
    candidate = installer.release_path(root, candidate_release)
    verified = rc10_hotfix.verify_rc10_hotfix(retained, policy=policy)
    manifest = _candidate(candidate)
    if manifest['version'] == verified.manifest['version']:
        raise ActivationError('The development candidate must have a distinct version from retained RC10.')
    # The pinned verifier binds its directory manifest. Also bind the path name
    # selected by install.json so a copied/renamed baseline cannot be selected.
    if verified.manifest['release'] != rc10_release or manifest['release'] != candidate_release:
        raise ActivationError('Release directory names do not match their verified identities.')
    after = dict(state)
    after.update(current=candidate_release if action == 'activate' else rc10_release,
                 previous=rc10_release if action == 'activate' else candidate_release)
    after_raw = _json_raw(after)
    return {'version': PLAN_VERSION, 'root': str(root), 'action': action,
            'candidate_release': candidate_release, 'rc10_release': rc10_release,
            'before_b64': base64.b64encode(raw).decode(), 'after_b64': base64.b64encode(after_raw).decode(),
            'before_sha256': _sha(raw), 'after_sha256': _sha(after_raw),
            'candidate_identity': _candidate_identity(candidate, manifest),
            'hotfix_identity': _hotfix_identity(verified)}


def plan_activation(root, candidate_release, *, expected_state_sha256=None, policy=rc10_hotfix.DEFAULT_POLICY):
    """Verify staged candidate and approved patched RC10; do not write files."""
    return _make_plan(root, 'activate', candidate_release, expected_state_sha256, policy)


def plan_rollback(root, *, expected_state_sha256=None, policy=rc10_hotfix.DEFAULT_POLICY):
    """Plan a code-only swap back to approved patched RC10, preserving data."""
    return _make_plan(root, 'rollback', None, expected_state_sha256, policy)


def _validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS or plan.get('version') != PLAN_VERSION:
        raise ActivationError('Unsupported controlled selection plan.')
    try:
        before = base64.b64decode(plan['before_b64'], validate=True)
        after = base64.b64decode(plan['after_b64'], validate=True)
    except (TypeError, ValueError) as error:
        raise ActivationError('The recorded selection bytes are invalid.') from error
    if _sha(before) != plan['before_sha256'] or _sha(after) != plan['after_sha256']:
        raise ActivationError('The recorded selection byte hashes do not match.')
    original, target = _decode(before), _decode(after)
    expected = dict(original)
    if plan['action'] == 'activate':
        if original['current'] != plan['rc10_release']:
            raise ActivationError('The plan does not start at approved RC10.')
        expected.update(current=plan['candidate_release'], previous=plan['rc10_release'])
    elif plan['action'] == 'rollback':
        if (original['current'] != plan['candidate_release'] or original['previous'] != plan['rc10_release']):
            raise ActivationError('The rollback plan has different recorded releases.')
        expected.update(current=plan['rc10_release'], previous=plan['candidate_release'])
    else:
        raise ActivationError('Unknown controlled selection action.')
    if target != expected or set(original) != STATE_KEYS or set(target) != STATE_KEYS:
        raise ActivationError('A code selection plan cannot alter the station data mapping or other metadata.')
    return before, after


def _gates(approved, apps_closed):
    if approved is not True or apps_closed is not True:
        raise ActivationError('Explicit approval and confirmed closed apps are required; no process is stopped automatically.')


def _once(path, raw):
    if path.exists() or path.is_symlink():
        if _read(path) != raw:
            raise ActivationError('Existing audit evidence differs and was preserved.')
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def _journal(audit_dir, value):
    path = audit_dir / 'journal.json'
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ActivationError('Nonregular transaction journal was preserved without replacement.')
    installer.atomic_write(path, _json_raw(value))


def _check_inputs(plan, policy):
    root = _root(plan['root'])
    verified = rc10_hotfix.verify_rc10_hotfix(installer.release_path(root, plan['rc10_release']), policy=policy)
    bundle = installer.release_path(root, plan['candidate_release'])
    manifest = _candidate(bundle)
    if (verified.manifest['release'] != plan['rc10_release'] or manifest['release'] != plan['candidate_release']
            or manifest['version'] == verified.manifest['version']):
        raise ActivationError('Planned release directories/version do not match the distinct verified identities.')
    if (_hotfix_identity(verified) != plan['hotfix_identity'] or
            _candidate_identity(bundle, manifest) != plan['candidate_identity']):
        raise ActivationError('A verified release changed since planning; nothing was selected.')
    return root, verified



def _expected_probe(plan):
    identity = plan['candidate_identity']
    return {'status': 'matched', 'version': identity['version'],
            'source_commit': identity['source_commit'], 'binary_sha256': identity['binary_sha256'],
            'arguments': ['--version']}


def _probe_candidate(plan):
    """Approved no-GUI version probe; never pass a station data directory."""
    bundle = installer.release_path(Path(plan['root']), plan['candidate_release'])
    try:
        result = subprocess.run([str(bundle / installer.EXECUTABLE), '--version'],
            cwd=bundle, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise ActivationError('The bounded candidate version probe could not finish; nothing was selected.') from error
    expected = ("Plague's Broadcast Station " + plan['candidate_identity']['version']).encode()
    if result.returncode != 0 or result.stdout.strip() != expected:
        raise ActivationError('Candidate version output disagrees with its manifest; metadata-only relabeling was refused.')
    return _expected_probe(plan)


_AUDIT_FILES = ('plan.json', 'install-before.json', 'install-after.json',
                'candidate-manifest.json', 'candidate-modes.json', 'version-probe.json',
                'rc10-hotfix/original-manifest.json', 'rc10-hotfix/patched-manifest.json',
                'rc10-hotfix/receipt.json')


def _evidence_hashes(audit_dir):
    return {name: _sha(_read(audit_dir / name)) for name in _AUDIT_FILES}

def prepare(plan, audit_dir, *, approved=False, apps_closed=False, policy=rc10_hotfix.DEFAULT_POLICY):
    """Capture immutable original/derived manifests and state; no pointer write."""
    _gates(approved, apps_closed)
    before, after = _validate_plan(plan)
    audit_dir = _plain_path(audit_dir)
    root = _root(plan['root'])
    data = Path(_decode(before)['data_dir']).absolute()
    if (audit_dir == root or audit_dir.is_relative_to(root) or root.is_relative_to(audit_dir)
            or audit_dir == data or audit_dir.is_relative_to(data) or data.is_relative_to(audit_dir)):
        raise ActivationError('Audit evidence must be outside installation and station data directories.')
    with installer.locked_root(root):
        current, _ = _state(root)
        if current != before:
            raise ActivationError('Installation selection changed before preparation; make a new plan.')
        _, verified = _check_inputs(plan, policy)
        probe = _probe_candidate(plan)
        _check_inputs(plan, policy)  # Detect changed bytes/modes/identity during the probe.
        if _read(root / 'install.json') != before:
            raise ActivationError('Selection changed during version probing; no preparation was committed.')
        audit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        audit_info = audit_dir.stat()
        if (not stat.S_ISDIR(audit_info.st_mode) or stat.S_IMODE(audit_info.st_mode) != 0o700
                or hasattr(os, 'getuid') and audit_info.st_uid != os.getuid()):
            raise ActivationError('Audit evidence needs an owned private directory with mode 0700; existing metadata was not changed.')
        _once(audit_dir / 'plan.json', _json_raw(plan))
        _once(audit_dir / 'install-before.json', before)
        _once(audit_dir / 'install-after.json', after)
        _once(audit_dir / 'candidate-manifest.json', _read(installer.release_path(root, plan['candidate_release']) / '.pbs-release.json'))
        _once(audit_dir / 'candidate-modes.json', _json_raw(_candidate_modes(installer.release_path(root, plan['candidate_release']))))
        _once(audit_dir / 'version-probe.json', _json_raw(probe))
        rc10_hotfix.capture_hotfix_evidence(verified, audit_dir / 'rc10-hotfix')
        initial = {'version': JOURNAL_VERSION, 'plan_sha256': _sha(_json_raw(plan)),
                   'evidence_sha256': _evidence_hashes(audit_dir), 'phase': 'prepared'}
        journal_path = audit_dir / 'journal.json'
        if journal_path.exists():
            if _decode(_read(journal_path)) != initial:
                raise ActivationError('An existing transaction has already advanced; inspect/recover it instead.')
        else:
            _journal(audit_dir, initial)
    return inspect_transaction(audit_dir)


def _load(audit_dir):
    audit_dir = _plain_path(audit_dir)
    plan_raw = _read(audit_dir / 'plan.json')
    plan = _decode(plan_raw)
    before, after = _validate_plan(plan)
    journal = _decode(_read(audit_dir / 'journal.json'))
    if (not isinstance(journal, dict) or set(journal) != {'version', 'plan_sha256', 'evidence_sha256', 'phase'}
            or journal['version'] != JOURNAL_VERSION or journal['plan_sha256'] != _sha(plan_raw)
            or journal['phase'] not in {'prepared', 'applying', 'committed'}
            or journal['evidence_sha256'] != _evidence_hashes(audit_dir)
            or _decode(_read(audit_dir / 'version-probe.json')) != _expected_probe(plan)
            or _read(audit_dir / 'install-before.json') != before or _read(audit_dir / 'install-after.json') != after):
        raise ActivationError('Transaction evidence changed or is unsupported; no selection was changed.')
    return audit_dir, plan, journal, before, after


def inspect_transaction(audit_dir):
    """Read-only status, including whether an interrupted pointer write landed."""
    _, plan, journal, before, after = _load(audit_dir)
    root = _root(plan['root'])
    actual, _ = _state(root)
    selection = 'before' if actual == before else 'after' if actual == after else 'unknown'
    return {'root': str(root), 'action': plan['action'], 'phase': journal['phase'], 'selection': selection,
            'station_data_mutation_by_tool': False, 'process_control': 'none', 'native_credentials': 'none'}


def _replace_selection(root, raw):
    installer.atomic_write(root / 'install.json', raw)


def apply(audit_dir, *, approved=False, apps_closed=False, policy=rc10_hotfix.DEFAULT_POLICY):
    """Apply one prepared code-only selection under the installer lock and CAS."""
    _gates(approved, apps_closed)
    audit_dir, plan, journal, before, after = _load(audit_dir)
    root = _root(plan['root'])
    with installer.locked_root(root):
        current, _ = _state(root)
        if current != before or journal['phase'] != 'prepared':
            raise ActivationError('Selection/transaction no longer matches the prepared state; inspect/recover it first.')
        _check_inputs(plan, policy)
        journal['phase'] = 'applying'
        _journal(audit_dir, journal)
        # Recheck after durable intent, before the only live metadata mutation.
        if _read(root / 'install.json') != before:
            raise ActivationError('Selection changed before atomic commit; no overwrite was attempted.')
        _check_inputs(plan, policy)
        if _evidence_hashes(audit_dir) != journal['evidence_sha256']:
            raise ActivationError('Audit evidence changed before atomic commit; nothing was selected.')
        try:
            _replace_selection(root, after)
        except BaseException as error:
            raise ActivationError('Selection write was interrupted; inspect/recover the journal to determine before/after state.') from error
        if _read(root / 'install.json') != after:
            raise ActivationError('Selection changed during commit; stop and inspect without overwriting it.')
        journal['phase'] = 'committed'
        try:
            _journal(audit_dir, journal)
        except BaseException as error:
            raise ActivationError('Selection committed, but its receipt did not finish; recover the matching journal.') from error
    return inspect_transaction(audit_dir)


def recover(audit_dir, *, approved=False, apps_closed=False, policy=rc10_hotfix.DEFAULT_POLICY):
    """Finalize known interrupted state only; never restore or overwrite data.

    Exact before state becomes prepared for an explicit later retry. Exact after
    state finalizes committed only if durable applying intent was recorded.
    Unknown state is always refused, with all metadata/evidence preserved.
    """
    _gates(approved, apps_closed)
    audit_dir, plan, journal, before, after = _load(audit_dir)
    root = _root(plan['root'])
    with installer.locked_root(root):
        _check_inputs(plan, policy)
        actual, _ = _state(root)
        if actual == before:
            if journal['phase'] == 'committed':
                raise ActivationError('A committed selection was changed externally; no recovery overwrite is allowed.')
            journal['phase'] = 'prepared'
        elif actual == after:
            if journal['phase'] not in {'applying', 'committed'}:
                raise ActivationError('Unrecorded external selection change was refused.')
            journal['phase'] = 'committed'
        else:
            raise ActivationError('Current selection is neither recorded before nor after; preserve it and make a new reviewed plan.')
        _journal(audit_dir, journal)
    return inspect_transaction(audit_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=['plan-activate', 'plan-rollback', 'verify', 'prepare', 'apply', 'recover'], default='verify')
    parser.add_argument('--install-dir', type=Path)
    parser.add_argument('--candidate-release')
    parser.add_argument('--expected-state-sha256')
    parser.add_argument('--plan-file', type=Path)
    parser.add_argument('--audit-dir', type=Path)
    parser.add_argument('--approved', action='store_true')
    parser.add_argument('--apps-closed', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action.startswith('plan-'):
            if args.install_dir is None:
                raise ActivationError('--install-dir is required for planning.')
            result = (plan_activation(args.install_dir, args.candidate_release, expected_state_sha256=args.expected_state_sha256)
                      if args.action == 'plan-activate' else plan_rollback(args.install_dir, expected_state_sha256=args.expected_state_sha256))
        elif args.action == 'verify':
            if args.audit_dir is None:
                raise ActivationError('--audit-dir is required to inspect a prepared transaction.')
            result = inspect_transaction(args.audit_dir)
        elif args.action == 'prepare':
            if args.plan_file is None or args.audit_dir is None:
                raise ActivationError('--plan-file and --audit-dir are required.')
            result = prepare(_decode(_read(args.plan_file)), args.audit_dir, approved=args.approved, apps_closed=args.apps_closed)
        else:
            if args.audit_dir is None:
                raise ActivationError('--audit-dir is required.')
            fn = apply if args.action == 'apply' else recover
            result = fn(args.audit_dir, approved=args.approved, apps_closed=args.apps_closed)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ActivationError, ValueError, OSError, KeyError, TypeError) as error:
        print('Controlled activation: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
