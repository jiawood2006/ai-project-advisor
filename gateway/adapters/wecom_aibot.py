#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业微信 · 智能机器人 AI+（API 模式）适配器

要点（已在 2026-08-22 全链路实测，细节见技能 wecom-robot-integration/references/wecom-aibot-api-mode.md）：
- 这条通道【免费】且【支持文件消息回调】（自建应用不推文件）
- 与自建应用协议不同：POST body 是 JSON {"encrypt": ...}，解密后是 JSON
- 回复 = 在回调响应体里返回【加密的 stream JSON】，不是单独调接口
- 文件下载回来的内容是企微加密的（OpenPGP 头），要用 AESKey 解密
本适配器只做「归一化 + 回复体构造」，加解密沿用 wecom/wecom_bot.py 里已实测的实现。
"""
import json

from .base import Adapter
from ..core import Message


class WeComAIBotAdapter(Adapter):
    platform = "wecom_aibot"

    def normalize(self, raw, tenant_id=None):
        """raw = 已解密的 JSON dict（解密由 wecom/wecom_bot.py 的 AIBot 类负责）"""
        if isinstance(raw, str):
            raw = json.loads(raw)
        data = raw.get("data", raw) if isinstance(raw, dict) else {}
        frm = data.get("from") or {}
        text = ""
        if isinstance(data.get("text"), dict):
            text = data["text"].get("content", "")
        elif isinstance(data.get("text"), str):
            text = data["text"]
        files = []
        if isinstance(data.get("file"), dict) and data["file"].get("url"):
            files.append({"type": "file", "url": data["file"]["url"],
                          "name": data["file"].get("filename", ""),
                          "note": "企微加密，需用 AESKey 解密后再解析"})
        chat_id = data.get("chatid") or data.get("chat_id")
        return Message(
            platform=self.platform,
            tenant=data.get("aibotid") or tenant_id,
            user_id=frm.get("userid", ""),
            sender_name=frm.get("name") or "老板",
            chat_id=chat_id,
            chat_type="group" if chat_id else "single",
            text=text,
            files=files,
            msg_id=data.get("msgid"),
            reply_url=data.get("response_url"),
            raw=data,
        )

    def build_reply_payload(self, text):
        """构造回调响应体（明文）；加密由现有 AIBot 实现处理"""
        return {"msgtype": "stream", "stream": {"id": "", "finish": True, "content": text}}

    # 发送由 HTTP 回调响应体完成（企微拿着加密后的 JSON 去发消息），不需要单独 send()
    def send(self, msg, text):
        return True   # 实际发送在回调响应里，见 wecom/wecom_bot.py
