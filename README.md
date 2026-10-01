# 工程项目 AI 顾问 · AI Project Advisor

**把 AI 顾问装进企业微信/钉钉：项目群里正常说话，它负责记录一切、盯节点、盯回款、提醒风险、出报告。**

不是软件，没有菜单按钮——会用企业微信就会用。记录用文字/语音/图片随手发，顾问在后台把内容变成结构化档案，并且**每天主动找您一次**（早报/日报/预警）。

```
企微群 · 老板单聊 · 钉钉 · H5 网页
        │
        ▼
适配器层  adapters/{wecom_app, wecom_aibot, dingtalk, h5}
        │   同客户同库 · msg_id 去重 · 群聊才带 group_id
        ▼
统一接入层  gateway/core.py        ← 一客户一租户一 SQLite
        │
        ▼
脱敏中间层  masking.py             ← 手机号/金额/单位/人名 → 占位符，本地金库可逆
        │
        ▼
顾问引擎  advisor_engine.py
   ├─ 规则命令层（建档/节点/签证/材料/回款…）  ← 不调大模型，零 token
   ├─ 记忆图谱（项目-事件-承诺-资源方，可回溯"谁在什么时候说了什么"）
   ├─ 8 个顾问（进度/回款/变更/资料/风险/巡检/复盘/新人）
   └─ LLM 兜底（自由问答、行业知识注入）
        │
        ▼
回复 / 早报 / 日报 / 驾驶舱看板
```

## 能力

| 你能说 | 它做什么 |
|---|---|
| 「新建项目：XX大厦装修，合同80万，工期3个月，负责人王经理」 | 6 步建档 → 自动生成付款计划与节点 |
| 「钢筋验收过了吗？」 | 查档案回答（谁/何时/依据），不看资料不编 |
| 「回款情况」/「现金流怎么样」 | 应收台账 + 逾期提醒 + 催款话术（附对话依据） |
| 「签证：外墙增加保温 2.8万」 | 登记签证台账 → 结算前自动提示未签/已签/已付 |
| 「材料：XX 100台 1750元」/「材料到货」 | 材料台账 + 到货跟踪 + 名称/数量/单价解析 |
| 「有什么风险」 | 风险雷达：甲方被执行/失信、对接人变动、资源方异常 |
| 「生成周报」/「项目总结」 | 基于全量数据生成周报、复盘材料并归档 |
| 群里随手说的承诺 | 自动归档（谁/何时/原话），**换人/赖账时调得出记录** |

- **多租户**：客户各自独立库，A 项目群看不到 B 项目群；老板单聊看全局。
- **多平台**：企业微信自建应用、企微智能机器人、钉钉、H5 网页版共用同一个引擎与数据模型。
- **省 token**：命令类走规则引擎（0 token），只有自由问答/总结才调模型；知识库走关键词检索而非向量库。
- **每日主动**：早报预警 7:30、日报推送 8:00、日报生成 22:00（cron）。

## 对外红线（写进 prompt 的硬约束）

1. **只记录事实、盯节点、提醒风险、给建议** —— 不替客户做商业决策（不比价、不选型、不定价）。
2. **不自动测算时间节点** —— 只标注合同/文件里写明的、以及客户口述提供的（避免责任风险）。
3. **需签章成果由持证人签署**（借人不借资质）。
4. **数据隔离**：一租户一库；对外脱敏后才送模型。

## 目录

```
advisor_engine.py     顾问引擎（规则命令 + 记忆图谱 + LLM 兜底）
advisor_llm.py        LLM 调用封装（OpenAI 兼容；key 走环境变量）
masking.py            脱敏中间层（可逆，本地金库，fail-closed）
gateway/
  core.py             统一接入层：租户路由 / 去重 / 分发
  onboard.py          扫码接入：专属码 → 开通 → 绑定租户
  adapters/           wecom_app / wecom_aibot / dingtalk / h5
h5/                   网页版对话页 + 服务
wecom_bot.py          企业微信机器人（单租户长连接版）
advisor_alerts.py     早报预警      advisor_daily_summary.py  日报生成/推送
test_*.py             测试（不调大模型，可离线跑）
```

## 快速开始

```bash
# 引擎自带 sqlite，无第三方依赖；测试可离线跑
python3 test_advisor.py      # 27 项
python3 test_gateway.py      # 29 项（4 平台报文 → 同一引擎 → 同一库）
python3 test_masking.py      # 16 项（脱敏可逆性/不泄漏/一致性/容错）
python3 test_onboard.py      # 30 项（扫码接入/专属码/限流）
python3 test_masking_e2e.py  # 2 项（端到端 + 金库权限 600）
```

实测（2026-10-01）：**5 个测试文件、104 项断言、全部通过**。

配置（环境变量）：

| 变量 | 说明 |
|---|---|
| `ADVISOR_DB` / 租户 db 路径 | 数据文件位置（默认按租户分库） |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 模型接入（key 只走环境变量，不写死） |
| `ADVISOR_MASK=0` | 关闭脱敏中间层 |
| `ASR_FIX_DICT`（见姊妹项目） | 语音转写纠错表 |

## 文档

- [产品介绍（客户试用版）](产品介绍_客户试用版.md) · [产品方案书](产品方案书.md) · [使用说明](使用说明.md)
- [记忆图谱技术设计](记忆图谱技术设计.md) · [脱敏中间层说明](脱敏中间层_说明.md) · [部署指南](部署指南.md)

## English

An AI project advisor that lives inside WeCom/DingTalk: chat normally in a project group and it turns
messages into structured records (milestones, payments, visa/change orders, material ledgers, promises),
watches deadlines and receivables, pushes a daily briefing, and answers questions with citations back to
the original conversation. Rule-engine first (zero tokens) with an LLM fallback; a reversible masking
layer strips phones/amounts/orgs/names before anything leaves the box; one SQLite DB per tenant behind a
unified multi-platform gateway (WeCom app/bot, DingTalk, H5).

## License

MIT（见 [LICENSE](LICENSE)）。
