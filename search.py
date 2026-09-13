"""快手搜索接口 /rest/v/search/feed 的纯算请求器（无任何浏览器依赖）。

链路:
    node sign.js  '<payload>'  ->  Jose(sig4 VM) 纯计算产出 __NS_hxfalcon + caver
    POST https://www.kuaishou.com/rest/v/search/feed?__NS_hxfalcon=...&caver=2

签名不依赖 cookie：Jose 内部的 cookie 白名单 w=[] 为空，只需要 path / query / body。
kww 请求头（= cookie kwfv1）与 kwscode / kwssectoken 经实测均非必需。
唯一必需的是 did，写死在下面的常量里。

直接跑:
    python search.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

# --------------------------------------------------------------------------- 写死的配置
KEYWORD = "乌拉"
MAX_PAGES = 3
DID = "web_52593783687a0ccef3635c148f315eed"

NODE_BIN = "/Users/britlee/.workbuddy/binaries/node/versions/22.22.2-3/bin/node"
BASE_DIR = Path(__file__).resolve().parent
SIGN_JS = BASE_DIR / "sign.js"

SEARCH_PATH = "/rest/v/search/feed"
HOST = "https://www.kuaishou.com"
REFERER = "https://www.kuaishou.com/search/%E4%B9%8C%E6%8B%89?source=NewReco"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

COOKIES = {
    "did": DID,
    "kpf": "PC_WEB",
    "kpn": "KUAISHOU_VISION",
    "clientid": "3",
}

PAGE_INTERVAL = 2.0
RETRY_TIMES = 3
RETRY_INTERVAL = 15.0


# --------------------------------------------------------------------------- 签名
def sign(url_path: str, query: dict, form: dict, request_body: dict) -> dict:
    """调用 Node 侧的 Jose sig4 VM，纯计算产出 __NS_hxfalcon。"""
    payload = {
        "cookie": "",
        "url": url_path,
        "query": query,
        "form": form,
        "requestBody": request_body,
    }
    proc = subprocess.run(
        [NODE_BIN, str(SIGN_JS), json.dumps(payload, ensure_ascii=False)],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"签名失败:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------- 请求
def fetch_page(
    session: requests.Session,
    keyword: str,
    pcursor: str,
    search_session_id: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "keyword": keyword,
        "page": "search",
        "webPageArea": "",
        "pcursor": pcursor,
    }
    if search_session_id:
        body["searchSessionId"] = search_session_id

    sig = sign(SEARCH_PATH, {}, {}, body)
    url = f"{HOST}{SEARCH_PATH}?__NS_hxfalcon={sig['sign']}&caver={sig['caver']}"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": HOST,
        "Referer": REFERER,
        "User-Agent": UA,
    }

    # result=2 是临时风控限流（同期浏览器请求正常），等一会儿重试即可恢复
    for attempt in range(RETRY_TIMES):
        resp = session.post(url, headers=headers, cookies=COOKIES, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") == 1:
            return data
        if attempt < RETRY_TIMES - 1:
            print(
                f"[warn] result={data.get('result')} 疑似限流，{RETRY_INTERVAL:.0f}s 后重试",
                file=sys.stderr,
            )
            time.sleep(RETRY_INTERVAL)

    raise RuntimeError(
        f"接口持续返回 result={data.get('result')} error_msg={data.get('error_msg')} "
        f"request_id={data.get('request_id')}"
    )


def compact_feed(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in data.get("feeds", []):
        photo = item.get("photo", {})
        author = item.get("author", {})
        photo_urls = photo.get("photoUrls") or []
        rows.append(
            {
                "id": photo.get("id"),
                "caption": photo.get("caption"),
                "author": author.get("name"),
                "author_id": author.get("id"),
                "like_count": photo.get("likeCount"),
                "view_count": photo.get("viewCount"),
                "duration_ms": photo.get("duration"),
                "cover_url": photo.get("coverUrl"),
                "video_url": photo_urls[0].get("url") if photo_urls else None,
            }
        )
    return rows


def main() -> None:
    session = requests.Session()
    session.trust_env = False

    rows: list[dict[str, Any]] = []
    pcursor = ""
    session_id = ""

    for page_no in range(MAX_PAGES):
        data = fetch_page(session, KEYWORD, pcursor, session_id)
        rows.extend(compact_feed(data))
        session_id = data.get("searchSessionId", session_id)
        pcursor = data.get("pcursor", "")
        print(
            f"第 {page_no + 1} 页: feeds={len(data.get('feeds', []))} 下一页 pcursor={pcursor!r}",
            file=sys.stderr,
        )
        if not pcursor:
            break
        time.sleep(PAGE_INTERVAL)

    print(json.dumps({"keyword": KEYWORD, "count": len(rows), "feeds": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, RuntimeError) as exc:
        print(f"失败: {exc}", file=sys.stderr)
        raise SystemExit(1)
