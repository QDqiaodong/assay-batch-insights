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
.venv/bin/python -m assay_insights --output run/report
.venv/bin/python -m assay_insights --output run/report
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -c 'import json; r=json.load(open("run/report/report.json")); print(r["samples"]); print(r["quality_controls"])'
```

用浏览器打开 `run/report/report.html`，无需服务。3 个普通样本组，2 个可报告：
S1 硝酸盐均值 4.6 mg/L、样本 SD 0.141421、RSD 3.074377%；S3 磷酸盐经 2 倍稀释
还原为 4 mg/L；S2 因重复样本 RSD 超限和 B2 硝酸盐 QC 回收率 150% 被阻止。
失效原因是分析结果，CLI 正常完成；输入格式/契约错误才以非零状态失败，不输出新报告。
完整口径、阈值作用域、舍入和零值规则见 `docs/metrics.md`。

## 结构与边界

- `assay_insights/input.py`：校准、样本、策略契约。
- `assay_insights/metrics.py`：Decimal 统计和批次质控传播。
- `assay_insights/report.py`：来源哈希、JSON 和 HTML。
- `data/`：校准表、仪器结果、策略和来源声明。
- `tests/`：已知数值、判定边界、重复运行、坏输入与 HTML 转义验证。

数据可离线分析；没有仪器驱动、外部账号、趋势模型或校准版本生效时间处理。
运行文件忽略，每仓独立环境。MIT 许可覆盖代码及自制数据，无第三方数据或代码复制。
