## Purpose

立即将TripPlan、task及查询容器的持久化业务状态分别缩减为2、3、3个枚举，沿用程序状态机与现有断点、执行和查询记录表达缺信息、等待、失败原因及恢复条件，避免丢失必要情况或重复执行。

## ADDED Requirements

### Requirement: 三层持久化业务状态

本次变更 SHALL 立即采用TripPlan.status仅running/completed，task.status仅pending/draft/validated，query_container.status仅empty/ready/failed，拼写统一failed。running SHALL 表示当前工作方案未完成，不表示程序必须持续运行；completed SHALL 仅表示当前版本方案已通过全程校验并成功保存。pending表示任务需处理，draft表示已有查询或选择草稿待校验，validated表示当前任务版本已校验；draft_stage SHALL 继续区分query与selection。empty表示当前条件下尚无查询结果，ready表示已取得当前结果，failed表示实际查询失败。模型 MUST NOT 直接设置业务状态或版本。

#### Scenario: 查询与选择阶段
- **WHEN** 任务取得查询结果但尚未完成候选选择
- **THEN** task为draft且draft_stage=query；取得结果的容器为ready，不能据此将任务validated或旅程completed

#### Scenario: 全程校验完成
- **WHEN** 所有必要任务选择符合当前条件且程序全程校验通过并保存成功
- **THEN** 对应任务validated，TripPlan为completed；保存失败不确认完成

### Requirement: 派生等待与执行原因

needs_input SHALL 从missing_fields、问题和checkpoint派生，任务未有草稿时保持pending，已有待校验草稿可保持draft，工作旅程保持running，保存后结束本轮。用户补充后 SHALL 核对身份与版本并继续原断点，不因running而自动轮询或重放。是否在途、等待依赖与可重试 SHALL 从执行记录、depends_on及实际条件派生，不增加task.running或容器querying/blocked。HTTP/provider结果和会话返回的needs_input/partial/error等诊断状态可继续存在，但 MUST NOT 写入上述三层业务status。

#### Scenario: 缺出发地或修改范围
- **WHEN** 新工作方案缺出发地或无法确定修改范围
- **THEN** 保存问题/缺失字段与checkpoint并结束本轮，无供应商请求，不写顶层或task的needs_input

#### Scenario: 请求在途及恢复
- **WHEN** 同一task/条件版本的请求在途，或重启发现已开始但没有确定完成记录
- **THEN** 不因容器仍为empty/failed而重复派发；恢复核对已有结果与执行记录，无法确定结果时保留未完成原因，不自动重放已启动请求

#### Scenario: 暂停或取消
- **WHEN** 用户明确暂停/取消或读取对应旧记录
- **THEN** 保留stop_reason与必要checkpoint记录停止意图，顶层不新增枚举；活动列表和自动恢复不把停止记录当作可执行running，恢复仍受用户明确意图与版本约束

### Requirement: 容器失效与受控转移

查询成功（包括明确空列表或有缺口的可用结果）SHALL 进入ready，实际请求失败进入failed；ready MUST NOT 等同于有候选、报价齐全或方案合规。参数改变或按既有规则结果过期时 SHALL 将旧current_query_ref保留为history引用并解除当前绑定，容器回到empty，保存invalidation_reason；有匹配有效缓存时可核验后重新绑定为ready。请求在途标记 SHALL 在调用前记录，并按request_id/task_revision/条件归属校验返回；旧版本或已失效请求返回不得覆盖新条件容器。失败重试只在既有策略/预算允许或用户明确请求时执行，不无限循环或新增默认自动补查策略。

正常task SHALL 从pending到draft再到validated。已有充分证据的改选 SHALL 从validated到draft，保留ready容器，校验后回validated；缺少新条件证据时工作task回pending并仅补缺口。校验失败保留draft及问题，不绕过校验。未应用修改失败 SHALL 保留上一已保存有效方案；已保存的合法工作变更未完成时可为running。

#### Scenario: 改选已有酒店
- **WHEN** 同城市/日期/人数下换成另一个有效候选
- **THEN** 工作task按validated→draft→validated流转，查询容器仍ready，未受影响结果保持有效

#### Scenario: 日期改变导致旧报价失效
- **WHEN** 新入住区间没有匹配有效报价
- **THEN** 相关容器ready→empty，解除当前引用并保留历史；成功新查询后ready，失败则failed，不使用旧报价证明新条件

#### Scenario: 空结果与仅酒店地点
- **WHEN** 查询成功但没有候选，或地点接口没有报价/空房证据
- **THEN** 容器ready并保存真实缺口，程序不盲目重复查询，任务/全程校验仍反映未满足条件

### Requirement: 旧状态兼容与权威索引一致

程序 SHALL 非破坏性兼容旧数据：旧顶层running/needs_input/partial/error统一映射running并保留原原因和断点；paused/cancelled映射running且保留停止语义与自动恢复屏障；completed仅在当前选择/校验有效时保持，否则记录需重验。旧task.running/needs_input映射pending，旧pending/draft/validated保持对应语义与证据，失效validated须重新校验。若存在旧容器stale/blocked/querying SHALL 映射empty并保留失效原因、依赖缺口或在途记录；ready须核验当前引用，failed及旧拼写faild统一failed。未知旧状态 SHALL 明确报兼容缺口，不猜测后执行。原身份、版本、历史和引用 MUST NOT 丢失。

状态与等待/停止必要摘要 SHALL 随权威TripPlan同次事务维护轻量小结；活动发现、阈值有效样本统计、恢复、主投影、前端展示和存储门面 SHALL 使用同一语义，不能只改枚举而保留旧SQL/状态分支导致漏读、自动恢复取消方案或误计历史偏好样本。正常流程 MUST NOT 扫描所有完整TripPlan用于兼容，小结先定位后加载及CAS继续有效。

#### Scenario: 旧needs_input恢复
- **WHEN** 加载旧缺信息行程且用户补齐条件
- **THEN** 读取为running与pending/原必要草稿，保留断点身份和版本，继续原任务而非创建新旅行

#### Scenario: 旧stale或querying容器
- **WHEN** 读取旧失效或在途状态
- **THEN** 映射empty同时保留对应原因/请求记录，旧引用不冒充有效结果，在途请求不自动重发

#### Scenario: 索引提交与样本筛选
- **WHEN** 简化状态后的快照保存或历史有效行程计数
- **THEN** 小结与快照一致且失败共同回滚，暂停/取消/失败/模拟样本继续被排除，不因映射running而丢停止意图
