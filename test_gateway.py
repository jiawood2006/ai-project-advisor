#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一接入层测试：4 个平台的报文 → 同一个引擎 → 同一个库
运行：/usr/bin/python3 test_gateway.py
只用规则引擎（不调大模型，零 token 消耗）
"""
import os, sys, json, tempfile, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gateway
from gateway import core
from gateway.adapters import get_adapter

FAIL = []
def check(name, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  → {detail}" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)

# ---------- 假引擎：只记录调用（验证路由/隔离，不碰真库） ----------
class FakeEngine:
    def __init__(self): self.calls = []
    def set_db_path(self, p): self.cur_db = p
    def handle_message(self, msg, who="老板", project_id=None, group_id=None):
        self.calls.append({"msg": msg, "who": who, "group_id": group_id, "db": self.cur_db})
        return f"REPLY[{who}|{project_id}|{group_id}]:{msg[:20]}"

# ---------- 1. 四个平台的报文归一化 ----------
print("=== 1. 平台报文归一化 ===")
DT_RAW = {                                  # 钉钉：群里 @机器人
    "conversationId": "cidXXXX==", "conversationType": "2", "msgId": "dt-msg-1",
    "senderStaffId": "staff-001", "senderNick": "张工", "robotCode": "ding_tenant_a",
    "text": {"content": " XX大厦今天收到进度款20万"}, "msgtype": "text",
    "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=abc",
}
WECOM_AIBOT_RAW = {                          # 企微 智能机器人 AI+（解密后的 JSON）
    "msgid": "ai-msg-1", "aibotid": "bot_tenant_b", "chatid": "wr_chat_1",
    "from": {"userid": "zhangsan", "name": "张总"},
    "msgtype": "text", "text": {"content": "XX大厦今天收到进度款20万"},
    "response_url": "https://qyapi.weixin.qq.com/cgi-bin/aibot/response",
}
WECOM_APP_RAW = """<xml><ToUserName><![CDATA[ww_corp_tenant_c]]></ToUserName>
<FromUserName><![CDATA[LiSi]]></FromUserName><CreateTime>1710000000</CreateTime>
<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[XX大厦今天收到进度款20万]]></Content>
<MsgId>app-msg-1</MsgId><ChatId>wr_chat_c</ChatId><AgentID>1000002</AgentID></xml>"""
H5_RAW = {"g": "h5_tenant_d", "msg": "XX大厦今天收到进度款20万", "user": "sess-1", "msg_id": "h5-1"}

cases = [
    ("dingtalk", DT_RAW, dict(tenant="ding_tenant_a", chat_type="group", user_id="staff-001",
                              sender_name="张工", chat_id="cidXXXX==", msg_id="dt-msg-1",
                              text="XX大厦今天收到进度款20万")),
    ("wecom_aibot", WECOM_AIBOT_RAW, dict(tenant="bot_tenant_b", chat_type="group",
                                          user_id="zhangsan", chat_id="wr_chat_1", msg_id="ai-msg-1")),
    ("wecom_app", WECOM_APP_RAW, dict(tenant="ww_corp_tenant_c", chat_type="group",
                                      user_id="LiSi", chat_id="wr_chat_c", msg_id="app-msg-1")),
    ("h5", H5_RAW, dict(tenant="h5_tenant_d", chat_type="group", user_id="sess-1", msg_id="h5-1")),
]
msgs = {}
for platform, raw, expect in cases:
    m = get_adapter(platform).normalize(raw)
    msgs[platform] = m
    ok = all(getattr(m, k) == v for k, v in expect.items())
    check(f"{platform} 归一化", ok, json.dumps({k: getattr(m, k) for k in expect}, ensure_ascii=False))
    check(f"{platform} 文本正确（含前导空格处理）", m.text == "XX大厦今天收到进度款20万", repr(m.text))

print("\n=== 2. 分发路由（租户 → 各自的库；群聊才带 group_id）===")
reg = core.TenantRegistry(path="/tmp/_gw_tenants.json")
reg.tenants = {
    "ding_tenant_a":  {"name": "甲公司(钉钉)", "platform": "dingtalk", "db": "/tmp/_t_a.db"},
    "bot_tenant_b":   {"name": "乙公司(企微AI+)", "platform": "wecom_aibot", "db": "/tmp/_t_b.db"},
    "ww_corp_tenant_c": {"name": "丙公司(企微应用)", "platform": "wecom_app", "db": "/tmp/_t_c.db"},
    "h5_tenant_d":    {"name": "丁公司(H5)", "platform": "h5", "db": "/tmp/_t_d.db"},
}
eng = FakeEngine()
for platform, m in msgs.items():
    r = core.dispatch(m, engine=eng, registry=reg)
    last = eng.calls[-1]
    check(f"{platform} 路由到 {reg.get(m.tenant)['db']}", last["db"] == reg.get(m.tenant)["db"], last["db"])
    check(f"{platform} 群聊带 group_id", last["group_id"] == m.chat_id, str(last["group_id"]))
    check(f"{platform} 有回复", bool(r))

print("\n=== 3. 重试去重（企微/钉钉都会重发，同一 msg_id 只能处理一次）===")
m2 = get_adapter("dingtalk").normalize({**DT_RAW, "msgId": "dt-dup-1"})   # 换新 msgId（否则被第2节的去重命中）
r1 = core.dispatch(m2, engine=eng, registry=reg)
r2 = core.dispatch(m2, engine=eng, registry=reg)   # 同一 msgId
check("第一次有回复", bool(r1))
check("重复消息被静默丢弃", r2 == "", repr(r2))

print("\n=== 4. 未注册客户不崩 ===")
unknown = get_adapter("h5").normalize({"g": "不存在的客户", "msg": "你好"})
check("未注册 → 明确提示", "未注册" in core.dispatch(unknown, engine=eng, registry=reg))

print("\n=== 5. 同一客户两个平台入口 → 同一个库（数据不分裂）===")
m_dt = get_adapter("dingtalk").normalize({**DT_RAW, "msgId": "dt-msg-2"})
core.dispatch(m_dt, engine=eng, registry=reg)
m_h5 = get_adapter("h5").normalize({"g": "ding_tenant_a", "msg": "在建项目几个", "msg_id": "h5-9"})
core.dispatch(m_h5, engine=eng, registry=reg)
check("钉钉与 H5 同客户共用库", eng.calls[-1]["db"] == "/tmp/_t_a.db", eng.calls[-1]["db"])

print("\n=== 6. 真实引擎端到端（离线规则，不调大模型）===")
import advisor_engine as E
for p in ["/tmp/_gw_real_a.db", "/tmp/_gw_real_b.db"]:
    if os.path.exists(p): os.remove(p)
reg2 = core.TenantRegistry(path="/tmp/_gw_tenants.json")
reg2.tenants = {
    "ding_tenant_a": {"name": "甲公司", "platform": "dingtalk", "db": "/tmp/_gw_real_a.db"},
    "bot_tenant_b":  {"name": "乙公司", "platform": "wecom_aibot", "db": "/tmp/_gw_real_b.db"},
}
msg_new = get_adapter("dingtalk").normalize({**DT_RAW, "msgId": "dt-real-1",
    "text": {"content": " 新建项目：测试大厦办公装修 合同80万 工期3个月 负责人 王经理"}})
out = core.dispatch(msg_new, registry=reg2)
check("钉钉新建项目成功", "测试大厦" in out or "已建" in out or "新建" in out, out[:120])
con = sqlite3.connect("/tmp/_gw_real_a.db")
n_a = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
name_a = con.execute("SELECT name FROM projects LIMIT 1").fetchone()
con.close()
check("项目落进甲公司库", n_a == 1, f"count={n_a}")
check("项目名正确", name_a and "测试大厦" in name_a[0], str(name_a))

msg_b = get_adapter("h5").normalize({"g": "bot_tenant_b", "msg": "在建项目几个", "msg_id": "h5-real-1"})
core.dispatch(msg_b, registry=reg2)
con = sqlite3.connect("/tmp/_gw_real_b.db")
n_b = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
con.close()
check("乙公司库独立（看不到甲的项目）", n_b == 0, f"count={n_b}")

print("\n" + "=" * 56)
print("结果:", "全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌ {FAIL}")
sys.exit(0 if not FAIL else 1)
