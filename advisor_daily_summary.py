#!/usr/bin/env python3
"""④ 每日群聊汇总：整理当天 events → 项目日报（进展/问题/承诺/变更）→ 存文件/推送
用法：
  python3 advisor_daily_summary.py --generate   # 每天 22:00 静默生成当日日报（存文件，不推送）
  python3 advisor_daily_summary.py --push       # 每天 8:00 推送昨日日报给老板
  python3 advisor_daily_summary.py --date 2026-08-23 --generate  # 指定日期补生成
无内容静默（watchdog 模式）
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

TENANTS_FILE = "/opt/advisor/wecom/tenants.json"
WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin"


def get_tenants():
    with open(TENANTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return [t for t in data.get("tenants", []) if t.get("db")]


def get_db(path):
    path = os.path.expanduser(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def load_llm():
    sys.path.insert(0, "/opt/advisor")
    try:
        import advisor_llm
        return advisor_llm
    except Exception:
        return None


def summary_dir(db_path):
    d = os.path.dirname(os.path.expanduser(db_path))
    sdir = os.path.join(d, "daily_summaries")
    os.makedirs(sdir, exist_ok=True)
    return sdir


def generate_one(db_path, date_str):
    """生成单个库当日日报 → {项目名: 日报文本}"""
    advisor_llm = load_llm()
    db = get_db(db_path)
    projects = db.execute("SELECT * FROM projects WHERE status='active'").fetchall()
    out = {}
    for p in projects:
        evs = db.execute(
            """SELECT event_type, who, subject, amount, due_date, source, created_at
               FROM events WHERE project_id=? AND created_at LIKE ? ORDER BY id""",
            (p["id"], date_str + "%")).fetchall()
        if not evs:
            continue
        marks = {"progress": "✅", "payment": "💰", "promise": "📌", "change": "🔧",
                 "issue": "⚠️", "contract": "📄", "material": "📦", "visa": "📋"}
        lines = []
        for e in evs:
            mark = marks.get(e["event_type"], "•")
            who = e["who"] or "老板"
            subj = (e["subject"] or "")[:80]
            ts = (e["created_at"] or "")[11:16]
            lines.append(f"{mark} [{ts}] {who}：{subj}")
        ev_txt = "\n".join(lines) or "（无）"
        report = None
        if advisor_llm:
            prompt = f"""你是工程项目助理。根据「{p['name']}」项目今天（{date_str}）的群聊/记录，生成一份【项目日报】：
【今日记录】
{ev_txt}
日报结构（简洁，5-8行）：
📋 {date_str} {p['name']} 日报
一、今日进展：…
二、今日问题：…
三、承诺/待办：…
四、资金动态：…
五、明日关注：…
只基于上面记录，不要编造。"""
            try:
                report = advisor_llm.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=400)
            except Exception:
                report = None
            if report and report.startswith("__LLM_ERROR__"):
                report = None
        if not report:
            report = f"📋 {date_str} {p['name']} 日报（原始记录）：\n{ev_txt}"
        out[p["name"]] = report
    db.close()
    # 写文件
    if out:
        sdir = summary_dir(db_path)
        fpath = os.path.join(sdir, f"{date_str}.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("\n\n".join(out.values()) + "\n")
        return fpath, out
    return None, {}


def generate(date_str=None):
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    total = 0
    for t in get_tenants():
        try:
            fpath, out = generate_one(t["db"], date_str)
            if out:
                print(f"[{t.get('name','?')}] {date_str} 日报 {len(out)} 个项目 → {fpath}")
                total += len(out)
        except Exception as e:
            print(f"[{t.get('name','?')}] 生成失败: {e}", file=sys.stderr)
    if total == 0:
        print("（当日无记录——静默）")
    return total


def push():
    """推送日报——优先发到项目群（大家都能看）；没有群则单聊推给老板/配置接收人"""
    date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    import requests
    # 0. 全局推送通道（任一有凭证的自建应用）
    channel = None
    for t in get_tenants():
        if t.get("agent_id") and str(t.get("agent_id")) != "0" and t.get("secret") and t.get("secret") != "PENDING":
            channel = t
            break
    if not channel:
        print("（无推送通道——静默）", file=sys.stderr)
        return 0
    token = requests.post(f"{WECOM_API}/gettoken", params={
        "corpid": channel["corp_id"], "corpsecret": channel["secret"]}, timeout=10).json().get("access_token", "")
    if not token:
        print("gettoken 失败", file=sys.stderr)
        return 0
    sent = 0
    for t in get_tenants():
        try:
            # 1. 读该租户库的日报
            sdir = summary_dir(t["db"])
            fpath = os.path.join(sdir, f"{date_str}.md")
            fdate = date_str
            if not os.path.exists(fpath):
                files = sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []
                if not files:
                    continue
                fpath = os.path.join(sdir, files[-1])
                fdate = files[-1].replace(".md", "")
            with open(fpath, encoding="utf-8") as f:
                text = f.read().strip()
            if not text:
                continue
            content = f"📊 {fdate} 项目日报\n\n{text}"
            # 2. 该库绑定的真实群（dm: 开头是单聊，跳过）
            db = get_db(t["db"])
            groups = [r["group_id"] for r in db.execute(
                "SELECT group_id FROM group_bindings WHERE group_id NOT LIKE 'dm:%'").fetchall()]
            db.close()
            # 3a. 有群 → 发群里（大家都能看）
            if groups:
                for gid in groups:
                    r = requests.post(f"{WECOM_API}/appchat/send", params={"access_token": token},
                                      json={"chatid": gid, "msgtype": "text",
                                            "text": {"content": content}}, timeout=10).json()
                    if r.get("errcode") == 0:
                        print(f"[{t.get('name','?')}] 已发群 {gid}（{fdate} 日报）")
                        sent += 1
                    else:
                        print(f"[{t.get('name','?')}] 发群失败 {gid}: {r}", file=sys.stderr)
            # 3b. 没群 → 单聊推给接收人
            else:
                touser = t.get("report_tousers") or t.get("owner_userid") or "LuanYongTao"
                if isinstance(touser, list):
                    touser = "|".join(touser)
                r = requests.post(f"{WECOM_API}/message/send", params={"access_token": token},
                                  json={"touser": touser, "msgtype": "text", "agentid": int(channel["agent_id"]),
                                        "text": {"content": content}}, timeout=10).json()
                if r.get("errcode") == 0:
                    print(f"[{t.get('name','?')}] 已单聊推送 {touser}（{fdate} 日报）")
                    sent += 1
                else:
                    print(f"[{t.get('name','?')}] 单聊推送失败: {r}", file=sys.stderr)
        except Exception as e:
            print(f"[{t.get('name','?')}] 推送异常: {e}", file=sys.stderr)
    if sent == 0:
        print("（无日报可推送——静默）")
    return sent


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--push" in args:
        push()
    elif "--generate" in args:
        d = None
        if "--date" in args:
            d = args[args.index("--date") + 1]
        generate(d)
    else:
        print("用法: --generate [--date YYYY-MM-DD] | --push")
