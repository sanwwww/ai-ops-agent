"""
RAG 知识库检索模块：Agent 的"参考书"。

作用：把本地运维知识文档（knowledge/*.md）切块后存入 ChromaDB 向量库，
Agent 排查问题时可以调用 search_knowledge 工具按语义检索相关经验片段，
把"凭 LLM 记忆回答"升级为"结合真实知识库回答"（这就是 RAG：
Retrieval-Augmented Generation，检索增强生成）。

设计要点（面试可讲）：
1. 向量库用 ChromaDB（PersistentClient，数据落盘 backend/chroma_db）
2. 文档按 "## " 标题切块，每个 chunk 带 source/title 元数据，检索结果可溯源
3. 混合检索（Hybrid Search）：默认 MiniLM embedding 是英文模型，中文语义
   匹配偏弱，所以在向量召回 top6 的基础上叠加关键词重排（query 中的
   错误码/协议名/中文 2-gram 在文档中的命中数加权），纯向量 RAG 对中文
   运维语料的召回质量明显提升
4. 检索失败不影响主流程：知识库挂了只返回提示语，Agent 继续用工具排查
   （降级策略：RAG 是增强，不是强依赖）
"""
import os
import re
import glob

CHROMA_DIR = os.path.join(os.path.dirname(__file__), "chroma_db")
COLLECTION_NAME = "ops_kb"
TOP_K = 3  # 每次检索返回的片段数

_client = None
_collection = None


def _get_collection():
    """惰性初始化：首次调用才建连接，避免 import 时就加载模型拖慢启动。"""
    global _client, _collection
    if _collection is not None:
        return _collection
    import chromadb  # 延迟导入：知识库初始化失败不阻塞服务启动

    _client = chromadb.PersistentClient(path=CHROMA_DIR)
    _collection = _client.get_or_create_collection(COLLECTION_NAME)
    return _collection


def _extract_keywords(query: str) -> list:
    """从查询里抽取关键词：英文/数字/错误码 token + 中文 2-gram。

    例："网站报 502 错误怎么排查" -> ["502", "网站", "站报", "错误", "怎么", "排查"]
    中文没有天然分词，2-gram 是无依赖下性价比最高的近似方案。
    """
    tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9]+", query) if len(t) >= 2]
    chinese = re.sub(r"[^\u4e00-\u9fff]", "", query)
    tokens += [chinese[i:i + 2] for i in range(len(chinese) - 1)]
    return tokens


def _rerank(query: str, docs: list, distances: list) -> list:
    """关键词重排：score = 关键词命中数 * 2 + 向量相似度(1 - distance)。

    向量召回负责"语义沾边"，关键词负责"精确命中"——两者互补。
    """
    kws = _extract_keywords(query)
    scored = []
    for doc, dist in zip(docs, distances):
        doc_l = doc.lower()
        kw_hits = sum(doc_l.count(k) for k in kws)
        score = kw_hits * 2 + (1 - dist)
        scored.append((score, doc))
    scored.sort(key=lambda x: -x[0])
    return [doc for _, doc in scored]


def _build_if_empty() -> bool:
    """兜底自愈：知识库为空时自动执行入库（部署时忘了跑 build_kb.py 也能用）。

    正常路径是 Render Build 命令里执行 build_kb.py（构建期完成，不占启动时间），
    这里只是保险丝：只会在库为空时触发一次，建好后续请求直接走检索。
    """
    try:
        import build_kb
        build_kb.main()
        return _get_collection().count() > 0
    except Exception as e:
        print(f"[knowledge_base] 自动构建失败: {e}")  # 构建失败走降级，不阻塞服务
        return False


def search(query: str, top_k: int = TOP_K) -> str:
    """按语义+关键词混合检索知识库，返回拼接好的参考片段（直接喂给 LLM）。

    返回格式为纯文本，因为它是作为 tool result 写回 LLM 的，
    长度受控（截断逻辑与 tools.py 一致）。
    """
    try:
        col = _get_collection()
        if col.count() == 0:
            _build_if_empty()  # 保险丝：库为空时自动重建
        if col.count() == 0:
            return "知识库为空，请先运行 python build_kb.py 构建知识库。"
        n = min(6, col.count())  # 过召回 6 条，重排后取前 top_k
        res = col.query(query_texts=[query], n_results=n)
        docs = _rerank(query, res["documents"][0], res["distances"][0])[:top_k]
        metas = {id: m for id, m in zip(res["ids"][0], res["metadatas"][0])}
        # 重排后顺序变了，metadata 要按文档内容对回源头
        id_by_doc = {d: i for i, d in zip(res["ids"][0], res["documents"][0])}
        parts = []
        for doc in docs:
            meta = metas.get(id_by_doc.get(doc, ""), {})
            parts.append(f"【{meta.get('source', 'unknown')} · {meta.get('title', '')}】\n{doc}")
        return "\n\n".join(parts)[:1200]  # 截断，防止撑爆上下文
    except Exception as e:
        # 降级：检索挂了不能影响排查主流程
        return f"知识库检索暂时不可用({e})，请基于已有工具检测结果分析。"


def count() -> int:
    """知识库条目数（给 /health 和前端状态栏用）。"""
    try:
        return _get_collection().count()
    except Exception:
        return -1  # -1 表示知识库未初始化或不可用
