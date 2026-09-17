"""
RAG 知识库检索模块：Agent 的"参考书"。

作用：把本地运维知识文档（knowledge/*.md）存入检索层，Agent 排查问题时
调用 search_knowledge 工具检索相关经验片段，把"凭 LLM 记忆回答"升级为
"结合真实知识库回答"（RAG：Retrieval-Augmented Generation，检索增强生成）。

三级检索策略（面试重点：这是真实的资源约束权衡）：
1. API 向量模式：配置 SILICONFLOW_API_KEY 时，用 bge-m3 embedding API
   向量化（中文效果好，内存占用≈0），适合生产/公网免费实例
2. 本地向量模式：未配 Key 时用 ChromaDB 内置 MiniLM(ONNX) 本地模型，
   零配置，但模型占内存（~200MB），小内存实例可能 OOM
3. 关键词降级模式：向量初始化失败（如免费实例 OOM）时自动降级为
   纯关键词打分检索——质量略降但服务永远可用（可用性 > 完美检索）

设计要点：
- 文档按 "## " 标题切块，chunk 带 source/title 元数据，检索结果可溯源
- 混合检索：向量召回 top6 后叠加关键词重排（MiniLM 英文模型中文召回弱，
  关键词命中数加权补足）；关键词模式则纯用关键词打分
- 检索失败不影响主流程：知识库挂了只返回提示语，Agent 继续用工具排查
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
CHROMA_DIR = os.path.join(HERE, "chroma_db")
COLLECTION_NAME = "ops_kb"
TOP_K = 3  # 每次检索返回的片段数

# 可选：API 向量模式（硅基流动 bge-m3，中文效果好且不占本机内存）
SILICONFLOW_KEY = os.getenv("SILICONFLOW_API_KEY", "")
SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"
EMBED_MODEL = "BAAI/bge-m3"

_mode = None      # "api_vector" / "local_vector" / "keyword"，惰性决定
_collection = None
_kw_chunks = None  # 关键词模式的内存语料 [(text, meta), ...]


# ---------- 通用：语料加载与关键词打分 ----------

def _load_chunks():
    """读取 knowledge/*.md 按 '## ' 标题切块，返回 [(text, metadata), ...]。

    切块逻辑（面试可讲）：一个二级标题下的内容就是一个完整排查思路，
    语义不会切到两个 chunk；标题行保留在 chunk 开头（标题含关键语义）。
    """
    import glob
    chunks = []
    for path in sorted(glob.glob(os.path.join(HERE, "knowledge", "*.md"))):
        source = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        sections = content.split("\n## ")
        for i, sec in enumerate(sections):
            sec = sec.strip()
            if not sec:
                continue
            if i == 0 and not sec.startswith("##"):
                continue  # 跳过文件开头的 # 总标题（无检索价值）
            if not sec.startswith("##"):
                sec = "## " + sec  # 恢复被 split 吃掉的标题前缀
            title = sec.split("\n", 1)[0].lstrip("# ").strip()
            chunks.append((sec, {"source": source, "title": title}))
    return chunks


def _extract_keywords(query: str) -> list:
    """从查询里抽取关键词：英文/数字/错误码 token + 中文 2-gram。

    例："网站报 502 错误怎么排查" -> ["502", "网站", "站报", "错误", ...]
    中文没有天然分词，2-gram 是无依赖下性价比最高的近似方案。
    """
    tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9]+", query) if len(t) >= 2]
    chinese = re.sub(r"[^\u4e00-\u9fff]", "", query)
    tokens += [chinese[i:i + 2] for i in range(len(chinese) - 1)]
    return tokens


def _keyword_top(query: str, top_k: int) -> list:
    """纯关键词打分：命中数 × 2 排序（降级模式用，不依赖任何模型）。"""
    global _kw_chunks
    if _kw_chunks is None:
        _kw_chunks = _load_chunks()
    kws = _extract_keywords(query)
    scored = []
    for text, meta in _kw_chunks:
        t = text.lower()
        score = sum(t.count(k) for k in kws) * 2
        scored.append((score, text, meta))
    scored.sort(key=lambda x: -x[0])
    return [(t, m) for s, t, m in scored[:top_k]]


def _format_parts(pairs) -> str:
    """把 [(text, meta), ...] 拼成喂给 LLM 的参考片段文本。"""
    parts = [f"【{m.get('source', 'unknown')} · {m.get('title', '')}】\n{t}"
             for t, m in pairs]
    return "\n\n".join(parts)[:1200]  # 截断，防止撑爆上下文


# ---------- 向量模式：ChromaDB + 可插拔 embedding ----------

def _embed_api(texts: list) -> list:
    """硅基流动 bge-m3 embedding（OpenAI 兼容协议）。"""
    from openai import OpenAI
    client = OpenAI(api_key=SILICONFLOW_KEY, base_url=SILICONFLOW_BASE)
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def _get_collection():
    """惰性初始化 ChromaDB 连接。"""
    global _collection
    if _collection is not None:
        return _collection
    import chromadb

    client = chromadb.PersistentClient(path=CHROMA_DIR)
    _collection = client.get_or_create_collection(COLLECTION_NAME)
    return _collection


def _build_if_empty() -> bool:
    """保险丝：向量库为空时自动入库。API Key 在则走 API 模型，否则本地模型。

    正常路径是部署时执行 python build_kb.py（构建期完成），
    这里只兜底，且只在库为空时触发一次。
    """
    try:
        col = _get_collection()
        if col.count() > 0:
            return True
        chunks = _load_chunks()
        if SILICONFLOW_KEY:
            # API 模式：自己算好 embedding 再写入（不依赖 chroma 内置模型）
            embs = _embed_api([c[0] for c in chunks])
            col.add(ids=[f"chunk_{i}" for i in range(len(chunks))],
                    embeddings=embs,
                    documents=[c[0] for c in chunks],
                    metadatas=[c[1] for c in chunks])
        else:
            col.add(ids=[f"chunk_{i}" for i in range(len(chunks))],
                    documents=[c[0] for c in chunks],
                    metadatas=[c[1] for c in chunks])  # 触发 chroma 内置 MiniLM
        return col.count() > 0
    except Exception as e:
        print(f"[knowledge_base] 向量构建失败: {e}")  # 失败则由调用方降级
        return False


def _search_vector(query: str, top_k: int) -> list:
    """向量混合检索：召回 top6 -> 关键词重排 -> top_k。"""
    col = _get_collection()
    if col.count() == 0 and not _build_if_empty():
        raise RuntimeError("知识库为空且自动构建失败")
    n = min(6, col.count())
    if SILICONFLOW_KEY:
        qvec = _embed_api([query])[0]
        res = col.query(query_embeddings=[qvec], n_results=n)
    else:
        res = col.query(query_texts=[query], n_results=n)  # chroma 内置模型

    docs, dists = res["documents"][0], res["distances"][0]
    id_by_doc = {d: i for i, d in zip(res["ids"][0], docs)}
    metas = {i: m for i, m in zip(res["ids"][0], res["metadatas"][0])}

    kws = _extract_keywords(query)
    scored = []
    for doc, dist in zip(docs, dists):
        doc_l = doc.lower()
        score = sum(doc_l.count(k) for k in kws) * 2 + (1 - dist)
        scored.append((score, doc, metas.get(id_by_doc.get(doc, ""), {})))
    scored.sort(key=lambda x: -x[0])
    return [(d, m) for s, d, m in scored[:top_k]]


# ---------- 对外接口 ----------

def search(query: str, top_k: int = TOP_K) -> str:
    """混合检索知识库，返回拼接好的参考片段（作为 tool result 直接喂给 LLM）。"""
    global _mode
    try:
        # 模式决策：配了 API Key 走 API 向量；否则试本地向量，失败永久降级关键词
        if _mode is None:
            _mode = "api_vector" if SILICONFLOW_KEY else "local_vector"
        if _mode == "keyword":
            return _format_parts(_keyword_top(query, top_k))
        try:
            return _format_parts(_search_vector(query, top_k))
        except Exception as e:
            print(f"[knowledge_base] 向量检索不可用({_mode}: {e})，降级为关键词模式")
            _mode = "keyword"
            return _format_parts(_keyword_top(query, top_k))
    except Exception as e:
        # 最终降级：检索挂了不能影响排查主流程
        return f"知识库检索暂时不可用({e})，请基于已有工具检测结果分析。"


def count() -> int:
    """知识库条目数（给 /health 用）。-1 表示不可用；关键词模式返回语料条数。"""
    try:
        if _mode == "keyword":
            return len(_kw_chunks) if _kw_chunks is not None else len(_load_chunks())
        return _get_collection().count()
    except Exception:
        return -1  # -1 表示知识库未初始化或不可用
