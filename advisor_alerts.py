#!/usr/bin/env python3
"""③ 主动预警：AI 早报/日报——节点到期、回款逾期、风险预警
cron 每天早上 7:30 跑：python3 /opt/advisor/advisor_alerts.py
无内容静默（watchdog 模式）；有内容推送自建应用消息给老板
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

DB = "/opt/advisor/data/advisor.db"
TENANTS_FILE = "/opt/advisor/wecom/tenants.json"
WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin"


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def get_app_credentials():
    """自建应用凭证（主动推送用）"""
    with open(TENANTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    for t in data.get("tenants", []):
        if t.get("agent_id") and t.get("secret"):
            return t
    return None


def get_token(cfg):
    r = requests_post(f"{WECOM_API}/gettoken", params={
        "corpid": cfg["corp_id"], "corpsecret": cfg["secret"]}).json()
    return r.get("access_token", "")


def requests_post(url, **kw):
    import requests
    return requests.post(url, **kw)


def send_text(cfg, token, touser, text):
    import requests
    r = requests.post(f"{WECOM_API}/message/send", params={"access_token": token},
                      json={"touser": touser, "msgtype": "text",
                            "agentid": int(cfg["agent_id"]),
                            "text": {"content": text}}, timeout=10).json()
    return r


def build_report():
    """生成提醒内容；无内容返回 None"""
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    lines = []
    projects = db.execute("SELECT * FROM projects WHERE status='active'").fetchall()
    if not projects:
        return None
    for p in projects:
        pname = p["name"]
        # 1. 节点：今天/明天到期
        ms = db.execute("""SELECT stage, plan_date FROM project_milestones
                           WHERE project_id=? AND plan_date IN (?,?) AND status!='done'""",
                        (p["id"], today, tomorrow)).fetchall()
        for m in ms:
            mark = "今天" if m["plan_date"] == today else "明天"
            lines.append(f"⏰ 【{pname}】{mark}节点：{m['stage']}（{m['plan_date']}）——记得确认/更新状态")
        # 2. 回款：逾期应收
        overdue = db.execute("""SELECT subject, amount, due_date FROM events
            WHERE project_id=? AND event_type='payment' AND status='open'
            AND due_date IS NOT NULL AND due_date < ?""", (p["id"], today)).fetchall()
        for o in overdue:
            lines.append(f"💰 【{pname}】回款逾期：{o['subject']} {o['amount']/10000:.1f} 万（应到 {o['due_date']}）——建议催收")
        # 3. 承诺到期（今天）
        prom = db.execute("""SELECT subject, due_date FROM events
            WHERE project_id=? AND event_type='promise' AND status='open'
            AND due_date IS not NULL AND due_date <= ?""", (p["id"], today)).fetchall()
        for q in prom:
            lines.append(f"📌 【{pname}】承诺到期：{q['subject']}（{q['due_date']}）——该兑现了")
        # 4. 风险未处理（open 超过 3 天）
        risks = db.execute("""SELECT content, created_at FROM alerts
            WHERE project_id=? AND status='open' AND created_at < datetime('now','localtime','-3 days')
            ORDER BY id DESC LIMIT 3""", (p["id"],)).fetchall()
        for rk in risks:
            lines.append(f"🚨 【{pname}】风险待复查：{rk['content'][:50]}（登记于 {rk['created_at'][:10]}）")
    db.close()
    if not lines:
        return None
    header = f"📋 {datetime.now().strftime('%Y-%m-%d')} AI 工程早报\n"
    return header + "\n".join(lines) + "\n\n（回复机器人可处理）"


def main():
    cfg = get_app_credentials()
    if not cfg:
        print("无自建应用凭证", file=sys.stderr)
        sys.exit(0)
    report = build_report()
    if not report:
        sys.exit(0)  # 无内容静默
    token = get_token(cfg)
    if not token:
        print("gettoken 失败", file=sys.stderr)
        sys.exit(0)
    touser = cfg.get("owner_userid", "LuanYongTao")
    r = send_text(cfg, token, touser, report)
    print(report[:200])
    if r.get("errcode") != 0:
        print("发送失败:", r, file=sys.stderr)


if __name__ == "__main__":
    main()
