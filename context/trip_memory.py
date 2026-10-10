"""Readable projection of canonical travel snapshots; never used to restore facts."""
import json
import logging
import os
from pathlib import Path
import tempfile

from context.session_store import storage_component

logger = logging.getLogger(__name__)


class TripMemory:
    def __init__(self, root, user_id):
        self.directory=Path(root)/storage_component(user_id)
        self.path=self.directory/'trip.md'

    def rebuild(self, workflow_store):
        from context.workflow_store import _file_lock
        with _file_lock(self.directory/'trip-summary.lock'):
            workflows=workflow_store.list_all()
            lines=['# 保存的旅行方案', '', 'JSON 工作流是权威状态；本文件为可重建摘要。规划不代表已经出行或预订。', '']
            for w in workflows:
                lines.extend(['## '+w['id'], f"revision: {w['revision']}", f"status: {w['status']}",
                              'record_kind: planned_itinerary', 'route: '+ ' → '.join([w['tasks'][0].get('origin') or '出发地待补充']+[t['destination'] for t in w['tasks']]),
                              'conditions: '+json.dumps(w['confirmed_conditions'],ensure_ascii=False),
                              'recent_change: '+json.dumps(w.get('last_update',{}),ensure_ascii=False)])
                for t in w['tasks']:
                    draft=t.get('draft_plan') or {}
                    selections={}
                    for domain in ('train','hotel'):
                        ref=draft.get(domain+'_selection')
                        row=w['results_by_query'].get(ref.get('query_id')) if ref else None
                        item=next((i for i in row['items'] if i['id']==ref.get('candidate_id')),None) if row else None
                        if item: selections[domain]={k:item.get(k) for k in ('id','train_number','hotel_name','departure_at','arrival_at','source') if k in item}
                    lines.append('- '+json.dumps(dict(task_id=t['id'],revision=t['revision'],status=t['status'],
                        purpose=t.get('purpose','unspecified'),requires_hotel=t['requires_hotel'],conditions=t['conditions'],
                        field_sources=t['field_sources'],schedule=draft.get('schedule',{}),selected=selections,
                        gaps=t['issues'],summary=t['summary']),ensure_ascii=False))
                lines.append('')
            legacy_path=self.directory/'trips.json'
            legacy=json.loads(legacy_path.read_text(encoding='utf-8')).get('trip_history',[]) if legacy_path.exists() else []
            ids={w['id'] for w in workflows}
            for index,record in enumerate(legacy):
                if record.get('workflow_id') in ids: continue
                lines.extend(['## legacy:'+str(record.get('trip_id',index)), json.dumps(record,ensure_ascii=False), ''])
            text='\n'.join(lines)+'\n'
            if self.path.exists() and self.path.read_text(encoding='utf-8')==text: return
            self.directory.mkdir(parents=True,exist_ok=True)
            name=None
            try:
                with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=self.directory,delete=False) as f:
                    name=f.name; f.write(text); f.flush(); os.fsync(f.fileno())
                os.replace(name,self.path)
            finally:
                if name and os.path.exists(name): os.unlink(name)

    def repair(self, workflow_store):
        try: self.rebuild(workflow_store)
        except (OSError,ValueError): logger.exception('Trip summary update failed; canonical workflows remain available')
