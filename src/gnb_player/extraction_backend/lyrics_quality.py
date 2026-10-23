"""Conservative publication gate; machine checks do not certify lyric accuracy."""
import math,re,time
VERSION='lyrics-qc-3'

# Hold likely credits/sign-offs for review; never remove their text. A bare
# "thank you", or listening embedded in a lyric, is not enough evidence.
_TRANSCRIPTION_ARTIFACT=re.compile(
    r'\b(?:thanks? (?:you )?for watching|subscribe to (?:my|the) channel|subtitles by|amara\.org)\b'
    r'|^\s*(?:thank\s+you|thanks)\s+for\s+listening[.!…\s]*$'
    r'|(?:©|\(c\)|\bcopyright\b)\s*(?:\d{4}\s*)?transcript(?:ion)?\b'
    r'|^\s*(?:transcript(?:ion)?|transcribed)\s+by\b',re.I)
_OPENING_CREDIT=re.compile(
    r'^\s*(?i:transcript(?:ion)?|transcribed)(?:\s+(?i:by))?\s+'
    r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b')

def artifact_suspected(data,index):
    segments=data.get('segments') or []
    if not 0<=index<len(segments):return False
    cue=segments[index]
    if not isinstance(cue,dict):return False
    if _TRANSCRIPTION_ARTIFACT.search(str(cue.get('text',''))):return True
    opening=[c for c in segments[:2] if isinstance(c,dict)]
    return bool(index<2 and _OPENING_CREDIT.search(' '.join(str(c.get('text','')) for c in opening)))

def assess(data):
    reasons=[]
    def flag(code,message,at=None):
        if any(r['code']==code for r in reasons):return
        row={'code':code,'message':message}
        if at is not None and isinstance(at,(int,float)) and math.isfinite(at):row['at']=round(max(0,at),3)
        reasons.append(row)
    duration=data.get('duration',0)
    if not isinstance(duration,(int,float)) or not math.isfinite(duration) or not 1<=duration<=900:
        flag('duration','Recording duration is outside the automatic song-publication range.');duration=0
    segments=data.get('segments',[]);words=[];last=-1;last_end=0;previous='';repeats=0
    if not isinstance(segments,list) or not segments:flag('empty','No complete lyric lines were produced.');segments=[]
    for index,cue in enumerate(segments):
        if not isinstance(cue,dict):flag('timing','Invalid lyric line.');continue
        start,end=cue.get('start'),cue.get('end')
        if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (start,end)):
            flag('timing','A line has invalid timing.');continue
        if start<last_end-.1 or end<=start or start<0 or end>duration+.05:flag('timing','Line timings overlap or fall outside the recording.',start)
        if last_end and start-last_end>=20 and not any(g.get('verified') is True and g.get('start',duration)<=last_end+.1 and g.get('end',0)>=start-.1 for g in (data.get('machine_review') or {}).get('confirmed_gaps',[])):flag('gap','Check this long gap for an instrumental break or missing vocals.',last_end)
        last_end=end
        text=str(cue.get('text','')).strip();row_words=cue.get('words',[])
        normal=lambda value:re.sub(r'[^\w]+','',value).casefold()
        if not row_words or normal(' '.join(str(w.get('text','')) for w in row_words))!=normal(text):flag('word_coverage','Some lyric text lacks matching word timestamps.',start)
        if cue.get('needs_review'):flag('uncertain','The recognizer marked a passage as uncertain.',start)
        for word in row_words:
            a,b=word.get('start'),word.get('end')
            if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (a,b)):
                flag('word_timing','A word has invalid timing.',start);continue
            if a<last-.1 or b<=a or a<start-.1 or b>end+.1 or b-a>8:
                flag('word_timing','Check overlapping, zero-length, or unusually stretched word timing.',a)
            last=b;words.append(word)
        phrase=normal(text)
        repeats=repeats+1 if phrase and phrase==previous else 1
        if repeats>=4:flag('repetition','Check this repeated passage for recognition looping.',start)
        previous=phrase
        if artifact_suspected(data,index):flag('hallucination','A passage resembles a common transcription artifact.',start)
    # Word-sized decoder output can split a credit across adjacent display rows.
    # This is a review flag only: it must never delete an audible lyric or a name.
    opening=[cue for cue in segments[:2] if isinstance(cue,dict)]
    if opening and _OPENING_CREDIT.search(' '.join(str(c.get('text','')) for c in opening)):
        flag('hallucination','A passage resembles a common transcription artifact.',opening[0].get('start'))
    primary=(data.get('recognition_evidence') or {}).get('primary') or {}
    for raw in primary.get('segments',[]):
        if not isinstance(raw,dict) or not str(raw.get('text','')).strip():continue
        silence,probability=raw.get('no_speech_prob'),raw.get('avg_logprob')
        if (isinstance(silence,(int,float)) and isinstance(probability,(int,float))
            and silence>=.6 and probability<=-1):
            flag('non_speech_evidence','Words were recognized in audio the model also judged likely non-speech; listen to this saved passage.',raw.get('start'))
    for warning in data.get('warnings',[]):
        if warning:flag('model_warning','The extraction model left review warnings. Check the notes below.')
    count=len(words)
    if count<max(4,min(40,int(duration*.3))) or (duration and count/duration<.12):flag('sparse','Too few timed words were recovered for unattended song publication.')
    if duration and count/duration>4:flag('dense','Word density is unusually high; check for duplicated words.')
    confidence=(data.get('metrics') or {}).get('recognition_confidence')
    low=(data.get('metrics') or {}).get('low_confidence_fraction')
    if confidence is None or low is None:flag('confidence','Recognition confidence was not measured; keep this draft for review.')
    elif confidence<.65 or low>.15:flag('confidence','Recognition confidence is too low for automatic publication.')
    return {'version':VERSION,'status':'review' if reasons else 'pass','checked_at':int(time.time()),'reasons':reasons,'notice':'Machine checks can miss incorrect or missing words. Corrections and unpublishing remain available.','metrics':{'words':count,'lines':len(segments),'recognition_confidence':confidence,'low_confidence_fraction':low}}
