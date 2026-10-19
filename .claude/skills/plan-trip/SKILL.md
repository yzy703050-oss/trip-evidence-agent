---
name: plan-trip
description: 主Agent通过原生工具参数规划和修改行程。
---

MainAgent负责规划、选择和最终回复，运行入口为 `trip_assistant.bootstrap.create_application`。独立脚本复用 `trip_assistant.cli.TripEvidenceCLI`，不启动第二套编排。

规划先通过 `read(resource=preferences, request_kind=planning)` 读取有效偏好；企业出差先调用 `query_policy`，取得政策小结和结构化约束。新规划不自动继承旧行程；修改先用摘要定位，有歧义澄清，再按ID读取当前版本。

通过 `write_trip` 的TripPlan参数提出有序task/item，每个task是一段旅程。住宿日期或晚数结合用户、长期记忆与政策提出，未明确单程默认考虑返程。来源和强度分开记录：用户软表达仍为soft，固定要求及适用政策为hard。程序不补固定晚数或暗加业务排序。

查询返回按硬条件过滤、按主Agent指定顺序排序的Top5；必要时用 `read(resource=candidates)` 的offset读取有效缓存后页。选择必须引用真实已见的query_id/result_revision/candidate_id。TripPlan的状态、版本和报价由程序控制，不能通过模型参数伪造。

所有行程修改，包括只换酒店，均用 `write_trip`；简单记忆写不能改行程。局部amend仅修改授权任务和组件，路线调整用route_tasks。程序保留原有效方案与工作进度，返回失效证据、校验和依赖冲突；修复用repair_issue_codes引用最新相关问题，主Agent决定软调整，硬冲突询问用户。暂停、取消和等待附原因，不因running自动执行。

从首次复杂写批次起最多3轮工具交互；准备工具不计3轮，每批最多8工具、12个不同外部查询。最后工具结果仍回主Agent统一答复，不再执行第4轮。缺必要报价的地点接口不能靠反复刷新补齐；allow_repair=false表示保存草稿、校验后停止修复。

最终答复按真实保存结果说明各段车次、时间、住宿日期、已知费用和缺口。地点候选不代表房价或空房，未知不是零或满足。completed仅表示规划校验保存成功，不代表已预订。正常答复由主Agent生成，程序仅做事实校验与异常回退；不输出旧action/finish/reply_template JSON。

工具字段以 `trip_assistant/tools/registry.py` 和 `trip_assistant/trips/contracts.py` 为准。
