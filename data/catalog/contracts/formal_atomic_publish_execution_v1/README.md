# Formal atomic publication execution contract v1

状态：`PASS__ISOLATED_STATE_MACHINE__WORKSPACE_EXECUTOR_DEFAULT_DISABLED`

本合同把真实候选发布前必须共同绑定的证据闭包和原子状态机固定下来。它解决的
是 synthetic 原子引擎与真实候选之间缺少严格控制面的工程缺口。当前版本既有隔离
状态机，也有另行受控、默认关闭的真实工作区执行器；后者的合同位于
`formal_atomic_publish_workspace_v1`，其存在不代表任何候选已经获准或完成发布。

## 请求必须精确绑定

1. 已通过的 `formal_publisher_preflight_v1` 请求及其 SHA-256/字节数；
2. preflight 内的候选 manifest、Stage-A `4/4`、来源权利和单次用户授权；
3. 当前 `data/catalog/datasets.json` 的 SHA-256/字节数；
4. 与候选 manifest 完全一一对应的 raw/curated payload layout；
5. 精确 `dataset_id + snapshot_id + formal_root + target_relative_path`；
6. 非空且可确定性 JSON 序列化的 `publication_profile.research_input_contract`；
7. 私有 staging、同卷 finalize、catalog compare-and-swap、崩溃恢复、qdata 后置
   条件、qdata 失败 catalog 回滚和授权成功后才消费等全部硬门。

任何缺项、额外字段、mutable identity、路径越界、reparse/symlink/hardlink、
大小/哈希不符、payload 少映射/多映射、目标碰撞或 catalog 漂移都失败关闭。

## 只读计划工具

`control/tools/validate_formal_atomic_publish_execution.py` 只读验证请求并把
`INPUT_INVENTORY_SEALED` 的确定性计划输出到 stdout：

```powershell
python -X utf8 control/tools/validate_formal_atomic_publish_execution.py `
  --request tmp/formal-publisher-execution/requests/<execution_id>.json
```

成功结果仍固定为：

```text
executable=false
write_performed=false
formal_effect=NONE
```

它不复制 payload、不建立 staging、不获取 catalog 锁、不运行 qdata、不消费授权、
不写 receipt、catalog、snapshot、published、registry 或报告。

## 隔离执行工具

`control/tools/execute_formal_atomic_publish_isolated.py` 复用同一份严格验证结果，并在
`tmp/formal-publisher-isolated-workspaces/<workspace_id>` 的 sentinel 绑定副本内执行完整
状态机。默认不执行；只有显式 `--execute-isolated` 才会在该副本中写入：

```powershell
python -X utf8 control/tools/execute_formal_atomic_publish_isolated.py `
  --request tmp/formal-publisher-execution/requests/<execution_id>.json `
  --isolated-workspace tmp/formal-publisher-isolated-workspaces/<workspace_id> `
  --execute-isolated
```

隔离状态机覆盖：

1. 每件 artifact 复制前后校验字节数和 SHA-256；
2. 私有 staging、同卷 `os.replace` finalize、不可变目标禁止覆盖；
3. catalog 基准哈希 CAS、同目录临时文件、`fsync` 和原子替换；
4. qdata research 身份/路径/不可变性/质量后置校验；
5. qdata 失败时精确恢复 catalog 原字节，并把 payload 移入隔离 rollback；
6. 单次授权 exclusive reservation、成功后消费和跨 execution 防重放；
7. journal、PID/start-token 锁、精确 stale-lock 哈希确认及 Windows 硬崩溃恢复。

真实 `${QUANTIFICATION_ROOT}`、真实 `datasets.json`、真实 snapshots/published 不满足隔离
sentinel 合同，工具会在任何写入前失败关闭。隔离成功只表示状态机工程验收通过，
不构成来源准入、质量晋级、正式 catalog 登记、正式快照或研究可用性结论。

同一执行请求现在也可交给另行受控的
`formal_atomic_publish_workspace_v1`。该执行面默认 dry-run，只有精确工作区根、显式
catalog 基准哈希和 `--execute-workspace` 同时出现才允许写。`license_status` 可为
`DECLARED` 或 `VERIFIED`，但两者都必须通过本合同原有的精确来源权利回执闭包；
`DECLARED` 不等于已完成独立法律核验。

`publication_profile.lifecycle` 固定为 `frozen`，使发布后的不可变正式快照与多因子
正式研究 resolver 的 `active/frozen` 准入集合一致；本合同不再允许把 `review` 身份
误发布为可研究快照。

通用发布器不推断、补写或重命名 `research_input_contract` 的业务字段；它只验证该值
是非空、可确定性序列化的 JSON 对象，并将其原样纳入 `manifest.json` 和快照
`content_hash`。具体研究入口必须再按自己的合同逐项核验语义。
