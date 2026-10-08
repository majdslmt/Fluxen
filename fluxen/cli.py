from __future__ import annotations
import argparse
from pathlib import Path
from .config import load,init_config

def main():
    parser=argparse.ArgumentParser(description='Fluxen: multi-model terminal coding agent')
    parser.add_argument('--backend',choices=('litellm','codex'),default='litellm',help='Agent backend (default: litellm)')
    parser.add_argument('--model',help='Configured model alias (e.g. gpt, claude)')
    parser.add_argument('--provider',help='Configured provider/model alias (e.g. azure)')
    parser.add_argument('--continue',dest='resume',action='store_true',help='Resume latest session')
    parser.add_argument('--init',action='store_true',help='Create user config and exit')
    parser.add_argument('--workspace',type=Path,default=Path.cwd())
    args=parser.parse_args()
    if args.init:print(init_config());return
    from .tui import Fluxen
    settings=load()
    Fluxen(args.workspace,settings,args.model or args.provider,args.resume,backend=args.backend).run()
if __name__=='__main__':main()
