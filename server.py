import html as html_lib
import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote, unquote, urljoin
from urllib.request import Request, urlopen
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from openai import OpenAI

# 1. 填入你的 DeepSeek API Key 替换成自己的
DEEPSEEK_API_KEY = "sk-523afdb1d891492bad2*******830d89b2"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# 简单内存缓存：(query) -> (timestamp, result_str)
_SEARCH_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 300.0


class ChatRequest(BaseModel):
    messages: list
    model: str = "deepseek-chat"
    temperature: float = 0.7
    enable_search: bool = True


HEADERS_PC = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

HEADERS_WAP = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _clean(raw: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _http_get(url: str, mobile: bool = False, timeout: int = 5) -> str:
    headers = HEADERS_WAP if mobile else HEADERS_PC
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _search_bing_rss(query: str, limit: int = 5) -> list[dict]:
    """必应 RSS 源：结构固定，通常比抓网页干净"""
    url = f"https://www.bing.com/search?q={quote(query)}&format=rss"
    xml_text = _http_get(url, mobile=False, timeout=6)
    root = ET.fromstring(xml_text)
    out = []
    for item in root.findall(".//item"):
        title = _clean(item.findtext("title", ""))
        link = _clean(item.findtext("link", ""))
        desc = _clean(item.findtext("description", ""))
        if title and link:
            out.append({"source": "必应RSS", "title": title, "snippet": desc[:220], "href": link})
        if len(out) >= limit:
            break
    return out


def _search_sogou_wap(query: str, limit: int = 5) -> list[dict]:
    """搜狗手机端轻量页"""
    url = f"https://wap.sogou.com/web/sl?keyword={quote(query)}"
    body = _http_get(url, mobile=True, timeout=6)
    if "验证码" in body[:3000] or "antispider" in body[:3000].lower():
        return []
    out = []
    seen = set()
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a >', body, flags=re.S | re.I):
        href = html_lib.unescape(m.group(1).strip())
        if href.startswith("/"):
            href = "https://wap.sogou.com" + href
        title = _clean(m.group(2))
        if len(title) < 6 or not href.startswith("http") or href in seen:
            continue
        snippet = _clean(body[m.end(): m.end() + 500])
        if snippet.startswith(title):
            snippet = snippet[len(title):].strip(" -|·:：")
        seen.add(href)
        out.append({"source": "搜狗WAP", "title": title[:100], "snippet": snippet[:200], "href": href})
        if len(out) >= limit:
            break
    return out


def _search_so360_wap(query: str, limit: int = 5) -> list[dict]:
    """360 手机端轻量页"""
    url = f"https://m.so.com/s?q={quote(query)}"
    body = _http_get(url, mobile=True, timeout=6)
    if "验证码" in body[:3000]:
        return []
    out = []
    seen = set()
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a >', body, flags=re.S | re.I):
        href = html_lib.unescape(m.group(1).strip())
        title = _clean(m.group(2))
        if len(title) < 6 or not href.startswith("http") or href in seen:
            continue
        snippet = _clean(body[m.end(): m.end() + 500])
        if snippet.startswith(title):
            snippet = snippet[len(title):].strip(" -|·:：")
        seen.add(href)
        out.append({"source": "360WAP", "title": title[:100], "snippet": snippet[:200], "href": href})
        if len(out) >= limit:
            break
    return out


def web_search(query: str, max_total: int = 8) -> str:
    q = query.strip()
    if not q:
        return ""

    now = time.time()
    cached = _SEARCH_CACHE.get(q)
    if cached and (now - cached[0]) < _CACHE_TTL:
        print(f"[命中缓存] 关键词：{q}")
        return cached[1]

    print(f"[轻量直连检索] 关键词：{q}")
    sources = {
        "必应RSS": _search_bing_rss,
        "搜狗WAP": _search_sogou_wap,
        "360WAP": _search_so360_wap,
    }

    all_items = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(fn, q, 4): name for name, fn in sources.items()}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                res = fut.result()
                print(f"  -> [{name}] {len(res)} 条")
                all_items.extend(res)
            except Exception as e:
                print(f"  -> [{name}] 异常：{e}")

    seen = set()
    unique = []
    for item in all_items:
        key = re.sub(r"\W+", "", item["title"])[:18]
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
        if len(unique) >= max_total:
            break

    if not unique:
        print("[轻量检索结果] 0 条")
        return "未检索到相关网页结果。"

    lines = [
        f"{i}. [{it['source']}] {it['title']}\n   摘要：{it['snippet'] or '（无摘要）'}\n   链接：{it['href']}"
        for i, it in enumerate(unique, 1)
    ]
    result_str = "\n".join(lines)
    _SEARCH_CACHE[q] = (now, result_str)
    return result_str


@app.post("/chat")
async def chat_stream(req: ChatRequest):
    now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
    user_query = req.messages[-1]["content"] if req.messages else ""

    search_context = ""
    if req.enable_search and user_query.strip():
        search_context = web_search(user_query)

    system_prompt = (
        f"你是一个严谨、务实、擅长清晰拆解问题的 AI 助手。\n"
        f"当前服务器真实时间是：{now_str}。\n"
    )
    if search_context:
        system_prompt += (
            f"\n以下是针对用户当前问题的实时联网检索结果，请结合这些信息进行准确、有条理的回答，并在适当位置注明来源和链接；若结果中缺少某项具体细节，请如实说明：\n"
            f"【联网搜索结果】\n{search_context}\n"
        )

    final_messages = [{"role": "system", "content": system_prompt}] + [
        m for m in req.messages if m.get("role") != "system"
    ]

    def event_generator():
        try:
            stream = client.chat.completions.create(
                model=req.model,
                messages=final_messages,
                temperature=req.temperature,
                stream=True,
            )
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta:
                    content = chunk.choices[0].delta.content
                    if content:
                        payload = json.dumps({"content": content}, ensure_ascii=False)
                        yield f"data: {payload}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            print(f"[模型请求报错] {e}")
            err_payload = json.dumps({"error": str(e)}, ensure_ascii=False)
            yield f"data: {err_payload}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    import socket
    import uvicorn

    # 检测 8000 端口是否已经被占用，防止重复静默启动多个后端实例
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", 8000)) == 0:
            raise SystemExit(0)

    uvicorn.run(app, host="127.0.0.1", port=8000)