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
"""

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db

# ---------- 意图识别（规则——不耗大模型） ----------
INTENT_RULES = [
    ("payment_received", ["收到", "到账", "回款", "付款", "付了", "打了", "给了", "收款", "收了", "结款"]),
    ("issue",           ["问题", "卡住", "延误", "没到", "还没到", "缺", "拖延", "出事了", "停工", "没来"]),
    ("promise",         ["答应", "承诺", "说好", "保证", "同意", "确认", "没问题", "可以", "ok"]),
    ("signoff",         ["签证", "签认", "签字", "确认单", "联系单"]),
    ("change",          ["变更", "增项", "加钱", "追加", "改方案"]),
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
    if group_id:
        db.execute("INSERT OR REPLACE INTO group_bindings (group_id, project_id) VALUES (?,?)", (group_id, pid))
    db.commit()
    resp = f"✅ 项目已创建：「{name}」合同 {contract/10000:.1f} 万"
    if received:
        resp += f"，已收 {received/10000:.1f} 万"
    resp += "。"
    if months:
        resp += f"工期 {months} 个月。"
    resp += "\n群里说的自动记录——有进度/回款/问题随时说。"
    return resp

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

    # ---- 未绑定群引导 ----
    if group_id and project_id is None:
        return ("📌 这是新项目群——先绑定项目才能记录：\n"
                "· 绑定现有项目：「绑定项目：项目名」\n"
                "· 创建新项目：「新建项目：XXX，合同80万，工期3个月，负责人王经理」")

    projects = [dict(r) for r in db.execute("SELECT * FROM projects WHERE status='active'")]
    proj = extract_project(msg, projects, project_id)
    pid = proj["id"] if proj else project_id
    amount = extract_amount(msg)

    # 命令词：诊断/周报 → 大模型生成
    if any(k in msg for k in ["诊断", "分析一下", "帮我看看这个项目"]):
        return llm_diagnose(pid)
    if any(k in msg for k in ["生成周报", "周报", "写周报", "出一份周报"]):
        return llm_report(pid)

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

    # 承诺 → 记录（带到期日解析）
    if intent == "promise":
        due = parse_due(msg)
        db.execute("INSERT INTO events (project_id,event_type,who,subject,due_date,source) VALUES (?,?,?,?,?,?)",
                   (pid, "promise", who, msg[:40], due, msg))
        db.commit()
        return f"📌 承诺已记录：{who} 说「{msg[:40]}」" + (f"，到期：{due}" if due else "") + "。到期我会提醒。"

    # 进度 → 记录
    if intent == "progress":
        db.execute("INSERT INTO events (project_id,event_type,who,subject,source) VALUES (?,?,?,?,?)",
                   (pid, "progress", who, msg[:60], msg))
        db.commit()
        return f"✅ 进度已记录：「{msg[:50]}」——已存入 {proj['name'] if proj else ''} 项目档案。"

    # 问题/风险 → 记录 + 预警
    if intent == "issue":
        db.execute("INSERT INTO events (project_id,event_type,who,subject,source) VALUES (?,?,?,?,?)",
                   (pid, "issue", who, msg[:60], msg))
        db.execute("INSERT INTO alerts (project_id,type,severity,content) VALUES (?,?,?,?)",
                   (pid, "issue", "warning", f"{who} 报告问题：{msg[:60]}"))
        db.commit()
        return f"⚠️ 问题已记录并预警：「{msg[:50]}」——已通知相关人跟进。"

    # 变更 → 记录 + 提醒签证
    if intent == "change":
        db.execute("INSERT INTO events (project_id,event_type,who,subject,amount,source) VALUES (?,?,?,?,?,?)",
                   (pid, "change", who, msg[:50], amount, msg))
        db.execute("INSERT INTO alerts (project_id,type,severity,content) VALUES (?,?,?,?)",
                   (pid, "change", "warning", f"变更需补签证：{msg[:50]}"))
        db.commit()
        return (f"📝 变更已记录" + (f"（金额 {amount/10000:.1f} 万）" if amount else "") +
                "。⚠️ 提醒：变更必须**书面签证**——口头不算数，建议立即补签证单。")

    # 其他 → 记录
    db.execute("INSERT INTO events (project_id,event_type,who,subject,source) VALUES (?,?,?,?,?)",
               (pid, "record", who, msg[:60], msg))
    db.commit()
    return f"📋 已记录：「{msg[:50]}」"

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

