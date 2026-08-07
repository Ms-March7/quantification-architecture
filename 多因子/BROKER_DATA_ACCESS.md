# 券商量化行情接入核验（2026-07-15）

## 当前机器结论

本机只有财通证券普通同花顺客户端，没有可调用的 QMT/MiniQMT、PTrade 或 `xtquant`。普通客户端不等于量化 API 权限，因此当前不能声称已经拉取任何券商专属历史、L2 或逐笔数据。

`factor_factory/qmt_source.py` 已实现纯只读 QMT 行情适配器。静态探针只分别报告包、客户端、配置和权限状态，不能把本地日历缓存误称为“已连接”。历史下载调用只能登记为 `DOWNLOAD_ATTEMPTED_UNVERIFIED`，必须逐标的回读、覆盖率和字段验收后才可能标记完整。

现有普通 CLI 只保留账户快照、显式风险状态初始化、影子意图和 kill switch。批准文件重放与 `submit-order` 已移除，底层也不提供下单方法；在签名批准、账户绑定、并发锁、券商委托回查幂等和独立 live 服务完成审计前，不存在实盘下单路径。

## 官方可确认的候选

- 国金证券：官网明确公示国金 QMT 和国金 PTrade；未公开统一资产门槛。
- 国投证券：官网提供迅投量化终端、PTrade、OneQuant，要求向分支机构申请。
- 国联证券：官网提供 QMT 正式版、测试版和账号申请入口。
- 华源证券：官网同时提供 QMT 与 PTrade；PTrade 页面确认研究、回测、仿真和实盘能力。
- 财通证券：公开下载中心没有个人 QMT/PTrade 下载入口；2026 年 QMT 二期项目中标公告只能证明券商建设过系统，不能证明当前账户已开通。

QMT 官方资料确认 XtData 可以提供股票列表、2005 年以来沪深 A 股历史数据、日线/分钟/tick、实时订阅、财务和板块数据；外部 Python 仍必须依赖已启动并获授权的 QMT/MiniQMT 客户端。历史 ST、L2 和部分特色数据不能默认视为免费或已授权。PTrade 的券商页面没有公开证明可从外部本地 Python 独立批量拉数，因此暂不伪造通用 PTrade 适配器。

## 需要用户向券商确认的原句

> 请确认我的普通股票账户能否开通 Windows QMT 及 XtQuant 外部 Python；是否包含全 A 日线/分钟/tick 历史、实时全推、历史 ST/停牌/涨跌停、复权因子、财务公告日口径和 Level-2；请说明资产门槛、费用、可回溯年限以及 API 并发限制。

拿到客户端和权限后，先运行只读探针，不连接交易：

```powershell
.venv\Scripts\python.exe -c "from factor_factory.qmt_source import QmtMarketDataSource; print(QmtMarketDataSource().probe())"
```

## 官方链接

- 迅投：[数据/API 总览](https://dict.thinktrader.net/)；[外部 Python 运行依赖](https://dict.thinktrader.net/nativeApi/start_now.html)
- 国金：[对外展业系统公示](https://www.gjzq.com.cn/main/company-overview/business-sysinfo.html)
- 国投：[专业交易软件下载](https://www.essence.com.cn/service/trading/software)
- 国联：[QMT 专区](https://www.glsc.com.cn/qmt/)
- 华源：[PC 软件下载](https://www.huayuanstock.com/software/pc/index.shtml)
- 财通：[当前下载中心](https://www.ctsec.com/service/down)
