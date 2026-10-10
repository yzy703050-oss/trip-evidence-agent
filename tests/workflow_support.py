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
