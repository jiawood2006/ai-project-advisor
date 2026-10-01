#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫码接入（自助开通）测试
跑法：/usr/bin/python3 test_onboard.py
真实 HTTP 服务 + 真实引擎（离线规则，不消耗 token）
"""
import os, sys, json, time, sqlite3, threading, urllib.request, urllib.error
from http.server import HTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "h5"))

# 测试期间用独立的注册表/令牌/库目录，不碰生产文件
os.environ.setdefault("ADVISOR_MASK", "0")
import gateway.core as gc
import gateway.onboard as go

TMP = "/tmp/_onboard_test"
os.makedirs(TMP, exist_ok=True)
for f in ["tenants.json", "tokens.json"]:
    p = os.path.join(TMP, f)
    if os.path.exists(p):
        os.remove(p)
gc.REGISTRY = gc.TenantRegistry(path=os.path.join(TMP, "tenants.json"))
go.TOKENS_PATH = os.path.join(TMP, "tokens.json")
go.TENANT_DB_DIR = os.path.join(TMP, "tenants_db")

import h5_server as hs
# 真实装配（不手动传 registry）——只有这样才能抓出"开通服务与消息服务各持一个注册表"这类 bug
hs.ONBOARDER = go.Onboarder(tokens_path=go.TOKENS_PATH, db_dir=go.TENANT_DB_DIR)
assert hs.ONBOARDER.registry is gc.REGISTRY, "开通服务与消息服务必须共用同一个注册表对象"

FAIL = []
def check(name, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  → {detail}" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)

srv = HTTPServer(("127.0.0.1", 0), hs.Handler)
PORT = srv.server_address[1]
BASE = f"http://127.0.0.1:{PORT}"
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)
print(f"测试服务：{BASE}\n")

def GET(path, raw=False):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        b = r.read()
    return b if raw else b.decode("utf-8")

def POST_ON(base, path, obj):
    req = urllib.request.Request(base + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def POST(path, obj):
    return POST_ON(BASE, path, obj)

print("=== 1. 扫开通码 → 开通页可用 ===")
page = GET("/onboard")
check("开通页返回", "开通" in page and "/api/onboard" in page)
qr_onboard = GET("/qr/onboard.png", raw=True)
check("开通码图片可生成（PNG）", qr_onboard[:8] == b"\x89PNG\r\n\x1a\n", str(qr_onboard[:8]))

print("\n=== 2. 填一个名字 → 拿到专属码（零配置）===")
code, r1 = POST("/api/onboard", {"name": "青岛测试装饰有限公司", "contact": "王经理"})
check("开通成功", code == 200 and r1.get("ok"), json.dumps(r1, ensure_ascii=False)[:160])
tok1, ten1 = r1.get("token"), r1.get("tenant")
check("返回专属链接", r1.get("url", "").endswith(f"/t/{tok1}"), r1.get("url", ""))
check("返回二维码地址", r1.get("qr", "").endswith(f"/qr/{tok1}.png"))
check("租户 ID 不可枚举（随机，不含公司名）", ten1 and ten1.startswith("t") and len(ten1) == 9, str(ten1))
check("已写进租户注册表", gc.REGISTRY.get(ten1) is not None)
check("已建独立库文件", os.path.exists(os.path.join(TMP, "tenants_db", f"{ten1}.db")))

print("\n=== 3. 扫专属码 → 直接进自己的空间 ===")
tpage = GET(f"/t/{tok1}")
check("专属码页面可打开", "工程顾问" in tpage)
check("页面注入客户空间标识", ten1 in tpage)
check("页面带上 token", tok1 in tpage)
qr1 = GET(f"/qr/{tok1}.png", raw=True)
check("专属码图片可生成", qr1[:8] == b"\x89PNG\r\n\x1a\n")
check("两个客户的码不同", qr1 != qr_onboard)

print("\n=== 4. 扫码后直接用（建项目 → 落进他自己的库）===")
code, rd = POST("/api/chat", {"token": tok1, "msg": "新建项目：测试大厦办公装修 合同80万 工期3个月 负责人 王经理"})
check("对话有回复", code == 200 and rd.get("reply"), json.dumps(rd, ensure_ascii=False)[:140])
con = sqlite3.connect(os.path.join(TMP, "tenants_db", f"{ten1}.db"))
n = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
nm = con.execute("SELECT name FROM projects LIMIT 1").fetchone()
con.close()
check("项目落进该客户的库", n == 1, f"count={n}")
check("项目名正确", nm and "测试大厦" in nm[0], str(nm))

print("\n=== 5. 第二个客户完全隔离 ===")
code, r2 = POST("/api/onboard", {"name": "济南另一家装饰", "contact": "李总"})
tok2, ten2 = r2.get("token"), r2.get("tenant")
check("第二家也能开通", r2.get("ok") and ten2 != ten1)
POST("/api/chat", {"token": tok2, "msg": "在建项目几个"})
con = sqlite3.connect(os.path.join(TMP, "tenants_db", f"{ten2}.db"))
n2 = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
con.close()
check("第二家看不到第一家的项目", n2 == 0, f"count={n2}")
check("第一家数据仍在", os.path.exists(os.path.join(TMP, "tenants_db", f"{ten1}.db")))

print("\n=== 6. 防滥用：坏 token / 空名字 / 限流 / 邀请码 ===")
code, bad = POST("/api/chat", {"token": "伪造的token", "msg": "你好"})
check("伪造 token 被拒", code == 200 and "失效" in bad.get("reply", ""), json.dumps(bad, ensure_ascii=False))
code, empty = POST("/api/onboard", {"name": "x"})
check("名字太短被拒", code == 400 and not empty.get("ok"))
os.environ["ONBOARD_KEY"] = "invite-abc"
code, noKey = POST("/api/onboard", {"name": "无邀请码的公司"})
check("设了邀请码后必须带码", code == 400 and not noKey.get("ok"), json.dumps(noKey, ensure_ascii=False))
code, withKey = POST("/api/onboard", {"name": "带邀请码的公司", "k": "invite-abc"})
check("带对邀请码可开通", withKey.get("ok"), json.dumps(withKey, ensure_ascii=False)[:120])
del os.environ["ONBOARD_KEY"]
codes = [POST("/api/onboard", {"name": f"限流测试{i}"})[1].get("ok") for i in range(10)]
check("超过每分钟 6 次被限流", codes.count(True) <= 6, f"通过 {codes.count(True)} 次")

print("\n=== 7. 撤销（专属码可作废）===")
check("撤销成功", hs.ONBOARDER.revoke(tok2))
code, gone = POST("/api/chat", {"token": tok2, "msg": "还在吗"})
check("撤销后不能用", "失效" in gone.get("reply", ""), json.dumps(gone, ensure_ascii=False))

print("\n=== 8. 反代子路径模式（部署在 /scan 下时，页面里的接口地址必须带前缀）===")
import importlib
os.environ["BASE_PATH"] = "/scan"
importlib.reload(hs)
srv2 = HTTPServer(("127.0.0.1", 0), hs.Handler)
p2 = srv2.server_address[1]
threading.Thread(target=srv2.serve_forever, daemon=True).start()
time.sleep(0.3)
with urllib.request.urlopen(f"http://127.0.0.1:{p2}/scan/onboard", timeout=10) as r:
    page2 = r.read().decode("utf-8")
check("开通页接口地址带前缀", "/scan/api/onboard" in page2 and "__BASE__" not in page2)
code, r3 = POST_ON(f"http://127.0.0.1:{p2}", "/scan/api/onboard", {"name": "前缀模式公司"})
with urllib.request.urlopen(f"http://127.0.0.1:{p2}/scan/t/{r3['token']}", timeout=10) as r:
    chat_page = r.read().decode("utf-8")
check("对话页接口地址带前缀", 'const BASE = "/scan"' in chat_page and 'BASE + "/api/chat"' in chat_page)
check("对话页返回的二维码地址带前缀", f"/scan/qr/{r3['token']}.png" in r3["qr"], r3["qr"])
srv2.shutdown()
os.environ.pop("BASE_PATH", None)

srv.shutdown()
print("\n" + "=" * 56)
print("结果:", "全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌ {FAIL}")
sys.exit(0 if not FAIL else 1)
