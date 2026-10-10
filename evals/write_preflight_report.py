"""Consolidate recorded results without repeating model or provider calls."""
import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

from evals.preflight_acceptance import evidence
from evals.run_preflight_simulated import ROOT,assess


def write_report():
    base=ROOT/'data/evals/2026-10-10-preflight-final'
    retest=ROOT/'data/evals/2026-10-10-preflight-feedback-retest'
    originals=json.loads((base/'report.json').read_text(encoding='utf-8'))
    retried={r['case']:r for r in json.loads((retest/'report.json').read_text(encoding='utf-8'))}
    records=[]
    for old in originals:
        r=retried.get(old['case'],old)
        path=(retest if r['case'] in retried else base)/(r['case']+'.json')
        saved=json.loads(path.read_text(encoding='utf-8')); result=saved['result']
        run=SimpleNamespace(external_request_count=r['latency']['external_requests'],tool_requests=r['latency']['tools'],effective_preferences={})
        if r['case']=='preference_plan': run.effective_preferences=result.get('workflow',{}).get('effective_preferences',{})
        prior=None
        if r['case']=='hotel_replace': prior=json.loads((base/'default_date.json').read_text(encoding='utf-8'))['result']
        if r['case']=='supplement_origin': prior=json.loads((base/'unknown_origin.json').read_text(encoding='utf-8'))['result']
        records.append({**r,'original_assessment':r['assessment'],'assessment':assess(r['case'],result,run,prior),'result_path':str(path)})
    coverage=evidence(ROOT/'data/evals/preflight-final-tests.xml')
    tests=ET.parse(ROOT/'data/evals/preflight-final-tests.xml').getroot().findall('.//testcase')
    passed_tests=sum(all(node.find(tag) is None for tag in ('failure','error','skipped')) for node in tests)
    skipped_tests=sum(node.find('skipped') is not None for node in tests)
    combined=dict(source_mode='real_llm_simulated_travel',records=records,offline_scenarios=coverage,
                  passed=all(r['assessment']['passed'] for r in records) and all(s['passed'] for s in coverage.values()),
                  criteria_note='未指定住宿时长的请求允许有来源的partial草稿，不要求模型编造时长或completed；既有偏好契约允许字符串或列表，并核验实际酒店查询关键词。原始状态、原始判断和失败记录均保留。')
    destination=ROOT/'data/evals/preflight-consolidated.json'
    destination.write_text(json.dumps(combined,ensure_ascii=False,indent=2),encoding='utf-8')
    with (ROOT/'data/evals/preflight-latency.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f); writer.writerow(['case','status','end_to_end_s','model_calls','main_plan_s','main_step_s','information_query_s','tool_service_ms','external_requests'])
        for r in records:
            timing=r['latency']; stages=timing['stages']
            writer.writerow([r['case'],r['status'],timing['end_to_end_ms']/1000,timing['model_calls'],
                             *[stages.get(s,{}).get('total_ms',0)/1000 for s in ('main:plan','main:step','agent:information_query')],
                             timing['tool_service_sum_ms'],timing['external_requests']])
    lines=['# 规划补全与真实模型耗时测评（2026-10-10）','',
           '模型为当前配置的 deepseek-flash；主流程使用供应商默认思考模式。火车、酒店使用显式注入的 simulation Provider，车次、价格、余票、酒店名称和地址均为测试数据。生产入口没有模拟回退。',
           '',f"最终默认配置测评：{sum(r['assessment']['passed'] for r in records)}/{len(records)}；P01–P33可执行测试证据：{sum(s['passed'] for s in coverage.values())}/33。",'',
           f'完整自动化回归：{passed_tests}项通过，{skipped_tests}项旧在线脚本跳过；原有DashScope弃用告警1条。跳过的在线脚本不计入通过数。','',
           '部分方案不等于失败：缺出发地或未确定住宿时长时，验收要求已有候选和部分草稿、说明缺口、不强制追问；不能为了通过测评编造住宿天数。默认日期已核验，酒店价格和库存仍未知。',
           '偏好测评按既有存储契约接受字符串或列表；本次实际保存“全季”，实际酒店查询也使用“全季”。原断言仅接受列表而误报失败，修正后保留原判断与原响应。',
           '', '| 用例 | 结果 | 总耗时秒 | 模型调用 | 初始判断秒 | 循环判断秒 | 信息获取模型秒 | 模拟工具毫秒 |',
           '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    labels={'default_date':'缺日期、重庆→上海','unknown_origin':'缺出发地、去上海','arrival_overnight':'指定抵达日、跨夜列车','query_price':'只查火车价格','preference_plan':'更新偏好并规划',
            'multi_route':'上海→北京→杭州→上海','hotel_replace':'换上海酒店，保留火车','explain':'解释已有酒店','ambiguous':'不满意、询问修改范围','pause':'暂停并保存','supplement_origin':'补出发地、沿用日期'}
    for r in records:
        t=r['latency']; s=t['stages']
        lines.append(f"| {labels[r['case']]} | {r['status']} | {t['end_to_end_ms']/1000:.1f} | {t['model_calls']} | {s.get('main:plan',{}).get('total_ms',0)/1000:.1f} | {s.get('main:step',{}).get('total_ms',0)/1000:.1f} | {s.get('agent:information_query',{}).get('total_ms',0)/1000:.1f} | {t['tool_service_sum_ms']:.2f} |")
    model_ms=sum(r['latency']['model_total_ms'] for r in records)
    wall_ms=sum(r['latency']['end_to_end_ms'] for r in records)
    steps=sum(r['latency']['stages'].get('main:step',{}).get('total_ms',0) for r in records)
    lines.extend(['',f'本批串行测评中模型累计耗时占端到端耗时 {model_ms/wall_ms:.1%}，其中主Agent循环判断占模型累计耗时 {steps/model_ms:.1%}。模拟工具为毫秒级，不能推断真实接口的网络延迟。',
        '', '瓶颈主要是多轮主Agent调用，以及默认高强度思考下的长输出。DeepSeek官方说明默认开启思考、默认high，输出token可能包含思考内容：[思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)。首响应块时间不是严格首token时间，也不是完整JSON交付时间。',
        '', '主Agent并非只调一次：意图识别后，每段通常还要分派、草稿、校验，再结束；信息获取通常是工具选择和结果小结两次模型调用。各工具可并行，工具服务耗时之和不能直接当作总等待时间。原始模型调用日志保留input/output tokens、latency_ms、ttft_ms及task身份，CSV列出汇总。',
        '', '格式修复、上下文与路由修正：一致的重复路线元数据在程序边界投影，矛盾仍拒绝；未知来源别名仅在未知值时归一化；上下文去除重复候选全文；截断初始JSON最多修复一次，DeepSeek修复调用关闭思考，正常调用仍使用原配置；明确travel_update必须进入执行流程，避免只口头暂停或继续索要个人字段。',
        '', '第一轮实测曾出现结构错误和截断，并保存在 `data/evals/2026-10-10-preflight-simulated/` 与 `data/evals/2026-10-10-preflight-final/`。换酒店、暂停、补出发地的修复后结果在 `data/evals/2026-10-10-preflight-feedback-retest/`。其余用例保留原响应，由最终规格逐项重新核对；没有重写原始结果。'])
    comparison=json.loads((ROOT/'data/evals/2026-10-10-preflight-thinking-disabled/report.json').read_text(encoding='utf-8'))
    lines.extend(['','关闭思考模式的两条对照（仅测评开关，未修改生产默认）：'])
    for r in comparison:
        normal=next(row for row in records if row['case']==r['case'])
        lines.append(f"- {labels[r['case']]}：默认 {normal['latency']['end_to_end_ms']/1000:.1f}s，关闭思考 {r['latency']['end_to_end_ms']/1000:.1f}s，{r['status']}，验收{'通过' if r['assessment']['passed'] else '未通过'}。")
    lines.extend(['', '复杂三段对照未完成全程校验，说明减少思考并不保证同等规划质量；不把该实验算入默认配置验收通过率。这些都是单次观测，不是稳定的性能承诺。',
        '', '自审与验证：按用户选择由当前代理整体自审。修复了局部换酒店的其他组件保护、出发地补充保留酒店、下游推导日期重检、初始阶段整轮超时、近期旅行排序及同ID历史计数。RAG调用链未修改。项目未初始化OpenSpec，本次未运行verify/archive，采用下表人工规格核对及实际测试证据。',
        '', '| 场景 | 测试证据 | 结果 |','| --- | --- | --- |'])
    for scenario,row in coverage.items(): lines.append(f"| {scenario} | {'、'.join(row['tests'])} | {'通过' if row['passed'] else '缺证据/失败'} |")
    lines.extend(['','外部验证限制：本次没有把失效的火车服务修成可用，也未声称验证真实票价、余票或酒店报价。区间历时使用可控接口payload验证跨夜、跨年与超过24小时；换供应商后仍需真实样本核对中途站语义。酒店只推荐地点，不估价或订房。',
                  '', '原始证据：`data/evals/preflight-consolidated.json`、`data/evals/preflight-latency.csv`、`data/evals/preflight-final-tests.log` 与 `data/evals/preflight-final-tests.xml`。'])
    report=ROOT/'docs/evals/2026-10-10-planning-preflight-evaluation.md'
    report.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(dict(passed=combined['passed'],live_cases=len(records),scenarios_passed=sum(s['passed'] for s in coverage.values()),model_share=model_ms/wall_ms,main_step_share=steps/model_ms,report=str(report)),ensure_ascii=False))
    return combined['passed']


if __name__=='__main__': raise SystemExit(0 if write_report() else 1)
