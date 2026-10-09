"""Weather and web retrieval without a summarization model."""
import asyncio
from datetime import datetime, timezone, date
from urllib.parse import quote, urlparse
import re

from travel_data.contracts import AgentDataResult, Source


class PublicQueryProvider:
    def __init__(self, weather_fetch=None, web_fetch=None):
        self.weather_fetch = weather_fetch or self._weather_fetch
        self.web_fetch = web_fetch or self._web_fetch

    @staticmethod
    def _weather_fetch(city):
        import httpx
        response = httpx.get(f'https://wttr.in/{quote(city)}?format=j1', timeout=10,
                             headers={'User-Agent': 'curl/7.64.1'})
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _web_fetch(query):
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as client:
            for backend in ('bing', 'duckduckgo', 'auto'):
                try:
                    rows = list(client.text(query, max_results=10, safesearch='on', region='cn-zh', backend=backend))
                    if rows:
                        return rows
                except Exception:
                    continue
        return []

    async def weather(self, city, requested_date):
        query = {'city': city, 'date': requested_date}
        source = Source('wttr.in', datetime.now(timezone.utc), f'https://wttr.in/{quote(city)}?format=j1')
        try:
            data = await asyncio.to_thread(self.weather_fetch, city)
            items = []
            if requested_date:
                date.fromisoformat(requested_date)
                for row in data.get('weather', []):
                    if row.get('date') == requested_date:
                        hourly = row.get('hourly') or [{}]
                        desc = (hourly[0].get('weatherDesc') or [{}])[0].get('value', '')
                        items.append({'date': requested_date, 'description': desc,
                            'min_temp_c': float(row['mintempC']), 'max_temp_c': float(row['maxtempC']), 'source': source.to_dict()})
            else:
                row = (data.get('current_condition') or [{}])[0]
                desc = (row.get('weatherDesc') or [{}])[0].get('value', '')
                if row.get('temp_C') is not None:
                    items.append({'date': None, 'temp_c': float(row['temp_C']), 'description': desc, 'source': source.to_dict()})
            return AgentDataResult('ok' if items else 'partial', query, items, [], source, source.fetched_at,
                                   None if items else '天气接口没有返回所请求日期的数据。')
        except Exception:
            return AgentDataResult('error', query, [], [], source, source.fetched_at, '天气接口查询失败。')

    async def web(self, query):
        source = Source('DDGS', datetime.now(timezone.utc), 'https://duckduckgo.com')
        try:
            rows = await asyncio.to_thread(self.web_fetch, query)
            items = []
            for row in rows:
                url = row.get('href', '')
                parsed = urlparse(url)
                host = parsed.hostname or ''
                name = host.rsplit('.', 2)[0]
                if parsed.scheme not in {'http', 'https'} or not host or re.search(r'\.(cc|tk|ml|ga|cf|gq|xyz|top|work|click|link|pw|buzz)$', host) or (len(name) >= 10 and re.fullmatch(r'[a-z0-9]+', name)):
                    continue
                item_source = Source('DDGS', source.fetched_at, url)
                items.append({'title': row.get('title', ''), 'snippet': row.get('body', ''), 'url': url, 'source': item_source.to_dict()})
                if len(items) == 5:
                    break
            return AgentDataResult('ok', {'query': query}, items, [], source, source.fetched_at,
                                   None if items else '未找到相关网页结果。')
        except Exception:
            return AgentDataResult('error', {'query': query}, [], [], source, source.fetched_at, '网页查询失败。')
