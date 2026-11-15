"""Review transitions for sectioned Qwen text. No models, guessed timing or approval."""
from copy import deepcopy
from .review_core import mark_unreviewed, now, sung_text, alignment_input
from .lyrics import Word
from .lyric_review import document_revision
from .alignment_contract import alignment_regions


def text_sections(document):
    return deepcopy(document.metadata.get('recognition_regions', []))


def edit_section(document, index, text):
    result=deepcopy(document);sections=result.metadata.get('recognition_regions',[])
    if type(index) is not int or not 0<=index<len(sections):raise ValueError('Select an extracted text section.')
    if len(text)>100000:raise ValueError('Section text exceeds the review limit.')
    if sections[index]['text']==text:return result
    before=sections[index]['text'];sections[index]['text']=text
    sections[index]['text_review_status']='operator_edited_unaligned'
    for hold in result.metadata.get('held_passages',[]):
        if hold.get('region')==index and hold.get('status')=='held':
            hold.update(status='corrected_by_operator_needs_alignment',corrected_text=text,
                decision='Operator edited this section. New alignment must recheck it; no approval granted.')
    result.metadata.setdefault('text_edits',[]).append({'at':now(),'section':index,'before':before,'after':text})
    # Section editor is only offered before alignment. Never erase existing anchors.
    if result.segments:raise ValueError('Edit timed phrases in the phrase table; their timing is preserved.')
    return mark_unreviewed(result,'Extracted text corrected; align and listen before approving.')


def qwen_alignment_input(document, supplied_text, *, source='draft', replace_timing=False, use_sections=False):
    if document.segments:
        request=alignment_input(document,supplied_text,source=source,replace_timing=replace_timing)
        if source=='draft' and document.metadata.get('alignment_windows'):
            regions=deepcopy(document.metadata['alignment_windows'])
            for i,region in enumerate(regions):
                cues=[c for c in document.segments if c.metadata.get('region')==i]
                if not cues:raise ValueError('A section has no timed text. Use the preserved text sections or explicit supplied text.')
                region['text']=' '.join(c.text for c in cues)
            request['alignment_windows']=regions
        if document.duration>180 and 'alignment_windows' not in request:
            raise ValueError('Qwen supplied-text alignment needs explicit sections of at most 180 seconds. Extract sectioned text first, or choose the Heart / CTC fallback for a full unsectioned sheet.')
        return request
    sections=text_sections(document) if use_sections or source=='draft' else []
    if sections:
        regions=[];mapping={}
        for old_index,section in enumerate(sections):
            text=sung_text(section.get('text',''))
            if not text:continue # operator removed this section's proposed text; retain raw evidence
            mapping[old_index]=len(regions)
            regions.append({'start':section['start'],'end':section['end'],'text':text,
                'evidence':{'kind':'operator_selected','source':'Saved extraction section; boundary is not a verified vocal onset.'}})
        text='\n'.join(r['text'] for r in regions)
        request={'reference_text':text,'reference_original':'\n'.join(s.get('text','') for s in sections),
            'alignment_windows':regions,'alignment_source':'saved_extraction_sections',
            'recognition_regions':sections,
            'recognition_model_evidence':document.metadata.get('provenance',{}).get('recognition_model_evidence',document.metadata.get('provenance',{}).get('model_evidence')),
            'recognition_evidence_directory':document.metadata.get('provenance',{}).get('recognition_evidence_directory',document.metadata.get('provenance',{}).get('raw_evidence')),
            'held_passages':[{**h,'region':mapping[h.get('recognition_region',h['region'])]} for h in document.metadata.get('held_passages',[]) if h.get('status')=='held' and h.get('recognition_region',h.get('region')) in mapping],
            'replacement_scope':'whole_draft','replaces_revision':document_revision(document)}
        alignment_regions(request,document.duration)
        return request
    if document.duration>180:
        raise ValueError('Qwen supplied-text alignment needs explicit sections of at most 180 seconds. Extract sectioned text first, or select the Heart / CTC fallback for a full unsectioned sheet.')
    return alignment_input(document,supplied_text,source=source,replace_timing=replace_timing)


def use_supported_phrase_timing(document, index):
    """Explicit operator grouping uses existing intervals, not estimated word durations."""
    result=deepcopy(document)
    if not 0<=index<len(result.segments):raise ValueError('Select a phrase first.')
    cue=result.segments[index];issues=cue.metadata.get('native_timing_issues')
    if not issues:raise ValueError('Selected phrase has no unresolved native word timing.')
    intervals=[list(pair) for w in cue.words if w.highlight_allowed for pair in
               (w.highlight_intervals if w.highlight_intervals is not None else ((w.start,w.end),))]
    if not intervals:raise ValueError('This phrase has no supported positive interval. Realign corrected text; no duration was invented.')
    constituents=[w.to_dict() for w in cue.words]
    cue.words=[Word(cue.text,cue.start,cue.end,{'grouped_text':True,'individual_word_boundaries':None,
        'highlight_intervals':intervals,'highlight_allowed':True,'native_constituents':constituents,
        'native_timing_issues_before_operator_grouping':deepcopy(issues),'flags':['operator_grouped_timing_review']})]
    cue.metadata['resolved_native_timing_issues']=cue.metadata.pop('native_timing_issues')
    cue.metadata.pop('highlight_allowed',None);cue.metadata.pop('highlight_intervals',None)
    cue.metadata['timing_kind']='operator_grouped_existing_support'
    result.metadata.setdefault('timing_edits',[]).append({'at':now(),'action':'operator_use_supported_group',
        'phrase':index,'rule':'Original positive intervals only; no new word times. Owner must listen before approval.'})
    return mark_unreviewed(result,'Phrase grouped over existing supported intervals. Individual word timing remains unknown; listen again.')
