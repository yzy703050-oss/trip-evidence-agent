# project-model-runtime Specification

## Purpose
完全移除AgentScope依赖，使用官方OpenAI SDK和项目轻量协议，保留模型预算、重试、审计和工具调用完整性。

## Requirements

### Requirement: 独立模型与消息协议

运行入口、业务代码、辅助脚本和测试 SHALL 不导入AgentScope，依赖声明 SHALL 直接包含OpenAI SDK。项目 SHALL 提供轻量消息/Agent接口及兼容OpenAI服务的异步模型适配，保留模型名、生成参数、工具定义、重试/熔断、健康检查、用量和上下文预算；既有config_agentscope模块可作为不依赖框架的过渡入口，不执行框架初始化。RAG外部项目不在本范围。

#### Scenario: 无框架启动
- **WHEN** 环境未安装AgentScope
- **THEN** CLI、注册器、业务Agent和离线测试可以导入运行

### Requirement: 完整响应后执行工具

SDK适配 SHALL 在完整消费流后合并文本与按index关联的工具ID、函数名及参数，校验参数JSON后返回完整快照。取消/截断/非法参数 MUST NOT 执行工具或记为成功；真实usage中的prompt/completion tokens SHALL 映射审计input/output tokens，缺失usage保留未知，不用估算冒充收费。

#### Scenario: 参数跨流片段
- **WHEN** 工具名称和参数分多次返回
- **THEN** 仅完整消费并校验后返回一次可执行工具指令

#### Scenario: 响应中断
- **WHEN** 流抛错、取消或工具参数未完成
- **THEN** 不返回可执行工具快照，不统计为成功调用
