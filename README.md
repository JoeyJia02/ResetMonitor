# ResetMonitor

在 Windows 本机运行的 Codex 额外额度重置公告监控器。

每天北京时间 **08:00 / 13:00 / 20:00**，从 Apify 采集 `@thsottiaux` 最新最多 **3 条**公开推文，用 DeepSeek 做一次批量语义分类，再通过 QQ 邮箱发送相关公告。无需在本机登录 X，无需云服务器；采集仍依赖 Apify 的上游服务。

## 本机控制台

- 服务连接、通知邮箱、后台状态和全链路测试集中在一个窗口。
- 三项凭据保存到当前 Windows 用户的凭据管理器；保存后清空输入，不回填密钥。
- 邮箱默认脱敏，可手动显示。私人地址与凭据命名空间放在忽略提交的 `config.local.json`。
- “运行一次测试”会采集最多 3 条、批量分析并发送一封测试邮件，执行前显示确认。

## 快速开始

需要 Windows 和带 Tkinter 的 Python。本项目只用 Python 标准库，当前验证环境为 Python 3.14。

1. 将 `config.example.json` 复制为 `config.local.json`，填写自己的发件与收件邮箱。已有配置不要覆盖。
2. 双击 `scripts/reset_monitor_settings.pyw`，填写 Apify Token、DeepSeek API Key、QQ 邮箱授权码并保存。
3. 在窗口中点击“运行一次测试”，确认后核对收件箱。这会产生供应商用量。
4. 按 [部署说明](DEPLOYMENT.md) 安装当前用户登录自启任务。

只看界面、不访问凭据或外部服务：

```powershell
python -B scripts/preview_settings.py
```

完整准备步骤见 [配置向导](SETUP_GUIDE.md)。

## 数据流

```text
Windows 登录 → 常驻调度 → Apify 最新最多 3 条
                              ↓
                  来源与全文校验、去重、精简字段
                              ↓
                    DeepSeek 一次批量分类
                              ↓
             POST / PRE / POSSIBLE → QQ SMTP → 邮箱
                  IRRELEVANT → 不发自动通知
```

模型输入包含轮内序号、推文时间、完整原文与必要的引用、父帖、投票上下文，不发送整份原始 JSON 或点赞等互动统计。分类结果严格校验为 `POST_RESET`、`PRE_RESET`、`POSSIBLE_RESET`、`IRRELEVANT` 四种；相对或有歧义的重置时间保留为 `null`。

## 入口与边界

| 入口 | 用途 |
| --- | --- |
| `scripts/reset_monitor_settings.pyw` | 本机设置控制台 |
| `scripts/background_monitor.py --serve` | 当前后台调度入口，自动通知相关公告 |
| `scripts/manual_test.py` | 控制台调用的手动全链路测试，共用采集、分类与邮件组件 |
| `main.py` | 默认离线；`--live` 是另一路受证据与预算约束的有限验证入口 |
| `scripts/*_candidate.py` | 实验性诊断和失败恢复候选，未接入默认后台 |

自动监控与手动测试共用外部服务组件，但调度、去重和邮件触发策略不同。不要使用旧的 `Manage-ResetMonitorTask.ps1 -Mode Live` 代替当前后台安装脚本。

电脑需保持开机、登录、联网且不休眠。错过的时点不补采；每轮仅取最新 3 条，接受漏报。当前后台对失败轮次不自动重试，也不保证失败推文下轮重新分析。SMTP 接受不等于邮件已进收件箱。每日三轮和数量限制不是全账户费用硬上限，手动测试另计。

## 开发与发布检查

```powershell
python -X utf8 -B -m unittest discover -s tests -q
git add .
python -B scripts/check_publication.py
```

测试使用模拟传输和合成数据，不调用真实 API 或 SMTP。发布检查读取 **Git 暂存区**，检查误加入的私人文件、邮箱和常见密钥格式；它不替代代码审查，也不检查远端历史。详见 [测试说明](TEST_PLAN.md) 和 [隐私说明](PRIVACY.md)。

GitHub PR 自动审查的配置步骤与项目规则见 [Code Review](CODE_REVIEW.md)。云端开关需要在仓库创建并连接后设置。

## 目录

- `monitor/`：来源、分类、邮件、凭据、状态与界面组件。
- `scripts/`：桌面、后台、验证和维护入口。
- `tests/fixtures/`：可公开的合成测试数据。
- `examples/`：离线演示输入。
- `config.example.json`：可公开的本机配置模板，不含有效地址或密钥。

本机运行记录、私人项目历史与原始采集结果不属于公开源码。项目尚未指定开源许可证。
