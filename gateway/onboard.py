#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫码自助开通（零配置接入）

客户扫【开通码】→ 填一个名字 → 立刻拿到【专属码】
专属码可发到项目群，任何人扫码就能用；同一客户的所有入口指向同一个库。

安全设计
- tenant_id / token 都是不可枚举的随机串（不是自增、不含客户名）
- 专属码 ≠ 库 ID，对外只暴露 token，映射存本地 0600 文件
- 可设 ONBOARD_KEY（邀请码）：只有拿到码的人能开新空间，防公网乱开
- 按 IP 限流（默认每分钟 6 次）
"""
import os, json, time, hmac, hashlib, secrets, threading, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOKENS_PATH = os.environ.get("ADVISOR_TOKENS", os.path.join(HERE, "tokens.json"))
TENANT_DB_DIR = os.environ.get("ADVISOR_TENANT_DB_DIR",
                               os.path.expanduser("~/wiki/ai-project-advisor/tenants"))
RATE_LIMIT = 6          # 每 IP 每分钟
_lock = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Onboarder:
    def __init__(self, registry=None, tokens_path=TOKENS_PATH, db_dir=TENANT_DB_DIR):
        from . import core as _core          # 动态取，保证和消息服务共用同一个注册表对象
        self.registry = registry or _core.REGISTRY
        self.tokens_path = tokens_path
        self.db_dir = db_dir
        os.makedirs(self.db_dir, exist_ok=True)
        self.tokens = {}
        self._hits = {}
        if os.path.exists(self.tokens_path):
            try:
                with open(self.tokens_path, encoding="utf-8") as f:
                    self.tokens = json.load(f)
            except Exception:
                self.tokens = {}

    # ------------------------------------------------ 持久化
    def _save(self):
        tmp = self.tokens_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.tokens, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.tokens_path)

    # ------------------------------------------------ 限流
    def rate_ok(self, ip, limit=RATE_LIMIT):
        now = time.time()
        with _lock:
            hits = [t for t in self._hits.get(ip, []) if now - t < 60]
            if len(hits) >= limit:
                self._hits[ip] = hits
                return False
            hits.append(now)
            self._hits[ip] = hits
            return True

    # ------------------------------------------------ 邀请码
    def invite_ok(self, key):
        """设置了 ONBOARD_KEY 才校验；未设置则开放（自用/内网场景）"""
        want = os.environ.get("ONBOARD_KEY", "").strip()
        if not want:
            return True, ""
        if key and hmac.compare_digest(str(key).strip(), want):
            return True, ""
        return False, "邀请码不对（或没填）——请向服务方索取开通码"

    # ------------------------------------------------ 开通
    def create(self, name, contact="", platform="h5", ip="", invite_key=""):
        name = (name or "").strip()[:40]
        if len(name) < 2:
            return {"ok": False, "error": "请填一个名字（公司名或项目名，2 个字以上）"}
        if not self.rate_ok(ip or "unknown"):
            return {"ok": False, "error": "操作太频繁，请一分钟后再试"}
        ok, err = self.invite_ok(invite_key)
        if not ok:
            return {"ok": False, "error": err}

        tenant_id = "t" + secrets.token_hex(4)                    # 不可枚举
        token = secrets.token_urlsafe(12).replace("-", "a").replace("_", "b")
        db_path = os.path.join(self.db_dir, f"{tenant_id}.db")

        # 建库（开通即建表，客户第一条消息就能落库）
        self.registry.add(tenant_id, name, platform, db_path,
                          {"contact": contact, "source": "扫码自助开通", "created": _now()})
        try:
            import advisor_engine as ae          # 懒引用，避免循环依赖
            ae.set_db_path(db_path)
            ae.get_db().close()                  # 触发建表
        except Exception as e:
            print(f"[onboard] 建库失败（首次发消息时会重试）：{e}")
        with _lock:
            self.tokens[token] = {"tenant": tenant_id, "name": name, "created": _now()}
            self._save()
        return {"ok": True, "tenant": tenant_id, "token": token, "name": name, "db": db_path}

    def resolve(self, token):
        rec = self.tokens.get((token or "").strip())
        return rec["tenant"] if rec else None

    def info(self, token):
        return self.tokens.get((token or "").strip())

    def revoke(self, token):
        with _lock:
            rec = self.tokens.pop((token or "").strip(), None)
            if rec:
                self._save()
            return bool(rec)


# ---------------------------------------------------------------- 二维码
def make_qr(url, out_path, box=10):
    """生成二维码 PNG（qrcode 库 + PIL）"""
    try:
        import qrcode
    except ImportError:
        return None
    img = qrcode.make(url, box_size=box, border=3)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)
    return out_path


def onboard_url(base, token=None):
    base = (base or "").rstrip("/")
    return f"{base}/onboard" if not token else f"{base}/t/{token}"
