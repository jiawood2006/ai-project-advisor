#!/usr/bin/env python3
"""
工程项目 AI 顾问 · 核心引擎（MVP v0.1）
记忆图谱底座：对话 → 意图识别 → 结构化提取 → 入库 → 查询/预警
存储：SQLite（~/wiki/ai-project-advisor/advisor.db）
"""
import sqlite3, os, re, json
from datetime import datetime, timedelta

try:
    import advisor_llm
    LLM_AVAILABLE = True
except ImportError:
    LLM_AVAILABLE = False

DB_PATH = os.path.expanduser("~/wiki/ai-project-advisor/advisor.db")

def set_db_path(path):
    """多租户：切换当前数据库文件（每客户一个独立库）"""
    global DB_PATH
    DB_PATH = os.path.expanduser(path)

# 免费版群数上限（收费版可调大——配置项）
MAX_GROUPS = 3

# 风险雷达：自动网络扫描（企查查/执行网/新闻）为收费版功能——暂不启用
# （手动录入的风险不受影响：老板说"XX公司有官司"→ 记录 orgs.risk_level）

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  client TEXT, contract REAL DEFAULT 0, received REAL DEFAULT 0,
  deadline TEXT, stage TEXT, manager TEXT, status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS persons (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT, org TEXT,
  project_id INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS orgs (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT,
  risk_level TEXT DEFAULT 'low', last_checked TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
  event_type TEXT NOT NULL, who TEXT NOT NULL, subject TEXT NOT NULL,
  amount REAL, due_date TEXT, status TEXT DEFAULT 'open',
  source TEXT, created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, project_id INTEGER, type TEXT,
  severity TEXT DEFAULT 'info', content TEXT,
  status TEXT DEFAULT 'open', created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS glossary (
  id INTEGER PRIMARY KEY, term TEXT NOT NULL, meaning TEXT,
  project_id INTEGER, created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS group_bindings (
  id INTEGER PRIMARY KEY,
  group_id TEXT UNIQUE,           -- 企业微信群 ID（唯一）
  project_id INTEGER,             -- 绑定项目（NULL=未绑定）
  binding_type TEXT DEFAULT 'project',  -- project/boss
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS project_docs (
  id INTEGER PRIMARY KEY,
  project_id INTEGER,
  doc_name TEXT,                  -- 资料名（合同/图纸/付款节点...）
  doc_type TEXT DEFAULT 'base',   -- base(基础)/node(节点)
  status TEXT DEFAULT 'missing',  -- missing/provided
  required_by TEXT,               -- 要求时间（阶段）
  provided_at TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS project_milestones (
  id INTEGER PRIMARY KEY,
  project_id INTEGER,
  stage TEXT,                     -- 阶段名
  plan_date TEXT,                 -- 计划日期
  status TEXT DEFAULT 'pending',  -- pending/done/overdue
  note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payment_schedule (
  id INTEGER PRIMARY KEY,
  project_id INTEGER,
  stage TEXT,                     -- 付款节点：预付款/进度款/结算款/质保金
  ratio REAL,                     -- 比例（0-1）
  amount REAL,                    -- 金额（元）
  due_date TEXT,                  -- 应到日期
  status TEXT DEFAULT 'pending',  -- pending/paid/overdue
  paid_date TEXT DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS visa_records (
  id INTEGER PRIMARY KEY,
  project_id INTEGER,
  title TEXT,                     -- 变更/签证内容
  amount REAL,                    -- 金额（元）
  status TEXT DEFAULT 'pending',  -- pending(口头未签)/signed/paid
  note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS material_ledger (
  id INTEGER PRIMARY KEY,
  project_id INTEGER,
  name TEXT,                      -- 材料名
  brand TEXT,                     -- 品牌
  spec TEXT,                      -- 规格
  qty REAL DEFAULT 0,
  unit TEXT DEFAULT '',
  price REAL DEFAULT 0,           -- 单价
  arrived INTEGER DEFAULT 0,      -- 0未到/1已到
  note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS resources (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,             -- 材料商/施工队/分包商名
  rtype TEXT,                     -- supplier/team/subcontractor
  contact TEXT, price TEXT, terms TEXT,
  rating INTEGER DEFAULT 3,       -- 信誉 1-5
  note TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS resource_events (
  id INTEGER PRIMARY KEY,
  resource_id INTEGER, event_type TEXT,  -- order/arrive/shortage/issue
  project_id INTEGER, content TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
"""

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    # 兼容旧库：补引导字段
    try:
        db.execute("ALTER TABLE project_docs ADD COLUMN guide_order INTEGER")
    except sqlite3.OperationalError:
        pass
    # 兼容旧库：签证/材料台账补时间列
    try:
        db.execute("ALTER TABLE visa_records ADD COLUMN created_at TEXT DEFAULT (datetime('now','localtime'))")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE material_ledger ADD COLUMN created_at TEXT DEFAULT (datetime('now','localtime'))")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE projects ADD COLUMN ptype TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE project_docs ADD COLUMN guide_done INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    return db

# ---------- 意图识别（规则——不耗大模型） ----------
INTENT_RULES = [
    ("payment_received", ["收到", "到账", "回款", "付款", "付了", "打了", "给了", "收款", "收了", "结款"]),
    ("issue",           ["有问题", "出问题", "卡住", "延误", "没到", "还没到", "缺", "拖延", "出事了", "停工", "没来", "摔", "受伤", "事故", "流血", "砸"]),
    ("change",          ["变更", "增项", "加钱", "追加", "改方案", "口头同意", "口头", "先干着"]),
    ("promise",         ["答应", "承诺", "说好", "保证", "同意", "确认", "没问题", "可以", "ok", "后面好说", "到时候再说", "回头再说"]),
    ("signoff",         ["签证", "签认", "签字", "确认单", "联系单"]),
    ("person_change",   ["换人", "离职", "走了", "调走", "换了", "辞职", "不干了"]),
    ("progress",        ["进度", "完成", "干了", "进场", "施工", "验收", "浇筑", "开工", "竣工"]),
    ("query",           ["什么情况", "怎么样", "多少钱", "几个项目", "查", "问一下", "看看", "汇报", "情况"]),
    ("delivery",        ["到货", "交货", "材料到", "进场了", "到了"]),
]

def classify_intent(msg):
    msg = msg.lower()
    # 查询优先（含"？"或明确查询词）
    if "?" in msg or "？" in msg or any(k in msg for k in ["什么情况", "怎么样", "多少钱", "汇报"]):
        return "query"
    for intent, keywords in INTENT_RULES:
        if any(k in msg for k in keywords):
            return intent
    return "record"  # 默认记录

# ---------- 结构化提取（规则+正则——MVP） ----------
def extract_amount(msg):
    m = re.search(r'(\d+(?:\.\d+)?)\s*(万|元|块)', msg)
    if m:
        v = float(m.group(1))
        return v * 10000 if m.group(2) == "万" else v
    return None

def extract_project(msg, projects, current_pid=None):
    """识别项目：消息明确提到→用它；否则用当前（群绑定）——不错误覆盖"""
    for p in projects:
        if p["name"][:2] in msg or p["name"] in msg:
            return p
    if current_pid:
        for p in projects:
            if p["id"] == current_pid:
                return p
    return projects[0] if projects else None

def bind_project(db, msg, group_id):
    """群里绑定项目：绑定项目：项目名"""
    m = re.search(r'绑定项目[：: ]*\s*(.+)', msg)
    if not m or not group_id:
        return "⚠️ 用法（在项目群里发）：绑定项目：项目名"
    # 群数限制检查（已绑定当前群的跳过）
    existing = db.execute("SELECT project_id FROM group_bindings WHERE group_id=?", (group_id,)).fetchone()
    if not existing or not existing["project_id"]:
        cnt = db.execute("SELECT COUNT(*) FROM group_bindings WHERE binding_type='project'").fetchone()[0]
        if cnt >= MAX_GROUPS:
            return f"⚠️ 免费版最多 {MAX_GROUPS} 个项目群——已达上限。升级解锁更多群。"
    name = m.group(1).strip()[:30]
    proj = db.execute("SELECT * FROM projects WHERE name LIKE ?", (f"%{name}%",)).fetchone()
    if not proj:
        cur = db.execute("INSERT INTO projects (name,stage) VALUES (?,?)", (name, "新项目"))
        db.execute("INSERT OR REPLACE INTO group_bindings (group_id, project_id) VALUES (?,?)",
                   (group_id, cur.lastrowid))
        db.commit()
        return (f"✅ 已创建并绑定项目「{name}」——群里说的自动记入该项目。\n"
                "可补充：合同XX万 / 工期X个月 / 负责人XX")
    db.execute("INSERT OR REPLACE INTO group_bindings (group_id, project_id) VALUES (?,?)",
               (group_id, proj["id"]))
    db.commit()
    return f"✅ 群已绑定项目「{proj['name']}」——群里说的自动记入该项目档案"

def create_project(db, msg, group_id):
    """群里创建项目：新建项目：XXX，合同80万，工期3个月，负责人王经理，已收30%"""
    m = re.search(r'新建项目[：: ]*\s*(.+)', msg)
    if not m:
        return "⚠️ 用法：新建项目：XXX，合同80万，工期3个月，负责人王经理"
    # 群数限制检查
    if group_id:
        existing = db.execute("SELECT project_id FROM group_bindings WHERE group_id=?", (group_id,)).fetchone()
        if not existing or not existing["project_id"]:
            cnt = db.execute("SELECT COUNT(*) FROM group_bindings WHERE binding_type='project'").fetchone()[0]
            if cnt >= MAX_GROUPS:
                return f"⚠️ 免费版最多 {MAX_GROUPS} 个项目群——已达上限。升级解锁更多群。"
    text = m.group(1)
    name = re.split(r'[，,。]', text)[0].strip()[:30]
    contract = extract_amount(text) or 0
    # 已收：百分比 或 金额
    received = 0
    m2 = re.search(r'已收[：:]?\s*(\d+(?:\.\d+)?)\s*[%％]', text)
    if m2 and contract:
        received = contract * float(m2.group(1)) / 100
    else:
        m3 = re.search(r'已收[：:]?\s*(\d+(?:\.\d+)?)\s*万', text)
        if m3:
            received = float(m3.group(1)) * 10000
    m4 = re.search(r'负责人[：:]?\s*([\u4e00-\u9fa5]{1,4})', text)
    manager = m4.group(1) if m4 else ""
    m5 = re.search(r'工期[：:]?\s*(\d+(?:\.\d+)?)\s*个月', text)
    months = m5.group(1) if m5 else None
    cur = db.execute("INSERT INTO projects (name,contract,received,manager,stage) VALUES (?,?,?,?,?)",
                     (name, contract, received, manager, "启动"))
    pid = cur.lastrowid
    ptype = infer_ptype(name)
    if ptype:
        db.execute("UPDATE projects SET ptype=? WHERE id=?", (ptype, pid))
    if group_id:
        db.execute("INSERT OR REPLACE INTO group_bindings (group_id, project_id) VALUES (?,?)", (group_id, pid))
    # 引导式建档（6 步——guide_order 顺序）
    for i, doc in enumerate(ONBOARDING_DOCS, 1):
        db.execute("INSERT INTO project_docs (project_id, doc_name, doc_type, required_by, guide_order, guide_done) VALUES (?,?,?,?,?,?)",
                   (pid, doc, "base", "项目启动", i, 1 if (i == 3 and months) else 0))
    resp = f"✅ 项目已创建：「{name}」合同 {contract/10000:.1f} 万"
    if received:
        resp += f"，已收 {received/10000:.1f} 万"
    resp += "。"
    if months:
        resp += f"工期 {months} 个月。"
        ms = generate_milestones(db, pid, float(months), ptype=ptype)
        resp += f"\n\n📅 **已生成节点计划**（按 {months} 个月" + (f"，检测到**{ptype}工程**流程" if ptype else "") + "）：\n"
        resp += "\n".join(f"· {s}：{d}" for s, d in ms)
        resp += "\n（发「节点：完成 施工实施」更新状态）"
    else:
        resp += "\n\n⚠️ 缺少工期计划——节点无法确认（建档第 3 步可补）。"
    db.commit()
    resp += "\n\n🎯 **开始基础资料建档**（我一步步引导，答一步走一步）：\n"
    resp += onboarding_next(db, pid)
    return resp

# ---------- 引导建档 ----------
ONBOARDING_HINTS = {
    1: "发文件，或说：合同80万，月结",
    2: "发文件即可（施工图/效果图/节点图）",
    3: "如：工期3个月（我会自动生成节点计划）",
    4: "如：首款30%，进度款按月，尾款10%",
    5: "如：XX建材、XX施工队",
    6: "如：负责人王经理，甲方对接人李工",
}
ONBOARDING_DOCS = ["合同/中标通知书", "施工图纸", "工期计划/节点", "付款节点（比例%）", "供应商/施工队名单", "负责人/对接人"]

def match_onboarding(msg):
    """把客户回复匹配到建档步骤"""
    if any(k in msg for k in ["工期", "个月"]):
        return 3
    if any(k in msg for k in ["合同", "中标"]):
        return 1
    if any(k in msg for k in ["图纸"]):
        return 2
    if any(k in msg for k in ["付款", "首款", "进度款", "尾款", "%", "％"]):
        return 4
    if any(k in msg for k in ["供应商", "施工队", "材料商", "劳务"]):
        return 5
    if any(k in msg for k in ["负责人", "对接人", "经理"]):
        return 6
    return None

def onboarding_status(db, pid):
    """建档进度"""
    if not pid:
        return "⚠️ 请先在项目群里操作"
    rows = [dict(r) for r in db.execute("SELECT * FROM project_docs WHERE project_id=? AND doc_type='base' ORDER BY guide_order", (pid,))]
    if not rows:
        return "该项目没有建档任务"
    done = sum(1 for r in rows if r.get("guide_done"))
    lines = [f"🎯 基础资料建档进度（{done}/{len(rows)}）："]
    for r in rows:
        mark = "✅" if r.get("guide_done") else "⏳"
        lines.append(f"  {mark} {r['doc_name']}")
    if done < len(rows):
        nxt = next((r for r in rows if not r.get("guide_done")), None)
        if nxt:
            lines.append(f"\n下一步：第 {nxt['guide_order']} 步【{nxt['doc_name']}】——{ONBOARDING_HINTS.get(nxt['guide_order'], '')}")
    else:
        lines.append("\n🎉 基础资料建档完成！项目已全面受管——之后正常干活说话即可。")
    return "\n".join(lines)

def onboarding_next(db, pid):
    """引导下一步"""
    nxt = db.execute("SELECT * FROM project_docs WHERE project_id=? AND doc_type='base' AND guide_done=0 ORDER BY guide_order LIMIT 1", (pid,)).fetchone()
    if not nxt:
        return "🎉 **基础资料建档完成！**\n项目已全面受管：节点提醒/回款跟踪/资料要求/风险预警全部就绪。\n之后正常干活说话即可——AI 自动记录。"
    return (f"**第 {nxt['guide_order']} 步**：请提供【{nxt['doc_name']}】——"
            f"{ONBOARDING_HINTS.get(nxt['guide_order'], '')}\n（发「建档」随时查看进度）")

def onboarding_complete_step(db, msg, pid, step_no):
    """完成建档某步并引导下一步"""
    doc = db.execute("SELECT * FROM project_docs WHERE project_id=? AND guide_order=?", (pid, step_no)).fetchone()
    if not doc:
        return onboarding_next(db, pid)
    db.execute("UPDATE project_docs SET guide_done=1, status='provided', provided_at=datetime('now','localtime') WHERE id=?",
               (doc["id"],))
    extra = ""
    if step_no == 3:
        m = re.search(r'(\d+(?:\.\d+)?)\s*个月', msg)
        if m:
            db.execute("DELETE FROM project_milestones WHERE project_id=?", (pid,))
            ms = generate_milestones(db, pid, float(m.group(1)))
            extra = "📅 节点计划已生成：" + "，".join(f"{n} {d}" for n, d in ms[:3]) + "..."
    if step_no == 4:
        # 付款节点（比例%）→ 生成回款计划表
        extra = build_payment_schedule(db, pid, msg)
    db.commit()
    return f"✅ 【{doc['doc_name']}】已确认。{extra}\n\n下一步：\n" + onboarding_next(db, pid)

def handle_message(msg, who="老板", project_id=None, group_id=None):
    """处理一条消息（支持多群）：群ID → 绑定项目 → 记录/响应"""
    db = get_db()
    try:
        return _handle(db, msg, who, project_id, group_id)
    finally:
        db.close()

def _handle(db, msg, who, project_id, group_id):
    # ---- 群 → 项目映射 ----
    if group_id:
        binding = db.execute("SELECT * FROM group_bindings WHERE group_id=?", (group_id,)).fetchone()
        if binding and binding["project_id"]:
            project_id = binding["project_id"]

    # ---- 绑定命令（群里）----
    if "绑定项目" in msg:
        return bind_project(db, msg, group_id)
    # ---- 新建项目命令 ----
    if "新建项目" in msg:
        return create_project(db, msg, group_id)

    # ---- 未绑定群：自由问答（不拦截）----
    if group_id and project_id is None:
        hint = "\n\n💡 发「新建项目：XXX，合同XX万，工期X个月，负责人XX」或「绑定项目：XXX」，即可开始项目记录"
        if LLM_AVAILABLE:
            try:
                resp = advisor_llm.chat([{"role": "system", "content": "你是工程顾问 AI 助手。客户还未绑定项目。请直接回答客户的问题（工程/施工/管理类均可），回答简洁专业。若客户想记录项目内容，提示先发「新建项目：项目名，合同金额，工期，负责人」。"}, {"role": "user", "content": msg}])
                if resp and not resp.startswith("__LLM_ERROR__"):
                    return resp  # 纯 LLM 回答（不再附加绑定提示）
            except Exception:
                pass
        return ("我是工程顾问 AI——目前还未绑定项目。\n"
                "· 新建项目：「新建项目：XXX，合同80万，工期3个月，负责人王经理」\n"
                "· 绑定项目：「绑定项目：项目名」")

    # ---- 资料清单命令 ----
    if ("资料" in msg) and ("提供资料" in msg):
        return provide_doc(db, msg, project_id)
    if ("资料" in msg) and any(k in msg for k in ["缺", "还差", "清单", "要求", "要提供"]):
        return docs_status(db, project_id)
    if msg.strip() in ("资料", "什么资料", "要什么资料"):
        return docs_status(db, project_id)
    # ---- 节点命令（排除"付款节点"——那是建档步骤4的付款条款，不能当节点计划查询）----
    if "节点" in msg and any(k in msg for k in ["查", "看", "状态", "计划", "清单", "进度"]) and not any(k in msg for k in ["付款", "支付"]):
        return milestones_status(db, project_id)
    if "节点：完成" in msg or "节点:完成" in msg:
        return milestone_done(db, msg, project_id)
    if "工期" in msg and "个月" in msg:
        return add_milestones(db, msg, project_id)
    # ---- 建档命令 ----
    if "建档" in msg:
        return onboarding_status(db, project_id)
    # ---- ⑧ 记忆图谱：回忆/查证（提前于建档拦截——避免"回忆合同"被误判为提供合同）----
    if any(k in msg for k in ["回忆", "查一下当时", "之前说过", "谁说的", "原话"]):
        return memory_recall(db, project_id, msg)

    # ---- ② 签证台账 / ③ 材料台账：登记/查询/更新（提前于顾问与建档——建档中也可用）----
    if (msg.startswith("签证：") or msg.startswith("签证:")) or any(k in msg for k in ["签证清单", "签证记录", "签证台账", "签证状态"]):
        return visa_cmd(db, msg, project_id)
    if (msg.startswith("材料：") or msg.startswith("材料:")) or any(k in msg for k in ["材料清单", "材料台账", "材料到货", "材料进场", "材料记录", "材料明细"]):
        return material_cmd(db, msg, project_id)

    # ---- ⑦ 资源方管理：登记/到货/缺货/查询（提前于建档拦截）----
    if msg.startswith("材料商") or msg.startswith("施工队") or msg.startswith("分包商"):
        return resource_cmd(db, msg, project_id)

    # ---- 顾问命令（风险/进度/回款/经营等）提前于建档拦截——建档中也要专业分析 ----
    if any(k in msg for k in ["风险分析", "有什么风险", "风险顾问", "会不会有风险", "哪里危险"]):
        return llm_advisor(project_id, "风险", msg)
    if any(k in msg for k in ["进度分析", "进度怎么样", "进度如何", "会不会延期", "进度顾问"]):
        return llm_advisor(project_id, "进度", msg)
    if any(k in msg for k in ["回款分析", "回款情况", "回款顾问", "催款", "应收", "收了多少", "回款周期", "回款时间", "回款多久", "什么时候回款", "回款进度"]):
        return llm_advisor(project_id, "回款", msg)
    if any(k in msg for k in ["盈利预测", "能赚多少", "利润", "现金流", "资金缺口", "接单评估", "这个单能接吗"]):
        return llm_biz(project_id, msg)
    # ⑤ 行业知识库：类别清单（精确命令提前——建档中也能查）
    if msg.strip() in ("知识库", "行业知识", "有什么知识", "知识库有哪些"):
        return kb_list()

    # ---- 建档中：资料匹配优先（合同/图纸/工期等建档步骤不能被 LLM 吞掉）----
    if project_id:
        _pending = db.execute("SELECT * FROM project_docs WHERE project_id=? AND doc_type='base' AND guide_done=0 ORDER BY guide_order LIMIT 1", (project_id,)).fetchone()
        if _pending:
            _step_no = match_onboarding(msg)
            if _step_no:
                return onboarding_complete_step(db, msg, project_id, _step_no)
            if ("提供资料" in msg) or ("已提供" in msg):
                return provide_doc(db, msg, project_id)

    # ---- LLM 智能理解优先（核心：自由对话，不被框架限制）----
    # 一次调用：判断意图 + 直接生成回答；命令类分发到专业模块
    if LLM_AVAILABLE and project_id:
        try:
            import advisor_llm
            _p = dict(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone() or {})
            _ev = db.execute("SELECT event_type,subject,amount,due_date FROM events WHERE project_id=? ORDER BY id DESC LIMIT 10", (project_id,)).fetchall()
            _ev_txt = "\n".join(f"- [{e['event_type']}]{e['subject']}" + (f"（{e['amount']/10000:.1f}万）" if e['amount'] else "") + (f" 应到{e['due_date']}" if e['due_date'] else "") for e in _ev) or "（暂无记录）"
            _ms = db.execute("SELECT stage,plan_date,status FROM project_milestones WHERE project_id=? ORDER BY plan_date", (project_id,)).fetchall()
            _ms_txt = "\n".join(f"- {m['stage']} {m['plan_date']}（{m['status']}）" for m in _ms) or "（暂无节点）"
            _ct = db.execute("SELECT source FROM events WHERE project_id=? AND event_type='contract' ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
            _ct_txt = (_ct["source"][:1200] if _ct and _ct["source"] else "（无合同文本——客户可发合同文件）")
            kb = kb_search(msg, limit=2)
            kb_txt = "\n".join(f"- [{c}]{t}：{content[:150]}" for _, c, t, content in kb) if kb else "（无匹配知识）"
            sys_p = f"""你是资深工程项目顾问（20年经验），服务客户「{_p.get('name','本项目')}」。项目数据：合同 {(_p.get('contract') or 0)/10000:.1f} 万，已收 {(_p.get('received') or 0)/10000:.1f} 万。
【合同文本】（必须基于此回答，引用具体条款/金额/日期）：
{_ct_txt}
项目记录：{_ev_txt}
节点计划：{_ms_txt}
行业知识（相关时引用并注明出处）：
{kb_txt}
客户发来消息。请【基于合同文本和项目数据正面回答】，规则：
- 【禁止编造】只能引用上方提供的数据（合同/记录/节点），没有的信息不能说"有"，不知道就明说不知道
- 必须引用合同里的具体条款/金额/日期/甲方信息，禁止说套话空话
- 数据不足时明说"合同未体现XX/记录中没有XX"，并给出建议怎么确认
- 报告事实（收款/变更/问题）→ 确认 + 关键提醒（如变更要签证）
- 回答像真顾问，直接给结论。只输出回答正文。"""
            resp = advisor_llm.chat([{"role": "system", "content": sys_p}, {"role": "user", "content": msg}], temperature=0.3, max_tokens=500)
            if resp and not resp.startswith("__LLM_ERROR__"):
                # 业务事实尽量落库
                _biz = classify_intent(msg)
                if _biz != "record":
                    try:
                        db.execute("INSERT INTO events (project_id,event_type,who,subject,source) VALUES (?,?,?,?,?)",
                                   (project_id, _biz, who, msg[:60], msg))
                        db.commit()
                    except Exception:
                        pass
                return resp
        except Exception:
            pass

    # ---- 业务消息优先：建档引导不拦截收款/承诺/变更/问题/进度等 ----
    _biz_intent = classify_intent(msg)
    _is_biz = _biz_intent not in ("record",)
    # ---- 引导模式（建档未完成——只拦非业务消息）----
    if project_id and not _is_biz:
        pending = db.execute("SELECT * FROM project_docs WHERE project_id=? AND doc_type='base' AND guide_done=0 ORDER BY guide_order LIMIT 1", (project_id,)).fetchone()
        if pending:
            step_no = match_onboarding(msg)
            if step_no:
                return onboarding_complete_step(db, msg, project_id, step_no)
            if ("提供资料" in msg) or ("已提供" in msg):
                return provide_doc(db, msg, project_id)
            # 建档进行中：先认真回答用户问题（顾问式），建档提示只放最后一行——不喧宾夺主
            try:
                import advisor_llm
                pname = db.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
                pname = pname["name"] if pname else "本项目"
                # 带项目数据（合同/已收/应收/节点）——让回答有的放矢
                _p = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                _p = dict(_p) if _p else {}
                _ev = db.execute("SELECT event_type,subject,amount,due_date FROM events WHERE project_id=? ORDER BY id DESC LIMIT 8", (project_id,)).fetchall()
                _ev_txt = "\n".join(f"- [{e['event_type']}]{e['subject']}" + (f"（{e['amount']/10000:.1f}万）" if e['amount'] else "") + (f" 应到{e['due_date']}" if e['due_date'] else "") for e in _ev) or "（暂无记录）"
                sys_p = f"""你是资深工程项目顾问，服务客户「{pname}」。项目数据：合同 {(_p.get('contract') or 0)/10000:.1f} 万，已收 {(_p.get('received') or 0)/10000:.1f} 万。
项目记录：{_ev_txt}
客户发来消息。请【必须正面回答客户的问题】——回款/进度/风险/施工问题都要给出明确答案（有数据用数据，没数据给判断方法）。
回答要专业、直接、给结论。不要只回复"已记录"，不要反问建档。"""
                resp = advisor_llm.chat([{"role": "system", "content": sys_p}, {"role": "user", "content": msg}], temperature=0.3, max_tokens=500)
                if resp and not resp.startswith("__LLM_ERROR__"):
                    return f"💬 {resp}\n\n_（建档进行中：第 {pending['guide_order']} 步——发「建档」可查看进度）_"
            except Exception:
                pass
            return (f"建档进行中——当前：**第 {pending['guide_order']} 步**【{pending['doc_name']}】\n"
                    f"{ONBOARDING_HINTS.get(pending['guide_order'], '')}\n"
                    f"（发「建档」查看进度）")

    projects = [dict(r) for r in db.execute("SELECT * FROM projects WHERE status='active'")]
    proj = extract_project(msg, projects, project_id)
    pid = proj["id"] if proj else project_id
    amount = extract_amount(msg)

    # 命令词：诊断/周报/复盘 → 大模型生成
    if any(k in msg for k in ["诊断", "分析一下", "帮我看看这个项目", "亏了", "复盘", "哪里亏", "哪亏", "为什么亏"]):
        return llm_diagnose(pid)
    if any(k in msg for k in ["生成周报", "周报", "写周报", "出一份周报"]):
        return llm_report(pid)
    # ⑤ 八大顾问模块：进度/回款/变更/资料/风险/巡检/新人
    if any(k in msg for k in ["进度分析", "进度怎么样", "进度如何", "会不会延期", "进度顾问"]):
        return llm_advisor(pid, "进度", msg)
    if any(k in msg for k in ["回款分析", "回款情况", "回款顾问", "催款", "应收", "收了多少"]):
        return llm_advisor(pid, "回款", msg)
    if any(k in msg for k in ["变更分析", "变更顾问"]):
        return llm_advisor(pid, "变更", msg)
    if any(k in msg for k in ["资料分析", "资料顾问", "资料全吗", "资料齐"]):
        return docs_status(db, project_id)
    if any(k in msg for k in ["风险分析", "有什么风险", "风险顾问", "会不会有风险", "哪里危险"]):
        return llm_advisor(pid, "风险", msg)
    if any(k in msg for k in ["巡检", "检查清单", "验收清单"]):
        return llm_advisor(pid, "巡检", msg)
    if any(k in msg for k in ["新人", "怎么干", "流程规范", "新手", "培训"]):
        return llm_advisor(pid, "新人", msg)
    # ② 签证台账 / ③ 材料台账（pid 版）
    if (msg.startswith("签证：") or msg.startswith("签证:")) or any(k in msg for k in ["签证清单", "签证记录", "签证台账", "签证状态"]):
        return visa_cmd(db, msg, pid)
    if (msg.startswith("材料：") or msg.startswith("材料:")) or any(k in msg for k in ["材料清单", "材料台账", "材料到货", "材料进场", "材料记录", "材料明细"]):
        return material_cmd(db, msg, pid)
    # ⑥ 风险登记（人工上报：风险：甲方被执行/供应商失信）
    if msg.startswith("风险：") or msg.startswith("风险:"):
        db.execute("INSERT INTO alerts (project_id,type,severity,content) VALUES (?,?,?,?)",
                   (pid, "manual", "warning", msg[3:].strip()))
        db.commit()
        return f"⚠️ 风险已登记并预警：「{msg[3:].strip()}」——已加入监控，后续可发「风险分析」查看处理建议。"
    # 风险清单/复查
    if "风险清单" in msg or "风险列表" in msg or "风险复查" in msg or "风险记录" in msg:
        rows = db.execute("SELECT content,severity,status,created_at FROM alerts WHERE project_id=? ORDER BY id DESC LIMIT 10", (pid,)).fetchall()
        if not rows:
            return "✅ 当前无风险记录。"
        lines = [f"🚨 风险清单（{len(rows)} 条）："]
        for r in rows:
            mark = "🟢已处理" if r["status"] == "done" else ("🔴" if r["severity"] == "critical" else "🟠")
            lines.append(f"- {mark} [{r['created_at'][:10]}] {r['content'][:60]}")
        lines.append("\n（发「风险：XX」登记新风险；「风险分析」看处理建议）")
        return "\n".join(lines)
    # ⑦ 资源方管理：登记/到货/缺货/查询
    if msg.startswith("材料商") or msg.startswith("施工队") or msg.startswith("分包商"):
        return resource_cmd(db, msg, pid)
    # ⑧ 记忆图谱：回忆/查证（对话即档案）
    if any(k in msg for k in ["回忆", "查一下当时", "之前说过", "谁说的", "原话"]):
        return memory_recall(db, pid, msg)
    # ⑨ 经营决策：盈利/现金流/接单
    if any(k in msg for k in ["盈利预测", "能赚多少", "利润", "现金流", "资金缺口", "接单评估", "这个单能接吗"]):
        return llm_biz(pid, msg)
    # 全局查询（跨项目比较）
    if any(k in msg for k in ["哪个项目", "所有项目", "全部项目", "最慢", "最快", "最危险", "对比"]):
        return global_query(db, msg)
    # ⑤ 行业知识库：清单/直接问答（放顾问命令之后——不抢项目分析）
    if msg.strip() in ("知识库", "行业知识", "有什么知识", "知识库有哪些"):
        return kb_list()
    _kb = kb_answer(msg)
    if _kb:
        return _kb

    intent = classify_intent(msg)

    # 规则没识别 → 大模型兜底（复杂句）
    if intent == "record" and LLM_AVAILABLE:
        names = [p["name"] for p in projects]
        structured = advisor_llm.extract_structured(msg, names)
        if structured and structured.get("intent") and structured["intent"] != "other":
            intent = structured["intent"]
            # 大模型提取的字段可覆盖
            if structured.get("amount"):
                amount = structured["amount"]
            if structured.get("project"):
                for p in projects:
                    if structured["project"] in p["name"] or p["name"] in structured["project"]:
                        pid = p["id"]
                        proj = p
                        break

    # 查询意图 → 直接查库回答
    if intent == "query":
        return query_respond(pid, msg)

    # 人员变动 → 生成核查清单
    if intent == "person_change":
        who2 = re.sub(r'[甲乙丙方]|公司|的|经理|总监|工程师|张|李|王|刘|陈|杨|赵', '', msg)[:6] or "对接人"
        db.execute("INSERT INTO events (project_id,event_type,who,subject,source) VALUES (?,?,?,?,?)",
                   (pid, "change", who, f"甲方对接人变动（{who2}）", msg))
        db.commit()
        return person_change_checklist(pid)

    # 回款收到 → 更新项目已收 + 记录
    if intent == "payment_received" and amount:
        if proj:
            new_received = (proj["received"] or 0) + amount
            db.execute("UPDATE projects SET received=? WHERE id=?", (new_received, pid))
        db.execute("INSERT INTO events (project_id,event_type,who,subject,amount,status,source) VALUES (?,?,?,?,?,?,?)",
                   (pid, "payment", who, "收款", amount, "done", msg))
        db.commit()
        return f"✅ 已记录收款 {amount/10000:.1f} 万。项目 {proj['name'] if proj else ''} 累计已收 {new_received/10000:.1f} 万（合同 {proj['contract']/10000:.1f} 万）。"

    # 变更 → 记录 + 提醒签证（口头变更=强提醒）
    if intent == "change":
        db.execute("INSERT INTO events (project_id,event_type,who,subject,amount,source) VALUES (?,?,?,?,?,?)",
                   (pid, "change", who, msg[:50], amount, msg))
        db.execute("INSERT INTO alerts (project_id,type,severity,content) VALUES (?,?,?,?)",
                   (pid, "change", "critical" if any(k in msg for k in ["口头", "先干着", "先做", "先施工"]) else "warning",
                    f"变更需补签证：{msg[:50]}"))
        db.commit()
        if any(k in msg for k in ["口头", "先干着", "先做", "先施工"]):
            return ("🚨 **危险警告：口头变更未签证！**\n"
                    "「先干着」= 已施工未签证——这是工程结算扯皮的头号原因。\n"
                    "立即处理：\n"
                    "1) 今天补签证单（联系单）让甲方签认\n"
                    "2) 明确变更金额和计价方式\n"
                    "3) 拍照留存施工前/后状态\n"
                    "已记录该变更并加急预警。")
        return (f"📝 变更已记录" + (f"（金额 {amount/10000:.1f} 万）" if amount else "") +
                "。⚠️ 提醒：变更必须**书面签证**——口头不算数，建议立即补签证单。")

    # 承诺 → 记录（模糊承诺追问）
    if intent == "promise":
        due = parse_due(msg)
        db.execute("INSERT INTO events (project_id,event_type,who,subject,due_date,source) VALUES (?,?,?,?,?,?)",
                   (pid, "promise", who, msg[:40], due, msg))
        db.commit()
        if any(k in msg for k in ["后面好说", "到时候再说", "回头再说", "好说"]):
            return ("📌 已记录模糊承诺（无时间/金额）。\n"
                    "⚠️ 提醒：「后面好说」= 无法执行——建议现在追问：\n"
                    "1) 具体什么时候付？\n"
                    "2) 金额多少？\n"
                    "3) 最好书面确认（微信文字/函件）——口头容易赖账")
        return f"📌 承诺已记录：{who} 说「{msg[:40]}」" + (f"，到期：{due}" if due else "") + "。到期我会提醒。"

    # 问题/风险 → 记录 + 预警（安全事故升级）
    if intent == "issue":
        db.execute("INSERT INTO events (project_id,event_type,who,subject,source) VALUES (?,?,?,?,?)",
                   (pid, "issue", who, msg[:60], msg))
        db.execute("INSERT INTO alerts (project_id,type,severity,content) VALUES (?,?,?,?)",
                   (pid, "issue", "critical" if any(k in msg for k in ["摔", "受伤", "事故", "流血", "砸"]) else "warning",
                    f"{who} 报告问题：{msg[:60]}"))
        db.commit()
        if any(k in msg for k in ["摔", "受伤", "事故", "流血", "砸"]):
            return ("🚨 **安全事故升级处理！**\n"
                    "1) 立即：送医/急救——安全第一\n"
                    "2) 上报：通知老板/安全员/甲方\n"
                    "3) 现场：保护现场、拍照记录\n"
                    "4) 停工排查：涉事区域暂停，查隐患\n"
                    "5) 记录：伤情/时间/原因——后续保险/责任认定\n"
                    "已记录并加急预警。")
        return f"⚠️ 问题已记录并预警：「{msg[:50]}」——已通知相关人跟进。"

    # 进度 → 记录
    if intent == "progress":
        db.execute("INSERT INTO events (project_id,event_type,who,subject,source) VALUES (?,?,?,?,?)",
                   (pid, "progress", who, msg[:60], msg))
        db.commit()
        return f"✅ 进度已记录：「{msg[:50]}」——已存入 {proj['name'] if proj else ''} 项目档案。"

    # 其他/未识别 → LLM 顾问核心：自然回答 + 说话录入（自动识别业务信息并记录）
    if LLM_AVAILABLE:
        try:
            pname = proj["name"] if proj else (db.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone() or {}).get("name") if pid else None
            recent = db.execute("SELECT subject, source FROM events WHERE project_id=? ORDER BY id DESC LIMIT 5", (pid,)).fetchall() if pid else []
            recent_txt = "\n".join(f"- {r['subject']}" for r in recent) if recent else "（暂无记录）"
            kb = kb_search(msg, limit=2)
            kb_txt = "\n".join(f"- [{c}]{t}：{content[:200]}" for _, c, t, content in kb) if kb else "（无匹配知识）"
            sys_p = (
                "你是资深工程项目顾问，服务中小施工/装修公司。客户发来消息，请：\n"
                "1) 【必须正面回答】客户的问题——回款/进度/风险/施工/材料问题都给明确答案（有项目数据用数据，没数据给判断方法和经验值）\n"
                "2) 回答要专业、直接、给结论，像真顾问聊天，不要只回复'已记录'\n"
                "3) 如果消息确实是业务事实（收款/付款/变更/承诺/问题/进度），回答完在末尾附一行：📌 已记录：<一句话概括>\n"
                "4) 不要机械，不要反复引导建档。\n"
                f"5) 行业知识（若与客户问题相关，引用并注明出处）：\n{kb_txt}\n"
            )
            user_p = f"客户：{who}\n项目：{pname or '未指定'}\n最近记录：\n{recent_txt}\n\n客户说：{msg}"
            resp = advisor_llm.chat([{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}], temperature=0.3, max_tokens=500)
            if resp and not resp.startswith("__LLM_ERROR__"):
                # 尽量把业务事实落库（events）
                if pid:
                    try:
                        db.execute("INSERT INTO events (project_id,event_type,who,subject,source) VALUES (?,?,?,?,?)",
                                   (pid, "record", who, msg[:60], msg))
                        db.commit()
                    except Exception:
                        pass
                return resp
        except Exception:
            pass
    db.execute("INSERT INTO events (project_id,event_type,who,subject,source) VALUES (?,?,?,?,?)",
               (pid, "record", who, msg[:60], msg))
    db.commit()
    return f"📋 已记录：「{msg[:50]}」"

# ---------- ⑤ 八大顾问模块 ----------
def llm_advisor(pid, module, msg):
    """顾问模块：进度/回款/变更/风险/巡检/新人——LLM 带项目上下文分析"""
    db = get_db()
    try:
        proj = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not proj:
            return "⚠️ 还没有项目——先「新建项目：XXX，合同XX万，工期X个月」"
        p = dict(proj)
        events = db.execute("SELECT event_type,who,subject,amount,due_date,created_at FROM events WHERE project_id=? ORDER BY id DESC LIMIT 12", (pid,)).fetchall()
        ev_txt = "\n".join(f"- [{e['created_at'][:10]}][{e['event_type']}]{e['who']}：{e['subject']}" for e in events) or "（暂无记录）"
        ms = db.execute("SELECT stage, plan_date, status FROM project_milestones WHERE project_id=? ORDER BY plan_date", (pid,)).fetchall()
        ms_txt = "\n".join(f"- {m['stage']}：{m['plan_date']}（{m['status']}）" for m in ms) or "（暂无节点）"
        import advisor_llm
        # 风险评估：专业框架（合同/甲方/项目三维度）
        if module == "风险":
            # 读合同全文（文件建项目时存的）
            contract = db.execute("""SELECT source FROM events WHERE project_id=? AND event_type='contract'
                ORDER BY id DESC LIMIT 1""", (pid,)).fetchone()
            contract_txt = (contract["source"][:1500] if contract and contract["source"] else "（未提供合同文本——可发合同文件让我分析条款）")
            kb = kb_search(msg, limit=2)
            kb_txt = "\n".join(f"- [{c}]{t}：{content[:150]}" for _, c, t, content in kb) if kb else "（无匹配知识）"
            prompt = f"""你是资深工程项目风险顾问（有 20 年工程纠纷处理经验）。项目「{p['name']}」：
合同金额 {p['contract']/10000:.1f} 万，已收 {p['received']/10000:.1f} 万，阶段 {p['stage']}。
合同文本：
{contract_txt}
项目记录：
{ev_txt}
节点计划：
{ms_txt}
行业知识（相关时引用并注明出处）：
{kb_txt}
请做【专业风险评估】，按以下框架输出：
一、合同风险：结合合同文本分析——付款条款是否有利、变更/签证条款、违约金/质保金比例、工期违约风险
二、甲方风险：结合合同中的甲方信息（名称/法人/主体）分析——付款能力、是否有拖延迹象、人员变动、被诉/被执行风险
三、项目风险：进度风险（节点是否按期）、成本风险（预算/超支）、质量风险（验收）、资料风险（签证/验收单缺失）
四、总体评级：高风险/中风险/低风险 + 一句话结论
五、应对建议：3-5 条可执行措施（分 P0/P1 优先级）
要求：结合合同具体条款和记录事实，不要空话，600字内。"""
        else:
            # 回款顾问：读付款计划表（如有）
            ps_txt = ""
            if module == "回款":
                ps = db.execute("SELECT stage,ratio,amount,due_date,status FROM payment_schedule WHERE project_id=? ORDER BY id", (pid,)).fetchall()
                if ps:
                    ps_txt = "\n".join(f"- {p['stage']}：{p['amount']/10000:.1f}万（{p['ratio']*100:.0f}%）应到{p['due_date']}，状态{p['status']}" for p in ps)
                else:
                    ps_txt = "（无回款计划——建档时提供付款条款可生成，如「付款节点：预付30%，进度款按月，质保金5%」）"
            if module == "变更":
                vs = db.execute("SELECT title,amount,status FROM visa_records WHERE project_id=? ORDER BY id DESC LIMIT 15", (pid,)).fetchall()
                if vs:
                    ps_txt = "\n".join(f"- {v['title']}" + (f"（{v['amount']/10000:.1f}万）" if v["amount"] else "") + f"【{'未签' if v['status']=='pending' else '已签' if v['status']=='signed' else '已付'}】" for v in vs)
                    ps_txt = "签证台账：\n" + ps_txt + "\n"
                else:
                    ps_txt = "（签证台账为空——口头变更请发「签证：XX」登记）"
            # 各顾问模块专业框架
            frameworks = {
                "进度": """你是资深进度顾问。请做进度分析：
一、当前进度判断：各节点状态（按期/临近/逾期）、整体进度偏差
二、关键路径：当前卡点在哪（设计/采购/施工/验收）
三、延期风险：逾期节点的影响、连锁风险
四、赶工建议：可执行措施（增派人员/平行作业/优化工序）分优先级""",
                "回款": """你是资深回款顾问。请做回款分析：
一、回款现状：合同额/已收/应收/逾期应收
二、应收账龄：每笔应收的账龄、逾期原因
三、催收策略：按优先级给催收话术和动作（发函/约谈/暂停施工/法律）
四、现金流影响：资金缺口、垫资压力""",
                "变更": """你是资深变更顾问。请做变更分析：
一、变更台账：已记录变更、金额、签证状态
二、签证完整性：口头变更未签证的风险点
三、计价风险：变更无价的隐患
四、补签建议：按优先级列出需要立即补签证的项""",
                "巡检": """你是资深巡检顾问。请按工程巡检清单分析：
一、质量巡检：关键工序（防水/电气/结构）检查要点
二、安全巡检：临电/防护/机械/消防
三、资料巡检：验收单/签证/隐蔽资料齐全性
四、巡检安排：本周应做的巡检项清单""",
                "新人": """你是资深工程老师傅。新人问：{msg}
请用大白话讲清楚：流程步骤、规范要点、常见坑、跟谁对接。分步说明，像师傅带徒弟。""",
            }
            fw = frameworks.get(module, "")
            ps_block = ("回款计划：\n" + ps_txt + "\n") if ps_txt else ""
            kb = kb_search(msg, limit=2)
            kb_txt = "\n".join(f"- [{c}]{t}：{content[:150]}" for _, c, t, content in kb) if kb else "（无匹配知识）"
            prompt = f"""你是资深工程项目顾问。客户项目「{p['name']}」（合同 {p['contract']/10000:.1f} 万，已收 {p['received']/10000:.1f} 万）。
节点计划：
{ms_txt}
{ps_block}项目记录：
{ev_txt}
行业知识（相关时引用并注明出处）：
{kb_txt}
{fw}
客户问（{module}顾问）：{msg}
请按上述框架专业分析，结合记录事实，简洁专业，500字内。"""
        resp = advisor_llm.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=800)
        if resp and not resp.startswith("__LLM_ERROR__"):
            return f"🔍 【{module}顾问】\n{resp}"
        return f"（顾问分析暂不可用——项目数据：{p['name']} 合同 {p['contract']/10000:.1f} 万，记录 {len(events)} 条）"
    finally:
        db.close()


# ---------- ⑥ 风险登记已接入 ----------
# ---------- ⑤ 行业知识库（L1：规范/质保金/签证/纠纷——轻量关键词检索 + LLM 注入） ----------
SEED_KB = [
    # (category, title, keywords, content)
    ("签证", "口头变更必须书面签证", "签证,口头变更,补签,联系单,签认,变更",
     "变更/增项必须**书面签证**（签证单/联系单），甲方签字确认才有效——口头指令不算数。签证单要素：工程名称、变更内容、工程量、单价/金额、日期、双方签字盖章。口头干了活再补签是结算扯皮头号原因；补签不了就留证据（微信/照片/录音）。"),
    ("回款", "工程款优先受偿权", "优先受偿,拍卖,折价,民法典807,受偿权,赖账,不给钱",
     "《民法典》第807条：发包人未按约支付工程款，承包人催告后仍不支付的，可就工程折价或拍卖所得价款**优先受偿**（优先于抵押权和其他债权）。行使期限：自应当给付工程款之日起18个月内主张（司法解释一）。发现甲方资不抵债要尽快主张。"),
    ("回款", "工程款诉讼时效3年", "诉讼时效,起诉,3年,过期,时效,欠款",
     "《民法典》第188条：工程款债权诉讼时效**3年**，自知道或应当知道权利受损起算。催款（微信/函件/起诉）可中断时效重新起算——**每3年至少留一次书面催收记录**。"),
    ("回款", "实际施工人可起诉发包人", "实际施工人,转包,违法分包,起诉,发包人,包工头",
     "司法解释（一）第43条：转包/违法分包的实际施工人，可以起诉发包人，在**欠付工程款范围内**承担责任。挂靠/转包的包工头欠款有这条兜底。"),
    ("质保金", "质保金比例上限3%", "质保金,保证金,3%,保函,缺陷责任期,质保",
     "《建设工程质量保证金管理办法》（建质〔2017〕138号）：质保金总预留比例**不得高于工程结算总额的3%**，可用银行保函替代。缺陷责任期一般1-2年、最长不超2年，届满应返还；保修期按《建设工程质量管理条例》：防水工程5年、主体结构为设计使用年限。"),
    ("工期", "工期顺延要及时发函", "工期顺延,延期,发函,窝工,索赔,停工",
     "非承包人原因（甲方变更/付款延迟/不可抗力/图纸延误）→ 应**及时发函**主张工期顺延和费用索赔，逾期可能被视为放弃（合同有约定按约定）。停工/窝工损失要有书面记录。"),
    ("验收", "隐蔽工程先验后隐", "隐蔽工程,验收,覆盖,剥露,通知,防水",
     "《建设工程质量管理条例》：隐蔽工程隐蔽**前**须通知建设单位/监理检验，合格才能覆盖。未通知就隐蔽→甲方可要求剥露重验，费用由责任方承担。隐蔽前拍照+记录是基本操作。"),
    ("验收", "竣工验收与结算视为认可", "竣工验收,视为认可,结算文件,答复,拖延,结算",
     "工程完工后发包人应及时组织验收。**竣工结算文件**：合同里写清「发包人收到结算文件后X日内不答复视为认可」——这是对付'拖着不结算'的关键条款。"),
    ("材料", "材料进场须报验", "材料进场,报验,合格证,检测报告,不合格,材料",
     "材料进场须**报验**（合格证/检测报告/复试），不合格材料不得使用。先用了再主张不合格→风险自担。甲方供材延迟/不合格→可索赔工期和损失。"),
    ("纠纷", "工程纠纷证据清单", "证据,纠纷,诉讼,证据保全,微信记录,聊天记录",
     "工程纠纷核心证据：合同及补充协议、签证单、联系单、**微信/聊天记录**、现场照片、施工日志、验收单、付款凭证。关键证据缺失是败诉主因——日常就要留痕。"),
    ("造价", "增项先谈价后施工", "增项,变更计价,谈价,定额,市场价,加钱",
     "变更/增项**先谈价后施工**：无约定按合同计价方式，合同没约定→参照定额或市场价。先干活后谈价最被动。"),
    ("造价", "垫资与利息", "垫资,利息,资金,垫付,回款慢",
     "垫资：合同有约定按约定（利息受司法保护上限约束），无约定按实际垫资事实处理。垫资前写清归还时间和利息。"),
]

def kb_search(msg, limit=3):
    """关键词检索行业知识库——命中 category/title/keywords"""
    try:
        rows = []
        for item in SEED_KB:
            cat, title, kws, content = item
            hit = 0
            for kw in (kws + "," + cat + "," + title).split(","):
                kw = kw.strip()
                if kw and kw in msg:
                    hit += 1
            if hit:
                rows.append((hit, cat, title, content))
        rows.sort(key=lambda x: -x[0])
        return rows[:limit]
    except Exception:
        return []

def kb_answer(msg):
    """行业知识直接问答（强知识词命中→直接返回，省 token）"""
    hits = kb_search(msg, limit=3)
    if not hits:
        return None
    lines = ["📖 行业知识（供参考）："]
    for _, cat, title, content in hits:
        lines.append(f"\n**{title}**（{cat}）\n{content}")
    lines.append("\n（发「知识库」查看全部类别）")
    return "\n".join(lines)

def kb_list():
    cats = []
    for item in SEED_KB:
        if item[0] not in cats:
            cats.append(item[0])
    return "📚 行业知识库（L1 规范/标准/判例）类别：\n" + "\n".join(
        f"- {c}（{sum(1 for i in SEED_KB if i[0]==c)}条）" for c in cats) + \
        "\n\n问相关问题我自动引用出处（如：质保金比例、口头变更、诉讼时效、隐蔽工程）"


# ---------- ② 签证台账（visa_records） ----------
def visa_cmd(db, msg, pid):
    """签证台账：登记 / 清单 / 状态更新（未签→已签→已付）"""
    try:
        if not pid:
            return "⚠️ 还没有项目——先「新建项目：XXX，合同XX万，工期X个月」"
        body = msg.split("：", 1)[-1].split(":", 1)[-1].strip() if ("：" in msg or ":" in msg) else msg
        # 状态更新：签证：已签 XX / 签证：完成 XX / 签证：已付 XX
        for kw, status, mark in [("已签", "signed", "✅ 已签"), ("完成", "signed", "✅ 已签"), ("已付", "paid", "💰 已付款")]:
            if body.startswith(kw):
                kw2 = body[len(kw):].strip()
                r = db.execute("SELECT * FROM visa_records WHERE project_id=? AND title LIKE ? ORDER BY id DESC LIMIT 1",
                               (pid, f"%{kw2}%")).fetchone()
                if not r:
                    return f"⚠️ 没找到包含「{kw2}」的签证记录——发「签证清单」查看"
                db.execute("UPDATE visa_records SET status=? WHERE id=?", (status, r["id"]))
                db.commit()
                return mark + "：" + r["title"] + (f"（{r['amount']/10000:.1f}万）" if r["amount"] else "")
        # 查询：签证清单/记录/台账/状态
        if any(k in msg for k in ["清单", "记录", "台账", "状态", "都有哪些", "哪些签证"]):
            rows = db.execute("SELECT * FROM visa_records WHERE project_id=? ORDER BY id DESC LIMIT 20", (pid,)).fetchall()
            if not rows:
                return "📋 暂无签证记录——有变更时发「签证：XX内容（金额）」登记"
            pending = [r for r in rows if r["status"] == "pending"]
            lines = [f"📋 签证台账（{len(rows)} 条）："]
            for r in rows:
                mark = "🔴未签" if r["status"] == "pending" else ("🟢已签" if r["status"] == "signed" else "💰已付")
                lines.append(f"- {mark} [{r['created_at'][:10]}] {r['title'][:40]}" + (f"（{r['amount']/10000:.1f}万）" if r["amount"] else ""))
            if pending:
                lines.append(f"\n🔴 未签 {len(pending)} 条——口头变更未签证是结算扯皮头号风险，建议尽快补签（发「签证：已签 XX」更新）")
            return "\n".join(lines)
        # 登记：签证：XX内容（金额）
        if len(body) < 3:
            return "📋 签证登记格式：「签证：塔吊租赁费变更（3万）」，查询「签证清单」，更新「签证：已签 XX」"
        amount = extract_amount(body)
        title = re.sub(r'[（(]\s*[\d.]+万?元?\s*[)）]', '', body).strip() or body
        db.execute("INSERT INTO visa_records (project_id,title,amount,status,note) VALUES (?,?,?,?,?)",
                   (pid, title, amount or 0, "pending", body[:80]))
        db.execute("INSERT INTO events (project_id,event_type,who,subject,amount,source) VALUES (?,?,?,?,?,?)",
                   (pid, "change", "老板", title, amount or 0, body[:120]))
        db.commit()
        tip = f"（金额 {amount/10000:.1f} 万）" if amount else "（未提到金额——补金额便于计价）"
        return (f"📋 签证已登记：{title}{tip}\n"
                "⚠️ 签证必须让**甲方签字确认**才有效——口头不算数。发「签证：已签 XX」更新状态。")
    except Exception as e:
        return f"⚠️ 签证处理异常：{e}"


# ---------- ③ 材料台账（material_ledger） ----------
def material_cmd(db, msg, pid):
    """材料台账：登记 / 到货 / 清单"""
    try:
        if not pid:
            return "⚠️ 还没有项目——先「新建项目：XXX，合同XX万，工期X个月」"
        body = msg.split("：", 1)[-1].split(":", 1)[-1].strip() if ("：" in msg or ":" in msg) else msg
        # 到货更新：材料到货：XX / 材料进场：XX
        if "到货" in msg or "进场" in msg:
            kw = body.replace("到货", "").replace("进场", "").strip()
            r = db.execute("SELECT * FROM material_ledger WHERE project_id=? AND name LIKE ? ORDER BY id DESC LIMIT 1",
                           (pid, f"%{kw}%")).fetchone()
            if not r:
                return f"⚠️ 没找到材料「{kw}」——先发「材料：{kw} 100个 1750元」登记"
            db.execute("UPDATE material_ledger SET arrived=1 WHERE id=?", (r["id"],))
            db.commit()
            return f"📦 已到货：{r['name']}" + (f"（{r['qty']:g}{r['unit']}）" if r["qty"] else "")
        # 查询：材料清单/台账
        if any(k in msg for k in ["清单", "台账", "记录", "明细", "材料有哪些"]):
            rows = db.execute("SELECT * FROM material_ledger WHERE project_id=? ORDER BY id DESC LIMIT 20", (pid,)).fetchall()
            if not rows:
                return "📦 暂无材料记录——发「材料：海尔冰箱 100台 1750元」登记"
            lines = [f"📦 材料台账（{len(rows)} 条）："]
            for r in rows:
                mark = "✅" if r["arrived"] else "⏳"
                line = f"- {mark} {r['name']}"
                if r["brand"]:
                    line += f"（{r['brand']}）"
                if r["spec"]:
                    line += f" {r['spec']}"
                if r["qty"]:
                    line += f" {r['qty']:g}{r['unit'] or ''}"
                if r["price"]:
                    line += f" ×{r['price']:g}元"
                lines.append(line)
            not_arrived = [r for r in rows if not r["arrived"]]
            if not_arrived:
                lines.append(f"\n⏳ 未到 {len(not_arrived)} 条——到货发「材料到货：XX」更新")
            return "\n".join(lines)
        # 登记：材料：海尔冰箱 100台 1750元
        if len(body) < 2:
            return "📦 材料登记格式：「材料：海尔冰箱 100台 1750元」，查询「材料清单」，到货「材料到货：海尔冰箱」"
        price = None
        m = re.search(r'([\d.]+)\s*(元|块)', body)
        if m:
            price = float(m.group(1))
        qty, unit = None, ""
        m2 = re.search(r'([\d.]+)\s*(台|个|套|件|米|方|吨|公斤|kg|箱|批|张|根|卷|桶)', body)
        if m2:
            qty = float(m2.group(1))
            unit = m2.group(2)
        name = body
        if m2:  # 删数量段（如 "100台"/"100 台"）
            name = name.replace(m2.group(0), "")
        if m:   # 删单价段（如 "1750元"/"1750 元"）
            name = name.replace(m.group(0), "")
        name = re.sub(r'[\d.]+', '', name).replace("元", "").replace("块", "").replace("×", "").replace("x", "").strip()
        name = re.sub(r'\s+', '', name)
        if not name:
            return "⚠️ 材料名没识别出来——格式：「材料：海尔冰箱 100台 1750元」"
        db.execute("INSERT INTO material_ledger (project_id,name,spec,qty,unit,price,arrived) VALUES (?,?,?,?,?,?,?)",
                   (pid, name, "", qty or 0, unit, price or 0, 0))
        db.execute("INSERT INTO events (project_id,event_type,who,subject,source) VALUES (?,?,?,?,?)",
                   (pid, "material", "老板", f"{name}" + (f" {qty:g}{unit}" if qty else "") + (f" {price:g}元" if price else ""), body[:120]))
        db.commit()
        return (f"📦 材料已登记：{name}" + (f"（{qty:g}{unit}）" if qty else "") + (f"，单价 {price:g} 元" if price else "")
                + "\n发「材料清单」查看，到货发「材料到货：XX」")
    except Exception as e:
        return f"⚠️ 材料处理异常：{e}"


# ---------- ⑦ 资源方管理 ----------
def resource_cmd(db, msg, pid):
    """材料商/施工队/分包商：登记 / 到货 / 缺货 / 查询"""
    try:
        # 解析资源名（去掉前缀和后缀动词）
        def _res_name():
            n = msg.split("：")[0].split(":")[0].replace("材料商", "").replace("施工队", "").replace("分包商", "").strip()
            for suf in ["到了", "到货", "缺货了", "缺货", "没到", "延迟", "干活", "负载", "工效", "忙不忙", "怎么样"]:
                n = n.replace(suf, "")
            return n.strip()
        if "到货" in msg or "到了" in msg:
            name = _res_name()
            r = db.execute("SELECT * FROM resources WHERE name LIKE ?", (f"%{name}%",)).fetchone()
            if not r:
                db.execute("INSERT INTO resources (name,rtype,note) VALUES (?,?,?)", (name, "supplier", "自动登记"))
                db.commit()
                r = db.execute("SELECT * FROM resources WHERE name LIKE ?", (f"%{name}%",)).fetchone()
            rid = r["id"] if isinstance(r, sqlite3.Row) else r
            db.execute("INSERT INTO resource_events (resource_id,event_type,project_id,content) VALUES (?,?,?,?)", (rid, "arrive", pid, msg[:80]))
            db.commit()
            return f"✅ 已记录到货：{name}"
        if "缺货" in msg or "没到" in msg or "延迟" in msg:
            name = _res_name()
            r = db.execute("SELECT * FROM resources WHERE name LIKE ?", (f"%{name}%",)).fetchone()
            if not r:
                db.execute("INSERT INTO resources (name,rtype,note) VALUES (?,?,?)", (name, "supplier", "自动登记"))
                db.commit()
                r = db.execute("SELECT * FROM resources WHERE name LIKE ?", (f"%{name}%",)).fetchone()
            rid = r["id"] if isinstance(r, sqlite3.Row) else r
            db.execute("INSERT INTO resource_events (resource_id,event_type,project_id,content) VALUES (?,?,?,?)", (rid, "shortage", pid, msg[:80]))
            db.execute("INSERT INTO alerts (project_id,type,severity,content) VALUES (?,?,?,?)", (pid, "resource", "warning", f"{name}缺货/延迟：{msg[:50]}"))
            db.commit()
            return f"⚠️ 已记录缺货并预警：{name}——已加入节点影响分析。"
        # 查询资源方
        if "工效" in msg or "干活" in msg or "哪个队" in msg:
            rows = db.execute("""SELECT r.name, r.rating,
                (SELECT COUNT(*) FROM resource_events re WHERE re.resource_id=r.id) as cnt
                FROM resources r ORDER BY r.rating DESC""").fetchall()
            if not rows:
                return "还没有施工队/材料商记录——发「施工队XX到了」登记"
            lines = ["📊 工效/信誉分析："]
            for r in rows:
                lines.append(f"- {r['name']}：信誉{'⭐'*r['rating']}，记录 {r['cnt']} 条")
            return "\n".join(lines)
        # 负载预警：资源方同时挂多个项目
        if "负载" in msg or "冲突" in msg or "忙" in msg:
            rows = db.execute("""SELECT r.name, COUNT(DISTINCT re.project_id) as pc
                FROM resources r JOIN resource_events re ON re.resource_id=r.id
                GROUP BY r.id HAVING pc >= 2""").fetchall()
            if rows:
                return "🚨 资源负载预警：\n" + "\n".join(f"- {r['name']} 同时在 {r['pc']} 个项目" for r in rows)
            return "✅ 当前无资源负载冲突。"
        rows = db.execute("SELECT name,rtype,rating,note FROM resources ORDER BY id DESC LIMIT 10").fetchall()
        if rows:
            lines = ["📇 资源方档案："]
            for r in rows:
                lines.append(f"- {r['name']}（{r['rtype']}）信誉{'⭐'*r['rating']} {r['note'] or ''}")
            return "\n".join(lines)
        return "还没有资源方档案——发「材料商XX：价格/账期」登记"
    except Exception as e:
        return f"⚠️ 资源方处理异常：{e}"


# ---------- ⑧ 记忆图谱（对话即档案，可调取） ----------
def memory_recall(db, pid, msg):
    """回忆/查证：从 events 调取历史原话（支持按人/按事/全部）"""
    try:
        # 按人查询：谁/老王/李经理
        who = None
        for w in ["老王", "李经理", "王经理", "张工", "李工", "王工", "老板", "甲方", "监理", "供应商", "施工队"]:
            if w in msg:
                who = w
                break
        kw = msg.replace("回忆", "").replace("查一下当时", "").replace("之前说过", "").replace("谁说的", "").replace("原话", "").replace("一下", "").replace(who or "", "").strip()
        q = f"%{kw}%" if kw else "%"
        if who:
            rows = db.execute("""SELECT who,subject,source,created_at FROM events
                WHERE project_id=? AND who LIKE ?
                ORDER BY id DESC LIMIT 10""", (pid, f"%{who}%")).fetchall()
            if not rows:
                return f"没有找到 {who} 的相关记录"
            lines = [f"🗂️ {who} 说过/做过（{len(rows)} 条）："]
            for r in rows:
                lines.append(f"- [{r['created_at'][:10]}] {r['source'] or r['subject']}")
            return "\n".join(lines)
        rows = db.execute("""SELECT who,subject,source,created_at FROM events
            WHERE project_id=? AND (subject LIKE ? OR source LIKE ?)
            ORDER BY id DESC LIMIT 8""", (pid, q, q)).fetchall()
        if not rows:
            return f"没有找到相关记录" + (f"（关键词：{kw}）" if kw else "")
        lines = [f"🗂️ 项目记忆（{'全部' if not kw else '关键词：' + kw}）："]
        for r in rows:
            lines.append(f"- [{r['created_at'][:10]}] {r['who']}：{r['source'] or r['subject']}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ 记忆查询异常：{e}"


# ---------- ⑨ 经营决策 ----------
def llm_biz(pid, msg):
    """盈利预测/现金流/接单评估——LLM 分析"""
    db = get_db()
    try:
        proj = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not proj:
            return "⚠️ 还没有项目数据——先建项目"
        p = dict(proj)
        events = db.execute("SELECT event_type,amount,subject,created_at FROM events WHERE project_id=? ORDER BY id DESC LIMIT 15", (pid,)).fetchall()
        ev_txt = "\n".join(f"- [{e['created_at'][:10]}][{e['event_type']}]{e['subject']}" + (f"（{e['amount']/10000:.1f}万）" if e['amount'] else "") for e in events) or "（暂无）"
        # 现金流预测表：应收（open 收款）+ 到期
        recv = db.execute("""SELECT subject, amount, due_date FROM events
            WHERE project_id=? AND event_type='payment' AND status='open' AND amount IS NOT NULL
            ORDER BY due_date""", (pid,)).fetchall()
        recv_txt = "\n".join(f"- {r['subject']} {r['amount']/10000:.1f} 万（应到 {r['due_date'] or '未定'}）" for r in recv) or "（无应收）"
        import advisor_llm
        prompt = f"""你是工程公司经营顾问。项目「{p['name']}」：合同 {p['contract']/10000:.1f} 万，已收 {p['received']/10000:.1f} 万，进度 {p['stage']}。
收支记录：
{ev_txt}
应收款：
{recv_txt}
客户问：{msg}
请做经营分析：当前盈利/现金流状况、应收风险、资金缺口预测、建议。简洁专业，400字内。"""
        resp = advisor_llm.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=600)
        if resp and not resp.startswith("__LLM_ERROR__"):
            return f"💼 经营顾问\n{resp}"
        return f"（经营分析暂不可用——合同 {p['contract']/10000:.1f} 万，已收 {p['received']/10000:.1f} 万）"
    finally:
        db.close()


def parse_due(msg):
    """解析承诺到期日（明天/下周/月底/几号）"""
    today = datetime.now()
    if "明天" in msg:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if "下周" in msg:
        return (today + timedelta(days=7)).strftime("%Y-%m-%d")
    if "月底" in msg or "月末" in msg:
        return today.strftime("%Y-%m-28")  # 简化
    m = re.search(r'(\d+)\s*号', msg)
    if m:
        return today.strftime(f"%Y-%m-{int(m.group(1)):02d}")
    return None

def person_change_checklist(pid):
    """人员变动 → 核查清单"""
    db = get_db()
    checklist = [
        "1) 旧对接人签过的签证/联系单——有无甲方盖章？没盖章 → 尽快找新对接人补签",
        "2) 口头付款承诺 → 立即书面化（发函/约谈新对接人确认）",
        "3) 已施工未签证的增项 → 立刻补签证（换人是补签最好时机）",
        "4) 合同主体/法代是否有变 → 核验新授权",
        "5) 新对接人关系 → 带进度+资料主动约交接",
    ]
    return ("⚠️ **人员变动风险提醒**——立即核查以下 5 项：\n" + "\n".join(checklist))

def query_respond(pid, msg):
    """查询响应"""
    db = get_db()
    proj = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        return "⚠️ 还没有项目——先报一个：新建项目：XXX，合同XX万，工期X个月"
    p = dict(proj)
    lines = [f"📋 {p['name']}（{p['stage']}）",
             f"· 合同 {p['contract']/10000:.1f} 万｜已收 {p['received']/10000:.1f} 万"]
    # 逾期应收
    overdue = db.execute("""SELECT subject, amount, due_date FROM events
        WHERE project_id=? AND event_type='payment' AND status='open'
        AND due_date IS NOT NULL AND due_date < date('now')""", (pid,)).fetchall()
    if overdue:
        lines.append("· ⚠️ 逾期应收：")
        for o in overdue:
            lines.append(f"    {o['subject']} {o['amount']/10000:.1f} 万（应到 {o['due_date']}）")
    # 开放承诺
    promises = db.execute("""SELECT subject, due_date FROM events
        WHERE project_id=? AND event_type='promise' AND status='open'""", (pid,)).fetchall()
    if promises:
        lines.append("· 📌 待兑现承诺：")
        for p in promises[:3]:
            lines.append(f"    「{p['subject'][:25]}」" + (f"（到期 {p['due_date']}）" if p['due_date'] else ""))
    # 最近问题
    issues = db.execute("""SELECT subject FROM events
        WHERE project_id=? AND event_type='issue' ORDER BY id DESC LIMIT 3""", (pid,)).fetchall()
    if issues:
        lines.append("· 🚨 最近问题：" + " / ".join(i["subject"][:20] for i in issues))
    return "\n".join(lines)

# ---------- 每日巡检（主动预警） ----------
def llm_diagnose(pid):
    """大模型项目诊断"""
    if not LLM_AVAILABLE:
        return "⚠️ 大模型未配置"
    db = get_db()
    try:
        proj = dict(db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone() or {})
        events = [dict(r) for r in db.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT 30", (pid,)).fetchall()]
    finally:
        db.close()
    if not proj:
        return "⚠️ 还没有项目——先报一个"
    resp = advisor_llm.diagnose(proj, events)
    return resp if resp else "⚠️ 诊断失败（大模型无响应）"

def llm_report(pid):
    """大模型生成周报"""
    if not LLM_AVAILABLE:
        return "⚠️ 大模型未配置"
    db = get_db()
    try:
        proj = dict(db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone() or {})
        events = [dict(r) for r in db.execute(
            "SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT 30", (pid,)).fetchall()]
    finally:
        db.close()
    if not proj:
        return "⚠️ 还没有项目——先报一个"
    resp = advisor_llm.generate_report(proj, events)
    return resp if resp else "⚠️ 周报生成失败（大模型无响应）"

def build_payment_schedule(db, pid, msg):
    """解析付款条款（预付30%/进度款按月/质保金5%）→ 生成回款计划表"""
    import re
    contract = db.execute("SELECT contract FROM projects WHERE id=?", (pid,)).fetchone()
    total = (contract["contract"] or 0) if contract else 0
    rows = []
    # 预付款：预付X%或首付X%
    m = re.search(r'(?:预付|首付|预付款)[^\d]*?(\d+(?:\.\d+)?)\s*%', msg)
    if m:
        ratio = float(m.group(1)) / 100
        rows.append(("预付款", ratio, total * ratio, "项目启动", "pending"))
    # 质保金
    m = re.search(r'质保[^\d]*?(\d+(?:\.\d+)?)\s*%', msg)
    if m:
        ratio = float(m.group(1)) / 100
        rows.append(("质保金", ratio, total * ratio, "验收后1年", "pending"))
    # 进度款：按月/按节点——剩余部分
    used = sum(r[1] for r in rows)
    remain = 1.0 - used
    if "按月" in msg or "月结" in msg or "进度款" in msg:
        # 按月进度款：剩余均分到工期月份（简单按 3 期）
        n = 3
        part = remain / n
        for i in range(n):
            rows.append((f"进度款{i+1}", part, total * part, f"施工期第{i+1}期", "pending"))
    elif remain > 0:
        rows.append(("结算款", remain, total * remain, "竣工验收", "pending"))
    # 入库
    db.execute("DELETE FROM payment_schedule WHERE project_id=?", (pid,))
    for stage, ratio, amount, due, status in rows:
        db.execute("INSERT INTO payment_schedule (project_id,stage,ratio,amount,due_date,status) VALUES (?,?,?,?,?,?)",
                   (pid, stage, ratio, amount, due, status))
    lines = [f"💰 回款计划已生成（合同 {total/10000:.1f} 万）："]
    for r in rows:
        lines.append(f"· {r[0]}：{r[2]/10000:.1f} 万（{r[1]*100:.0f}%）{r[3]}")
    return "\n".join(lines)

# ---------- ④ 节点按工程类型 ----------
PROJECT_STAGE_TEMPLATES = {
    "弱电": [  # 弱电/智能化：深化→采购→敷线→安装→调试→验收
        ("深化设计", 0.15, "点位复核、图纸深化、设备清单确认"),
        ("设备采购", 0.20, "主设备下单、到货验收、样品确认"),
        ("管线敷设", 0.20, "桥架/管路敷设、线缆穿放、隐蔽验收"),
        ("设备安装", 0.25, "前端设备安装、机房设备就位、接线"),
        ("调试联调", 0.12, "单点调试、系统联调、试运行"),
        ("验收交付", 0.08, "竣工验收、培训移交、资料归档"),
    ],
    "装修": [  # 装修/装饰：设计→拆除→水电→泥木→油漆→收尾
        ("设计深化", 0.15, "效果图深化、材料选型、预算确认"),
        ("拆除放线", 0.10, "拆改、放线、砌筑、防水基层"),
        ("水电改造", 0.20, "水电管线敷设、隐蔽验收、防水"),
        ("泥瓦木作", 0.25, "贴砖、吊顶、木作、墙面基层"),
        ("油漆安装", 0.20, "油漆涂饰、主材安装、洁具灯具"),
        ("验收交付", 0.10, "竣工验收、保洁、移交钥匙"),
    ],
    "安装": [  # 安装/机电：会审→采购→基础→安装→调试→验收
        ("图纸会审", 0.10, "图纸会审、技术交底、施工方案"),
        ("设备采购", 0.20, "设备/材料下单、到货验收"),
        ("基础支架", 0.10, "基础浇筑、支架制作、预埋"),
        ("设备安装", 0.35, "设备吊装就位、管道连接、接线"),
        ("调试运行", 0.15, "单机调试、系统试运行、整改"),
        ("验收交付", 0.10, "竣工验收、移交资料、培训"),
    ],
    "土建": [  # 土建/市政：基础→主体→安装→装饰→竣工
        ("基础工程", 0.25, "土方、垫层、基础浇筑、验收"),
        ("主体工程", 0.40, "主体结构施工、砌体、验收"),
        ("安装工程", 0.20, "机电安装、管线预埋、安装验收"),
        ("装饰装修", 0.10, "室内外装修、场地恢复"),
        ("竣工交付", 0.05, "竣工验收、资料归档、移交"),
    ],
}
# 默认（通用）：设计→采购→施工→验收→售后
DEFAULT_STAGES = [
    ("设计/预算确认", 0.15, "施工图/方案确认、预算清单、开工报审"),
    ("材料采购", 0.20, "主材下单、进场验收、样品确认"),
    ("施工实施", 0.55, "分项施工、隐蔽验收、过程检查"),
    ("验收交付", 0.08, "竣工验收、整改销项、移交资料"),
    ("售后/结算", 0.02, "结算对账、质保期服务、尾款回收"),
]

def infer_ptype(name):
    """从项目名推断工程类型（弱电/装修/安装/土建/None通用）——关键词优先级：弱电>安装>装修>土建"""
    if not name:
        return None
    weak = ["弱电", "智能", "监控", "安防", "网络", "布线", "机房", "门禁", "音视频", "会议", "信息化", "道闸", "一卡通"]
    install = ["安装", "机电", "暖通", "空调", "电梯", "消防", "给排水", "管道", "风机", "设备安装", "冷库"]
    decorate = ["装修", "装饰", "精装", "翻新", "家装", "工装", "软装", "整装"]
    civil = ["土建", "市政", "道路", "管网", "桥梁", "绿化", "景观", "基础", "结构", "混凝土", "园林"]
    for kw in weak:
        if kw in name:
            return "弱电"
    for kw in install:
        if kw in name:
            return "安装"
    for kw in decorate:
        if kw in name:
            return "装修"
    for kw in civil:
        if kw in name:
            return "土建"
    return None

def generate_milestones(db, pid, months, start_date=None, ptype=None):
    """按工期生成节点计划（按工程类型选模板——弱电/装修/安装/土建各有施工流程）"""
    from datetime import datetime as dt, timedelta as td
    start = dt.strptime(start_date, "%Y-%m-%d") if start_date else dt.now()
    total_days = int(months * 30)
    stages = PROJECT_STAGE_TEMPLATES.get(ptype, DEFAULT_STAGES) if ptype else DEFAULT_STAGES
    rows = []
    offset = 0
    for name, ratio, note in stages:
        offset += ratio
        plan = (start + td(days=int(total_days * offset))).strftime("%Y-%m-%d")
        db.execute("INSERT INTO project_milestones (project_id, stage, plan_date, note) VALUES (?,?,?,?)", (pid, name, plan, note))
        rows.append((name, plan))
    if ptype:
        db.execute("UPDATE projects SET ptype=? WHERE id=?", (ptype, pid))
    return rows

def milestones_status(db, pid):
    """节点计划状态（专业展示）"""
    if not pid:
        return "⚠️ 请先在项目群里操作"
    rows = [dict(r) for r in db.execute("SELECT * FROM project_milestones WHERE project_id=? ORDER BY plan_date", (pid,))]
    if not rows:
        return "⚠️ 还没有节点计划——请提供工期（如「工期3个月」）——否则顾问无法按节点提醒"
    today = datetime.now().strftime("%Y-%m-%d")
    done = sum(1 for r in rows if r["status"] == "done")
    pct = int(done / len(rows) * 100)
    proj = db.execute("SELECT ptype FROM projects WHERE id=?", (pid,)).fetchone()
    ptype = proj["ptype"] if proj and proj["ptype"] else None
    lines = [f"📅 项目节点计划" + (f"（{ptype}工程）" if ptype else "") + f"（进度 {pct}%）："]
    lines.append(f"`{'█' * (pct // 10)}{'░' * (10 - pct // 10)}` {done}/{len(rows)}")
    for r in rows:
        if r["status"] == "done":
            mark = "✅"
        elif r["status"] == "overdue" or (r["status"] == "pending" and r["plan_date"] < today):
            mark = "🚨"
        else:
            mark = "⏳"
        over = "（已逾期！）" if mark == "🚨" else ""
        note = f" — {r['note']}" if r.get("note") else ""
        lines.append(f"  {mark} **{r['stage']}** {r['plan_date']}{over}\n    {note}")
    lines.append("（发「节点：完成 施工实施」更新；「节点状态」查看）")
    return "\n".join(lines)

def add_milestones(db, msg, pid):
    """补充工期 → 生成节点计划"""
    if not pid:
        return "⚠️ 请先在项目群里操作"
    m = re.search(r'工期[：:]?\s*(\d+(?:\.\d+)?)\s*个月', msg)
    if not m:
        return "⚠️ 请提供工期：如「工期3个月」"
    db.execute("DELETE FROM project_milestones WHERE project_id=?", (pid,))
    proj = db.execute("SELECT name,ptype FROM projects WHERE id=?", (pid,)).fetchone()
    ptype = infer_ptype(proj["name"] if proj else "") or (proj["ptype"] if proj and proj["ptype"] else None)
    rows = generate_milestones(db, pid, float(m.group(1)), ptype=ptype)
    db.execute("UPDATE project_docs SET status='provided', provided_at=datetime('now','localtime') WHERE project_id=? AND doc_name LIKE '%工期%'", (pid,))
    db.execute("UPDATE project_docs SET guide_done=1 WHERE project_id=? AND guide_order=3", (pid,))
    db.commit()
    return "📅 节点计划已生成（按工期 %s 个月" % m.group(1) + ("，**%s工程**流程" % ptype if ptype else "") + "）：\n" + "\n".join(f"· {s}：{d}" for s, d in rows)

def milestone_done(db, msg, pid):
    """标记节点完成：节点：完成 施工实施"""
    if not pid:
        return "⚠️ 请先在项目群里操作"
    m = re.search(r'(?:节点[：:]\s*完成\s*|完成\s*)(.+)', msg)
    name = m.group(1).strip()[:20] if m else ""
    if not name:
        return "⚠️ 用法：节点：完成 施工实施"
    row = db.execute("SELECT * FROM project_milestones WHERE project_id=? AND stage LIKE ?", (pid, f"%{name}%")).fetchone()
    if not row:
        return f"⚠️ 找不到节点「{name}」——发「节点计划」查看"
    db.execute("UPDATE project_milestones SET status='done' WHERE id=?", (row["id"],))
    db.commit()
    return f"✅ 节点「{row['stage']}」已完成。\n" + milestones_status(db, pid)

def docs_status(db, pid):
    """资料清单状态——顾问要求/缺口查询"""
    if not pid:
        return "⚠️ 请先在项目群里操作"
    rows = [dict(r) for r in db.execute("SELECT * FROM project_docs WHERE project_id=? ORDER BY id", (pid,))]
    if not rows:
        return "📋 该项目暂无资料要求清单"
    provided = [r for r in rows if r["status"] == "provided"]
    missing = [r for r in rows if r["status"] == "missing"]
    lines = [f"📋 项目资料清单（已提供 {len(provided)}/{len(rows)}）："]
    for r in rows:
        mark = "✅" if r["status"] == "provided" else "⏳"
        req = f"（{r['required_by']}）" if r.get("required_by") else ""
        lines.append(f"  {mark} {r['doc_name']}{req}")
    if missing:
        lines.append("\n⏳ 还缺：" + "、".join(r["doc_name"] for r in missing))
        lines.append("发「提供资料：合同」标记已提供（或直接发文件）")
    return "\n".join(lines)

def provide_doc(db, msg, pid):
    """标记资料已提供"""
    if not pid:
        return "⚠️ 请先在项目群里操作"
    m = re.search(r'(?:提供资料|已提供|补上)[：: ]*\s*(.+)', msg)
    name = m.group(1).strip()[:30] if m else msg.strip()[:30]
    rows = [dict(r) for r in db.execute("SELECT * FROM project_docs WHERE project_id=? AND status='missing'", (pid,))]
    matched = None
    for r in rows:
        if r["doc_name"] in name or name in r["doc_name"] or r["doc_name"][:2] in name:
            matched = r
            break
    if matched:
        db.execute("UPDATE project_docs SET status='provided', provided_at=datetime('now','localtime') WHERE id=?",
                   (matched["id"],))
        db.commit()
        return f"✅ 已记录「{matched['doc_name']}」已提供。\n" + docs_status(db, pid)
    db.execute("INSERT INTO project_docs (project_id, doc_name, status, provided_at) VALUES (?,?,?,datetime('now','localtime'))",
               (pid, name, "provided"))
    db.commit()
    return f"✅ 已记录「{name}」已提供。\n" + docs_status(db, pid)

def global_query(db, msg):
    """全局查询（跨项目比较）"""
    rows = [dict(r) for r in db.execute("SELECT * FROM projects WHERE status='active'")]
    if not rows:
        return "⚠️ 还没有项目——先新建一个"
    if "回款" in msg or "收款" in msg or "慢" in msg:
        rows.sort(key=lambda p: ((p["received"] or 0) / p["contract"]) if p["contract"] else 1)
        lines = ["📊 项目回款排名（最慢 → 最快）："]
        for p in rows:
            rate = (p["received"] or 0) / p["contract"] * 100 if p["contract"] else 0
            lines.append(f"  {p['name']}: 已收 {(p['received'] or 0)/10000:.1f} 万（{rate:.0f}%）")
        return "\n".join(lines)
    lines = ["📊 所有项目："]
    for p in rows:
        rate = (p["received"] or 0) / p["contract"] * 100 if p["contract"] else 0
        lines.append(f"  {p['name']}: 合同 {p['contract']/10000:.1f} 万｜已收 {(p['received'] or 0)/10000:.1f} 万（{rate:.0f}%）")
    return "\n".join(lines)

def daily_check():
    """每日自动巡检：逾期应收/承诺到期/风险——生成预警（主动推送内容）"""
    db = get_db()
    alerts = []
    # 逾期应收
    rows = db.execute("""SELECT p.name, e.subject, e.amount, e.due_date FROM events e
        JOIN projects p ON e.project_id=p.id
        WHERE e.event_type='payment' AND e.status='open'
        AND e.due_date IS NOT NULL AND e.due_date < date('now')""").fetchall()
    for r in rows:
        alerts.append(f"💸 逾期应收：{r['name']} {r['subject']} {r['amount']/10000:.1f} 万（应到 {r['due_date']}）→ 建议催款")
    # 承诺到期（3天内）
    rows = db.execute("""SELECT p.name, e.subject, e.due_date FROM events e
        JOIN projects p ON e.project_id=p.id
        WHERE e.event_type='promise' AND e.status='open'
        AND e.due_date IS NOT NULL AND e.due_date <= date('now','+3 day')""").fetchall()
    for r in rows:
        alerts.append(f"📌 承诺到期：{r['name']}「{r['subject'][:30]}」（{r['due_date']}）")
    # 高风险组织
    rows = db.execute("SELECT name FROM orgs WHERE risk_level IN ('medium','high')").fetchall()
    for r in rows:
        alerts.append(f"🚨 高风险关联方：{r['name']}——建议核实")
    return alerts

# ---------- 演示 ----------
if __name__ == "__main__":
    db = get_db()
    # 清理演示数据
    for t in ["events", "alerts", "projects", "persons", "orgs", "group_bindings"]:
        db.execute(f"DELETE FROM {t}")
    db.commit()
    db.close()

    print("=== 工程项目 AI 顾问 · 多群接入测试 ===\n")

    # 场景：老板建了两个项目群，拉机器人进群
    print("【场景：两个项目群 + 老板单聊】\n")

    # 群A：新建项目（绑定）
    print("👤 群A（老板）：新建项目：XX大厦办公装修，合同80万，已收30%，负责人王经理")
    print("🤖", handle_message("新建项目：XX大厦办公装修，合同80万，已收30%，负责人王经理", who="老板", group_id="group_A"), "\n")

    # 群A：报进度款
    print("👤 群A（老板）：刚收了进度款20万")
    print("🤖", handle_message("刚收了进度款20万", who="老板", group_id="group_A"), "\n")

    # 群B：新建项目（绑定）
    print("👤 群B（老板）：新建项目：XX厂房改造，合同120万，已收20%，负责人刘工")
    print("🤖", handle_message("新建项目：XX厂房改造，合同120万，已收20%，负责人刘工", who="老板", group_id="group_B"), "\n")

    # 群B：报收款（应记到厂房——不是大厦）
    print("👤 群B（老板）：收到甲方首款15万")
    print("🤖", handle_message("收到甲方首款15万", who="老板", group_id="group_B"), "\n")

    # 群A：查询（应显示大厦——不是厂房）
    print("👤 群A（老板）：项目现在什么情况？")
    print("🤖", handle_message("项目现在什么情况？", who="老板", group_id="group_A"), "\n")

    # 群B：查询（应显示厂房）
    print("👤 群B（老板）：项目现在什么情况？")
    print("🤖", handle_message("项目现在什么情况？", who="老板", group_id="group_B"), "\n")

    # 老板单聊（无群）：全局
    print("👤 单聊（老板）：所有项目什么情况？")
    db = get_db()
    rows = db.execute("SELECT name, contract, received FROM projects").fetchall()
    db.close()
    for r in rows:
        print(f"🤖   {r['name']}: 合同 {r['contract']/10000:.1f} 万｜已收 {r['received']/10000:.1f} 万")
    print()

    # 未绑定群引导
    print("👤 新群C（老板）：甲方说周五来验收")
    print("🤖", handle_message("甲方说周五来验收", who="老板", group_id="group_C"), "\n")

    print("=== 每日自动巡检 ===")
    for a in daily_check():
        print(f"  {a}")

