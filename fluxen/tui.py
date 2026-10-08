from __future__ import annotations
import asyncio
from pathlib import Path
from textual.app import App,ComposeResult
from textual.containers import Horizontal,Vertical
from textual.screen import ModalScreen
from textual.widgets import Header,Footer,Input,RichLog,Static,Button,DirectoryTree
from textual.binding import Binding
from textual import work
from rich.table import Table
from rich.text import Text
from .runs import Runs,ACTIVE
from .config import Settings,CONFIG_PATH,init_config
from .engine import Agent
from .session import Sessions
from .tools import Workspace

CSS='''
Screen {layout: vertical;}
#body {height: 1fr;}
#sidebar {width: 32%; min-width: 28;}
#left {height: 1fr; border: round $primary;}
#agents {height: 14; border: round $secondary; padding: 0 1; overflow-y: auto;}
#right {width: 1fr;}
#chat {height: 1fr; border: round $secondary; padding: 0 1;}
#status {height: 1; color: $text-muted; padding: 0 1;}
#prompt {dock: bottom; margin: 0 1 1 1;}
Approval {align: center middle; background: $background 70%;}
#dialog {width: 80%; max-width: 100; height: auto; max-height: 80%; border: thick $warning; background: $surface; padding: 1 2;}
#details {height: auto; max-height: 18; overflow-y: auto;}
#buttons {height: 3; align-horizontal: right;}
#buttons Button {margin-left: 2;}
'''
class Approval(ModalScreen[bool]):
    BINDINGS=[('escape','reject','Reject')]
    def __init__(self,message:str):super().__init__();self.message=message
    def compose(self)->ComposeResult:
        with Vertical(id='dialog'):
            yield Static('Approve tool execution?')
            yield Static(self.message,id='details')
            with Horizontal(id='buttons'):
                yield Button('Reject',id='no',variant='error')
                yield Button('Approve',id='yes',variant='success')
    def on_button_pressed(self,event:Button.Pressed)->None:
        self.dismiss(event.button.id=='yes')
    def action_reject(self)->None:self.dismiss(False)

class Fluxen(App):
    TITLE='Fluxen · terminal coding agent'
    CSS=CSS
    BINDINGS=[Binding('ctrl+c','cancel_task','Cancel task'),Binding('ctrl+l','clear_chat','Clear view'),
              Binding('ctrl+d','toggle_dark','Theme'),Binding('ctrl+q','quit','Quit')]
    def __init__(self,root:Path,settings:Settings,model:str|None=None,resume=False,backend="litellm"):
        super().__init__()
        self.workspace=Workspace(root,settings.max_output_chars)
        self.settings=settings;self.sessions=Sessions(root)
        if resume:self.sessions.resume()
        self.backend=backend
        if backend=='codex':
            from .codex import CodexAgent
            self.agent=CodexAgent(model,settings,self.workspace,self.sessions,self.emit,self.approve)
        else:
            self.agent=Agent(settings.model(model),settings,self.workspace,self.sessions,self.emit,self.approve)
        self.runs=Runs(self.sessions.dir)
        self.active=None
        self.partial=''
    def compose(self)->ComposeResult:
        yield Header()
        with Horizontal(id='body'):
            with Vertical(id='sidebar'):
                yield DirectoryTree(str(self.workspace.root),id='left')
                yield Static(id='agents',markup=False)
            with Vertical(id='right'):
                yield RichLog(id='chat',wrap=True,markup=True,highlight=True,auto_scroll=True)
                yield Static('',id='status')
        yield Input(placeholder='Describe a task or type /help',id='prompt')
        yield Footer()
    def on_mount(self)->None:
        self.query_one('#prompt',Input).focus()
        self.refresh_agents()
        self.set_interval(2,self.refresh_agents)
        self.write_chat(f'[bold green]Fluxen[/] · {self.workspace.root} · model: {self.agent.model.name}')
        if self.sessions.messages:self.write_chat(f'Resumed session {self.sessions.id} ({len(self.sessions.messages)} messages)')
    def write_chat(self,text:str):self.query_one('#chat',RichLog).write(text)
    def refresh_agents(self):
        if self.runs.current and self.runs.current['status'] in ACTIVE:
            self.runs.save()
        table=Table(title='Workspace agents',expand=True)
        for column in ('Agent / task','State','Progress'):
            table.add_column(column,overflow='fold')
        records=self.runs.list()
        for run in records:
            table.add_row(Text(run['model']+' · '+run['task'][:80]),Text(run['status']),Text(run['progress'][:80]))
        if not records:
            table.add_row('No agent runs yet','Idle','Enter a task to start')
        self.query_one('#agents',Static).update(table)
    async def emit(self,kind:str,value:str):
        if kind=='status':
            self.runs.update(progress=value)
            if value=='Max agent steps reached':self.runs.update(status='Step limit')
        elif kind=='error':self.runs.update(status='Failed',progress=value)
        elif kind=='tool':self.runs.update(progress=value.split('\n',1)[0])
        if kind!='token':self.refresh_agents()
        if kind=='token':
            self.partial+=value
            # Refresh streaming block by appending tokens, with a newline at completion.
            self.query_one('#status',Static).update(f'Streaming · {len(self.partial)} characters')
            if '\n' in value or len(self.partial)%160< len(value):
                self.write_chat(self.partial);self.partial=''
        else:
            if self.partial:self.write_chat(self.partial);self.partial=''
            if kind=='status' or kind=='usage':self.query_one('#status',Static).update(value)
            else:self.write_chat(f'[bold]{kind.upper()}[/] {value}')
    async def approve(self,description:str)->bool:
        self.runs.update(status='Waiting approval',progress=description.split('\n',1)[0])
        self.refresh_agents()
        try:
            return await self.push_screen_wait(Approval(description))
        finally:
            self.runs.update(status='Running')
    async def on_input_submitted(self,event:Input.Submitted):
        text=event.value.strip();event.input.value=''
        if not text:return
        if text.startswith('/'):
            self.command(text);return
        if self.active and self.active.is_running:
            self.write_chat('Task already running; Ctrl+C cancels it');return
        self.write_chat(f'\n[bold cyan]You:[/] {text}')
        self.active=self.run_agent(text)
    @work(exclusive=True,group='agent')
    async def run_agent(self,text:str):
        self.runs.start(text,self.agent.model.name)
        self.refresh_agents()
        try:
            await self.agent.run(text)
            if self.runs.current['status'] in ACTIVE:
                self.runs.update(status='Completed',progress='Done')
        except asyncio.CancelledError:
            self.runs.update(status='Cancelled',progress='Cancelled by user')
            self.write_chat('Task cancelled')
        except Exception as exc:
            self.runs.update(status='Failed',progress=str(exc))
            self.write_chat(f'Task failed: {exc}')
        finally:
            self.refresh_agents()
            if self.partial:self.write_chat(self.partial);self.partial=''
            self.query_one('#status',Static).update(f'Ready · {self.agent.model.name}')
            self.query_one('#left',DirectoryTree).reload()
    def command(self,cmd:str):
        parts=cmd.split(maxsplit=1);name=parts[0];arg=parts[1].strip() if len(parts)>1 else ''
        if name in ('/model','/provider'):
            if not arg:self.write_chat('Models: '+', '.join(m.name for m in self.settings.models));return
            try:
                if self.backend=='codex':
                    from types import SimpleNamespace
                    self.agent.model=SimpleNamespace(name=arg,model=arg)
                else:self.agent.model=self.settings.model(arg)
                self.write_chat(f'Model: {arg}')
            except ValueError as exc:self.write_chat(str(exc))
        elif name=='/agents':self.refresh_agents();self.query_one('#agents',Static).scroll_visible()
        elif name=='/models':self.write_chat('\n'.join(f'{m.name}: {m.model} [{m.provider}]' for m in self.settings.models))
        elif name=='/plan':self.agent.plan=not self.agent.plan;self.write_chat('Plan mode: '+str(self.agent.plan))
        elif name=='/diff':self.write_chat(self.workspace.last_diff or 'No agent file diff available')
        elif name=='/undo':self.write_chat(self.workspace.undo_last());self.query_one('#left',DirectoryTree).reload()
        elif name=='/sessions':self.write_chat('Saved session IDs: '+', '.join(self.sessions.list()))
        elif name=='/config':self.write_chat(f'Edit {init_config()} then restart for configuration changes')
        elif name=='/help':self.write_chat('/model NAME · /provider NAME · /models · /agents · /plan · /diff · /undo · /sessions · /config · /help')
        else:self.write_chat('Unknown command. /help')
    def action_cancel_task(self):
        if self.active and self.active.is_running:self.active.cancel()
    def action_clear_chat(self):self.query_one('#chat',RichLog).clear()
    def action_toggle_dark(self):self.theme='textual-light' if self.theme!='textual-light' else 'textual-dark'
