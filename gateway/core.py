#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一接入核心：平台无关的消息模型 + 租户注册表 + 分发

设计原则
1. 引擎只有一个（advisor_engine），平台适配器只做两件事：把平台消息【归一化】、把回复【发回去】
2. 新平台 = 新写一个 adapter，不动引擎
3. 同一客户不管从哪个平台进来，都指向【同一个库】（否则客户数据会分裂）
4. 全局按 msg_id 去重（企微/钉钉都会重试，5 秒窗口）
"""
import os, json, time, threading

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(HERE, "tenants.json")

PLATFORMS = {
    "wecom_aibot": "企业微信 · 智能机器人 AI+ 模式（免费，支持文件）",
    "wecom_app": "企业微信 · 自建应用（回调，需备案域名）",
    "dingtalk": "钉钉 · 企业内部机器人 Stream 模式（免域名免备案）",
    "h5": "网页扫码对话（零配置，无群聊）",
    "weixin_group": "微信客户群（走企微客户群实现，非个人微信机器人）",
}


class Message:
    """平台无关的统一消息"""
    __slots__ = ("platform", "tenant", "user_id", "sender_name", "chat_id",
                 "chat_type", "text", "files", "msg_id", "reply_url", "raw")

    def __init__(self, platform, tenant, user_id="", text="", chat_id=None,
                 chat_type="single", sender_name="老板", files=None,
                 msg_id=None, reply_url=None, raw=None):
        self.platform = platform
        self.tenant = tenant
        self.user_id = user_id
        self.sender_name = sender_name or "老板"
        self.chat_id = chat_id
        self.chat_type = chat_type            # single | group
        self.text = (text or "").strip()
        self.files = files or []
        self.msg_id = msg_id
        self.reply_url = reply_url
        self.raw = raw

    @property
    def group_id(self):
        """只把群聊当群（单聊不绑定项目，避免串项目）"""
        return self.chat_id if self.chat_type == "group" else None

    def __repr__(self):
        return (f"<Message {self.platform}/{self.tenant} {self.chat_type} "
                f"user={self.user_id} text={self.text[:20]!r}>")


# ---------------------------------------------------------------- 去重（所有平台都重试）
class Deduper:
    def __init__(self, cap=500):
        self.cap = cap
        self.seen = {}
        self.lock = threading.Lock()

    def first_time(self, key):
        if not key:
            return True
        with self.lock:
            now = time.time()
            if len(self.seen) > self.cap:
                self.seen = {k: v for k, v in self.seen.items() if now - v < 600}
            if key in self.seen:
                return False
            self.seen[key] = now
            return True


DEDUP = Deduper()


# ---------------------------------------------------------------- 租户注册表
class TenantRegistry:
    """tenants.json 结构（每客户一条）：
    {
      "<tenant_id>": {
        "name": "青岛某装饰",
        "platform": "dingtalk",
        "db": "/root/tenants/qd_zs.db",
        "credentials": {"client_id": "...", "client_secret": "..."}   # 按平台不同
      }
    }
    兼容旧结构：wecom/tenants.json（corp_id/agent_id/secret/token/aes_key）
    """

    def __init__(self, path=REGISTRY_PATH):
        self.path = path
        self.tenants = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.tenants = json.load(f)
        return self.tenants

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.tenants, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self, tenant_id):
        return self.tenants.get(tenant_id)

    def get_or_reload(self, tenant_id):
        """内存里没有就重读一次文件——开通服务与消息服务是两个进程/两个实例时必需"""
        t = self.tenants.get(tenant_id)
        if t is None:
            try:
                self.load()
            except Exception:
                return None
            t = self.tenants.get(tenant_id)
        return t

    def add(self, tenant_id, name, platform, db, credentials=None):
        self.tenants[tenant_id] = {
            "name": name, "platform": platform, "db": db,
            "credentials": credentials or {}, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save()
        return self.tenants[tenant_id]

    def by_platform(self, platform):
        return {k: v for k, v in self.tenants.items() if v.get("platform") == platform}

    def find_by_corp(self, corp_id):
        for k, v in self.tenants.items():
            c = v.get("credentials", {})
            if c.get("corp_id") == corp_id:
                return k, v
        return None, None


REGISTRY = TenantRegistry()


# ---------------------------------------------------------------- 分发
def dispatch(msg, engine=None, registry=None):
    """统一入口：归一化后的消息 → 引擎 → 回复文本
    返回回复文本；被去重命中时返回 ""（不重复回复）
    """
    reg = registry or REGISTRY
    tenant = reg.get_or_reload(msg.tenant) if msg.tenant else None
    if tenant is None:
        return "⛔ 未注册的客户，请联系服务方开通"
    if not DEDUP.first_time(f"{msg.platform}:{msg.msg_id}" if msg.msg_id else None):
        return ""
    if not msg.text:
        return ""
    if engine is None:
        import advisor_engine as engine
    engine.set_db_path(tenant["db"])          # 同一客户多平台入口 → 同一个库
    return engine.handle_message(msg.text, who=msg.sender_name, group_id=msg.group_id)
