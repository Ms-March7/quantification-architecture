# Formal atomic workspace publisher v1

状态：
`PASS__WORKSPACE_EXECUTION_ENGINE_VERIFIED__DEFAULT_DISABLED__NO_REAL_PUBLICATION_EXECUTED`

本合同为 `formal_atomic_publish_execution_v1` 增加真实工作区执行面，但不替代其证据
闭包。当前只完成临时工作区机械验收；没有对 `${QUANTIFICATION_ROOT}` 发布任何数据。

## 三重显式写入门

一次写入必须同时给出：

1. `--workspace-root ${QUANTIFICATION_ROOT}`，且必须与发布器编译时可信根精确相等；
2. `--catalog-baseline-sha256 <64位小写SHA-256>`，且必须与执行请求绑定的
   `data/catalog/datasets.json` 基准相等；
3. `--execute-workspace`。

缺少第 3 项时只执行完整只读验证并返回
`PASS__WORKSPACE_EXECUTION_DISABLED__NO_WRITE`。

### dry-run

```powershell
& 'control/runtime/data-engineering/.venv/Scripts/python.exe' -X utf8 `
  control/tools/execute_formal_atomic_publish_workspace.py `
  --request 'tmp/formal-publisher-execution/requests/<execution_id>.json' `
  --workspace-root '${QUANTIFICATION_ROOT}' `
  --catalog-baseline-sha256 '<catalog_sha256>'
```

### 显式发布（必须等待该候选单独审计批准）

```powershell
& 'control/runtime/data-engineering/.venv/Scripts/python.exe' -X utf8 `
  control/tools/execute_formal_atomic_publish_workspace.py `
  --request 'tmp/formal-publisher-execution/requests/<execution_id>.json' `
  --workspace-root '${QUANTIFICATION_ROOT}' `
  --catalog-baseline-sha256 '<catalog_sha256>' `
  --execute-workspace
```

### 硬崩溃恢复

只接受原执行请求及遗留锁文件的精确 SHA-256：

```powershell
& 'control/runtime/data-engineering/.venv/Scripts/python.exe' -X utf8 `
  control/tools/execute_formal_atomic_publish_workspace.py `
  --request 'tmp/formal-publisher-execution/requests/<execution_id>.json' `
  --workspace-root '${QUANTIFICATION_ROOT}' `
  --catalog-baseline-sha256 '<原catalog_sha256>' `
  --recover-stale-lock-sha256 '<遗留锁SHA-256>' `
  --execute-workspace
```

工具核验 PID/start-token、主机、execution/request/plan 身份后才归档旧锁并恢复；活锁、
未知进程状态、哈希不符或 catalog 不属于 sealed before/after 状态均失败关闭。

## 必需授权与权利回执

执行请求必须引用通过 `formal_publisher_preflight_v1` 的单次授权。授权文件严格字段为：

```json
{
  "schema_version": 1,
  "contract_id": "formal_publish_authorization_receipt_v1",
  "authorization_id": "<immutable-id>",
  "action": "AUTHORIZE_EXACT_FORMAL_SNAPSHOT_PUBLICATION",
  "authorized_by": "USER",
  "authorized_at_utc": "<带时区时间>",
  "statement": "<明确授权语句>",
  "scope": ["CORE-01", "CORE-02", "CORE-03", "CORE-06"],
  "dataset_id": "<dataset-id>",
  "snapshot_id": "<snapshot-id>",
  "target_relative_path": "data/market/snapshots/<dataset-id>/<snapshot-id>",
  "candidate_manifest_sha256": "<sha256>",
  "stage_a_bundle_sha256": "<sha256>",
  "source_rights_receipt_sha256": "<sha256>",
  "single_use": true
}
```

`license_status=DECLARED` 与 `VERIFIED` 都必须有 `source_rights_receipt_v1`，且权利范围
必须精确为本地保存、内部研究、派生研究、raw 保留、禁止外部分发。授权先
`RESERVED`；只有 qdata research 后置校验成功后才标记 `CONSUMED`。

正式研究快照的 `lifecycle` 固定为 `frozen`，与
`多因子/factor_factory/qdata_input.py` 的正式研究 resolver 一致。`review` 只表示待审
状态，不能由本发布器写成“可供新研究读取”的正式身份。

执行请求还必须给出非空 `publication_profile.research_input_contract`。发布器不解释
其中的具体因子/市场语义，只做确定性 JSON 校验，并把该对象原样写入
`manifest.json`；该对象参与不可变 `content_hash`，因此发布后不能无痕补写或改名。

## 原子性、回滚与 qdata 后置条件

- payload 逐文件复制前后复核字节数/SHA-256；私有 staging 与正式目标必须同卷；
- raw、curated 与精确不可变 snapshot 目录完成后，catalog 才以同目录临时文件和
  `os.replace` 做 compare-and-swap 可见；目标碰撞禁止覆盖；
- snapshot 必须只含五份 authority docs，并通过深度 payload 合同校验；
- `manifest.json` 必须原样封存执行请求中的 `research_input_contract`；
- qdata 必须以精确 `dataset_id + snapshot_id`、`intent=research`、登记 consumer
  解析；同时要求 lifecycle=`frozen`、immutable、`quality_status=OK`、`RESEARCH_ONLY`、非 `latest`、
  license 为 `DECLARED/VERIFIED`、物理路径与 content hash 精确一致；
- 同一身份还必须通过 `多因子/factor_factory/qdata_input.py` 的正式
  `QDataResearchResolver`；发布器不会以较宽松的内部 catalog 检查替代因子入口门；
- qdata 失败时，catalog 只在仍等于 sealed candidate hash 时 CAS 恢复原字节，已
  finalize 的 raw/curated/snapshot/stage 移入受控 rollback，授权标记
  `ABORTED_NOT_CONSUMED`；catalog 发生第三方漂移时拒绝猜测性回滚。
