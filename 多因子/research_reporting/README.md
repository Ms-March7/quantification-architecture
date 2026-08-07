# Research Reporting

这是一个与 `factor_factory` 解耦的只读报告器。它只接受存在唯一终态标记的运行目录：

- `COMMITTED.json`：先校验 `manifest.json`，再校验清单登记的每个产物的字节数和 SHA-256，全部通过后才汇总。
- `ABORTED.json`：只报告技术中止证据，不读取未提交的部分候选或回测文件。

运行示例：

```powershell
py -3.11 -B -m research_reporting `
  --run research_runs\factor_factory\<run-name> `
  --output-dir research_runs\reports `
  --top-n 10
```

输出为原子替换的同名 `.json` 和中文 `.md`。报告明确区分“产物已提交”“研究门合格”和“因子已晋级”；零候选或零入选不会包装成成功。
