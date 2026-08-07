# V7 Ensemble-First 因子协议（最小实现）

V7 把统计假设从“若干单因子分别通过 Validation 后再拼组合”改成“Discovery 冻结一个组合，Validation 只确认这个组合”。它不降低统计、成本、回撤或数据质量门槛，也不打开 Holdout。

## 状态流

```text
generated candidates
  -> Discovery 单因子数值门（不做单因子事件门）
  -> 预注册分数排序（分数降序，candidate_id 固定破同分）
  -> 一次确定性低相关贪心扫描
  -> 固定组件数 + 等权横截面标准化组合
  -> Discovery 组合数值门 + 一次五相位组合事件回测
  -> discovery-freeze.json（先于 Validation 写入并校验哈希）
  -> Validation：一个冻结组合、一个置换检验、一次组合事件回测
  -> 整体确认或整体否决
  -> 显式 FROZEN_AWAITING_HOLDOUT；独立命令才能开 Holdout
```

## 防止组合枚举过拟合

- `frozen_ensemble_component_count` 在配置中预注册，不能在 Discovery/Validation 根据结果改变。
- `ensemble_selection_rule` 当前只允许 `score_ordered_greedy_low_correlation_v1`。
- `maximum_ensemble_trials` 当前强制等于 `1`。
- 不进行 beam search、子集枚举、权重搜索或 Validation 重选；找不到固定数量的低相关组件就直接失败。
- 组合权重固定为等权；每个截面先对完整共同样本内的组件做 z-score。
- Validation 的假设数固定为一，BY 在 `m=1` 时等价于 `q=p`。

## 审计合同

`discovery-freeze.json` 保存并封印：

- 预注册策略与策略哈希；
- 全候选确定性顺序；
- 每个候选的分数、决策、与已选组件的绝对秩相关、拟议共同覆盖率；
- 固定组件、公式、表达式、等权权重；
- Discovery 组合指标、组合事件回测和唯一 Validation 假设数；
- `validation_component_tests_allowed=false`、`validation_reselection_allowed=false`、`holdout_was_opened=false`。

最终 manifest 继续记录该文件的 artifact SHA-256、Discovery freeze hash、组件测试数、Validation 假设数、组合指标及 Holdout 未开启状态。

## 启用条件

配置必须同时满足：

```json
{
  "protocol_version": 7,
  "discovery_first_selection": true,
  "ensemble_first_validation": true,
  "frozen_ensemble_component_count": 3,
  "maximum_ensemble_trials": 1,
  "ensemble_selection_rule": "score_ordered_greedy_low_correlation_v1"
}
```

当前提交只包含架构、最小管线分支和本地合成面板测试；没有注册正式研究尝试，也没有执行正式 Validation 或 Holdout。
