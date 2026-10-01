#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台适配器基类

每个平台只需要回答两个问题：
1. 平台推来的东西（JSON / XML / 表单）怎么变成统一 Message？
2. 统一回复文本怎么发回平台？
"""
from ..core import Message


class Adapter:
    platform = "base"

    def normalize(self, raw, tenant_id=None):
        """raw（平台原始报文）→ Message（或 Message 列表）。子类必须实现。"""
        raise NotImplementedError

    def send(self, msg, text):
        """把 text 发回平台，返回 True/False。子类必须实现。"""
        raise NotImplementedError

    def handle(self, raw, tenant_id=None, engine=None):
        """标准流程：归一化 → 分发 → 回复（发送失败不吞异常，返回 (ok, reply)）"""
        from ..core import dispatch
        reply = dispatch(self.normalize(raw, tenant_id), engine=engine)
        if not reply:
            return True, ""            # 去重命中或空消息 → 静默，但仍算成功（避免平台重试）
        return self.send(self.normalize(raw, tenant_id), reply), reply
