#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""H5 适配器（扫码网页对话）—— 零配置过渡方案，已上线

客户什么都不用装：扫码 → 网页对话。没有群聊，但可以先让客户用起来。
"""
from .base import Adapter
from ..core import Message


class H5Adapter(Adapter):
    platform = "h5"

    def normalize(self, raw, tenant_id=None):
        """raw = 表单/JSON：{"g": 客户标识, "msg": 文本, "user": 会话ID}"""
        if isinstance(raw, str):
            import json
            raw = json.loads(raw)
        return Message(
            platform=self.platform,
            tenant=raw.get("g") or tenant_id,
            user_id=raw.get("user") or "h5",
            chat_id=raw.get("g"),          # H5 一个客户一个会话空间
            chat_type="group",             # 当成一个"项目空间"用（可绑定项目）
            text=raw.get("msg", ""),
            msg_id=raw.get("msg_id"),
            raw=raw,
        )

    def send(self, msg, text):
        return True    # 回复直接写回 HTTP 响应体（见 h5/h5_server.py）
