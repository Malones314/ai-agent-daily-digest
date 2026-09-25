"""校验 CI 产物：结构、去重、摘要语言。"""
import re
from pathlib import Path

t = Path(r"C:\Users\Malones\ai-digest-cloud\ci-artifact\preview.txt").read_text(encoding="utf-8")
secs = re.split(r"^== (.+?)（(\d+)）==$", t, flags=re.M)
urls, blocks = [], {}
for i in range(1, len(secs), 3):
    name, body = secs[i], secs[i + 2]
    blocks[name] = body
    urls += re.findall(r"https?://\S+", body)

ITEM_RE = re.compile(r"^\d+\. ", flags=re.M)
print("=== 结构与去重 ===")
for k, v in blocks.items():
    print(f"  {k}: {len(ITEM_RE.findall(v))} 条")
print(f"  链接 {len(urls)} 个，唯一 {len(set(urls))} 个")

cjk = re.compile(r"[\u4e00-\u9fff]")
print("\n=== 摘要语言检查 ===")
for k, v in blocks.items():
    lines = [l for l in v.splitlines() if l.strip()]
    summaries = [lines[i + 1] for i, l in enumerate(lines) if re.match(r"^\d+\. ", l) and i + 1 < len(lines)]
    with_cjk = sum(1 for s in summaries if cjk.search(s) and not s.startswith("http"))
    print(f"  {k}: 带中文摘要 {with_cjk}/10")

print("\n=== 抽样：AI 热点前 3 条 ===")
print("\n".join(list(blocks.values())[0].splitlines()[:9]))
