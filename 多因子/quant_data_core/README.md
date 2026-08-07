# Quant Data Core

这是独立于 `factor_factory` 的不可变数据发布与登记层。它只管理数据身份、来源、完整性和用途权限，不加载因子，不读取项目 `.state`，也不会联网下载数据。

工作区统一发现入口为 `${QUANTIFICATION_ROOT}\data\catalog\datasets.json`；`quant_data_core/catalog.sqlite3`、release manifest 和 `PROVENANCE.json` 仍是不可变快照事实的权威。

工作区级只读入口是 `${QUANTIFICATION_ROOT}\data\catalog\dataset_cli.py`。现有两份冻结 Qlib 数据本体已于 2026-07-20 删除，并从活动 catalog 移入 tombstone；从 2026-07-19 之后新建的共享市场快照应把发布根设为 `${QUANTIFICATION_ROOT}\data\market\snapshots`，完成 manifest/provenance 后再登记到工作区目录。不得复制、链接或复用旧快照来制造新数据。

## 核心合同

- SQLite 以 `(dataset_id, snapshot_id)` 为不可变主键；数据库触发器拒绝更新和删除。
- 发布时对目录路径、结构、文件大小和字节计算 `sha256-tree-v1`，并单独封印 `PROVENANCE.json`。
- manifest 通过临时文件、`fsync` 和原子替换写入；解析时复核数据库、manifest、PROVENANCE 和目录哈希。
- `research`、`shadow`、`live` 禁止隐式 latest，必须给出 `snapshot_id` 或 `as_of`。
- 用途按 `ENGINEERING_ONLY < RESEARCH_ONLY < SHADOW_ONLY < LIVE_READY` 逐级校验；`live` 还要求 `pit_verified=true`。

## 离线 GitHub release 摄取

`ingest-github-staged` 是可信边界，不是下载器。调用前应把 GitHub REST release API 响应原样保存为 JSON，并把目标 asset 下载到 staging。摄取器随后执行：

1. 校验正式 release、仓库 URL、release/asset ID、唯一 asset 名称和 `sha256:` 发布者摘要。
2. 对 staging 文件复核“普通文件、精确字节数、SHA-256 摘要和配置上限”。
3. 预检 tar 全部成员，拒绝绝对路径、`..`、大小写冲突、Windows 歧义路径、软/硬链接、设备文件、稀疏文件和解压膨胀超限；不调用 `extractall`。
4. 要求唯一 Qlib 根目录，检查 `calendars/day.txt` 严格递增且截止日准确、`instruments/all.txt` 非空，并确认存在非空 `.day.bin` 特征文件。
5. 在目标磁盘内构建临时 release，原子重命名后才登记 catalog；任何前置失败都不会产生 catalog 行或正式发布目录。

示例（命令本身不访问网络）：

```powershell
Set-Location '${QUANTIFICATION_ROOT}\多因子'

py -m quant_data_core --catalog quant_data_core\catalog.sqlite3 ingest-github-staged `
  --owner OWNER --repository REPO `
  --metadata-file staging\release.json `
  --asset-file staging\qlib-cn-2026-07-14.tar.gz `
  --asset-name qlib-cn-2026-07-14.tar.gz `
  --expected-asset-bytes 123456789 `
  --expected-publisher-sha256 sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef `
  --publish-root data_releases `
  --dataset-id cn-a-share-qlib-daily --snapshot-id 2026-07-14 `
  --expected-calendar-end 2026-07-14 `
  --source-tier VERIFIED_SECONDARY --frequency 1d --universe SH,SZ `
  --coverage-file quant_data_core\bootstrap\qlib-2026-07-14-coverage.json `
  --schema-version qlib-bin-v1 --license LICENSE-NAME `
  --ingested-at 2026-07-19T00:00:00Z
```

若 GitHub tag 与 `snapshot_id` 不相同，必须显式增加 `--expected-tag`。`--expected-asset-bytes` 与 `--expected-publisher-sha256` 是推荐的带外固定值：它们会先与 API metadata 互相校验，再校验本地 staging 文件。默认安全上限为 20 GiB asset、100 万个 tar 成员和 200 GiB 解压字节，可用 CLI 参数收紧或调整。

## 查询与验证

```powershell
Set-Location '${QUANTIFICATION_ROOT}\多因子'

py -m quant_data_core --catalog quant_data_core\catalog.sqlite3 list

py -m quant_data_core --catalog quant_data_core\catalog.sqlite3 resolve `
  --dataset-id cn-a-share-qlib-daily `
  --purpose research `
  --snapshot-id 2026-07-14
```

本项目不可变 release catalog 仍保存历史 `RESEARCH_ONLY` 记录，但对应数据本体已经删除。新任务必须使用工作区 `qdata`；两个旧身份现在返回“未登记”。项目内 `resolve` 示例只保留为历史工具说明，不构成使用授权，也不能成功回读原路径。

## 测试

```powershell
py -B -m unittest discover -s quant_data_core -p 'test_*.py' -v
```

测试完全使用临时目录和本地伪 tar/伪 GitHub 元数据，不触网，也不修改现有快照。
