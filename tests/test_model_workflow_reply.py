from copy import deepcopy

import pytest
from test_natural_reply_regressions import logged_result
from test_preflight_workflow import runtime


def facts(result=None):
    from utils.workflow_reply import build_reply_facts
    return build_reply_facts(result or logged_result())


def complete_template(bundle):
    return ('这趟可以先这样安排：'
            + '。'.join('{{' + key + '}}' for key in bundle['required']) + '。')


def test_model_template_keeps_facts_and_does_not_expose_provenance():
    from utils.workflow_reply import render_reply_template
    result = logged_result()
    before = deepcopy(result)
    bundle = facts(result)
    text = render_reply_template(complete_template(bundle), bundle)
    for value in ['D6', 'D9', '338元', '317元', '655元', '2026年10月22日 08:00', '驿小栈']:
        assert value in text
    assert '房价' in text and '空房' in text
    assert '来源' not in text and 'http' not in text and '{{' not in text
    assert result == before


def test_final_reply_preserves_model_paragraphs_through_display_boundary():
    from utils.response_renderer import finalize_business_result
    from utils.workflow_reply import render_reply_template
    result = logged_result()
    bundle = facts(result)
    template = complete_template(bundle).replace('}}。{{', '}}。\n\n{{', 1)
    text = render_reply_template(template, bundle)
    assert '\n\n' in text
    result.update(final_answer=text, reply_style='model')
    assert finalize_business_result(result)['final_answer'] == text


@pytest.mark.parametrize('extra', ['G9999明早发车。', '票价199元。', '已预订成功。',
                                  'https://example.com。', '明天十点出发。', '免费车票。'])
def test_model_cannot_append_unverified_train_fare_time_or_booking(extra):
    from utils.workflow_reply import render_reply_template
    bundle = facts()
    with pytest.raises(ValueError):
        render_reply_template(complete_template(bundle) + extra, bundle)


def test_missing_or_repeated_facts_and_unknown_placeholders_are_rejected():
    from utils.workflow_reply import render_reply_template
    bundle = facts()
    template = complete_template(bundle)
    first = '{{' + bundle['required'][0] + '}}'
    for bad in [template.replace(first, ''), template + first, template + '{{made_up}}']:
        with pytest.raises(ValueError):
            render_reply_template(bad, bundle)


def test_failed_train_uses_planned_dates_and_one_concise_actionable_limitation():
    from utils.workflow_reply import fallback_workflow_reply
    result = logged_result()
    result['status'] = 'partial'
    result['validated_plan']['reconstructed_tasks'][0]['train'] = None
    result['validated_plan']['reconstructed_tasks'][0]['schedule'].update(
        departure_at=None, arrival_at=None)
    result['gaps'].extend([
        {'code': 'train_selection_missing', 'message': '当前段尚无已核实的train候选选择。'},
        {'code': 'invalid_draft', 'message': '草稿包含不支持的字段。'}])
    text = fallback_workflow_reply(result)
    assert '没查到' in text and '售票' in text
    assert '2026年10月21日' in text and '驿小栈' in text
    for bad in ['D6', '338元', '这趟行程可以这样安排', '草稿', '非用户确认',
                '目前尚无可核实', '全程费用尚未完整核实', '本轮仅完成']:
        assert bad not in text
    assert text.count('房价') == 1






def test_quoted_hotel_dates_appear_once_without_losing_booking_conditions():
    result = logged_result()
    plan = result['validated_plan']['reconstructed_tasks'][0]
    plan['task_id'] = 'outgoing'
    result['workflow']['tasks'] = [dict(id='outgoing', requires_hotel=True,
        conditions={'nights': 2}, field_sources={'nights': 'user'})]
    plan['hotel'] = dict(kind='hotel_quote', hotel_name='测试酒店', room_type='双床房',
        check_in='2026-10-19', check_out='2026-10-21', guests=2,
        stay_total_cny='800', fees_included=True, availability='available',
        cancellation='提前一天可取消')
    text = facts(result)['fallback']
    assert text.count('2026年10月19日入住') == 1
    assert text.count('2026年10月21日离店') == 1
    for value in ['住2晚', '双床房', '2人入住', '800元', '含费用', '有空房', '提前一天可取消']:
        assert value in text




@pytest.mark.asyncio
async def test_pending_origin_question_does_not_call_reply_model(tmp_path):
    r = runtime(tmp_path, [dict(origin=None, destination='上海', requires_hotel=True,
                               conditions={'nights': 2})])
    calls = []
    async def compose(context):
        calls.append(context)
        return {'template': '不应调用'}
    r.main.compose_workflow_reply = compose
    result = await r.turn('去上海玩')
    assert result['status'] == 'needs_input' and calls == []
    assert result['final_answer'] == '你准备从哪个城市出发？'
    assert not r.train.calls and not r.hotel.calls
