"""Local human review bound to exact recording bytes and lyric revision."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from .lyrics import Cue, LyricsDocument, MAX_LYRIC_BYTES, fingerprint, lyric_write_lock, _review_state_digest


def document_revision(document: LyricsDocument) -> str:
    data = document.to_dict()
    data.pop("human_review", None)
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rows(rows, duration, *, allow_empty=False):
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= 86400:
        raise ValueError("The recording needs a valid duration before review.")
    result, previous = [], 0.0
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise ValueError("Each lyric row needs start, end and text.")
        start, end, text = row
        if isinstance(start, bool) or isinstance(end, bool):
            raise ValueError("Lyric timestamps must be finite numbers.")
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError) as error:
            raise ValueError("Lyric timestamps must be finite numbers.") from error
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or
                end > duration or start < previous):
            raise ValueError("Lyric lines must be ordered, nonoverlapping intervals within the recording.")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise ValueError("Every lyric row needs nonempty text of at most 20,000 characters. Remove unwanted rows explicitly.")
        result.append((start, end, text))
        previous = end
    if (not result and not allow_empty) or len(result) > 10000:
        raise ValueError("Review requires between 1 and 10,000 lyric rows.")
    return result


def _validate(document, *, allow_empty=False, validate_words=True):
    _rows([(cue.start, cue.end, cue.text) for cue in document.segments], document.duration, allow_empty=allow_empty)
    if not validate_words:
        return
    for cue in document.segments:
        previous = cue.start
        for word in cue.words:
            if (isinstance(word.start, bool) or isinstance(word.end, bool) or
                    not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (word.start, word.end)) or
                    word.start < previous or word.end <= word.start or word.end > cue.end):
                raise ValueError("Word timings must be ordered and stay inside their lyric line. Edit that line to retain line timing without stale word timing.")
            if not isinstance(word.text, str) or not word.text.strip():
                raise ValueError("Timed words need text. Use Line timing to remove invalid word timing from this line.")
            previous = word.end
        normalize = lambda text: re.sub(r"\W+", "", text.casefold())
        if cue.words and normalize(" ".join(word.text for word in cue.words)) != normalize(cue.text):
            raise ValueError("Timed word labels do not match the lyric line. Use Line timing to retain its text and line times without incorrect word timing.")


def is_approved(document: LyricsDocument, source_sha256: str | None = None) -> bool:
    try:
        review = document.human_review
        digest = document.source_sha256
        when = review.get("reviewed_at")
        if (not re.fullmatch(r"[a-f0-9]{64}", digest) or review.get("status") != "approved" or
                review.get("method") != "human" or review.get("source_sha256") != digest or
                (source_sha256 is not None and source_sha256 != digest) or
                isinstance(when, bool) or not isinstance(when, (int, float)) or not math.isfinite(when) or when <= 0):
            return False
        _validate(document)
        return review.get("document_revision") == document_revision(document)
    except (TypeError, ValueError, AttributeError):
        return False


def _hold_quality(document):
    if "review_original_quality" not in document.metadata:
        document.metadata["review_original_quality"] = deepcopy(document.quality)
    quality = deepcopy(document.quality)
    reasons = deepcopy(quality.get("reasons")) if isinstance(quality.get("reasons"), list) else []
    if not any(isinstance(reason, dict) and reason.get("code") == "human_revision" for reason in reasons):
        reasons.append({"code": "human_revision", "message": "This human-reviewed revision requires its recording- and revision-bound approval before airing."})
    quality.update(status="review", reasons=reasons)
    document.quality = quality


def _alignment(document):
    value = document.human_review.get("alignment_needed", [])
    return sorted({index for index in value if type(index) is int and 0 <= index < len(document.segments)}) if isinstance(value, list) else []


def invalidate_review(document: LyricsDocument, reason: str = "Lyrics changed; review this revision before airing.") -> LyricsDocument:
    result = deepcopy(document)
    result._review_base_state = getattr(document, "_review_base_state", _review_state_digest(document))
    alignment = _alignment(result)
    result.human_review = {"status": "draft", "reason": str(reason)}
    if isinstance(alignment, list) and alignment:
        result.human_review["alignment_needed"] = deepcopy(alignment)
    _hold_quality(result)
    return result


def retarget_review(document: LyricsDocument, current_document: LyricsDocument | None = None) -> LyricsDocument:
    """Prepare an explicitly imported replacement against its displayed target.

    The caller still supplies that target's document_revision to the save API.
    An imported file's approval never transfers to a replacement automatically.
    """
    result = invalidate_review(document, "Imported draft; approve this recording and revision before airing.")
    result._review_base_state = _review_state_digest(current_document) if current_document is not None else None
    return result


def edit_document(document: LyricsDocument, rows) -> LyricsDocument:
    rows = _rows(rows, document.duration, allow_empty=True)
    result = invalidate_review(document)
    # Reserve every exact match before assigning changed rows, so deleting a
    # line never transfers its word timing/metadata to the following line.
    assigned, used = {}, set()
    for index, row in enumerate(rows):
        for old_index, cue in enumerate(document.segments):
            if old_index not in used and row == (cue.start, cue.end, cue.text):
                assigned[index] = old_index
                used.add(old_index)
                break
    original_alignment = _alignment(document)
    alignment, cues = [], []
    for index, (start, end, text) in enumerate(rows):
        if index in assigned:
            old_index = assigned[index]
            cues.append(deepcopy(document.segments[old_index]))
            if isinstance(original_alignment, list) and old_index in original_alignment:
                alignment.append(index)
            continue
        candidates = [i for i, cue in enumerate(document.segments) if i not in used and (cue.start, cue.end) == (start, end)]
        candidates += [i for i, cue in enumerate(document.segments) if i not in used and cue.text == text and i not in candidates]
        if not candidates and index < len(document.segments) and index not in used:
            candidates = [index]
        metadata = deepcopy(document.segments[candidates[0]].metadata) if candidates else {}
        if candidates:
            used.add(candidates[0])
        cues.append(Cue(text, start, end, [], metadata))
        alignment.append(index)
    result.segments = cues
    if alignment:
        result.human_review["alignment_needed"] = alignment
    else:
        result.human_review.pop("alignment_needed", None)
    _validate(result, allow_empty=True, validate_words=False)
    return result


def approve_document(document: LyricsDocument, source_sha256: str) -> LyricsDocument:
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", source_sha256) or document.source_sha256 != source_sha256:
        raise ValueError("Approval requires the exact fingerprint of this lyric document's recording.")
    result = deepcopy(document)
    result._review_base_state = getattr(document, "_review_base_state", _review_state_digest(document))
    _validate(result)
    _hold_quality(result)
    alignment = _alignment(result)
    result.human_review = {"status": "approved", "source_sha256": source_sha256,
                           "document_revision": document_revision(result), "reviewed_at": int(time.time()), "method": "human"}
    if isinstance(alignment, list) and alignment:
        result.human_review["alignment_needed"] = deepcopy(alignment)
    return result


def _target_bytes(target):
    if target.is_symlink():
        raise ValueError("Save review to a regular JSON file, not a symbolic link.")
    if not target.exists():
        return None
    if not target.is_file() or target.stat().st_size > MAX_LYRIC_BYTES:
        raise ValueError("The existing lyric target is not a supported JSON file.")
    return target.read_bytes()


def _history(target, original):
    folder = target.parent / "history"
    if folder.is_symlink():
        raise ValueError("Refusing a symbolic-link lyric history directory.")
    folder.mkdir(exist_ok=True, mode=0o700)
    backup = folder / (target.stem + "-review-" + str(time.time_ns()) + "-" + uuid.uuid4().hex[:8] + ".json")
    with backup.open("xb") as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    backup.chmod(0o600)


def save_review_document(document: LyricsDocument, target, source, expected_revision, approve: bool = False) -> LyricsDocument:
    """Save one displayed base revision; only explicit approve=True signs it.

    Cooperating new app/import/extraction writers use the same lock. Legacy or
    unrelated writers are additionally checked immediately before replacement;
    their later changes cannot inherit a valid revision-bound approval.
    """
    target, source = Path(target), Path(source)
    if target.suffix.lower() != ".json" or target.resolve() == source.resolve():
        raise ValueError("Save review to a separate .json lyric file.")
    with lyric_write_lock(target):
        original = _target_bytes(target)
        if original is None:
            if expected_revision is not None:
                raise ValueError("The lyric file was removed or moved. Reopen it before saving.")
        else:
            current = LyricsDocument.from_dict(json.loads(original.decode("utf-8-sig")))
            if expected_revision is None or document_revision(current) != expected_revision:
                raise ValueError("The lyric file changed since review opened. Reopen the latest revision before saving.")
            base_state = getattr(document, "_review_base_state", _review_state_digest(document))
            if base_state != _review_state_digest(current):
                raise ValueError("The lyric review/approval state changed since review opened. Reopen it before saving.")
        source_stat = source.stat()
        digest = fingerprint(source)
        if document.source_sha256 and document.source_sha256 != digest:
            raise ValueError("The recording changed or these lyrics belong to another recording. Nothing was saved.")
        if original is not None and current.source_sha256 and current.source_sha256 != digest:
            raise ValueError("The existing lyric target belongs to another recording. Choose a separate lyric file.")
        result = deepcopy(document)
        result.source_sha256 = digest
        _validate(result, allow_empty=not approve, validate_words=approve)
        result = approve_document(result, digest) if approve else invalidate_review(result, "Saved draft; approve this exact revision before airing.")
        payload = json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
        if len(payload) > MAX_LYRIC_BYTES:
            raise ValueError("The reviewed lyric file exceeds the supported size.")
        fd, temporary = tempfile.mkstemp(prefix=".lyric-review-", suffix=".json", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if original is not None:
                _history(target, original)
            current_stat = source.stat()
            if (current_stat.st_size, current_stat.st_mtime_ns) != (source_stat.st_size, source_stat.st_mtime_ns) or fingerprint(source) != digest:
                raise ValueError("The recording changed while saving. Existing lyrics were kept.")
            if _target_bytes(target) != original:
                raise ValueError("The lyric file changed while saving. Reopen the latest revision.")
            os.replace(temporary, target)
            result._review_base_state = _review_state_digest(result)
            return result
        finally:
            Path(temporary).unlink(missing_ok=True)
