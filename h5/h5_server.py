#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工程顾问 · 扫码接入服务（H5 + 自助开通）
========================================
三条路由，客户全程扫码，零安装零配置：

  /onboard            开通页（扫【开通码】进来，填一个名字）
  /api/onboard        提交开通 → 返回【专属码】链接
  /t/<token>          专属对话页（扫专属码进来，任何人可发到项目群）
  /qr/<token>.png     专属码图片（可保存/转发）
  /qr/onboard.png     开通码图片（发给新客户的入口）
  /?g=客户标识         兼容老链接（仍可用）
  /api/chat           对话接口（支持 token 或 group_id）

启动：
  PORT=8001 python3 h5/h5_server.py
  ONBOARD_KEY=xxx PORT=8001 python3 h5/h5_server.py   # 只有拿到邀请码的人能开新空间
"""
import os, sys, json, uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR + "/..")
import advisor_engine as ae
from gateway import core as gcore
from gateway.onboard import Onboarder, make_qr

H5_HTML = os.path.join(BASE_DIR, "h5.html")
ONBOARD_HTML = os.path.join(BASE_DIR, "onboard.html")
QR_DIR = os.path.join(BASE_DIR, "qr")
ONBOARDER = Onboarder()
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")   # 反代子路径，例如 /scan（为空则挂在根上）


def _strip_base(path):
    if BASE_PATH and path.startswith(BASE_PATH):
        return path[len(BASE_PATH):] or "/"
    return path


def ensure_group_bind(group_id, project_name=None):
    if not project_name:
        return
    db = ae.get_db()
    cur = db.execute("SELECT id FROM projects WHERE name LIKE ?", (f"%{project_name}%",)).fetchone()
    if cur:
        db.execute("INSERT OR REPLACE INTO group_bindings (group_id, project_id) VALUES (?,?)",
                   (group_id, cur["id"]))
        db.commit()
    db.close()


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>开通 · 工程顾问</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"PingFang SC",sans-serif;background:#EAF2F8;color:#16324F}
.wrap{max-width:460px;margin:0 auto;padding:22px 18px 40px}
.card{background:#fff;border-radius:14px;padding:20px;box-shadow:0 4px 18px rgba(14,42,71,.08);margin-bottom:16px}
h1{font-size:20px;margin:6px 0 4px} p{color:#4A6076;font-size:14px;line-height:1.7;margin:6px 0}
label{display:block;font-size:13px;color:#4A6076;margin:14px 0 6px}
input{width:100%;font-size:16px;padding:12px;border:1px solid #CFE0EC;border-radius:9px;outline:none}
input:focus{border-color:#1D6FA5}
button{width:100%;margin-top:16px;font-size:16px;font-weight:600;padding:13px;border:0;border-radius:9px;background:#1D6FA5;color:#fff}
button:active{background:#0E2A47}
.tip{font-size:12px;color:#7B8FA3;margin-top:10px}
img.qr{display:block;width:220px;height:220px;margin:10px auto}
.code{font-family:ui-monospace,Menlo,monospace;font-size:13px;background:#F4F8FB;border:1px dashed #CFE0EC;border-radius:8px;padding:10px;word-break:break-all}
.err{background:#FDECEC;border:1px solid #F5C2C2;color:#B03030;border-radius:9px;padding:10px;font-size:13px;display:none}
.ok{font-size:20px;font-weight:700;color:#128A8A;text-align:center;margin:4px 0 10px}
</style></head><body><div class="wrap">
<div class="card">
  <h1>工程顾问 · 开通</h1>
  <p>填一个名字就行，10 秒开通。开通后你会拿到一个<b>专属码</b>，发到项目群里，谁扫谁能用。<br>不需要装任何软件、不需要关注公众号。</p>
  <div class="err" id="err"></div>
  <div id="form">
    <label>公司名 / 项目名（必填）</label>
    <input id="name" placeholder="例如：青岛某某装饰 / 某大厦装修项目" autocomplete="off">
    <label>你的称呼（选填，方便机器人称呼你）</label>
    <input id="contact" placeholder="例如：王经理" autocomplete="off">
    <label id="klabel" style="display:none">开通码（服务方提供的邀请码）</label>
    <input id="key" style="display:none" placeholder="开通码" autocomplete="off">
    <button onclick="go()">开通，拿我的专属码</button>
    <div class="tip">开通后可随时把专属码转发给项目上的人一起用；同一个名字＝同一个空间，数据不分散。</div>
  </div>
  <div id="done" style="display:none">
    <div class="ok">开通成功 ✓</div>
    <img class="qr" id="qrimg" alt="专属码">
    <p style="text-align:center;margin-top:-4px">把这个码发到项目群，谁扫谁能用</p>
    <div class="code" id="link"></div>
    <button onclick="copyLink()">复制链接</button>
    <div class="tip">保存这张图，或直接转发链接给项目上的人。</div>
  </div>
</div>
<div class="card" style="padding:14px 20px">
  <p style="margin:0;font-size:13px">想让它进企业微信/钉钉的群，或需要更专业的功能（签证漏项核查、废标核查、资料组卷），告诉服务方即可开通。</p>
</div>
</div>
<script>
const NEED_KEY = __NEED_KEY__;
if (NEED_KEY) { document.getElementById('key').style.display='block'; document.getElementById('klabel').style.display='block'; }
function go(){
  const name=document.getElementById('name').value.trim();
  const err=document.getElementById('err');
  if(name.length<2){ err.style.display='block'; err.textContent='请填公司名或项目名（2 个字以上）'; return; }
  err.style.display='none';
  fetch('__BASE__/api/onboard',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:name,contact:document.getElementById('contact').value.trim(),k:document.getElementById('key').value.trim()})})
   .then(r=>r.json()).then(d=>{
     if(!d.ok){ err.style.display='block'; err.textContent=d.error||'开通失败'; return; }
     document.getElementById('form').style.display='none';
     document.getElementById('done').style.display='block';
     document.getElementById('qrimg').src=d.qr;
     document.getElementById('link').textContent=d.url;
     window.__link=d.url;
   }).catch(e=>{ err.style.display='block'; err.textContent='网络错误：'+e; });
}
function copyLink(){
  const t=window.__link||''; navigator.clipboard && navigator.clipboard.writeText(t);
  alert('已复制：'+t);
}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AdvisorH5/2.0"

    # ------------------------------------------------ 工具
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _base_url(self):
        """用客户请求用的域名/IP 生成二维码——换成正式域名无需改代码"""
        host = self.headers.get("Host") or f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        proto = "https" if (self.headers.get("X-Forwarded-Proto") == "https") else "http"
        return f"{proto}://{host}{BASE_PATH}"

    def _ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0] or "").split(",")[0].strip()

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ------------------------------------------------ GET
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        base = self._base_url()

        path = _strip_base(url.path)
        if path in ("/", "/index.html"):
            url = url._replace(path=path)
            group_id = (q.get("g") or [""])[0]
            if group_id:
                ensure_group_bind(group_id, (q.get("bind") or [None])[0])
            html = (open(H5_HTML, encoding="utf-8").read()
                    .replace("__GROUP_ID__", group_id).replace("__TOKEN__", "")
                    .replace("__BASE__", BASE_PATH))
            return self._send(200, html)

        if path == "/onboard":
            need_key = "true" if os.environ.get("ONBOARD_KEY", "").strip() else "false"
            return self._send(200, PAGE.replace("__NEED_KEY__", need_key).replace("__BASE__", BASE_PATH))

        if path.startswith("/t/"):
            token = path[3:].strip()
            info = ONBOARDER.info(token)
            if not info:
                return self._send(404, "<h3 style='font-family:sans-serif;padding:24px'>链接已失效，请向服务方索取新二维码</h3>")
            html = open(H5_HTML, encoding="utf-8").read()
            html = (html.replace("__GROUP_ID__", info["tenant"]).replace("__TOKEN__", token)
                    .replace("__BASE__", BASE_PATH))
            return self._send(200, html)

        if path.startswith("/qr/"):
            name = path[4:]
            os.makedirs(QR_DIR, exist_ok=True)
            if name == "onboard.png":
                target = f"{base}/onboard"
                path = os.path.join(QR_DIR, "onboard.png")
            elif name.endswith(".png"):
                token = name[:-4]
                info = ONBOARDER.info(token)
                if not info:
                    return self._send(404, b"not found")
                target = f"{base}/t/{token}"
                path = os.path.join(QR_DIR, f"{token}.png")
            else:
                return self._send(404, b"not found")
            if not os.path.exists(path) or name == "onboard.png":
                if make_qr(target, path) is None:
                    return self._send(500, b"qrcode lib missing")
            with open(path, "rb") as f:
                return self._send(200, f.read(), "image/png")

        if path == "/api/health":
            return self._json({"ok": True, "tenants": len(gcore.REGISTRY.tenants),
                               "invite_required": bool(os.environ.get("ONBOARD_KEY", "").strip())})
        return self._send(404, b"not found")

    # ------------------------------------------------ POST
    def do_POST(self):
        url = urlparse(self.path)
        path = _strip_base(url.path)
        if path == "/api/onboard":
            d = self._body()
            r = ONBOARDER.create(d.get("name", ""), d.get("contact", ""), "h5",
                                ip=self._ip(), invite_key=d.get("k", ""))
            if not r.get("ok"):
                return self._json(r, 400)
            base = self._base_url()
            r["url"] = f"{base}/t/{r['token']}"
            r["qr"] = f"{base}/qr/{r['token']}.png"
            r["onboard_qr"] = f"{base}/qr/onboard.png"
            return self._json(r)

        if path == "/api/chat":
            d = self._body()
            msg = (d.get("msg") or "").strip()
            if not msg:
                return self._json({"reply": "请输入内容"})
            token = (d.get("token") or "").strip()
            if token:
                tenant = ONBOARDER.resolve(token)
                if not tenant:
                    return self._json({"reply": "链接已失效，请向服务方索取新二维码"})
                rec = gcore.REGISTRY.get_or_reload(tenant)
                ae.set_db_path(rec["db"])
                group_id = tenant
            else:                                  # 兼容老链接 ?g=客户标识
                group_id = (d.get("group_id") or "").strip() or "h5-default"
                rec = gcore.REGISTRY.get_or_reload(group_id)
                if rec:
                    ae.set_db_path(rec["db"])
            try:
                reply = ae.handle_message(msg, group_id=group_id, who=d.get("who") or "客户")
            except Exception as e:
                import traceback; traceback.print_exc()
                reply = f"服务异常：{e}"
            reply = (reply or "").replace("**", "").replace("##", "")   # 网页里不显示 markdown 记号
            return self._json({"reply": reply})
        return self._send(404, b"not found")

    def log_message(self, *a):
        pass


def main():
    port = int(os.environ.get("PORT", "8000"))
    print(f"✅ 扫码接入服务启动 http://0.0.0.0:{port}{BASE_PATH or '/'}")
    print(f"   开通码：http://<域名或IP>:{port}{BASE_PATH}/onboard  （二维码：{BASE_PATH}/qr/onboard.png）")
    print(f"   邀请码限制：{'开' if os.environ.get('ONBOARD_KEY','').strip() else '关（任何人可开新空间）'}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
