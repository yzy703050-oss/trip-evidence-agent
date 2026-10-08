# Aligo 差旅出行助手（V0）

Aligo 的 V0 是一个基于 AgentScope 的命令行多 Agent 差旅助手。用户用自然语言提出行程、政策或历史偏好问题；意图识别 Agent 生成任务计划，调度器按优先级执行所需子 Agent，再汇总回复。本分支还加入了可恢复的会话记忆和 EDD（评测驱动开发）基线。

本仓库只包含 **V0 命令行版本**。火车车次、酒店价格和天气尚未接入行程规划的实时核验，规划结果中的这些信息目前仅供参考。

## 它怎样工作

```text
用户输入
  ↓
IntentionAgent → agent_schedule（Agent 名称、优先级、任务原因）
  ↓
OrchestrationAgent → 按优先级分批调度
  ├─ 同一批：并发调用所需子 Agent
  └─ 下一批：接收前面批次的结果
  ↓
汇总结果 → CLI 展示
```

- **意图识别**：模型判断本轮需要哪些能力，产出 `agent_schedule`；它不直接执行子 Agent。
- **调度**：`agents/orchestration_agent.py` 用程序逻辑排序、并发执行同优先级任务，并为每个子 Agent 构造输入 `Msg`。调度决策依赖模型生成的计划，执行顺序由代码控制。
- **子 Agent**：`agents/lazy_agent_registry.py` 扫描 `.claude/skills/*/script/agent.py`，首次用到时创建实例，之后在当前 CLI 进程内缓存。Skill 目录同时包含说明和可运行脚本；运行脚本的是 Python，模型不会自行读取并执行其中的代码。
- **结果传递**：同一优先级的 Agent 完成后，结果进入后续批次的 `previous_results`。当前实现会传递此前批次的结果；业务变复杂时可按依赖关系收窄输入。

现有能力包括行程规划、偏好提取、事件收集、知识库问答、信息查询和历史记忆查询。具体会调用哪几个，由当轮 `agent_schedule` 决定。AgentScope 在这里提供 `AgentBase`、`Msg` 和模型封装；计划解释、Skill 加载、调度和记忆由项目代码实现。

## 会话与记忆

启动时输入 `user_id`；可选择已有 `session_id` 恢复会话，或回车新建。两者分别标识用户与会话；`turn_id`、事件序号和 `tool_call_id` 只用于内部关联。

```text
data/memory/{user_id}/
├─ profile.json                 # 可查看、可手动修改的用户偏好
├─ trips.json                   # 历史行程
└─ sessions/{session_id}/
   ├─ events.jsonl             # 按发生顺序追加的会话事件
   ├─ state.json               # 压缩摘要与覆盖边界
   └─ runs.jsonl               # 调度、耗时、调用量和 token 记录
```

`events.jsonl` 可记录用户消息、助手回复，以及以后模型直接调用工具时的调用和结果。当前 V0 的子 Agent 调用属于**调度执行阶段**，不会伪装成模型工具调用。会话变长时，程序只在已完成轮次或阶段的安全边界选择压缩范围；不会切在工具调用与结果之间。摘要供模型读取，原始事件仍保留。跨会话查询使用用户画像及相关历史摘要。

用户可在 CLI 中执行 `preferences` 查看画像，执行 `preferences set hotel_brands '["全季"]'` 修改偏好，或用 `preferences delete hotel_brands` 删除。手动设置优先于 Agent 自动提取。

## 本地运行 V0

需要 Python 3.11，以及支持 OpenAI 兼容接口的模型服务。在**仓库根目录**执行；Skill 注册器使用相对路径发现子 Agent。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

在本地 `.env` 至少配置 `LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL`。CLI 会调用真实模型。不要提交 `.env`。

```powershell
python cli.py
```

按提示输入用户 ID，随后输入自然语言问题。输入 `help` 可查看 `status`、`history`、`preferences`、`health` 和 `exit` 等命令。单独检查模型连通性可运行 `python cli.py health`。

### 启用 V0 知识库问答

虚构企业的演示政策文档位于 `.claude/skills/ask-question/data/documents/`，不代表真实企业规定。V0 使用本地 `BAAI/bge-small-zh-v1.5` 嵌入模型和 Milvus Lite；模型文件与生成的向量库不在 Git 中。将模型放在 `data/models/bge-small-zh-v1.5/`，或让首次运行按配置下载。准备好资源后，在仓库根目录初始化：

```powershell
python .claude/skills/ask-question/script/init_knowledge_base.py
```

初始化脚本会重建目标 collection，请勿将它用于需要保留原有内容的知识库。Windows 上若项目路径含中文，Faiss 可能无法在该路径写索引；可在初始化和运行 CLI **之前**设置纯英文目录，例如 `$env:V0_RAG_DB_DIR = 'C:\aligo-rag'`。该变量只改变向量数据库位置。

## 测试与真实测评

不调用模型的回归测试：

```powershell
python -m pytest -q tests --ignore=tests/test_intention_agent.py
```

旧的 `test_intention_agent.py` 会直接请求模型，所以离线运行时排除。离线测试结果以本仓库当前测试运行输出为准。

真实 EDD 会检查计划与实际 Agent 调用是否一致、执行是否成功、答案关键事实、RAG 来源与依据、跨会话回忆、行程约束，并记录延迟、模型调用量及 token。先用 CLI 完成相应会话，再用对应 case 对已保存的日志评分：

```powershell
python -m evals.v0_memory.runner --cases evals/v0_memory/cases.json --user-id USER_ID --session-id SESSION_ID
```

`evals/v0_memory/` 另有 `trip_case.json` 和 `memory_case.json`。它们分别检查行程与跨会话偏好；评测器读取既有会话日志，不会替你重新发起模型请求。

### 2026-10-08 真实测评

| 场景 | 结果 | 模型调用 | 耗时 |
| --- | --- | ---: | ---: |
| 北京住宿政策问答 | 通过：找到政策依据，回答 500 元/晚 | 2 次 | 12.80 秒 |
| 上海至北京三日行程 | 预算内有估算组合（最低 2520 元）；**严格上限保证未通过**，最贵组合为 3080 元 | 4 次 | 43.29 秒 |
| 跨会话酒店偏好 | 通过：从用户画像恢复“全季” | 3 次 | 10.69 秒 |

三例合计 9 次模型调用、12,215 输入 token 和 13,852 输出 token。记录的成本字段目前为未知：模型返回的用量不足以还原实际账单中的缓存命中和时段价格。详细过程、修复及评测边界见 [V0 真实测评报告](docs/evals/2026-10-08-v0-live-evaluation.md)。一条 RAG 用例不足以代表整体检索准确率；行程中的车次、价格等也未接入实时核验。

## 代码入口

| 位置 | 职责 |
| --- | --- |
| `cli.py` | 命令行入口、用户交互与一轮请求 |
| `agents/intention_agent.py` | 意图识别与 `agent_schedule` |
| `agents/orchestration_agent.py` | 分批执行、结果传递与聚合 |
| `agents/lazy_agent_registry.py` | 发现、实例化和缓存 Skill 子 Agent |
| `.claude/skills/*/script/agent.py` | 各专业子 Agent 的 Python 实现 |
| `context/` | 会话事件、用户画像、压缩与遥测 |
| `evals/v0_memory/` | V0 EDD 用例与评分器 |

记忆方案见 [设计说明](docs/design/v0-memory-redesign.md)。本仓库尚未附带开源许可证。

