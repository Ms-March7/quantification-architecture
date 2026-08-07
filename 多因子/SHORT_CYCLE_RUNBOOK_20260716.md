# 多因子短周期系统：2026-07-16 盘中观察运行手册

## 结论

截至 2026-07-15 晚，系统可直接用于盘前检查、1/5分钟持仓监控、风险告警、QMT只读账户同步和影子意图预审。仓库不存在自动或人工触发的下单接口；公共行情永远只有观察资格。

本机现有财通证券同花顺客户端不是 QMT、MiniQMT 或 PTrade，不能安全地当作程序化交易 API。财通是否向当前账户开放 QMT/MiniQMT、外部 XtQuant 和模拟柜台，必须由财通 95336 或客户经理确认。

程序化交易还需先向券商确认信息报告状态。证监会规则要求“先报告、后交易”，个人投资者也在适用范围内：[证监会《证券市场程序化交易管理规定（试行）》](https://www.csrc.gov.cn/csrc/c100028/c7480577/content.shtml)。

## 系统边界

```text
公开双源行情 ──> 观察/影子监控 ───────────────┐
                                                ├─> RiskGate ─> 人工复核 ─> 影子意图
QMT分钟行情 + 账户 + 持仓 + 可卖数量 + 回报 ──┘
```

模型不能直接调用券商 SDK。当前 RiskGate 只做影子检查：QMT官方会话、账户重查、最新报价、T+1可卖数量、白名单、限价偏差、单笔/单日额度、亏损闸门和幂等键。检查通过也不会生成或提交真实委托。

## 明天直接启动

### 推荐：本机网页防守台

```powershell
powershell -ExecutionPolicy Bypass -File "${QUANTIFICATION_ROOT}\多因子\tools\start-short-cycle-dashboard.ps1" -Broker public
```

浏览器会打开 `http://127.0.0.1:8791/`。该服务只监听本机回环地址，不向局域网或互联网开放。按 `Ctrl+C` 停止。

### 1. 没有 QMT 权限：公共行情观察台

在 PowerShell 中运行：

```powershell
powershell -ExecutionPolicy Bypass -File "${QUANTIFICATION_ROOT}\多因子\tools\start-short-cycle-monitor.ps1" -Broker public
```

输出持续写入：

```text
${QUANTIFICATION_ROOT}\多因子\.tmp\short-cycle\latest-monitor.json
```

截图持仓已保存为被 Git 忽略的本机快照 `.tmp\short-cycle\manual-account.json`，不含资金账号或认证信息。任何手工成交、撤单或可卖数量变化后都必须更新该快照；没有 QMT 成交回报时，系统始终显示 `manual_position_snapshot_requires_reconciliation`。

此模式会轮询东财和新浪，统一成交量为“股”、成交额为“人民币”，只使用已经完成的K线。任何单源、源分歧、时间戳异常或缺失都会降低为观察或阻断。此模式不允许自动委托。

### 2. 已获得财通官方 QMT/MiniQMT 权限：只读账户监控

先由你本人启动 MiniQMT、完成官方登录和人机验证。账号只放在当前 PowerShell 会话，不写进仓库：

```powershell
$env:MULTIFACTOR_QMT_USERDATA = '券商官方终端的 userdata_mini 绝对路径'
$env:MULTIFACTOR_QMT_ACCOUNT_ID = '你的资金账号'

py "${QUANTIFICATION_ROOT}\多因子\ml\qmt_broker_cli.py" snapshot
```

若只读快照成功，再启动 QMT 监控：

```powershell
powershell -ExecutionPolicy Bypass -File "${QUANTIFICATION_ROOT}\多因子\tools\start-short-cycle-monitor.ps1" -Broker qmt
```

迅投官方说明：XtQuant 需要先启动 MiniQMT；`XtTrader` 可以查询资产、持仓、委托和成交并接收回报，[官方交易 API](https://dict.thinktrader.net/nativeApi/xttrader.html)、[官方行情 API](https://dict.thinktrader.net/nativeApi/xtdata.html)。

## 影子意图（真实委托已禁用）

当前程序只允许账户/行情只读检查和影子风险预审。先显式初始化当日 fail-closed 风险状态：

```powershell
py "${QUANTIFICATION_ROOT}\多因子\ml\qmt_broker_cli.py" initialize-state
```

随后可以生成只读影子意图：

```powershell
py "${QUANTIFICATION_ROOT}\多因子\ml\qmt_broker_cli.py" `
  preview-order `
  --symbol '证券代码' `
  --side 'BUY或SELL' `
  --quantity 100 `
  --limit-price 1.000 `
  --reason '人工复核后的理由'
```

结果外层状态只能是 `SHADOW_INTENT_ONLY`；内部风险状态为 `RISK_CHECK_PASSED` 也不代表实盘批准。该命令不生成批准文件或确认码，仓库没有 `submit-order` 子命令，也没有底层提交方法。

在签名批准、账户指纹绑定、一次性人工 nonce、并发锁、券商委托回查幂等、不可变额度和独立 live 服务全部实现并重新审计前，不得恢复真实委托。

## 2026-07-16 时间表

- 08:30–09:00：公告、停复牌、除权；从券商核对总持仓和可卖数量。
- 09:15–09:25：只观察集合竞价；不产生模型订单。
- 09:30–09:45：至少等待3根完整5分钟K线，禁止新增模型风险。
- 09:45–11:15：每15分钟产生一次可复核状态。
- 11:15后：不新增上午订单；11:25前处理未成交单。
- 13:00–13:15：重新暖机并检查午间事件。
- 13:15–14:30：每15分钟产生一次可复核状态。
- 14:45后：停止新增模型风险。
- 14:57–15:00：首期系统不产生订单。
- 15:00后：核对成交、滑点、持仓锁定和审计日志。
- 15:05–15:30：首期不参与上交所盘后固定价格交易。

## 10万元试验闸门

- 总试验敞口不超过100,000元；初始至少保留50,000元现金。
- 单笔不超过10,000元；单一股票不超过10,000元；单只股票ETF不超过20,000元。
- 同一高相关主题簇不超过30,000元；全账户的半导体集中风险必须同时展示。
- 每天最多3次新增决定；每个标的每分钟最多一次动作。
- 仅限价单；不融资、不融券、不自动追价、不做撤单循环。
- 公开行情、陈旧报价、缺失可卖数量、源分歧、断线、重复幂等键，一律拒单。
- 试验策略损失达到1,000元、连续3个完成决策亏损、两次滑点超过估计2倍，停止新增风险。
- 全账户相对日初权益损失达到1%，试验系统立即停机。
- 508033 是 REIT，单独显示为 `SEPARATE_REIT_REVIEW`，不套用股票/ETF动作模型。

## 当前模型资格

日频 `short_cycle_champion_challenger.py` 只是研究晋级门，不是明日分钟策略。当前已经加入：

- 收盘信号、次日开盘入场；
- T+1兼容持有期；
- 训练/验证/测试边界按完整持有期 purge；
- 基准排除出候选资产；
- 成本翻倍、绝对收益、信息系数和最大回撤闸门；
- 线性 ridge champion 与 CUDA MLP challenger。

它仍然只能输出 `PROMOTED_FOR_SHADOW_ONLY` 或 `NO_DEPLOY`。旧 `mean_field_jump_fbsde.py` 的同 bar 成交回测不能作为实盘证据；该文件有用户现存修改，本轮没有改动。

## 紧急停机

有 QMT 时运行：

```powershell
py "${QUANTIFICATION_ROOT}\多因子\ml\qmt_broker_cli.py" kill-switch
```

然后在券商官方客户端核对账户与持仓。停机只阻止本系统继续生成影子意图；仓库本身不会发单，也不能管理券商客户端中由人工产生的订单。
