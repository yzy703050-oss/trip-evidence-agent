"""Program-owned next-page commands over current, authorized cached evidence."""

from copy import deepcopy

from agents.workflow_queries import query_views
from travel_data.candidates import query_cache_key
from travel_data.tools import PARAMETERS, _constraints_compatible


def cached_page_options(workflow, candidates, *, limit):
    options = []
    if candidates is None:
        return options
    for task in workflow['tasks']:
        for view in query_views(workflow, task['id'], limit=limit):
            domain = view['domain']
            if domain not in {'train', 'hotel'} or domain not in task.get('update_scope', ['train', 'hotel']):
                continue
            if domain == 'hotel' and not task['requires_hotel']:
                continue
            container = task.get('query_containers', {}).get(domain)
            if container and container.get('current_query_ref') != view['id']:
                continue
            if view.get('needs_revalidation') or view['status'] not in {'ok', 'partial'}:
                continue
            offset = view['view_offset'] + len(view['items'])
            if not view['items'] or offset >= view['candidate_total']:
                continue
            raw = candidates.cache.get(query_cache_key(domain, view['parameters']))
            if raw is None or raw.status not in {'ok', 'partial'}:
                continue
            if raw.to_dict().get('fetched_at') != view.get('fetched_at'):
                continue
            name = ('train_search_by_arrival' if view['parameters'].get('search_kind') == 'train_arrival'
                    else domain + '_search')
            parameters = {k: deepcopy(v) for k, v in view['parameters'].items() if k in PARAMETERS[name]}
            parameters.update(constraints=deepcopy(view['constraints']), candidate_offset=offset)
            options.append({
                'query_id': view['id'], 'result_revision': view['result_revision'],
                'task_id': task['id'], 'task_revision': task['revision'],
                'domain': domain, 'name': name, 'parameters': parameters,
            })
    return options


def validate_cached_page_batch(batch, workflow, candidates, *, limit):
    if not batch.get('requests') or batch.get('gaps'):
        raise ValueError('本地翻页必须指定完整、有效的下一页请求。')
    options = cached_page_options(workflow, candidates, limit=limit)
    checked, seen = deepcopy(batch), set()
    for request in checked['requests']:
        params = deepcopy(request.get('parameters', {}))
        if not isinstance(params, dict) or params.get('refresh', False) is not False:
            raise ValueError('本地翻页不能刷新供应商数据。')
        params.pop('refresh', None)
        constraints = params.pop('constraints', {})
        if type(params.get('candidate_offset')) is not int:
            raise ValueError('本地翻页需要有效的下一页偏移。')
        matches = []
        for option in options:
            if any(request.get(k) != option[k] for k in ('task_id', 'task_revision', 'domain', 'name')):
                continue
            if any(k in request and request[k] != option[k] for k in ('query_id', 'result_revision')):
                continue
            expected = {k: v for k, v in option['parameters'].items() if k != 'constraints'}
            fixed = option['parameters']['constraints']
            if (params == expected and isinstance(constraints, dict) and set(constraints) == set(fixed)
                    and _constraints_compatible(fixed, constraints)):
                matches.append(option)
        if len(matches) != 1 or matches[0]['query_id'] in seen:
            raise ValueError('仅允许读取当前有效查询的下一缓存页，不能修改条件或重复目标。')
        seen.add(matches[0]['query_id'])
        request['cache_only'] = True
    return checked
