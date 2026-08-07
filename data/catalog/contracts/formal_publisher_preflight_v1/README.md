# Formal publisher preflight v1

这是正式发布器之前的**只读预检合同**，不是发布器。它只读取一个显式、
哈希绑定的请求并把不可执行计划或阻断缺口输出到 stdout。

## 输入闭包

请求必须同时绑定：

1. `candidate_manifest_v1` 候选清单及其每个候选文件的字节数和 SHA-256；
2. `selected_core_stage_a_bundle_v1`，且内容哈希、四项 ready 状态全部通过；
3. `source_rights_receipt_v1`，并绑定候选清单哈希；
4. `formal_publish_authorization_receipt_v1`，由用户签发、单次使用，并同时
   绑定候选清单、Stage-A 内容哈希、权利回执、目标数据集、目标快照和目标路径；
5. 精确 `dataset_id + snapshot_id`、合同允许的正式根目录；
6. 精确 qdata 研究解析、不可变快照、原子可见和失败回滚等发布后置条件。

任何缺项、额外字段、路径越界、链接/重解析点、硬链接、哈希/字节不符、
可变身份（如 `latest`）或目标已存在都会 fail closed。

## 不可逾越的边界

- 输出恒含 `executable=false`、`formal_write=false`、`formal_effect=NONE`。
- 工具没有 `--execute`、`--output` 或其他写入开关。
- 工具不导入、不调用生产/隔离原子发布实现，不启动子进程，也不运行 qdata。
- 工具不写 `tmp`、报告、Roadmap、catalog、registry、snapshots 或 published。
- PASS 只表示证据闭包可交给**另一个、另行批准并验证的生产发布器**；它不
  表示已正式准入、已发布或已可研究。

调用方式：

```powershell
python -X utf8 control/tools/run_formal_publisher_preflight.py `
  --request tmp/formal-publisher-preflight/requests/<preflight_id>.json
```

成功退出码为 0；任何缺口以 `FAIL_CLOSED` JSON 输出并返回 2。
