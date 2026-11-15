"""Explicit operator timing edits: no inference, scaling, or invented word division."""
from copy import deepcopy
from dataclasses import replace
import math
from .lyrics import validate_highlight_intervals
from .lyric_review import _validate
from .review_core import mark_unreviewed, now


def number(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError('Timing must be a finite number of seconds.')
    return float(value)


def exclusions(document):
    """Only explicit owner-confirmed exclusions, never heuristically detected gaps."""
    original=document.metadata.get('reference_provenance',{}).get('original_review_data',{})
    values=document.metadata.get('confirmed_instrumental_gaps',original.get('confirmed_instrumental_gaps',[]))
    result=[]
    for row in values:
        a,b=(row['start'],row['end']) if isinstance(row,dict) else row
        a,b=number(a),number(b)
        if not 0<=a<b<=document.duration:raise ValueError('Stored instrumental exclusion is invalid.')
        result.append((a,b))
    return sorted(result)


def subtract_gaps(pairs,gaps):
    for left,right in gaps:
        remaining=[]
        for a,b in pairs:
            if b<=left or a>=right:remaining.append((a,b))
            else:
                if a<left:remaining.append((a,left))
                if b>right:remaining.append((right,b))
        pairs=remaining
    return pairs


def _word(word,start,end,gaps,*,delta=None,restore=None):
    start,end=number(start),number(end)
    if delta is None and restore is None and (start,end)==(word.start,word.end):return deepcopy(word)
    if not 0<=start<end:raise ValueError('Start must be nonnegative and earlier than end.')
    meta=deepcopy(word.metadata)
    if restore is not None:
        pairs=restore.highlight_intervals
        if pairs is None:pairs=((restore.start,restore.end),)
        allowed=restore.highlight_allowed
    else:
        allowed=word.highlight_allowed
        pairs=word.highlight_intervals
        if pairs is None:pairs=((word.start,word.end),)
        if delta is not None:
            pairs=[(a+delta,b+delta) for a,b in pairs]
        elif pairs:
            # Bounds edit outer supported edges only. Internal holes remain holes.
            pairs=list(pairs)
            if abs(pairs[0][0]-word.start)<1e-6:pairs[0]=(start,pairs[0][1])
            if abs(pairs[-1][1]-word.end)<1e-6:pairs[-1]=(pairs[-1][0],end)
            pairs=[(max(start,a),min(end,b)) for a,b in pairs if min(end,b)>max(start,a)]
    pairs=subtract_gaps(list(pairs),gaps) if allowed else []
    historical={key:deepcopy(meta[key]) for key in ['timing_status','phrase_timing_status','owner_review_status','operator_bounds'] if key in meta}
    if historical:meta.setdefault('timing_status_history',[]).append(historical)
    for key in ['phrase_timing_status','owner_review_status']:meta.pop(key,None)
    meta.update(highlight_allowed=allowed,highlight_intervals=[list(pair) for pair in pairs],
                timing_status='operator_edited_needs_review',operator_bounds=[start,end])
    meta['flags']=list(dict.fromkeys(meta.get('flags',[])+['manual_timing_review']))
    updated=replace(word,start=start,end=end,metadata=meta)
    validate_highlight_intervals(updated.to_dict())
    return updated


def _finish(original,result,action,details):
    # Structural validation rejects crossing neighbours and bounds beyond duration.
    _validate(result,allow_empty=True)
    result.metadata.setdefault('timing_edits',[]).append({'at':now(),'action':action,**details})
    return mark_unreviewed(result,'Timing changed manually. Listen to this revision before approving.')


def _selection(document,index,word_index):
    if type(index) is not int or not 0<=index<len(document.segments):raise ValueError('Select a phrase first.')
    cue=document.segments[index]
    if word_index is not None and (type(word_index) is not int or not 0<=word_index<len(cue.words)):
        raise ValueError('Select an existing word or grouped phrase.')
    return cue


def edit_bounds(document,index,word_index,start,end):
    """Edit word/group bounds, or phrase outer word edges; interior anchors stay fixed."""
    cue=_selection(document,index,word_index);start,end=number(start),number(end)
    if not 0<=start<end<=document.duration:raise ValueError('Choose ordered bounds inside this recording.')
    result=deepcopy(document);changed=result.segments[index];gaps=exclusions(document)
    if word_index is None:
        if (start,end)==(cue.start,cue.end):return result
        changed.start,changed.end=start,end
        if len(cue.words)==1:changed.words[0]=_word(cue.words[0],start,end,gaps)
        elif cue.words:
            changed.words[0]=_word(cue.words[0],start,cue.words[0].end,gaps)
            changed.words[-1]=_word(cue.words[-1],cue.words[-1].start,end,gaps)
        prior=[cue.start,cue.end]
    else:
        word=cue.words[word_index]
        if (start,end)==(word.start,word.end):return result
        changed.words[word_index]=_word(word,start,end,gaps)
        if word_index==0:changed.start=start if abs(cue.start-word.start)<1e-6 else min(cue.start,start)
        if word_index==len(cue.words)-1:changed.end=end if abs(cue.end-word.end)<1e-6 else max(cue.end,end)
        prior=[word.start,word.end]
    changed.metadata['timing_status']='operator_edited_needs_review'
    return _finish(document,result,'bounds',{'phrase':index,'word':word_index,'before':prior,'after':[start,end],
        'rule':'Outer edges only; use an offset to move all internal anchors together.'})


def offset(document,delta,*,indices=None,word_index=None):
    """Explicit translation of a word/group, selected phrases or whole draft."""
    delta=number(delta)
    if not document.segments:raise ValueError('There are no lyric spans to move.')
    selected=list(range(len(document.segments))) if indices is None else sorted(set(indices))
    if not selected:raise ValueError('Select at least one phrase to move.')
    for i in selected:_selection(document,i,None)
    if word_index is not None and len(selected)!=1:raise ValueError('Select one word/group to move.')
    if delta==0:return deepcopy(document)
    result=deepcopy(document);gaps=exclusions(document)
    for i in selected:
        old=document.segments[i];cue=result.segments[i]
        if word_index is not None:
            _selection(document,i,word_index);word=old.words[word_index]
            cue.words[word_index]=_word(word,word.start+delta,word.end+delta,gaps,delta=delta)
            if word_index==0:cue.start=word.start+delta if abs(cue.start-word.start)<1e-6 else min(cue.start,word.start+delta)
            if word_index==len(old.words)-1:cue.end=word.end+delta if abs(cue.end-word.end)<1e-6 else max(cue.end,word.end+delta)
        else:
            cue.start+=delta;cue.end+=delta
            cue.words=[_word(w,w.start+delta,w.end+delta,gaps,delta=delta) for w in old.words]
        cue.metadata['timing_status']='operator_edited_needs_review'
    return _finish(document,result,'offset',{'seconds':delta,'phrases':selected,'word':word_index,
        'scope':'whole_draft' if indices is None else 'word_or_group' if word_index is not None else 'selected_phrases',
        'rule':'All selected support translated together; confirmed audio exclusions remain fixed.'})


def reset_timing(document,baseline):
    """Restore opened timing while retaining edited text, only with matching structure."""
    if len(document.segments)!=len(baseline.segments) or any(len(a.words)!=len(b.words) for a,b in zip(document.segments,baseline.segments)):
        raise ValueError('Phrase/word grouping changed. Use Undo or Restore saved draft instead of guessing a timing mapping.')
    result=deepcopy(document);gaps=exclusions(document)
    for cue,old in zip(result.segments,baseline.segments):
        cue.start,cue.end=old.start,old.end
        cue.words=[_word(w,b.start,b.end,gaps,restore=b) for w,b in zip(cue.words,old.words)]
        cue.metadata['timing_status']='operator_reset_needs_review'
    return _finish(document,result,'reset_timing',{'reference':'candidate opened in this review session; current text retained'})
