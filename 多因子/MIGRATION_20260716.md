# 迁移记录

- 迁移日期：2026-07-16
- 原位置：`${QUANTIFICATION_ROOT}\CWOPT相关\CWOPT`
- 新位置：`${QUANTIFICATION_ROOT}\多因子`
- 范围：因子工厂、配置、真实市场快照、研究结果、短周期/券商只读模块、测试、启动脚本、文档和本项目临时状态。
- 明确保留在原项目：CWOPT 的 Git 仓库、前端、原有 ML 模型、`data/history`、`.venv`、`package-lock.json` 和用户已有修改。

V2 的 `manifest.json` 是已提交的不可变研究证据，其中保存的旧数据绝对路径不会被改写。迁移后数据对应位置为：

`${QUANTIFICATION_ROOT}\多因子\data\market\snapshots\2026-07-14\qlib_bin`

该位置变化不改变归档文件和研究产物的内容哈希。
