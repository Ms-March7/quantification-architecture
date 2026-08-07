# 候选层统一日维护合同 v1

`control/tools/run_candidate_daily_maintenance.py` 严格按以下顺序执行：

1. 沪深价量日增量候选；
2. B 路径日增量候选；
3. 从已封存 B 路径候选中选择最早未完成的财务 PIT 队列，每次最多续跑一项；若所有队列已完成，仅做哈希/状态复核并返回无网络 `NO_CHANGE`；
4. 仅当固定的本地 JoinQuant 股本源存在时，构建或验证股本 PIT 候选；源未变且候选已提交时只做幂等哈希验证，源缺失时跳过，源漂移或不可变输出不完整时失败关闭；
5. 最后重建并原子写入中央候选资产索引。

前四步相互独立：某一步失败不会阻止其他步骤尝试；最后仍刷新索引，使已经成功封存的不可变候选可以被登记。任何一步失败都会令统一维护回执为 `FAIL_CLOSED`。已经封存的不可变候选不回滚，也不覆盖。

统一入口使用 Windows 内核字节区间锁阻止并发维护。锁由进程句柄持有，进程正常退出或硬崩后都会由操作系统自动释放；磁盘上的 `.maintenance.lock` 只是诊断载体，不是需要人工删除的哨兵。

JoinQuant 步骤没有外部客户端，只接受本地已封存人工源；本地源缺失不会触发下载或浏览器操作。财务步骤只有在存在未完成队列时才可能调用 RQData，已完成队列在导入客户端前返回 `NO_CHANGE__QUEUE_COMPLETE__NO_NETWORK`。

该入口只维护 `INSPECTION_ONLY/NOT_ADMITTED` 候选和 `data/catalog/candidate_assets.json`。它禁止修改 `datasets.json`、Roadmap、发布正式快照、晋级质量、运行因子、消费 Validation/Holdout，也不读取、显示或保存凭据值。正式数据发布与研究运行仍是独立工作包。
