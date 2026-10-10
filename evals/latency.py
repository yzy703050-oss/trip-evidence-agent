"""Latency statistics: serial model latency vs parallel tool service sum."""
from collections import defaultdict
from statistics import mean


def summarize_latency(calls,tools,*,total_ms,external_requests):
    grouped=defaultdict(list)
    for row in calls: grouped[row.get('stage','unspecified')].append(row)
    stages={}
    for name,rows in grouped.items():
        values=sorted(float(r['latency_ms']) for r in rows)
        stages[name]=dict(calls=len(rows),total_ms=round(sum(values),3),average_ms=round(mean(values),3),
                          maximum_ms=values[-1],p50_ms=values[len(values)//2],p95_ms=values[min(len(values)-1,int(len(values)*.95))],
                          input_tokens=sum(r.get('input_tokens') or 0 for r in rows),output_tokens=sum(r.get('output_tokens') or 0 for r in rows),
                          first_chunk_ms=[r['ttft_ms'] for r in rows if r.get('ttft_ms') is not None])
    model_total=sum(float(c['latency_ms']) for c in calls)
    output=sum(r.get('output_tokens') or 0 for r in calls)
    return dict(end_to_end_ms=round(total_ms,3),model_calls=len(calls),model_total_ms=round(model_total,3),
                non_model_wall_ms=round(max(0,total_ms-model_total),3),stages=stages,
                slowest_stage=max(stages,key=lambda n:stages[n]['total_ms']) if stages else None,
                output_tokens_per_second=round(output/(model_total/1000),3) if model_total and output else None,
                tool_calls=len(tools),external_requests=external_requests,
                tool_service_sum_ms=round(sum(t.get('elapsed_ms',0) for t in tools),3),
                tools=[{k:t.get(k) for k in ('name','status','cache_hit','elapsed_ms')} for t in tools],
                note='工具耗时为服务耗时之和，可能并行或含内部日期查询，不可直接相加为总等待时间；模型合计按串行主Agent/信息获取链路统计。首块延迟非严格首token延迟。')
