## English Introduction

**AI Project Advisor** is a WeChat-based AI assistant for construction / engineering / building projects. It helps project owners and managers run projects by just chatting in WeChat groups — no software to learn, no forms to fill.

### Key Features

- **Memory Graph (Chat = Archive)**: Every word in your project group is archived automatically — promises, confirmations, payments, changes — with who/when/original message, ready as evidence for payment collection and dispute resolution
- **Multi-Group Support**: One project per group (WeChat Work group), auto-binding group ↔ project, no data interference between projects; boss chat = global view
- **Rule + LLM Hybrid Engine**: Daily recording costs zero tokens (rule-based intent recognition); complex understanding / deep diagnosis / report generation use LLM on demand (DeepSeek — cheap)
- **Risk Radar**: Litigation / key personnel changes / supplier failures — proactive alerts + action checklists (auto web-scan is a paid-tier feature)
- **8 Advisor Modules**: Progress / Payment / Change / Documentation / Risk / Inspection / Review / Training
- **Proactive Service**: Daily morning brief, deadline countdown, anomaly detection (overdue payments, missing materials, schedule slippage), one-click reports, voice/photo input

### Quick Start

```bash
# Python 3 + optional DEEPSEEK_API_KEY (in ~/.hermes/.env or env var)
python3 advisor_engine.py   # multi-group demo
python3 advisor_llm.py      # LLM enhancement test (diagnosis/report)
```

### Architecture

```
Chat (WeChat Work group)
  → Rule engine (intent recognition — 0 token)
  → Memory graph (SQLite: projects/events/group_bindings...)
  → LLM layer (DeepSeek: complex parsing/diagnosis/reports — on demand)
  → Proactive push (morning brief / alerts / reminders)
```

### Free vs Paid

- Free: 3 project groups + recording/reminders + limited daily Q&A
- Paid: more groups + deep diagnosis / reports / risk radar auto-scan

MIT License. Star & share if useful!

---

# 🏗️ 工程项目 AI 顾问（AI Project Advisor）

微信对话式工程项目管理助手——**老板不用管项目，AI 替他管**：发现问题、给出方案、自动干活。

## 核心能力

- **记忆图谱（对话即档案）**：群里说的每句话自动存档——承诺/确认/付款/变更——有据可查（清款依据）
- **多群接入**：一个项目一个群——群↔项目绑定——多项目互不干扰
- **规则 + 大模型混合**：日常记录零成本（规则引擎），深度诊断/周报按需调大模型（DeepSeek）
- **风险雷达**：官司负面/人员变动/资源方暴雷——预警+核查清单（自动扫描=收费版）
- **8 大顾问模块**：进度/回款/变更/资料/风险/巡检/复盘/新人

## 快速开始

```bash
# 依赖：Python 3 + 可选 DEEPSEEK_API_KEY（~/.hermes/.env 或环境变量）
python3 advisor_engine.py          # 跑多群演示
python3 advisor_llm.py             # 测大模型增强（诊断/周报）
```

## 使用示例

```
老板（群里）：新建项目：XX大厦办公装修，合同80万，已收30%，负责人王经理
AI：✅ 项目已创建，群已绑定——群里说的自动记录

老板（群里）：刚收了进度款20万
AI：✅ 已记录收款 20 万，累计已收 44 万（合同 80 万）

老板（群里）：甲方张经理离职了，换了个李工
AI：⚠️ 人员变动核查清单：签证补签/承诺书面化/增项补签...

老板（群里）：生成周报
AI：📋 完整周报（进展/问题/回款/计划/风险）
```

## 架构

```
对话（企业微信/群）
  → 规则引擎（意图识别——0 token）
  → 记忆图谱（SQLite：projects/events/group_bindings...）
  → 大模型层（DeepSeek：复杂句/诊断/周报——按需）
  → 主动推送（早报/预警/提醒）
```

## 免费版 vs 收费版

- 免费版：3 个项目群 + 记录/提醒 + 每天限量问答
- 收费版：更多群数 + 深度诊断/周报/风险雷达自动扫描

## 文件

- `advisor_engine.py` — 核心引擎（意图识别/图谱/多群/预警）
- `advisor_llm.py` — 大模型增强层（DeepSeek）
- `记忆图谱技术设计.md` — 数据库设计
- `产品方案书.md` — 完整产品设计

MIT License
