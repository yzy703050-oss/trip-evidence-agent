# V0 子 Agent 数据能力扩展设计

## 目标与边界

让 V0 在规划行程时区分“已查询到的报价与事实”和“模型生成的安排建议”。火车票、酒店与旅游攻略由独立子 Agent 查询；行程规划 Agent 消费它们的结构化结果，不再自行编造车次、房价、余票、开放时间或预约规则。用户关心的是可靠数据和价格，下单流程不在本次范围内；有可用链接时附上来源或查看入口。

本设计以已发布的 V0 命令行仓库为基线。现有用户记忆、政策 RAG、事项收集与 CLI 继续使用。当前尚未提供数据平台名称和接口权限，因此设计固定内部契约，不预设某家平台的参数、价格字段或授权范围。接入具体平台前，须核对该账号实际开通的查询接口。

## 现状与职责调整

| 组件 | 调整后的职责 |
| --- | --- |
| `IntentionAgent` | 识别用户要查询火车、酒店、攻略或组合行程，提取意图并建议调度；不产出价格或事实。 |
| `EventCollectionAgent` | 提取出发地、目的地、出行日期、入住与离店日期、人数等查询条件；无法确定的条件标为缺失，不将画像推断当作用户确认。 |
| `TrainSearchAgent`（新增） | 通过获准使用的数据接口查询车次、席别、票价与可订状态，返回原始来源和查询时间。 |
| `HotelSearchAgent`（新增） | 按城市、日期、人数和偏好查询酒店房型、含税费价格、库存与取消规则，返回来源和查询时间。 |
| `TravelGuideAgent`（新增） | 查询景点、地点间路线、对应日期的天气，以及开放与预约信息；逐项保留依据。 |
| `ItineraryPlanningAgent` | 组合已查询的候选项，安排每日活动、取舍与说明；不新增未经来源验证的价格、车次、房源或开放规则。 |
| `InformationQueryAgent` | 保留独立天气问答与通用搜索；不把网页摘要当作票务或酒店可订报价，攻略场景交由攻略 Agent。 |
| `RAGKnowledgeAgent` | 仅回答演示政策库问题，不作为外部实时信息来源。 |

原 `plan-trip/SKILL.md` 中允许按季节推测天气、按常识补景点与预算的规则需收窄：可以给出标为建议的活动框架，但实际天气、营业、预约和费用必须来自对应查询结果。旧的 `estimated_budget` 字符串兼容显示，新增结果使用结构化费用与缺项字段。

## 数据契约与来源状态

三个新 Agent 的输出都采用同一外层格式：`status`（`ok`、`partial`、`needs_input`、`unavailable`、`error`）、`query`、`items`、`missing_fields`、`source`、`fetched_at`。`source` 至少记录平台名称和可访问的来源 URL（若平台提供）；时间使用带时区的 ISO 8601。每条候选项或事实可另附自己的来源和时间，避免多个数据源混用时丢失依据。

- 火车候选项：出发/到达站、日期与时间、车次、席别、人民币票价、可订状态、可用数量（仅接口确实提供时）、查看链接。一个席别对应一个报价；无票价或库存字段时保持 `null`，不得用模型补齐。
- 酒店候选项：酒店及房型标识、入住/离店日期、入住人数、每晚价与总价（明确币种、税费是否计入）、可订状态、取消规则、查看链接。价格条件不完整时不得标为“可订”。
- 攻略事实：景点名称与位置、路线方式与耗时、对应日期的天气、开放时间和预约规则。每一类事实分别记录来源。搜索结果摘要只能帮助发现候选景点；开放与预约规则须能追溯到景区官方页面或明确标注为“待核实”。

价格与库存只在本次授权接口成功返回时称为“查询到的当前报价”，并显示查询时间；系统不承诺后续下单时价格和库存保持不变。超时、限流、认证失败、数据缺项、日期不在查询范围等情况使用明确状态和原因，保留其他成功的 Agent 结果。不得以历史报价、网页片段或模型估计静默替代实时报价。

## 调度与数据流

```text
用户请求
  → 意图识别
  → 事项收集与偏好/记忆读取
  → 按需要并行查询火车、酒店、攻略
  → 行程规划与确定性费用计算
  → CLI 展示候选项、来源、查询时间、缺项
```

调度器根据 Agent 的依赖关系修正模型给出的优先级：需要行程条件的查询必须在事项收集之后；组合行程规划必须在已请求的查询结束之后。查询火车或酒店本身不强制生成完整行程。只请求一般天气时仍可走 `InformationQueryAgent`。同批次的火车、酒店和攻略查询可以并行，失败不阻断其他结果。

可查询性由程序校验。火车至少需要起终点与出发日期；酒店至少需要城市、入住/离店日期和人数；攻略至少需要目的地，日期缺失时只给不依赖日期的地点候选，不声称当天的天气、开放或预约状态。用户画像可用于排序和解释推荐，但未确认的画像地点不能自动成为对外查询的确定条件。

费用由程序计算，而非让模型写一个总价字符串：选定的火车票价乘人数，加选定酒店房价的有效入住总价，再加有来源的其他明确费用。输出已知费用小计、币种、计算项和未报价类别。缺少关键费用时，不展示“全程总预算已核实”或“预算内保证”；预算上限判断只基于完整可比较的费用组合。用户仍可获得路线与活动建议，并看到哪些价格尚未查到。

## 外部数据接入

每个领域 Agent 只依赖自己的 Provider 接口，平台鉴权、请求和响应转换留在适配层。凭据从本地环境变量读取，不写入仓库或日志。首个生产适配器依据用户提供的平台账号及已开通接口确定。没有可用权限时，Agent 返回 `unavailable`，不放演示报价到正式回复。

攻略数据可分层接入：地点与市内路线使用地图服务，天气使用天气服务，开放和预约优先核对景区官方信息。高德开放平台有 [POI 搜索](https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch)和[路径规划 API](https://lbs.amap.com/api/webservice/guide/api/newroute)；和风天气提供需申请凭据的[天气 API](https://dev.qweather.com/docs/start/)。这些是可评估的工具来源，并非已经接入的能力。酒店平台的动态价格与库存接口通常需要分销权限；例如[同程旅仓酒店实时详情文档](https://open.lvcang.cn/doc/api/hotel/apiList/detail.html)明确描述动态库存、价格与账号请求头。12306 提供[官网余票查询](https://mobile.12306.cn/weixin/wxcore/init)，但本设计不把网页端请求当作获准的开放 API；火车 Provider 需基于账号实际授权能力落地。

## 分阶段交付

1. **数据契约与调度边界**：完成统一状态、来源和价格模型，调整意图与调度，禁止无来源价格进入最终回复；用可控的假 Provider 测试完整路径。这里的假数据只在自动测试中使用。
2. **火车与酒店真实数据**：根据平台账号接入获准接口，验证身份、字段、价格口径、限流和失败行为；交付可查看的真实候选项及已知费用小计。
3. **旅游攻略工具链**：接入地点、路线、天气和开放/预约来源，输出按日期关联且逐项可追溯的攻略，再接入组合行程。

每一阶段都可独立验收。阶段 2 需先得到平台名称、接口文档或权限清单；阶段 3 的开放与预约事实若没有可核对来源，必须保持待核实状态。

## 验收场景

- 用户问“下周上海到北京高铁还有票吗、多少钱”：有授权响应时列出车次、席别、价格、可订状态、来源与查询时间；无权限或接口失败时明确说明，不能给估算票价。
- 用户问“北京住两晚，预算 600 元”：明确入住与离店日期、人数和总价口径；返回可订房型与取消规则。条件缺失时先标注需补充的字段。
- 用户要“三天北京攻略”：给出景点、相邻地点路线、对应日期天气及开放预约信息；每条时间敏感事实有来源，无法确认的标为待核实。
- 用户要包含火车、住宿和游玩的完整行程：查询 Agent 先执行，规划 Agent 只消费已返回的候选项；预算展示已知小计和未报价类别。
- 某个平台超时或返回空结果：其他 Agent 的成功结果仍展示，最终状态为部分完成，不把失败当作“无票”或“无房”。

测试以 Provider 假实现覆盖调度、字段校验、价格计算、来源展示和失败降级；真实接口用单独的受凭据保护的联调检查，不在 CI 中调用。现有离线回归测试与 V0 记忆评测继续运行，并增加“无来源不得报实时价格”的 EDD 断言。

## Confirmed field and completion semantics (final review, 2026-10-09)

- `passengers` is the confirmed train passenger count; `guests` is the confirmed hotel occupant count. Collector emits both for an explicitly shared party size when both domains are requested. Explicit “two train passengers, one hotel guest” stays separate. Hotel-only guest counts never become train passengers. Unknown train passengers retain the existing TrainQuery default of one; unknown hotel guests remain missing. Budget uses the actual TrainQuery passenger count.
- Guide `start_date..end_date` means calendar itinerary dates including both endpoints. A confirmed start_date alone means a single day; unknown dates yield an empty visit_dates list. Explicit visit_dates takes precedence. Hotel check_out remains exclusive and later than check_in; itinerary dates never silently become hotel dates.
- Collector asks for hotel guests/check_in/check_out only when this request includes hotel search. Train-only and guide-only queries do not require hotel conditions.
- Domain `ok` maps to execution `success`; `partial`, `needs_input`, `unavailable`, and `error` remain explicit execution states. Mixed successful and incomplete domain results aggregate as `partial_failure`, preserving every domain result. When all requested domains need input or are unavailable, the aggregate retains that uniform state. Errors aggregate as `partial_failure` for compatibility. EDD execution_success requires every requested domain to be `ok`, even for old records wrapped as success.
- Dependency batches use compact integer ranks rather than arithmetic on model priorities. Nonfinite priorities use the default rank. Collector precedes a common domain batch, which precedes planner, including extreme finite model values.
