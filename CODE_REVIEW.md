# GitHub 自动 Code Review

本项目计划使用 ChatGPT / Codex 的官方 GitHub 自动审查。下面是配置说明，
这些仓库文件本身不会开启云端开关。

## 开启方法

1. 将经过隐私检查的源码推送到 GitHub 仓库。
2. 在 ChatGPT / Codex 的 Code Review 设置中确认 GitHub 连接可访问这个仓库。
3. 选择这个仓库，在 **Review code** 下启用 **Automatic review**。
4. 对本仓库选择 **All PRs**，并将触发时机设为新建 PR / 转为待审时审查。
   不需要为所有仓库修改个人全局偏好，也不需要启用额外的 Security Review。
5. 用一个有实际改动的新 PR 验证：确认出现 Codex 审查结果，而不是仅凭开关判断成功。

若当前界面的选项名称或可用触发方式不同，以账号实际设置为准。按成本优先策略，
不默认要求每次 push 都重新审查。需要再次审查某个 PR 时，可在该 PR 留言
`@codex review`。直接 push 到主分支不会形成 PR 审查流程。

## 项目规则

Codex 会读取适用于改动文件的 `AGENTS.md`：

- `monitor/AGENTS.md`：本机隐私、分类输入与输出完整性、手动测试可用性。
- `scripts/AGENTS.md`：每轮调用上限、重复计费、调度入口一致性与通知结果。

本机根目录的任务地图绑定说明不提交到 GitHub；上述两份公开规则不含私人绑定、
邮箱或密钥。审查规则不能代替离线测试、真实入口验收或人工合并决策。

官方说明：[Review GitHub pull requests with Codex](https://learn.chatgpt.com/docs/third-party/github)。
