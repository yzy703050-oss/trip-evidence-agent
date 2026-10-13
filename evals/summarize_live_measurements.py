"""Summarize recorded usage without calling a model or rewriting historical reports."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "docs/evals"
API_RUN = EVALS / "2026-10-13-api-shaped-simulation"
CAPABILITY_RUN = EVALS / "2026-10-13-api-shaped-capability-fixed"
OUTPUT = EVALS / "2026-10-13-api-shaped-summary"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    sources = []
    for path in sorted(EVALS.glob("2026-10-13-*/summary.json")):
        data = read(path)
        if "runs" not in data or not data.get("model"):
            continue
        sources.append(
            {
                "directory": path.parent.name,
                "model_calls": sum(r["model_calls"] for r in data["runs"]),
                "input_tokens": sum(r["input_tokens"] for r in data["runs"]),
                "output_tokens": sum(r["output_tokens"] for r in data["runs"]),
                "total_tokens": sum(r["total_tokens"] for r in data["runs"]),
                "runs": [
                    {
                        k: r.get(k)
                        for k in (
                            "case",
                            "status",
                            "stop_reason",
                            "latency_ms",
                            "model_calls",
                            "total_tokens",
                        )
                    }
                    for r in data["runs"]
                ],
            }
        )
    cases = [
        (API_RUN, "tourism_simulated", "普通旅游：聚合/高德原始响应"),
        (API_RUN, "business_simulated", "普通出差：聚合原始响应＋独立报价夹具"),
        (API_RUN, "business_conflict", "政策冲突：聚合原始响应＋超标报价重查"),
        (CAPABILITY_RUN, "business_api_simulated", "报价能力缺口：聚合/高德原始响应"),
    ]
    selected = []
    lines = [
        "# API形状一致的模拟验收与消耗记录",
        "",
        "真实模型为项目配置的 deepseek-flash；政策通过真实本地向量RAG检索虚构制度，"
        "embedding在本地运行。供应商HTTP全部模拟，没有重试真实聚合/高德网络接口。",
        "",
        "火车注入聚合error_code/result/prices原始响应，复用生产价格、余票与时长解析。"
        "高德注入status/infocode/pois原始响应，复用POI解析、去重及坏坐标排除；"
        "business.cost不是房价，住宿价格、房型、库存保持未知。"
        "普通出差及冲突场景的酒店使用独立HotelOffer报价契约夹具，不能解释为高德房价。",
        "",
        "| 场景 | 模型调用 | 输入token | 输出token | 合计 | 请求延时 | 结果 | 验收 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for directory, case, label in cases:
        report = read(directory / case / "report.json")
        responses = read(directory / case / "model-responses.json")
        report["record_directory"] = str((directory / case).relative_to(ROOT))
        selected.append(report)
        accepted = "通过" if report["expected_behavior_success"] else "未通过"
        lines.append(
            f"| {label} | {report['model_calls']} | {report['input_tokens']:,} | "
            f"{report['output_tokens']:,} | {report['total_tokens']:,} | "
            f"{report['latency_ms'] / 1000:.3f}s | {report['status']} | {accepted} |"
        )
        report["call_timings"] = [
            {k: r.get(k) for k in ("stage", "latency_ms", "ttft_ms", "status", "usage")}
            for r in responses
        ]
    measured = sum(r["total_tokens"] for r in selected)
    lines.extend(
        [
            "",
            f"以上四个验收记录共 **{measured:,} token / "
            f"{sum(r['model_calls'] for r in selected)} 次模型调用**。",
            "旅游为主规划＋批量选择；普通出差多一次政策提取；冲突多一次酒店重查决定。"
            "两段火车均只查一次，超标酒店由700/760元每晚刷新为380/420元每晚，"
            "按当前500元上限验证完成；旧900元制度和无审批800元例外没有放宽额度。",
            "",
            "`partial` 的高德场景表示任务未完成，但缺报价被如实保留且验收符合预期。"
            "程序完整保存/校验后按已知能力缺口结束；不再依赖模型填写allow_repair=false。"
            "混合火车、时间、未知政策冲突仍走原有限修复。",
            "",
            "请求延时从进入WorkflowRunner到返回计算，包含模型/检索/工具执行，"
            "不包含RAG首次初始化及入库；每次模型的TTFT/耗时/usage见原始记录。"
            "这是每场景单次测量，不能据此给出稳定性或延时分位数保证。",
            "",
            "## 未通过轮次与全部消耗",
            "",
            "第一次纯高德模拟正确返回partial，但5调用24,449 token后no_progress，验收未通过。"
            "该记录原样保留；定向修复后只重测此场景，没有重跑已通过的其他三场景。"
            "普通出差旧报告曾将调用数未达标计入task_success；新报告独立记录"
            "task_success与optimization_success，不回写历史记录。",
            "",
            "| 记录目录 | 模型调用 | 输入token | 输出token | 合计 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for source in sources:
        lines.append(
            f"| [{source['directory']}](../{source['directory']}/summary.json) | "
            f"{source['model_calls']} | {source['input_tokens']:,} | "
            f"{source['output_tokens']:,} | {source['total_tokens']:,} |"
        )
    overall = sum(r["total_tokens"] for r in sources)
    new_round = sum(r["total_tokens"] for r in sources if "api-shaped" in r["directory"])
    lines.extend(
        [
            "",
            f"本次API形状模拟（含失败案例及定向重测）实际共 **{new_round:,} token**。"
            f"全部已保存的十轮测量共 **{overall:,} token / "
            f"{sum(r['model_calls'] for r in sources)} 次调用**，包含失败和修复轮次，"
            "不能只报最终通过的46,029 token，也不能把累计调试量当成单次行程消耗。",
            "这是SDK报告的输入＋输出使用量；未据此计算费用，缓存折扣/价格及账单额度未核对。",
            "",
            "## 逐次记录及模拟数据",
            "",
        ]
    )
    for directory, case, label in cases:
        base = f"../{directory.name}/{case}"
        lines.append(
            f"- {label}：[原始响应及生产解析结果]({base}/supplier-fixtures.json)、"
            f"[模型请求与输出/TTFT]({base}/model-responses.json)、"
            f"[token/延时/验收]({base}/report.json)、[程序校验]({base}/validation.json)。"
        )
    lines.extend(
        [
            "",
            "## 验证",
            "",
            "TDD复现原始响应模拟缺失、到达查询工具绑定和能力缺口重复调用后修复；"
            "相关回归实际执行。全量测试、Ruff及规格核对证据见"
            "[自审记录](../../superpowers/reviews/2026-10-13-live-model-contracts-verification.md)。",
        ]
    )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUTPUT / "measurements.json").write_text(
        json.dumps(
            {
                "selected_runs": selected,
                "all_measurement_iterations": sources,
                "selected_tokens": measured,
                "api_shaped_iteration_tokens": new_round,
                "all_iteration_tokens": overall,
                "all_iteration_calls": sum(r["model_calls"] for r in sources),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "selected_tokens": measured,
                "api_shaped_tokens": new_round,
                "all_iteration_tokens": overall,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
