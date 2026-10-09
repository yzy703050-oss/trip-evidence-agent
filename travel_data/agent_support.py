"""Validated query construction for internal information tools."""
from datetime import date, timedelta
from travel_data.contracts import TrainQuery, HotelQuery, GuideQuery


def make_query(domain, fields):
    aliases = {'departure_date': 'start_date', 'city': 'destination'}
    required = {
        'train': ('origin', 'destination', 'departure_date'),
        'hotel': ('city', 'check_in', 'check_out', 'guests'),
        'guide': ('destination',),
    }[domain]
    values, missing = {}, []
    for key in required:
        value = fields.get(key, fields.get(aliases.get(key)))
        try:
            if key in {'departure_date', 'check_in', 'check_out'}:
                value = date.fromisoformat(value) if isinstance(value, str) else value
                if type(value) is not date:
                    raise ValueError()
            elif key == 'guests':
                if type(value) is not int or value < 1:
                    raise ValueError()
            elif not isinstance(value, str) or not value.strip():
                raise ValueError()
            values[key] = value
        except (ValueError, TypeError):
            missing.append(key)
    if missing:
        return None, missing
    if domain == 'train':
        passengers = fields.get('passengers')
        passengers = 1 if passengers is None else passengers
        if type(passengers) is not int or passengers < 1:
            return None, ['passengers']
        return TrainQuery(**values, passengers=passengers), []
    if domain == 'hotel':
        if values['check_out'] <= values['check_in']:
            return None, ['check_out']
        return HotelQuery(**values), []
    dates = fields.get('visit_dates')
    if dates is None:
        dates = []
        if fields.get('start_date'):
            try:
                start = date.fromisoformat(fields['start_date']) if isinstance(fields['start_date'], str) else fields['start_date']
                end_value = fields.get('end_date') or start
                end = date.fromisoformat(end_value) if isinstance(end_value, str) else end_value
                if type(start) is not date or type(end) is not date or end < start:
                    raise ValueError()
                dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            except (ValueError, TypeError):
                return None, ['visit_dates']
    try:
        if not isinstance(dates, list):
            raise ValueError()
        dates = [date.fromisoformat(item) if isinstance(item, str) else item for item in dates]
        if any(type(item) is not date for item in dates):
            raise ValueError()
    except (ValueError, TypeError):
        return None, ['visit_dates']
    return GuideQuery(**values, visit_dates=dates), []
