"""Opaque station snapshots/restore; generated fixtures only, no real paths."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

PATH=Path(__file__).resolve().parents[1]/'scripts/station_snapshot.py'
spec=importlib.util.spec_from_file_location('station_snapshot_under_test',PATH)
snapshot=importlib.util.module_from_spec(spec);sys.modules[spec.name]=snapshot;spec.loader.exec_module(snapshot)


@pytest.fixture
def pair(tmp_path):
    stations=[]
    for index,name in enumerate(('station','radio')):
        root=tmp_path/name;root.mkdir()
        values={'settings.json':json.dumps({'gpu':bool(index),'separate_vocals':not bool(index),'runtime':'keep selected runtime'}).encode(),
                'session.json':b'{"selection":"original station selection"}',
                'broadcast-settings.json':b'{"schema":1,"destination":"rtmps://dummy.invalid/live2","remember":false}',
                'broadcast-profile.json':b'{"schema":2,"active_slot":1,"credential_revision":"nonsecret fixture reference"}',
                'broadcast-profile-attempt.json':b'{"schema":1}',
                'output-diagnostics.json':b'{"output":"dummy redacted data"}',
                'lyrics-cache/lyrics/approved.json':b'{"human_review":{"status":"approved"},"segments":[{"text":"Keep human words"}]}',
                'lyrics-cache/lyrics/history/prior.json':b'original saved lyric revision',
                'lyrics-cache/recognition-drafts/human.json':b'{"status":"unaligned","text":"Human correction"}',
                'lyrics-cache/failures/raw.json':b'{"status":"failed","original":"machine evidence"}'}
        for number,(relative,raw) in enumerate(values.items()):
            path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            path.chmod(0o600 if number%2 else 0o640)
            os.utime(path,ns=(1600000000000000000+number,1600000000000000000+number))
        model=root/'model-setup/models/large.bin';model.parent.mkdir(parents=True);model.write_bytes(b'untouched model sentinel')
        media=root/'recordings/song.wav';media.parent.mkdir();media.write_bytes(b'untouched media sentinel')
        paths=('settings.json','session.json','broadcast-settings.json','broadcast-profile.json','broadcast-profile-attempt.json','output-diagnostics.json','lyrics-cache/lyrics','lyrics-cache/recognition-drafts','lyrics-cache/failures')
        stations.append(snapshot.Station(name,root,paths))
    return stations


def files(stations):
    return {str(path):(path.read_bytes(),stat.S_IMODE(path.stat().st_mode),path.stat().st_mtime_ns)
            for station in stations for path in Path(station.data_dir).rglob('*') if path.is_file()}


def take(pair,tmp_path):
    plan=snapshot.make_plan(pair)
    captured=snapshot.capture_snapshot(plan,tmp_path/'snapshot',approved=True,apps_closed=True)
    return plan,captured


def restore_plan(captured):
    current=snapshot.inspect_current(captured['path'],expected_manifest_sha256=captured['manifest_sha256'])
    return snapshot.plan_restore(captured['path'],expected_manifest_sha256=captured['manifest_sha256'],expected_current=current)


def test_both_stations_private_complete_snapshot_and_restore_preserve_original_data(pair,tmp_path):
    before=files(pair);plan,captured=take(pair,tmp_path)
    assert files(pair)==before
    assert 'large.bin' not in json.dumps(plan) and 'song.wav' not in json.dumps(plan)
    root=Path(captured['path']);manifest=snapshot.verify_snapshot(root,expected_manifest_sha256=captured['manifest_sha256'])
    assert len(manifest['entries'])==20
    for path in root.rglob('*'):
        assert stat.S_IMODE(path.stat().st_mode)==(0o700 if path.is_dir() else 0o600)
    for station in pair:
        (Path(station.data_dir)/'settings.json').write_bytes(b'approved test preference change')
        (Path(station.data_dir)/'lyrics-cache/lyrics/approved.json').write_bytes(b'explicit test correction awaiting reviewed restore')
    plan=restore_plan(captured)
    result=snapshot.restore_snapshot(plan,tmp_path/'restore',approved=True,apps_closed=True)
    assert result['status']=='complete' and files(pair)==before
    assert json.loads((Path(pair[0].data_dir)/'lyrics-cache/lyrics/approved.json').read_text())['human_review']['status']=='approved'


def test_mutation_requires_explicit_approval_and_closed_writers(pair,tmp_path):
    plan=snapshot.make_plan(pair)
    for flags in ({},{'approved':True},{'apps_closed':True}):
        with pytest.raises(snapshot.SnapshotError,match='approval'):
            snapshot.capture_snapshot(plan,tmp_path/'not created',**flags)
    assert not (tmp_path/'not created').exists()


def test_changed_state_after_plan_never_creates_snapshot(pair,tmp_path):
    plan=snapshot.make_plan(pair)
    (Path(pair[0].data_dir)/'settings.json').write_bytes(b'new human settings')
    with pytest.raises(snapshot.SnapshotError,match='changed since planning'):
        snapshot.capture_snapshot(plan,tmp_path/'not created',approved=True,apps_closed=True)
    assert not (tmp_path/'not created').exists()


@pytest.mark.parametrize('path',['model-setup','model-cache','recordings','unknown.json'])
def test_unknown_or_model_media_scopes_refused(pair,path):
    with pytest.raises(snapshot.SnapshotError,match='Unknown scope'):
        snapshot.make_plan([snapshot.Station('station',pair[0].data_dir,(path,))])


def test_requested_missing_and_overlapping_roots_or_backup_refused(pair,tmp_path):
    with pytest.raises(snapshot.SnapshotError):snapshot.make_plan([snapshot.Station('station',pair[0].data_dir,('references',))])
    with pytest.raises(snapshot.SnapshotError,match='overlap'):
        snapshot.make_plan([pair[0],snapshot.Station('nested',Path(pair[0].data_dir)/'lyrics-cache',('lyrics',))])
    plan=snapshot.make_plan(pair)
    with pytest.raises(snapshot.SnapshotError,match='inside station'):
        snapshot.capture_snapshot(plan,Path(pair[0].data_dir)/'snapshot',approved=True,apps_closed=True)


def test_symlink_directory_file_hardlink_and_unknown_tree_file_refused(pair,tmp_path):
    root=Path(pair[0].data_dir);file=root/'lyrics-cache/lyrics/approved.json'
    file.unlink();file.symlink_to(root/'settings.json')
    with pytest.raises(snapshot.SnapshotError):snapshot.make_plan(pair)
    file.unlink();os.link(root/'settings.json',file)
    with pytest.raises(snapshot.SnapshotError):snapshot.make_plan(pair)
    file.unlink();file.write_bytes(b'normal metadata')
    (root/'lyrics-cache/lyrics/weights.bin').write_bytes(b'do not read as metadata')
    with pytest.raises(snapshot.SnapshotError,match='unknown file type'):snapshot.make_plan(pair)


def test_explicit_external_lyrics_only_and_not_implicitly_discovered(pair,tmp_path):
    external=tmp_path/'external lyric.json';external.write_bytes(b'externally linked approved revision')
    assert str(external) not in json.dumps(snapshot.make_plan(pair))
    selected=[snapshot.Station(pair[0].name,pair[0].data_dir,pair[0].paths,(external,)),pair[1]]
    _,captured=take(selected,tmp_path)
    external.write_bytes(b'explicit newer external fixture correction')
    snapshot.restore_snapshot(restore_plan(captured),tmp_path/'restore',approved=True,apps_closed=True)
    assert external.read_bytes()==b'externally linked approved revision'
    with pytest.raises(snapshot.SnapshotError):snapshot.make_plan([snapshot.Station('station',pair[0].data_dir,pair[0].paths,(tmp_path/'missing.json',))])


def test_space_floor_checked_before_snapshot_or_restore_directory(pair,tmp_path,monkeypatch):
    plan=snapshot.make_plan(pair)
    monkeypatch.setattr(snapshot.shutil,'disk_usage',lambda _:SimpleNamespace(free=snapshot.RESERVE_BYTES))
    with pytest.raises(snapshot.SnapshotError,match='20 GiB'):
        snapshot.capture_snapshot(plan,tmp_path/'not created',approved=True,apps_closed=True)
    assert not (tmp_path/'not created').exists()


def test_interrupted_snapshot_has_no_complete_marker_and_cannot_verify(pair,tmp_path,monkeypatch):
    plan=snapshot.make_plan(pair);original=snapshot._atomic
    def fail(path,raw,**kwargs):
        if Path(path).name=='00003.bin':raise OSError('fixture interruption')
        return original(path,raw,**kwargs)
    monkeypatch.setattr(snapshot,'_atomic',fail)
    with pytest.raises(OSError):snapshot.capture_snapshot(plan,tmp_path/'partial',approved=True,apps_closed=True)
    assert not (tmp_path/'partial/complete.json').exists()
    with pytest.raises((snapshot.SnapshotError,OSError)):
        snapshot.verify_snapshot(tmp_path/'partial',expected_manifest_sha256='a'*64)


def test_snapshot_data_manifest_or_pin_corruption_refuses_restore(pair,tmp_path):
    _,captured=take(pair,tmp_path)
    with pytest.raises(snapshot.SnapshotError,match='pinned'):
        snapshot.verify_snapshot(captured['path'],expected_manifest_sha256='a'*64)
    content=next((Path(captured['path'])/'content').iterdir());content.write_bytes(b'corrupt fixture')
    with pytest.raises(snapshot.SnapshotError,match='content changed'):
        snapshot.inspect_current(captured['path'],expected_manifest_sha256=captured['manifest_sha256'])


def test_restore_requires_reviewed_inventory_and_rejects_newer_human_edit(pair,tmp_path):
    _,captured=take(pair,tmp_path)
    expected=snapshot.inspect_current(captured['path'],expected_manifest_sha256=captured['manifest_sha256'])
    path=Path(pair[1].data_dir)/'lyrics-cache/lyrics/approved.json';path.write_bytes(b'new human correction')
    with pytest.raises(snapshot.SnapshotError,match='explicitly reviewed'):
        snapshot.plan_restore(captured['path'],expected_manifest_sha256=captured['manifest_sha256'],expected_current=expected)
    plan=restore_plan(captured);path.write_bytes(b'even newer human correction')
    with pytest.raises(snapshot.SnapshotError,match='Newer station state'):
        snapshot.restore_snapshot(plan,tmp_path/'restore',approved=True,apps_closed=True)
    assert path.read_bytes()==b'even newer human correction' and not (tmp_path/'restore').exists()


def interrupted_restore(pair,tmp_path,monkeypatch):
    _,captured=take(pair,tmp_path)
    for station in pair:(Path(station.data_dir)/'settings.json').write_bytes(b'reviewed modified settings before restoration')
    before=files(pair);plan=restore_plan(captured);original=snapshot._write_target;calls=[]
    def fail(path,raw,metadata,**kwargs):
        calls.append(path)
        if len(calls)==3:raise OSError('interruption before third atomic replacement')
        return original(path,raw,metadata,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(snapshot,'_write_target',fail)
        with pytest.raises(snapshot.SnapshotError) as error:
            snapshot.restore_snapshot(plan,tmp_path/'restore',approved=True,apps_closed=True)
    return captured,plan,error.value.journal_path,before


def test_interrupted_restore_journal_can_recover_both_stations(pair,tmp_path,monkeypatch):
    captured,plan,journal,before=interrupted_restore(pair,tmp_path,monkeypatch)
    result=snapshot.recover_restore(journal,expected_manifest_sha256=captured['manifest_sha256'],expected_intent_sha256=plan['intent_sha256'],approved=True,apps_closed=True)
    assert result['status']=='recovered' and files(pair)==before


def test_recovery_does_not_overwrite_post_failure_human_edit(pair,tmp_path,monkeypatch):
    captured,plan,journal,before=interrupted_restore(pair,tmp_path,monkeypatch)
    path=Path(pair[0].data_dir)/'settings.json';path.write_bytes(b'new human edit after interruption')
    with pytest.raises(snapshot.SnapshotError,match='unknown/newer edit'):
        snapshot.recover_restore(journal,expected_manifest_sha256=captured['manifest_sha256'],expected_intent_sha256=plan['intent_sha256'],approved=True,apps_closed=True)
    assert path.read_bytes()==b'new human edit after interruption'


def test_corrupted_progress_or_pinned_intent_cannot_delete_unrelated_directory(pair,tmp_path,monkeypatch):
    captured,plan,journal,before=interrupted_restore(pair,tmp_path,monkeypatch)
    unrelated=tmp_path/'unrelated';unrelated.mkdir()
    path=Path(journal);value=json.loads(path.read_text());value['created_directories']=[str(unrelated)];path.write_text(json.dumps(value))
    with pytest.raises(snapshot.SnapshotError,match='corrupted recovery journal'):
        snapshot.recover_restore(path,expected_manifest_sha256=captured['manifest_sha256'],expected_intent_sha256=plan['intent_sha256'],approved=True,apps_closed=True)
    assert unrelated.is_dir()
    value.pop('created_directories');path.write_text(json.dumps(value))
    intent_path=path.parent/'intent.json';intent=json.loads(intent_path.read_text());first=next(iter(intent['operations']));intent['operations'][first]['before']=None;intent_path.write_text(json.dumps(intent))
    with pytest.raises(snapshot.SnapshotError,match='pinned receipt'):
        snapshot.recover_restore(path,expected_manifest_sha256=captured['manifest_sha256'],expected_intent_sha256=plan['intent_sha256'],approved=True,apps_closed=True)
    assert unrelated.is_dir()


def test_known_empty_lock_placeholders_preserved_but_unknown_locks_refused(pair,tmp_path):
    root=Path(pair[0].data_dir)
    lyric_lock=root/'lyrics-cache/lyrics/.approved.json.review.lock';lyric_lock.write_bytes(b'')
    draft_lock=root/'lyrics-cache/recognition-drafts'/('.'+'a'*64+'.json.lock');draft_lock.write_bytes(b'\0')
    _,captured=take(pair,tmp_path)
    manifest=snapshot.verify_snapshot(captured['path'],expected_manifest_sha256=captured['manifest_sha256'])
    assert any(entry['path']==str(lyric_lock) for entry in manifest['entries'].values())
    (root/'lyrics-cache/lyrics/unknown.lock').write_bytes(b'')
    with pytest.raises(snapshot.SnapshotError):snapshot.make_plan(pair)


def test_restore_missing_selected_tree_and_file_without_deleting_newer_extra_file(pair,tmp_path):
    import shutil
    before=files(pair);_,captured=take(pair,tmp_path)
    root=Path(pair[0].data_dir)
    (root/'settings.json').unlink()
    shutil.rmtree(root/'lyrics-cache/lyrics')
    extra=root/'lyrics-cache/recognition-drafts/new-human.json';extra.write_bytes(b'newer untimed human text kept')
    plan=restore_plan(captured)
    result=snapshot.restore_snapshot(plan,tmp_path/'restore',approved=True,apps_closed=True)
    assert result['status']=='complete'
    for name,value in before.items():
        path=Path(name)
        assert (path.read_bytes(),stat.S_IMODE(path.stat().st_mode),path.stat().st_mtime_ns)==value
    assert extra.read_bytes()==b'newer untimed human text kept'


def test_recovery_refuses_new_directory_permissions_before_any_mutation(pair,tmp_path,monkeypatch):
    captured,plan,journal,_=interrupted_restore(pair,tmp_path,monkeypatch)
    directory=Path(pair[0].data_dir)/'lyrics-cache/lyrics'
    directory.chmod(0o700)
    current=files(pair)
    with pytest.raises(snapshot.SnapshotError,match='unknown/newer permissions'):
        snapshot.recover_restore(journal,expected_manifest_sha256=captured['manifest_sha256'],expected_intent_sha256=plan['intent_sha256'],approved=True,apps_closed=True)
    assert stat.S_IMODE(directory.stat().st_mode)==0o700 and files(pair)==current


def test_recovery_refuses_post_failure_new_human_file_before_any_mutation(pair,tmp_path,monkeypatch):
    captured,plan,journal,_=interrupted_restore(pair,tmp_path,monkeypatch)
    extra=Path(pair[0].data_dir)/'lyrics-cache/lyrics/new-human.json';extra.write_bytes(b'new approved words')
    current=files(pair)
    with pytest.raises(snapshot.SnapshotError,match='Unknown/newer station files'):
        snapshot.recover_restore(journal,expected_manifest_sha256=captured['manifest_sha256'],expected_intent_sha256=plan['intent_sha256'],approved=True,apps_closed=True)
    assert files(pair)==current


def test_space_drop_during_staging_cannot_cross_reserve(pair,tmp_path,monkeypatch):
    root=Path(pair[0].data_dir);large=root/'settings.json';large.write_bytes(b'x'*(4*1024**2))
    selected=[snapshot.Station('station',root,('settings.json',))]
    _,captured=take(selected,tmp_path)
    large.write_bytes(b'y'*(4*1024**2));before=large.read_bytes()
    plan=restore_plan(captured)
    def space(path):
        free=50*1024**3 if not (tmp_path/'restore').exists() else snapshot.RESERVE_BYTES+2*1024**2
        return SimpleNamespace(free=free)
    monkeypatch.setattr(snapshot.shutil,'disk_usage',space)
    with pytest.raises(snapshot.SnapshotError,match='20 GiB'):
        snapshot.restore_snapshot(plan,tmp_path/'restore',approved=True,apps_closed=True)
    assert large.read_bytes()==before
    assert not list((tmp_path/'restore/before').iterdir())


def test_crash_after_target_rename_before_progress_write_is_recoverable(pair,tmp_path,monkeypatch):
    selected=[snapshot.Station(station.name,station.data_dir,('settings.json',)) for station in pair]
    _,captured=take(selected,tmp_path)
    for station in pair:(Path(station.data_dir)/'settings.json').write_bytes(b'reviewed test settings before restore')
    before=files(pair);plan=restore_plan(captured);original=snapshot._atomic
    def interrupted(path,raw,**kwargs):
        if Path(path).name=='journal.json':
            value=json.loads(raw)
            if value.get('status')=='restoring' and len(value.get('committed',[]))==1 and value.get('pending') is None:
                raise OSError('fixture interruption immediately after target rename')
        return original(path,raw,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(snapshot,'_atomic',interrupted)
        with pytest.raises(snapshot.SnapshotError) as error:
            snapshot.restore_snapshot(plan,tmp_path/'restore',approved=True,apps_closed=True)
    result=snapshot.recover_restore(error.value.journal_path,expected_manifest_sha256=captured['manifest_sha256'],expected_intent_sha256=plan['intent_sha256'],approved=True,apps_closed=True)
    assert result['status']=='recovered' and files(pair)==before


def test_loss_of_private_snapshot_permissions_refuses_use(pair,tmp_path):
    _,captured=take(pair,tmp_path)
    (Path(captured['path'])/'manifest.json').chmod(0o644)
    with pytest.raises(snapshot.SnapshotError,match='must remain private'):
        snapshot.verify_snapshot(captured['path'],expected_manifest_sha256=captured['manifest_sha256'])
