"""Operator-driven native smoke: isolated state/hub, trial only, no Telegram client."""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.orca_bridge import OrcaBridge, OrcaQueue
from app.orca_handoff import Handoff
from app.state import StateStore


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['init','start','tick','status','switch','reply','recover'])
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--run')
    p.add_argument('--coordinator')
    p.add_argument('--id')
    p.add_argument('--agent',choices=['codex','claude','astra','sol'],default='astra')
    p.add_argument('--source',default='hub:smoke-switch-1')
    p.add_argument('--text',default='SMOKE_CONTINUE_741: Read the durable handoff context before any work; report its objective, checkpoint and prior dispatch ID via worker_done. Read-only smoke: edit no files and finish successfully.')
    args=p.parse_args()
    state=args.state.resolve()
    if not str(state).startswith('/tmp/orca-handoff-smoke-'):
        p.error('--state must be a dedicated /tmp/orca-handoff-smoke-... directory')
    config_path=state/'smoke-config.json'
    if args.action=='init':
        if not args.run or not args.coordinator or config_path.exists():
            p.error('init requires a NEW --run and --coordinator, and unused state directory')
        base=tomllib.loads((ROOT/'config/orca.toml').read_text())
        if args.run==base['run_id']:
            p.error('Never use the production bridge Run for this smoke')
        config=dict(base,run_id=args.run,coordinator=args.coordinator,hub_root=str(state/'hub'),projects={'trial':base['projects']['trial']})
        state.mkdir(parents=True,mode=0o700)
        config_path.write_text(json.dumps(config))
        print(config_path)
        return
    config=json.loads(config_path.read_text())
    q=OrcaQueue(StateStore(state/'state.sqlite',ROOT/'schema.sql'),config)
    bridge=OrcaBridge(q)
    if args.action=='start':
        text=('READ-ONLY NATIVE HANDOFF SMOKE. Edit no files and do not run hub claim/done, commits or messages outside this Run. '
              'Read the handoff context named in this spec first. If its instruction contains SMOKE_CONTINUE_741, '
              'report that marker, original objective, checkpoint and old dispatch ID via worker_done and finish. '
              'Otherwise use the native ask command to ask the coordinator: SMOKE_WAIT_741 ready for model switch? '
              'Wait for its answer; do not send worker_done until answered. No website changes or Telegram messages.')
        print(q.submit('hub:smoke-start-1','trial',args.agent,text)['id'])
    elif args.action=='tick':
        bridge.tick()
    elif args.action=='status':
        print(q.describe(args.id or ''))
        with q.store.connect() as db:
            print(json.dumps([dict(r) for r in db.execute('SELECT request_id,state,error FROM orca_changes')],ensure_ascii=False))
    else:
        if not args.id: p.error('action requires --id')
        if args.action=='switch': print(Handoff(q).request(args.id,args.source,args.agent,args.text)['request_id'])
        elif args.action=='reply': q.answer(args.id,args.source,args.text)
        elif args.action=='recover': q.recover(args.id)


if __name__=='__main__': main()
