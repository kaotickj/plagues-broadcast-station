"""Controlled code selection uses fixture roots only; never actual installations."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import platform
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import controlled_activation as controlled
import linux_installer as installer
import rc10_hotfix


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    path.chmod(0o644)


def fixture_release(path, version, source, release=None):
    path.mkdir(parents=True)
    path.chmod(0o755)
    binary = path / installer.EXECUTABLE
    binary.write_text('#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] != ["--version"]: raise SystemExit(99)\nprint("Plague\'s Broadcast Station ' + version + '")\n')
    binary.chmod(0o755)
    guard = path / '_internal/gnb_player/extraction_backend/lyrics_review.py'
    guard.parent.mkdir(parents=True)
    for item in guard.parents:
        if item == path: break
        item.chmod(0o755)
    guard.write_bytes(b'original fixture review guard\n'); guard.chmod(0o644)
    (path / 'content.txt').write_bytes(b'unchanged package content\n'); (path / 'content.txt').chmod(0o644)
    files = installer.inventory(path)
    content = sha(json.dumps(files, sort_keys=True).encode())
    release = release or f'{version}-{source[:12]}-{content[:12]}'
    manifest = {'schema': 1, 'app_id': installer.APP_ID, 'platform': 'Linux',
                'architecture': platform.machine(), 'version': version, 'release': release,
                'source_commit': source, 'source_tree_sha256': 'c' * 64, 'source_dirty': False,
                'bundle_sha256': content, 'files': files}
    write_json(path / '.pbs-release.json', manifest)
    return manifest


@pytest.fixture
def station(tmp_path):
    def make(name):
        root = tmp_path / name
        root.mkdir()
        (root / '.pbs-installation').write_text(installer.MARKER)
        data = tmp_path / (name + ' station data')
        (data / 'lyrics/history').mkdir(parents=True)
        (data / 'settings.json').write_bytes(b'{"extraction":{"use_gpu":false,"separate_vocals":true}}\n')
        (data / 'session.json').write_bytes(b'{"name":"human station selection"}\n')
        (data / 'lyrics/approved.json').write_bytes(b'{"human_review":{"status":"approved"},"text":"human correction"}\n')
        (data / 'lyrics/history/prior.json').write_bytes(b'old human revision\n')
        (data / 'model.bin').write_bytes(b'existing model, never copied or opened by tool\n')
        rc10 = '0.2.4rc10-fixture'
        baseline = root / 'releases' / rc10
        manifest = fixture_release(baseline, '0.2.4rc10', 'a' * 40, rc10)
        manifest_raw = (baseline / '.pbs-release.json').read_bytes()
        guard = baseline / '_internal/gnb_player/extraction_backend/lyrics_review.py'
        original_guard = guard.read_bytes()
        guard.write_bytes(b'approved fixture neighbour guard\n')
        policy = rc10_hotfix.Policy(release=rc10, version='0.2.4rc10', source_commit='a' * 40,
            original_manifest_sha256=sha(manifest_raw), binary_sha256=sha((baseline / installer.EXECUTABLE).read_bytes()),
            guard_path='_internal/gnb_player/extraction_backend/lyrics_review.py',
            original_guard_sha256=sha(original_guard), approved_guard_sha256=sha(guard.read_bytes()), directory_mode=0o755)
        temporary = root / 'releases' / 'candidate-preparation'
        new = fixture_release(temporary, '0.2.4rc11.dev1', 'b' * 40)
        candidate = root / 'releases' / new['release']
        temporary.rename(candidate)
        state = {'app_id': installer.APP_ID, 'schema': 1, 'current': rc10,
                 'previous': '0.2.4rc9-fixture', 'data_dir': str(data)}
        write_json(root / 'install.json', state)
        return {'root': root, 'data': data, 'rc10': baseline, 'candidate': candidate,
                'release': new['release'], 'policy': policy, 'manifest_raw': manifest_raw,
                'audit': tmp_path / (name + ' audits')}
    return make


def contents(path):
    return {str(p.relative_to(path)): (p.read_bytes(), p.stat().st_mtime_ns) for p in path.rglob('*') if p.is_file()}


def plan(station):
    return controlled.plan_activation(station['root'], station['release'], policy=station['policy'])


def prepared(station):
    value = plan(station)
    controlled.prepare(value, station['audit'], approved=True, apps_closed=True, policy=station['policy'])
    return value


def run(fn, station):
    return fn(station['audit'], approved=True, apps_closed=True, policy=station['policy'])


def test_plan_is_read_only_and_requires_candidate_inside_managed_release(station):
    item = station('first')
    before = contents(item['root']); data = contents(item['data'])
    value = plan(item)
    assert value['action'] == 'activate'
    assert contents(item['root']) == before and contents(item['data']) == data
    assert not (item['root'] / '.install.lock').exists() and not item['audit'].exists()
    with pytest.raises(ValueError):
        controlled.plan_activation(item['root'], '../outside', policy=item['policy'])
    with pytest.raises(OSError):
        controlled.plan_activation(item['root'], 'not-staged', policy=item['policy'])


def test_explicit_gates_prevent_mutation(station):
    item = station('first'); value = plan(item)
    before = (item['root'] / 'install.json').read_bytes()
    for flags in [{}, {'approved': True}, {'apps_closed': True}]:
        with pytest.raises(controlled.ActivationError, match='approval'):
            controlled.prepare(value, item['audit'], policy=item['policy'], **flags)
    assert not item['audit'].exists() and (item['root'] / 'install.json').read_bytes() == before


def test_two_stations_activate_rollback_keep_data_approval_and_original_manifests(station):
    first, second = station('first'), station('second')
    originals = {name: contents(item['data']) for name, item in [('first', first), ('second', second)]}
    second_before = (second['root'] / 'install.json').read_bytes()
    for item in (first, second):
        prepared(item)
        assert (item['audit'] / 'rc10-hotfix/original-manifest.json').read_bytes() == item['manifest_raw']
        assert (item['audit'] / 'rc10-hotfix/patched-manifest.json').exists()
        assert (item['audit'] / 'rc10-hotfix/receipt.json').exists()
        state = run(controlled.apply, item)
        assert state['selection'] == 'after' and state['phase'] == 'committed'
        active = installer.read_state(item['root'])
        assert active['current'] == item['release'] and active['data_dir'] == str(item['data'])
        assert installer.verify_release(installer.release_path(item['root'], active['current']))['release'] == item['release']
        assert (item['rc10'] / '.pbs-release.json').read_bytes() == item['manifest_raw']
        if item is first: assert (second['root'] / 'install.json').read_bytes() == second_before
        rollback = controlled.plan_rollback(item['root'], policy=item['policy'])
        rollback_audit = item['audit'].with_name(item['audit'].name + ' rollback')
        controlled.prepare(rollback, rollback_audit, approved=True, apps_closed=True, policy=item['policy'])
        controlled.apply(rollback_audit, approved=True, apps_closed=True, policy=item['policy'])
        assert installer.read_state(item['root'])['current'] == item['rc10'].name
        with pytest.raises(ValueError): installer.verify_release(item['rc10'])  # Generic strict verifier remains strict.
    assert contents(first['data']) == originals['first'] and contents(second['data']) == originals['second']


@pytest.mark.parametrize('after_replace', [False, True])
def test_interrupted_selection_before_or_after_atomic_write_is_recoverable(station, monkeypatch, after_replace):
    item = station('first'); value = prepared(item)
    original = controlled._replace_selection
    def interrupted(root, raw):
        if after_replace: original(root, raw)
        raise OSError('fixture interrupted selection')
    monkeypatch.setattr(controlled, '_replace_selection', interrupted)
    with pytest.raises(controlled.ActivationError, match='interrupted'):
        run(controlled.apply, item)
    observed = controlled.inspect_transaction(item['audit'])
    assert observed['phase'] == 'applying'
    assert observed['selection'] == ('after' if after_replace else 'before')
    monkeypatch.setattr(controlled, '_replace_selection', original)
    recovered = run(controlled.recover, item)
    assert recovered['phase'] == ('committed' if after_replace else 'prepared')
    if not after_replace: assert run(controlled.apply, item)['phase'] == 'committed'


def test_failure_to_finalize_receipt_reports_committed_selection(station, monkeypatch):
    item = station('first'); prepared(item)
    original = controlled._journal
    def fail_final(directory, value):
        if value['phase'] == 'committed': raise OSError('fixture receipt failure')
        original(directory, value)
    monkeypatch.setattr(controlled, '_journal', fail_final)
    with pytest.raises(controlled.ActivationError, match='Selection committed'):
        run(controlled.apply, item)
    assert installer.read_state(item['root'])['current'] == item['release']
    monkeypatch.setattr(controlled, '_journal', original)
    assert run(controlled.recover, item)['phase'] == 'committed'


def test_unknown_state_is_not_overwritten_by_recovery(station):
    item = station('first'); prepared(item)
    state = installer.read_state(item['root']); state['previous'] = 'externally-selected-fixture'
    write_json(item['root'] / 'install.json', state)
    before = (item['root'] / 'install.json').read_bytes(); journal = (item['audit'] / 'journal.json').read_bytes()
    with pytest.raises(controlled.ActivationError, match='neither recorded'):
        run(controlled.recover, item)
    assert (item['root'] / 'install.json').read_bytes() == before
    assert (item['audit'] / 'journal.json').read_bytes() == journal


def test_candidate_or_state_change_after_preparation_refuses_activation(station):
    first, second = station('first'), station('second')
    for item in (first, second): prepared(item)
    before = (first['root'] / 'install.json').read_bytes()
    (first['candidate'] / 'unknown-file').write_bytes(b'not approved')
    with pytest.raises(ValueError): run(controlled.apply, first)
    assert (first['root'] / 'install.json').read_bytes() == before
    second_state = second['root'] / 'install.json'; changed = second_state.read_bytes() + b' \n'; second_state.write_bytes(changed)
    with pytest.raises(controlled.ActivationError, match='prepared state'):
        run(controlled.apply, second)
    assert second_state.read_bytes() == changed


def test_rollback_failure_and_new_human_edits_are_preserved(station, monkeypatch):
    item = station('first'); prepared(item); run(controlled.apply, item)
    human = item['data'] / 'lyrics/approved.json'; human.write_bytes(b'newly reviewed human correction and approval\n')
    newer = contents(item['data'])
    rollback = controlled.plan_rollback(item['root'], policy=item['policy'])
    audit = item['audit'].with_name('rollback audit')
    controlled.prepare(rollback, audit, approved=True, apps_closed=True, policy=item['policy'])
    original = controlled._replace_selection
    monkeypatch.setattr(controlled, '_replace_selection', lambda *a: (_ for _ in ()).throw(OSError('rollback failure')))
    with pytest.raises(controlled.ActivationError):
        controlled.apply(audit, approved=True, apps_closed=True, policy=item['policy'])
    assert installer.read_state(item['root'])['current'] == item['release']
    monkeypatch.setattr(controlled, '_replace_selection', original)
    controlled.recover(audit, approved=True, apps_closed=True, policy=item['policy'])
    controlled.apply(audit, approved=True, apps_closed=True, policy=item['policy'])
    assert contents(item['data']) == newer


def test_unknown_rc10_difference_refuses_rollback(station):
    item = station('first'); prepared(item); run(controlled.apply, item)
    guard = item['rc10'] / '_internal/gnb_player/extraction_backend/lyrics_review.py'
    guard.write_bytes(b'unapproved later edit\n')
    before = (item['root'] / 'install.json').read_bytes()
    with pytest.raises(ValueError): controlled.plan_rollback(item['root'], policy=item['policy'])
    assert (item['root'] / 'install.json').read_bytes() == before


def test_prepare_interruption_has_no_false_complete_journal(station, monkeypatch):
    item = station('first'); value = plan(item)
    original = rc10_hotfix.capture_hotfix_evidence
    monkeypatch.setattr(rc10_hotfix, 'capture_hotfix_evidence', lambda *a: (_ for _ in ()).throw(OSError('evidence failure')))
    with pytest.raises(OSError):
        controlled.prepare(value, item['audit'], approved=True, apps_closed=True, policy=item['policy'])
    assert not (item['audit'] / 'journal.json').exists()
    assert (item['root'] / 'install.json').read_bytes() == base64.b64decode(value['before_b64'])
    monkeypatch.setattr(rc10_hotfix, 'capture_hotfix_evidence', original)
    assert controlled.prepare(value, item['audit'], approved=True, apps_closed=True, policy=item['policy'])['phase'] == 'prepared'


def test_plan_cannot_change_data_mapping_even_if_hashes_are_recomputed(station):
    item = station('first'); value = deepcopy(plan(item))
    after = json.loads(base64.b64decode(value['after_b64']))
    after['data_dir'] = '/different-data'
    raw = controlled._json_raw(after)
    value['after_b64'] = base64.b64encode(raw).decode(); value['after_sha256'] = sha(raw)
    with pytest.raises(controlled.ActivationError, match='data mapping'):
        controlled.prepare(value, item['audit'], approved=True, apps_closed=True, policy=item['policy'])
    assert not item['audit'].exists()


def test_cli_mutation_requires_both_explicit_gates(tmp_path):
    assert controlled.main(['apply', '--audit-dir', str(tmp_path / 'not-created')]) == 1
    assert not (tmp_path / 'not-created').exists()


def test_metadata_only_version_relabel_is_refused_during_approved_prepare(station):
    item = station('first')
    binary = item['candidate'] / installer.EXECUTABLE
    binary.write_text('#!/usr/bin/env python3\nprint("Plague\'s Broadcast Station 0.2.4rc10")\n')
    binary.chmod(0o755)
    manifest = json.loads((item['candidate'] / '.pbs-release.json').read_text())
    manifest['files'] = installer.inventory(item['candidate'])
    manifest['bundle_sha256'] = sha(json.dumps(manifest['files'], sort_keys=True).encode())
    manifest['release'] = f"{manifest['version']}-{manifest['source_commit'][:12]}-{manifest['bundle_sha256'][:12]}"
    write_json(item['candidate'] / '.pbs-release.json', manifest)
    renamed = item['candidate'].with_name(manifest['release']); item['candidate'].rename(renamed)
    item.update(candidate=renamed, release=manifest['release'])
    value = plan(item)
    before = (item['root'] / 'install.json').read_bytes()
    with pytest.raises(controlled.ActivationError, match='metadata-only'):
        controlled.prepare(value, item['audit'], approved=True, apps_closed=True, policy=item['policy'])
    assert not item['audit'].exists() and (item['root'] / 'install.json').read_bytes() == before


def test_version_probe_is_not_run_without_both_gates(station, monkeypatch):
    item = station('first'); value = plan(item)
    monkeypatch.setattr(controlled, '_probe_candidate', lambda *a: (_ for _ in ()).throw(AssertionError('must not execute')))
    with pytest.raises(controlled.ActivationError, match='approval'):
        controlled.prepare(value, item['audit'], policy=item['policy'])


def test_candidate_changed_by_version_probe_is_refused(station, monkeypatch):
    item = station('first'); value = plan(item)
    original = controlled._probe_candidate
    def changed(plan):
        result = original(plan)
        (item['candidate'] / 'content.txt').write_bytes(b'changed during version probe')
        return result
    monkeypatch.setattr(controlled, '_probe_candidate', changed)
    with pytest.raises(ValueError):
        controlled.prepare(value, item['audit'], approved=True, apps_closed=True, policy=item['policy'])
    assert not item['audit'].exists() and (item['root'] / 'install.json').read_bytes() == base64.b64decode(value['before_b64'])


@pytest.mark.parametrize('change', ['mode', 'empty-directory'])
def test_noncontent_candidate_changes_cannot_evade_prepared_identity(station, change):
    item = station('first'); value = prepared(item)
    if change == 'mode': (item['candidate'] / 'content.txt').chmod(0o600)
    else: (item['candidate'] / 'unrecorded-empty-directory').mkdir()
    # The unchanged generic verifier intentionally checks its original semantics.
    assert installer.verify_release(item['candidate'])['release'] == item['release']
    with pytest.raises(controlled.ActivationError, match='changed since planning'):
        run(controlled.apply, item)
    assert (item['root'] / 'install.json').read_bytes() == base64.b64decode(value['before_b64'])


@pytest.mark.parametrize('name', ['version-probe.json', 'candidate-modes.json', 'rc10-hotfix/receipt.json', 'rc10-hotfix/original-manifest.json'])
def test_prepared_evidence_tamper_is_refused(station, name):
    item = station('first'); value = prepared(item)
    (item['audit'] / name).write_bytes(b'changed evidence\n')
    with pytest.raises(ValueError): run(controlled.apply, item)
    assert (item['root'] / 'install.json').read_bytes() == base64.b64decode(value['before_b64'])


def test_new_candidate_difference_after_intent_journal_prevents_commit(station, monkeypatch):
    item = station('first'); value = prepared(item)
    original = controlled._journal
    def changed(directory, journal):
        original(directory, journal)
        if journal['phase'] == 'applying':
            (item['candidate'] / 'content.txt').chmod(0o600)
    monkeypatch.setattr(controlled, '_journal', changed)
    with pytest.raises(controlled.ActivationError, match='changed since planning'):
        run(controlled.apply, item)
    assert (item['root'] / 'install.json').read_bytes() == base64.b64decode(value['before_b64'])
    assert controlled.inspect_transaction(item['audit'])['selection'] == 'before'


def test_crafted_plan_cannot_select_a_misnamed_release_directory(station):
    item = station('first'); value = plan(item)
    alias = item['candidate'].with_name('misnamed-candidate')
    item['candidate'].rename(alias)
    value['candidate_release'] = alias.name
    after = json.loads(base64.b64decode(value['after_b64'])); after['current'] = alias.name
    raw = controlled._json_raw(after); value['after_b64'] = base64.b64encode(raw).decode(); value['after_sha256'] = sha(raw)
    with pytest.raises(controlled.ActivationError, match='directories/version'):
        controlled.prepare(value, item['audit'], approved=True, apps_closed=True, policy=item['policy'])
    assert not item['audit'].exists()


def test_audit_mutation_after_durable_intent_prevents_selection(station, monkeypatch):
    item = station('first'); value = prepared(item)
    original = controlled._journal
    def changed(directory, journal):
        original(directory, journal)
        if journal['phase'] == 'applying': (directory / 'version-probe.json').write_bytes(b'changed probe receipt')
    monkeypatch.setattr(controlled, '_journal', changed)
    with pytest.raises(controlled.ActivationError, match='Audit evidence changed'):
        run(controlled.apply, item)
    assert (item['root'] / 'install.json').read_bytes() == base64.b64decode(value['before_b64'])


def test_version_probe_timeout_does_not_publish_prepared_transaction(station, monkeypatch):
    item = station('first'); value = plan(item)
    def timeout(*args, **kwargs):
        raise controlled.subprocess.TimeoutExpired('fixture-version', 10)
    monkeypatch.setattr(controlled.subprocess, 'run', timeout)
    with pytest.raises(controlled.ActivationError, match='bounded'):
        controlled.prepare(value, item['audit'], approved=True, apps_closed=True, policy=item['policy'])
    assert not item['audit'].exists()


def test_existing_nonprivate_audit_directory_is_not_repurposed(station):
    item = station('first'); value = plan(item)
    item['audit'].mkdir(); item['audit'].chmod(0o755)
    with pytest.raises(controlled.ActivationError, match='private directory'):
        controlled.prepare(value, item['audit'], approved=True, apps_closed=True, policy=item['policy'])
    assert not list(item['audit'].iterdir()) and item['audit'].stat().st_mode & 0o777 == 0o755


def test_audit_directory_cannot_contain_target_installation_or_data(station):
    item = station('first'); value = plan(item)
    with pytest.raises(controlled.ActivationError, match='outside'):
        controlled.prepare(value, item['root'].parent, approved=True, apps_closed=True, policy=item['policy'])


def test_broken_journal_link_is_preserved_not_replaced(station):
    item = station('first'); value = plan(item)
    item['audit'].mkdir(mode=0o700)
    link = item['audit'] / 'journal.json'; link.symlink_to(item['audit'] / 'missing-target')
    with pytest.raises(ValueError):
        controlled.prepare(value, item['audit'], approved=True, apps_closed=True, policy=item['policy'])
    assert link.is_symlink() and not (item['audit'] / 'missing-target').exists()



def test_regular_cli_help_does_not_create_bytecode_or_change_helper_inventory(tmp_path):
    import os
    import shutil
    import subprocess

    copied = tmp_path / 'copied immutable readiness tools'
    copied.mkdir()
    for name in ('controlled_activation.py', 'linux_installer.py', 'rc10_hotfix.py',
                 'rc10-neighbour-guard-policy.json'):
        shutil.copy2(SCRIPTS / name, copied / name)

    def inventory():
        result = {'.': ('directory', copied.stat().st_mode, copied.stat().st_mtime_ns)}
        for path in sorted(copied.rglob('*')):
            info = path.lstat()
            result[str(path.relative_to(copied))] = (
                'directory' if path.is_dir() else 'file', info.st_mode, info.st_mtime_ns,
                None if path.is_dir() else sha(path.read_bytes()))
        return result

    before = inventory()
    environment = os.environ.copy()
    environment.pop('PYTHONDONTWRITEBYTECODE', None)
    environment.pop('PYTHONPYCACHEPREFIX', None)
    # Deliberately ordinary Python: no -B and no bytecode-related environment.
    result = subprocess.run([sys.executable, str(copied / 'controlled_activation.py'), '--help'],
                            cwd=tmp_path, env=environment, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert '--apps-closed' in result.stdout
    assert not list(copied.rglob('__pycache__'))
    assert inventory() == before
