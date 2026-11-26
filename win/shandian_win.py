#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闪电全能下载 · Windows 客户端

macOS 原版是 SwiftUI 壳 + Gopeed 内核 + yt-dlp。
本文件只重写壳：内核与 yt-dlp 都是跨平台二进制，直接复用。
引擎通信走 Gopeed 官方 REST API（http://127.0.0.1:<port>/api/v1/），
与 macOS 版 Engine.swift 的调用完全一致，因此两端的任务库格式互通。

零第三方依赖，只用 Python 3.10+ 标准库。
"""

from __future__ import annotations

import atexit
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

def _app_dir() -> Path:
    """可写目录：安装目录（开发时是脚本目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _res_dir() -> Path:
    """只读资源目录：PyInstaller 打包后是 _MEIPASS。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()
RES_DIR = _res_dir()

# 可写数据（配置、任务库、按需下载的 ffmpeg）放用户目录，安装到 Program Files 时也不会写失败
USER_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ShandianDownload"
TOOLS_DIR = USER_DIR / "tools"
DATA_DIR = USER_DIR / "data"
CONFIG_FILE = USER_DIR / "config.json"
WEB_DIR = RES_DIR / "web"

# 安装包自带的二进制（只读）
BUNDLED_TOOLS = APP_DIR / "tools"

GOPEED_NAME = "gopeed.exe"
YTDLP_NAME = "yt-dlp.exe"
FFMPEG_NAME = "ffmpeg.exe"


def tool_path(name: str) -> Path | None:
    for p in (TOOLS_DIR / name, BUNDLED_TOOLS / name):
        if p.exists():
            return p
    return None

ENGINE_VERSION = "1.9.3"
# 官方 release 的 Windows 包是 GUI 版，不接受 -A/-P/-T 无头参数；
# 无头内核由本仓库的 GitHub Actions 用补丁源码编译，随 win-latest 一起发布
GOPEED_URLS = [
    "https://github.com/zczy-k/shandian-download/releases/download/win-latest/gopeed.exe",
]
YTDLP_URLS = [
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
    "https://ghfast.top/https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
]
FFMPEG_URLS = [
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "https://ghfast.top/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]

# 站点识别表，与 Sources/Shandian/VideoSite.swift:133 一致
SITES = [
    ("movie.douban.com", "豆瓣"),
    ("bilibili", "B 站"),
    ("b23.tv", "B 站"),
    ("youtu", "YouTube"),
    ("douyin", "抖音"),
    ("xiaohongshu", "小红书"),
    ("xhslink", "小红书"),
    ("kuaishou", "快手"),
    ("weibo", "微博"),
    ("ixigua", "西瓜视频"),
    ("vimeo", "Vimeo"),
    ("x.com", "X"),
    ("twitter", "X"),
]

DEFAULT_CONFIG = {
    "download_dir": str(Path.home() / "Downloads" / "Shandian"),
    "concurrency": 4,
    "cookies_from_browser": "",
    "engine_port": 0,
    "max_running": 3,
}

# BT tracker 源，与 macOS 版 Engine.swift:357 一致，另加两个镜像应对国内网络
TRACKER_SOURCES = [
    "https://cf.trackerslist.com/best.txt",
    "https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt",
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
    "https://ghfast.top/https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
]

BTIH_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[A-Za-z2-7]{32})$")

LOCK = threading.Lock()
VIDEO_TASKS: dict[str, dict] = {}
PENDING: dict[str, dict] = {}  # 已受理、内核还没返回任务号的占位卡片
CONFIG: dict = {}


# --------------------------------------------------------------------------- 配置


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    Path(cfg["download_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- 工具下载


def _download(urls: list[str], dest: Path, label: str) -> bool:
    for url in urls:
        try:
            print(f"[工具] 正在下载 {label} …")
            req = urllib.request.Request(url, headers={"User-Agent": "Shandian-Win/1.0"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=120) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                tmp = dest.with_suffix(dest.suffix + ".part")
                got = 0
                last = 0.0
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
    """从压缩包里取出文件名包含 key 的第一个 ext 文件。

    Gopeed 的 Windows 包内文件名是 Gopeed-v1.9.3-windows-amd64.exe，
    ffmpeg 的包内是 bin/ffmpeg.exe，都不是固定名，所以按关键字匹配。
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            target = None
            for info in zf.infolist():
                name = info.filename.lower()
                if name.endswith(ext) and key.lower() in name:
                    target = info
                    break
            if not target:
                print(f"[工具] 压缩包里没找到含 {key} 的 {ext}：{[i.filename for i in zf.infolist()]}")
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(target) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
        return True
    except Exception as exc:
        print(f"[工具] 解压失败：{exc}")
        return False


def ensure_tools(need_ffmpeg: bool = False) -> None:
    """补齐工具。安装包自带内核和 yt-dlp，缺的（通常是 ffmpeg）下到用户目录。"""
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
            ok1 = _unzip_one(z, "ffmpeg", TOOLS_DIR / FFMPEG_NAME)
            _unzip_one(z, "ffprobe", TOOLS_DIR / "ffprobe.exe")
            if ok1:
                print(f"[工具] ffmpeg 就绪：{TOOLS_DIR / FFMPEG_NAME}")
            z.unlink(missing_ok=True)


# --------------------------------------------------------------------------- 引擎


class EngineError(RuntimeError):
    pass


def fetch_trackers() -> list[str]:
    """拉公开 tracker 列表。冷门磁力靠 tracker 找 peer，没有基本下不动。"""
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
    """托管 Gopeed headless，并按其 REST API 通信。

    启动参数与 macOS 版 Engine.swift:242 相同：
        gopeed -A 127.0.0.1 -P <port> -T <token> -d <data> -c <server.json>
    """

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
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            engine_data = DATA_DIR / "engine"
            engine_data.mkdir(parents=True, exist_ok=True)
            log_path = DATA_DIR / "engine.log"
            flags = 0
            if sys.platform == "win32":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                self.proc = subprocess.Popen(
                    [
                        str(gopeed),
                        "-A", "127.0.0.1",
                        "-P", str(port),
                        "-T", self.token,
                        "-d", str(engine_data),
                        "-c", str(engine_data / "server.json"),
                    ],
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

    # ---- API 封装（与 Engine.swift 一一对应）

    def tasks(self) -> list[dict]:
        return self._req("GET", "tasks") or []

    def create(self, url: str, directory: str) -> str:
        # 磁力 / 种子要先从网络拿元数据，官方接口在这种情况下会等较久
        tid = self._req("POST", "tasks", {"req": {"url": url}, "opts": {"path": directory}}, timeout=180)
        if not tid:
            raise EngineError("内核未返回任务编号")
        return str(tid)

    def pause(self, tid: str) -> None:
        self._req("PUT", f"tasks/{tid}/pause")

    def resume(self, tid: str) -> None:
        self._req("PUT", f"tasks/{tid}/continue")

    def delete(self, tid: str, with_files: bool = False) -> None:
        self._req("DELETE", f"tasks/{tid}?force={'true' if with_files else 'false'}")

    def resolve(self, url: str) -> dict:
        return self._req("POST", "resolve", {"req": {"url": url}}, timeout=300) or {}

    # ---- 配置（对应 macOS 版 Engine.swift:326 applySettings）

    def apply_settings(self) -> None:
        try:
            cfg = self._req("GET", "config") or {}
        except Exception as exc:
            print(f"[引擎] 读取配置失败：{exc}")
            return

        cfg["downloadDir"] = CONFIG["download_dir"]
        cfg["maxRunning"] = int(CONFIG.get("max_running") or 3)

        extra = cfg.get("extra") or {}
        extra["shandianDownloadLimit"] = 0
        cfg["extra"] = extra

        # 全局代理关掉：开着时 BT 也走代理，磁力连不上 peer
        cfg["proxy"] = {
            "enable": False, "system": False, "scheme": "http",
            "host": "127.0.0.1:7897", "usr": "", "pwd": "",
        }

        proto = cfg.get("protocolConfig") or {}
        http = proto.get("http") or {}
        http["connections"] = 8
        http["userAgent"] = "ShandianDownload/0.22.6 (Windows)"
        proto["http"] = http

        bt = proto.get("bt") or {}
        bt["listenPort"] = 6881  # 固定端口，别的节点才连得进来
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
    status = {
        "ready": "waiting",
        "waiting": "waiting",
        "running": "downloading",
        "pause": "paused",
        "paused": "paused",
        "done": "done",
        "error": "error",
    }.get(status_raw, status_raw or "waiting")
    name = res.get("name") or t.get("name") or ""
    if not name:
        req_url = (meta.get("req") or {}).get("url") or ""
        name = req_url.rstrip("/").rsplit("/", 1)[-1][:80] or "未命名任务"
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
        "created": t.get("createdAt") or "",
    }


# --------------------------------------------------------------------------- 视频任务


def site_name(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    for key, label in SITES:
        if key in host:
            return label
    return None


def is_video_url(url: str) -> bool:
    return site_name(url) is not None


def find_node() -> str | None:
    for cand in ("node.exe", "node"):
        p = shutil.which(cand)
        if p:
            return p
    for base in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe",
    ):
        if base.exists():
            return str(base)
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
        str(ytdlp),
        "-P", str(outdir),
        "-o", "%(title).80s [%(id)s].%(ext)s",
        "--no-playlist",
        "--newline",
        "--no-colors",
        "--progress",
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
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
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
                def _num(s: str) -> int:
                    try:
                        return int(float(s))
                    except Exception:
                        return 0
                done = _num(parts[0])
                total = _num(parts[1]) or _num(parts[2])
                speed = _num(parts[3])
                with LOCK:
                    task["downloaded"] = done
                    if total:
                        task["size"] = total
                    task["speed"] = speed
        elif line.startswith("SDFILE "):
            with LOCK:
                task["file"] = line[7:].strip()
                task["downloaded"] = task["size"] or task["downloaded"]
        elif line.startswith("ERROR:"):
            last_error = line[6:].strip()
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


def add_video(url: str, directory: str | None = None) -> dict:
    tid = "v-" + uuid.uuid4().hex[:12]
    task = {
        "id": tid,
        "kind": "video",
        "name": url,
        "url": url,
        "status": "waiting",
        "downloaded": 0,
        "size": 0,
        "speed": 0,
        "dir": directory or CONFIG["download_dir"],
        "error": None,
        "site": site_name(url),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "file": None,
        "pid": None,
    }
    with LOCK:
        VIDEO_TASKS[tid] = task
    threading.Thread(target=_video_worker, args=(task,), daemon=True).start()
    return task


# --------------------------------------------------------------------------- 统一操作


def validate_magnet(url: str) -> str | None:
    """磁力链接格式校验。返回错误文案，没问题返回 None。

    复制不完整时 btih 会少字符（常见 39 位），内核只会回一句 unhandled xt parameter，
    用户看到的是「加了任务但没反应」，所以在这里拦下来。
    """
    if not url.lower().startswith("magnet:"):
        return None
    q = url.split("?", 1)[1] if "?" in url else ""
    xts = [part[3:] for part in q.split("&") if part.lower().startswith("xt=")]
    if not xts:
        return "磁力链接缺少 xt 参数，可能复制不完整"
    for xt in xts:
        xt = urllib.parse.unquote(xt)
        if xt.lower().startswith("urn:btih:"):
            h = xt[9:]
            if BTIH_RE.match(h):
                return None
            return (
                f"磁力链接的 btih 是 {len(h)} 位，标准应为 40 位十六进制或 32 位 base32。"
                "链接多半是复制时漏了字符，请重新完整复制"
            )
    return "磁力链接缺少 urn:btih 标识"


def _submit_engine_task(pid: str, url: str, directory: str) -> None:
    """后台提交给内核。磁力要先从网络拿种子信息，可能几十秒，不能让 HTTP 请求等。"""
    try:
        ENGINE.create(url, directory)
        with LOCK:
            PENDING.pop(pid, None)  # 任务已进内核列表，占位卡片撤掉
    except Exception as exc:
        with LOCK:
            t = PENDING.get(pid)
            if t:
                t["status"] = "error"
                t["error"] = str(exc)
        print(f"[引擎] 提交失败：{exc}")


def add_task(url: str, directory: str | None = None) -> dict:
    url = url.strip()
    if not url:
        raise ValueError("链接为空")
    magnet_error = validate_magnet(url)
    if magnet_error:
        raise ValueError(magnet_error)
    directory = directory or CONFIG["download_dir"]
    if is_video_url(url):
        return add_video(url, directory)
    if not ENGINE.alive and not ENGINE.start():
        raise EngineError("下载内核未运行，无法添加该任务")

    pid = "p-" + uuid.uuid4().hex[:10]
    task = {
        "id": pid,
        "kind": "engine",
        "name": url,
        "status": "waiting",
        "downloaded": 0,
        "size": 0,
        "speed": 0,
        "dir": directory,
        "error": None,
        "site": None,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with LOCK:
        PENDING[pid] = task
    threading.Thread(target=_submit_engine_task, args=(pid, url, directory), daemon=True).start()
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
    return {
        "engine": {"alive": ENGINE.alive, "version": ENGINE.version, "port": ENGINE.port},
        "tools": {
            "gopeed": tool_path(GOPEED_NAME) is not None,
            "ytdlp": tool_path(YTDLP_NAME) is not None,
            "ffmpeg": tool_path(FFMPEG_NAME) is not None,
            "node": bool(find_node()),
        },
        "download_dir": CONFIG["download_dir"],
        "concurrency": CONFIG.get("concurrency", 4),
        "cookies_from_browser": CONFIG.get("cookies_from_browser", ""),
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
        if action == "delete" and pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        elif action in ("pause", "resume"):
            raise EngineError("视频任务暂不支持暂停/继续，可删除后重下")
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
    else:
        raise ValueError(f"未知操作 {action}")


def open_path(tid: str) -> None:
    target = CONFIG["download_dir"]
    if tid:
        if tid.startswith("v-"):
            with LOCK:
                t = VIDEO_TASKS.get(tid)
            if t:
                target = t.get("file") and str(Path(t["file"]).parent) or t["dir"]
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


# --------------------------------------------------------------------------- HTTP 服务


class Handler(BaseHTTPRequestHandler):
    server_version = "ShandianWin/1.0"
    protocol_version = "HTTP/1.1"  # 浏览器复用连接，减少每次点按都重新握手
    timeout = 60                   # 空闲连接 60 秒回收，避免线程堆积

    def log_message(self, fmt, *args):  # 静音默认日志
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
            pass  # 浏览器提前关页面，不算错误

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

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
                task = add_task(body.get("url", ""), body.get("dir"))
                self._json({"ok": True, "task": task})
            elif path == "/api/action":
                do_action(str(body.get("id", "")), str(body.get("action", "")))
                self._json({"ok": True})
            elif path == "/api/open":
                open_path(str(body.get("id", "")))
                self._json({"ok": True})
            elif path == "/api/settings":
                if "download_dir" in body:
                    d = str(body["download_dir"]).strip()
                    if d:
                        Path(d).mkdir(parents=True, exist_ok=True)
                        CONFIG["download_dir"] = d
                if "concurrency" in body:
                    CONFIG["concurrency"] = max(1, min(16, int(body["concurrency"])))
                if "cookies_from_browser" in body:
                    CONFIG["cookies_from_browser"] = str(body["cookies_from_browser"]).strip()
                save_config(CONFIG)
                self._json({"ok": True, "config": CONFIG})
            elif path == "/api/install-ffmpeg":
                threading.Thread(target=ensure_tools, kwargs={"need_ffmpeg": True}, daemon=True).start()
                self._json({"ok": True, "msg": "开始下载 ffmpeg，完成前视频合并会失败"})
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
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


def _is_shandian(port: int) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/ping", timeout=0.4) as r:
            return json.loads(r.read() or b"{}").get("app") == "shandian-win"
    except Exception:
        return False


def serve(preferred: int = 8765) -> None:
    for candidate in range(preferred, preferred + 30):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
        except OSError:
            # 端口被占：如果是自己已经开着的实例，直接把浏览器指过去，不再起第二个内核
            if _is_shandian(candidate):
                ENGINE.stop()  # 本进程刚拉起的多余内核退掉
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


def main() -> None:
    global CONFIG
    USER_DIR.mkdir(parents=True, exist_ok=True)
    # 打包成无控制台程序后 sys.stdout 是 None，print 会抛异常，统一重定向到日志
    if sys.stdout is None or sys.stderr is None:
        log_file = open(USER_DIR / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = log_file
        sys.stderr = log_file
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 58)
    print(" 闪电全能下载 · Windows")
    print("=" * 58)

    CONFIG = load_config()
    ensure_tools(need_ffmpeg=False)
    ENGINE.start()
    atexit.register(ENGINE.stop)
    serve(8765)


if __name__ == "__main__":
    main()
