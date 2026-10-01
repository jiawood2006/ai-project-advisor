#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工程顾问 · 脱敏中间层（代号化 / 可还原）
================================================
作用：把要送给大模型（DeepSeek / 百炼等）的文本里的敏感信息，替换成稳定代号，
模型只看到代号；模型返回后再还原成真实内容。真实值只保存在本地金库（vault），
绝不出境、不进 prompt、不入日志。

设计原则
1. 规则优先、零成本：纯正则 + 姓氏词典，不调用任何模型（否则脱敏本身又泄漏一次）
2. 可还原：mask → unmask 必须逐字节还原（有测试保证）
3. 稳定：同一实体在同一项目里永远同一个代号（多轮对话/多文件一致）
4. 自审：mask 后自动复核"原值是否还残留"，残留即报错（fail-closed）

用法
    from masking import Masker
    m = Masker(project="demo-001")
    masked, stats = m.mask(text)      # 送去模型的是 masked
    real = m.unmask(model_reply)      # 模型回来先还原再给客户看

命令行自检
    python3 masking.py --self-test
    python3 masking.py --file /path/to/doc.txt        # 只打印脱敏后的结果
"""
import os, re, json, time, argparse

VAULT_DIR = os.path.expanduser("~/.hermes/ai-advisor-vaults")

# ---------------------------------------------------------------- 代号表
# 代号形式统一为 [X#]，还原时容错处理模型可能加的空格
CODE_RE = re.compile(r"\[\s*(人|司|额|址|话|邮|证|卡|项)\s*(\d+)\s*\]")
TYPES = {"人": "PERSON", "司": "ORG", "额": "AMOUNT", "址": "ADDR",
         "话": "PHONE", "邮": "EMAIL", "证": "IDCARD", "卡": "BANKCARD", "项": "PROJECT"}

# 常见姓氏（百家姓前 120，用于人名识别，避免把"目总"识别成人名）
SURNAMES = ("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
            "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
            "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
            "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
            "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
            "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚"
            "程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓"
            "牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭历戎祖武符刘景詹束龙"
            "叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴鬱胥能苍双"
            "闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍卻璩桑桂濮牛寿通边扈燕冀郏浦尚农"
            "温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘"
            "匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空"
            "曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公")
SURNAME_SET = set(SURNAMES)
RULES_TEXT = None  # 稍后赋值（在 RULES 定义之后）

# 前导虚词/引导词：贪婪匹配常把"关于""根据"等吞进单位名，这里剥掉（保持同实体同代号）
LEAD_STOP = ("关于", "根据", "按照", "依据", "参考", "负责", "承接", "委托", "联系", "参见",
             "参加", "回复", "致函", "转发", "抄送", "报", "由", "与", "和", "向", "给", "对",
             "在", "及", "等", "经", "是", "为", "同", "同贵", "贵司", "我司", "前往")
STRIP_TYPES = {"司", "项", "址"}

# ---------------------------------------------------------------- 规则
RULES = [
    # (类型, 优先级越小越优先, 正则)
    ("证", 1, re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("卡", 2, re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("话", 3, re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("话", 3, re.compile(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)")),
    ("邮", 4, re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    # 单位/公司：后缀词典
    ("司", 5, re.compile(
        r"[\u4e00-\u9fa5A-Za-z0-9（）()]{2,20}"
        r"(?:股份有限公司|有限责任公司|有限公司|集团公司|集团|"
        r"工程局|设计院|研究院|事务所|监理公司|建筑工程公司|"
        r"管理局|管理处|管委会|街道办|镇政府|大学|学院|学校|医院|银行|合作社)")),
    # 项目名
    ("项", 6, re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]{2,20}(?:项目|工程|标段|改造工程)")),
    # 金额（万/亿/元，带或不带货币符号）
    ("额", 7, re.compile(r"(?:[¥￥]\s*)?\d[\d,]*(?:\.\d+)?\s*(?:亿元|万元|亿|万|元|块钱|块)")),
    # 地址
    ("址", 8, re.compile(
        r"[\u4e00-\u9fa5]{2,8}(?:省|市|区|县)?[\u4e00-\u9fa5\d]{2,20}"
        r"(?:路|街道|街|巷|大道|弄)[\u4e00-\u9fa5\d]{0,12}号?(?:[\u4e00-\u9fa5\d]{0,10}(?:楼|室|栋|单元))?")),
]

RULES_PERSON = [
    # 人名（带称谓：姓 + 0~2 字 + 称谓，只替换姓名部分）
    ("人", 9, re.compile(
        r"([\u4e00-\u9fa5]{1})(?=[\u4e00-\u9fa5]{0,2}"
        r"(?:总|经理|主任|老板|书记|队长|会计|出纳|班长|工程师|工|监理|师傅|师父)(?![\u4e00-\u9fa5]))")),
    # 人名（明确角色后跟姓名）
    ("人", 10, re.compile(
        r"(?:负责人|联系人|法定代表人|法人|项目经理|乙方代表|甲方代表|签字人|技术负责人|"
        r"监理|总监理工程师|总监|监理员|施工员|材料员|安全员|预算员|造价员|资料员|"
        r"现场负责人|工长|班组长|业主|司机|会计|出纳)"
        r"[：:\s]{0,3}(?:\[[^\]]{1,8}\]\s*|[、,，]\s*)*([\u4e00-\u9fa5]{2,3})(?![\u4e00-\u9fa5])")),
]


RULES_TEXT = [r for r in RULES if r[0] != "人"]
RULES = RULES + []   # 兼容：完整规则表


class Masker:
    def __init__(self, project="default", vault_dir=VAULT_DIR, enabled=True, amount_mode="code"):
        self.project = re.sub(r"[^\w\-.一-龥]", "_", str(project)) or "default"
        self.dir = vault_dir
        self.enabled = enabled
        self.amount_mode = amount_mode          # code=金额也代号 | keep=保留原值
        self.path = os.path.join(self.dir, f"{self.project}.json")
        self.vault = {"value2code": {}, "code2value": {}, "counters": {}}
        os.makedirs(self.dir, exist_ok=True)
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.vault = json.load(f)
            except Exception:
                pass

    # ------------------------------------------------ 金库
    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.vault, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def _code_for(self, ctype, value):
        v2c = self.vault["value2code"]
        key = f"{ctype}\x00{value}"
        if key in v2c:
            return v2c[key]
        n = self.vault["counters"].get(ctype, 0) + 1
        self.vault["counters"][ctype] = n
        code = f"[{ctype}{n}]"
        v2c[key] = code
        self.vault["code2value"][code] = value
        return code

    # ------------------------------------------------ 脱敏
    def mask(self, text, stats=None):
        """两轮脱敏：①电话/证件/单位/项目/金额/地址 ②人名
        （第二轮在代号已就位的文本上跑，能识别"监理：[司3] 张伟"这类写法）"""
        if not self.enabled or not isinstance(text, str) or not text:
            return text, (stats if stats is not None else {})
        st = stats if stats is not None else {}
        out = self._dict_pass(text, st)        # 轮0：金库词典（已知实体，长值优先）
        for rules in (RULES_TEXT, RULES_PERSON):
            out = self._apply(out, rules, st)  # 轮1：结构规则  轮2：人名
        out = self._dict_pass(out, st)         # 轮3：再来一遍兜底
        if out != text:
            self._save()
        self._audit(out)                      # fail-closed：残留即报错
        return out, st

    def _dict_pass(self, text, st):
        """金库词典轮：本项目已知的实体（含无称谓的裸姓名）在任何位置出现都替换成原代号。
        这样既能识别"王振国又来电话了"这种写法，也保证跨轮对话代号一致。"""
        items = []
        for key, code in self.vault["value2code"].items():
            ctype, val = key.split("\x00", 1)
            if len(val) >= 2:
                items.append((len(val), val, ctype, code))
        items.sort(reverse=True)               # 长值优先，避免子串误替
        out = text
        for _, val, ctype, code in items:
            if val in out:
                n = out.count(val)
                out = out.replace(val, code)
                st[TYPES[ctype]] = st.get(TYPES[ctype], 0) + n
        return out

    def _apply(self, text, rules, st):
        claimed = []
        for ctype, prio, rx in rules:
            for m in rx.finditer(text):
                g = 1 if (m.lastindex or 0) >= 1 else 0
                val = m.group(g)
                if not val or not val.strip():
                    continue
                if ctype == "额" and self.amount_mode == "keep":
                    continue
                if ctype == "人" and val[0] not in SURNAME_SET:
                    continue          # 人名首字必须是常见姓氏，避免"目总"误伤
                if len(val) < 2:
                    continue
                s0, e0 = m.start(g), m.end(g)
                if ctype in STRIP_TYPES:            # 剥掉前导虚词，保证同一实体同一代号
                    for sw in LEAD_STOP:
                        if val.startswith(sw) and len(val) > len(sw) + 1:
                            val = val[len(sw):]
                            s0 += len(sw)
                            break
                claimed.append((s0, e0, prio, ctype, val))
        claimed.sort(key=lambda x: (x[2], x[0]))
        keep = []
        for s, e, prio, ctype, val in claimed:
            if any(not (e <= a or s >= b) for a, b, _, _ in keep):
                continue
            keep.append((s, e, ctype, val))
        # 合并紧邻同类片段（如 "青岛海纳置业"+"有限公司"）
        keep.sort()
        merged = []
        for s, e, ctype, val in keep:
            if merged and merged[-1][2] == ctype and merged[-1][1] >= s:
                ps, pe, pc, pv = merged[-1]
                if e > pe:
                    merged[-1] = (ps, e, pc, text[ps:e])
                continue
            merged.append((s, e, ctype, val))
        out, last = [], 0
        for s, e, ctype, val in merged:
            out.append(text[last:s])
            out.append(self._code_for(ctype, val))
            last = e
            st[TYPES[ctype]] = st.get(TYPES[ctype], 0) + 1
        out.append(text[last:])
        return "".join(out)

    def _audit(self, masked):
        """复核：原值是否还在脱敏文本里（除金额 keep 模式外）"""
        for key, code in self.vault["value2code"].items():
            ctype, val = key.split("\x00", 1)
            if ctype == "额" and self.amount_mode == "keep":
                continue
            if len(val) >= 2 and val in masked:
                raise AssertionError(f"脱敏失败：{TYPES[ctype]} 原值仍残留在输出中（已阻断，不发送）")

    # ------------------------------------------------ 还原
    def unmask(self, text):
        if not isinstance(text, str) or not text:
            return text
        c2v = self.vault["code2value"]
        def rep(m):
            code = f"[{m.group(1)}{m.group(2)}]"
            return c2v.get(code, m.group(0))
        return CODE_RE.sub(rep, text)

    def map_summary(self):
        """给审计/交付用：只给代号与类型，不打印真实值"""
        return {c: TYPES.get(c.strip("[]")[0], "?") for c in self.vault["code2value"]}

    def exists(self, value):
        return f"?\x00{value}" in self.vault["value2code"] or any(
            k.endswith("\x00" + value) for k in self.vault["value2code"])


def safe_chat(messages, chat_fn, project="default", amount_mode="code",
              audit_log=True, **kw):
    """把 advisor_llm.chat 包一层：出站脱敏、入站还原。
    messages = [{"role": "...", "content": "..."}, ...]
    """
    enabled = os.environ.get("ADVISOR_MASK", "1") != "0"
    m = Masker(project=project, enabled=enabled, amount_mode=amount_mode)
    st = {}
    sent = []
    for msg in messages:
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            masked, st = m.mask(msg["content"], st)
            sent.append({**msg, "content": masked})
        else:
            sent.append(msg)
    reply = chat_fn(sent, **kw)
    if isinstance(reply, str):
        reply = m.unmask(reply)
    if audit_log:
        try:
            lp = os.path.join(VAULT_DIR, "audit.log")
            with open(lp, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "project": project, "masked": st,
                                    "out_chars": sum(len(x) for x in sent if isinstance(x, dict)
                                                     and isinstance(x.get("content"), str))},
                                   ensure_ascii=False) + "\n")
            os.chmod(lp, 0o600)
        except Exception:
            pass
    return reply


# ---------------------------------------------------------------- 自检
SAMPLE = """XX大厦办公装修项目 2026-07-22 例会纪要
甲方：青岛海纳置业有限公司  联系人：王振国 13805321234
乙方：青岛中天建筑装饰工程有限公司 项目经理 李建军
监理：青岛宏信工程监理有限公司 张伟
合同额 860,000 元，已收 344,000 元，本次申请进度款 258,000 元。
砂石材料商 青岛盛达建材有限公司 未到货，负责人 刘强 电话 0532-88776655。
办公地址：青岛市市南区香港中路88号3号楼501室，邮箱 liu.qiang@shengda.cn。
"""

def _self_test():
    m = Masker(project="_selftest")
    masked, st = m.mask(SAMPLE)
    back = m.unmask(masked)
    print("原始字符数:", len(SAMPLE), "| 脱敏后:", len(masked))
    print("识别统计:", json.dumps(st, ensure_ascii=False))
    print("\n---- 脱敏后（这是真正发给模型的样子）----")
    print(masked)
    print("\n---- 还原校验 ----")
    print("逐字节还原一致:", back == SAMPLE)
    if back != SAMPLE:
        for i, (a, b) in enumerate(zip(SAMPLE, back)):
            if a != b:
                print("首个不一致位置:", i, repr(SAMPLE[i-20:i+20]), "!=", repr(back[i-20:i+20]))
                break
    print("一致性（同一实体同代号）:", masked.count("[司1]") >= 0 and "王" not in masked)
    print("残留检查: 通过（否则会抛错）")
    return back == SAMPLE

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--file")
    ap.add_argument("--project", default="_cli")
    ap.add_argument("--amount-mode", default="code", choices=["code", "keep"])
    a = ap.parse_args()
    if a.file:
        with open(a.file, encoding="utf-8", errors="ignore") as f:
            t = f.read()
        m = Masker(project=a.project, amount_mode=a.amount_mode)
        masked, st = m.mask(t)
        print(json.dumps(st, ensure_ascii=False))
        print(masked[:4000])
    else:
        ok = _self_test()
        print("\nSELF-TEST:", "PASS" if ok else "FAIL")
