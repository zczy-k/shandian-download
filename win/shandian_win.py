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
import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import re
import shutil
import signal
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

APP_VERSION = "0.23.3"
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
BARE_HASH_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[A-Za-z2-7]{32})$")
BTIH_IN_URL_RE = re.compile(r"btih:([0-9a-zA-Z]{32,40})", re.IGNORECASE)
TRACKER_CACHE: list[str] = []
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
    if scheme not in ("http", "https"):
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


def default_trackers() -> list[str]:
    """公共 tracker 列表，进程内只拉一次。"""
    global TRACKER_CACHE
    if not TRACKER_CACHE:
        try:
            TRACKER_CACHE = fetch_trackers()
        except Exception:
            TRACKER_CACHE = []
    return TRACKER_CACHE


def normalize_bt_input(raw: str) -> str:
    """裸 infohash 补成完整磁力；没有 tr= 的磁力补上公共 tracker。

    只给 DHT 的磁力在老种子上经常「有元数据、零 Peer」，
    补 tracker 能把这种情况的命中率拉起来（对齐 qBittorrent 的自动加 tracker 行为）。
    """
    s = raw.strip()
    if BARE_HASH_RE.match(s):
        s = "magnet:?xt=urn:btih:" + s
    if not s.lower().startswith("magnet:?"):
        return s
    low = s.lower()
    if "tr=" not in low:
        for tr in default_trackers()[:24]:
            s += "&tr=" + urllib.parse.quote(tr, safe="")
    return s


def infohash_of(url: str) -> str:
    m = BTIH_IN_URL_RE.search(url or "")
    return m.group(1).upper() if m else ""


def _bdecode(data: bytes, i: int = 0):
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            out[k] = v
        return out, i + 1
    if c.isdigit():
        j = data.index(b":", i)
        n = int(data[i:j])
        s = j + 1
        return data[s:s + n], s + n
    raise ValueError("ben-code 解析失败")


def torrent_meta(data: bytes) -> dict:
    """解析 .torrent：算 infohash、取名字/trackers/总大小。

    Gopeed 1.9.3 不认本地 .torrent 路径（提交成功但任务直接消失），
    所以这里自己算 infohash 转成磁力再交给内核。
    """
    idx = data.find(b"4:infod")
    if idx < 0:
        raise ValueError("不是有效的 .torrent 文件")
    start = idx + 6
    i, depth, n = start, 1, len(data)
    while i < n:
        c = data[i:i + 1]
        if c in (b"d", b"l"):
            depth += 1
            i += 1
        elif c == b"e":
            depth -= 1
            i += 1
            if depth == 0:
                break
        elif c == b"i":
            i = data.index(b"e", i) + 1
        elif c.isdigit():
            j = data.index(b":", i)
            i = j + 1 + int(data[i:j])
        else:
            i += 1
    raw = data[start:i - 1]
    ih = hashlib.sha1(raw).hexdigest()

    try:
        root, _ = _bdecode(data, 0)
    except Exception:
        root = {}
    info = root.get(b"info") or {}
    name = (info.get(b"name") or b"").decode("utf-8", "replace")
    trackers: list[str] = []
    ann = root.get(b"announce")
    if isinstance(ann, bytes) and ann:
        trackers.append(ann.decode("utf-8", "replace"))
    for tier in root.get(b"announce-list") or []:
        for tr in tier or []:
            if isinstance(tr, bytes):
                s = tr.decode("utf-8", "replace")
                if s and s not in trackers:
                    trackers.append(s)
    size = int(info.get(b"length") or 0)
    if not size:
        for f in info.get(b"files") or []:
            try:
                size += int(f.get(b"length") or 0)
            except Exception:
                pass
    return {"infohash": ih, "name": name, "trackers": trackers, "size": size}


def magnet_display_name(url: str) -> str:
    """从磁力链接取出可读任务名，取不到就用 btih 前 12 位。"""
    if not str(url).lower().startswith("magnet:?"):
        return ""
    try:
        qs = urllib.parse.parse_qs(urlparse(url).query)
    except Exception:
        qs = {}
    for key in ("dn", "xl"):
        val = (qs.get(key) or [""])[0].strip()
        if val:
            return val[:120]
    xt = (qs.get("xt") or [""])[0]
    if "btih:" in xt.lower():
        h = xt.split(":", 2)[-1].strip()
        return f"磁力任务 · {h[:12].upper()}"
    return "磁力任务"


def looks_like_infohash(name: str) -> bool:
    """内核在拿到元数据前会把任务名填成 infohash，这种名字要替换成人话。"""
    s = str(name or "").strip()
    return bool(re.fullmatch(r"[0-9A-Fa-f]{40}", s) or re.fullmatch(r"[A-Za-z2-7]{32}", s))


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
    "max_running": 8,
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
    "auto_resume": True,
    "bad_infohash": [],
    "user_paused": [],
    "name_hints": {},
    "recycle": [],
    "seen_version": "",
}

LOCK = threading.RLock()
VIDEO_TASKS: dict[str, dict] = {}
PENDING: dict[str, dict] = {}
PENDING_IH: dict[str, str] = {}   # pending id -> infohash，用于拦重复种子
CANCELLED: set[str] = set()   # 已被用户删除、但内核提交仍在飞行中的 pending id
LAST_TASKS: list[dict] = []   # 内核不可用时的兜底快照
LAST_SUBMIT: dict = {"url": "", "ts": 0.0}   # 最近一次提交给内核的 URL，用于崩溃归因
BAD_INFOHASH: set[str] = set()               # 会把内核对穿（panic）的 infohash，永不再提交
FROZEN_SAMPLES: dict[str, list] = {}         # tid -> [last_downloaded, last_ts, healed_ts] 僵死检测
HEAL_LOCK = threading.Lock()
USER_PAUSED: set[str] = set()                # 用户亲手点过暂停的任务，任何自愈都不许动
RESUME_TRIES: dict[str, float] = {}          # tid -> 上次自动接回时间，避免死循环
NAME_HINTS: dict[str, str] = {}              # infohash -> 真名，元数据没下来前先用它显示
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


def _save_user_paused() -> None:
    """把"用户亲手暂停"的任务 id 落盘，避免重启后被自愈逻辑偷偷继续下。"""
    CONFIG["user_paused"] = sorted(USER_PAUSED)
    save_config()


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


def _port_alive(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.25):
            return True
    except Exception:
        return False


def _port_owner_pids(port: int) -> list[int]:
    """谁占着这个本地端口。Windows 走 netstat，别的系统走 lsof。"""
    pids: list[int] = []
    port = int(port)
    try:
        if sys.platform == "win32":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                                 text=True, timeout=8).stdout or ""
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(f":{port}"):
                    try:
                        pid = int(parts[-1])
                    except ValueError:
                        continue
                    if pid and pid not in pids:
                        pids.append(pid)
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True,
                                 text=True, timeout=8).stdout or ""
            pids = [int(x) for x in out.split() if x.isdigit()]
    except Exception:
        return []
    return pids


def _pids_by_name(name: str) -> list[int]:
    """按映像名找 PID（Windows）。"""
    pids: list[int] = []
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/FI", f"IMAGENAME eq {name}"],
                             capture_output=True, timeout=10).stdout or b""
    except Exception:
        return []
    text = out.decode("gbk", "replace")
    for line in text.splitlines():
        parts = [x.strip().strip('"') for x in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def _proc_exe(pid: int) -> str:
    """某个 PID 对应的可执行文件全路径，拿不到就返回空串。"""
    if sys.platform != "win32":
        return ""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x0400 | 0x0010, False, int(pid))  # QUERY_INFORMATION | VM_READ
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.psapi.GetModuleFileNameExW(h, None, buf, 1024):
                return buf.value
            return ""
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def kill_stale_engine() -> int:
    """清掉上次残留的自家内核进程。

    两个 gopeed 同时打开一个 bbolt 任务库，会把库写坏、任务全部消失——
    这正是"关掉软件再打开，下载列表空了"的根因。
    """
    gopeed = tool_path(GOPEED_NAME)
    if not gopeed:
        return 0
    target = os.path.normcase(os.path.abspath(str(gopeed)))
    killed = 0
    for pid in _pids_by_name(Path(gopeed).name):
        if pid == os.getpid():
            continue
        exe = _proc_exe(pid)
        if not exe or os.path.normcase(os.path.abspath(exe)) != target:
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=8)
            killed += 1
        except Exception:
            pass
    if killed:
        print(f"[引擎] 清掉 {killed} 个上次残留的内核进程")
    return killed


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
            # 连不上 = 内核进程已退出（或端口被回收）；不能再对外声称 alive
            self.alive = False
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
            port = int(CONFIG.get("engine_port") or 0)
            if port and _port_alive(port) and self._adopt(port):
                # 上次退出没把内核收拾干净：直接接管它，绝不另起一个抢同一个 data 目录。
                # （两个 gopeed 同时打开一个 bbolt 文件会把任务库写坏，任务全消失）
                return True
            if not port:
                port = self._free_port()
            elif _port_alive(port):
                for pid in _port_owner_pids(port):
                    try:
                        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                       capture_output=True, timeout=8)
                    except Exception:
                        pass
                time.sleep(0.4)
                if _port_alive(port):
                    port = self._free_port()
            CONFIG["engine_port"] = port
            try:
                save_config()
            except Exception:
                pass
            engine_data = DATA_DIR / "engine"
            engine_data.mkdir(parents=True, exist_ok=True)
            log_path = DATA_DIR / "engine.log"
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            if sys.platform == "win32":
                kill_stale_engine()
                if _port_alive(port):
                    port = self._free_port()
                    CONFIG["engine_port"] = port
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

    def _adopt(self, port: int) -> bool:
        """端口上已经有一个内核（上次残留的）时，用 token 认一下，能通就直接用。"""
        old = self.port
        self.port = int(port)
        try:
            info = self._req("GET", "info", timeout=2)
        except Exception:
            self.port = old
            return False
        self.alive = True
        self.version = (info or {}).get("version")
        self.apply_settings()
        print(f"[引擎] 接管已在运行的内核 v{self.version} @ 127.0.0.1:{self.port}")
        return True

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
        # 注意：Gopeed 1.9.3 收到 opts.name 会直接崩溃退出，名字只在 UI 层展示，绝不传给内核
        opts: dict = {"path": directory}
        if select_files:
            opts["selectFiles"] = select_files
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

    def _status_of(self, tid: str) -> str:
        try:
            for t in self.tasks():
                if str(t.get("id")) == str(tid):
                    return (t.get("status") or "").lower()
        except Exception:
            pass
        return ""

    def pause(self, tid: str) -> None:
        try:
            self._req("PUT", f"tasks/{tid}/pause")
        except EngineError as exc:
            # Gopeed 1.9.3 偶发在状态机上返回 task not found，实际已暂停，复查一次再决定
            if "task not found" in str(exc) and self._status_of(tid) in ("pause", "paused", "done"):
                return
            raise

    def resume(self, tid: str) -> None:
        try:
            self._req("PUT", f"tasks/{tid}/continue")
        except EngineError as exc:
            if "task not found" in str(exc) and self._status_of(tid) in ("running", "wait", "ready", "done"):
                return
            raise

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
        cfg["maxRunning"] = int(CONFIG.get("max_running") or 8)
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
            global TRACKER_CACHE
            TRACKER_CACHE = trackers
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


DISK_CACHE: dict[str, tuple[float, int]] = {}
SPEED_SAMPLES: dict[str, tuple[float, int]] = {}
PROGRESS_STATE: dict[str, dict] = {}
PROGRESS_LOCK = threading.Lock()


def dir_bytes(path: str) -> int:
    """统计目录实际占用（4 秒缓存），用来兜底内核不上报的进度。"""
    now = time.time()
    with PROGRESS_LOCK:
        hit = DISK_CACHE.get(path)
        if hit and now - hit[0] < 4:
            return hit[1]
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        total = 0
    with PROGRESS_LOCK:
        DISK_CACHE[path] = (now, total)
    return total


def engine_task_view(t: dict) -> dict:
    meta = t.get("meta") or {}
    res = meta.get("res") or {}
    files = res.get("files") or []
    size = res.get("size") or sum(int(f.get("size") or 0) for f in files)
    size = int(size or 0)
    prog = t.get("progress") or {}
    status_raw = (t.get("status") or "").lower()
    status = {"wait": "waiting", "ready": "waiting", "waiting": "waiting",
              "running": "downloading", "pause": "paused", "paused": "paused",
              "done": "done", "error": "error"}.get(status_raw, status_raw or "waiting")
    name = res.get("name") or t.get("name") or ""
    req_url = (meta.get("req") or {}).get("url") or ""
    if not name or name.lower().startswith("magnet:?"):
        name = magnet_display_name(req_url) or name
    # 元数据没下来时内核只能给出 infohash，用我们加任务时记住的真名顶上
    ih_now = infohash_of(req_url)
    if ih_now and (not name or looks_like_infohash(name)):
        hint = NAME_HINTS.get(ih_now)
        if hint:
            name = hint
        elif not name or looks_like_infohash(name):
            # 连 hint 都没有（纯 infohash 磁力）：别把 40 位哈希甩到界面上
            name = f"未知种子 {ih_now[:8]}（等待种子信息）"
    if not name:
        name = req_url.rstrip("/").rsplit("/", 1)[-1][:80] or "未命名任务"
    if ih_now and name and not looks_like_infohash(name) and not name.startswith("未知种子"):
        # 元数据到手后内核会给出真名，顺手记下来，下次重启也不用再猜
        if NAME_HINTS.get(ih_now) != name:
            NAME_HINTS[ih_now] = name
            CONFIG["name_hints"] = dict(list(NAME_HINTS.items())[-200:])
    tid = str(t.get("id"))

    downloaded = int(prog.get("downloaded") or 0)
    if downloaded <= 0 and t.get("protocol") == "bt" and size > 0:
        # Gopeed 1.9.3 在部分 BT 任务上 downloaded 恒为 0，改看磁盘真实字节
        base = (meta.get("opts") or {}).get("path") or CONFIG["download_dir"]
        target = str(Path(base) / name) if name else base
        on_disk = dir_bytes(target) if Path(target).exists() else dir_bytes(base)
        if on_disk > 0:
            downloaded = min(on_disk, size) if size else on_disk

    speed = int(prog.get("speed") or 0) if status == "downloading" else 0
    now = time.time()
    if status == "downloading":
        with PROGRESS_LOCK:
            prev = SPEED_SAMPLES.get(tid)
        if speed <= 0 and prev and now - prev[0] >= 1.5 and downloaded > prev[1]:
            speed = int((downloaded - prev[1]) / (now - prev[0]))
        if not prev or now - prev[0] >= 1.5:
            with PROGRESS_LOCK:
                SPEED_SAMPLES[tid] = (now, downloaded)

    return {
        "id": tid,
        "kind": "engine",
        "name": name,
        "status": status,
        "downloaded": downloaded,
        "size": size,
        "speed": speed,
        "dir": (meta.get("opts") or {}).get("path") or CONFIG["download_dir"],
        "error": t.get("shandianError") or None,
        "site": None,
        "protocol": t.get("protocol"),
        "created": t.get("createdAt") or "",
        "note": None,
        "_created": bool(t.get("isCreated")),
        "files": [{"name": f.get("name"), "size": f.get("size"), "path": f.get("path")} for f in files],
    }


def annotate_stall(task: dict, meta_ready: bool) -> None:
    """给「在跑但一动不动」的任务补一句人话，别让用户对着'下载中'发呆。"""
    tid = task["id"]
    now = time.time()
    with PROGRESS_LOCK:
        st = PROGRESS_STATE.setdefault(tid, {"bytes": -1, "ts": now})
        if task["downloaded"] != st["bytes"]:
            st["bytes"] = task["downloaded"]
            st["ts"] = now
        idle = now - st["ts"]
    if task["status"] != "downloading" or task["speed"] > 0:
        return
    if not meta_ready:
        if idle > 120:
            task["note"] = ("一直拿不到种子信息：多半是当前网络封了 BT（DHT / UDP 被挡），"
                            "换手机热点试试，或用直链下载同一个文件")
        elif idle > 20:
            task["note"] = "正在从 DHT / Tracker 找种子，暂时还没有 Peer 应答"
        return
    if idle > 120 and (not task["size"] or task["downloaded"] < task["size"]):
        task["note"] = ("连不上 Peer，速度一直是 0：当前网络可能封了 BT（UDP / DHT 被挡），"
                        "换网络，或改用直链下载")
    elif idle > 40 and (not task["size"] or task["downloaded"] < task["size"]):
        task["note"] = "等待做种：当前没有可连接的 Peer，速度会一直是 0"


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


# --------------------------------------------------------------------------- 启动恢复


def auto_resume_unfinished() -> None:
    """内核重启后会把未完成任务置为暂停，这里把它们接回去。

    已完成的（进度等于总大小）不动，避免把 done 的任务重新拉起来。
    """
    time.sleep(1.5)
    if not CONFIG.get("auto_resume", True) or not ENGINE.alive:
        return
    try:
        tasks = ENGINE.tasks()
    except Exception:
        return
    resumed = 0
    for t in tasks:
        if (t.get("status") or "").lower() not in ("pause", "paused"):
            continue
        if str(t.get("id")) in USER_PAUSED:
            continue
        url = ((t.get("meta") or {}).get("req") or {}).get("url") or ""
        ih = infohash_of(url)
        if ih and ih in BAD_INFOHASH:
            print(f"[引擎] 跳过崩溃元凶 {ih[:12]}（{str(t.get('name',''))[:24]}）")
            try:
                ENGINE.delete(str(t.get("id")), with_files=False)
            except Exception:
                pass
            continue
        res = (t.get("meta") or {}).get("res") or {}
        size = int(res.get("size") or 0)
        done = int((t.get("progress") or {}).get("downloaded") or 0)
        if size and done >= size:
            continue
        try:
            ENGINE.resume(str(t["id"]))
            resumed += 1
        except Exception:
            pass
    if resumed:
        print(f"[引擎] 已恢复 {resumed} 个未完成任务")


def heal_frozen() -> None:
    """把「状态 running、但字节数长时间不动、速度恒 0」的 BT 任务踢一脚。

    踩到过的真实场景：同一种子被加了两次，anacrolix/torrent 里两条任务抢同一批
    文件锁，双双卡死在 0 速，pause/resume 也救不回来——只有重启内核算。
    重启内核会把所有任务打断，代价太大，所以先做一次 pause→resume 尝试；
    仍不动就留给用户手动处理，并在任务上打标提示。
    """
    now = time.time()
    try:
        raw = ENGINE.tasks()
    except Exception:
        return
    seen: set[str] = set()
    for t in raw:
        tid = str(t.get("id") or "")
        if not tid:
            continue
        st = (t.get("status") or "").lower()
        if st in ("pause", "paused") and tid not in USER_PAUSED:
            # 内核有时会自己把任务置成 paused（排队/内部异常），界面就永远卡"已暂停"。
            # 用户没亲手暂停的，就直接接回来。
            prog = t.get("progress") or {}
            done = int(prog.get("downloaded") or 0)
            size = int(((t.get("meta") or {}).get("res") or {}).get("size") or 0)
            if size and done >= size:
                continue
            last_try = RESUME_TRIES.get(tid, 0.0)
            if now - last_try < 60:
                continue
            RESUME_TRIES[tid] = now
            try:
                ENGINE.resume(tid)
                print(f"[引擎] 任务 {tid[:10]} 被内核静默暂停，已自动接回")
            except Exception:
                pass
            continue
        if st != "running":
            continue
        if (t.get("protocol") or "") != "bt":
            continue
        tid = str(t.get("id") or "")
        if not tid:
            continue
        seen.add(tid)
        prog = t.get("progress") or {}
        done = int(prog.get("downloaded") or 0)
        speed = int(prog.get("speed") or 0)
        if speed > 0:
            with HEAL_LOCK:
                FROZEN_SAMPLES.pop(tid, None)
            continue
        if done <= 0:
            # 元数据阶段（DHT 找 peer）本来就是 0 字节 0 速，不算僵死
            with HEAL_LOCK:
                FROZEN_SAMPLES.pop(tid, None)
            continue
        with HEAL_LOCK:
            rec = FROZEN_SAMPLES.get(tid)
            if not rec or done != rec[0]:
                FROZEN_SAMPLES[tid] = [done, now, (rec[2] if rec else 0.0)]
                continue
            frozen_for = now - rec[1]
            healed_at = rec[2]
            if frozen_for < 180 or now - healed_at < 600:
                continue
            rec[2] = now
        print(f"[引擎] 任务 {tid[:10]} 已 {int(frozen_for)} 秒 0 速，尝试 pause→resume 唤醒")
        try:
            ENGINE.pause(tid)
            time.sleep(1.5)
            ENGINE.resume(tid)
        except Exception as exc:
            print(f"[引擎] 唤醒失败：{exc}")
    with HEAL_LOCK:
        for tid in list(FROZEN_SAMPLES):
            if tid not in seen:
                FROZEN_SAMPLES.pop(tid, None)


def engine_guard() -> None:
    """内核被外部杀掉或自己崩了之后，及时把 alive 状态纠正过来并自动拉起。

    没有这个守护，"内核已死但界面还显示运行中"会让所有添加/暂停/删除全部报错。
    另外负责崩溃归因：Gopeed 1.9.3 的 anacrolix/torrent 在拿到无效 infohash 时会
    panic 整个进程，这里把凶手拉黑，避免"重启→自动恢复→再崩"的死循环。
    """
    crashes: list[float] = []
    time.sleep(10)
    while True:
        ok = False
        if ENGINE.alive:
            try:
                ENGINE._req("GET", "info", timeout=3)
                ok = True
            except Exception:
                ok = False
        if ok:
            try:
                heal_frozen()
            except Exception:
                pass
            time.sleep(8)
            continue

        now = time.time()
        with LOCK:
            url = LAST_SUBMIT.get("url", "")
            ts = float(LAST_SUBMIT.get("ts") or 0.0)
        if url and 0 < now - ts < 20:
            ih = infohash_of(url)
            if ih:
                BAD_INFOHASH.add(ih)
                CONFIG["bad_infohash"] = sorted(BAD_INFOHASH)
                try:
                    save_config()
                except Exception:
                    pass
                print(f"[引擎] 判定 infohash {ih[:12]} 会把内核打崩，已拉黑")

        crashes = [c for c in crashes if now - c < 600] + [now]
        print(f"[引擎] 内核已不可用，正在重启…（近 10 分钟第 {len(crashes)} 次）")

        try:
            ENGINE.stop()
        except Exception:
            pass
        if ENGINE.start():
            if len(crashes) < 2:
                try:
                    auto_resume_unfinished()
                except Exception:
                    pass
            else:
                print("[引擎] 近期多次崩溃，本次跳过自动恢复，避免死循环")
            time.sleep(8)
        else:
            print("[引擎] 重启失败，20 秒后重试")
            time.sleep(20)


# --------------------------------------------------------------------------- 统一任务操作


def find_duplicate(ih: str) -> dict | None:
    """找出已经在列表（内核任务或待提交任务）里的同 infohash 任务。

    内核侧不要出现两条同 infohash 的 BT 任务：它们会抢同一批文件，
    实测会让两条一起永久 0 速。
    """
    ih = (ih or "").upper()
    if not ih:
        return None
    try:
        for t in ENGINE.tasks():
            url = ((t.get("meta") or {}).get("req") or {}).get("url") or ""
            if infohash_of(url) == ih:
                return engine_task_view(t)
    except Exception:
        pass
    with LOCK:
        for pid, pih in PENDING_IH.items():
            if pih == ih:
                t = PENDING.get(pid)
                if t:
                    return dict(t)
    return None


def _submit_engine_task(pid: str, url: str, directory: str, headers: dict | None,
                        name: str | None, select_files: list[int] | None) -> None:
    with LOCK:
        LAST_SUBMIT["url"] = url
        LAST_SUBMIT["ts"] = time.time()
    try:
        tid = ENGINE.create(url, directory, headers=headers, name=name, select_files=select_files)
        with LOCK:
            cancelled = pid in CANCELLED
            CANCELLED.discard(pid)
            PENDING.pop(pid, None)
            PENDING_IH.pop(pid, None)
        if cancelled and tid:
            # 用户在提交飞行中已经点了删除：内核那边刚建好，立刻清掉，别让卡片复活
            try:
                ENGINE.delete(str(tid), with_files=True)
            except Exception:
                try:
                    ENGINE.delete(str(tid), with_files=False)
                except Exception:
                    pass
    except Exception as exc:
        with LOCK:
            CANCELLED.discard(pid)
            PENDING_IH.pop(pid, None)
            t = PENDING.get(pid)
            if t:
                t["status"] = "error"
                t["error"] = str(exc)
        print(f"[引擎] 提交失败：{exc}")


def add_task(url: str, directory: str | None = None, headers: dict | None = None,
             name: str | None = None, select_files: list[int] | None = None,
             size: int | None = None) -> dict:
    url = url.strip()
    if not url:
        raise ValueError("链接为空")
    url = normalize_bt_input(url)
    err = validate_magnet(url)
    if err:
        raise ValueError(err)

    kind = link_kind(url)
    if kind is None:
        if url.lower().endswith(".torrent"):
            raise ValueError("种子文件不存在，请确认路径正确（也支持直接把 .torrent 文件拖进窗口）")
        if re.fullmatch(r"[0-9a-fA-F]{8,64}", url):
            raise ValueError(
                f"磁力 infohash 长度不对（当前 {len(url)} 位）：应为 40 位十六进制或 32 位 Base32")
        raise ValueError("无法识别的链接：只支持 http/https 直链、磁力(magnet:)、ed2k、以及本地 .torrent 文件")
    ih = infohash_of(url)
    if ih and ih in BAD_INFOHASH:
        raise ValueError("这条磁力曾把下载内核打崩（infohash 无效或种子损坏），已列入黑名单不再提交，请换一个种子")
    if is_wechat_share(url):
        raise ValueError("微信视频号链接需要配合浏览器扩展使用：在扩展里点「下载这个视频」")
    if baidu_share_url(url):
        raise ValueError("百度网盘分享链接需要在浏览器扩展里选「用闪电下载」，或先安装 bdpan 后重试")

    if kind == "torrent" and os.path.isfile(url):
        try:
            meta = torrent_meta(Path(url).read_bytes())
        except Exception as exc:
            raise ValueError(f"种子文件解析失败：{exc}")
        name = name or meta["name"] or None
        size = size or meta["size"] or None
        magnet = "magnet:?xt=urn:btih:" + meta["infohash"].upper()
        if meta["name"]:
            magnet += "&dn=" + urllib.parse.quote(meta["name"])
        # 老种子的 tracker 多半已经死了，把本机维护的活 tracker 一起带上，
        # 否则只会一直"找 peer"却一个都连不上。
        merged: list[str] = []
        for tr in meta["trackers"] + list(TRACKER_CACHE):
            if tr.startswith("udp://") or tr.startswith("http"):
                if tr not in merged:
                    merged.append(tr)
        for tr in merged[:40]:
            magnet += "&tr=" + urllib.parse.quote(tr, safe="")
        url, kind = magnet, "magnet"
        print(f"[种子] {meta['name'] or '(无名字)'} · {meta['infohash'][:12]} · "
              f"{len(merged)} tracker（含本地 {len(TRACKER_CACHE)} 条）")

    if kind == "video":
        return add_video(url, directory or dir_for("video"))

    # 同一个 infohash 重复提交给内核，会生成两条抢同一批文件的 BT 任务，
    # 结果两条一起永久 0 速（实测：原任务从 7 MB/s 直接掉到 0 且再也起不来，
    # 只有重启内核才恢复）。所以这里直接拦掉，把已有那条指给用户。
    ih = infohash_of(url)
    if ih:
        dup = find_duplicate(ih)
        if dup:
            raise ValueError(
                f"这个种子已经在列表里了（{str(dup.get('name') or '')[:40]}），"
                f"直接看那条任务即可；重复添加会让两条任务一起卡在 0 速")

    if kind == "magnet" and TRACKER_CACHE:
        # 用户直接粘来的磁力经常只带一两个早就死掉的 tracker（有的还是一个都没有），
        # 补上本机维护的活 tracker，否则在 DHT / UDP 被封的网络里一个 peer 都找不到，
        # 界面上就表现为"一直 0 速、一直在找种子"。
        have = {urllib.parse.unquote(x) for x in re.findall(r"[?&]tr=([^&]+)", url)}
        added = 0
        for tr in TRACKER_CACHE:
            if added >= 30 or tr in have:
                continue
            if not (tr.startswith("udp://") or tr.startswith("http")):
                continue
            url += "&tr=" + urllib.parse.quote(tr, safe="")
            added += 1
        if added:
            print(f"[磁力] 补上 {added} 条活 tracker")

    directory = directory or dir_for(kind)
    if not ENGINE.alive and not ENGINE.start():
        raise EngineError("下载内核未运行，无法添加该任务")

    pid = "p-" + uuid.uuid4().hex[:10]
    display = name or magnet_display_name(url) or None
    if ih and display and not looks_like_infohash(display) and NAME_HINTS.get(ih) != display:
        NAME_HINTS[ih] = display
        CONFIG["name_hints"] = dict(list(NAME_HINTS.items())[-200:])
        try:
            save_config()
        except Exception:
            pass
    task = {
        "id": pid, "kind": "engine", "name": display or url, "status": "waiting",
        "downloaded": 0, "size": int(size or 0), "speed": 0, "dir": directory, "error": None,
        "site": None, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": [],
    }
    with LOCK:
        PENDING[pid] = task
        if ih:
            PENDING_IH[pid] = ih
    threading.Thread(target=_submit_engine_task,
                     args=(pid, url, directory, headers, display, select_files), daemon=True).start()
    return dict(task)


def snapshot() -> dict:
    if ENGINE.alive and ENGINE.port and not _port_alive(ENGINE.port):
        ENGINE.alive = False
    tasks: list[dict] = []
    if ENGINE.alive:
        try:
            tasks = [engine_task_view(t) for t in ENGINE.tasks()]
            with LOCK:
                LAST_TASKS[:] = [dict(t) for t in tasks]
        except Exception as exc:
            print(f"[引擎] 拉取任务失败：{exc}")
            ENGINE.alive = False
    with LOCK:
        if not tasks and LAST_TASKS:
            # 内核暂时不可用：保留上次快照，别让用户看到任务凭空消失
            tasks = [dict(t) for t in LAST_TASKS]
        tasks += [dict(t) for t in PENDING.values()]
        tasks += [dict(t) for t in VIDEO_TASKS.values()]
    tasks.sort(key=lambda t: t.get("created") or "", reverse=True)
    for t in tasks:
        t.pop("pid", None)
        meta_ready = t.pop("_created", True)
        if t.get("kind") == "engine":
            annotate_stall(t, meta_ready)
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
            PENDING_IH.pop(tid, None)
            if action != "restore":
                CANCELLED.add(tid)
                if len(CANCELLED) > 500:
                    CANCELLED.clear()
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
        USER_PAUSED.add(tid)
        _save_user_paused()
        ENGINE.pause(tid)
    elif action == "resume":
        USER_PAUSED.discard(tid)
        _save_user_paused()
        ENGINE.resume(tid)
    elif action == "delete":
        USER_PAUSED.discard(tid)
        ENGINE.delete(tid, with_files=False)
    elif action == "delete-file":
        USER_PAUSED.discard(tid)
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
    # 用 HTTP/1.0：每次请求一条新连接。keep-alive 下服务端回收空闲连接时，
    # 浏览器可能仍在复用已被关闭的连接，fetch 就会报 Failed to fetch。
    protocol_version = "HTTP/1.0"
    timeout = 60

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
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


class UiServer(ThreadingHTTPServer):
    """Windows 上默认的 SO_REUSEADDR 允许两个进程绑同一个端口，
    双开时请求会随机落到后启动那个（任务列表看起来"空了"）。这里强制独占。"""

    allow_reuse_address = False
    allow_reuse_port = False

    def server_bind(self):
        if sys.platform == "win32":
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except Exception:
                pass
        super().server_bind()


def start_gateway() -> None:
    try:
        httpd = UiServer(("127.0.0.1", GATEWAY_PORT), GatewayHandler)
    except OSError as exc:
        print(f"[网关] 6800 绑定失败：{exc}")
        return
    GATEWAY_STATE["running"] = True
    print(f"[网关] 浏览器扩展接口 127.0.0.1:{GATEWAY_PORT}")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


# --------------------------------------------------------------------------- 界面 API


class Handler(BaseHTTPRequestHandler):
    server_version = "ShandianWin/1.0"
    # HTTP/1.0 + Connection: close，避免浏览器复用被服务端回收的空闲连接
    protocol_version = "HTTP/1.0"
    timeout = 60

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
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
                added, errors = 0, []
                for u in urls:
                    try:
                        add_task(str(u))
                        added += 1
                    except Exception as exc:
                        errors.append(str(exc))
                self._json({"ok": True, "added": added, "errors": errors})
            elif path == "/api/add-torrent":
                raw = str(body.get("data") or "")
                if not raw:
                    raise ValueError("没有收到种子文件内容")
                try:
                    blob = base64.b64decode(raw.split(",")[-1])
                except Exception:
                    raise ValueError("种子文件内容不是合法的 base64")
                meta = torrent_meta(blob)
                store = DATA_DIR / "torrents"
                store.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^\w.\-]", "_", str(body.get("name") or "upload.torrent"))[-80:]
                fp = store / f"{uuid.uuid4().hex[:8]}-{safe}"
                fp.write_bytes(blob)
                task = add_task(str(fp), body.get("dir"), name=meta["name"] or None,
                                size=meta["size"] or None)
                self._json({"ok": True, "task": task, "infohash": meta["infohash"],
                            "size": meta["size"]})
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
            httpd = UiServer(("127.0.0.1", candidate), Handler)
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
    for h in CONFIG.get("bad_infohash") or []:
        if isinstance(h, str) and h:
            BAD_INFOHASH.add(h.upper())
    for tid in CONFIG.get("user_paused") or []:
        if isinstance(tid, str) and tid:
            USER_PAUSED.add(tid)
    for ih, nm in (CONFIG.get("name_hints") or {}).items():
        if isinstance(ih, str) and isinstance(nm, str) and nm:
            NAME_HINTS[ih.upper()] = nm

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

    def _graceful(signum=None, frame=None) -> None:
        """被关掉/被 kill 的时候先把内核收干净，别留孤儿进程去抢 bbolt 库。"""
        try:
            ENGINE.stop()
        except Exception:
            pass
        try:
            os._exit(0)
        except Exception:
            sys.exit(0)

    for _sig in ("SIGTERM", "SIGINT", "SIGBREAK"):
        try:
            signal.signal(getattr(signal, _sig), _graceful)
        except Exception:
            pass

    start_gateway()
    register_protocol()
    if CONFIG.get("launch_at_login"):
        set_launch_at_login(True)
    threading.Thread(target=clipboard_watcher, daemon=True).start()
    threading.Thread(target=schedule_worker, daemon=True).start()
    threading.Thread(target=auto_resume_unfinished, daemon=True).start()
    threading.Thread(target=engine_guard, daemon=True).start()

    if CONFIG.get("prevent_sleep"):
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        except Exception:
            pass

    serve(UI_PORT)


if __name__ == "__main__":
    main()
