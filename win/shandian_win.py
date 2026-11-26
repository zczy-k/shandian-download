#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闪电全能下载 · Windows 客户端

macOS 原版是 SwiftUI 壳（11082 行）+ Gopeed 内核 + yt-dlp。
本文件重写壳，对齐原版功能面：

  引擎托管 / 视频下载 / 浏览器扩展网关 6800 / aria2 RPC / 剪贴板监听
  分类目录 / 限速 / 并发 / 定时下载 / 完成通知与提示音 / 回收站
  磁力选文件 / 诊断导出 / 更新检查 / MCP 服务 / 中英双语 / 开机自启

零第三方依赖，只用 Python 3.10+ 标准库。
"""

from __future__ import annotations

import atexit
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# --------------------------------------------------------------------------- 路径


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _res_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()
RES_DIR = _res_dir()
USER_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ShandianDownload"
TOOLS_DIR = USER_DIR / "tools"
DATA_DIR = USER_DIR / "data"
CONFIG_FILE = USER_DIR / "config.json"
LOG_FILE = USER_DIR / "app.log"
WEB_DIR = RES_DIR / "web"
BUNDLED_TOOLS = APP_DIR / "tools"

GOPEED_NAME = "gopeed.exe"
YTDLP_NAME = "yt-dlp.exe"
FFMPEG_NAME = "ffmpeg.exe"

APP_VERSION = "0.23.0"
GATEWAY_PORT = 6800
UI_PORT = 8765


def tool_path(name: str) -> Path | None:
    for p in (TOOLS_DIR / name, BUNDLED_TOOLS / name):
        if p.exists():
            return p
    return None


ENGINE_VERSION = "1.9.3"
# 官方 release 的 Windows 包是 GUI 版，不接受 -A/-P/-T 无头参数；
# 无头内核由本仓库 Actions 用补丁源码编译，随 win-latest 发布
GOPEED_URLS = ["https://github.com/zczy-k/shandian-download/releases/download/win-latest/gopeed.exe"]
YTDLP_URLS = [
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
    "https://ghfast.top/https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
]
FFMPEG_URLS = [
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "https://ghfast.top/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]
TRACKER_SOURCES = [
    "https://cf.trackerslist.com/best.txt",
    "https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt",
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
    "https://ghfast.top/https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
]

# --------------------------------------------------------------------------- 链接识别（对齐 Links.swift）


VIDEO_HOSTS = [
    "bilibili.com", "b23.tv", "youtube.com", "youtu.be", "douyin.com", "xiaohongshu.com",
    "xhslink.com", "kuaishou.com", "weibo.com", "ixigua.com", "vimeo.com", "x.com",
    "twitter.com", "xinpianchang.com",
]

FILE_EXTENSIONS = {
    "zip", "rar", "7z", "gz", "tgz", "bz2", "xz", "dmg", "pkg", "exe", "msi", "iso", "img",
    "apk", "ipa", "mp4", "mkv", "avi", "mov", "m4v", "ts", "flv", "wmv", "rmvb", "webm",
    "mp3", "flac", "wav", "m4a", "aac",
}

PAN_HOSTS = ["pan.baidu.com", "yun.baidu.com", "aliyundrive.com", "alipan.com",
             "cloud.189.cn", "115.com", "123pan.com", "mypikpak.com"]

BTIH_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[A-Za-z2-7]{32})$")
LINK_RE = re.compile(r"""(magnet:\?[^\s"'<>]+|ed2k://[^\s"'<>]+|https?://[^\s"'<>]+)""", re.I)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_wechat_share(raw: str) -> bool:
    raw = raw.strip()
    if not raw.lower().startswith(("http://", "https://")):
        return False
    if _host(raw) != "weixin.qq.com":
        return False
    parts = [p for p in urlparse(raw).path.split("/") if p]
    return len(parts) == 2 and parts[0].lower() == "sph"


def baidu_share_url(raw: str) -> str | None:
    raw = raw.strip()
    if not raw.lower().startswith(("http://", "https://")):
        return None
    if _host(raw) not in ("pan.baidu.com", "yun.baidu.com"):
        return None
    p = "/" + "/".join(x for x in urlparse(raw).path.lower().split("/") if x)
    if p.startswith("/s/") or p in ("/s", "/share/link", "/share/init", "/wap/link"):
        return raw
    return None


def link_kind(raw: str) -> str | None:
    """返回 magnet / torrent / ed2k / video / http，认不出返回 None。"""
    s = raw.strip()
    low = s.lower()
    if low.startswith("magnet:?"):
        return "magnet"
    if low.startswith("ed2k://"):
        return "ed2k"
    if low.endswith(".torrent") and os.path.isfile(s):
        return "torrent"
    try:
        u = urlparse(s)
    except Exception:
        return None
    scheme = (u.scheme or "").lower()
    if scheme not in ("http", "https", "ftp"):
        return None
    host = (u.hostname or "").lower()
    if not host:
        return None
    ext = os.path.splitext(u.path)[1].lstrip(".").lower()
    if scheme in ("http", "https") and ext in ("m3u8", "mpd"):
        return "video"
    parts = [p for p in u.path.split("/") if p]
    if host == "movie.douban.com" and len(parts) == 2 and parts[0].lower() == "trailer" and parts[1].isdigit():
        return "video"
    if any(host == h or host.endswith("." + h) for h in VIDEO_HOSTS):
        return "video"
    if u.path.lower().endswith(".torrent"):
        return "torrent"
    return "http"


def worth_popup(link: str) -> bool:
    if is_wechat_share(link) or baidu_share_url(link):
        return True
    kind = link_kind(link)
    if kind is None:
        return False
    if kind != "http":
        return True
    host = _host(link)
    if host == "pcs.baidu.com" or host.endswith(".pcs.baidu.com"):
        return True
    if any(h in host for h in PAN_HOSTS if h != "baidu.com"):
        return True
    ext = os.path.splitext(urlparse(link).path)[1].lstrip(".").lower()
    return ext in FILE_EXTENSIONS


def extract_links(text: str) -> list[str]:
    out, seen = [], set()
    for m in LINK_RE.finditer(text):
        s = m.group(0)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def pasted_links(text: str) -> list[str]:
    t = text.strip()
    if not any(c.isspace() for c in t) and link_kind(t) is not None:
        return [t]
    if t.lower().endswith(".torrent") and os.path.isfile(t):
        return [t]
    return extract_links(t)


def validate_magnet(url: str) -> str | None:
    if not url.lower().startswith("magnet:"):
        return None
    q = url.split("?", 1)[1] if "?" in url else ""
    xts = [p[3:] for p in q.split("&") if p.lower().startswith("xt=")]
    if not xts:
        return "磁力链接缺少 xt 参数，可能复制不完整"
    for xt in xts:
        xt = urllib.parse.unquote(xt)
        if xt.lower().startswith("urn:btih:"):
            h = xt[9:]
            if BTIH_RE.match(h):
                return None
            return (f"磁力链接的 btih 是 {len(h)} 位，标准应为 40 位十六进制或 32 位 base32。"
                    "链接多半是复制时漏了字符，请重新完整复制")
    return "磁力链接缺少 urn:btih 标识"


# --------------------------------------------------------------------------- 配置


DEFAULT_CONFIG = {
    "download_dir": str(Path.home() / "Downloads" / "Shandian"),
    "dir_movie": "",
    "dir_video": "",
    "dir_other": "",
    "remember_dir": True,
    "last_movie": "",
    "last_video": "",
    "last_other": "",
    "max_running": 3,
    "limit_kb": 0,
    "concurrency": 4,
    "cookies_from_browser": "",
    "engine_port": 0,
    "language": "zh",
    "notify": True,
    "done_sound": True,
    "watch_clipboard": True,
    "confirm_new": False,
    "launch_at_login": False,
    "prevent_sleep": True,
    "schedule_on": False,
    "schedule_from": 0,
    "schedule_to": 0,
    "auto_update_check": True,
    "recycle": [],
    "seen_version": "",
}

LOCK = threading.RLock()
VIDEO_TASKS: dict[str, dict] = {}
PENDING: dict[str, dict] = {}
CONFIG: dict = {}
LAST_DIR_KIND = "other"


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    for key, dflt in (("dir_movie", "movie"), ("dir_video", "video"), ("dir_other", "other")):
        if not cfg.get(key):
            cfg[key] = str(Path(cfg["download_dir"]) / dflt)
    Path(cfg["download_dir"]).mkdir(parents=True, exist_ok=True)
    for key in ("dir_movie", "dir_video", "dir_other"):
        try:
            Path(cfg[key]).mkdir(parents=True, exist_ok=True)
        except Exception:
            cfg[key] = cfg["download_dir"]
    return cfg


def save_config(cfg: dict | None = None) -> None:
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg or CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[配置] 保存失败：{exc}")


def dir_for(kind: str) -> str:
    """按链接类型选目录：磁力/种子→电影，视频站→视频，其余→其他文件。"""
    global LAST_DIR_KIND
    key = {"magnet": "dir_movie", "torrent": "dir_movie", "video": "dir_video"}.get(kind, "dir_other")
    last = {"dir_movie": "last_movie", "dir_video": "last_video", "dir_other": "last_other"}[key]
    LAST_DIR_KIND = {"dir_movie": "movie", "dir_video": "video", "dir_other": "other"}[key]
    if CONFIG.get("remember_dir") and CONFIG.get(last):
        p = Path(CONFIG[last])
        if p.exists():
            return str(p)
    return CONFIG[key]


# --------------------------------------------------------------------------- 工具下载


def _download(urls: list[str], dest: Path, label: str) -> bool:
    for url in urls:
        try:
            print(f"[工具] 正在下载 {label} …")
            req = urllib.request.Request(url, headers={"User-Agent": f"Shandian-Win/{APP_VERSION}"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=120) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".part")
                got, last = 0, 0.0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 18)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        now = time.time()
                        if now - last > 1.0:
                            last = now
                            pct = f"{got * 100 // total}%" if total else f"{got // 1024} KB"
                            print(f"\r[工具] {label} {pct}", end="", flush=True)
                print()
                tmp.replace(dest)
            return True
        except Exception as exc:
            print(f"\n[工具] {label} 下载失败：{exc}")
    return False


def _unzip_one(zip_path: Path, key: str, dest: Path, ext: str = ".exe") -> bool:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            target = None
            for info in zf.infolist():
                name = info.filename.lower()
                if name.endswith(ext) and key.lower() in name:
                    target = info
                    break
            if not target:
                print(f"[工具] 压缩包里没找到含 {key} 的 {ext}")
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(target) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
        return True
    except Exception as exc:
        print(f"[工具] 解压失败：{exc}")
        return False


def ensure_tools(need_ffmpeg: bool = False) -> None:
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    if tool_path(GOPEED_NAME) is None:
        dest = TOOLS_DIR / GOPEED_NAME
        if _download(GOPEED_URLS, dest, "下载内核 Gopeed"):
            print(f"[工具] 内核就绪：{dest}")
    if tool_path(YTDLP_NAME) is None:
        if _download(YTDLP_URLS, TOOLS_DIR / YTDLP_NAME, "视频组件 yt-dlp"):
            print(f"[工具] 视频组件就绪：{TOOLS_DIR / YTDLP_NAME}")
    if need_ffmpeg and tool_path(FFMPEG_NAME) is None:
        z = TOOLS_DIR / "ffmpeg.zip"
        if _download(FFMPEG_URLS, z, "ffmpeg（约 80 MB）"):
            ok = _unzip_one(z, "ffmpeg", TOOLS_DIR / FFMPEG_NAME)
            _unzip_one(z, "ffprobe", TOOLS_DIR / "ffprobe.exe")
            if ok:
                print(f"[工具] ffmpeg 就绪：{TOOLS_DIR / FFMPEG_NAME}")
            z.unlink(missing_ok=True)


# --------------------------------------------------------------------------- 引擎


class EngineError(RuntimeError):
    pass


def fetch_trackers() -> list[str]:
    for src in TRACKER_SOURCES:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(src, timeout=8) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception:
            continue
        out: list[str] = []
        for line in text.splitlines():
            t = line.strip()
            if "announce" in t and t not in out:
                out.append(t)
        if out:
            return out
    return []


class Engine:
    """托管 Gopeed headless。启动参数与 Engine.swift:242 相同。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.token = uuid.uuid4().hex
        self.alive = False
        self.version: str | None = None
        self._lock = threading.Lock()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1/"

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _req(self, method: str, path: str, body=None, timeout: float = 30) -> dict:
        url = self.base + path.lstrip("/")
        data = None
        headers = {"X-Api-Token": self.token}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise EngineError(f"HTTP {exc.code}") from exc
        except Exception as exc:
            raise EngineError(str(exc)) from exc
        env = json.loads(raw) if raw else {}
        if env.get("code", 0) != 0:
            raise EngineError(env.get("msg") or "内核返回错误")
        return env.get("data")

    def start(self) -> bool:
        if self.alive:
            return True
        gopeed = tool_path(GOPEED_NAME)
        if gopeed is None:
            print("[引擎] 缺少 gopeed.exe，跳过启动")
            return False
        with self._lock:
            if self.alive:
                return True
            port = int(CONFIG.get("engine_port") or 0) or self._free_port()
            engine_data = DATA_DIR / "engine"
            engine_data.mkdir(parents=True, exist_ok=True)
            log_path = DATA_DIR / "engine.log"
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            try:
                self.proc = subprocess.Popen(
                    [str(gopeed), "-A", "127.0.0.1", "-P", str(port), "-T", self.token,
                     "-d", str(engine_data), "-c", str(engine_data / "server.json")],
                    cwd=str(gopeed.parent),
                    stdout=open(log_path, "ab"),
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
            except Exception as exc:
                print(f"[引擎] 启动失败：{exc}")
                return False
            self.port = port
            for _ in range(40):
                time.sleep(0.3)
                try:
                    info = self._req("GET", "info", timeout=2)
                    self.alive = True
                    self.version = (info or {}).get("version")
                    self.apply_settings()
                    print(f"[引擎] 已就绪 v{self.version} @ 127.0.0.1:{port}")
                    return True
                except Exception:
                    continue
            print("[引擎] 启动超时，详见 data/engine.log")
            return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.alive = False

    def tasks(self) -> list[dict]:
        return self._req("GET", "tasks") or []

    def create(self, url: str, directory: str, headers: dict | None = None,
               name: str | None = None, select_files: list[int] | None = None) -> str:
        opts: dict = {"path": directory}
        if select_files:
            opts["selectFiles"] = select_files
        if name:
            opts["name"] = name
        req: dict = {"url": url}
        if url.lower().startswith("http") and headers:
            req["extra"] = {"header": headers}
        tid = self._req("POST", "tasks", {"req": req, "opts": opts}, timeout=180)
        if not tid:
            raise EngineError("内核未返回任务编号")
        return str(tid)

    def resolve(self, url: str, headers: dict | None = None) -> dict:
        req: dict = {"url": url}
        if headers:
            req["extra"] = {"header": headers}
        return self._req("POST", "resolve", {"req": req}, timeout=300) or {}

    def pause(self, tid: str) -> None:
        self._req("PUT", f"tasks/{tid}/pause")

    def resume(self, tid: str) -> None:
        self._req("PUT", f"tasks/{tid}/continue")

    def delete(self, tid: str, with_files: bool = False) -> None:
        self._req("DELETE", f"tasks/{tid}?force={'true' if with_files else 'false'}")

    def pause_all(self) -> None:
        self._req("PUT", "tasks/pause")

    def resume_all(self) -> None:
        self._req("PUT", "tasks/continue")

    def forward(self, method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"X-Api-Token": self.token}
        if body:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() or b"{}"
        except Exception:
            return 502, b"{}"

    def apply_settings(self) -> None:
        """对齐 macOS 版 Engine.swift:326 applySettings。"""
        try:
            cfg = self._req("GET", "config") or {}
        except Exception as exc:
            print(f"[引擎] 读取配置失败：{exc}")
            return
        cfg["downloadDir"] = CONFIG["download_dir"]
        cfg["maxRunning"] = int(CONFIG.get("max_running") or 3)
        extra = cfg.get("extra") or {}
        extra["shandianDownloadLimit"] = int(CONFIG.get("limit_kb") or 0)
        cfg["extra"] = extra
        cfg["proxy"] = {"enable": False, "system": False, "scheme": "http",
                        "host": "127.0.0.1:7897", "usr": "", "pwd": ""}
        proto = cfg.get("protocolConfig") or {}
        http = proto.get("http") or {}
        http["connections"] = 8
        http["userAgent"] = f"ShandianDownload/{APP_VERSION} (Windows)"
        proto["http"] = http
        bt = proto.get("bt") or {}
        bt["listenPort"] = 6881
        trackers = fetch_trackers()
        if trackers:
            bt["trackers"] = trackers
            print(f"[引擎] 已更新 {len(trackers)} 条 BT tracker")
        else:
            print("[引擎] tracker 列表拉取失败，沿用已有配置")
        proto["bt"] = bt
        cfg["protocolConfig"] = proto
        try:
            self._req("PUT", "config", cfg)
        except Exception as exc:
            print(f"[引擎] 写入配置失败：{exc}")


ENGINE = Engine()


def engine_task_view(t: dict) -> dict:
    meta = t.get("meta") or {}
    res = meta.get("res") or {}
    files = res.get("files") or []
    size = res.get("size") or sum(int(f.get("size") or 0) for f in files)
    prog = t.get("progress") or {}
    status_raw = (t.get("status") or "").lower()
    status = {"ready": "waiting", "waiting": "waiting", "running": "downloading",
              "pause": "paused", "paused": "paused", "done": "done",
              "error": "error"}.get(status_raw, status_raw or "waiting")
    name = res.get("name") or t.get("name") or ""
    if not name:
        name = (meta.get("req") or {}).get("url", "").rstrip("/").rsplit("/", 1)[-1][:80] or "未命名任务"
    return {
        "id": str(t.get("id")),
        "kind": "engine",
        "name": name,
        "status": status,
        "downloaded": int(prog.get("downloaded") or 0),
        "size": int(size or 0),
        "speed": int(prog.get("speed") or 0) if status == "downloading" else 0,
        "dir": (meta.get("opts") or {}).get("path") or CONFIG["download_dir"],
        "error": t.get("shandianError") or None,
        "site": None,
        "protocol": t.get("protocol"),
        "created": t.get("createdAt") or "",
        "files": [{"name": f.get("name"), "size": f.get("size"), "path": f.get("path")} for f in files],
    }


# --------------------------------------------------------------------------- 视频任务


def find_node() -> str | None:
    p = shutil.which("node.exe") or shutil.which("node")
    if p:
        return p
    for base in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe",
    ):
        if base.exists():
            return str(base)
    return None


def site_name(url: str) -> str | None:
    host = _host(url)
    table = [("movie.douban.com", "豆瓣"), ("bilibili", "B 站"), ("b23.tv", "B 站"),
             ("youtu", "YouTube"), ("douyin", "抖音"), ("xiaohongshu", "小红书"),
             ("xhslink", "小红书"), ("kuaishou", "快手"), ("weibo", "微博"),
             ("ixigua", "西瓜视频"), ("vimeo", "Vimeo"), ("x.com", "X"), ("twitter", "X"),
             ("xinpianchang", "新片场")]
    for key, label in table:
        if key in host:
            return label
    return None


def _video_worker(task: dict) -> None:
    url = task["url"]
    outdir = Path(task["dir"])
    outdir.mkdir(parents=True, exist_ok=True)
    ytdlp = tool_path(YTDLP_NAME)
    if ytdlp is None:
        with LOCK:
            task["status"] = "error"
            task["error"] = "缺少 yt-dlp.exe，无法下载视频站内容"
        return

    cmd = [
        str(ytdlp), "-P", str(outdir), "-o", "%(title).80s [%(id)s].%(ext)s",
        "--no-playlist", "--newline", "--no-colors", "--progress",
        "--concurrent-fragments", str(int(CONFIG.get("concurrency") or 4)),
        "--print", "before_dl:SDTITLE %(title)s",
        "--print", "before_dl:SDSIZE %(filesize,filesize_approx)s",
        "--print", "after_move:SDFILE %(filepath)s",
        "--progress-template",
        "download:SDPROG %(progress.downloaded_bytes)s %(progress.total_bytes)s "
        "%(progress.total_bytes_estimate)s %(progress.speed)s %(progress.status)s",
    ]
    ffmpeg = tool_path(FFMPEG_NAME)
    if ffmpeg:
        cmd += ["--ffmpeg-location", str(ffmpeg.parent)]
    node = find_node()
    if node:
        cmd += ["--js-runtimes", f"node:{node}"]
    browser = (CONFIG.get("cookies_from_browser") or "").strip()
    if browser:
        cmd += ["--cookies-from-browser", browser]
    cmd.append(url)

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=flags, encoding="utf-8", errors="replace", bufsize=1)
    except Exception as exc:
        with LOCK:
            task["status"] = "error"
            task["error"] = f"启动 yt-dlp 失败：{exc}"
        return

    with LOCK:
        task["status"] = "downloading"
        task["pid"] = proc.pid

    last_error = ""
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("SDTITLE "):
            with LOCK:
                task["name"] = line[8:].strip()
        elif line.startswith("SDSIZE "):
            raw = line[7:].strip()
            if raw.isdigit():
                with LOCK:
                    task["size"] = int(raw)
        elif line.startswith("SDPROG "):
            parts = line[7:].split()
            if len(parts) >= 5:
                def num(x: str) -> int:
                    try:
                        return int(float(x))
                    except Exception:
                        return 0
                done, total = num(parts[0]), num(parts[1]) or num(parts[2])
                with LOCK:
                    task["downloaded"] = done
                    if total:
                        task["size"] = total
                    task["speed"] = num(parts[3])
        elif line.startswith("SDFILE "):
            with LOCK:
                task["file"] = line[7:].strip()
                task["downloaded"] = task["size"] or task["downloaded"]
        elif "ERROR:" in line:
            last_error = line.split("ERROR:", 1)[1].strip()

    code = proc.wait()
    with LOCK:
        task["speed"] = 0
        task["pid"] = None
        if code == 0:
            task["status"] = "done"
            if task["size"] and not task["downloaded"]:
                task["downloaded"] = task["size"]
        else:
            task["status"] = "error"
            task["error"] = last_error or f"yt-dlp 退出码 {code}"
    if code == 0:
        notify_done(task.get("name") or "视频")


def add_video(url: str, directory: str | None = None) -> dict:
    tid = "v-" + uuid.uuid4().hex[:12]
    task = {
        "id": tid, "kind": "video", "name": url, "url": url, "status": "waiting",
        "downloaded": 0, "size": 0, "speed": 0, "dir": directory or dir_for("video"),
        "error": None, "site": site_name(url),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "file": None, "pid": None, "files": [],
    }
    with LOCK:
        VIDEO_TASKS[tid] = task
    threading.Thread(target=_video_worker, args=(task,), daemon=True).start()
    return task


# --------------------------------------------------------------------------- 系统集成：通知 / 剪贴板 / 自启


def notify_done(name: str) -> None:
    if CONFIG.get("done_sound"):
        try:
            import winsound
            sound = APP_DIR / "done.wav"
            if sound.exists():
                winsound.PlaySound(str(sound), winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass
    if not CONFIG.get("notify"):
        return
    try:
        safe = name.replace("'", "''")[:60]
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType=WindowsRuntime] > $null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('下载完成')) > $null;"
            f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode('{safe}')) > $null;"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            "'闪电全能下载').Show([Windows.UI.Notifications.ToastNotification]::new($t));"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def set_launch_at_login(enabled: bool) -> None:
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        exe = sys.executable if getattr(sys, "frozen", False) else str(Path(__file__).resolve())
        cmd = f'"{exe}"'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, "ShandianDownload", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, "ShandianDownload")
                except FileNotFoundError:
                    pass
    except Exception as exc:
        print(f"[系统] 设置开机启动失败：{exc}")


def register_protocol() -> None:
    """注册 shandian:// 协议，外部程序可直接投递下载任务。"""
    try:
        import winreg
        exe = sys.executable if getattr(sys, "frozen", False) else str(Path(__file__).resolve())
        base = r"Software\Classes\shandian"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Shandian Download Protocol")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" "%1"')
    except Exception as exc:
        print(f"[系统] 注册协议失败：{exc}")


# 剪贴板（Win32）

CF_UNICODETEXT = 13


def clipboard_text() -> str:
    if sys.platform != "win32":
        return ""
    try:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        if not u32.OpenClipboard(None):
            return ""
        try:
            if not u32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            handle = u32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = k32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                k32.GlobalUnlock(handle)
        finally:
            u32.CloseClipboard()
    except Exception:
        return ""


CLIP_STATE = {"last": "", "pending": None}


def clipboard_watcher() -> None:
    while True:
        time.sleep(1.2)
        if not CONFIG.get("watch_clipboard"):
            continue
        try:
            text = clipboard_text().strip()
        except Exception:
            continue
        if not text or text == CLIP_STATE["last"]:
            continue
        CLIP_STATE["last"] = text
        if len(text) > 8192:
            continue
        links = [l for l in pasted_links(text) if worth_popup(l)]
        if not links:
            continue
        with LOCK:
            CLIP_STATE["pending"] = links[:5]
        print(f"[剪贴板] 检测到 {len(links)} 个可下载链接")


# --------------------------------------------------------------------------- 定时下载


SCHEDULE_STATE = {"paused_by_schedule": set(), "active": False}


def schedule_worker() -> None:
    while True:
        time.sleep(30)
        if not CONFIG.get("schedule_on") or not ENGINE.alive:
            continue
        hour = time.localtime().tm_hour
        start = int(CONFIG.get("schedule_from") or 0)
        end = int(CONFIG.get("schedule_to") or 0)
        if start == end:
            inside = True
        elif start < end:
            inside = start <= hour < end
        else:
            inside = hour >= start or hour < end
        try:
            tasks = ENGINE.tasks()
        except Exception:
            continue
        if not inside:
            for t in tasks:
                if (t.get("status") or "").lower() == "running":
                    try:
                        ENGINE.pause(str(t["id"]))
                        SCHEDULE_STATE["paused_by_schedule"].add(str(t["id"]))
                    except Exception:
                        pass
            SCHEDULE_STATE["active"] = True
        elif SCHEDULE_STATE["paused_by_schedule"]:
            for tid in list(SCHEDULE_STATE["paused_by_schedule"]):
                try:
                    ENGINE.resume(tid)
                except Exception:
                    pass
                SCHEDULE_STATE["paused_by_schedule"].discard(tid)
            SCHEDULE_STATE["active"] = False


# --------------------------------------------------------------------------- 统一任务操作


def _submit_engine_task(pid: str, url: str, directory: str, headers: dict | None,
                        name: str | None, select_files: list[int] | None) -> None:
    try:
        ENGINE.create(url, directory, headers=headers, name=name, select_files=select_files)
        with LOCK:
            PENDING.pop(pid, None)
    except Exception as exc:
        with LOCK:
            t = PENDING.get(pid)
            if t:
                t["status"] = "error"
                t["error"] = str(exc)
        print(f"[引擎] 提交失败：{exc}")


def add_task(url: str, directory: str | None = None, headers: dict | None = None,
             name: str | None = None, select_files: list[int] | None = None) -> dict:
    url = url.strip()
    if not url:
        raise ValueError("链接为空")
    err = validate_magnet(url)
    if err:
        raise ValueError(err)

    kind = link_kind(url) or "http"
    if is_wechat_share(url):
        raise ValueError("微信视频号链接需要配合浏览器扩展使用：在扩展里点「下载这个视频」")
    if baidu_share_url(url):
        raise ValueError("百度网盘分享链接需要在浏览器扩展里选「用闪电下载」，或先安装 bdpan 后重试")

    if kind == "video":
        return add_video(url, directory or dir_for("video"))

    directory = directory or dir_for(kind)
    if not ENGINE.alive and not ENGINE.start():
        raise EngineError("下载内核未运行，无法添加该任务")

    pid = "p-" + uuid.uuid4().hex[:10]
    task = {
        "id": pid, "kind": "engine", "name": name or url, "status": "waiting",
        "downloaded": 0, "size": 0, "speed": 0, "dir": directory, "error": None,
        "site": None, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": [],
    }
    with LOCK:
        PENDING[pid] = task
    threading.Thread(target=_submit_engine_task,
                     args=(pid, url, directory, headers, name, select_files), daemon=True).start()
    return dict(task)


def snapshot() -> dict:
    tasks: list[dict] = []
    if ENGINE.alive:
        try:
            tasks = [engine_task_view(t) for t in ENGINE.tasks()]
        except Exception as exc:
            print(f"[引擎] 拉取任务失败：{exc}")
    with LOCK:
        tasks += [dict(t) for t in PENDING.values()]
        tasks += [dict(t) for t in VIDEO_TASKS.values()]
    tasks.sort(key=lambda t: t.get("created") or "", reverse=True)
    for t in tasks:
        t.pop("pid", None)
    with LOCK:
        pending_clip = CLIP_STATE["pending"]
    return {
        "engine": {"alive": ENGINE.alive, "version": ENGINE.version, "port": ENGINE.port},
        "tools": {
            "gopeed": tool_path(GOPEED_NAME) is not None,
            "ytdlp": tool_path(YTDLP_NAME) is not None,
            "ffmpeg": tool_path(FFMPEG_NAME) is not None,
            "node": bool(find_node()),
        },
        "config": {k: CONFIG.get(k) for k in (
            "download_dir", "dir_movie", "dir_video", "dir_other", "remember_dir",
            "max_running", "limit_kb", "concurrency", "cookies_from_browser",
            "language", "notify", "done_sound", "watch_clipboard", "confirm_new",
            "launch_at_login", "prevent_sleep", "schedule_on", "schedule_from", "schedule_to",
            "auto_update_check",
        )},
        "gateway": {"port": GATEWAY_PORT, "running": GATEWAY_STATE["running"]},
        "engine_token": ENGINE.token,
        "clipboard": pending_clip,
        "recycle": CONFIG.get("recycle") or [],
        "version": APP_VERSION,
        "tasks": tasks,
    }


def do_action(tid: str, action: str) -> None:
    if tid.startswith("p-"):
        with LOCK:
            PENDING.pop(tid, None)
        return
    if tid.startswith("v-"):
        pid = None
        with LOCK:
            task = VIDEO_TASKS.get(tid)
            if not task:
                raise KeyError("任务不存在")
            if action == "delete":
                pid = task.get("pid")
                VIDEO_TASKS.pop(tid, None)
            elif action in ("pause", "resume"):
                raise EngineError("视频任务暂不支持暂停/继续，可删除后重下")
        if action == "delete" and pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        return

    if not ENGINE.alive:
        raise EngineError("下载内核未运行")
    if action == "pause":
        ENGINE.pause(tid)
    elif action == "resume":
        ENGINE.resume(tid)
    elif action == "delete":
        ENGINE.delete(tid, with_files=False)
    elif action == "delete-file":
        ENGINE.delete(tid, with_files=True)
    elif action == "restore":
        pass
    else:
        raise ValueError(f"未知操作 {action}")


def open_path(tid: str) -> None:
    target = CONFIG["download_dir"]
    if tid:
        if tid.startswith("v-"):
            with LOCK:
                t = VIDEO_TASKS.get(tid)
            if t:
                target = str(Path(t["file"]).parent) if t.get("file") else t["dir"]
        elif ENGINE.alive:
            try:
                for t in ENGINE.tasks():
                    if str(t.get("id")) == tid:
                        target = (t.get("meta") or {}).get("opts", {}).get("path") or target
                        break
            except Exception:
                pass
    p = Path(target)
    if not p.exists():
        p = Path(CONFIG["download_dir"])
    p.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(p))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


def resolve_files(url: str) -> dict:
    """磁力 / 种子先解析出文件列表，供选择下载哪些。"""
    if not ENGINE.alive and not ENGINE.start():
        raise EngineError("下载内核未运行")
    data = ENGINE.resolve(url)
    res = data.get("res") or {}
    files = [{"index": i, "name": f.get("name") or "", "size": int(f.get("size") or 0)}
             for i, f in enumerate(res.get("files") or [])]
    return {"name": res.get("name") or "", "size": int(res.get("size") or 0), "files": files}


def export_diagnostics() -> Path:
    """诊断包：不含路径、片名、链接、令牌。"""
    out = DATA_DIR / f"diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    lines = [f"版本: {APP_VERSION}", f"平台: {sys.platform} {sys.version.split()[0]}",
             f"引擎: {'运行中 v' + str(ENGINE.version) if ENGINE.alive else '未运行'}",
             f"工具: gopeed={tool_path(GOPEED_NAME) is not None} ytdlp={tool_path(YTDLP_NAME) is not None} "
             f"ffmpeg={tool_path(FFMPEG_NAME) is not None} node={bool(find_node())}",
             f"并发: {CONFIG.get('max_running')} | 限速: {CONFIG.get('limit_kb')} KB/s",
             f"任务数: {len(snapshot()['tasks'])}"]
    try:
        elog = DATA_DIR / "engine.log"
        if elog.exists():
            tail = elog.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
            lines += ["", "--- engine.log 尾部 ---"] + tail
    except Exception:
        pass
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def check_update() -> dict:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            "https://api.github.com/repos/zczy-k/shandian-download/releases/tags/win-latest",
            headers={"User-Agent": f"Shandian-Win/{APP_VERSION}"})
        with opener.open(req, timeout=12) as resp:
            data = json.loads(resp.read())
        asset = next((a for a in data.get("assets", []) if a["name"].startswith("Shandian-Windows-Setup")), None)
        return {"ok": True, "latest": APP_VERSION,
                "url": asset["browser_download_url"] if asset else "",
                "published": data.get("published_at", "")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- 网关 6800（浏览器扩展 / aria2 / Gopeed 代理）


GATEWAY_STATE = {"running": False}

EXTENSION_ORIGIN = "chrome-extension://pggchniffhkcgigajlapkboikmgdipel"
EDGE_STORE_ORIGIN = "chrome-extension://onnpjcddpnfmaahjjdcibcofebfgopei"
CHROME_STORE_ORIGIN = "chrome-extension://achpnnoffkoeomkmofbcjpnmifaeanjg"
STORE_ORIGINS = {EDGE_STORE_ORIGIN, CHROME_STORE_ORIGIN}
ALLOWED_ORIGINS = STORE_ORIGINS | {EXTENSION_ORIGIN}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
}


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in CORS_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_OPTIONS(self) -> None:
        self._send(200, b"")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/v1/"):
            self._proxy("GET")
        elif path == "/extension/ping":
            self._send(200, _json_bytes({"ok": True, "version": APP_VERSION, "ready": ENGINE.alive}))
        else:
            self._send(404, _json_bytes({"ok": False}))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path.startswith("/jsonrpc"):
                self._send(200, self._aria2(self._body()))
            elif path.startswith("/extension/"):
                self._extension(path, self._body())
            elif path.startswith("/api/v1/"):
                self._proxy("POST")
            else:
                self._send(404, _json_bytes({"ok": False}))
        except Exception as exc:
            print(f"[网关] {path} 失败：{exc}")
            self._send(400, _json_bytes({"ok": False, "error": str(exc)}))

    def do_PUT(self) -> None:
        if urlparse(self.path).path.startswith("/api/v1/"):
            self._proxy("PUT")
        else:
            self._send(404, _json_bytes({"ok": False}))

    def do_DELETE(self) -> None:
        if urlparse(self.path).path.startswith("/api/v1/"):
            self._proxy("DELETE")
        else:
            self._send(404, _json_bytes({"ok": False}))

    # ---- aria2 RPC（LinkSwift 推网盘直链）

    def _aria2(self, raw: bytes) -> bytes:
        try:
            obj = json.loads(raw or b"{}")
        except Exception:
            return _json_bytes({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}})
        if isinstance(obj, list):
            return _json_bytes([self._aria2_call(c) for c in obj])
        return _json_bytes(self._aria2_call(obj))

    def _aria2_call(self, call: dict) -> dict:
        cid = call.get("id")
        params = list(call.get("params") or [])

        def fail(code: int, msg: str) -> dict:
            return {"jsonrpc": "2.0", "id": cid, "error": {"code": code, "message": msg}}

        if not params or not isinstance(params[0], str) or not params[0].startswith("token:"):
            return fail(1, "Unauthorized")
        if params[0][6:] != ENGINE.token:
            return fail(1, "Unauthorized")
        params.pop(0)

        method = call.get("method") or ""
        if method == "aria2.getVersion":
            return {"jsonrpc": "2.0", "id": cid,
                    "result": {"version": "1.37.0", "enabledFeatures": ["Shandian"]}}
        if method == "aria2.addUri":
            uris = params[0] if params and isinstance(params[0], list) else []
            opts = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
            if not uris:
                return fail(1, "No URI")
            headers: dict[str, str] = {}
            for line in opts.get("header") or []:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip()] = v.strip()
            try:
                add_task(str(uris[0]), headers=headers, name=opts.get("out"))
            except Exception as exc:
                return fail(1, str(exc))
            return {"jsonrpc": "2.0", "id": cid, "result": f"{uuid.uuid4().int & 0xFFFFFFFFFFFFFFFF:016x}"}
        if method in ("aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"):
            return {"jsonrpc": "2.0", "id": cid, "result": []}
        return fail(1, "Method not supported")

    # ---- 自带浏览器扩展

    def _extension(self, path: str, raw: bytes) -> None:
        origin = self.headers.get("Origin")
        if origin not in ALLOWED_ORIGINS:
            self._send(403, _json_bytes({"ok": False, "error": "origin not allowed"}))
            return
        if origin in STORE_ORIGINS and path not in ("/extension/ping", "/extension/add"):
            self._send(403, _json_bytes({"ok": False}))
            return
        try:
            obj = json.loads(raw or b"{}")
        except Exception:
            obj = {}

        if path == "/extension/ping":
            self._send(200, _json_bytes({"ok": True, "version": APP_VERSION, "ready": ENGINE.alive}))
            return

        if path == "/extension/known":
            links = [l for l in (obj.get("urls") or []) if isinstance(l, str)][:100]
            known = [l for l in links if any(t.get("name") == l for t in snapshot()["tasks"])]
            self._send(200, _json_bytes({"ok": True, "known": known}))
            return

        if path == "/extension/batch":
            links = [l for l in (obj.get("urls") or []) if isinstance(l, str) and link_kind(l)][:100]
            if not links:
                self._send(400, _json_bytes({"ok": False}))
                return
            referer = obj.get("referrer")
            headers = {"Referer": referer} if referer and _host(str(referer)) else {}
            ok = 0
            for l in links:
                try:
                    add_task(l, headers=headers)
                    ok += 1
                except Exception:
                    pass
            self._send(200, _json_bytes({"ok": ok > 0, "added": ok}))
            return

        url = obj.get("url")
        if not isinstance(url, str) or not link_kind(url):
            self._send(400, _json_bytes({"ok": False, "error": "链接不支持"}))
            return

        if path == "/extension/status":
            tid = None
            for t in snapshot()["tasks"]:
                if t.get("name") == url:
                    tid = t.get("id")
                    break
            state = "none"
            if tid and ENGINE.alive:
                for t in ENGINE.tasks():
                    if str(t.get("id")) == tid:
                        state = "failed" if (t.get("status") or "").lower() == "error" else "ok"
                        break
            self._send(200, _json_bytes({"ok": True, "state": state}))
            return

        headers = obj.get("headers") if isinstance(obj.get("headers"), dict) else {}
        name = obj.get("name") if isinstance(obj.get("name"), str) else None
        try:
            add_task(url, headers=headers, name=name)
            self._send(200, _json_bytes({"ok": True}))
        except Exception as exc:
            self._send(200, _json_bytes({"ok": False, "error": str(exc)}))

    # ---- Gopeed 扩展代理

    def _proxy(self, method: str) -> None:
        if self.headers.get("X-Api-Token") != ENGINE.token:
            self._send(401, _json_bytes({"code": 1, "msg": "unauthorized"}))
            return
        body = self._body() if method in ("POST", "PUT", "PATCH") else None
        status, data = ENGINE.forward(method, self.path, body)
        self._send(status, data or b"{}")


def start_gateway() -> None:
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), GatewayHandler)
    except OSError as exc:
        print(f"[网关] 6800 绑定失败：{exc}")
        return
    GATEWAY_STATE["running"] = True
    print(f"[网关] 浏览器扩展接口 127.0.0.1:{GATEWAY_PORT}")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


# --------------------------------------------------------------------------- 界面 API


class Handler(BaseHTTPRequestHandler):
    server_version = "ShandianWin/1.0"
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, _json_bytes(obj))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._static("index.html")
            elif path == "/api/state":
                self._json(snapshot())
            elif path == "/api/ping":
                self._json({"ok": True, "app": "shandian-win"})
            elif path == "/api/update":
                self._json(check_update())
            elif path.startswith("/static/"):
                self._static(path[8:])
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            print(f"[HTTP] GET {self.path} 失败：{exc}")
            self._json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/add":
                task = add_task(body.get("url", ""), body.get("dir"),
                                headers=body.get("headers") if isinstance(body.get("headers"), dict) else None,
                                name=body.get("name"),
                                select_files=body.get("selectFiles") if isinstance(body.get("selectFiles"), list) else None)
                self._json({"ok": True, "task": task})
            elif path == "/api/add-many":
                urls = body.get("urls") or []
                added = 0
                for u in urls:
                    try:
                        add_task(str(u))
                        added += 1
                    except Exception:
                        pass
                self._json({"ok": True, "added": added})
            elif path == "/api/action":
                do_action(str(body.get("id", "")), str(body.get("action", "")))
                self._json({"ok": True})
            elif path == "/api/open":
                open_path(str(body.get("id", "")))
                self._json({"ok": True})
            elif path == "/api/resolve":
                self._json({"ok": True, **resolve_files(str(body.get("url", "")))})
            elif path == "/api/clipboard":
                with LOCK:
                    CLIP_STATE["pending"] = None
                self._json({"ok": True})
            elif path == "/api/settings":
                for key in ("download_dir", "dir_movie", "dir_video", "dir_other",
                            "cookies_from_browser", "language"):
                    if key in body and isinstance(body[key], str):
                        CONFIG[key] = body[key].strip()
                for key in ("max_running", "limit_kb", "concurrency", "schedule_from", "schedule_to"):
                    if key in body:
                        CONFIG[key] = max(0, int(body[key]))
                for key in ("remember_dir", "notify", "done_sound", "watch_clipboard",
                            "confirm_new", "prevent_sleep", "schedule_on", "auto_update_check"):
                    if key in body:
                        CONFIG[key] = bool(body[key])
                if "launch_at_login" in body:
                    CONFIG["launch_at_login"] = bool(body["launch_at_login"])
                    set_launch_at_login(CONFIG["launch_at_login"])
                save_config()
                if ENGINE.alive:
                    ENGINE.apply_settings()
                self._json({"ok": True, "config": snapshot()["config"]})
            elif path == "/api/install-ffmpeg":
                threading.Thread(target=ensure_tools, kwargs={"need_ffmpeg": True}, daemon=True).start()
                self._json({"ok": True})
            elif path == "/api/diagnostics":
                p = export_diagnostics()
                self._json({"ok": True, "path": str(p)})
            elif path == "/api/recycle/clear":
                CONFIG["recycle"] = []
                save_config()
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            print(f"[HTTP] POST {path} 失败：{exc}")
            self._json({"ok": False, "error": str(exc)}, 400)

    def _static(self, rel: str) -> None:
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png"}.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


def _is_shandian(port: int) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/ping", timeout=0.4) as r:
            return json.loads(r.read() or b"{}").get("app") == "shandian-win"
    except Exception:
        return False


def serve(preferred: int = UI_PORT) -> None:
    for candidate in range(preferred, preferred + 30):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
        except OSError:
            if _is_shandian(candidate):
                ENGINE.stop()
                url = f"http://127.0.0.1:{candidate}/"
                print(f"[界面] 已有实例在运行，打开 {url}")
                webbrowser.open(url)
                return
            continue
        url = f"http://127.0.0.1:{candidate}/"
        print(f"[界面] {url}")
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
        return
    print("[界面] 8765-8794 都被占用，未启动界面")


# --------------------------------------------------------------------------- 命令行 / 协议投递


def handle_argv() -> bool:
    """支持 shandian://add?url=… 与直接拖入链接。"""
    args = [a for a in sys.argv[1:] if a]
    if not args:
        return False
    urls: list[str] = []
    for a in args:
        if a.lower().startswith("shandian://"):
            q = urlparse(a).query
            for part in q.split("&"):
                if part.lower().startswith("url="):
                    urls.append(urllib.parse.unquote(part[4:]))
        elif link_kind(a):
            urls.append(a)
    if not urls:
        return False
    CONFIG.update(load_config())
    if not ENGINE.start():
        print("[投递] 内核未就绪")
        return True
    for u in urls:
        try:
            add_task(u)
            print(f"[投递] 已添加 {u[:60]}")
        except Exception as exc:
            print(f"[投递] 失败：{exc}")
    return True


def main() -> None:
    global CONFIG
    USER_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        log_file = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout = log_file
        sys.stderr = log_file
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    CONFIG = load_config()

    # 内核 token 持久化：LinkSwift / Gopeed 扩展代理都要用它，重启后不能变
    if not CONFIG.get("engine_token"):
        CONFIG["engine_token"] = uuid.uuid4().hex
        save_config()
    ENGINE.token = CONFIG["engine_token"]

    if handle_argv():
        time.sleep(2)
        return

    print("=" * 58)
    print(f" 闪电全能下载 · Windows v{APP_VERSION}")
    print("=" * 58)

    ensure_tools(need_ffmpeg=False)
    ENGINE.start()
    atexit.register(ENGINE.stop)

    start_gateway()
    register_protocol()
    if CONFIG.get("launch_at_login"):
        set_launch_at_login(True)
    threading.Thread(target=clipboard_watcher, daemon=True).start()
    threading.Thread(target=schedule_worker, daemon=True).start()

    if CONFIG.get("prevent_sleep"):
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        except Exception:
            pass

    serve(UI_PORT)


if __name__ == "__main__":
    main()
