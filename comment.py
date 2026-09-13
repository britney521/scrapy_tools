"""快手评论接口 /rest/v/photo/comment/list 的纯 HTTP 请求器（无签名、无浏览器依赖）。

和搜索接口不同，这个接口不在 sig4 的白名单里（白名单只有 profile / search / feed / collect 那几条），
所以不需要 __NS_hxfalcon，也不需要 kww 头，带上 did 直接 POST 就能拿到 result=1。

返回体用的是 V2 字段：rootCommentsV2 / pcursorV2 / commentCountV2。
翻页把上一轮返回的 pcursorV2 塞回 body 的 pcursor。

直接跑:
    python comment.py
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import requests

# --------------------------------------------------------------------------- 写死的配置
PHOTO_ID = "3xvp4t5n3vzhn7i"
PCURSOR = "1130590190593"
MAX_PAGES = 3
DID = "web_5299e766327f5b3fc91ba3e4b9641d54"

COMMENT_PATH = "/rest/v/photo/comment/list"
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

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Content-Type": "application/json",
    "Origin": HOST,
    "Referer": REFERER,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": UA,
}

PAGE_INTERVAL = 2.0
RETRY_TIMES = 3
RETRY_INTERVAL = 15.0


# --------------------------------------------------------------------------- 请求
def fetch_page(session: requests.Session, photo_id: str, pcursor: str) -> dict[str, Any]:
    body = {"photoId": photo_id, "pcursor": pcursor}
    url = f"{HOST}{COMMENT_PATH}"

    # 偶发返回非 result=1（限流），退避重试即可恢复
    for attempt in range(RETRY_TIMES):
        resp = session.post(url, headers=HEADERS, cookies=COOKIES, json=body, timeout=30)
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

    raise RuntimeError(f"接口持续返回 result={data.get('result')} error_msg={data.get('error_msg')}")


def compact_comments(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for c in data.get("rootCommentsV2", []):
        rows.append(
            {
                "comment_id": c.get("comment_id"),
                "author_name": c.get("author_name"),
                "author_id": c.get("author_id"),
                "content": c.get("content"),
                "like_count": c.get("likeCount"),
                "sub_count": c.get("commentCount"),
                "has_sub": c.get("hasSubComments"),
                "timestamp": c.get("timestamp"),
                "reply_to": c.get("reply_to"),
                "reply_to_name": c.get("replyToUserName"),
                "head_url": c.get("headurl"),
            }
        )
    return rows


def main() -> None:
    session = requests.Session()
    session.trust_env = False

    rows: list[dict[str, Any]] = []
    pcursor = PCURSOR
    total = None

    for page_no in range(MAX_PAGES):
        data = fetch_page(session, PHOTO_ID, pcursor)
        rows.extend(compact_comments(data))
        total = data.get("commentCountV2", total)
        pcursor = data.get("pcursorV2", "")
        print(
            f"第 {page_no + 1} 页: 评论={len(data.get('rootCommentsV2', []))} 下一页 pcursorV2={pcursor!r}",
            file=sys.stderr,
        )
        if not pcursor:
            break
        time.sleep(PAGE_INTERVAL)

    print(
        json.dumps(
            {"photo_id": PHOTO_ID, "total": total, "count": len(rows), "comments": rows},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, RuntimeError) as exc:
        print(f"失败: {exc}", file=sys.stderr)
        raise SystemExit(1)
