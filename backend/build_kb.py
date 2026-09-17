"""
RAG 知识库入库脚本：把 knowledge/*.md 切块并写入 ChromaDB。

切块策略（面试可讲）：
- 按 "## " 二级标题切块：一个标题下的内容就是一个完整排查思路，
  语义完整、不会把一个知识点切到两个 chunk 里
- # 一级标题作为文档来源名，## 标题作为 chunk 标题，都存进 metadata
- 检索时带上 source + title，Agent（和面试官）能看到知识出处

使用：在 backend 目录执行  python build_kb.py
部署：Render 的 Build 命令改为
  pip install -r requirements.txt && python build_kb.py
"""
import os
import glob

import chromadb

HERE = os.path.dirname(os.path.abspath(__file__))
KB_DIR = os.path.join(HERE, "knowledge")
CHROMA_DIR = os.path.join(HERE, "chroma_db")
COLLECTION_NAME = "ops_kb"


def load_chunks():
    """读取所有知识文档，按 '## ' 标题切块。返回 [(text, metadata), ...]"""
    chunks = []
    for path in sorted(glob.glob(os.path.join(KB_DIR, "*.md"))):
        source = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # 按 "## " 切块，保留标题行作为 chunk 开头（标题本身含关键语义，利于检索）
        sections = content.split("\n## ")
        for i, sec in enumerate(sections):
            sec = sec.strip()
            if not sec:
                continue
            if i == 0 and not sec.startswith("##"):
                continue  # 跳过文件开头的 # 总标题（无检索价值）
            # 恢复被 split 吃掉的标题前缀
            if not sec.startswith("##"):
                sec = "## " + sec
            lines = sec.split("\n", 1)
            title = lines[0].lstrip("# ").strip()
            chunks.append((sec, {"source": source, "title": title}))
    return chunks


def main():
    chunks = load_chunks()
    print(f"从 knowledge/ 切出 {len(chunks)} 个知识片段")

    client = chromadb.PersistentClient(path=CHROMA_DIR)
    # 重建式入库：保证库内容和文档文件完全一致（幂等）
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    col = client.get_or_create_collection(COLLECTION_NAME)
    col.add(
        ids=[f"chunk_{i}" for i in range(len(chunks))],
        documents=[c[0] for c in chunks],
        metadatas=[c[1] for c in chunks],
    )
    print(f"入库完成：{col.count()} 条 → {CHROMA_DIR}")


if __name__ == "__main__":
    main()
