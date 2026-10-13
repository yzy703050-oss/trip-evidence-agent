## MODIFIED Requirements

### Requirement: 主视图去重并精简双向委派

系统 SHALL 提供轻量任务索引和当前判断所需详细任务，批量判断时提供本批有界候选和必要条件；候选只在必要结果字段出现一次。火车酒店查询结果 SHALL 由程序直接投影给主模型。专业子结果保留摘要和查询元数据，系统 MUST NOT 向专业子Agent传入全部workflow、无关任务、主历史或主读取消息。每次专业子调用 SHALL 独立，主历史仍只按完整旧用户轮次压缩。

#### Scenario: 当前段有大量查询
- **WHEN** 程序向主模型或专业子Agent组装判断资料
- **THEN** 无关草稿与候选不重复展开，当前有效引用和必要衔接事实保留

#### Scenario: 批量判断多个任务
- **WHEN** 主Agent收到去程、酒店和返程工具结果
- **THEN** 每个任务保留有界候选及身份，不展开全部缓存或增加摘要子模型

### Requirement: 保留现有 JSON 状态机和分页协议

系统 SHALL 通过受控JSON执行业务，支持首轮批量查询计划和批量草稿与回复模板提交；程序 SHALL 管理任务状态，自动在草稿齐全时执行全程校验并在成功后完成回复。dispatch、draft_task、validate_workflow、finish及受控修复 SHALL 可供兼容和异常处理使用，正常路径 MUST NOT 为这些可合并动作分别增加主模型请求。系统 MUST NOT 为字段精简新增主Agent原生只读工具循环或强制列表后再次读取单项详情。candidate_offset与有效缓存 SHALL 可继续使用，最终回复 SHALL 基于reply_facts。

#### Scenario: 一页候选足以决定
- **WHEN** 必要字段已足以选择
- **THEN** 主模型直接输出合法草稿或批量提交，不强制增加详情读取请求

#### Scenario: 批量方案可以完成
- **WHEN** 草稿齐全且程序全程校验通过
- **THEN** 程序渲染reply_facts回复，不另外请求校验和结束动作
