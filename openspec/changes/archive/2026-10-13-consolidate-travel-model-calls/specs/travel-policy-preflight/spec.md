## Purpose

在出差规划前复用企业RAG检索取得适用规则和证据，向主Agent提供可校验的结构化强制约束，明确其与非强制偏好的优先关系，同时保持现有RAG底层设计，避免旅游误套企业制度或缺资料时假定允许。

## ADDED Requirements

### Requirement: 出差政策前置与现有RAG继承

明确出差请求 SHALL 在主Agent生成行程安排前，通过继承rag_knowledge的政策Agent取得约束、适用范围、证据引用和缺口。对已有旅程的后续修改，已保存且仍适用有效的约束JSON可复用，不要求每轮重新调用政策Agent；适用条件/证据有效性变化时重新核对并按需取得资料。现有检索底层与引用接入 SHALL 继续复用，不重建RAG。普通旅游 MUST NOT 自动请求企业政策；无法判断目的时保留澄清或必要分类，不把这种额外调用隐瞒成固定2次路径。

#### Scenario: 企业出差规划
- **WHEN** 用户明确出差并提供必要员工/项目上下文
- **THEN** 政策Agent先返回资料，再将其交主Agent规划

#### Scenario: 普通旅游
- **WHEN** 用户只是旅游且没有政策要求
- **THEN** 不调用企业政策Agent

### Requirement: 检索理解与带来源JSON约束提取

政策Agent SHALL 负责检索政策、理解适用条件与例外、提取强制约束并返回带来源的结构化JSON；不得只拼接检索原文交给主Agent再做一遍政策提取。输入 SHALL 包括必要员工/项目/城市/日期等上下文与检索证据，检索基础设施继续复用。正常证据齐全路径 SHALL 直接从检索结果进行一次模型理解与提取，不先生成通用答案再调用模型转JSON；实际补检索、非法输出和冲突请求仍按实计数。

返回 SHALL 包含status、constraints、applicability、references、effective_date、missing_fields和conflicts。每条constraint SHALL 有稳定id、可理解规则、适用条件、hard强度、可支持规则的field/operator/value/unit及evidence_ids；复杂规则保留文字与条件，不能伪装成已支持的程序校验。references SHALL 以证据id绑定原检索文档/片段身份、可用版本、位置和有效期，不得捏造来源定位。程序 MUST 校验JSON、引用存在性、值类型、单位、适用范围及审批缺口，再把紧凑约束和来源索引交主Agent。原始片段保存为证据，不在主上下文重复发送answer与citations全文；相关强制约束不得为缩短上下文静默删除。

#### Scenario: 外部RAG只返回片段
- **WHEN** 现有RAG service返回检索引用和原文片段
- **THEN** 政策Agent基于片段理解并直接生成约束JSON，主Agent收到规则及可追溯来源，无需重复从原文提取

#### Scenario: 同时检索基础规则和项目例外
- **WHEN** 检索到基础住宿标准与项目提高限额说明但未有当前员工的有效审批
- **THEN** 返回适用基础约束、例外条件与来源，标识未批准例外，不擅自选择较宽松标准

#### Scenario: 引用无效或复杂规则未支持
- **WHEN** 模型输出不存在的evidence_id、非法单位，或程序无法执行的复杂规则
- **THEN** 前两者拒绝为有效约束；后者保留规则及待核实缺口，不声明已完成自动合规验证

#### Scenario: 规则相互矛盾或证据不足
- **WHEN** 无法根据资料确认规则优先级或必要资料缺失
- **THEN** 返回conflicts或missing_fields，不静默省略强制规则，也不当作无限制

### Requirement: 强制政策与软偏好

适用政策 SHALL 作为强制约束，偏好仅是非强制参考，程序 MUST 验证引用和适用条件。明确用户条件与政策冲突时不得擅改条件或批准例外；RAG失败/无知识 SHALL 保留缺口，不声明已满足未知制度。

#### Scenario: 品牌偏好与限额冲突
- **WHEN** 优先品牌候选超过适用住宿限额
- **THEN** 不以偏好突破限额；在用户许可范围内选其他候选或报告不可行

#### Scenario: 无批准的例外
- **WHEN** 检索资料包含更高限额但缺少适用于当前员工项目的审批
- **THEN** 不采用未批准例外，并保留证据和缺口
