"""快手关键词采集工具（PyQt5 GUI 版）

功能:
    1. 关键词搜索采集  ->  POST /rest/v/search/feed   (sig4 签名, node sign.js 纯算)
    2. 视频评论采集    ->  POST /rest/v/photo/comment/list (无签名)
    3. 浏览器统一 Chrome, 自动化统一 DrissionPage, 只负责打开页面登录 + 读取 cookie
    4. cookie / 登录态统一存 ks_config/cookies.json, 下次启动自动载入、复用登录态
    5. Windows / macOS 通用; 采集与浏览器操作全在 QThread 里跑, 主线程只负责 UI

运行:
    python ks_gui.py
依赖:
    pip install PyQt5 DrissionPage requests      + 本机需装 Node.js(搜索接口签名用)
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

import requests
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSizePolicy, QTextEdit, QVBoxLayout,
    QWidget,
)

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

PAGE_INTERVAL = 2.0          # 搜索翻页间隔(秒)
COMMENT_PAGE_INTERVAL = 1.5  # 评论翻页间隔(秒)
RETRY_TIMES = 3              # 限流退避重试次数
RETRY_INTERVAL = 15.0        # 重试间隔(秒)

IS_WIN = os.name == "nt"
IS_FROZEN = getattr(sys, "frozen", False)   # PyInstaller 打包后为 True


def _resource_dir() -> Path:
    """只读资源目录(sign.js / jose.js / node.exe)。

    打包后 PyInstaller 会把 datas 解压到 sys._MEIPASS。
    """
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return Path(__file__).resolve().parent


def _app_dir() -> Path:
    """可写目录: cookie 存档 / chrome profile 放这里(打包后取 exe 所在目录)。"""
    if IS_FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _resource_dir()
SIGN_JS = BASE_DIR / "sign.js"
CONFIG_DIR = _app_dir() / "ks_config"
COOKIE_FILE = CONFIG_DIR / "cookies.json"

RED = "#FF2442"
RED_HOVER = "#E01E3A"
GREY = "#F2F2F2"
GREY_HOVER = "#E4E4E4"
LOG_GREEN = "#2E9E44"

FONT_FAMILY = "Microsoft YaHei" if IS_WIN else "PingFang SC"
FONT_MONO = "Consolas" if IS_WIN else "Menlo"


# --------------------------------------------------------------------------- node
FALLBACK_NODE = "/usr/local/bin/node"   # macOS 上 which 不到 node 时的兜底


def find_node() -> str:
    """定位 node 可执行文件: 打包内置的 > 系统 PATH > 常见安装路径。"""
    if IS_FROZEN:      # exe 自带一份 node, 免得用户还得装 Node.js
        builtin = BASE_DIR / ("node.exe" if IS_WIN else "node")
        if builtin.exists():
            return str(builtin)
    p = shutil.which("node") or shutil.which("node.exe")
    if p:
        return p
    if Path(FALLBACK_NODE).exists():
        return FALLBACK_NODE
    raise RuntimeError("未找到 Node.js，请安装后重试（搜索接口签名需要）")


def _no_window() -> dict:
    """Windows 下 GUI 模式运行时不弹黑色控制台窗口。"""
    if not IS_WIN:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": si}


def sign(node_bin: str, url_path: str, query: dict, form: dict, request_body: dict) -> dict:
    payload = {
        "cookie": "", "url": url_path, "query": query,
        "form": form, "requestBody": request_body,
    }
    proc = subprocess.run(
        [node_bin, str(SIGN_JS), json.dumps(payload, ensure_ascii=False)],
        cwd=str(BASE_DIR), capture_output=True, text=True, timeout=60, **_no_window(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"签名失败:\n{proc.stderr[-1500:]}")
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------- cookie 存档(JSON)
def load_cookie_store() -> dict[str, Any]:
    """读取 cookie 存档; 兼容旧格式(扁平 name->value)。"""
    empty = {"cookies": {}, "updated_at": "", "source": "", "user_id": "", "logged_in": False}
    if not COOKIE_FILE.exists():
        return empty
    try:
        data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(data, dict) or "cookies" not in data:
        data = {
            "cookies": {k: v for k, v in data.items() if isinstance(v, str)},
            "updated_at": "", "source": "legacy", "user_id": "", "logged_in": False,
        }
    return data


def save_cookies(cookies: dict[str, str], source: str = "browser") -> dict[str, Any]:
    """合并写入 cookie 存档(JSON)并返回完整存档。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    store = load_cookie_store()
    merged = dict(store.get("cookies", {}))
    merged.update(cookies or {})

    user_id = (cookies or {}).get("userId") or store.get("user_id", "")
    logged_in = bool(user_id) or bool((cookies or {}).get("passToken"))
    store = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "user_id": user_id,
        "logged_in": logged_in,
        "cookies": merged,
    }
    COOKIE_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    return store


def load_cookies() -> dict[str, str]:
    return load_cookie_store().get("cookies", {})


# --------------------------------------------------------------------------- 浏览器(DrissionPage)
_browser: Any = None


def detect_chrome() -> str:
    """自动探测本机 Chrome 可执行文件(优先标准名, 再退到任意含 chrome 的 App)。"""
    env_path = os.environ.get("CHROME_PATH", "")
    if env_path and Path(env_path).exists():
        return env_path

    cands: list[str] = []
    if IS_WIN:
        cands += [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        local = os.environ.get("LOCALAPPDATA", "")
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

    if IS_WIN:   # 兜底: 扫 Program Files 下任意 *Chrome*/Application/chrome.exe
        for root in (r"C:\Program Files", r"C:\Program Files (x86)"):
            d = Path(root)
            if not d.exists():
                continue
            for exe in sorted(d.glob("*Chrome*/Application/chrome.exe")):
                if exe.is_file():
                    return str(exe)
        return ""

    apps = Path("/Applications")
    if apps.exists():   # 兜底: 扫 /Applications 下名字含 chrome 的 App(如 GPT Chrome.app)
        for app in sorted(apps.glob("*.app")):
            if "chrome" in app.name.lower() or "chromium" in app.name.lower():
                for exe in sorted((app / "Contents" / "MacOS").glob("*")):
                    if exe.is_file() and os.access(exe, os.X_OK):
                        return str(exe)
    return ""


def browser_has_page(port: int, host: str = "127.0.0.1") -> bool:
    """端口上是否有一个真正可用(带页面标签)的调试浏览器。"""
    try:
        r = requests.get(f"http://{host}:{port}/json", timeout=2)
        return any(t.get("type") in ("page", "webview") for t in r.json())
    except Exception:
        return False


def find_free_port(start: int) -> int:
    for p in range(start, start + 30):
        with closing(socket.socket()) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def open_browser(port: int, browser_path: str = "") -> tuple[Any, dict[str, str], int]:
    """打开(或接管)Chrome, 停在快手首页, 读取并保存 cookie; 返回 (page, cookies, 实际端口)。

    端口上已有带页面的调试浏览器 -> 直接接管(不重复拉起, 免得抢 profile 锁);
    否则                      -> 换一个空闲端口, 用固定 profile 目录拉起,
                                 目录持久化 = 登录态下次可直接复用。
    """
    global _browser
    from DrissionPage import ChromiumPage, ChromiumOptions

    path = browser_path.strip() or detect_chrome()
    if not path:
        raise RuntimeError("未找到 Chrome，请在「浏览器路径」里手动指定")

    co = ChromiumOptions()
    if browser_has_page(port):
        co.set_address(f"127.0.0.1:{port}")            # 接管已有浏览器
        _browser = ChromiumPage(co)
    else:
        # profile 目录按端口独立: 端口空闲就不会和别的实例抢同一个 profile 锁,
        # 目录持久化又能让登录态下次直接复用; 万一还起不来, 换端口重试。
        last_err: Exception | None = None
        for _ in range(3):
            port = find_free_port(port)
            opt = (ChromiumOptions().set_local_port(port)
                   .set_browser_path(path)
                   .set_user_data_path(str(CONFIG_DIR / f"chrome_profile_{port}")))
            try:
                _browser = ChromiumPage(opt)
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                port += 1
        if last_err is not None:
            raise RuntimeError(f"拉起 Chrome 失败: {last_err}")

    _browser.get(f"{HOST}/")
    time.sleep(1.5)
    return _browser, read_cookies_from_browser(), port


def read_cookies_from_browser() -> dict[str, str]:
    if _browser is None:
        raise RuntimeError("浏览器未打开")
    cks = _browser.cookies(all_domains=True)
    cookies = {c["name"]: c["value"] for c in cks if "kuaishou" in (c.get("domain") or "")}
    if not cookies.get("did"):
        raise RuntimeError("浏览器里没有拿到 did cookie，请确认页面已打开 kuaishou.com")
    save_cookies(cookies, source="browser")
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
        "keyword": keyword, "page": "search", "webPageArea": "", "pcursor": pcursor,
    }
    if search_session_id:
        body["searchSessionId"] = search_session_id

    sig = sign(node_bin, SEARCH_PATH, {}, {}, body)
    url = f"{HOST}{SEARCH_PATH}?__NS_hxfalcon={sig['sign']}&caver={sig['caver']}"
    headers = {
        "Accept": "application/json", "Content-Type": "application/json",
        "Origin": HOST, "Referer": REFERER, "User-Agent": UA,
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
        f"搜索接口持续返回 result={data.get('result')} error_msg={data.get('error_msg')}")


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
        "Accept": "application/json", "Accept-Language": "zh-CN,zh;q=0.9",
        "Cache-Control": "no-cache", "Pragma": "no-cache",
        "Content-Type": "application/json", "Origin": HOST, "Referer": REFERER,
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin", "User-Agent": UA,
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
        f"评论接口持续返回 result={data.get('result')} error_msg={data.get('error_msg')}")


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
                save_dir: str, stop: threading.Event) -> str:
    """在工作线程里执行; stop 被 set 时尽快中断; 返回结束原因。"""
    node_bin = find_node()
    log(f"Node: {node_bin}")

    def nap(seconds: float) -> bool:
        """可中断的 sleep, 返回 True 表示已收到停止指令。"""
        return stop.wait(seconds)

    if _browser is not None:
        try:
            cookies = read_cookies_from_browser()
            log("已从浏览器读取最新 cookie")
        except Exception as exc:
            cookies = load_cookies()
            log(f"浏览器读取 cookie 失败({exc}), 改用本地存档")
    else:
        cookies = load_cookies()
        log("浏览器未打开, 使用本地 cookie 存档")

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
                log("评论采集被中断")
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


# --------------------------------------------------------------------------- 工作线程
class BrowserWorker(QThread):
    """打开浏览器 + 读取 cookie(重活, 不占主线程)。"""

    log = pyqtSignal(str)
    finished_ok = pyqtSignal(dict)   # cookie 存档
    failed = pyqtSignal(str)

    def __init__(self, port: int, browser_path: str) -> None:
        super().__init__()
        self.port = port
        self.browser_path = browser_path
        self.used_port = port

    def run(self) -> None:
        try:
            _, cookies, used = open_browser(self.port, self.browser_path)
            self.used_port = used
            if used != self.port:
                self.log.emit(f"端口 {self.port} 上没有可接管的页面，已改用端口 {used}")
            self.log.emit(f"浏览器已打开(端口 {used})，如需登录请在页面操作后再次点击")
            self.log.emit(f"已读取 cookie: did={cookies['did'][:28]}… 共 {len(cookies)} 项")
            self.finished_ok.emit(load_cookie_store())
        except Exception as exc:
            self.failed.emit(f"打开浏览器失败: {exc}")


class CollectWorker(QThread):
    """搜索 + 评论采集。"""

    log = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, keyword: str, max_feeds: int, max_comments: int, save_dir: str) -> None:
        super().__init__()
        self.keyword = keyword
        self.max_feeds = max_feeds
        self.max_comments = max_comments
        self.save_dir = save_dir
        self.stop = threading.Event()

    def request_stop(self) -> None:
        self.stop.set()

    def run(self) -> None:
        try:
            reason = run_collect(self.log.emit, self.keyword, self.max_feeds,
                                 self.max_comments, self.save_dir, self.stop)
            self.finished_ok.emit(reason)
        except Exception as exc:
            self.failed.emit(f"采集失败: {exc}")


# --------------------------------------------------------------------------- 控件
class CardButton(QPushButton):
    """统一的扁平按钮(固定高度、点击区够大、hover 有反馈)。"""

    def __init__(self, text: str, bg: str = GREY, fg: str = "#333333",
                 hover: str = GREY_HOVER, height: int = 36, font_size: int = 12,
                 bold: bool = False) -> None:
        super().__init__(text)
        self._bg, self._hover, self._fg = bg, hover, fg
        self.setMinimumHeight(height)
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(QFont(FONT_FAMILY, font_size, QFont.Bold if bold else QFont.Normal))
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._apply(bg)

    def _apply(self, bg: str) -> None:
        self.setStyleSheet(
            f"QPushButton {{ background:{bg}; color:{self._fg}; border:none;"
            f" border-radius:6px; padding:0 16px; }}"
            f"QPushButton:disabled {{ background:#F5F5F5; color:#BBBBBB; }}"
        )

    def enterEvent(self, event) -> None:        # noqa: N802
        if self.isEnabled():
            self._apply(self._hover)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:        # noqa: N802
        if self.isEnabled():
            self._apply(self._bg)
        super().leaveEvent(event)


def line_edit(text: str = "", width: int | None = None) -> QLineEdit:
    e = QLineEdit(text)
    e.setMinimumHeight(34)
    if width:
        e.setFixedWidth(width)
    e.setFont(QFont(FONT_FAMILY, 12))
    e.setStyleSheet(
        "QLineEdit { border:1px solid #DDDDDD; border-radius:6px; padding:0 10px;"
        " background:#FFFFFF; }"
        "QLineEdit:focus { border:1px solid #FF2442; }"
    )
    return e


# --------------------------------------------------------------------------- 主窗口
class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.browser_worker: BrowserWorker | None = None
        self.collect_worker: CollectWorker | None = None
        self.log_count = 0

        self.setWindowTitle("快手关键词采集工具")
        self.resize(880, 720)
        self.setMinimumSize(800, 660)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(0)
        card = QFrame()
        card.setStyleSheet(
            "QFrame{background:#FFFFFF; border:1px solid #E5E5E5; border-radius:10px;}")
        root.addWidget(card)
        box = QVBoxLayout(card)
        box.setContentsMargins(32, 26, 32, 26)
        box.setSpacing(0)

        title = QLabel("快手关键词采集工具")
        title.setFont(QFont(FONT_FAMILY, 20, QFont.Bold))
        title.setStyleSheet(f"color:{RED};")
        box.addWidget(title)
        sub = QLabel("打开浏览器登录后即可采集；cookie 与登录态保存在 ks_config/cookies.json")
        sub.setFont(QFont(FONT_FAMILY, 11))
        sub.setStyleSheet("color:#999999;")
        box.addWidget(sub)
        box.addSpacing(16)

        form = QGridLayout()
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(12)
        form.setColumnStretch(1, 1)
        box.addLayout(form)

        form.addWidget(self._label("关键字"), 0, 0)
        self.keyword_edit = line_edit(DEFAULT_KEYWORD)
        form.addWidget(self.keyword_edit, 0, 1, 1, 3)

        form.addWidget(self._label("采集视频数"), 1, 0)
        row1 = QHBoxLayout()
        self.max_feeds_edit = line_edit(str(DEFAULT_MAX_FEEDS), 90)
        row1.addWidget(self.max_feeds_edit)
        row1.addSpacing(20)
        row1.addWidget(self._label("每视频评论数"))
        self.max_comments_edit = line_edit(str(DEFAULT_MAX_COMMENTS), 90)
        row1.addWidget(self.max_comments_edit)
        row1.addStretch(1)
        form.addLayout(row1, 1, 1, 1, 3)

        form.addWidget(self._label("浏览器"), 2, 0)
        brow = QHBoxLayout()
        self.open_btn = CardButton("打开浏览器", height=36)
        self.open_btn.clicked.connect(self.on_open_browser)
        brow.addWidget(self.open_btn)
        brow.addSpacing(12)
        brow.addWidget(self._label("端口", 44))
        self.port_edit = line_edit(str(DEFAULT_PORT), 70)
        brow.addWidget(self.port_edit)
        brow.addSpacing(12)
        self.cookie_status = QLabel("未载入 cookie")
        self.cookie_status.setFont(QFont(FONT_FAMILY, 11))
        self.cookie_status.setStyleSheet("color:#999999;")
        brow.addWidget(self.cookie_status)
        brow.addStretch(1)
        form.addLayout(brow, 2, 1, 1, 3)

        form.addWidget(self._label("浏览器路径"), 3, 0)
        self.browser_path_edit = line_edit(detect_chrome())
        self.path_btn = CardButton("选择", height=34)
        self.path_btn.clicked.connect(self.on_pick_browser)
        form.addWidget(self.browser_path_edit, 3, 1, 1, 2)
        form.addWidget(self.path_btn, 3, 3)

        form.addWidget(self._label("保存目录"), 4, 0)
        self.save_dir_edit = line_edit(DEFAULT_SAVE_DIR)
        self.dir_btn = CardButton("选择", height=34)
        self.dir_btn.clicked.connect(self.on_pick_dir)
        form.addWidget(self.save_dir_edit, 4, 1, 1, 2)
        form.addWidget(self.dir_btn, 4, 3)

        box.addSpacing(18)

        act = QHBoxLayout()
        act.setSpacing(12)
        self.start_btn = CardButton("🚀 开始采集", bg=RED, fg="white", hover=RED_HOVER,
                                    height=48, font_size=14, bold=True)
        self.start_btn.setMinimumWidth(200)
        self.start_btn.clicked.connect(self.on_start)
        act.addWidget(self.start_btn, 3)
        self.stop_btn = CardButton("⏹ 停止采集", height=48, font_size=14)
        self.stop_btn.setMinimumWidth(150)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.on_stop)
        act.addWidget(self.stop_btn, 1)
        box.addLayout(act)

        box.addSpacing(20)

        log_bar = QHBoxLayout()
        self.log_title = QLabel("运行日志 (0)")
        self.log_title.setFont(QFont(FONT_FAMILY, 12))
        log_bar.addWidget(self.log_title)
        log_bar.addStretch(1)
        self.copy_btn = CardButton("复制日志", height=30, font_size=11)
        self.copy_btn.clicked.connect(self.on_copy_log)
        self.clear_btn = CardButton("清空日志", height=30, font_size=11)
        self.clear_btn.clicked.connect(self.on_clear_log)
        log_bar.addWidget(self.clear_btn)
        log_bar.addSpacing(8)
        log_bar.addWidget(self.copy_btn)
        box.addLayout(log_bar)
        box.addSpacing(6)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont(FONT_MONO, 11))
        self.log_view.setStyleSheet(
            "QTextEdit{background:#FAFAFA; color:" + LOG_GREEN + ";"
            "border:1px solid #E5E5E5; border-radius:6px; padding:6px;}")
        self.log_view.setMinimumHeight(180)
        box.addWidget(self.log_view, 1)

        self._refresh_cookie_status()
        store = load_cookie_store()
        if store.get("source") == "legacy":        # 旧扁平格式 -> 升级成带登录态的结构
            store = save_cookies(store["cookies"], source="migrated")
        if store.get("cookies"):
            self.log(
                f"已载入本地 cookie 存档: {COOKIE_FILE}"
                f"（更新于 {store.get('updated_at') or '未知时间'}，"
                f"{'已登录' if store.get('logged_in') else '未登录'}）")
        else:
            self.log("尚无 cookie 存档，点击「打开浏览器」登录后会自动写入 JSON")

    # ---------------------------------------------------------------- 小工具
    def _label(self, text: str, width: int = 88) -> QLabel:
        lb = QLabel(text)
        lb.setFont(QFont(FONT_FAMILY, 12))
        lb.setStyleSheet("color:#333333;")
        lb.setFixedWidth(width)
        return lb

    def log(self, msg: str) -> None:
        """只由主线程改 UI(工作线程经信号转接)。"""
        self.log_view.append(time.strftime("[%H:%M:%S] ") + msg)
        self.log_count += 1
        self.log_title.setText(f"运行日志 ({self.log_count})")

    def _refresh_cookie_status(self) -> None:
        store = load_cookie_store()
        if not store.get("cookies"):
            self.cookie_status.setText("未载入 cookie")
            self.cookie_status.setStyleSheet("color:#999999;")
            return
        who = f"已登录 {store.get('user_id')}" if store.get("logged_in") else "未登录(游客)"
        self.cookie_status.setText(f"cookie 已就绪 · {who} · {store.get('updated_at', '')}")
        self.cookie_status.setStyleSheet(
            f"color:{'#2E9E44' if store.get('logged_in') else '#999999'};")

    # ---------------------------------------------------------------- 事件
    def on_pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", self.save_dir_edit.text())
        if d:
            self.save_dir_edit.setText(d)

    def on_pick_browser(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "选择 Chrome 可执行文件", "/Applications")
        if p:
            self.browser_path_edit.setText(p)

    def on_copy_log(self) -> None:
        QApplication.clipboard().setText(self.log_view.toPlainText())
        self.log("日志已复制到剪贴板")

    def on_clear_log(self) -> None:
        self.log_view.clear()
        self.log_count = 0
        self.log_title.setText("运行日志 (0)")

    def on_open_browser(self) -> None:
        if self.browser_worker and self.browser_worker.isRunning():
            return
        try:
            port = int(self.port_edit.text() or DEFAULT_PORT)
        except ValueError:
            QMessageBox.warning(self, "提示", "端口必须是数字")
            return

        # 点击后立刻反馈, 重活丢给线程
        self.cookie_status.setText("正在打开浏览器…")
        self.cookie_status.setStyleSheet("color:#999999;")
        self.open_btn.setEnabled(False)
        self.log("正在启动 Chrome…")

        self.browser_worker = BrowserWorker(port, self.browser_path_edit.text())
        self.browser_worker.log.connect(self.log)
        self.browser_worker.finished_ok.connect(self._on_browser_ready)
        self.browser_worker.failed.connect(self._on_browser_failed)
        self.browser_worker.finished.connect(lambda: self.open_btn.setEnabled(True))
        self.browser_worker.start()

    def _on_browser_ready(self, _store: dict) -> None:
        if self.browser_worker is not None:
            self.port_edit.setText(str(self.browser_worker.used_port))
        self._refresh_cookie_status()
        self.log(f"cookie 已写入 {COOKIE_FILE}")

    def _on_browser_failed(self, msg: str) -> None:
        self.log(msg)
        self.cookie_status.setText("浏览器未打开")
        self.cookie_status.setStyleSheet("color:#999999;")

    def on_start(self) -> None:
        if self.collect_worker and self.collect_worker.isRunning():
            return
        keyword = self.keyword_edit.text().strip()
        if not keyword:
            QMessageBox.warning(self, "提示", "请填写关键字")
            return
        try:
            max_feeds = max(1, int(self.max_feeds_edit.text() or "20"))
            max_comments = max(0, int(self.max_comments_edit.text() or "0"))
        except ValueError:
            QMessageBox.warning(self, "提示", "采集数量必须是数字")
            return
        save_dir = self.save_dir_edit.text().strip() or DEFAULT_SAVE_DIR

        self.start_btn.setEnabled(False)
        self.start_btn.setText("采集中…")
        self.stop_btn.setEnabled(True)

        self.collect_worker = CollectWorker(keyword, max_feeds, max_comments, save_dir)
        self.collect_worker.log.connect(self.log)
        self.collect_worker.finished_ok.connect(self.log)
        self.collect_worker.failed.connect(self.log)
        self.collect_worker.finished.connect(self._on_collect_finished)
        self.collect_worker.start()

    def on_stop(self) -> None:
        if self.collect_worker and self.collect_worker.isRunning():
            self.log("收到停止指令, 正在收尾…")
            self.collect_worker.request_stop()
            self.stop_btn.setEnabled(False)

    def _on_collect_finished(self) -> None:
        self.start_btn.setEnabled(True)
        self.start_btn.setText("🚀 开始采集")
        self.stop_btn.setEnabled(False)

    def closeEvent(self, event) -> None:          # noqa: N802
        if self.collect_worker and self.collect_worker.isRunning():
            self.collect_worker.request_stop()
            self.collect_worker.wait(3000)
        close_browser()
        super().closeEvent(event)


def main() -> None:
    app = QApplication([])
    app.setFont(QFont(FONT_FAMILY, 12))
    win = MainWindow()
    win.show()
    app.exec_()


if __name__ == "__main__":
    main()
