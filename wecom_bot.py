#!/usr/bin/env python3
"""
工程顾问 · 企业微信接入层（多租户·独立部署版）
================================================
机器人作为"机器人员员"入驻客户企业微信：
  - 老板/总经理单聊机器人（绑定客户项目）
  - 机器人被拉进项目群（群聊——群里的事记到对应项目）

多租户：
  - 每个客户一套企微应用凭证（tenants.json）
  - 每客户独立数据库（数据隔离）
  - 回调按 CorpID 自动分流

部署：
  pip3 install requests cryptography
  配置 tenants.json（凭证不硬编码）
  回调URL：https://你的域名/wecom/callback（企微后台配置）

消息流（企微 → 引擎 → 回复）：
  单聊：FromUserName（员工）→ 引擎 group_id=单聊标识（客户默认项目）
  群聊：ChatId（群）→ 引擎 group_id=群ID（群↔项目绑定）
"""
import os, sys, json, time, base64, hashlib, threading, re
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import requests

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    CRYPTO_OK = True
except ImportError:
    CRYPTO_OK = False

WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, ".."))
import advisor_engine as ae

# ---------- 租户管理 ----------
def load_tenants():
    p = os.path.join(BASE_DIR, "tenants.json")
    if not os.path.exists(p):
        return []
    return json.load(open(p, encoding="utf-8")).get("tenants", [])

TENANTS = load_tenants()          # 每客户：name/corp_id/agent_id/secret/token/aes_key/db
TENANT_BY_CORP = {t["corp_id"]: t for t in TENANTS}


class TenantBot:
    """单客户企微机器人（token 缓存 + 收发）"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._token, self._expire = None, 0
        self._lock = threading.Lock()

    def get_token(self):
        with self._lock:
            if self._token and time.time() < self._expire - 300:
                return self._token
        r = requests.get(f"{WECOM_API}/gettoken", params={
            "corpid": self.cfg["corp_id"], "corpsecret": self.cfg["secret"],
        }, timeout=10).json()
        if r.get("errcode") != 0:
            raise RuntimeError(f"token失败: {r}")
        with self._lock:
            self._token, self._expire = r["access_token"], time.time() + r.get("expires_in", 7200)
        return self._token

    # ---------- 回调解密 ----------
    def _decrypt(self, encrypt):
        if not CRYPTO_OK:
            raise RuntimeError("缺 cryptography：pip3 install cryptography")
        key = base64.b64decode(self.cfg["aes_key"] + "=")
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        raw = cipher.decryptor().update(base64.b64decode(encrypt)) + cipher.decryptor().finalize()
        raw = raw[:-raw[-1]]                       # 去 PKCS7 填充
        msg_len = int.from_bytes(raw[16:20], "big")
        return raw[20:20 + msg_len].decode("utf-8")

    def verify(self, params):
        """回调URL验证：echostr 解密返回"""
        sig = params.get("msg_signature", "")
        ts, nonce, echo = params.get("timestamp", ""), params.get("nonce", ""), params.get("echostr", "")
        s = "".join(sorted([self.cfg["token"], ts, nonce, echo]))
        if hashlib.sha1(s.encode()).hexdigest() != sig:
            return "signature error", 403
        return self._decrypt(echo)

    # ---------- 发送 ----------
    def send_text(self, touser, text):
        r = requests.post(f"{WECOM_API}/message/send", params={"access_token": self.get_token()},
                          json={"touser": touser, "msgtype": "text",
                                "agentid": int(self.cfg["agent_id"]),
                                "text": {"content": text}}, timeout=10).json()
        return r

    # ---------- 消息处理 ----------
    def handle(self, xml_body, timestamp=None, nonce=None):
        # 智能机器人回调是 JSON（msgtype/file/response_url）；自建应用是 XML
        if xml_body and xml_body.lstrip()[:1] == "{":
            try:
                result = self.handle_aibot(json.loads(xml_body), timestamp, nonce)
                try:
                    _mid = (json.loads(xml_body).get("msgid") or "").strip()
                    if _mid:
                        if not hasattr(type(self), "_seen_msgs"):
                            type(self)._seen_msgs = {}
                        type(self)._seen_msgs[_mid] = (time.time(), result)
                except Exception:
                    pass
                return result
            except Exception as e:
                try:
                    with open("/tmp/wecom_bot_debug.log", "a") as f:
                        f.write(f"{time.strftime('%m-%d %H:%M:%S')} [AIBOT异常] {type(e).__name__}: {e}\n")
                except Exception:
                    pass
                return "success"
        msg = ET.fromstring(xml_body)  # 已是明文 XML（decrypt_by_tenant 已解密）
        mtype = msg.findtext("MsgType")
        content = (msg.findtext("Content") or "").strip()
        user = msg.findtext("FromUserName") or ""
        chat_id = msg.findtext("ChatId")          # 群聊才有
        # 调试日志
        try:
            with open("/tmp/wecom_bot_debug.log", "a") as f:
                f.write(f"{time.strftime('%m-%d %H:%M:%S')} mtype={mtype} user={user[:14]} content={content[:60]} chat={chat_id}\n")
        except Exception:
            pass
        # 消息去重：企微 5 秒超时会重试同一消息——只处理一次
        msgid = msg.findtext("MsgId") or ""
        if msgid:
            if msgid in _processed_msgids:
                return "success"
            _processed_msgids.add(msgid)
            if len(_processed_msgids) > 300:
                _processed_msgids.clear()
        # 文件消息：解析 Excel 自动建项目
        if mtype == "file":
            return self.handle_file(msg, user, chat_id)
        # 图片消息：OCR 识别
        if mtype == "image":
            return self.handle_image(msg, user, chat_id)
        # 语音消息：转写
        if mtype == "voice":
            return self.handle_voice(msg, user, chat_id)
        if mtype != "text" or not content:
            return "success"
        # 切到该客户数据库
        ae.set_db_path(self.cfg["db"])
        if chat_id:
            # 群聊：静默记录（不刷屏）——消息自动存档 events，节点/回款/风险/承诺自动更新
            # 老板交互走智能机器人（@触发）；自建应用群里不回复
            try:
                ae.handle_message(content, group_id=chat_id, who=user)
            except Exception as e:
                try:
                    with open("/tmp/wecom_bot_debug.log", "a") as f:
                        f.write(f"{time.strftime('%m-%d %H:%M:%S')} [群静默记录异常] {e}\n")
                except Exception:
                    pass
            return "success"
        else:
            # 单聊：用员工标识作为群ID（客户默认项目）
            reply = ae.handle_message(content, group_id=f"dm:{user}", who=user)
            try:
                with open("/tmp/wecom_bot_debug.log", "a") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} REPLY单聊[{user[:12]}]: {reply[:150]}\n")
            except Exception:
                pass
            try:
                r = self.send_text(user, reply)
                try:
                    with open("/tmp/wecom_bot_debug.log", "a") as f:
                        f.write(f"{time.strftime('%H:%M:%S')} SEND errcode={r.get('errcode')} msg={r.get('errmsg','')[:50]}\n")
                except Exception:
                    pass
            except Exception as e:
                print(f"单聊回复失败: {e}")
        return "success"

    # ---------- 文件消息处理 ----------
    # ---------- 智能机器人（API 模式 JSON 消息）处理 ----------
    def handle_aibot(self, data, timestamp=None, nonce=None):
        """智能机器人回调：{"msgtype":"file","file":{"url":...},"response_url":...,"from":{"userid":...}}"""
        # 消息去重：企微会重试，同一 msgid 10 秒内直接返回上次响应
        try:
            _mid = (data.get("msgid") or "").strip()
            if _mid:
                import threading
                if not hasattr(type(self), "_seen_msgs"):
                    type(self)._seen_msgs = {}
                now = time.time()
                if _mid in type(self)._seen_msgs and now - type(self)._seen_msgs[_mid][0] < 10:
                    return type(self)._seen_msgs[_mid][1]
        except Exception:
            pass
        mtype = data.get("msgtype", "")
        user = (data.get("from") or {}).get("userid", "")
        resp_url = data.get("response_url", "")
        chatid = data.get("chatid") or (data.get("group") or {}).get("chatid") if isinstance(data.get("group"), dict) else data.get("chatid")
        try:
            with open("/tmp/wecom_bot_debug.log", "a") as f:
                f.write(f"{time.strftime('%m-%d %H:%M:%S')} AIBOT mtype={mtype} user={user[:14]} chat={chatid}\n")
        except Exception:
            pass
        ae.set_db_path(self.cfg["db"])
        reply = ""
        if mtype == "file":
            f = data.get("file") or {}
            furl = f.get("url", "")
            fname = f.get("name", "未知文件.xlsx")
            if not furl:
                reply = "⚠️ 未获取到文件下载地址"
            else:
                try:
                    r = requests.get(furl, timeout=60)
                    if r.status_code != 200:
                        reply = f"⚠️ 文件下载失败（HTTP {r.status_code}）"
                    else:
                        raw = r.content
                        # 智能机器人文件是企微加密的（OpenPGP 头）——用 AESKey 解密
                        if raw[:4] != b"PK\x03\x04" and len(raw) >= 16:
                            try:
                                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                                aes_key = base64.b64decode(self.cfg["aes_key"] + "=")
                                iv = aes_key[:16]
                                cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
                                dec = cipher.decryptor()
                                padded = dec.update(raw) + dec.finalize()
                                pad_len = padded[-1] if padded else 0
                                raw = padded[:-pad_len] if pad_len and pad_len <= 32 else padded
                            except Exception as e:
                                try:
                                    with open("/tmp/wecom_bot_debug.log", "a") as f:
                                        f.write(f"{time.strftime('%m-%d %H:%M:%S')} [文件解密失败] {type(e).__name__}: {e}\n")
                                except Exception:
                                    pass
                        ext = ".xlsx"
                        if raw[:2] == b"PK":
                            ext = ".xlsx"
                        elif raw[:4] in (b"\xd0\xcf\x11\xe0",):
                            ext = ".xls"
                        elif raw[:5] in (b"<?xml",) or raw[:1] == b"<":
                            ext = ".xls"
                        path = os.path.join("/tmp/advisor_files", f"{int(time.time())}{ext}")
                        os.makedirs("/tmp/advisor_files", exist_ok=True)
                        with open(path, "wb") as fp:
                            fp.write(raw)
                        fname2 = fname if fname and fname != "未知文件.xlsx" else os.path.basename(path)
                        # 复用自建应用的文件解析逻辑
                        try:
                            info = parse_excel_project(path)
                        except Exception as e:
                            reply = f"⚠️ 文件解析失败：{e}"
                            info = None
                        if info:
                            # 去重：同名项目已存在则绑定现有，不重复建
                            gid2 = chatid or f"dm:{user}"
                            db3 = ae.get_db()
                            dup = db3.execute("SELECT id FROM projects WHERE name=? AND status='active'", (info.get('name'),)).fetchone()
                            if dup:
                                # 更新绑定到现有项目
                                bind = db3.execute("SELECT id FROM group_bindings WHERE group_id=?", (gid2,)).fetchone()
                                if bind:
                                    db3.execute("UPDATE group_bindings SET project_id=? WHERE group_id=?", (dup["id"], gid2))
                                else:
                                    db3.execute("INSERT INTO group_bindings (group_id,project_id) VALUES (?,?)", (gid2, dup["id"]))
                                db3.commit()
                                db3.close()
                                reply = f"📊 项目「{info.get('name')}」已存在（无需重复创建）——已绑定此群。\n发「节点状态」或「有什么风险」开始管理。"
                            else:
                                db3.close()
                                # 工期：文件提取到就用；没有默认 3 个月（1 个月节点分布太密集看着乱）
                                dur = info.get('duration') or 3
                                text = f"新建项目：{info.get('name')}，合同{info.get('amount') or 0}万，工期{dur}个月，负责人{info.get('owner') or '老板'}"
                                reply = ae.handle_message(text, group_id=gid2, who=user)
                                reply = f"📊 已从「{fname2}」识别项目信息并创建：\n{reply}"
                        else:
                            # 没匹配到固定字段——读全表内容 + LLM 智能分析（不限于指定格式）
                            reply = self._analyze_file_content(path, fname2, chatid, user)
                        # 存合同/文档全文（供风险评估/记忆调用）——长文本视为合同/方案
                        try:
                            rows_t = read_excel_rows(path)
                            full_txt = " | ".join(str(c) for row in rows_t[:50] for c in row if c is not None)[:2500]
                            if len(full_txt) > 100:
                                gid = chatid or f"dm:{user}"
                                db2 = ae.get_db()
                                binding = db2.execute("SELECT project_id FROM group_bindings WHERE group_id=?", (gid,)).fetchone()
                                if binding and binding["project_id"]:
                                    db2.execute("INSERT INTO events (project_id,event_type,who,subject,source) VALUES (?,?,?,?,?)",
                                                (binding["project_id"], "contract", user, f"文档：{fname2}", full_txt))
                                    db2.commit()
                                db2.close()
                        except Exception:
                            pass
                except Exception as e:
                    reply = f"⚠️ 文件处理异常：{e}"
        elif mtype == "image":
            # 拍照即记账：收据/签证/验收单 OCR 识别 → 自动入账
            img_url = (data.get("image") or {}).get("url", "") if isinstance(data.get("image"), dict) else ""
            try:
                r = requests.get(img_url, timeout=20)
                if r.status_code == 200:
                    path = f"/tmp/advisor_files/img_{int(time.time())}.jpg"
                    os.makedirs("/tmp/advisor_files", exist_ok=True)
                    with open(path, "wb") as fp:
                        fp.write(r.content)
                    text = ocr_image(path)
                    if text and text.strip():
                        # 尝试识别业务信息（收据/签证/进度）并记录
                        reply = self._handle_image_text(text, user, chatid)
                    else:
                        reply = "收到图片——未识别到文字内容"
                else:
                    reply = f"⚠️ 图片下载失败（HTTP {r.status_code}）"
            except Exception as e:
                reply = f"⚠️ 图片处理异常：{e}"
        elif mtype == "text":
            content = (data.get("text") or {}).get("content", "") if isinstance(data.get("text"), dict) else str(data.get("text", ""))
            # 剥掉 @机器人 前缀（群里@触发时 content 可能带 @机器人名，如 "@智能机器人AI助手 进度怎么样"）
            content = re.sub(r'^@[^\s:：，,]{1,20}\s*[:：]?\s*', '', content).strip()
            gid = chatid or f"dm:{user}"
            if chatid and not content:
                # 群聊纯@无实质内容——不回复（空 stream 企微不展示）
                reply = ""
            else:
                reply = ae.handle_message(content, group_id=gid, who=user)
        else:
            reply = "✅ 已收到"
        # 回复：智能机器人要求回调响应体返回【加密 JSON】（msgtype=stream）
        # 格式：{"encrypt","msg_signature","timestamp","nonce"}（企微解密后得到 stream 回复）
        if resp_url:
            # 也尝试异步 response_url（template_card 等场景用）
            try:
                requests.post(resp_url, json={"msgtype": "stream", "stream": {"id": hashlib.md5(f"{time.time()}{reply[:8]}".encode()).hexdigest()[:16], "finish": True, "content": reply}}, timeout=5)
            except Exception:
                pass
        return self._encrypt_aibot_reply(data, reply, timestamp, nonce)

    def _encrypt_aibot_reply(self, data, reply, timestamp=None, nonce=None):
        """智能机器人回复：加密 JSON（msgtype=stream），回调响应体返回"""
        aibotid = data.get("aibotid", "") or self.cfg.get("receive_id", "")
        ts = timestamp or str(int(time.time()))
        ns = nonce or str(int(time.time() * 1000))[-8:]
        stream_id = hashlib.md5(f"{time.time()}{reply[:16]}".encode()).hexdigest()[:16]
        plaintext = json.dumps({"msgtype": "stream", "stream": {"id": stream_id, "finish": True, "content": reply}}, ensure_ascii=False)
        try:
            aes_key = base64.b64decode(self.cfg["aes_key"] + "=")
            iv = aes_key[:16]
            random16 = os.urandom(16)
            msg = plaintext.encode("utf-8")
            msg_len = len(msg).to_bytes(4, "big")
            raw = random16 + msg_len + msg + aibotid.encode("utf-8")
            # PKCS7 填充（32 字节块）
            pad_len = 32 - (len(raw) % 32)
            raw += bytes([pad_len]) * pad_len
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
            enc = cipher.encryptor()
            encrypted = enc.update(raw) + enc.finalize()
            encrypt_b64 = base64.b64encode(encrypted).decode()
        except Exception as e:
            try:
                with open("/tmp/wecom_bot_debug.log", "a") as f:
                    f.write(f"{time.strftime('%m-%d %H:%M:%S')} [加密回复失败] {type(e).__name__}: {e}\n")
            except Exception:
                pass
            return json.dumps({"reply": reply}, ensure_ascii=False)
        msg_signature = hashlib.sha1("".join(sorted([self.cfg["token"], ts, ns, encrypt_b64])).encode()).hexdigest()
        return json.dumps({"encrypt": encrypt_b64, "msg_signature": msg_signature, "timestamp": ts, "nonce": ns}, ensure_ascii=False)

    def _handle_image_text(self, text, user, chatid):
        """OCR 文本 → LLM 识别业务类型（收据/签证/进度/承诺）→ 记录 + 回答"""
        try:
            import advisor_llm
            sys_p = (
                "你是工程项目顾问。客户发来一张图片（OCR 文字如下）。请判断这是什么（收据/签证单/验收单/进度照片/项目文档/其他），"
                "提取关键业务信息（金额/项目/事项），然后以顾问口吻回复客户：确认收到什么、记了什么、下一步提醒。简洁专业。"
            )
            resp = advisor_llm.chat([{"role": "system", "content": sys_p}, {"role": "user", "content": f"OCR 内容：\n{text[:800]}"}], temperature=0.3, max_tokens=400)
            if resp and not resp.startswith("__LLM_ERROR__"):
                # 尽量记录到当前绑定项目
                gid = chatid or f"dm:{user}"
                db = ae.get_db()
                try:
                    binding = db.execute("SELECT project_id FROM group_bindings WHERE group_id=?", (gid,)).fetchone()
                    pid = binding["project_id"] if binding else None
                    if pid:
                        db.execute("INSERT INTO events (project_id,event_type,who,subject,source) VALUES (?,?,?,?,?)",
                                   (pid, "record", user, f"图片：{text[:50]}", text[:200]))
                        db.commit()
                except Exception:
                    pass
                finally:
                    db.close()
                return f"🖼️ {resp}"
        except Exception:
            pass
        # 兜底：尝试提取项目信息建项目
        info = extract_project_from_text(text)
        if info and info.get("name"):
            try:
                gid = chatid or f"dm:{user}"
                cmd = f"新建项目：{info['name']}，合同{info.get('amount') or 0}万，工期{info.get('duration') or 3}个月，负责人{info.get('owner') or '老板'}"
                r2 = ae.handle_message(cmd, group_id=gid, who=user)
                return f"🖼️ 已从图片识别并创建项目：\n{r2}"
            except Exception:
                pass
        return f"收到图片——识别到内容：{text[:100]}"

    def _analyze_file_content(self, path, fname, chatid=None, user=""):
        """读取文件全部内容 → LLM 结构化提取（项目名/金额/工期/节点）→ 建项目 + 按文件节点覆盖"""
        try:
            rows = read_excel_rows(path)
            lines = []
            for i, row in enumerate(rows[:40]):
                vals = [str(c) for c in row if c is not None and str(c).strip() != ""]
                if vals:
                    lines.append(" | ".join(vals)[:150])
            sample = "\n".join(lines)[:2500]
            if not sample.strip():
                return f"收到文件「{fname}」——文件内容为空或无法读取"
            import advisor_llm
            prompt = (
                f"你是工程项目顾问。客户发来文件「{fname}」，内容如下：\n{sample}\n\n"
                "请提取项目信息，**只输出 JSON**（不要多余文字）：\n"
                '{"name": "项目名称", "amount": 合同金额万元(数字), "duration": 工期月(数字), '
                '"owner": "负责人", "milestones": [{"stage": "节点名称", "date": "YYYY-MM-DD"}]}\n'
                "规则：\n"
                "- 没有的信息用 null；name 必须有才建项目\n"
                "- 如果文件里有节点/里程碑/进度计划/施工计划（含日期），提取为 milestones（按顺序）；没有则 []\n"
                "- 金额统一换算成万元（如 3440324 元 → 344.03）\n"
                "- 日期统一 YYYY-MM-DD"
            )
            resp = advisor_llm.chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=900)
            info = None
            if resp and not resp.startswith("__LLM_ERROR__"):
                try:
                    import json as _json, re as _re
                    m = _re.search(r"\{.*\}", resp, _re.S)
                    if m:
                        info = _json.loads(m.group())
                except Exception:
                    info = None
            # 有项目名 → 建项目（节点按文件覆盖）
            if info and info.get("name"):
                gid = chatid or f"dm:{user}"
                dur = info.get("duration") or 3
                text = f"新建项目：{info['name']}，合同{info.get('amount') or 0}万，工期{dur}个月，负责人{info.get('owner') or '老板'}"
                reply = ae.handle_message(text, group_id=gid, who=user)
                # 文件里有节点 → 覆盖默认测算节点（依据文件）
                ms = info.get("milestones") or []
                if ms:
                    try:
                        db = ae.get_db()
                        binding = db.execute("SELECT project_id FROM group_bindings WHERE group_id=?", (gid,)).fetchone()
                        if binding:
                            db.execute("DELETE FROM project_milestones WHERE project_id=?", (binding["project_id"],))
                            for x in ms:
                                if x.get("stage") and x.get("date"):
                                    db.execute("INSERT INTO project_milestones (project_id, stage, plan_date, note) VALUES (?,?,?,?)",
                                               (binding["project_id"], x["stage"], x["date"], "依据文件"))
                            db.commit()
                        db.close()
                        reply += f"\n\n📅 已按文件提取 {len(ms)} 个节点（替代默认测算）：\n"
                        reply += "\n".join(f"· {x['stage']}：{x['date']}" for x in ms if x.get("stage") and x.get("date"))
                    except Exception:
                        pass
                return reply
            # 没建项目 → 返回分析文本
            if resp and not resp.startswith("__LLM_ERROR__"):
                return f"📄 已分析「{fname}」：\n{resp}"
            return f"收到文件「{fname}」——已读取内容，正在分析..."
        except Exception as e:
            return f"收到文件「{fname}」——解析失败：{e}"

    def handle_file(self, msg, user, chat_id):
        media_id = msg.findtext("MediaId") or ""
        fname = msg.findtext("FileName") or "文件"
        if not media_id:
            return "success"
        try:
            path = self.download_media(media_id, fname)
        except Exception as e:
            try:
                self.send_text(user, f"⚠️ 文件下载失败：{e}")
            except Exception:
                pass
            return "success"
        try:
            info = parse_excel_project(path)
        except Exception as e:
            try:
                self.send_text(user, f"⚠️ 文件解析失败：{e}")
            except Exception:
                pass
            return "success"
        if not info:
            try:
                self.send_text(user, f"收到文件「{fname}」——未识别到项目信息（需包含项目名称和合同金额）")
            except Exception:
                pass
            return "success"
        # 构造新建项目文本 → 走引擎创建（自动绑定该群）
        ae.set_db_path(self.cfg["db"])
        text = f"新建项目：{info.get('name')}，合同{info.get('amount') or 0}万，工期{info.get('duration') or 1}个月，负责人{info.get('owner') or '老板'}"
        reply = ae.handle_message(text, group_id=chat_id or f"dm:{user}", who=user)
        try:
            self.send_text(user, f"📊 已从「{fname}」识别项目信息并创建：\n{reply}")
        except Exception as e:
            print(f"文件回复失败: {e}")
        return "success"

    def download_media(self, media_id, fname):
        tok = self.get_token()
        r = requests.get(f"{WECOM_API}/media/get", params={"access_token": tok, "media_id": media_id}, timeout=30)
        ct = r.headers.get("Content-Type", "")
        if "json" in ct or (r.text and r.text.lstrip().startswith("{")):
            raise RuntimeError(f"media/get 失败: {r.text[:120]}")
        import re as _re
        os.makedirs("/tmp/advisor_files", exist_ok=True)
        safe = _re.sub(r"[^\w.\-]", "_", fname)
        path = os.path.join("/tmp/advisor_files", f"{int(time.time())}_{safe}")
        with open(path, "wb") as f:
            f.write(r.content)
        return path

    # ---------- 图片消息：OCR 识别 ----------
    def handle_image(self, msg, user, chat_id):
        media_id = msg.findtext("MediaId") or ""
        fname = msg.findtext("FileName") or "image.jpg"
        try:
            path = self.download_media(media_id, fname)
            text = ocr_image(path)
        except Exception as e:
            try:
                self.send_text(user, f"⚠️ 图片识别失败：{e}")
            except Exception:
                pass
            return "success"
        if not text.strip():
            try:
                self.send_text(user, "收到图片——未识别到文字内容")
            except Exception:
                pass
            return "success"
        # 尝试从图片文字提取项目信息 → 创建项目；否则按文字自由问答
        info = extract_project_from_text(text)
        ae.set_db_path(self.cfg["db"])
        if info and info.get("name"):
            cmd = f"新建项目：{info['name']}，合同{info.get('amount') or 0}万，工期{info.get('duration') or 1}个月，负责人{info.get('owner') or '老板'}"
            reply = ae.handle_message(cmd, group_id=chat_id or f"dm:{user}", who=user)
            try:
                self.send_text(user, f"🖼️ 已从图片识别并创建项目：\n{reply}")
            except Exception:
                pass
            return "success"
        reply = ae.handle_message(text[:800], group_id=chat_id or f"dm:{user}", who=user)
        try:
            self.send_text(user, f"🖼️ 图片识别内容：{text[:200]}\n\n{reply}")
        except Exception:
            pass
        return "success"

    # ---------- 语音消息：转写 ----------
    def handle_voice(self, msg, user, chat_id):
        media_id = msg.findtext("MediaId") or ""
        try:
            path = self.download_media(media_id, "voice.silk")
            text = voice_to_text(path)
        except Exception as e:
            try:
                self.send_text(user, f"⚠️ 语音识别失败：{e}")
            except Exception:
                pass
            return "success"
        if not text.strip():
            try:
                self.send_text(user, "收到语音——未能识别内容")
            except Exception:
                pass
            return "success"
        ae.set_db_path(self.cfg["db"])
        reply = ae.handle_message(text, group_id=chat_id or f"dm:{user}", who=user)
        try:
            self.send_text(user, f"🎤 已识别：{text}\n\n{reply}")
        except Exception:
            pass
        return "success"


# ---------- HTTP 服务 ----------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        corp = q.get("corp_id", [""])[0]           # 企微回调带 corp_id（企业微信）或从路径
        if url.path == "/wecom/callback":
            # 企微配置回调时：URL 通常带验证参数（msg_signature/timestamp/nonce/echostr）
            params = {k: (v[0] if v else "") for k, v in q.items()}
            bot = tenant_for_callback(params, corp)
            if not bot and params.get("echostr"):
                # GET 验证不带 corp_id、echostr 是随机串（非 XML）——逐个租户试解定位
                for cfg in TENANTS:
                    try:
                        b = TenantBot(cfg)
                        b._decrypt(params["echostr"])
                        bot = b
                        break
                    except Exception:
                        continue
            if not bot:
                self._text("tenant not found", 403)
                return
            result = bot.verify(params)
            if isinstance(result, tuple):
                self._text(result[0], result[1])
            else:
                self._text(result)
        else:
            self._text("ok")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/wecom/callback":
            self._text("not found", 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        # 企微回调 XML：ToUserName=corp_id → 找租户
        try:
            body = body.decode("utf-8", "replace")
            try:
                with open("/tmp/wecom_bot_debug.log", "a") as f:
                    f.write(f"{time.strftime('%m-%d %H:%M:%S')} [BODY] {body[:400]}\n")
            except Exception:
                pass
            # 智能机器人回调是 JSON：{"encrypt": "..."}；自建应用是 XML：<xml><Encrypt>..</Encrypt></xml>
            enc = None
            try:
                enc = json.loads(body).get("encrypt")
            except Exception:
                pass
            if not enc:
                root = ET.fromstring(body)
                enc = root.findtext("Encrypt")
            if not enc:
                raise ValueError("no Encrypt")
            # 从密文解出 ToUserName 前先按 corp_id 参数/或轮询匹配
            # 企微 POST 不带 corp_id——用密文试解（按租户 aes_key 逐个尝试——最多几十个租户可接受）
            bot, xml = decrypt_by_tenant(enc)
            if not bot:
                self._text("corp not found", 403)
                return
            try:
                with open("/tmp/wecom_bot_debug.log", "a") as f:
                    f.write(f"{time.strftime('%m-%d %H:%M:%S')} [POST] bot={bot.cfg.get('name')} xml={xml[:2000]}\n")
            except Exception:
                pass
            result = bot.handle(xml, timestamp=q.get("timestamp", [str(int(time.time()))])[0] if (q := parse_qs(url.query)) else str(int(time.time())), nonce=(q.get("nonce", [""])[0] if q else ""))
            self._text(result)
        except Exception as e:
            try:
                with open("/tmp/wecom_bot_debug.log", "a") as f:
                    f.write(f"{time.strftime('%m-%d %H:%M:%S')} [POST异常] {type(e).__name__}: {e}\n")
            except Exception:
                pass
            self._text(f"error: {e}", 500)

    def _text(self, s, code=200):
        b = str(s).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def tenant_for_callback(params, corp):
    if corp and corp in TENANT_BY_CORP:
        return TenantBot(TENANT_BY_CORP[corp])
    return None


def decrypt_by_tenant(encrypt):
    """按租户 aes_key 逐个尝试解密（POST 回调没有 corp_id，试解定位租户）
    注意：智能机器人 API 模式的 echostr 是纯数字串（非 XML），不做 ToUserName 校验"""
    if not encrypt:
        return None, None
    for cfg in TENANTS:
        try:
            bot = TenantBot(cfg)
            xml = bot._decrypt(encrypt)
            return bot, xml  # 解密成功即匹配（key 精确性由 AES 解密保证）
        except Exception:
            continue
    return None, None


# ---------- 图片 OCR / 语音转写 ----------
def ocr_image(path):
    """tesseract OCR（中文+英文）"""
    import subprocess
    r = subprocess.run(["tesseract", path, "stdout", "-l", "chi_sim+eng"],
                       capture_output=True, text=True, timeout=90)
    return r.stdout.strip()


def extract_project_from_text(text):
    """从文本提取项目信息（图片 OCR 结果）——规则匹配 + LLM 兜底"""
    import re as _re
    m_name = _re.search(r"(?:项目名称|项目名|工程名称)[：:\s]*([^\s，,。；;]+)", text)
    if m_name:
        name = m_name.group(1).strip()
        m_amt = _re.search(r"(?:合同金额|合同价|合同额|金额|造价)[：:\s]*([\d,，.]+万?)", text)
        m_dur = _re.search(r"(?:工期|周期)[：:\s]*([\d.]+)", text)
        m_own = _re.search(r"(?:负责人|项目经理)[：:\s]*([^\s，,。；;]+)", text)
        amt = _re.sub(r"[^\d.]", "", m_amt.group(1)) if m_amt else None
        return {"name": name, "amount": amt,
                "duration": m_dur.group(1) if m_dur else None,
                "owner": m_own.group(1) if m_own else None}
    # LLM 兜底：从合同/协议/表格等任意文本提取项目信息
    try:
        import advisor_llm
        prompt = ("从下面的文本中提取项目信息，只输出 JSON，格式："
                  '{"name": "项目名称", "amount": 合同金额(万元数字,没有就null), "duration": 工期(月数字,没有就null), "owner": "负责人或受托方(没有就null)"}。'
                  "找不到的字段用 null。只输出 JSON。文本：\n" + text[:1500])
        resp = advisor_llm.chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=200)
        if resp and not resp.startswith("__LLM_ERROR__"):
            m = _re.search(r"\{.*\}", resp, _re.S)
            if m:
                d = json.loads(m.group(0))
                if d.get("name"):
                    return {"name": d.get("name"), "amount": d.get("amount"),
                            "duration": d.get("duration"), "owner": d.get("owner")}
    except Exception:
        pass
    return None


_WHISPER = None
_processed_msgids = set()  # 已处理消息 ID（防企微重试重复回复）
def voice_to_text(path):
    """silk → wav → faster-whisper 转写（模型全局缓存）"""
    global _WHISPER
    import pilk
    wav = path.rsplit(".", 1)[0] + ".wav"
    pilk.decode(path, wav)
    if _WHISPER is None:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from faster_whisper import WhisperModel
        _WHISPER = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = _WHISPER.transcribe(wav, language="zh")
    return "".join(s.text for s in segments).strip()


# ---------- Excel 解析（文件消息智能建项目） ----------
def read_excel_rows(path):
    """统一读取 Excel/文档行（.xlsx 用 openpyxl；.xls 用 xlrd；伪 xlsx 实为 Word 则抽文本）——返回 list[tuple]"""
    rows = []
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".xls":
            import xlrd
            wb = xlrd.open_workbook(path)
            ws = wb.sheet_by_index(0)
            for i in range(ws.nrows):
                rows.append(tuple(ws.row_values(i)))
        else:
            import openpyxl
            try:
                wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
                ws = wb.active
                rows = list(ws.iter_rows(values_only=True))
                wb.close()
            except Exception:
                # openpyxl 打不开——可能是 Word(docx) 伪装的 .xlsx，或 csv/其他
                raw = open(path, "rb").read(4)
                if raw[:2] == b"PK":
                    # zip 容器：尝试 word/document.xml（docx）
                    import zipfile, re
                    try:
                        with zipfile.ZipFile(path) as z:
                            xml = z.read("word/document.xml").decode("utf-8", "ignore")
                        text = re.sub(r"<[^>]+>", " ", xml)
                        text = re.sub(r"\s+", " ", text).strip()
                        rows = [(text[:3000],)]
                    except Exception:
                        rows = [(f"（无法解析的文件格式：{ext}）",)]
                else:
                    # 纯文本/csv
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        rows = [(line.strip(),) for line in f.readlines()[:100] if line.strip()]
    except Exception:
        return rows
    return rows


def parse_excel_project(path):
    """解析 Excel（xlsx/xls）：识别项目名称/合同金额/工期/负责人——返回 dict 或 None"""
    import re as _re
    rows = read_excel_rows(path)
    if not rows:
        return None
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    data = [list(r) for r in rows[1:] if any(c is not None for c in r)]

    def find_col(*keys):
        for i, h in enumerate(header):
            if any(k in h for k in keys):
                return i
        return -1

    col_name = find_col("项目名称", "项目名", "工程名称", "项目")
    col_amt = find_col("合同金额", "合同价", "合同额", "金额", "造价")
    col_dur = find_col("工期", "周期", "天数")
    col_own = find_col("负责人", "项目经理", "联系人")
    if col_name < 0 or not data:
        return llm_extract_project(rows[:40])
    first = data[0]
    name = str(first[col_name]).strip() if col_name < len(first) else ""
    amount = str(first[col_amt]).strip() if 0 <= col_amt < len(first) else ""
    duration = str(first[col_dur]).strip() if 0 <= col_dur < len(first) else ""
    owner = str(first[col_own]).strip() if 0 <= col_own < len(first) else ""
    if not name:
        return llm_extract_project(rows[:40])
    m = _re.search(r"(\d+(?:\.\d+)?)", amount.replace("万", ""))
    amt = m.group(1) if m else None
    m2 = _re.search(r"(\d+(?:\.\d+)?)", duration)
    dur = m2.group(1) if m2 else None
    return {"name": name, "amount": amt, "duration": dur, "owner": owner}


def llm_extract_project(rows):
    """表头没匹配到——LLM 从 Excel 内容提取项目信息（读全表，含合计/总价）"""
    try:
        import advisor_llm, re as _re, json as _json
        sample = "\n".join(" | ".join(str(c) if c is not None else "" for c in r) for r in rows)
        sample = sample[:3000]
        prompt = f"""从下面的 Excel 内容中提取项目信息，只输出 JSON：{{"name": "项目名称", "amount": 合同金额(万元,数字), "duration": 工期(月,数字), "owner": "负责人"}}。
要求：
1. 项目名称：从内容/表格标题/文件名语境推断（如"XX中心电器清单"→项目名"XX中心"）
2. amount：优先找"合计/总计/总价/总金额"行（单位是元就除以10000转成万元）；找不到用 null
3. duration/owner：有就填，没有用 null
Excel 内容：
{sample}"""
        resp = advisor_llm.chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=200)
        if resp and not resp.startswith("__LLM_ERROR__"):
            m = _re.search(r"\{.*\}", resp, _re.S)
            if m:
                return _json.loads(m.group(0))
    except Exception:
        pass
    return None


def main():
    if not TENANTS:
        print("❌ 没有租户配置：编辑 wecom/tenants.json")
        sys.exit(1)
    port = int(os.environ.get("PORT", "8000"))
    print(f"✅ 企微多租户接入层启动：0.0.0.0:{port}/wecom/callback")
    print(f"   租户数: {len(TENANTS)}")
    for t in TENANTS:
        print(f"     - {t['name']} (corp: {t['corp_id']}) db: {t['db']}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
