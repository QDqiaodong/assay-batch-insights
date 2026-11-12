# Assay Batch Insights

为虚构水质实验室把仪器信号转换为带有质控依据的批次分析报告。已实现按分析物校准、稀释还原、
重复样本均值/样本标准差/RSD、QC 回收率和样本可报告性判断，输出可核验 JSON 及独立 HTML。
这不是医疗或监管判断工具，数据与阈值全部为明确标注的合成演示。

## 安装

Python 3.11+；无第三方运行依赖。构建依赖已锁定，首次安装需网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
```

## 运行与验证

在根目录运行，`run/report` 不存在即为首次干净演示。同命令可重放生成一致报告。

```sh
# 旧格式（schema_version=1，原有行为不变）
.venv/bin/python -m assay_insights --output run/report
.venv/bin/python -m assay_insights --output run/report
# 新格式：按测量时间选择校准版本（schema_version=2）
.venv/bin/python -m assay_insights \
  --calibrations data/timed_calibrations.csv \
  --results data/timed_results.csv \
  --output run/timed
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -c 'import json; r=json.load(open("run/report/report.json")); print(r["samples"]); print(r["quality_controls"])'
```

用浏览器打开 `run/report/report.html` 或 `run/timed/report.html`，无需服务。

旧格式仍是 3 个普通样本组，2 个可报告：S1 硝酸盐均值 4.6 mg/L、样本 SD
0.141421、RSD 3.074377%；S3 磷酸盐经 2 倍稀释还原为 4 mg/L；S2 因重复样本 RSD
超限和 B2 硝酸盐 QC 回收率 150% 被阻止。

新示例 `data/timed_*.csv` 在 2026-03-01T12:00:00Z 切换硝酸盐校准（旧
`N-OLD` slope=2/intercept=1 → 新 `N-NEW` slope=4/intercept=1），展示：

- 边界：11:59:59Z 用 `N-OLD`，恰为 12:00:00Z 用 `N-NEW`；不同偏移的同一绝对时刻
  归一化后选择同一版本。
- 数值反例：signal=21、dilution=1 在旧版本得到 10 mg/L，在新版本得到 5 mg/L。
- S1 只用旧版本，旧版本 QC 100% 通过，即使新版本 QC（Q2）只有 75% 失败，S1 仍可报告。
- S2、S8 重复测量跨版本但浓度都为 10 mg/L、RSD 0，仍以
  `mixed_calibration_versions` 拒绝报告。
- S4 只用新版本，因 `qc_failed:N-NEW` 不可报告；B2/S7 需要新版本 QC 但缺失，
  旧版本 QC 不能补足（`missing_qc:N-NEW`）。
- S3 同时保留跨版本、RSD 超限和新版本 QC 失败三个原因。

失效原因是分析结果，CLI 正常完成；输入格式/契约错误（重叠、非法时间、无覆盖、
新旧混用等）才以非零状态失败，并在写输出前完成校验，不改变已有报告。
完整口径、阈值作用域、舍入和零值规则见 `docs/metrics.md`。

## 结构与边界

- `assay_insights/input.py`：校准、样本、策略契约；按完整表头识别新旧两套格式。
- `assay_insights/metrics.py`：Decimal 统计和按校准版本隔离的批次质控传播。
- `assay_insights/report.py`：来源哈希、schema 1/2 JSON 和 HTML。
- `data/`：旧校准表、仪器结果与策略，以及新的按时间校准示例和来源声明。
- `tests/`：已知数值、版本边界、契约错误、重复运行、坏输入、CLI 子进程端到端与
  HTML 转义验证。

数据可离线分析；没有仪器驱动、外部账号或趋势模型。校准版本只按测量时间选择，
不增加 QC 前后夹持或时间窗口规则。运行文件忽略，每仓独立环境。MIT 许可覆盖代码
及自制数据，无第三方数据或代码复制。所有数据与阈值均为合成演示，不构成医疗或
监管有效性声明。
