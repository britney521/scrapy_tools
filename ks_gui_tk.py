"""快手关键词采集工具（GUI 版）

功能:
    1. 关键词搜索采集  ->  POST /rest/v/search/feed   (sig4 签名, node sign.js 纯算)
    2. 视频评论采集    ->  POST /rest/v/photo/comment/list (无签名)
    3. 浏览器统一 Chrome, 自动化统一 DrissionPage, 仅用于打开页面登录 + 读取 cookie
    4. cookie 自动保存到本地 ks_config/cookies.json, 下次可直接复用登录态
    5. Windows / macOS 通用 (tkinter + DrissionPage 自动定位 Chrome)

运行:
    python ks_gui.py
依赖:
    pip install DrissionPage requests   +  本机需装有 Node.js(搜索接口签名用)
"""

from __future__ import annotations

import csv
import json
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

import requests

# --------------------------------------------------------------------------- 写死的配置
HOST = "https://www.kuaishou.com"
SEARCH_PATH = "/rest/v/search/feed"
COMMENT_PATH = "/rest/v/photo/comment/list"
REFERER = "https://www.kuaishou.com/search/%E4%B9%8C%E6%8B%89?source=NewReco"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

DEFAULT_PORT = 9222
DEFAULT_KEYWORD = "乌拉"
DEFAULT_MAX_FEEDS = 20
DEFAULT_MAX_COMMENTS = 50
DEFAULT_SAVE_DIR = str(Path.home() / "Desktop")

PAGE_INTERVAL = 2.0          # 翻页间隔(秒)
RETRY_TIMES = 3              # 限流退避重试次数
RETRY_INTERVAL = 15.0        # 重试间隔(秒)
COMMENT_PAGE_INTERVAL = 1.5  # 评论翻页间隔(秒)

BASE_DIR = Path(__file__).resolve().parent
SIGN_JS = BASE_DIR / "sign.js"
CONFIG_DIR = BASE_DIR / "ks_config"
COOKIE_FILE = CONFIG_DIR / "cookies.json"

# macOS 上找不到 node 命令时的兜底路径
FALLBACK_NODE = "/usr/local/bin/node"

RED = "#FF2442"
BG = "#F7F5F2"
CARD = "#FFFFFF"
LOG_GREEN = "#2E9E44"

# Windows / macOS 字体自适应
_IS_WIN = __import__("sys").platform == "win32"
F_TITLE = ("Microsoft YaHei" if _IS_WIN else "PingFang SC", 20, "bold")
F_SUB = ("Microsoft YaHei" if _IS_WIN else "PingFang SC", 11)
F_BODY = ("Microsoft YaHei" if _IS_WIN else "PingFang SC", 12)
F_BTN = ("Microsoft YaHei" if _IS_WIN else "PingFang SC", 11)
F_BIG = ("Microsoft YaHei" if _IS_WIN else "PingFang SC", 14, "bold")
F_SMALL = ("Microsoft YaHei" if _IS_WIN else "PingFang SC", 10)
F_LOG = ("Consolas" if _IS_WIN else "Menlo", 11)


# --------------------------------------------------------------------------- node 查找
def find_node() -> str:
    p = shutil.which("node")
    if p:
        return p
    if Path(FALLBACK_NODE).exists():
        return FALLBACK_NODE
    raise RuntimeError("未找到 Node.js，请安装后重试（搜索接口签名需要）")


# --------------------------------------------------------------------------- 签名
def sign(node_bin: str, url_path: str, query: dict, form: dict, request_body: dict) -> dict:
    payload = {
        "cookie": "",
        "url": url_path,
        "query": query,
        "form": form,
        "requestBody": request_body,
    }
    proc = subprocess.run(
        [node_bin, str(SIGN_JS), json.dumps(payload, ensure_ascii=False)],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"签名失败:\n{proc.stderr[-1500:]}")
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------- cookie
def save_cookies(cookies: dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cookies() -> dict[str, str] | None:
    if COOKIE_FILE.exists():
        try:
            return json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------- DrissionPage 浏览器
_browser = None  # type: Any


def detect_chrome() -> str:
    """自动探测本机 Chrome 可执行文件（优先标准名, 再退到任意含 chrome/chromium 的 App）。"""
    env_path = __import__("os").environ.get("CHROME_PATH", "")
    if env_path and Path(env_path).exists():
        return env_path

    cands: list[str] = []
    if _IS_WIN:
        cands += [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        local = __import__("os").environ.get("LOCALAPPDATA", "")
        if local:
            cands.append(str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    else:
        cands += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    for c in cands:
        if c and Path(c).exists():
            return c

    # 最后兜底: 扫 /Applications 下名字含 chrome 的 App(比如 GPT Chrome.app)
    apps = Path("/Applications") if Path("/Applications").exists() else None
    if apps:
        for app in sorted(apps.glob("*.app")):
            if "chrome" in app.name.lower() or "chromium" in app.name.lower():
                for exe in sorted((app / "Contents" / "MacOS").glob("*")):
                    if exe.is_file() and __import__("os").access(exe, __import__("os").X_OK):
                        return str(exe)
    return ""


def open_browser(port: int, browser_path: str = "") -> tuple[Any, dict[str, str]]:
    """打开(或接管)指定端口的 Chrome, 停在快手首页, 读取并保存 cookie。"""
    global _browser
    from DrissionPage import ChromiumPage, ChromiumOptions

    co = ChromiumOptions().set_local_port(port)
    path = browser_path.strip() or detect_chrome()
    if path:
        co.set_browser_path(path)
    _browser = ChromiumPage(co)
    _browser.get(f"{HOST}/")
    time.sleep(1.5)
    cookies = read_cookies_from_browser()
    return _browser, cookies


def read_cookies_from_browser() -> dict[str, str]:
    if _browser is None:
        raise RuntimeError("浏览器未打开")
    cks = _browser.cookies(all_domains=True)
    cookies = {c["name"]: c["value"] for c in cks if "kuaishou" in (c.get("domain") or "")}
    if not cookies.get("did"):
        raise RuntimeError("浏览器里没有拿到 did cookie，请确认页面已打开 kuaishou.com")
    save_cookies(cookies)
    return cookies


def close_browser() -> None:
    global _browser
    if _browser is not None:
        try:
            _browser.quit()
        except Exception:
            pass
        _browser = None


# --------------------------------------------------------------------------- 搜索采集
def fetch_search_page(session: requests.Session, node_bin: str, cookies: dict[str, str],
                      keyword: str, pcursor: str, search_session_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "keyword": keyword,
        "page": "search",
        "webPageArea": "",
        "pcursor": pcursor,
    }
    if search_session_id:
        body["searchSessionId"] = search_session_id

    sig = sign(node_bin, SEARCH_PATH, {}, {}, body)
    url = f"{HOST}{SEARCH_PATH}?__NS_hxfalcon={sig['sign']}&caver={sig['caver']}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": HOST,
        "Referer": REFERER,
        "User-Agent": UA,
    }

    data: dict[str, Any] = {}
    for attempt in range(RETRY_TIMES):
        resp = session.post(url, headers=headers, cookies=cookies, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") == 1:
            return data
        if attempt < RETRY_TIMES - 1:
            time.sleep(RETRY_INTERVAL)
    raise RuntimeError(
        f"搜索接口持续返回 result={data.get('result')} error_msg={data.get('error_msg')}"
    )


def compact_feed(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in data.get("feeds", []):
        photo = item.get("photo", {})
        author = item.get("author", {})
        photo_urls = photo.get("photoUrls") or []
        rows.append({
            "video_id": photo.get("id"),
            "caption": (photo.get("caption") or "").replace("\n", " "),
            "author": author.get("name"),
            "author_id": author.get("id"),
            "like_count": photo.get("likeCount"),
            "view_count": photo.get("viewCount"),
            "comment_count": photo.get("commentCount"),
            "duration_s": round((photo.get("duration") or 0) / 1000, 1),
            "cover_url": photo.get("coverUrl"),
            "video_url": photo_urls[0].get("url") if photo_urls else None,
            "share_url": f"https://www.kuaishou.com/short-video/{photo.get('id')}",
        })
    return rows


# --------------------------------------------------------------------------- 评论采集
def fetch_comment_page(session: requests.Session, cookies: dict[str, str],
                       photo_id: str, pcursor: str) -> dict[str, Any]:
    body = {"photoId": photo_id, "pcursor": pcursor}
    headers = {
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
    data: dict[str, Any] = {}
    for attempt in range(RETRY_TIMES):
        resp = session.post(f"{HOST}{COMMENT_PATH}", headers=headers,
                            cookies=cookies, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") == 1:
            return data
        if attempt < RETRY_TIMES - 1:
            time.sleep(RETRY_INTERVAL)
    raise RuntimeError(
        f"评论接口持续返回 result={data.get('result')} error_msg={data.get('error_msg')}"
    )


def compact_comments(data: dict[str, Any], photo_id: str) -> list[dict[str, Any]]:
    rows = []
    for c in data.get("rootCommentsV2", []):
        rows.append({
            "photo_id": photo_id,
            "comment_id": c.get("comment_id"),
            "author_name": c.get("author_name"),
            "author_id": c.get("author_id"),
            "content": (c.get("content") or "").replace("\n", " "),
            "like_count": c.get("likeCount"),
            "sub_count": c.get("commentCount"),
            "reply_to": c.get("reply_to"),
            "reply_to_name": c.get("replyToUserName"),
            "timestamp": c.get("timestamp"),
        })
    return rows


# --------------------------------------------------------------------------- CSV 导出
def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------- 采集主流程
def run_collect(log: Callable[[str], None], keyword: str, max_feeds: int, max_comments: int,
                save_dir: str, port: int, stop: threading.Event) -> str:
    """在子线程里执行, 通过 log() 汇报进度, stop 被 set 时尽快中断。返回结束原因。"""
    node_bin = find_node()
    log(f"Node: {node_bin}")

    def nap(seconds: float) -> bool:
        """可中断的 sleep, 返回 True 表示已被要求停止。"""
        return stop.wait(seconds)

    if stop.is_set():
        return "已停止"

    # cookie: 优先从已打开的浏览器现取, 否则用本地缓存
    if _browser is not None:
        try:
            cookies = read_cookies_from_browser()
            log("已从浏览器读取最新 cookie")
        except Exception as exc:
            cookies = load_cookies()
            log(f"浏览器读取 cookie 失败({exc}), 尝试本地缓存")
    else:
        cookies = load_cookies()
        log("浏览器未打开, 使用本地缓存 cookie")

    if not cookies or not cookies.get("did"):
        raise RuntimeError("没有可用 cookie, 请先点击「打开浏览器」")

    session = requests.Session()
    session.trust_env = False

    # ---------------- 搜索采集
    feeds: list[dict[str, Any]] = []
    pcursor, search_session_id = "", ""
    log(f"开始搜索采集: {keyword} (目标 {max_feeds} 条)")
    while len(feeds) < max_feeds:
        if stop.is_set():
            log("搜索采集在翻页前被中断")
            break
        data = fetch_search_page(session, node_bin, cookies, keyword, pcursor, search_session_id)
        rows = compact_feed(data)
        feeds.extend(rows)
        search_session_id = data.get("searchSessionId", search_session_id)
        pcursor = data.get("pcursor", "")
        log(f"  搜索页累计 {len(feeds)} 条 (本页 {len(rows)}, 下一页 pcursor={pcursor!r})")
        if not pcursor:
            log("  没有更多搜索结果了")
            break
        if nap(PAGE_INTERVAL):
            log("搜索采集在翻页间隔被中断")
            break
    feeds = feeds[:max_feeds]
    log(f"搜索采集完成, 共 {len(feeds)} 条")

    out_dir = Path(save_dir)
    ts = time.strftime("%Y%m%d_%H%M%S")
    feed_csv = out_dir / f"{keyword}_视频_{ts}.csv"
    write_csv(feed_csv, feeds)
    log(f"已保存: {feed_csv}")

    if stop.is_set():
        return "已停止（视频列表已保存）"

    # ---------------- 评论采集
    if max_comments > 0 and feeds:
        log(f"开始评论采集: 每视频最多 {max_comments} 条")
        all_comments: list[dict[str, Any]] = []
        for idx, feed in enumerate(feeds, 1):
            if stop.is_set():
                log("收到停止指令, 评论采集中断")
                break
            pid = feed["video_id"]
            if not pid:
                continue
            got, pc = 0, ""
            try:
                while got < max_comments:
                    if stop.is_set():
                        break
                    data = fetch_comment_page(session, cookies, pid, pc)
                    rows = compact_comments(data, pid)
                    all_comments.extend(rows)
                    got += len(rows)
                    pc = data.get("pcursorV2", "")
                    if not pc or not rows:
                        break
                    if nap(COMMENT_PAGE_INTERVAL):
                        break
                log(f"  [{idx}/{len(feeds)}] {pid} 评论 {got} 条")
            except Exception as exc:
                log(f"  [{idx}/{len(feeds)}] {pid} 评论失败: {exc}")
            if stop.is_set():
                break
            nap(PAGE_INTERVAL)

        comment_csv = out_dir / f"{keyword}_评论_{ts}.csv"
        write_csv(comment_csv, all_comments)
        log(f"评论采集完成, 共 {len(all_comments)} 条")
        log(f"已保存: {comment_csv}")

    return "已停止" if stop.is_set() else "全部完成 ✔"


# --------------------------------------------------------------------------- GUI
def _darken(hex_color: str, factor: float = 0.9) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#{:02X}{:02X}{:02X}".format(int(r * factor), int(g * factor), int(b * factor))


class FlatBtn(tk.Label):
    """Label 实现的扁平按钮 —— macOS 原生 tk.Button 会忽略 bg 颜色, Label 不会。"""

    def __init__(self, master, text: str, command=None, bg: str = "#F2F2F2",
                 fg: str = "#333333", font: tuple = F_BTN,
                 padx: int = 16, pady: int = 6) -> None:
        super().__init__(master, text=text, bg=bg, fg=fg, font=font,
                         padx=padx, pady=pady, cursor="hand2")
        self._normal_bg, self._hover_bg = bg, _darken(bg)
        self._fg_normal = fg
        self.command = command
        self.enabled = True
        self.bind("<Enter>", lambda e: self.configure(bg=self._hover_bg))
        self.bind("<Leave>", lambda e: self.configure(bg=self._normal_bg))
        self.bind("<Button-1>", self._click)

    def _click(self, _event) -> None:
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, on: bool, disabled_fg: str = "#CCCCCC") -> None:
        self.enabled = on
        self.configure(fg=self._fg_normal if on else disabled_fg,
                       cursor="hand2" if on else "arrow")


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.log_count = 0
        self.collecting = False
        self.stop_event = threading.Event()

        root.title("快手关键词采集工具")
        root.configure(bg=BG)
        root.geometry("820x680")
        root.minsize(760, 620)

        outer = tk.Frame(root, bg=BG)
        outer.pack(fill="both", expand=True, padx=24, pady=16)

        card = tk.Frame(outer, bg=CARD, highlightthickness=1,
                        highlightbackground="#E5E5E5")
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=36, pady=28)

        # 标题
        tk.Label(inner, text="快手关键词采集工具", font=F_TITLE,
                 fg=RED, bg=CARD, anchor="w").pack(fill="x")
        tk.Label(inner, text="打开浏览器登录后即可采集（端口+本地配置目录可复用登录态）",
                 font=F_SUB, fg="#999999", bg=CARD,
                 anchor="w").pack(fill="x", pady=(2, 14))

        form = tk.Frame(inner, bg=CARD)
        form.pack(fill="x")

        # 关键字
        tk.Label(form, text="关键字", font=F_BODY, fg="#333333",
                 bg=CARD, width=9, anchor="w").grid(row=0, column=0, sticky="w", pady=8)
        self.keyword_var = tk.StringVar(value=DEFAULT_KEYWORD)
        tk.Entry(form, textvariable=self.keyword_var, font=F_BODY,
                 relief="flat", highlightthickness=1, highlightcolor=RED,
                 highlightbackground="#DDDDDD").grid(row=0, column=1, columnspan=3,
                                                     sticky="we", pady=8, ipady=6)
        form.columnconfigure(1, weight=1)

        # 数量
        tk.Label(form, text="采集视频数", font=F_BODY, fg="#333333",
                 bg=CARD, width=9, anchor="w").grid(row=1, column=0, sticky="w", pady=8)
        self.max_feeds_var = tk.StringVar(value=str(DEFAULT_MAX_FEEDS))
        tk.Entry(form, textvariable=self.max_feeds_var, font=F_BODY,
                 relief="flat", highlightthickness=1, highlightcolor=RED,
                 highlightbackground="#DDDDDD", width=10).grid(row=1, column=1,
                                                               sticky="w", pady=8, ipady=6)
        tk.Label(form, text="每视频评论数", font=F_BODY, fg="#333333",
                 bg=CARD, anchor="e").grid(row=1, column=2, sticky="e", pady=8, padx=(18, 8))
        self.max_comments_var = tk.StringVar(value=str(DEFAULT_MAX_COMMENTS))
        tk.Entry(form, textvariable=self.max_comments_var, font=F_BODY,
                 relief="flat", highlightthickness=1, highlightcolor=RED,
                 highlightbackground="#DDDDDD", width=10).grid(row=1, column=3,
                                                               sticky="w", pady=8, ipady=6)

        # 浏览器
        tk.Label(form, text="浏览器", font=F_BODY, fg="#333333",
                 bg=CARD, width=9, anchor="w").grid(row=2, column=0, sticky="w", pady=8)
        brow = tk.Frame(form, bg=CARD)
        brow.grid(row=2, column=1, columnspan=3, sticky="we", pady=8)
        self.open_btn = FlatBtn(brow, text="打开浏览器", command=self.on_open_browser)
        self.open_btn.pack(side="left")
        tk.Label(brow, text="端口", font=F_SUB, fg="#333333",
                 bg=CARD).pack(side="left", padx=(14, 6))
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        tk.Entry(brow, textvariable=self.port_var, font=F_SUB, width=6,
                 relief="flat", highlightthickness=1, highlightcolor=RED,
                 highlightbackground="#DDDDDD").pack(side="left", ipady=4)
        self.browser_status = tk.Label(brow, text="浏览器未打开", font=F_SUB,
                                       fg="#999999", bg=CARD)
        self.browser_status.pack(side="right")

        # 浏览器路径
        tk.Label(form, text="浏览器路径", font=F_BODY, fg="#333333",
                 bg=CARD, width=9, anchor="w").grid(row=3, column=0, sticky="w", pady=8)
        self.browser_path_var = tk.StringVar(value=detect_chrome())
        tk.Entry(form, textvariable=self.browser_path_var, font=F_BODY,
                 relief="flat", highlightthickness=1, highlightcolor=RED,
                 highlightbackground="#DDDDDD").grid(row=3, column=1, columnspan=2,
                                                     sticky="we", pady=8, ipady=6)
        FlatBtn(form, text="选择", command=self.on_pick_browser).grid(row=3, column=3,
                                                                     sticky="e", padx=(10, 0))

        # 保存目录
        tk.Label(form, text="保存目录", font=F_BODY, fg="#333333",
                 bg=CARD, width=9, anchor="w").grid(row=4, column=0, sticky="w", pady=8)
        self.save_dir_var = tk.StringVar(value=DEFAULT_SAVE_DIR)
        tk.Entry(form, textvariable=self.save_dir_var, font=F_BODY,
                 relief="flat", highlightthickness=1, highlightcolor=RED,
                 highlightbackground="#DDDDDD").grid(row=4, column=1, columnspan=2,
                                                     sticky="we", pady=8, ipady=6)
        FlatBtn(form, text="选择", command=self.on_pick_dir).grid(row=4, column=3,
                                                                 sticky="e", padx=(10, 0))

        # 开始 / 停止按钮
        act = tk.Frame(inner, bg=CARD)
        act.pack(fill="x", pady=(18, 10))
        self.start_btn = FlatBtn(act, text="🚀 开始采集", command=self.on_start,
                                 bg=RED, fg="white", font=F_BIG, padx=20, pady=10)
        self.start_btn.pack(side="left", fill="x", expand=True)
        self.stop_btn = FlatBtn(act, text="⏹ 停止采集", command=self.on_stop,
                                bg="#F2F2F2", fg="#333333", font=F_BIG, padx=20, pady=10)
        self.stop_btn.pack(side="left", padx=(12, 0))
        self.stop_btn.set_enabled(False, "#999999")

        # 日志
        log_bar = tk.Frame(inner, bg=CARD)
        log_bar.pack(fill="x", pady=(6, 4))
        self.log_title = tk.Label(log_bar, text="运行日志 (0)", font=F_BODY,
                                  fg="#333333", bg=CARD)
        self.log_title.pack(side="left")
        FlatBtn(log_bar, text="复制日志", command=self.on_copy_log, bg="#F2F2F2",
                fg="#555555", font=F_SMALL, padx=10, pady=4).pack(side="right", padx=6)
        FlatBtn(log_bar, text="清空日志", command=self.on_clear_log, bg="#F2F2F2",
                fg="#555555", font=F_SMALL, padx=10, pady=4).pack(side="right")

        log_wrap = tk.Frame(inner, bg="#FAFAFA", highlightthickness=1,
                            highlightbackground="#E5E5E5")
        log_wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_wrap, font=F_LOG, fg=LOG_GREEN, bg="#FAFAFA",
                                relief="flat", wrap="word", state="disabled", height=10)
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)

        self.root.after(120, self._poll_log)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log(f"调试端口默认 {DEFAULT_PORT}，配置目录可复用登录态（{COOKIE_FILE}）")

    # ---------------------------------------------------------------- 日志
    def log(self, msg: str) -> None:
        self.log_queue.put(time.strftime("[%H:%M:%S] ") + msg)

    def _poll_log(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
                self.log_count += 1
                self.log_title.configure(text=f"运行日志 ({self.log_count})")
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log)

    def on_copy_log(self) -> None:
        text = self.log_text.get("1.0", "end").strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log("日志已复制到剪贴板")

    def on_clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.log_count = 0
        self.log_title.configure(text="运行日志 (0)")

    # ---------------------------------------------------------------- 事件
    def on_pick_dir(self) -> None:
        d = filedialog.askdirectory()
        if d:
            self.save_dir_var.set(d)

    def on_pick_browser(self) -> None:
        p = filedialog.askopenfilename(title="选择 Chrome 可执行文件")
        if p:
            self.browser_path_var.set(p)

    def _ui(self, fn: Callable[[], None]) -> None:
        """从工作线程安排一次 UI 更新（窗口已关闭时静默失败）。"""
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    def on_open_browser(self) -> None:
        self.open_btn.set_enabled(False)
        self.browser_status.configure(text="正在打开…", fg="#999999")

        def worker() -> None:
            try:
                port = int(self.port_var.get() or DEFAULT_PORT)
                _, cookies = open_browser(port, self.browser_path_var.get())
                self.log(f"浏览器已打开(端口 {port})，如需登录请在页面操作后再次点击")
                self.log(f"已读取 cookie: did={cookies['did'][:28]}… 共 {len(cookies)} 项")
                self._ui(lambda: self.browser_status.configure(
                    text="浏览器已打开", fg=LOG_GREEN))
            except Exception as exc:
                self.log(f"打开浏览器失败: {exc}")
                self._ui(lambda: self.browser_status.configure(
                    text="浏览器未打开", fg="#999999"))
            finally:
                self._ui(lambda: (self.open_btn.set_enabled(True),
                                            self.open_btn.configure(text="打开浏览器")))

        threading.Thread(target=worker, daemon=True).start()

    def on_stop(self) -> None:
        if not self.collecting:
            return
        self.log("收到停止指令, 正在收尾…")
        self.stop_event.set()
        self.stop_btn.set_enabled(False, "#999999")

    def on_start(self) -> None:
        if self.collecting:
            return
        keyword = self.keyword_var.get().strip()
        if not keyword:
            messagebox.showwarning("提示", "请填写关键字")
            return
        try:
            max_feeds = max(1, int(self.max_feeds_var.get() or "20"))
            max_comments = max(0, int(self.max_comments_var.get() or "0"))
            port = int(self.port_var.get() or DEFAULT_PORT)
        except ValueError:
            messagebox.showwarning("提示", "采集数量 / 端口必须是数字")
            return
        save_dir = self.save_dir_var.get().strip() or DEFAULT_SAVE_DIR

        self.collecting = True
        self.stop_event.clear()
        self.start_btn.set_enabled(False)
        self.start_btn.configure(text="采集中…")
        self.stop_btn.set_enabled(True)

        def worker() -> None:
            try:
                reason = run_collect(self.log, keyword, max_feeds, max_comments,
                                     save_dir, port, self.stop_event)
                self.log(reason)
            except Exception as exc:
                self.log(f"采集失败: {exc}")
            finally:
                self.collecting = False

                def restore() -> None:
                    self.start_btn.set_enabled(True)
                    self.start_btn.configure(text="🚀 开始采集")
                    self.stop_btn.set_enabled(False, "#999999")

                self._ui(restore)

        threading.Thread(target=worker, daemon=True).start()

    def on_close(self) -> None:
        close_browser()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
