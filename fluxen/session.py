from __future__ import annotations
import json
from datetime import datetime,timezone
from pathlib import Path
from platformdirs import user_data_dir
import uuid

class Sessions:
    def __init__(self,workspace:Path):
        import hashlib
        key=hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]
        self.dir=Path(user_data_dir('fluxen'))/'sessions'/key
        self.dir.mkdir(parents=True,exist_ok=True)
        self.id=uuid.uuid4().hex[:12]
        self.messages:list[dict]=[]
    def save(self):
        p=self.dir/f'{self.id}.json'
        tmp=p.with_suffix('.tmp')
        tmp.write_text(json.dumps({'id':self.id,'updated':datetime.now(timezone.utc).isoformat(),
                         'messages':self.messages},ensure_ascii=False,indent=2),encoding='utf-8')
        tmp.replace(p)
    def resume(self):
        paths=sorted(self.dir.glob('*.json'),key=lambda x:x.stat().st_mtime,reverse=True)
        if not paths:return False
        data=json.loads(paths[0].read_text(encoding='utf-8'))
        self.id=data['id'];self.messages=data['messages']
        return True
    def list(self):
        return [p.stem for p in sorted(self.dir.glob('*.json'),reverse=True)[:20]]
