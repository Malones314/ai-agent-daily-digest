import re
from pathlib import Path

f = Path(r"C:\Users\Malones\ai-digest-cloud\ci-artifact\preview.txt")
t = f.read_text(encoding="utf-8")
secs = re.split(r"^== (.+?)（(\d+)）==$", t, flags=re.M)
urls = []
print("CI 产物校验：")
for i in range(1, len(secs), 3):
    u = re.findall(r"https?://\S+", secs[i + 2])
    urls += u
    print(f"  {secs[i]}: {secs[i+1]} 条（链接 {len(u)}）")
print(f"  合计链接 {len(urls)}，唯一 {len(set(urls))}")
# 摘要覆盖率：有 summary 的条目数
# 摘要覆盖率：有 summary 的条目数
count = len(re.findall(r"^\d+\. ", t, flags=re.M))
print(f"  条目总数 {count}")
print("\n--- 前 8 行 ---")
print("\n".join(t.splitlines()[:8]))
print("\n--- AI 项目板块前 3 条 ---")
i = t.find("GitHub 热门 AI 项目")
print("\n".join(t[i : i + 420].splitlines()[:8]))
