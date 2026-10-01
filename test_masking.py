#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""脱敏中间层测试：可逆性 / 不泄漏 / 一致性 / 容错 / 真实文档
运行：/usr/bin/python3 test_masking.py
"""
import os, sys, json, importlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from masking import Masker, CODE_RE, VAULT_DIR

FAIL = []
def check(name, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)

S1 = """关于青岛海纳置业有限公司XX大厦办公装修项目的会议纪要
甲方 青岛海纳置业有限公司，联系人 王振国，电话 13805321234
乙方 青岛中天建筑装饰工程有限公司，项目经理 李建军，手机 15953218888
项目合同额 1,280,000 元，已收款 512,000 元，甲方承诺 8 月 30 日前再付 300,000 元
材料供货商 青岛盛达建材有限公司 负责人 刘强 0532-88776655
地址：山东省青岛市市南区香港中路88号3号楼501室  邮箱 liu.qiang@shengda.cn
身份证 370202199001011234 银行卡 6222021234567890123
"""

def main():
    print("=== 1. 可逆性（mask → unmask 必须逐字节还原） ===")
    m = Masker(project="_t1")
    masked, st = m.mask(S1)
    back = m.unmask(masked)
    check("逐字节还原一致", back == S1)
    check("确实发生了替换", masked != S1 and "[司1]" in masked)

    print("\n=== 2. 不泄漏（原值不得出现在发送给模型的文本里）===")
    secrets = ["青岛海纳置业有限公司", "王振国", "13805321234", "李建军", "15953218888",
               "1,280,000 元", "512,000 元", "青岛盛达建材有限公司", "刘强", "0532-88776655",
               "香港中路88号", "liu.qiang@shengda.cn", "370202199001011234", "6222021234567890123"]
    leaked = [s for s in secrets if s in masked]
    check("零泄漏（14 项敏感值全被替换）", not leaked, f"泄漏={leaked}")
    check("识别统计完整", all(k in st for k in ["ORG", "PERSON", "PHONE", "AMOUNT", "ADDR", "EMAIL", "IDCARD", "BANKCARD"]),
          json.dumps(st, ensure_ascii=False))

    print("\n=== 3. 一致性（同一实体跨多次调用 → 同一代号）===")
    m2 = Masker(project="_t1")          # 重新加载金库
    again, _ = m2.mask("王振国又来电话了，青岛海纳置业有限公司同意先付 100,000 元")
    check("人名代号稳定", "[人1]" in again)
    check("单位代号稳定", "[司1]" in again)
    check("金额代号自增不复用", "[额" in again)

    print("\n=== 4. 容错还原（模型可能把代号写歪） ===")
    m3 = Masker(project="_t1")
    for weird in ["[人 1] 已确认", "[ 人1 ] 已确认", "[人1]已确认"]:
        out = m3.unmask(weird)
        check(f"还原 {weird!r}", "王振国" in out, out)

    print("\n=== 5. 金额策略（keep=保留原值，用于需要算术的内部任务） ===")
    mk = Masker(project="_t2", amount_mode="keep")
    mk_masked, mk_st = mk.mask("合同额 1,280,000 元，已收 512,000 元")
    check("keep 模式保留金额", "1,280,000 元" in mk_masked)
    check("keep 模式不再代号化金额", "AMOUNT" not in mk_st)

    print("\n=== 6. 跨进程一致性（新进程仍用同一代号） ===")
    import subprocess
    code = ("import sys;sys.path.insert(0,'%s');from masking import Masker;"
            "m=Masker(project='_t1');print(m.mask('王振国')[0])" % os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    check("新进程输出 [人1]", "[人1]" in r.stdout, r.stdout.strip())

    print("\n=== 7. 真实文档测试（你自己的文件，只打印脱敏结果与统计） ===")
    real = os.path.expanduser("~/Desktop/联通项目/副本项目决策信息表-2024年中国联通山东服务兵大平台研发项目.docx")
    if os.path.exists(real):
        try:
            from docx import Document
            d = Document(real)
            txt = "\n".join(p.text for p in d.paragraphs)
            for t in d.tables:
                for r_ in t.rows:
                    txt += "\n" + " | ".join(c.text for c in r_.cells)
            txt = txt.strip()
            rv = Masker(project="_real_ltdoc")
            rmasked, rst = rv.mask(txt)
            check("真实 docx 可逆", rv.unmask(rmasked) == txt)
            check("真实 docx 有替换", rst != {})
            print("     字符数 %d → %d | 统计 %s" % (len(txt), len(rmasked), json.dumps(rst, ensure_ascii=False)))
            print("     ---- 脱敏前后对照（左=原，右=送去模型） ----")
            orig_lines = [l for l in txt.split("\n") if l.strip()][:4]
            masked_lines = [l for l in rmasked.split("\n") if l.strip()][:4]
            for a, b in zip(orig_lines, masked_lines):
                print("     原: " + a[:60])
                print("     脱: " + b[:60])
        except Exception as e:
            check("真实 docx 测试", False, str(e))
    else:
        print("  ⚠️ 未找到测试用 docx，跳过")

    print("\n" + ("=" * 54))
    print("结果:", "全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌ {FAIL}")
    return 0 if not FAIL else 1

if __name__ == "__main__":
    sys.exit(main())
