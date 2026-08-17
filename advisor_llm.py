#!/usr/bin/env python3
"""
工程项目 AI 顾问 · 大模型增强层（DeepSeek）
规则引擎兜底 + 大模型处理复杂语义/诊断/报告生成
成本：deepseek-chat ≈ ¥0.003/千 token——便宜
"""
import os, json, re
import urllib.request

def load_key():
    """从 .env 读 DEEPSEEK_API_KEY"""
    env_path = os.path.expanduser("~/.hermes/.env")
    with open(env_path) as f:
        for line in f:
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.strip().split("=", 1)[1].strip().strip('"').strip("'")
    # 备用：.ops_tokens
    tok_path = os.path.expanduser("~/.hermes/scripts/.ops_tokens")
    if os.path.exists(tok_path):
        with open(tok_path) as f:
            for line in f:
                if line.startswith("DEEPSEEK"):
                    return line.strip().split("=", 1)[1].strip()
    return None

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

def chat(messages, temperature=0.3, max_tokens=1000):
    """调用 DeepSeek chat"""
    key = load_key()
    if not key:
        return None
    body = json.dumps({
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
            return d["choices"][0]["message"]["content"]
    except Exception as e:
        return f"__LLM_ERROR__: {e}"

# ---------- 1. 复杂消息结构化提取 ----------
def extract_structured(msg, project_names):
    """大模型理解复杂句 → JSON（意图+字段）
    返回: {"intent": "...", "who": "...", "amount": null, "subject": "...", "due": null}
    """
    prompt = f"""你是工程项目管理助手。解析这条老板发的消息，输出 JSON（不要多余文字）：
消息：「{msg}」
项目列表：{project_names}

意图枚举：payment_received(已收款)/promise(承诺)/issue(问题)/change(变更)/person_change(人员变动)/progress(进度)/query(查询)/report(生成周报)/diagnose(项目诊断)/other

输出格式：
{{"intent":"意图","project":"项目名或null","who":"说话对象或null","amount":金额数字或null,"subject":"简述","due":"到期日YYYY-MM-DD或null"}}"""
    resp = chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=500)
    if not resp or resp.startswith("__LLM_ERROR__"):
        return None
    m = re.search(r'\{.*\}', resp, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

# ---------- 2. 项目诊断 ----------
def diagnose(project, events):
    """深度诊断：进度/成本/回款/风险——分析+建议"""
    prompt = f"""你是资深工程项目管理顾问。基于以下项目数据做诊断，输出中文诊断报告（简洁、结论先行）：

【项目】
{json.dumps(project, ensure_ascii=False)}

【项目记录（记忆图谱事件）】
{json.dumps(events[:20], ensure_ascii=False, indent=1)}

诊断要求：
1. 当前最突出的 3 个问题（按严重度排序）
2. 每个问题的原因分析
3. 具体可执行的建议（老板马上能做的）
4. 风险预警（回款/进度/材料/人员）"""
    resp = chat([{"role": "user", "content": prompt}], temperature=0.4, max_tokens=1200)
    return resp if resp and not resp.startswith("__LLM_ERROR__") else None

# ---------- 3. 周报生成 ----------
def generate_report(project, events):
    """一键生成项目周报"""
    prompt = f"""你是工程项目管理助理。基于以下数据生成一份简洁的项目周报（中文、markdown 结构）：

【项目】{json.dumps(project, ensure_ascii=False)}

【本周记录】
{json.dumps(events[:30], ensure_ascii=False, indent=1)}

周报结构：
## 项目周报：{project.get('name','')}
### 本周进展
### 本周问题
### 回款情况
### 下周计划
### 风险与建议"""
    resp = chat([{"role": "user", "content": prompt}], temperature=0.4, max_tokens=1000)
    return resp if resp and not resp.startswith("__LLM_ERROR__") else None

# ---------- 4. 意图兜底（规则没识别） ----------
def intent_fallback(msg):
    """规则没命中时——大模型判断意图"""
    prompt = f"""判断这条消息的意图（只输出一个词）：
「{msg}」
选项：payment_received/promise/issue/change/person_change/progress/query/report/diagnose/other"""
    resp = chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=10)
    if resp and not resp.startswith("__LLM_ERROR__"):
        resp = resp.strip().lower()
        allowed = ["payment_received","promise","issue","change","person_change","progress","query","report","diagnose","other"]
        for a in allowed:
            if a in resp:
                return a
    return "other"

# ---------- 测试 ----------
if __name__ == "__main__":
    print("=== 大模型增强层测试 ===\n")
    print("1. 复杂句提取测试：")
    r = extract_structured("王工说周三的进度款甲方只批了15万，还有5万下周给", ["XX大厦办公装修"])
    print(f"   结果: {json.dumps(r, ensure_ascii=False) if r else 'LLM不可用'}")
    print("\n2. 意图兜底测试：")
    print(f"   '把上周的会议纪要整理一下' → {intent_fallback('把上周的会议纪要整理一下')}")
    print("\n3. 诊断/周报：用演示项目数据测试")
    demo_project = {"name": "XX大厦办公装修", "contract": 800000, "received": 440000,
                    "stage": "R6施工", "deadline": "2026-11-15"}
    demo_events = [
        {"event_type": "payment", "who": "老板", "subject": "收款", "amount": 200000},
        {"event_type": "promise", "who": "张经理", "subject": "答应下周付尾款", "due_date": "2026-08-24"},
        {"event_type": "issue", "who": "老板", "subject": "砂石材料商还没到货"},
    ]
    print("   诊断：")
    d = diagnose(demo_project, demo_events)
    print(f"   {d[:200] if d else '不可用'}...")
    print("\n   周报：")
    rp = generate_report(demo_project, demo_events)
    print(f"   {rp[:200] if rp else '不可用'}...")
