from __future__ import annotations
import asyncio
import difflib
import json
import os
from pathlib import Path
import re
import shutil

SPECS = [
    ('read_file','Read a UTF-8 workspace file',{'path':str}),
    ('list_files','List files in the workspace (excluding common ignored directories)',{'path':str}),
    ('search','Search workspace text files for a literal string',{'query':str}),
    ('write_file','Create or overwrite a UTF-8 workspace file',{'path':str,'content':str}),
    ('delete_file','Delete a workspace file',{'path':str}),
    ('run_shell','Run a shell command in workspace (requires approval)',{'command':str}),
    ('git_status','Show Git status',{}),
    ('git_diff','Show working tree Git diff',{}),
]
def schemas() -> list[dict]:
    result=[]
    for name, description, args in SPECS:
        result.append({'type':'function','function':{
            'name':name,'description':description,
            'parameters':{'type':'object','properties':{k:{'type':'string'} for k in args},
                          'required':list(args),'additionalProperties':False}}})
    return result

class Workspace:
    def __init__(self,root:Path, limit:int=12000):
        self.root=root.resolve(strict=True)
        self.limit=limit
        self.undo: list[tuple[Path,bytes|None]]=[]
        self.last_diff=''

    def path(self,value:str) -> Path:
        p = (self.root / value).resolve()
        if not p.is_relative_to(self.root): raise PermissionError('Path escapes workspace')
        if p == self.root: raise PermissionError('Expected a file or subdirectory')
        return p

    def preview(self,name:str,args:dict) -> str:
        p=self.path(args['path'])
        if name == 'delete_file':
            return f'DELETE {p.relative_to(self.root)}\n' + (p.read_text(errors='replace')[:3000] if p.is_file() else '(not a file)')
        old=p.read_text(errors='replace').splitlines(keepends=True) if p.exists() else []
        new=args['content'].splitlines(keepends=True)
        return ''.join(difflib.unified_diff(old,new,fromfile=f'a/{args["path"]}',tofile=f'b/{args["path"]}'))[:self.limit]

    async def execute(self,name:str,args:dict) -> str:
        if name == 'read_file':
            p=self.path(args['path'])
            if not p.is_file(): return 'Not a file'
            return p.read_text(encoding='utf-8',errors='replace')[:self.limit]
        if name == 'list_files':
            base=self.path(args.get('path') or '.') if args.get('path') not in (None,'.','') else self.root
            if not base.is_dir(): return 'Not a directory'
            ignored={'.git','.venv','node_modules','__pycache__','.mypy_cache','.pytest_cache'}
            found=[]
            for root,dirs,files in os.walk(base,followlinks=False):
                dirs[:]=[d for d in dirs if d not in ignored and not (Path(root)/d).is_symlink()]
                for f in sorted(files):
                    p=Path(root)/f
                    if p.is_symlink():continue
                    found.append(str(p.relative_to(self.root)))
                    if len(found)>=300:break
                if len(found)>=300:break
            return '\n'.join(found) or '(empty)'
        if name == 'search':
            query=args['query']; results=[]
            if not query: return 'Empty query'
            for p in self.root.rglob('*'):
                if any(x in {'.git','.venv','node_modules','__pycache__'} for x in p.relative_to(self.root).parts):continue
                if not p.is_file() or p.is_symlink() or p.stat().st_size>1_000_000:continue
                try:
                    for i,line in enumerate(p.read_text(encoding='utf-8').splitlines(),1):
                        if query in line: results.append(f'{p.relative_to(self.root)}:{i}: {line[:250]}')
                        if len(results)>=100:break
                except (UnicodeError,OSError): pass
                if len(results)>=100:break
            return '\n'.join(results)[:self.limit] or 'No matches'
        if name in ('write_file','delete_file'):
            p=self.path(args['path'])
            if p.exists() and not p.is_file(): raise ValueError('Not a regular file')
            original=p.read_bytes() if p.exists() else None
            self.last_diff=self.preview(name,args)
            if name=='write_file':
                p.parent.mkdir(parents=True,exist_ok=True)
                # Re-resolve after directory creation, to reject symlink races in ordinary cases.
                if not p.resolve().is_relative_to(self.root): raise PermissionError('Path escapes workspace')
                tmp=p.with_name(p.name+'.fluxen-tmp')
                tmp.write_text(args['content'],encoding='utf-8')
                os.replace(tmp,p)
            else:
                if original is None:return 'Already absent'
                p.unlink()
            self.undo.append((p,original))
            return f'{name}: {p.relative_to(self.root)}'
        if name=='run_shell':
            command=args['command']
            proc=await asyncio.create_subprocess_shell(command,cwd=self.root,
                      stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
            try:
                data,_=await asyncio.wait_for(proc.communicate(),timeout=30)
            except asyncio.TimeoutError:
                proc.kill();await proc.communicate();return 'Command timed out after 30s'
            except asyncio.CancelledError:
                proc.kill();await proc.communicate();raise
            return f'exit={proc.returncode}\n'+data.decode(errors='replace')[:self.limit]
        if name in ('git_status','git_diff'):
            args2=['git','status','--short'] if name=='git_status' else ['git','diff','--no-ext-diff']
            proc=await asyncio.create_subprocess_exec(*args2,cwd=self.root,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
            data,_=await asyncio.wait_for(proc.communicate(),timeout=15)
            return data.decode(errors='replace')[:self.limit]
        raise ValueError('Unknown tool: '+name)

    def undo_last(self) -> str:
        if not self.undo:return 'Nothing to undo in this process'
        p,old=self.undo.pop()
        if old is None:
            if p.exists():p.unlink()
        else:
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_bytes(old)
        return f'Undid change to {p.relative_to(self.root)}'
