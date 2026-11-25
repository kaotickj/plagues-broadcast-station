"""Conservative playlist preparation. Previous vocal approvals are not accuracy evidence."""
from .review_sheet import embedded_sources, embedded_instrumental_candidate, bracket_preview, make_sheet

def batch_plan(document, embedded, *, existing=None, missing_only=False, allow_long_ctc=False, backend='qwen'):
    candidates=[c for c in (document,existing) if c is not None]
    instrumentals=[c for c in candidates if c.source_sha256==document.source_sha256
                   and c.metadata.get('instrumental') is True and not c.segments and not c.untimed_text]
    if instrumentals:
        if any(c.segments or c.untimed_text for c in candidates if c not in instrumentals):
            return 'review', 'Conflicting vocal and instrumental records; resolve by listening.', None
        return 'instrumental', 'Explicit recording-bound instrumental designation retained; no model.', None
    if missing_only and any(c.segments or c.untimed_text or c.metadata.get('recognition_regions') for c in candidates):
        return 'existing', 'Existing candidate retained; use Audit playlist to reprocess.', None
    if embedded.get('kind') == 'probe_failed':
        return 'review', 'Metadata probe failed; inspect source before transcription.', None
    if embedded_instrumental_candidate(embedded):
        return 'review', 'Possible instrumental; listen and confirm or explicitly override.', None
    texts=[row.get('text','') for row in embedded_sources(embedded)]
    texts.extend(embedded.get('tags',{}).values())
    unique=list(dict.fromkeys(t.strip() for t in texts if isinstance(t,str) and t.strip()))
    if not unique and embedded.get('text','').strip():
        unique=[embedded['text'].strip()]
    if not unique:
        return 'extract', 'No embedded words; audio-only recognition and alignment.', None
    if len(unique)!=1:
        return 'review', 'Conflicting embedded lyric sources; choose the correct sheet manually.', None
    original=unique[0]
    if len(original)>100000:
        return 'review', 'Embedded sheet exceeds review limit.', None
    sung,rows=bracket_preview(original)
    if any(r['action']=='keep' for r in rows) or '[' in sung or ']' in sung or not sung.strip():
        return 'review', 'Ambiguous bracketed words or directions; inspect lyrics sheet.', None
    if not 1<=document.duration<=900:
        return 'review', 'Duration unavailable or outside supplied alignment limits.', None
    if document.duration>180 and backend=='qwen' and not allow_long_ctc:
        return 'review', 'Long embedded sheet: enable CPU CTC alignment or review manually.', None
    sheet=make_sheet(original,sung,rows,source_sha256=document.source_sha256,
                     duration=document.duration,title=document.title,
                     import_source={'kind':'embedded_batch_unverified','trusted':False})
    sheet['origin']='embedded_batch_unverified'
    return 'align', 'Align embedded words without recognition; fresh listening required.', sheet
