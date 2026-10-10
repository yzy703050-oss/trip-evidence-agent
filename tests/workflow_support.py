from copy import deepcopy


def proposal(cities=None):
    cities = cities or ['上海', '北京', '杭州', '上海']
    return {
        'confirmed_conditions': {'start_date': '2026-10-11'},
        'tasks': [dict(origin=a, destination=b, requires_hotel=i < len(cities)-2,
                       conditions={'departure_date': f'2026-10-{11+i*2:02d}',
                                   **({'check_in': f'2026-10-{11+i*2:02d}',
                                       'check_out': f'2026-10-{13+i*2:02d}'} if i < len(cities)-2 else {})},
                       field_sources={'departure_date': 'user', 'check_in': 'user', 'check_out': 'user'})
                  for i, (a, b) in enumerate(zip(cities, cities[1:]))]
    }


def workflow():
    from agents.workflow_contracts import create_workflow
    return create_workflow(deepcopy(proposal()), {'original_query': '安排火车酒店'})


def populated_workflow(*, quoted=False):
    from agents.workflow_queries import record_query
    from test_workflow_queries import put_train, SOURCE
    w = workflow()
    for i, t in enumerate(w['tasks']):
        train = put_train(w, i)
        draft = {'task_revision': 1,
                 'train_selection': dict(query_id=train['id'], result_revision=1, candidate_id=train['items'][0]['id']),
                 'hotel_selection': None, 'schedule': {k: v for k, v in t['conditions'].items() if k in {'check_in', 'check_out'}},
                 'unverified_requirements': []}
        if t['requires_hotel']:
            if quoted:
                p = {'city': t['destination'], 'check_in': t['conditions']['check_in'],
                     'check_out': t['conditions']['check_out'], 'guests': 1}
                item = dict(id=f'hotel{i}', hotel_name='酒店', room_type='标准间', check_in=p['check_in'], check_out=p['check_out'],
                            guests=1, stay_total_cny='200', fees_included=True, availability='available', cancellation=None,
                            source=SOURCE, url=SOURCE['url'])
            else:
                p = {'city': t['destination'], 'search_kind': 'hotel_place', 'keywords': '酒店'}
                item = dict(id=f'place{i}', kind='hotel_place', hotel_name='酒店', address='地址', city=t['destination'],
                            district='', location='116.1,39.1', source=SOURCE)
            data = dict(status='ok', items=[item], source=SOURCE, fetched_at=SOURCE['fetched_at'], missing_fields=[])
            hotel = record_query(w, t['id'], p, {}, data, {}, domain='hotel')
            draft['hotel_selection'] = dict(query_id=hotel['id'], result_revision=1, candidate_id=hotel['items'][0]['id'],
                                            kind='hotel_offer' if quoted else 'hotel_place')
        t.update(status='draft', draft_plan=draft)
    return w
