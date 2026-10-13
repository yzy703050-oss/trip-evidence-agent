# priority-tool-scheduling Specification

## Purpose
将已有优先级和依赖安排延续到程序工具执行中，确保偏好与必要前置资料先完成，再让互不依赖的火车酒店天气请求并行，且保留任务身份、总额度、事务保存和局部结果，避免共享状态引起数据串写或错误执行顺序。

## Requirements

### Requirement: 前置屏障与独立工具并行

程序 SHALL 保留priority/depends_on，明确偏好更新最高优先；规划及查询 MUST 等待必要偏好、历史和政策前置依赖。依赖已满足且相互独立的火车、酒店和天气请求 SHALL 并行运行；依赖前段实际结果的请求 MUST 等待，不强制为所有任务创建天气容器或请求未要求领域。

#### Scenario: 酒店火车天气独立
- **WHEN** 三项请求条件齐全且彼此无依赖
- **THEN** 程序在同一执行批次并行请求，统一按身份返回

#### Scenario: 酒店日期依赖抵达日
- **WHEN** 酒店报价日期尚待前段实际抵达确定
- **THEN** 挂起该酒店请求，允许其余独立请求执行，不能编造日期抢跑

### Requirement: 并发作用域隔离与静默信息Agent

并行请求 SHALL 使用独立上下文与唯一请求身份，按实际task/query归并结果，协调总预算、缓存及事务保存，MUST NOT 对共享current_task_id直接并发修改。information_query SHALL 保留代码与设计供后续开发，但本阶段全部停止调度和模型加载，包括其天气/网页/攻略路径；历史与偏好读取 SHALL 改为工具。未在本阶段迁移的入口应报告未启用，不静默回调该Agent。

#### Scenario: 并发结果乱序返回
- **WHEN** 不同任务工具以任意顺序完成
- **THEN** 结果仍写入正确任务和容器，无重复扣额或串写

#### Scenario: 信息Agent保留但禁用
- **WHEN** 主计划或旧调度请求information_query
- **THEN** 不实例化或调用该模型，不通过降级路径绕过静默；已迁移领域走程序工具
