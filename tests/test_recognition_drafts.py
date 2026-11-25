"""Untimed recovery text survives editing without entering timed lyric storage."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import threading

import pytest

from gnb_player import recognition_drafts as drafts

SHA = 'a' * 64
OTHER = 'b' * 64


@pytest.fixture
def files(tmp_path):
    cache = tmp_path / 'private cache'
    failures = cache / 'failures'
    failures.mkdir(parents=True)
    failure = failures / (SHA + '-' + '1' * 32 + '.json')
    evidence = {'pre_alignment': [{'text': 'Thank you. ♪'}, {'text': '原始 line\nnext line'}]}
    failure.write_text(json.dumps({'version':'extraction-failure-1','source_sha256':SHA,
                                  'status':'failed','recognition_evidence':evidence}))
    canonical = cache / 'lyrics' / (SHA + '.json')
    canonical.parent.mkdir()
    canonical.write_bytes(b'untouched existing human lyric bytes')
    return cache, failure, canonical, evidence


def initial(files, text='Machine\n原始 words'):
    cache, failure, _, _ = files
    result = drafts.create_initial_draft(cache, SHA, text, failure)
    assert result is not None
    return result


def test_failed_text_is_untimed_and_canonical_file_is_untouched(files):
    cache, failure, canonical, evidence = files
    before = canonical.read_bytes()
    raw_failure = failure.read_bytes()
    text = drafts.text_from_evidence(evidence)
    result = drafts.create_initial_draft(cache, SHA, text, failure)
    assert result.text == 'Thank you. ♪\n原始 line\nnext line'
    assert result.path == cache / 'recognition-drafts' / (SHA + '.json')
    assert result.payload['status'] == 'unaligned'
    assert result.payload['needs_review'] is True
    assert not {'segments','human_review','publication','approved'} & result.payload.keys()
    assert result.payload['origin_failure'] == {
        'file_name':failure.name,'sha256':hashlib.sha256(raw_failure).hexdigest()}
    assert canonical.read_bytes() == before
    assert failure.read_bytes() == raw_failure
    assert result.path.stat().st_mode & 0o777 == 0o600
    detached = result.payload
    detached['text'] = 'External mutation'
    assert result.payload['text'] == text


def test_unicode_line_breaks_save_and_reopen_without_approval(files):
    result = initial(files)
    original_origin = result.payload['origin_failure']
    edited = 'Human correction: café — 安全\r\n\nNext line\n'
    saved = drafts.save_user_draft(result.path, edited, expected_revision=result.revision,
                                  expected_source_sha256=SHA)
    reopened = drafts.load_draft(result.path, expected_source_sha256=SHA)
    assert reopened.text == edited
    assert reopened.raw_payload == saved.raw_payload
    assert reopened.revision != result.revision
    assert reopened.payload['edited_by'] == 'human'
    assert reopened.payload['edit_revision'] == 1
    assert reopened.payload['origin_failure'] == original_origin
    assert reopened.payload['created_at'] == result.payload['created_at']
    assert reopened.payload['needs_review'] is True
    assert reopened.payload['status'] == 'unaligned'
    assert files[2].read_bytes() == b'untouched existing human lyric bytes'


def test_automatic_retry_cannot_replace_saved_human_or_machine_draft(files):
    machine = initial(files)
    assert drafts.create_initial_draft(files[0], SHA, 'New automatic words', files[1]) is None
    assert machine.path.read_bytes() == machine.raw_payload
    human = drafts.save_user_draft(machine.path, 'Saved manual correction',
                                  expected_revision=machine.revision, expected_source_sha256=SHA)
    new_failure = files[1].with_name(SHA + '-' + '2' * 32 + '.json')
    new_failure.write_bytes(files[1].read_bytes())
    assert drafts.create_initial_draft(files[0], SHA, 'Another model result', new_failure) is None
    assert machine.path.read_bytes() == human.raw_payload
    assert drafts.draft_path(files[0], SHA) == human.path


def test_initial_creation_preserves_even_an_unreadable_schema(files):
    path = drafts.draft_path(files[0], SHA)
    path.parent.mkdir()
    path.write_bytes(b'Unknown manually preserved file')
    assert drafts.create_initial_draft(files[0], SHA, 'New words', files[1]) is None
    assert path.read_bytes() == b'Unknown manually preserved file'


def test_create_only_commit_does_not_overwrite_a_noncooperating_race(files, monkeypatch):
    path = drafts.draft_path(files[0], SHA)
    real_link = os.link
    def race(*args, **kwargs):
        path.write_bytes(b'New external human file')
        return real_link(*args, **kwargs)
    monkeypatch.setattr(drafts.os, 'link', race)
    assert drafts.create_initial_draft(files[0], SHA, 'Machine text', files[1]) is None
    assert path.read_bytes() == b'New external human file'
    assert not list(path.parent.glob('.draft-*.tmp'))


def test_stale_revision_preserves_current_text_and_the_callers_buffer(files):
    opened = initial(files)
    saved = drafts.save_user_draft(opened.path, 'First correction', expected_revision=opened.revision,
                                  expected_source_sha256=SHA)
    buffer = 'A different unsaved correction\n原始'
    with pytest.raises(drafts.RecognitionDraftError, match='changed since it opened'):
        drafts.save_user_draft(opened.path, buffer, expected_revision=opened.revision,
                               expected_source_sha256=SHA)
    assert opened.path.read_bytes() == saved.raw_payload
    assert buffer == 'A different unsaved correction\n原始'


def test_two_cooperating_writers_can_commit_only_one_old_revision(files):
    opened = initial(files)
    barrier = threading.Barrier(2)
    def save(text):
        barrier.wait()
        try:
            return drafts.save_user_draft(opened.path, text, expected_revision=opened.revision,
                                          expected_source_sha256=SHA)
        except drafts.RecognitionDraftError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ['Writer one', 'Writer two']))
    accepted = [r for r in results if isinstance(r, drafts.RecognitionDraft)]
    refused = [r for r in results if isinstance(r, drafts.RecognitionDraftError)]
    assert len(accepted) == len(refused) == 1
    assert drafts.load_draft(opened.path, expected_source_sha256=SHA).text == accepted[0].text


def test_failed_atomic_replace_keeps_original_bytes_and_buffer(files, monkeypatch):
    opened = initial(files)
    buffer = 'Retain this correction\n✓'
    def unavailable(*args, **kwargs):
        raise OSError('Disposable simulated full disk')
    monkeypatch.setattr(drafts.os, 'replace', unavailable)
    with pytest.raises(drafts.RecognitionDraftError, match='could not be saved'):
        drafts.save_user_draft(opened.path, buffer, expected_revision=opened.revision,
                               expected_source_sha256=SHA)
    assert opened.path.read_bytes() == opened.raw_payload
    assert buffer == 'Retain this correction\n✓'
    assert not list(opened.path.parent.glob('.draft-*.tmp'))


def test_human_may_clear_text_but_empty_extraction_does_not_make_saved_draft(files):
    with pytest.raises(drafts.RecognitionDraftError, match='No recognition text'):
        drafts.create_initial_draft(files[0], SHA, ' \n', files[1])
    assert not drafts.draft_path(files[0], SHA).exists()
    opened = initial(files)
    saved = drafts.save_user_draft(opened.path, '', expected_revision=opened.revision,
                                  expected_source_sha256=SHA)
    assert drafts.load_draft(saved.path, expected_source_sha256=SHA).text == ''
    assert saved.payload['edited_by'] == 'human'


@pytest.mark.parametrize('field,value',[
    ('segments', []), ('human_review', {'approved':True}), ('publication', {'approved':True}),
    ('approved', True), ('quality', {'status':'pass'}), ('version', 'broadcast-player-lyrics-1'),
    ('source_sha256', OTHER), ('status', 'aligned'), ('needs_review', False),
    ('edit_revision', True), ('edited_by', 'approved'),
])
def test_timed_approved_or_unbound_payloads_are_rejected(files, field, value):
    opened = initial(files)
    payload = opened.payload
    payload[field] = value
    opened.path.write_text(json.dumps(payload))
    original = opened.path.read_bytes()
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.load_draft(opened.path, expected_source_sha256=SHA)
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.save_user_draft(opened.path, 'Must not replace invalid data',
                               expected_revision=hashlib.sha256(original).hexdigest(), expected_source_sha256=SHA)
    assert opened.path.read_bytes() == original


def test_selected_recording_and_namespace_are_required(files):
    opened = initial(files)
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.load_draft(opened.path, expected_source_sha256=OTHER)
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.load_draft(files[2], expected_source_sha256=SHA)
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.load_draft(opened.path.parent / '..' / 'recognition-drafts' / opened.path.name,
                          expected_source_sha256=SHA)
    assert files[2].read_bytes() == b'untouched existing human lyric bytes'


def test_symbolic_link_target_and_ancestor_are_not_followed(files, tmp_path):
    path = drafts.draft_path(files[0], SHA)
    path.parent.mkdir()
    victim = tmp_path / 'victim.txt'; victim.write_text('Never touch')
    path.symlink_to(victim)
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.create_initial_draft(files[0], SHA, 'New words', files[1])
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.load_draft(path, expected_source_sha256=SHA)
    path.unlink()
    path.with_name('.' + path.name + '.lock').unlink()
    path.parent.rmdir()
    outside = tmp_path / 'outside';outside.mkdir()
    path.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.create_initial_draft(files[0], SHA, 'New words', files[1])
    assert victim.read_text() == 'Never touch'
    assert not list(outside.iterdir())


def test_symbolic_link_lock_and_failure_are_not_followed(files, tmp_path):
    opened = initial(files)
    lock = opened.path.with_name('.' + opened.path.name + '.lock')
    lock.unlink()
    victim = tmp_path / 'victim';victim.write_text('Unchanged')
    lock.symlink_to(victim)
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.save_user_draft(opened.path, 'New words', expected_revision=opened.revision,
                               expected_source_sha256=SHA)
    assert victim.read_text() == 'Unchanged'
    opened.path.unlink();lock.unlink()
    source = files[1];raw = source.read_bytes();source.unlink();source.symlink_to(victim)
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.create_initial_draft(files[0], SHA, 'New words', source)
    assert victim.read_text() == 'Unchanged'


@pytest.mark.parametrize('corrupt',[b'{bad json', b'\xef\xbb\xbf{}', b'{}', b'\x00'])
def test_malformed_drafts_are_preserved(files, corrupt):
    opened = initial(files);opened.path.write_bytes(corrupt)
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.load_draft(opened.path, expected_source_sha256=SHA)
    assert opened.path.read_bytes() == corrupt


def test_duplicate_fields_and_large_documents_are_refused(files):
    opened = initial(files)
    raw = opened.raw_payload.decode().replace('"text":', '"text":"duplicate", "text":')
    opened.path.write_text(raw)
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.load_draft(opened.path, expected_source_sha256=SHA)
    opened.path.write_bytes(b' ' * (drafts.MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.load_draft(opened.path, expected_source_sha256=SHA)


def test_no_partial_truncation_or_revision_guessing(files):
    opened = initial(files)
    for text, revision in [('x' * (drafts.MAX_TEXT_CHARACTERS + 1), opened.revision),
                           ('\ud800', opened.revision), ('new', None)]:
        with pytest.raises(drafts.RecognitionDraftError):
            drafts.save_user_draft(opened.path, text, expected_revision=revision,
                                   expected_source_sha256=SHA)
        assert opened.path.read_bytes() == opened.raw_payload


def test_origin_failure_must_match_recording_and_private_cache(files, tmp_path):
    other = tmp_path / files[1].name;other.write_bytes(files[1].read_bytes())
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.create_initial_draft(files[0], SHA, 'New words', other)
    payload = json.loads(files[1].read_text());payload['source_sha256'] = OTHER
    files[1].write_text(json.dumps(payload))
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.create_initial_draft(files[0], SHA, 'New words', files[1])
    assert not drafts.draft_path(files[0], SHA).exists()


def test_raw_cpu_and_vulkan_rows_keep_whitespace_and_signoff_text():
    cpu = {'primary':{'windows':[{'recognition':{'segments':[{'text':' Thank you.\n'}, {'text':'歌詞'}]}}]}}
    vulkan = {'primary':{'windows':[{'raw':{'transcription':[{'text':" We'll be right back. ♪"}]}}]}}
    assert drafts.text_from_evidence(cpu) == ' Thank you.\n\n歌詞'
    assert drafts.text_from_evidence(vulkan) == " We'll be right back. ♪"
    assert drafts.text_from_evidence({'primary':{'windows':[]}}) == ''


def test_invalid_text_rows_are_not_silently_skipped():
    for evidence in [{'pre_alignment':[{'text':'Good'}, {'text':None}]},
                     {'primary':{'windows':['invalid']}},
                     {'pre_alignment':[{'text':'x' * (drafts.MAX_TEXT_CHARACTERS + 1)}]}]:
        with pytest.raises(drafts.RecognitionDraftError):
            drafts.text_from_evidence(evidence)


@pytest.mark.parametrize('fingerprint',['bad', '../escape', 'a' * 63, None])
def test_invalid_recording_identifiers_cannot_create_files(files, fingerprint):
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.create_initial_draft(files[0], fingerprint, 'New words', files[1])
    assert not (files[0] / 'recognition-drafts').exists()


def test_nonregular_fifo_target_is_rejected_without_reading(files):
    path = drafts.draft_path(files[0], SHA);path.parent.mkdir()
    os.mkfifo(path)
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.load_draft(path, expected_source_sha256=SHA)
    with pytest.raises(drafts.RecognitionDraftError):
        drafts.create_initial_draft(files[0], SHA, 'New words', files[1])


def test_platform_without_secure_directory_capabilities_fails_closed(files, monkeypatch):
    monkeypatch.setattr(drafts.os, 'supports_dir_fd', set())
    with pytest.raises(drafts.RecognitionDraftError, match='unavailable on this platform'):
        drafts.create_initial_draft(files[0], SHA, 'New words', files[1])
    assert not drafts.draft_path(files[0], SHA).exists()


def test_late_external_edit_is_detected_before_atomic_replace(files, monkeypatch):
    opened = initial(files)
    original_read = drafts._read
    count = 0
    external = opened.payload
    external['text'] = 'External newer saved text'
    raw_external = json.dumps(external).encode()
    def changed_before_commit(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            opened.path.write_bytes(raw_external)
        return original_read(*args, **kwargs)
    monkeypatch.setattr(drafts, '_read', changed_before_commit)
    with pytest.raises(drafts.RecognitionDraftError, match='changed while saving'):
        drafts.save_user_draft(opened.path, 'Stale editor text', expected_revision=opened.revision,
                               expected_source_sha256=SHA)
    assert opened.path.read_bytes() == raw_external


def test_revision_overflow_does_not_write_an_unloadable_document(files):
    opened = initial(files);data = opened.payload;data['edit_revision'] = 2**63 - 1
    raw = json.dumps(data).encode();opened.path.write_bytes(raw)
    with pytest.raises(drafts.RecognitionDraftError, match='revision limit'):
        drafts.save_user_draft(opened.path, 'New text', expected_revision=hashlib.sha256(raw).hexdigest(),
                               expected_source_sha256=SHA)
    assert opened.path.read_bytes() == raw
