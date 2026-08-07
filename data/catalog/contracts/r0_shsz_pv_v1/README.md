# R0_SHSZ_PV 独立窄门合同

本合同不修改、替代或满足 canonical `R0_PV`。范围严格冻结为：

- `XSHG`、`XSHE`；
- `2005-01-01` 至 `2024-12-31`；
- 375 个已知冲突键失败关闭地排除；
- 日终事实仅从下一观测开市日 `09:30 Asia/Shanghai` 起可用；
- 北交所不在范围内；上游材料中的北交所引用只能作为排除或审计证据，不能成为候选行。

只读验证器：

```powershell
& control/runtime/data-engineering/.venv/Scripts/python.exe -X utf8 `
  control/tools/verify_r0_shsz_pv_gate.py
```

输出同时给出 `evidence_integrity_verdict` 和 `research_gate_verdict`。当前固定证据应为：

- 证据完整性：`PASS`；
- 研究门：`FAIL`；
- 进程退出码：`2`。

退出码 `0` 只允许表示研究门本身通过。验证器不写文件、不联网、不发布快照、不修改
`datasets.json`、Roadmap 或 source registry，也不运行因子。
