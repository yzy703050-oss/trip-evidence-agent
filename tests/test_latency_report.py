from evals.latency import summarize_latency
import pytest


def test_latency_distinguishes_parallel_service_time_from_wall_time():
    calls=[dict(stage='main:plan',latency_ms=1000,input_tokens=100,output_tokens=50,ttft_ms=200),
           dict(stage='main:step',latency_ms=2000,input_tokens=200,output_tokens=100),
           dict(stage='agent:information_query',latency_ms=3000,input_tokens=300,output_tokens=150)]
    tools=[dict(name='train_search',elapsed_ms=100,cache_hit=False),dict(name='hotel_search',elapsed_ms=100,cache_hit=False)]
    report=summarize_latency(calls,tools,total_ms=6250,external_requests=2)
    assert report['model_total_ms']==6000 and report['end_to_end_ms']==6250
    assert report['slowest_stage']=='agent:information_query'
    assert report['stages']['main:plan']['average_ms']==1000
    assert report['tool_service_sum_ms']==200
    assert report['non_model_wall_ms']==250
    assert report['output_tokens_per_second']==50


async def test_stream_latency_records_first_chunk_and_total_response_time():
    from types import SimpleNamespace
    from context.telemetry import MeteredModel
    rows=[]
    async def model(messages):
        async def chunks():
            yield SimpleNamespace(content='a',usage=None)
            yield SimpleNamespace(content='ab',usage={'input_tokens':10,'output_tokens':2})
        return chunks()
    response=await MeteredModel(model,rows.append)([])
    async for chunk in response: pass
    assert 0<=rows[0]['ttft_ms']<=rows[0]['latency_ms']
    assert rows[0]['output_tokens']==2


@pytest.mark.parametrize('brands,keywords,expected',[
    ('全季','全季',True), (['全季'],'全季',True),
    ('全季','汉庭',False), (['全季'],'汉庭',False), ('汉庭','全季',False),
])
def test_live_assessment_checks_preference_value_and_actual_query(monkeypatch,brands,keywords,expected):
    from types import SimpleNamespace
    from evals.run_preflight_simulated import assess
    monkeypatch.setattr('agents.workflow_guard.check_workflow',lambda w: {'issues':[]})
    result={'status':'partial','data_mode':'simulation','workflow':{
        'tasks':[{'draft_plan':{'hotel_selection':{}}}], 'checkpoint':None,
        'results_by_query':{'hotel-query':{'domain':'hotel','parameters':{'keywords':keywords}}},
    }}
    run=SimpleNamespace(effective_preferences={'hotel_brands':brands})
    assert assess('preference_plan',result,run)['checks']['preference_updated'] is expected
