# R0_SHSZ_PV publication package v2

该合同只生成并验证 dry-run 包。请求必须绑定一份独立、精确、单次使用的
`formal_publish_authorization_receipt_v2`；builder 不得从说明文字推导授权。

执行请求不携带 `published_at`。真正执行时由 workspace publisher 在持有
catalog 原子写锁后生成提交时刻，并写入 snapshot authorities、content hash
和 catalog。`available_at` 早于当前时钟之前，任何授权都不得被预留或消费。

权利状态固定为 `DECLARED__NOT_VERIFIED__NO_LEGAL_OPINION`。RIGHTS_BASIS、
CSMAR 声明/核验回执和 RQData 配额回执必须经 SHA-256 绑定进入 source-rights、
execution authorization 及最终 formal provenance。
