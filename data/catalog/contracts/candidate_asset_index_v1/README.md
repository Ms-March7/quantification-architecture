# 中央候选资产索引合同 v1

`control/tools/build_candidate_asset_index.py` 只盘点
`data/market/quarantine/` 下每个顶层候选根的 manifest、receipt、verification、
result 和 checkpoint JSON，并把路径、字节数及 SHA-256 原子写入
`data/catalog/candidate_assets.json`。

该索引解决“候选文件存在但缺少中央机器清单”的问题。它不读取业务 payload，
不扫描 `planned_datasets.json`，不写 `datasets.json`，不发布快照、不晋级质量，
也不授予研究用途。

```powershell
& 'control/runtime/data-engineering/.venv/Scripts/python.exe' -X utf8 `
  'control/tools/build_candidate_asset_index.py' --write

& 'control/runtime/data-engineering/.venv/Scripts/python.exe' -X utf8 `
  'control/tools/build_candidate_asset_index.py' --check
```
