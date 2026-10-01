#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业微信 · 自建应用适配器（回调 XML，需备案域名）

实测边界（务必记住）：
- 文本/图片/语音会推送；**文件消息不推送**（要文件走 AI+ 模式 或 会话存档）
- 回调 5 秒超时 → 企微重试，必须按 MsgId 去重（core.Dedup 已统一处理）
- 发消息需要「企业可信 IP」，否则静默失败
"""
from xml.etree import ElementTree as ET

from .base import Adapter
from ..core import Message


class WeComAppAdapter(Adapter):
    platform = "wecom_app"

    def normalize(self, raw, tenant_id=None):
        """raw = 已解密的明文 XML 字符串（解密由 wecom/wecom_bot.py 的 TenantBot 负责）"""
        if isinstance(raw, dict):
            d = raw
            return Message(platform=self.platform, tenant=tenant_id,
                           user_id=d.get("FromUserName", ""),
                           chat_id=d.get("ChatId"),
                           chat_type="group" if d.get("ChatId") else "single",
                           text=d.get("Content", ""), msg_id=d.get("MsgId"),
                           raw=d)
        root = ET.fromstring(raw)
        g = lambda k: (root.findtext(k) or "")
        chat_id = g("ChatId") or None
        return Message(
            platform=self.platform,
            tenant=g("ToUserName") or tenant_id,
            user_id=g("FromUserName"),
            sender_name="老板",
            chat_id=chat_id,
            chat_type="group" if chat_id else "single",
            text=g("Content"),
            msg_id=g("MsgId"),
            raw=raw,
        )

    def build_reply_xml(self, msg, text):
        return (f"<xml><ToUserName><![CDATA[{msg.user_id}]]></ToUserName>"
                f"<FromUserName><![CDATA[{msg.tenant}]]></FromUserName>"
                f"<CreateTime>{int(__import__('time').time())}</CreateTime>"
                f"<MsgType><![CDATA[text]]></MsgType>"
                f"<Content><![CDATA[{text}]]></Content></xml>")

    def send(self, msg, text):
        return True   # 明文回复体由现有 TenantBot 加密后返回
