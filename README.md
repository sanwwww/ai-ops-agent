# -*- coding: utf-8 -*-
# AI Ops Agent - 最小可闭环版本（面试演示用）

## 这是什么

一个最小闭环的 AI 智能运维助手：
用户用自然语言描述故障 → Agent 自动调用网络检测工具（DNS/ping/端口/HTTP）→ 基于真实检测结果给出排查结论。

总共 4 个核心文件，每个都能在面试里讲清楚：

```
ai-ops-agent/
└── backend/
    ├── main.py          # FastAPI 入口：接收 HTTP 请求，返回 JSON（后端门面）
    ├── agent.py         # Agent 循环：LLM思考→调工具→结果喂回→再思考（项目灵魂）
    ├── tools.py         # 4 个网络检测工具：dns_lookup / ping_host / check_port / http_check
    └── static/index.html # 聊天页面（单文件前端，无需 npm）
```

## 怎么跑起来（3 步）

```bash
# 1. 进入目录，装依赖（建议先建虚拟环境）
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 2. 配置 API Key
# 复制 .env.example 为 .env，填入你的 DeepSeek Key（platform.deepseek.com 申请）

# 3. 启动
python main.py
# 浏览器打开 http://127.0.0.1:8000
```

## 试着问它

- "网站打不开了，帮我排查 example.com"
- "baidu.com 的 443 端口通吗"
- "https://example.com 一直 502，是什么问题"

## 数据流（面试必会画）

```
用户输入
  ↓
index.html  --POST /api/chat-->  main.py (FastAPI)
  ↓
agent.py：LLM 思考 → 决定调哪个工具 → tools.py 执行真实检测
  ↓
检测结果喂回 LLM → 继续分析（可能再调工具）→ 最终结论
  ↓
JSON {"answer", "steps"} 返回前端展示
```
