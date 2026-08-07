# Source normalization adapter v1

本合同只把**显式指定、已经由 `local_raw_seal_v1` 哈希封存的本地 UTF-8 CSV**转换为
可审计的日频标准化候选。它不扫描目录、不联网、不下载、不写正式 `data/market`、
不修改 `datasets.json`、不发布正式快照，也不授予来源准入、质量晋级或研究用途。

## 输入闭合

请求必须显式给出：

- `seal_run_id`、`sealed_relative_path` 与 `sealed_sha256`；
- `source_id`、`dataset_id`、单一 `trade_date`；
- `Asia/Shanghai` 与带 `+08:00` 的 `available_at`；
- 九个标准输入字段的一对一 `field_map` 和固定 `unit_map`；
- 来源证券代码到永久证券 ID 的完整映射，以及预期永久证券 ID 全集。

工具会验证 `SEAL_MANIFEST.json`、`SEAL_MANIFEST.sha256`、封存文件字节数和 SHA-256。
CSV v1 限定严格 UTF-8、LF、末尾换行、单行记录和精确表头；不接受 BOM、NUL、CR、
嵌入换行或未映射附加字段。

## 失败关闭

下列情况整批失败且不留下最终输出：未知/额外证券、预期证券缺失、重复证券、字段
缺失或漂移、日期不一致、时区或 `available_at` 不合法、空值、非有限数、负数、
非正价格、OHLC 关系错误、seal/hash/bytes 不一致。未知值绝不填 0；来源冲突不在
本适配器内裁决。

## 输出

显式执行只允许新建
`tmp/source-normalization-adapter/<run_id>/`，并原子写入：

- `normalized.csv`：按永久证券 ID 排序的标准化候选；
- `row_lineage.jsonl`：逐行绑定原始 CSV 物理行 SHA-256、封存文件、来源、字段映射、
  单位、PIT 时间和永久身份；
- `RESULTS.json`：边界与计数回执；
- `MANIFEST.json`：上述三个业务文件的字节数与 SHA-256。

输出固定为 `INSPECTION_ONLY/NOT_ADMITTED`，不得绕过 `qdata` 用于研究。

## CLI

```powershell
python -X utf8 control/tools/run_source_normalization_adapter.py `
  --request tmp/source-normalization-adapter/requests/example.json

python -X utf8 control/tools/run_source_normalization_adapter.py `
  --request tmp/source-normalization-adapter/requests/example.json --execute
```

默认是只读 PLAN；只有 `--execute` 才写隔离输出，且同一 `run_id` 不可覆盖。
