# 后台部署

## 当前入口

`scripts/Manage-ResetMonitorBackground.ps1` 管理专属任务 `CodexResetMonitorBackground`。任务在当前 Windows 用户登录时启动 `pythonw.exe scripts/background_monitor.py --serve`，使用普通用户权限，无控制台窗口。后台进程按固定 UTC+8 时间检查 08:00、13:00、20:00 三个时点。

安装前先按 [配置向导](SETUP_GUIDE.md) 完成真实手动测试。以下命令从项目根目录执行：

```powershell
# 预览不会注册、启动或删除任务
.\scripts\Manage-ResetMonitorBackground.ps1 -Action Preview

# 注册并启动；此后到点会自动访问服务并发出相关通知
.\scripts\Manage-ResetMonitorBackground.ps1 -Action Install

# 查看任务状态
.\scripts\Manage-ResetMonitorBackground.ps1 -Action Status
```

脚本从 PATH 寻找 Python。多版本安装时使用 `-PythonPath 'C:\Path\To\Python\pythonw.exe'` 指定真实路径。维护已有任务时应继续使用安装时的 Python 路径；脚本会检查所有权、账户、动作和工作目录，不覆盖其他任务。

停止并移除本项目任务可使用 `-Action Uninstall`。它不会删除凭据、邮箱配置或运行记录。

## 调度与通知

- 必须保持已登录、联网且不休眠；这不是系统启动前即可运行的 Windows 服务。
- 错过时点不补跑；同一时点先记录尝试，失败或发送结果不确定时不自动重试。
- 每轮 Apify 最多返回 3 条，只有未见过的推文进入一次 DeepSeek 批量分析。
- `POST_RESET`、`PRE_RESET`、`POSSIBLE_RESET` 可触发汇总通知；没有新内容或全为 `IRRELEVANT` 时不发信。
- 手动测试额外计费，并会发送测试邮件；它与后台共用采集、分类和 SMTP 组件，但通知和去重策略不同。

## 本地记录

`%LOCALAPPDATA%\CodexResetMonitor\background` 保存后台心跳、轮次回执和去重记录。相邻的 `manual-test`、`full-chain-test` 等目录保存各自的验证结果。它们可能包含邮箱、原文、服务调用标识和账单信息，仅留在本机。

控制台只展示白名单状态和时间，不把日志原文显示在窗口。后台状态过期不能单独证明某一轮是否执行，排查时应结合任务状态、轮次回执和系统事件。

## 更新与恢复

更新源码前备份本机配置和状态。已经运行的 Python 进程不会自动加载新代码；更新后台逻辑需要安排受控重启。不要删除轮次回执、去重文件或账本来强行重跑，避免重复计费或重复发信。

`scripts/background_monitor_recovery_candidate.py` 与诊断候选为实验文件，默认任务不调用它们。当前生产入口不承诺失败推文自动恢复；发布改动时应分别验证调度入口、手动入口和候选代码。

旧的 `main.py --live --scheduled-validation` 是有限验证流程，可能因来源、Token 或费用证据缺失而拒绝执行。它不等同于当前后台任务，不应通过改旧证明字段来替代后台部署。

目前没有自动日志轮转或用量总账硬限额；长期运行需定期检查本地磁盘占用及服务账单。
