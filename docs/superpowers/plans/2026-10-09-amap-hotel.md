# 高德酒店接入 Implementation Plan

> 执行：当前代理单代理实施与自审。用户已确认接入方案及目标目录并要求开始修改。

**Goal:** 让现有 hotel_search 返回可追溯的高德酒店地点候选。
**Spec:** ../specs/2026-10-09-amap-hotel-design.md
**Architecture:** 独立地点 Provider 和数据类型，原工具执行器追加能力适配，报价链路保持兼容。
**Tech Stack:** Python 3.11、requests、pytest、既有 AgentScope 工具循环。

## 约束与评审重点

- business.cost 不能成为房价，酒店地点不能宣称有房或预算合格。
- 无城市不联网；缺入住日期/人数允许地点搜索；报价 Provider 仍遵守原契约。
- 结果和行程校验不能误丢弃地点；地点不进入已知预算。
- 密钥不出现在日志/错误/来源；检查鉴权失败、超时、异常 JSON、无结果及缺选填字段。
- 不改攻略 Provider；共享文件修改前后检查当前工作区，避免覆盖并行修改。

## Task 1: 地点 Provider 与配置

Files: 新增 travel_data/hotel_places.py、travel_data/amap_hotel.py；修改 config.py、.env.example；测试 tests/test_amap_hotel.py。
Interface: AmapHotelProvider(api_key, *, http_get=None, timeout=10.0).search(HotelPlaceQuery) -> AgentDataResult。
- [x] RED：请求正确住宿类型/城市限制，多地点和来源；缺 Key、业务错误、超时、非法响应；cost 不映射房价。
- [x] 验证失败：pytest tests/test_amap_hotel.py -q，缺 Provider/地点契约导致 12 项失败。
- [x] 实现最小网络归一化与密钥配置。
- [x] GREEN：同命令 12 项通过。

## Task 2: 工具、校验、候选与展示贯通

Files: travel_data/tools.py、candidates.py、plan_guard.py、result_guard.py、cli.py、酒店/信息获取指南；测试 tests/test_amap_hotel_flow.py。
Interface: hotel_search 地点 schema 必填 city，keywords 可选；既有报价 Provider 的 schema/验证不改变。
- [x] RED：城市单独查询成功、缺城市不请求；默认五候选/下一窗口不重查；预算/空房条件显示未验证；行程和 CLI 保留地点，不虚构房价。
- [x] 验证失败：pytest tests/test_amap_hotel_flow.py -q，旧查询要求日期/人数或丢弃地点导致 11 项失败、3 项通过。
- [x] 实现与注册地点分支，同步模型工具说明。
- [x] GREEN：酒店新测试和旧回归通过。
- [x] 全量 pytest、自审、检查另一窗口修改；配置 Key 存在时安全调用验证；记录结果。

## 实施记录

- 基线：48 passed，1 个既有 dashscope 弃用警告。
- 项目未初始化 OpenSpec，不声称运行 propose/apply/verify/archive。
- 不创建新工作树：目标已经是独立项目目录且用户另一个窗口正在修改攻略，使用已确认目录做最小补丁。
- 最终全量验证：`python -m pytest -q --tb=short`，277 passed、3 skipped、1 个既有 dashscope 弃用警告，退出码 0。
- 全量测试曾发现旧火车启动测试过度断言所有 Provider 为空；修正为仅断言未注册 train_search，保留火车 unavailable 场景。
- 新增 CLI 启动到信息获取工具的接线测试；新旧酒店契约、地点行程引用和预算未知均有回归覆盖。
- 自审：逐项对照设计、请求字段、错误/密钥处理、报价兼容、候选缓存、行程/预算和启动注册；未发现未处理的重要偏差。
- 新增的两个模块和两个测试文件经 Ruff 整理格式及导入顺序后，`ruff check` 全部通过；`git diff --check` 通过。
- 共享文件通过小块补丁追加；未修改攻略 Provider、攻略测试或攻略工具实现。根工作区的已有修改保持原样。
- 初次实现时 Key 为空，缺 Key 冒烟检查返回 unavailable、0 候选且无异常。
- 用户填写 Key 后完成真实调用验证：查询上海汉庭酒店，HTTP 200、高德 status=1/infocode=10000，返回 25 个有效地点，工具 status=ok、默认显示 5 个。下一候选窗口显示 5 个且缓存命中，仅产生 1 次外部请求；行程保留所选真实地点，住宿报价和空房仍未知、预算仍标记缺少住宿费用。验证脚本退出码 0，未输出密钥。真实鉴权和本次线上查询通过，未验证其他查询或配额上限。
- OpenSpec 未初始化，未运行 OpenSpec verify 或 archive；已人工逐项核对本设计和测试证据。初次实现保留工作区修改，后续用户授权本地合并。

## 合并到 V0 main

- 确认目标是本仓库的 V0 命令行 main；其原提交 d203130 已包含 DDGS 旅游攻略，根目录的全栈仓库不参与此次合并。
- 酒店功能提交：76055fb。合并仅在 cli.py 的导入及 Provider 注册位置冲突，保留 DDGSGuideProvider 和 AmapHotelProvider 两者。模型工具说明也同时保留攻略和酒店规则。
- 合并结果全量验证：`python -m pytest -q --tb=short`，287 passed、3 skipped、1 个既有警告；退出码 0。自审确认攻略代码和测试保留，密钥文件不在暂存内容中。
- main 工作目录原无 `.env`，复制已验证的本地配置到被 Git 忽略的 `.env`；未覆盖已有配置，也未输出或提交任何密钥。
- 在 main 工作目录完成真实酒店工具查询：status=ok、25 个地点候选、显示 5 个、来源为 amap、房价未知，退出码 0。
- 本次为本地合并，保留仍被当前工作目录使用的功能分支；不推送远端、不删除其他窗口的工作目录。
