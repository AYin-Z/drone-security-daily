# 无人机感知与反制技术日报 Agent

每日自动检索「无人机感知探测 / 反制防御技术」相关资讯，筛选去重、分类标注后生成 HTML 日报并发送邮件。
> **背景**：本需求此前经腾讯 WorkBuddy（腾讯生态内公众号搜索）实现；本系统为脱离腾讯生态后的替代方案，
> 微信公众号检索改用**搜狗微信搜索**（低频直连）+ 人工清单 + 搜索结果自动提取 + wechat-download-api 对接四条路径。
**邮件除日报本体外，附带 Agent 从开始到结束每一步的执行记录（trace 日志），全程可追溯、去黑箱化。**

- 纯 Python 实现，无闭源依赖，可移植到任意 Linux 服务器（Python 3.9+）
- 单机一键部署：`bash install.sh`
- 所有产物（日报 HTML / trace 日志 / 邮件草稿）落盘在 `data/` 目录，随时可查

---

## 1. 功能对照（需求）

| 需求 | 实现 |
|---|---|
| 多渠道检索 | **27 个 RSS**（8 中文科技媒体 + 14 国际反无人机/防务/垂直 + arXiv）+ **9 个中文行业/政务站点直抓**（宇辰网/无人系统网/中国航空报双栏目/中国民航报/民航局要闻/电子发烧友/传感器专家网/凤凰军事）+ Google News 关键词检索 + CSDN 技术搜索 API + 搜狗微信搜索 + 公众号三条辅助路径 |
| 当天优先，不足回溯 | 优先收录执行当天文章；不足 6 篇自动回溯 48 小时并在卡片上标注实际发布日期 |
| 筛选去重 | URL 跨天去重 + 标题相似度去重（>0.86 视为重复）+ LLM 相关性判定 |
| 分类标注 | 7 类标签（探测感知 / 干扰反制 / 实战案例 / 政策法规 / 技术前沿 / 行业动态 / 国际视野），彩色标签 |
| HTML 日报 | 单文件、无 CDN、深色主色 `#1a365d`、响应式、纯 JS 分类筛选、今日焦点卡片、免责声明与生成时间戳 |
| 邮件发送 | 163/QQ 等 SMTP + 授权码；正文内嵌完整 HTML 日报；附件：`agent-trace-*.txt` 逐步执行日志 |
| **Trace 去黑箱化** | 每步（LLM 调用 / 各数据源抓取 / 去重 / 筛选 / 分类 / 渲染 / 发信）写入 JSONL（16 字段），并渲染为**人读 txt + 可视化 HTML** 双格式随邮件发送；HTML 版支持按阶段/错误筛选、关键字搜索、点击展开原始 JSON |

## 2. 目录结构

```
drone-security-daily/
├── run_daily.py             # 主入口
├── install.sh               # 一键安装（venv + 依赖 + config 初始化）
├── scheduler_setup.sh       # crontab 定时安装（时间在 config.yaml 配置）
├── config.example.yaml      # 配置模板（复制为 config.yaml）
├── requirements.txt
├── dsdaily/                 # 核心代码
│   ├── agent.py             #   分步编排（检索→筛选→分类→生成→发送）
│   ├── search.py            #   数据源层（RSS/站点/Tavily/公众号）
│   ├── llm.py               #   LLM 客户端（OpenAI 兼容 + mock 模式）
│   ├── tracelog.py          #   逐步日志（JSONL 记录 + txt/HTML 渲染）
│   ├── dashboard.py         #   管理面板（邮箱/定时/来源渠道/手动运行）
│   ├── htmlrender.py        #   HTML 日报渲染（样式规范）
│   ├── mailer.py            #   SMTP 发送 / dry-run .eml
│   └── config.py            #   配置加载
├── data/
│   ├── reports/             # 日报 HTML：drone-security-daily-YYYY-MM-DD.html
│   ├── traces/              # 执行日志：agent-trace-<run_id>.{txt,jsonl}（按运行隔离，同日多跑互不污染）
│   │                        #   + agent-trace-YYYY-MM-DD.txt（当日最新副本，每次运行覆盖）
│   ├── state/seen.json      # 历史 URL 去重状态
│   └── emails/              # dry-run 邮件草稿 .eml
└── logs/                    # 运行日志 + cron.log
```

## 3. 快速部署

```bash
# 1) 安装（Python 3.9+）
bash install.sh

# 2) 编辑 config.yaml
vim config.yaml
#    · llm.api_key        —— 必填：DeepSeek 等 OpenAI 兼容 API key
#    · smtp.user/password —— 发件邮箱 + SMTP 授权码（邮箱网页端开启 SMTP 服务生成）
#    · smtp.to            —— 收件人（当前为测试邮箱 yinz7032@qq.com，正式改为目标邮箱）

# 3) 手动试跑（推荐先 mock + dry-run，不耗 API、不发真信）
./run_daily.py --mock-llm --dry-run-mail
#    检查: data/reports/*.html 日报样式、data/traces/*.txt 执行日志、data/emails/*.eml 邮件草稿

# 4) 正式单次运行
./run_daily.py --dry-run-mail     # 先发草稿人工检查
./run_daily.py                    # 真实发送

# 5) 配置每日定时（时间在 config.yaml 的 schedule.cron 中修改，当前为占位）
bash scheduler_setup.sh
crontab -l | grep drone-security-daily   # 查看已安装的定时任务
```

> 定时时间目前为**占位待定**（默认 `0 8 * * *` = 每天 08:00，符合需求「每天」）。确认最终时间后修改
> `config.yaml` 的 `schedule.cron`，再运行 `bash scheduler_setup.sh` 生效。

## 4. 配置说明（config.yaml）

| 段 | 键 | 说明 |
|---|---|---|
| smtp | host / port / user / password | SMTP 服务器、端口(465 SSL)、账号、**授权码**（非登录密码） |
| smtp | to | 收件人列表（可多个） |
| smtp | dry_run | true=只生成 .eml；false=真实发送 |
| smtp | attach_report_html | 是否把日报 html 作为附件（默认 false，见 §6） |
| llm | base_url / api_key / model | OpenAI 兼容端点（默认 DeepSeek）。api_key 为空自动 mock |
| search | window_hours / min_articles / lookback_hours | 时间窗逻辑（当天 / 不足回溯） |
| search | keywords | 检索关键词清单（需求 Prompt 提供，可增删） |
| search | rss_feeds / sites | 数据源（可增删；站点结构变化时需调整 link_regex） |
| search | tavily_api_key | 可选：Tavily 搜索增强（免费 1000 次/月） |
| search | wechat.manual_urls | 微信公众号文章 URL 清单（二期） |
| runtime | send_email / timezone | 是否发信 / 时区 |
| schedule | cron | 定时表达式（占位待定） |

## 5. 输出与邮件内容

- **日报 HTML**：`data/reports/drone-security-daily-YYYY-MM-DD.html`
  顶部（标题/中文日期/统计摘要）→ 今日焦点（可选）→ 分类筛选按钮 → 文章卡片（彩色标签/标题链接/来源·发布时间/摘要/阅读原文）→ 底部（免责声明 + AI 生成时间戳）
- **执行日志（去黑箱化）**：`data/traces/agent-trace-<run_id>.{txt,html,jsonl}`；每次运行独立文件，`agent-trace-YYYY-MM-DD.{txt,html}` 为当日最新副本
  - `.txt`：纯文本逐步清单（邮件附件，网关友好）
  - `.html`：可视化版本（深色技术风，阶段/错误筛选、搜索、点击展开原始 JSON；默认随邮件附件，被网关拦截可设 `smtp.attach_trace_html: false`）
  - `.jsonl`：机器可读全量记录
  每条记录含：时间戳、步骤序号、阶段、动作、工具、输入/输出摘要、耗时、token、状态、错误、产物路径
- **邮件**：正文 = 日报分类分区版（邮件客户端友好）；正文末尾追加以内联样式渲染的 **Agent 执行日志可视化分区**（成功/失败徽章、逐步明细、错误标记）；
  附件 = 日报 HTML（可交互筛选版）+ 执行日志 txt + 执行日志可视化 HTML。dry-run 时在 `data/emails/` 生成 .eml 草稿

### 执行日志示例

```
============================================================
  无人机感知与反制技术日报 —— Agent 执行日志（去黑箱化记录）
============================================================
  运行 ID    : run_20260818_xxxxxx
  ...
[01] 08:00:01 prepare  generate_keywords 输入: 基础词 20 个 → 生成 12 个
[02] 08:00:04 search   rss_fetch 输入: 钛媒体 → 获得 45 条
[03] 08:00:07 search   rss_fetch 输入: sUAS News → 获得 12 条
...
[11] 08:00:30 filter   dedupe 输入: 候选 63 条 → 去重后 51 条（剔除 12）
[12] 08:00:33 llm      filter_articles 输入: 51 篇候选 → 判定 51 篇
[13] 08:00:41 llm      classify_and_summarize 输入: 8 篇 → 完成 8 篇
[14] 08:00:42 render   render_html 输入: 8 篇文章 → 已生成 drone-security-daily-2026-08-18.html [产出: ...]
[15] 08:00:43 render   render_trace 输入: ... → 已生成 agent-trace-2026-08-18.txt [产出: ...]
[16] 08:00:44 email    send_email 输入: to=[...] → 已发送至 ...
  错误: 无
```

## 6. 邮件送达注意事项（重要）

- 高校/机构邮箱（如 ppsuc.edu.cn）普遍部署反垃圾网关，**`.html` 附件可能被直接拦截**。
  因此默认策略：**正文内嵌完整日报 + 附件仅 trace 文本（.txt）**；如需日报 html 附件，
  先开启 `attach_report_html: true` 发送测试信确认可达。
- 首次投递前请**先发测试信**（`smtp.to` 填 `yinz7032@qq.com` 验证链路，再确认目标邮箱可达性）。
- **邮件客户端不支持 JS**：分类筛选按钮在邮件正文里无响应属正常（QQ/Outlook/Gmail 等均剥离脚本）；
  正文顶部有提示条。完整交互版在 `data/reports/drone-security-daily-*.html`（浏览器打开）；
  如需直接邮件附件收到交互版，开启 `smtp.attach_report_html: true`（注意 edu 网关可能拦截 .html 附件）。
- 发件频率固定（每日 1 封）、发件人固定，可显著降低进垃圾箱概率。
- QQ 邮箱有「防频繁发信」间隔限制；163/QQ 均需在网页端开启 SMTP 并生成**授权码**。

## 7. 数据源说明与二期规划

- **数据源全景（全部实测可用，2026-08）**：
  · RSS 27 个：中文科技媒体（36氪/爱范儿/IT之家/少数派/量子位/极客公园/雷锋网/钛媒体）+
    国际反无人机垂直（DroneDJ/DroneXL/DroneII/dronelife 反无人机分类(googlebot UA)/MyDefence/AeroVironment/
    sUAS News/Unmanned Airspace/The Drone Girl/Defense News/AeroTime/The War Zone/C4ISRNET/
    UK Defence Journal/The Aviationist/Defence Industry Europe/Army Times/Air Force Times）+ arXiv 信号处理
  · 站点直抓 9 个：宇辰网/无人系统网/中国航空报（低空经济+防务）/中国民航报无人机频道/民航局民航要闻/
    电子发烧友/传感器专家网/凤凰军事
  · 搜索：Google News 关键词（6 词）+ CSDN API（3 词）+ 可选 Tavily；搜狗微信搜索（低频）
  · 每轮典型候选 800+ 篇 → 关键词命中预筛 120 → LLM 严格筛选（宁缺毋滥）
  · 已排除（勿配置）：Bing（format=rss 与 API 均停用）、uav.com.cn（停站）、飞赞网（不存在）、
    通航在线（停运）、知乎（签名墙）、公安部官网（521）、新浪/网易 RSS（废弃）
- **微信公众号（三条路径可并用）**：① `wechat.manual_urls` 人工维护文章 URL 清单（最稳）；
  ② `wechat.auto_from_search` 自动把搜索结果中的 mp.weixin.qq.com 链接抓为公众号文章（默认开）；
  ③ `wechat.api_url` 对接开源 [wechat-download-api](https://github.com/tmwgsicp/wechat-download-api)
  等工具的通用 JSON 接口（`GET ?keyword=<词>&days=2`，返回含 title/url 的数组或 `{data:[...]}`）
- **说明**：Bing 新闻的 `format=rss` 已失效（返回 HTML）勿启用；民航局公告列表为 JS 动态加载，
  其内容多经新闻源转载（Google News 可覆盖）；知乎/CSDN 反爬强，建议人工跟进
- **国际源扩充**：可自行在 `rss_feeds` 增加 dronelife / C-UAS Hub 等（部分站点有 IP 风控，需实测）。

## 8. 常见问题

- **没配 API key 能跑吗？** 能。自动进入 mock 模式（规则引擎），用于流程联调；正式运行必须配置 `llm.api_key`。
- **当天没搜到文章怎么办？** 自动回溯 48 小时；仍不足时生成报告并如实标注 0 篇（宁缺毋滥）。
- **单数据源失败会中断吗？** 不会。每个源独立容错，失败记入执行日志（⚠️ 标记）。
- **如何换收件人？** 编辑 `config.yaml` 的 `smtp.to`（支持多收件人）。
- **cron 没生效？** 检查 `crontab -l`；确认机器在定时时间处于开机状态；日志见 `logs/cron.log`。
- **运行退出码**：0=成功；1=邮件等关键步骤失败（真实发送模式下缺 SMTP 凭据等），cron/监控可据此感知失败。
- **搜狗微信搜索触发验证码？** 系统检测到风控会自动跳过该词并记入执行日志（⚠️ 标记），不影响其他来源；
  用浏览器打开 weixin.sogou.com 搜索一次，复制 Cookie 到 `wechat.sogou.cookie` 即可恢复；
  若为 IP 级风控（无 Cookie 也返回 antispider），需等待约 30-60 分钟解封，期间系统自动降级（其他来源不受影响）；
  生产环境每日仅 6 次低频查询，通常不会触发；调试时请勿连续重跑；
  若长期被风控，建议改用 `wechat.api_url`（wechat-download-api）或人工清单路径。
- **同日多次运行**：每次运行 trace 独立成文件（`agent-trace-<run_id>.*`），互不污染；邮件附件为该次运行的日志。

## 9. 管理面板（部署后自定义）

启动：`source .venv/bin/activate && python3 -m dsdaily.dashboard`（默认 http://127.0.0.1:8787）

| 页面 | 能力 |
|---|---|
| 概览 | 系统状态（LLM 模式/定时/发件收件）、**手动运行一次**（真实/mock 可选）、实时日志 |
| 邮件设置 | 自定义发件邮箱/授权码/发件人/收件人（多行）/dry-run/附件开关 |
| LLM 设置 | base_url / model / api_key（脱敏显示，留空不修改） |
| 数据源渠道 | **增删 RSS 源 / 行业站点 / 公众号人工清单 / 检索关键词**、开关新闻引擎、对接 wechat-download-api |
| 定时 | 自定义 cron 表达式 + 一键安装/移除 crontab |
| 产物 | 查看/下载历史日报、执行日志（txt/html）、邮件草稿 |

安全：默认仅绑定 127.0.0.1；建议在 `config.yaml` 的 `dashboard.password` 设置访问密码；
改 `host: 0.0.0.0` 可局域网访问（务必设密码）。面板保存会自动规范化 config.yaml 格式。

## 10. 测试

- 端到端联调：`./run_daily.py --mock-llm --dry-run-mail`（真实 RSS + 规则 LLM + 邮件草稿）
- 邮件链路验证：`smtp.to` 设为 `yinz7032@qq.com`，`./run_daily.py --dry-run-mail` 检查 .eml，
  确认后去掉 `--dry-run-mail` 真实发送一封测试信。

---
*本系统由 AI 调研驱动的开发流程生成；数据源内容版权归原作者所有，仅作技术资讯参考。*
