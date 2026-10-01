#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉钉适配器 —— 企业内部机器人 · Stream 模式（免域名免备案）

为什么钉钉是"今天就能上"的那条路：
- Stream 模式用【长连接】收消息，不需要公网 URL、不需要域名、不需要备案
- 回复走消息自带的 sessionWebhook（一次 POST 就发出去了），不需要额外鉴权
- 客户侧只要：建应用 → 开机器人 → 把 ClientID/ClientSecret 给我们（3 分钟）

字段映射独立成表：官方字段名如与实际回调有出入，改这张表即可，不动逻辑。
收消息的两种跑法：
  ① 官方 SDK（推荐，一条命令）：pip3 install dingtalk-stream → serve_stream()
  ② 自建长连接：需要自己按钉钉协议实现 websocket 握手（未实现，留了位置）
"""
import json, os, urllib.request

from .base import Adapter
from ..core import Message

# 钉钉机器人回调字段 → 统一字段
FIELD_MAP = {
    "conversationId": "chat_id",
    "conversationType": "chat_type",        # "1"=单聊  "2"=群聊
    "msgId": "msg_id",
    "senderStaffId": "user_id",
    "senderNick": "sender_name",
    "sessionWebhook": "reply_url",
    "robotCode": "robot_code",
}
GROUP_TYPE = "2"        # 钉钉约定：2 = 群聊


class DingTalkAdapter(Adapter):
    platform = "dingtalk"

    # ---------------------------------------------------------- 收
    def normalize(self, raw, tenant_id=None):
        if isinstance(raw, str):
            raw = json.loads(raw)
        data = raw.get("data", raw) if isinstance(raw, dict) else {}
        get = lambda k: data.get(k, raw.get(k) if isinstance(raw, dict) else None)

        text = ""
        t = data.get("text") or raw.get("text")
        if isinstance(t, dict):
            text = t.get("content", "")
        elif isinstance(t, str):
            text = t
        text = (text or "").lstrip()          # 钉钉群里 @机器人 会带一个前导空格

        files = []
        mt = data.get("msgtype") or raw.get("msgtype")
        if mt and mt != "text":
            files.append({"type": mt, "raw": json.dumps(data, ensure_ascii=False)[:2000]})

        chat_type = "group" if str(get("conversationType")) == GROUP_TYPE else "single"
        # 多租户定位：优先用 robotCode（每个客户一个机器人），退回传入的 tenant_id
        tid = data.get("robotCode") or tenant_id
        return Message(
            platform=self.platform,
            tenant=tid,
            user_id=get("senderStaffId") or "",
            sender_name=get("senderNick") or "老板",
            chat_id=get("conversationId"),
            chat_type=chat_type,
            text=text,
            files=files,
            msg_id=get("msgId"),
            reply_url=get("sessionWebhook"),
            raw=data,
        )

    # ---------------------------------------------------------- 发
    def send(self, msg, text, timeout=10):
        if not msg.reply_url:
            return False
        body = json.dumps({"msgtype": "text", "text": {"content": text}}).encode()
        req = urllib.request.Request(msg.reply_url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read())
            return res.get("errcode", 0) == 0
        except Exception as e:
            print(f"[dingtalk] send failed: {e}")
            return False

    # ---------------------------------------------------------- Stream 长连接（需客户凭证）
    def serve_stream(self, client_id, client_secret, engine=None):
        """用官方 SDK 跑长连接。需要 pip3 install dingtalk-stream。
        未装 SDK / 未拿到客户凭证时不启动，只提示——不假装能跑。
        """
        try:
            import dingtalk_stream
        except ImportError:
            raise RuntimeError(
                "缺少 dingtalk-stream：pip3 install dingtalk-stream 后再启动"
                "（客户需提供 ClientID/ClientSecret，并在钉钉后台开启机器人）")

        adapter = self
        reg = None

        class Handler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback):
                data = callback.data if isinstance(callback.data, dict) else json.loads(callback.data)
                tenant_id = data.get("robotCode") or client_id
                engine_mod = engine
                if engine_mod is None:
                    import advisor_engine as engine_mod
                from ..core import dispatch
                reply = dispatch(adapter.normalize(data, tenant_id), engine=engine_mod)
                if reply:
                    try:
                        self.reply_text(reply, callback)
                    except Exception as e:
                        print(f"[dingtalk] reply failed: {e}")
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        cred = dingtalk_stream.Credential(client_id, client_secret)
        client = dingtalk_stream.DingTalkStreamClient(cred)
        client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC, Handler())
        print(f"[dingtalk] Stream 已启动（client_id={client_id[:6]}…）")
        client.start_forever()
