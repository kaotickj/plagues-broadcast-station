"""Independent two-station readiness qualification using disposable fixtures.

No live application roots, user data, credential store, models or processes are
read or changed. Fixture media bytes establish identity only; no decoding runs.
"""
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def raw_json(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def write(path, raw, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def manifest_for(installer, bundle, version, source, release=None):
    files = installer.inventory(bundle)
    content = sha(json.dumps(files, sort_keys=True).encode())
    release = release or f'{version}-{source[:12]}-{content[:12]}'
    value = {'schema': 1, 'app_id': installer.APP_ID, 'version': version,
             'release': release, 'architecture': 'x86_64', 'platform': 'Linux',
             'source_commit': source, 'source_tree_sha256': 'd' * 64,
             'source_dirty': False, 'bundle_sha256': content, 'files': files}
    write(bundle / '.pbs-release.json', raw_json(value))
    return value


def tree_state(path):
    return {str(p.relative_to(path)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode), p.stat().st_mtime_ns)
            for p in path.rglob('*') if p.is_file() and not p.is_symlink()}


@pytest.fixture
def stations(tmp_path):
    installer = importlib.import_module('linux_installer')
    hotfix = importlib.import_module('rc10_hotfix')
    control = importlib.import_module('controlled_activation')
    snapshot = importlib.import_module('station_snapshot')
    from gnb_player.lyrics import Cue, LyricsDocument, fingerprint, save_lyrics
    from gnb_player.lyric_review import document_revision, save_review_document
    from gnb_player.recognition_drafts import create_initial_draft, save_user_draft

    guard = '_internal/gnb_player/extraction_backend/lyrics_review.py'
    old_release = '0.2.4rc10-fixture-baseline'
    before_guard, approved_guard = b'original fixture guard\n', b'approved fixture neighbour guard\n'
    rows = []
    policy = None
    for index, name in enumerate(('alpha', 'beta')):
        root = tmp_path / 'installations' / (name + ' app')
        old = root / 'releases' / old_release
        binary = write(old / installer.EXECUTABLE,
                       b'#!/usr/bin/env python3\nprint("Plague\'s Broadcast Station 0.2.4rc10")\n', 0o755)
        write(old / guard, before_guard)
        write(old / 'licenses' / 'fixture.txt', b'fixture native package notice\n')
        for directory in [old, *(p for p in old.rglob('*') if p.is_dir())]: directory.chmod(0o755)
        baseline = manifest_for(installer, old, '0.2.4rc10', 'b' * 40, old_release)
        original_manifest = (old / '.pbs-release.json').read_bytes()
        if policy is None:
            policy = hotfix.Policy(release=old_release, version='0.2.4rc10', source_commit='b' * 40,
                original_manifest_sha256=sha(original_manifest), binary_sha256=sha(binary.read_bytes()),
                guard_path=guard, original_guard_sha256=sha(before_guard),
                approved_guard_sha256=sha(approved_guard), directory_mode=0o755)
        write(old / guard, approved_guard)  # Original manifest is deliberately retained.
        candidate = root / 'releases' / 'candidate-staging'
        write(candidate / installer.EXECUTABLE,
              b'#!/usr/bin/env python3\nprint("Plague\'s Broadcast Station 0.2.4rc11.dev1")\n', 0o755)
        write(candidate / guard, approved_guard)
        write(candidate / 'licenses' / 'fixture.txt', b'fixture candidate package notice\n')
        development = manifest_for(installer, candidate, '0.2.4rc11.dev1', 'c' * 40)
        staged = candidate.with_name(development['release'])
        candidate.rename(staged)
        data = tmp_path / ('station ' + name + ' state')
        settings = {'extraction': {'use_gpu': bool(index), 'separate_vocals': not bool(index),
                                  'python_path': '/fixture/runtime unchanged'},
                    'operator_preference': 'keep ' + name}
        write(data / 'settings.json', raw_json(settings), 0o640)
        write(data / 'session.json', raw_json({'version': 1, 'name': name, 'tracks': [], 'settings': {'preview': bool(index)}}), 0o600)
        source = write(data / 'recordings' / 'identity only.wav', b'Disposable recording identity ' + name.encode())
        identity = fingerprint(source)
        cache = data / 'lyrics-cache'
        lyric = cache / 'lyrics' / (identity + '.json')
        document = LyricsDocument([Cue('Human checked ' + name + ' — 雨', 1, 3)], 5,
                                   source_sha256=identity, quality={'status': 'review'})
        save_lyrics(document, lyric)
        approved = save_review_document(document, lyric, source, document_revision(document), approve=True)
        # A second saved approved revision establishes real revision history.
        save_review_document(approved, lyric, source, document_revision(approved), approve=True)
        failure = write(cache / 'failures' / (identity + '-' + '1' * 32 + '.json'), raw_json({
            'version': 'extraction-failure-1', 'status': 'failed', 'source_sha256': identity,
            'created_at': 1, 'message': 'Generated fixture rejection',
            'recognition_evidence': {'pre_alignment': [{'text': 'Machine fixture words'}]}}), 0o600)
        machine = create_initial_draft(cache, identity, 'Machine fixture words', failure)
        human = save_user_draft(machine.path, 'Human untimed correction ' + name + '\nsecond line',
                                expected_revision=machine.revision, expected_source_sha256=identity)
        model = write(data / 'models' / 'keep-model.bin', b'Fixture model sentinel: never copy or alter')
        write(root / '.pbs-installation', installer.MARKER.encode())
        state = {'app_id': installer.APP_ID, 'schema': 1, 'current': old_release,
                 'previous': 'retained-older-fixture', 'data_dir': str(data)}
        write(root / 'install.json', raw_json(state), 0o600)
        write(root / 'releases' / state['previous'] / 'keep.txt', b'older rollback fixture: retain')
        rows.append(SimpleNamespace(name=name, root=root, old=old, candidate=staged,
            development=development, data=data, settings=settings, lyric=lyric, source=source,
            identity=identity, human=human, model=model, original_manifest=original_manifest,
            before=tree_state(data), original_state=(root / 'install.json').read_bytes()))
    selected = [snapshot.Station(row.name, row.data,
        ('settings.json', 'session.json', 'lyrics-cache/lyrics',
         'lyrics-cache/recognition-drafts', 'lyrics-cache/failures')) for row in rows]
    return SimpleNamespace(installer=installer, hotfix=hotfix, control=control,
        snapshot=snapshot, rows=rows, selected=selected, policy=policy, base=tmp_path)


def capture(files, name='private-snapshot'):
    plan = files.snapshot.make_plan(files.selected)
    result = files.snapshot.capture_snapshot(plan, files.base / name, approved=True, apps_closed=True)
    return result


def prepared(files, row, action='activate', suffix=''):
    plan = (files.control.plan_activation(row.root, row.development['release'],
                expected_state_sha256=sha((row.root / 'install.json').read_bytes()), policy=files.policy)
            if action == 'activate' else files.control.plan_rollback(row.root,
                expected_state_sha256=sha((row.root / 'install.json').read_bytes()), policy=files.policy))
    audit = files.base / (row.name + '-' + action + suffix)
    files.control.prepare(plan, audit, approved=True, apps_closed=True, policy=files.policy)
    return audit


def assert_human_state(row):
    from gnb_player.lyrics import load_lyrics, fingerprint
    from gnb_player.lyric_review import is_approved
    from gnb_player.recognition_drafts import load_draft
    assert tree_state(row.data) == row.before
    assert is_approved(load_lyrics(row.lyric), fingerprint(row.source))
    assert load_draft(row.human.path, expected_source_sha256=row.identity).raw_payload == row.human.raw_payload
    assert json.loads((row.data / 'settings.json').read_text()) == row.settings


def test_two_station_snapshot_and_code_roundtrip_preserve_real_human_revisions(stations):
    files = stations
    saved = capture(files)
    manifest = files.snapshot.verify_snapshot(saved['path'], expected_manifest_sha256=saved['manifest_sha256'])
    assert not any('models' in entry['path'] or 'recordings' in entry['path'] for entry in manifest['entries'].values())
    for row in files.rows:
        with pytest.raises(ValueError):
            files.installer.verify_release(row.old)  # Generic strict rule stays strict.
        audit = prepared(files, row)
        assert (audit / 'rc10-hotfix' / 'original-manifest.json').read_bytes() == row.original_manifest
        derived = json.loads((audit / 'rc10-hotfix' / 'patched-manifest.json').read_text())
        assert derived != json.loads(row.original_manifest)
        selected = files.control.apply(audit, approved=True, apps_closed=True, policy=files.policy)
        assert selected['selection'] == 'after' and selected['phase'] == 'committed'
        state = json.loads((row.root / 'install.json').read_text())
        assert state['current'] == row.development['release'] and state['data_dir'] == str(row.data)
        assert files.installer.verify_release(files.installer.release_path(row.root, state['current'])) == row.development
        assert_human_state(row)
        rollback = prepared(files, row, 'rollback')
        files.control.apply(rollback, approved=True, apps_closed=True, policy=files.policy)
        state = json.loads((row.root / 'install.json').read_text())
        assert state['current'] == row.old.name and state['data_dir'] == str(row.data)
        assert (row.old / '.pbs-release.json').read_bytes() == row.original_manifest
        assert (row.root / 'releases/retained-older-fixture/keep.txt').exists()
        assert_human_state(row)


@pytest.mark.parametrize('approved,closed', [(False, False), (False, True), (True, False)])
def test_mutation_gates_refuse_without_any_station_or_selection_change(stations, approved, closed):
    files = stations
    row = files.rows[0]
    plan = files.control.plan_activation(row.root, row.development['release'], policy=files.policy)
    audit = files.base / 'not-authorized'
    with pytest.raises(ValueError):
        files.control.prepare(plan, audit, approved=approved, apps_closed=closed, policy=files.policy)
    assert not audit.exists()
    assert (row.root / 'install.json').read_bytes() == row.original_state
    before = tree_state(row.data)
    snapshot_plan = files.snapshot.make_plan(files.selected)
    with pytest.raises(ValueError):
        files.snapshot.capture_snapshot(snapshot_plan, files.base / 'no-snapshot', approved=approved, apps_closed=closed)
    assert not (files.base / 'no-snapshot').exists() and tree_state(row.data) == before


@pytest.mark.parametrize('after_write', [False, True])
def test_interrupted_activation_recovers_only_known_selection_and_keeps_both_stations(stations, monkeypatch, after_write):
    files, row = stations, stations.rows[0]
    audit = prepared(files, row)
    original = files.control._replace_selection
    def interrupted(root, raw):
        if after_write: original(root, raw)
        raise OSError('Fixture interruption ' + ('after' if after_write else 'before') + ' atomic selection')
    with monkeypatch.context() as patch:
        patch.setattr(files.control, '_replace_selection', interrupted)
        with pytest.raises(ValueError):
            files.control.apply(audit, approved=True, apps_closed=True, policy=files.policy)
    actual = files.control.inspect_transaction(audit)
    assert actual['selection'] == ('after' if after_write else 'before')
    recovered = files.control.recover(audit, approved=True, apps_closed=True, policy=files.policy)
    assert recovered['phase'] == ('committed' if after_write else 'prepared')
    for current in files.rows: assert_human_state(current)
    if not after_write:
        files.control.apply(audit, approved=True, apps_closed=True, policy=files.policy)
    assert json.loads((row.root / 'install.json').read_text())['current'] == row.development['release']


def test_changed_selection_and_unknown_rollback_bytes_are_never_overwritten(stations):
    files, row = stations, stations.rows[0]
    audit = prepared(files, row)
    changed = json.loads(row.original_state)
    changed['previous'] = 'operator-selected-new-previous'
    write(row.root / 'install.json', raw_json(changed), 0o600)
    expected = (row.root / 'install.json').read_bytes()
    for action in (files.control.apply, files.control.recover):
        with pytest.raises(ValueError): action(audit, approved=True, apps_closed=True, policy=files.policy)
        assert (row.root / 'install.json').read_bytes() == expected
    write(row.root / 'install.json', row.original_state, 0o600)
    files.control.apply(audit, approved=True, apps_closed=True, policy=files.policy)
    rollback = prepared(files, row, 'rollback')
    guard = row.old / files.policy.guard_path
    write(guard, b'Unapproved new patch must remain unselected')
    active = (row.root / 'install.json').read_bytes()
    with pytest.raises(ValueError):
        files.control.apply(rollback, approved=True, apps_closed=True, policy=files.policy)
    assert (row.root / 'install.json').read_bytes() == active
    assert guard.read_bytes() == b'Unapproved new patch must remain unselected'
    for current in files.rows: assert_human_state(current)


def test_snapshot_plan_refuses_newer_human_bytes_in_either_station(stations):
    files = stations
    plan = files.snapshot.make_plan(files.selected)
    changed = files.rows[1].data / 'settings.json'
    write(changed, b'{"human":"new preference must survive"}', 0o640)
    before = tree_state(files.rows[1].data)
    with pytest.raises(ValueError):
        files.snapshot.capture_snapshot(plan, files.base / 'stale-capture', approved=True, apps_closed=True)
    assert not (files.base / 'stale-capture').exists()
    assert tree_state(files.rows[1].data) == before


def restore_plan(files, saved):
    current = files.snapshot.inspect_current(saved['path'], expected_manifest_sha256=saved['manifest_sha256'])
    return files.snapshot.plan_restore(saved['path'], expected_manifest_sha256=saved['manifest_sha256'],
                                       expected_current=current)


def changed_preferences(files):
    for row in files.rows:
        settings = deepcopy(row.settings)
        settings['operator_preference'] = 'Newer explicit preference ' + row.name
        write(row.data / 'settings.json', raw_json(settings), 0o640)


def test_private_snapshot_optional_restore_retains_both_approvals_and_model_sentinels(stations):
    files = stations
    saved = capture(files)
    folder = Path(saved['path'])
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in folder.rglob('*') if p.is_file())
    models = [(row.model.read_bytes(), row.model.stat().st_mtime_ns) for row in files.rows]
    changed_preferences(files)
    plan = restore_plan(files, saved)
    done = files.snapshot.restore_snapshot(plan, files.base / 'explicit-data-restore', approved=True, apps_closed=True)
    assert done['status'] == 'complete'
    assert done['intent_sha256'] == plan['intent_sha256']
    for index, row in enumerate(files.rows):
        assert_human_state(row)
        assert (row.model.read_bytes(), row.model.stat().st_mtime_ns) == models[index]
        assert (row.root / 'install.json').read_bytes() == row.original_state


def test_stale_restore_inventory_cannot_discard_newer_untimed_human_revision(stations):
    from gnb_player.recognition_drafts import save_user_draft
    files = stations
    saved = capture(files)
    stale = files.snapshot.inspect_current(saved['path'], expected_manifest_sha256=saved['manifest_sha256'])
    row = files.rows[1]
    latest = save_user_draft(row.human.path, 'A newer human correction must not disappear',
                            expected_revision=row.human.revision, expected_source_sha256=row.identity)
    before = tree_state(row.data)
    with pytest.raises(ValueError):
        files.snapshot.plan_restore(saved['path'], expected_manifest_sha256=saved['manifest_sha256'],
                                    expected_current=stale)
    assert tree_state(row.data) == before and row.human.path.read_bytes() == latest.raw_payload


def interrupted_restore(files, saved, monkeypatch, name):
    changed_preferences(files)
    original = files.snapshot._write_target
    first_settings = str(files.rows[0].data / 'settings.json')
    plan = restore_plan(files, saved)
    before = [tree_state(row.data) for row in files.rows]
    def stop_after_settings(path, raw, metadata, **kwargs):
        result = original(path, raw, metadata, **kwargs)
        if str(path) == first_settings:
            raise OSError('Fixture interruption after one settings rename')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(files.snapshot, '_write_target', stop_after_settings)
        with pytest.raises(files.snapshot.SnapshotError) as failure:
            files.snapshot.restore_snapshot(plan, files.base / name, approved=True, apps_closed=True)
    assert failure.value.intent_sha256 == plan['intent_sha256']
    assert Path(failure.value.journal_path).exists()
    return plan, Path(failure.value.journal_path), before


def test_interrupted_data_restore_recovers_exact_known_before_state_for_both_stations(stations, monkeypatch):
    files = stations
    saved = capture(files)
    plan, journal, before = interrupted_restore(files, saved, monkeypatch, 'interrupted-data')
    recovered = files.snapshot.recover_restore(journal,
        expected_manifest_sha256=saved['manifest_sha256'], expected_intent_sha256=plan['intent_sha256'],
        approved=True, apps_closed=True)
    assert recovered['status'] == 'recovered'
    assert [tree_state(row.data) for row in files.rows] == before
    for row in files.rows:
        assert (row.root / 'install.json').read_bytes() == row.original_state


@pytest.mark.parametrize('corruption', ['outside-directory', 'remove-before-state'])
def test_corrupt_data_recovery_cannot_remove_unrelated_dirs_or_existing_human_files(stations, monkeypatch, corruption):
    files = stations
    saved = capture(files)
    plan, journal, _ = interrupted_restore(files, saved, monkeypatch, 'corrupted-recovery')
    outside = files.base / 'unrelated empty directory'
    outside.mkdir()
    if corruption == 'outside-directory':
        progress = json.loads(journal.read_text())
        progress['created_directories'] = [str(outside)]
        write(journal, raw_json(progress), 0o600)
    else:
        intent_path = journal.parent / 'intent.json'
        intent = json.loads(intent_path.read_text())
        first = next(iter(intent['operations']))
        assert intent['operations'][first]['before'] is not None
        intent['operations'][first]['before'] = None
        write(intent_path, raw_json(intent), 0o600)
        # A mutable progress file cannot replace the separately reviewed pin.
        progress = json.loads(journal.read_text())
        progress['intent_sha256'] = sha(intent_path.read_bytes())
        write(journal, raw_json(progress), 0o600)
    before = [tree_state(row.data) for row in files.rows]
    with pytest.raises(ValueError):
        files.snapshot.recover_restore(journal,
            expected_manifest_sha256=saved['manifest_sha256'], expected_intent_sha256=plan['intent_sha256'],
            approved=True, apps_closed=True)
    assert outside.is_dir()
    assert [tree_state(row.data) for row in files.rows] == before


def test_data_recovery_refuses_unknown_human_changes_before_any_further_restore(stations, monkeypatch):
    files = stations
    saved = capture(files)
    plan, journal, _ = interrupted_restore(files, saved, monkeypatch, 'pause-for-operator')
    write(files.rows[1].data / 'settings.json', b'{"human":"new edit while paused"}', 0o640)
    before = [tree_state(row.data) for row in files.rows]
    with pytest.raises(ValueError):
        files.snapshot.recover_restore(journal,
            expected_manifest_sha256=saved['manifest_sha256'], expected_intent_sha256=plan['intent_sha256'],
            approved=True, apps_closed=True)
    assert [tree_state(row.data) for row in files.rows] == before


def test_snapshot_space_floor_and_explicit_scope_exclusions_do_not_start_copy(stations, monkeypatch):
    files = stations
    plan = files.snapshot.make_plan(files.selected)
    with monkeypatch.context() as patch:
        patch.setattr(files.snapshot.shutil, 'disk_usage', lambda _: SimpleNamespace(free=20 * 1024**3 + 1))
        with pytest.raises(ValueError):
            files.snapshot.capture_snapshot(plan, files.base / 'below-reserve', approved=True, apps_closed=True)
    assert not (files.base / 'below-reserve').exists()
    for scope in ('models', 'recordings', 'native-keyring', 'model-setup'):
        bad = [files.snapshot.Station('bad-scope', files.rows[0].data, (scope,))]
        with pytest.raises(ValueError): files.snapshot.make_plan(bad)
    for row in files.rows: assert_human_state(row)


@pytest.mark.parametrize('change', ['guard-bytes', 'guard-mode', 'extra-file', 'extra-directory'])
def test_scoped_hotfix_verification_rejects_unknown_code_inventory_or_modes(stations, change):
    files, row = stations, stations.rows[0]
    if change == 'guard-bytes': write(row.old / files.policy.guard_path, b'Other unapproved guard')
    elif change == 'guard-mode': (row.old / files.policy.guard_path).chmod(0o600)
    elif change == 'extra-file': write(row.old / 'unexpected-source.py', b'print("unapproved")')
    else: (row.old / 'unexpected-empty-directory').mkdir(mode=0o755)
    with pytest.raises(ValueError): files.hotfix.verify_rc10_hotfix(row.old, policy=files.policy)
    assert (row.root / 'install.json').read_bytes() == row.original_state
    for current in files.rows: assert_human_state(current)


@pytest.mark.parametrize('after_write', [False, True])
def test_interrupted_code_rollback_requires_known_journal_and_retains_human_state(stations, monkeypatch, after_write):
    files, row = stations, stations.rows[0]
    files.control.apply(prepared(files, row), approved=True, apps_closed=True, policy=files.policy)
    audit = prepared(files, row, 'rollback')
    original = files.control._replace_selection
    def interrupted(root, raw):
        if after_write: original(root, raw)
        raise OSError('Fixture rollback interruption')
    with monkeypatch.context() as patch:
        patch.setattr(files.control, '_replace_selection', interrupted)
        with pytest.raises(ValueError):
            files.control.apply(audit, approved=True, apps_closed=True, policy=files.policy)
    result = files.control.recover(audit, approved=True, apps_closed=True, policy=files.policy)
    assert result['selection'] == ('after' if after_write else 'before')
    if not after_write:
        files.control.apply(audit, approved=True, apps_closed=True, policy=files.policy)
    assert json.loads((row.root / 'install.json').read_text())['current'] == row.old.name
    for current in files.rows: assert_human_state(current)


def test_metadata_only_development_relabel_is_refused_by_approved_version_probe(stations):
    files, row = stations, stations.rows[0]
    binary = row.candidate / files.installer.EXECUTABLE
    write(binary, b'#!/usr/bin/env python3\nprint("Plague\'s Broadcast Station 0.2.4rc10")\n', 0o755)
    declared = manifest_for(files.installer, row.candidate, '0.2.4rc11.dev1', 'c' * 40)
    renamed = row.candidate.with_name(declared['release'])
    row.candidate.rename(renamed)
    plan = files.control.plan_activation(row.root, declared['release'], policy=files.policy)
    with pytest.raises(ValueError, match='version'):
        files.control.prepare(plan, files.base / 'dishonest-version-audit', approved=True, apps_closed=True, policy=files.policy)
    assert (row.root / 'install.json').read_bytes() == row.original_state
    for current in files.rows: assert_human_state(current)


def test_data_recovery_preserves_new_human_file_added_after_interruption(stations, monkeypatch):
    files = stations
    saved = capture(files)
    plan, journal, _ = interrupted_restore(files, saved, monkeypatch, 'pause-before-new-file')
    added = files.rows[1].data / 'lyrics-cache/lyrics/manual-new-human-revision.json'
    write(added, b'{"human_review":{"status":"approved"},"text":"new operator revision"}', 0o600)
    before = [tree_state(row.data) for row in files.rows]
    with pytest.raises(ValueError):
        files.snapshot.recover_restore(journal,
            expected_manifest_sha256=saved['manifest_sha256'], expected_intent_sha256=plan['intent_sha256'],
            approved=True, apps_closed=True)
    assert added.read_bytes().startswith(b'{"human_review"')
    assert [tree_state(row.data) for row in files.rows] == before
