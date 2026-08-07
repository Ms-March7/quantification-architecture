# R0_SHSZ_PV publication package v1

该合同把统一面板候选转换为正式发布器可读的证据包，但自身没有正式发布能力。

默认调用只做无写入计划：

```powershell
& 'control/runtime/data-engineering/.venv/Scripts/python.exe' -X utf8 `
  control/tools/build_r0_shsz_pv_publication_package.py `
  --request 'tmp/r0-shsz-pv-publication-package/requests/<package_id>.json'
```

只有显式 `--materialize-package` 才会写入：

- `tmp/r0-shsz-pv-publication-package/packages/<package_id>/`；
- `tmp/formal-publisher-preflight/requests/preflight-<package_id>.json`；
- `tmp/formal-publisher-execution/requests/execution-<package_id>.json`。

随后构建器只运行三项只读检查：formal preflight、execution plan validator、
workspace publisher 的 `enabled=false` dry-run。它没有 `--execute-workspace`，不会修改
`datasets.json`、Roadmap、source registry、正式 snapshots/published，也不会运行因子。

Stage-A 的四个目标固定为 `R0-SHSZ-IDENTITY/CALENDAR/PV/EXECUTION-MASK`，只属于
独立 `R0_SHSZ_PV` 窄门，不得解释为 canonical CORE-01/02/03/06、CORE20 或
canonical `R0_PV` 已闭合。

权利状态固定为 `DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION`：

- CSMAR 只绑定既有精确资产用户声明；
- RQData 只绑定教育版许可观察、无凭据配额回执、用户在本任务中的本机集成授权，
  以及米筐官方文档展示的 DataFrame/CSV 导出能力；
- 范围仅限本机内部非商业研究、raw 留存、内部派生、禁止外部分发；
- `RIGHTS_BASIS.json/.md` 明确不构成独立合同核验、法律意见或正式来源准入。

请求和输出不得包含 License、密码、token、私钥或其他凭据值。
