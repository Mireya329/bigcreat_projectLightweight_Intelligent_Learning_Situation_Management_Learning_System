# -*- coding: utf-8 -*-
"""解压队长交付包（只解压，不执行任何脚本），并打印校验清单。"""
import hashlib
import os
import zipfile

ZIP = r"D:\微信文档\xwechat_files\wxid_732wgfr20rjk22_c7f2\msg\file\2026-10\组长任务交付包_v2_20261009.zip"
DEST = r"D:\bigcreat_project\vendor"

os.makedirs(DEST, exist_ok=True)
z = zipfile.ZipFile(ZIP)

# 安全检查：防止 zip slip（路径穿越）
for info in z.infolist():
    target = os.path.realpath(os.path.join(DEST, info.filename))
    if not target.startswith(os.path.realpath(DEST)):
        raise SystemExit("发现可疑路径，拒绝解压：%s" % info.filename)

z.extractall(DEST)
print("解压到：%s" % DEST)
print("共 %d 个文件\n" % len(z.infolist()))

sums_path = os.path.join(DEST, "captain_ai_v2", "SHA256SUMS.txt")
print("===== SHA256SUMS.txt =====")
if os.path.exists(sums_path):
    with open(sums_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    print(content)
    print("===== 实际校验 =====")
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        expect, name = parts[0], parts[1]
        path = os.path.join(DEST, "captain_ai_v2", name.replace("/", os.sep))
        if not os.path.exists(path):
            print("缺失：%s" % name)
            continue
        actual = hashlib.sha256(open(path, "rb").read()).hexdigest()
        print("%s  %s" % ("OK  " if actual == expect else "不符", name))
else:
    print("包内没有 SHA256SUMS.txt")
