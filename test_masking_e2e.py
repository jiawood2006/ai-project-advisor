#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验证：真实调用 DeepSeek，证明「线上只看到代号，客户看到真名」
运行：/usr/bin/python3 test_masking_e2e.py
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import advisor_llm as L

SENSITIVE = ("请把下面三要素原样重复一遍：甲方 青岛海纳置业有限公司；联系人 王振国；合同额 1,280,000 元。"
             "另外注意 王振国 的电话是 13805321234。")

captured = {}
_orig = L._chat_raw
def spy(messages, **kw):
    captured["sent"] = json.dumps(messages, ensure_ascii=False)
    return _orig(messages, **kw)
L._chat_raw = spy

print("=== 出站内容（这就是真正发给 DeepSeek 的东西）===")
reply = L.chat([{"role": "user", "content": SENSITIVE}], project="_e2e", max_tokens=300)
sent = captured.get("sent", "")
print(sent[:600] if sent else "(未捕获到出站内容)")

print("\n=== 校验 ===")
secrets = ["青岛海纳置业有限公司", "王振国", "13805321234", "1,280,000"]
leak = [s for s in secrets if s in sent]
print("出站是否泄漏真实值:", ("❌ 泄漏 " + str(leak)) if leak else "✅ 零泄漏（全部为代号）")
print("\n=== 模型回复（经过自动还原后给客户看到的样子）===")
print(reply[:400] if reply else "(LLM 不可用)")
restored = all(s in (reply or "") for s in ["青岛海纳置业有限公司", "王振国", "1,280,000"])
print("\n回复中的真值是否已还原:", "✅ 已还原" if restored else "⚠️ 部分未还原（可能模型改写了代号）")

print("\n=== 审计日志（本地留痕，只有数量没有原值）===")
p = os.path.expanduser("~/.hermes/ai-advisor-vaults/audit.log")
if os.path.exists(p):
    print(open(p, encoding="utf-8").read().strip().split("\n")[-1])
print("\n金库文件权限:", oct(os.stat(os.path.expanduser("~/.hermes/ai-advisor-vaults/_e2e.json")).st_mode)[-3:])
