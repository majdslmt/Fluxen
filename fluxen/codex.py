"""Codex app-server backend using its local stdio protocol."""
from __future__ import annotations
import asyncio
import json
import shutil
from types import SimpleNamespace

class CodexAgent:
    def __init__(self, model, settings, workspace, sessions, emit, approve):
        self.model = SimpleNamespace(name=model or 'codex', model=model)
        self.settings=settings; self.workspace=workspace; self.sessions=sessions
        self.emit=emit; self.approve=approve; self.plan=False; self.usage=[0,0]
        self.thread_id=None; self.turn_id=None; self.sequence=0; self.items={}
        self.state_path=sessions.dir / (sessions.id + '.codex')
        if self.state_path.exists():
            self.thread_id=self.state_path.read_text(encoding='utf-8').strip() or None

    async def send(self, message):
        self.proc.stdin.write((json.dumps(message)+'\n').encode())
        await self.proc.stdin.drain()

    async def receive(self):
        line=await self.proc.stdout.readline()
        if not line:
            raise RuntimeError('Codex app-server exited. Check codex login and your Codex installation.')
        return json.loads(line)

    async def request(self, method, params):
        self.sequence+=1
        identifier=self.sequence
        await self.send(dict(id=identifier, method=method, params=params))
        while True:
            message=await self.receive()
            if message.get('id')==identifier and 'method' not in message:
                if 'error' in message:
                    raise RuntimeError(message['error'].get('message',str(message['error'])))
                return message.get('result',{})
            await self.event(message)

    async def event(self, message):
        method=message.get('method',''); params=message.get('params',{})
        if 'id' in message and method:
            if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
                allowed=False
                if not self.plan and self.settings.permission!='read-only':
                    preview=dict(params)
                    if params.get('itemId') in self.items:preview['item']=self.items[params['itemId']]
                    allowed=await self.approve(method+'\n'+json.dumps(preview,ensure_ascii=False,indent=2))
                await self.send({'id':message['id'],'result':{'decision':'accept' if allowed else 'decline'}})
            elif method=='item/permissions/requestApproval':
                await self.send({'id':message['id'],'result':{'permissions':{},'scope':'turn'}})
            else:
                await self.send({'id':message['id'],'error':{'code':-32601,'message':'Unsupported Fluxen client request'}})
            return
        if method=='item/agentMessage/delta':
            text=params.get('delta','')
            self.reply+=text
            await self.emit('token',text)
        elif method in ('item/started','item/completed'):
            item=params.get('item',{})
            if item.get('id'):self.items[item['id']]=item
            if item.get('type') not in ('agentMessage','userMessage'):
                await self.emit('tool',item.get('type','tool')+': '+item.get('status','started'))
        elif method=='turn/diff/updated':
            self.workspace.last_diff=params.get('diff','')
        elif method=='error':
            await self.emit('error',params.get('error',{}).get('message','Codex error'))
        elif method=='turn/started':
            self.turn_id=params.get('turn',{}).get('id')
        elif method=='turn/completed':
            self.finished=params.get('turn',{})

    async def run(self,prompt):
        executable=shutil.which('codex')
        if not executable:
            raise RuntimeError('Codex CLI is not installed or on PATH. Install Codex and run codex login first.')
        self.reply=''; self.finished=None; self.turn_id=None
        await self.emit('status','Starting Codex')
        self.proc=await asyncio.create_subprocess_exec(executable,'app-server',cwd=self.workspace.root,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,limit=4*1024*1024)
        self.sessions.messages.append({'role':'user','content':prompt})
        self.sessions.save()
        try:
            await asyncio.wait_for(self.request('initialize',{'clientInfo':{'name':'fluxen','title':'Fluxen','version':'0.1.0'}}),30)
            await self.send({'method':'initialized','params':{}})
            readonly=self.plan or self.settings.permission=='read-only'
            options={'cwd':str(self.workspace.root),'approvalPolicy':'never' if readonly else 'untrusted',
                     'sandbox':'read-only' if readonly else 'workspace-write'}
            if self.model.model:options['model']=self.model.model
            if self.thread_id:
                options['threadId']=self.thread_id
                result=await self.request('thread/resume',options)
            else:
                result=await self.request('thread/start',options)
            self.thread_id=result['thread']['id']
            self.state_path.write_text(self.thread_id,encoding='utf-8')
            policy={'type':'readOnly'} if readonly else {'type':'workspaceWrite',
                    'writableRoots':[str(self.workspace.root)],'networkAccess':False}
            turn={'threadId':self.thread_id,'input':[{'type':'text','text':prompt}],
                  'approvalPolicy':'never' if readonly else 'untrusted','sandboxPolicy':policy}
            if self.plan:turn['input'][0]['text']='Planning only: inspect and explain a plan without changing files. '+prompt
            if self.model.model:turn['model']=self.model.model
            result=await self.request('turn/start',turn)
            self.turn_id=result['turn']['id']
            while self.finished is None:
                await self.event(await self.receive())
            status=self.finished.get('status')
            if status!='completed':
                raise RuntimeError('Codex turn '+str(status)+': '+str(self.finished.get('error') or 'interrupted'))
        except asyncio.CancelledError:
            if self.thread_id and self.turn_id:
                try:
                    await asyncio.wait_for(self.request('turn/interrupt',{'threadId':self.thread_id,'turnId':self.turn_id}),5)
                except Exception:pass
            raise
        finally:
            if self.reply:self.sessions.messages.append({'role':'assistant','content':self.reply})
            self.sessions.save()
            if self.proc.returncode is None:
                self.proc.terminate()
                try:await asyncio.wait_for(self.proc.wait(),5)
                except asyncio.TimeoutError:
                    self.proc.kill();await self.proc.wait()
