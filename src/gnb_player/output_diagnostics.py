"""Private, bounded output troubleshooting history. Never store destinations."""
from collections import deque
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid

from .broadcast import redact

MAX_BYTES = 65536
MAX_EVENTS = 64
TEXT_FIELDS = {'message', 'reason', 'stderr', 'stderr_tail', 'error', 'timestamp_utc'}
NUMBER_FIELDS = {'attempt', 'pid', 'returncode', 'width', 'height', 'fps', 'bitrate_kbps',
                 'retry', 'delay_seconds', 'user_stop', 'running', 'elapsed_seconds',
                 'video_frames', 'audio_frames', 'audio_dropped_frames', 'silence_frames',
                 'delivery_lag_seconds', 'reconnects', 'duplicated_video_frames',
                 'replaced_video_inputs', 'audio_buffer_seconds', 'timestamp', 'next_attempt'}


def clean(value, target='', secrets=()):
    text = str(value)
    text = re.sub(r'(?i)\b(password|passwd|token|access[_-]?token|api[_-]?key|stream[_-]?key|key|secret)["\']?\s*[:=]\s*(?:"[^"]*"|\'[^\']*\'|[^\s,;]+)',
                  lambda m: m.group(1)+'=[redacted]', text)
    text = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [redacted]', text)
    return redact(text, target, secrets)


class OutputDiagnostics:
    def __init__(self, path, metadata=None):
        self.path = Path(path)
        self.metadata = {k: clean(v) for k,v in (metadata or {}).items() if k in {'app_version','build'}}
        self.events = deque(maxlen=MAX_EVENTS)
        self.persistence_error = ''
        self.session = uuid.uuid4().hex
        try:
            if self.path.is_file() and not self.path.is_symlink() and self.path.stat().st_size <= MAX_BYTES:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if data.get('schema') == 1 and isinstance(data.get('events'),list):
                    for event in data['events'][-MAX_EVENTS:]:
                        if isinstance(event,dict): self.events.append(self._safe(event))
        except (OSError,ValueError,TypeError,AttributeError):
            self.persistence_error = 'Previous diagnostics could not be read.'

    def _safe(self, event, target='', secrets=()):
        result = {}
        name = event.get('event','unknown')
        result['event'] = name if isinstance(name,str) and re.fullmatch('[a-z_]{1,40}',name) else 'unknown'
        for key,value in event.items():
            if key in TEXT_FIELDS or key in {'time_utc','app_version','build','session'}:
                if isinstance(value,str):result[key] = clean(value,target,secrets)
            elif key == 'mode' and isinstance(value,str) and value in {'record','stream'}:result[key] = value
            elif key == 'local_feed_errors' and isinstance(value,list):
                result[key] = [clean(v,target,secrets)[:400] for v in value[-4:] if isinstance(v,str)]
            elif key == 'counters' and isinstance(value,dict):
                result[key] = {k:v for k,v in value.items() if k in NUMBER_FIELDS
                               and isinstance(v,(int,float)) and math.isfinite(v)}
            elif key in NUMBER_FIELDS and (value is None or isinstance(value,(bool,int,float))):
                if value is None or isinstance(value,bool) or math.isfinite(value):result[key] = value
        return result

    def _payload(self, target='', secrets=()):
        return {'schema':1,'metadata':{k:clean(v,target,secrets) for k,v in self.metadata.items()},
                'events':[self._safe(e,target,secrets) for e in self.events],
                'persistence_error':self.persistence_error}

    def append(self, event, target='', secrets=()):
        if not isinstance(event,dict):return
        row = self._safe(event,target,secrets)
        row.update(time_utc=datetime.now(timezone.utc).isoformat(),session=self.session,
                   app_version=clean(self.metadata.get('app_version','unknown'),target,secrets),
                   build=clean(self.metadata.get('build','unrecorded'),target,secrets))
        self.events.append(row)
        while len(self.text().encode('utf-8')) > MAX_BYTES and len(self.events)>1:self.events.popleft()
        temporary = None
        try:
            if self.path.is_symlink():raise OSError('Refusing diagnostic symlink')
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=self.path.parent,prefix='.output-log-',delete=False) as handle:
                temporary = Path(handle.name);os.chmod(temporary,0o600)
                handle.write(self.text());handle.flush();os.fsync(handle.fileno())
            temporary.replace(self.path)
            self.persistence_error = ''
        except OSError as error:
            self.persistence_error = 'Diagnostics are available in memory; saving failed ('+type(error).__name__+').'
        finally:
            if temporary is not None:
                try:temporary.unlink(missing_ok=True)
                except OSError:pass

    def text(self, target='', secrets=()):
        return json.dumps(self._payload(target,secrets),ensure_ascii=False,allow_nan=False,indent=2)
