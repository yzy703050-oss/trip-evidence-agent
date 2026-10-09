"""Retrieve one existing travel guide link through DDGS, without page scraping."""

import asyncio
from datetime import datetime, timezone
from math import ceil

from travel_data.contracts import AgentDataResult, GuideFact, GuideQuery, Source
from travel_data.result_guard import valid_url


class DDGSGuideProvider:
    def __init__(self, text_search=None, *, timeout=10):
        self.timeout = timeout
        self.text_search = text_search or self._text_search

    def _text_search(self, query):
        from ddgs import DDGS

        with DDGS(timeout=max(1, ceil(self.timeout))) as client:
            return client.text(query, max_results=5, region='cn-zh',
                               safesearch='moderate', backend='bing')

    async def search(self, query: GuideQuery) -> AgentDataResult:
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(self.text_search, f'{query.destination.strip()} 旅游攻略'),
                timeout=self.timeout,
            )
            valid = [row for row in rows if isinstance(row, dict) and valid_url(row.get('href'))]
            # Search engines can rank an unrelated tourism university ahead of guides.
            preferred = next((row for row in valid if '攻略' in
                              f"{row.get('title', '')} {row.get('body', '')}"), None)
            if valid:
                row = preferred or valid[0]
                url = row['href']
                title = row.get('title')
                title = title.strip() if isinstance(title, str) and title.strip() else '旅游攻略链接'
                source = Source('DDGS', datetime.now(timezone.utc), url)
                # This verifies the retrieved link, not facts inside the linked article.
                fact = GuideFact('link', f'{title}\n{url}', 'verified', source)
                return AgentDataResult('ok', query.to_dict(), [fact.to_dict()], [], source,
                                       source.fetched_at, '返回检索到的攻略链接；网页内容未作核验。')
            return AgentDataResult('ok', query.to_dict(), [], [], None, None,
                                   '未找到相关攻略链接，请尝试更具体的目的地。')
        except Exception:
            return AgentDataResult('error', query.to_dict(), [], [], None, None,
                                   '攻略搜索失败或超时，请稍后重试。')
