# 测试与发布核对

## 离线回归

在 Windows、带 Tkinter 的 Python 环境中运行：

```powershell
python -X utf8 -B -m unittest discover -s tests -q
```

覆盖来源数量与身份校验、长文本和上下文规范化、严格模型输出、SMTP 状态、去重、调度、凭据读写接口与实际 Tk 控件。外部传输、凭据接口和邮件发送使用模拟实现；合成数据位于 `tests/fixtures`，不依赖私人 `evidence` 文件。

恢复和诊断候选有单独测试，但候选通过测试不代表已部署到后台。

## 界面检查

```powershell
python -B scripts/preview_settings.py
```

预览使用虚构地址和内存凭据状态，无真实凭据访问、API 请求或邮件。检查不同窗口大小的布局、默认地址脱敏、保存后的输入清空，以及模拟测试结束后按钮恢复。

## GitHub 提交前

```powershell
git add .
python -B scripts/check_publication.py
git diff --cached --stat
```

检查工具只报告文件名、行号与规则名，不输出匹配的敏感值。检查目标是暂存区实际内容，修改后需重新暂存再检查。

还应从暂存区导出一份没有 `config.local.json`、运行记录和私人历史的干净源码，运行完整回归，确认可独立测试。公开截图只能使用演示模式。

## 真实验收

真实 API / SMTP 测试不属于自动回归，会使用服务额度。需要使用者在控制台明确发起，核对采集数量、分类、SMTP 回执与实际收件。后台验收必须观察后台入口的一次真实固定时点运行，不能仅凭手动邮件成功作结论。
