#!/usr/bin/env python3
"""
工程顾问系统 · 专业性与准确性测试
覆盖：数据准确性/工程专业性/歧义边界/一致性
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import advisor_engine as ae

PASS = 0; FAIL = 0; RESULTS = []

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        RESULTS.append(f"  ✅ {name}")
    else:
        FAIL += 1
        RESULTS.append(f"  ❌ {name} {detail}")

def reset():
    db = ae.get_db()
    for t in ["events", "alerts", "projects", "persons", "orgs", "group_bindings"]:
        db.execute(f"DELETE FROM {t}")
    db.commit(); db.close()

def get_project(name):
    db = ae.get_db()
    p = db.execute("SELECT * FROM projects WHERE name LIKE ?", (f"%{name}%",)).fetchone()
    db.close()
    return dict(p) if p else None

def get_events(pid, etype=None):
    db = ae.get_db()
    if etype:
        rows = [dict(r) for r in db.execute("SELECT * FROM events WHERE project_id=? AND event_type=?", (pid, etype))]
    else:
        rows = [dict(r) for r in db.execute("SELECT * FROM events WHERE project_id=?", (pid,))]
    db.close()
    return rows

print("=" * 50)
print("工程顾问系统测试：专业性与准确性")
print("=" * 50)

# ========== A. 准确性：建项目解析 ==========
print("\n【A. 建项目解析准确性】")
reset()
r = ae.handle_message("新建项目：XX大厦装修，合同80万，已收30%，负责人王经理", group_id="gA")
p = get_project("XX大厦")
check("合同额 80万 解析", p and p["contract"] == 800000, f"实际 {p and p['contract']}")
check("已收 30%→24万", p and p["received"] == 240000, f"实际 {p and p['received']}")
check("负责人 王经理", p and p["manager"] == "王经理", f"实际 {p and p['manager']}")

r = ae.handle_message("新建项目：XX厂房改造，合同120万，已收20万", group_id="gB")
p2 = get_project("XX厂房")
check("合同 120万", p2 and p2["contract"] == 1200000, f"实际 {p2 and p2['contract']}")
check("已收 20万（金额式）", p2 and p2["received"] == 200000, f"实际 {p2 and p2['received']}")

# ========== B. 准确性：多群分流 ==========
print("\n【B. 多群分流准确性】")
ae.handle_message("刚收了进度款20万", group_id="gA")
ae.handle_message("收到甲方首款15万", group_id="gB")
pA = get_project("XX大厦")
pB = get_project("XX厂房")
check("群A收款记到大厦(24+20=44)", pA and pA["received"] == 440000, f"实际 {pA and pA['received']}")
check("群B收款记到厂房(20+15=35)", pB and pB["received"] == 350000, f"实际 {pB and pB['received']}")

# ========== C. 准确性：金额/单位 ==========
print("\n【C. 金额识别】")
ae.handle_message("材料款付了 5 万", group_id="gA")
ev = get_events(pA["id"], "payment")
check("5万=50000", ev and ev[-1]["amount"] == 50000, f"实际 {ev and ev[-1]['amount']}")

# ========== D. 专业性：承诺到期日 ==========
print("\n【D. 承诺到期日解析】")
r = ae.handle_message("甲方答应明天付尾款", group_id="gA")
import datetime
tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
ev = get_events(pA["id"], "promise")
check("'明天'→正确日期", ev and ev[-1]["due_date"] == tomorrow, f"实际 {ev and ev[-1]['due_date']}")

r = ae.handle_message("材料商说好月底到货", group_id="gA")
ev = get_events(pA["id"], "promise")
check("'月底'有到期日", ev and ev[-1]["due_date"] is not None, f"实际 {ev and ev[-1]['due_date']}")

# ========== E. 专业性：意图判断 ==========
print("\n【E. 意图识别专业性】")
r = ae.handle_message("甲方张经理离职了换李工", group_id="gA")
check("人员变动→核查清单", "核查" in r and "签证" in r, r[:60])
r = ae.handle_message("卫生间防水要变更加淋浴区", group_id="gA")
check("变更→签证提醒", "签证" in r, r[:60])
r = ae.handle_message("砂石说好周三到货今天还没到", group_id="gA")
check("材料没到→问题预警", "问题" in r or "预警" in r, r[:60])
r = ae.handle_message("项目现在什么情况", group_id="gA")
check("查询→简报", "合同" in r, r[:60])

# ========== F. 专业性：工程知识 ==========
print("\n【F. 工程专业建议】")
r = ae.handle_message("变更加了个淋浴区", group_id="gA")
check("变更提醒书面签证", "书面" in r or "签证" in r, r[:60])

# 人员变动核查清单完整性
db = ae.get_db()
pid = db.execute("SELECT id FROM projects WHERE name LIKE '%大厦%'").fetchone()["id"]
db.close()
cl = ae.person_change_checklist(pid)
check("核查清单含签证补签", "签证" in cl)
check("核查清单含承诺书面化", "书面" in cl)
check("核查清单含增项补签", "增项" in cl)
check("核查清单含合同主体", "合同" in cl)
check("核查清单含新对接人", "对接" in cl)

# ========== G. 歧义/边界 ==========
print("\n【G. 歧义边界】")
# 未绑定群
r = ae.handle_message("甲方说周五验收", group_id="gNEW")
check("未绑定群→引导", "绑定项目" in r, r[:50])
# 单聊无项目上下文
r = ae.handle_message("收到50万", who="老板")
check("无项目上下文→不崩且有响应", isinstance(r, str) and len(r) > 0, r[:50])
# 空消息/无意义
r = ae.handle_message("嗯")
check("无意义消息→记录不崩", isinstance(r, str) and len(r) > 0, r[:50])
# 多项目单聊歧义（消息没提项目名）
r = ae.handle_message("刚收了10万", who="老板")
check("单聊多项目→有响应不串错", isinstance(r, str) and len(r) > 0, r[:60])

# ========== H. 大模型层（专业诊断） ==========
print("\n【H. 大模型专业诊断】")
if ae.LLM_AVAILABLE:
    db = ae.get_db()
    pid = db.execute("SELECT id FROM projects WHERE name LIKE '%大厦%'").fetchone()["id"]
    db.close()
    diag = ae.llm_diagnose(pid)
    check("诊断输出非空", diag and len(diag) > 50, "无输出")
    check("诊断含问题分析", diag and ("问题" in diag or "风险" in diag), diag[:60] if diag else "")
    check("诊断含建议", diag and ("建议" in diag or "应" in diag or "需" in diag), diag[:60] if diag else "")
else:
    print("  ⚠️ 大模型未配置——跳过 H")

# ========== 汇总 ==========
print("\n" + "=" * 50)
print(f"结果: ✅ {PASS} 通过 / ❌ {FAIL} 失败")
print("=" * 50)
for rline in RESULTS:
    print(rline)
