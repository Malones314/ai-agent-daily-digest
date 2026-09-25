# AI / Agent 每日早报（GitHub Actions → 邮件）

每天早上 **07:00（北京时间）** 自动生成一封邮件，四个板块各 10 条，发到指定邮箱。全程跑在 GitHub 的机器上，**不需要任何常开设备**。

| 板块 | 内容 |
|---|---|
| 📰 AI 热点（10） | 近 24-48 小时 AI 领域要闻，中文摘要 + 原文链接 |
| 🔥 GitHub 热门 AI 项目（10） | 模型/推理/数据/多模态/RAG 等方向的活跃开源项目 |
| 🤖 Agent 热点（10） | 智能体、MCP、编码 agent、agent 安全等新闻 |
| 🛠 GitHub 热门 Agent 项目（10） | agent 框架、工具链、记忆、编排等开源项目 |

## 特性

- **零依赖**：纯 Python 标准库，CI 里不用 `pip install`
- **零成本**：公开仓库的 Actions 分钟数不计量；DeepSeek 计费约每天几分钱
- **数据源全免费**：RSS/Atom 订阅源 + Hacker News Algolia API + GitHub 公开 Search API
- **防编造**：LLM 只能从候选池里挑，选中的 URL 必须在候选池里存在，否则丢弃
- **跨天去重**：`.state/seen.json` 记录近 7 天推过的链接，写回仓库（顺带让仓库保持活跃，避免定时任务因 60 天无活动被禁用）
- **跨板块去重**：同一条新闻/仓库不会同时出现在两个板块
- **有兜底**：LLM 调用失败时用候选池按时间/热度补齐，仍然发满 40 条

## 部署（5 分钟）

### 1. 建仓库并上传

```bash
cd ai-digest-cloud
git init -b main
git add -A
git commit -m "init: ai agent daily digest"
git remote add origin https://github.com/<你的用户名>/ai-digest.git
git push -u origin main
```

> 建议建 **public** 仓库：公开仓库的 Actions 用量不计费。仓库里不含任何密钥（全部走 Secrets）。

### 2. 准备发信通道（推荐 Brevo，不碰 Gmail）

**为什么不用 Gmail**：新注册的 Gmail 账号第一次发信就来自数据中心 IP（Actions 跑在 Azure 上），容易触发谷歌风控，报 `534-5.7.9 Please log in via your web browser`。Brevo 是专业发信服务，免费额度 300 封/天，专门干这个。

1. 注册 [brevo.com](https://www.brevo.com)（免费计划）
2. **Senders, Domains & Dedicated IPs → Senders → Add a sender**，填 `你的邮箱@example.com`，去你的 Gmail 收确认信点一下完成验证（这是验证"发件地址"，**不需要新建任何邮箱**）
3. **SMTP & API → SMTP**，拿到 SMTP key（形如 `xsmtpsib-...`）

### 3. 配置 Secrets

仓库 → **Settings → Secrets and variables → Actions → New repository secret**：

| Secret | 填什么 |
|---|---|
| `DEEPSEEK_API_KEY` | 你的 DeepSeek key |
| `MAIL_USERNAME` | 你的 **Brevo 登录邮箱**（不是收件地址） |
| `MAIL_APP_PASSWORD` | 上一步拿到的 **Brevo SMTP key** |
| `MAIL_FROM` | 已验证的发件地址：`你的邮箱@example.com` |
| `MAIL_TO` | 收件地址：`你的邮箱@example.com` |

再切到同页的 **Variables** 标签，加两个：

| Variable | 值 |
|---|---|
| `SMTP_HOST` | `smtp-relay.brevo.com` |
| `SMTP_PORT` | `587` |

<details>
<summary>换其他服务商（点击展开）</summary>

| 服务商 | SMTP_HOST | SMTP_PORT | MAIL_USERNAME | MAIL_APP_PASSWORD | MAIL_FROM |
|---|---|---|---|---|---|
| Brevo | `smtp-relay.brevo.com` | `587` | Brevo 账号邮箱 | Brevo SMTP key | 已验证的发件地址 |
| Resend | `smtp.resend.com` | `465` | `resend` | Resend API key | `onboarding@resend.dev`（需先验证自有域名，否则只能发给注册邮箱） |
| Gmail | `smtp.gmail.com` | `465` | Gmail 地址 | 应用专用密码 | 同 MAIL_USERNAME |
| QQ 邮箱 | `smtp.qq.com` | `465` | QQ 邮箱 | 授权码 | 同 MAIL_USERNAME |
| 163 邮箱 | `smtp.163.com` | `465` | 163 邮箱 | 授权码 | 同 MAIL_USERNAME |

</details>

### 4. 手动验证一次

仓库 → **Actions → AI / Agent 早报 → Run workflow**：

- 先勾 **dry_run** 跑一次 → 跑完在该次运行的 Artifacts 里下载 `digest-preview` 看排版和内容
- 确认没问题后再不勾 dry_run 跑一次 → 邮箱应该收到邮件

之后每天 07:00 自动运行。

## 时间说明

- GitHub 的 cron 是 UTC：`0 23 * * *` = 北京时间 07:00
- GitHub 免费版的定时任务**可能延迟几分钟到几十分钟**（高峰期排队），这是平台特性，不是脚本问题
- 需要严格准点就用 workfow_dispatch + 外部触发器，或换自建主机

## 本地调试

```bash
DRY_RUN=1 DEEPSEEK_API_KEY=sk-xxx python digest.py
# 输出 preview.html / preview.txt，不发信
```

## 改内容

- **加/减新闻源**：改 `digest.py` 里的 `FEEDS` 列表
- **调节条数**：改 `PER_SECTION`
- **调节保留天数**：改 `SEEN_DAYS`
- **换模型**：设环境变量 `DEEPSEEK_MODEL`（默认 `deepseek-flash`）
- **换发信通道**：设 `SMTP_HOST` / `SMTP_PORT`，代码不用动

## 结构

```
digest.py                       # 全部逻辑（采集 → LLM 选编 → 渲染 → 发信）
.github/workflows/daily-digest.yml
.state/seen.json                # 自动维护：近 7 天已推链接
```

## 已知限制

- 摘要依赖 DeepSeek API；额度用尽时会退化成"只有标题+链接"仍照发
- 个别 RSS 源偶尔 429/404（脚本自动跳过，不影响整体）
- GitHub 未认证 Search API 限流 10 次/分钟；CI 里已用 `secrets.GITHUB_TOKEN` 提额
