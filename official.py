#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官方学生端模式：上课直接用 MmcStudent.exe，我们只负责「换教室」和「热键」。

和 watch.py 的分工
------------------
``watch.py`` 是自己实现协议（登录 + 缩略图应答），广播窗口也自己拉。
本文件反过来了：**连接这块全部交回官方 MmcStudent.exe**，我们只做两件官方没有的事：

1. **换教室** —— 改 ``skin\\core.conf`` 的 ``/RemoteIp/<教师机IP>/``，
   再让 ``MultiMediaConfigForTradition.exe`` 点一下「保存」重算它的 ``/MD5/`` 校验位，
   然后重启 ``MmcStudent.exe`` 让它重新登录。切过的教室会缓存成 ``configs\\<教室>.conf``，
   下次进同一间教室直接拷回来，秒切、不闪窗口。详见下面「一、换教室」的注释。
2. **热键** —— 广播播放窗口（窗口类 ``MultiRenderWindowClass``）不管是谁拉起来的
   （官方学生端拉的和我们拉的一样），都能被关掉 / 重开成窗口或全屏。

为什么这么改
------------
官方学生端自己会做：登录、在线状态、收下发文件、提交作业、**老师点进来查看屏幕**
（它会拉 ``ScreenSender.exe`` 做视频上行）。最后这条我们自己的 client 一直做不到
（``cmd=109 kind=5`` 只能收到不能应答，``ScreenSender.exe`` 加壳没逆向出来）。
所以把「连接」还给官方，只保留热键这一个官方没有的能力，是最省事也最稳的组合。

用法::

    python official.py                自动识别教室 → 写配置 → 重启学生端 → 挂热键
    python official.py 5-216          指定教室（支持名字片段）
    python official.py --ask          强制弹出教室菜单
    python official.py --status       只读：当前配置、进程、播放器窗口
    python official.py --no-restart   只挂热键，不改配置也不动学生端
    python official.py --no-hotkey    只切教室，不挂热键
    python official.py --gui          打开官方配置工具（手动改，会弹 UAC）

热键（和 watch.py 一致，可改）::

    alt+u       窗口化广播   ——没开就窗口化打开，开着就切回窗口
    ctrl+alt+f  全屏广播     ——没开就全屏打开，开着就切成全屏
    alt+k       关闭广播窗口

    python official.py --show-key ctrl+alt+1 --hide-key ctrl+alt+2
                        --fullscreen-key ctrl+alt+3
"""
from __future__ import annotations

import argparse
import ctypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import broadcast  # noqa: E402
import native  # noqa: E402
import start  # noqa: E402

STUDENT_EXE = "MmcStudent.exe"
CONFIG_TOOL = "MultiMediaConfigForTradition.exe"
USER_CONF = native.USER_CONF
STUDENT_LOG = native.STUDENT_LOG

DEFAULT_UDP_PORT = 7778
MANAGE_PORT = 9003
NATIVE_WINDOW_CLASS = broadcast.NATIVE_WINDOW_CLASS

# 学生端退出后可能残留的子进程（不清理会占着 7778 / 抢着推流）
CHILD_EXES = ("ScreenRender.exe", "ScreenSender.exe", "StudentScreenHelper.exe",
              "AudioRender.exe", "AudioPlayRender.exe", "AudioDirectRepeater.exe")

CONF_TEACHER_RE = re.compile(rb"/MutiTeacherIp/[^/\r\n]*/")
LOGIN_RE = re.compile(r"StudentLogic::Login\(\): Ip\[([^\]]+)\]")
LOG_NODE_ERR_RE = re.compile(r"NODE::Connected\(\): Error \[(\d+)\]")

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_CLOSE, PM_REMOVE = 0x0312, 0x0010, 0x0001
SW_SHOW, SW_RESTORE = 5, 9
VK_F1 = 0x70

user32 = ctypes.windll.user32
stopping = threading.Event()


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
        except Exception:
            pass


def _psutil():
    try:
        import psutil
    except ImportError:
        return None
    return psutil


# --------------------------------------------------------------------------
# 一、换教室：改 skin\core.conf 的 /RemoteIp/，再让官方工具重算校验
# --------------------------------------------------------------------------
#
# 踩过的坑（很重要，别再走回头路）：
#
# 1. 教师机 IP 有两个地方存：
#      skin\core.conf   /RemoteIp/<IP>/     ← 全局配置，真正的「当前教师机」
#      %APPDATA%\Mmc\user.conf /MutiTeacherIp/<IP>/ ← 学生端本地覆盖
#    学生端**优先用 user.conf**，user.conf 不存在或校验不过时才回退到 core.conf。
#
# 2. 两个文件都有 /MD5/<32位>/ 校验位，**内容是整份文件**（改任何一个字段都会变），
#    而且是自定义的 16 符号替换表（字母表恰好是 6789NPQRSTUVWXYZ），
#    不是标准 md5/sha1 的任何常见变体 —— 手算不出来，别浪费时间。
#    改了内容却不更新 /MD5/，学生端会判 invalid 并直接把文件清成两行的残桩，
#    然后回退到 core.conf（于是「改了 IP 却没切过去」，正是 2026-10-27 那次事故）。
#
# 3. 官方配置工具 MultiMediaConfigForTradition.exe 保存时会**自己重算** /MD5/。
#    所以正确姿势：先用文本改 core.conf 的 /RemoteIp/，再启动配置工具点一下「保存」，
#    它会按当前内容重新生成校验位。工具是 requireAdministrator，管理员下不会弹 UAC。
#    （保存后它会把 %APPDATA%\Mmc 整个清空 —— 官方行为，等于让学生端重新初始化。）
#
# 4. 每换一间教室都要跑一次工具太慢，所以第一次成功后把整份 core.conf 存进
#    configs\<教室>.conf；下次进同一间教室直接拷回来（秒切、不弹窗）。
#    拷之前会校验「除 RemoteIp/MD5 外其它字段完全一致」，防止把别的设置改回旧值。

CORE_CONF = native.BASE / "skin" / "core.conf"
CACHE_DIR = HERE / "configs"
CORE_REMOTE_RE = re.compile(rb"/RemoteIp/[0-9.]*/")
CORE_MD5_RE = re.compile(rb"/MD5/[A-Z0-9]*/")
SAVE_BTN = (240, 742)          # 「保存」按钮在工具窗口里的相对坐标（窗口 480x773）
TOOL_WINDOW_CLASS = "xmlwnd"


def core_remote_ip() -> str | None:
    """读 core.conf 里的 /RemoteIp/（当前生效的教师机 IP）。"""
    if not CORE_CONF.exists():
        return None
    match = re.search(rb"/RemoteIp/([0-9.]+)/", CORE_CONF.read_bytes())
    return match.group(1).decode("ascii") if match else None


def teacher_ip_from_conf() -> str | None:
    """当前学生端会用哪个教师机 IP（user.conf 优先，没有就用 core.conf）。"""
    return _user_conf_ip() or core_remote_ip()


def _backup(path: Path, keep: int = 10) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    backup.write_bytes(path.read_bytes())
    try:
        old = sorted(path.parent.glob(f"{path.name}.bak-*"), key=lambda p: p.stat().st_mtime)
        for extra in old[:-keep]:
            extra.unlink()
    except OSError:
        pass
    return backup


def set_core_remote_ip(ip: str) -> dict:
    """只改 core.conf 的 /RemoteIp/（这时 /MD5/ 会失配，必须再 reseal）。"""
    raw = CORE_CONF.read_bytes() if CORE_CONF.exists() else b""
    if not raw:
        return {"changed": False, "old": "", "backup": None}
    old = (CORE_REMOTE_RE.search(raw).group(0).split(b"/")[2].decode("ascii", "replace")
           if CORE_REMOTE_RE.search(raw) else "")
    new = CORE_REMOTE_RE.sub(b"/RemoteIp/" + ip.encode("ascii") + b"/", raw, count=1)
    if new == raw:
        return {"changed": False, "old": old, "backup": None}
    backup = _backup(CORE_CONF)
    CORE_CONF.write_bytes(new)
    return {"changed": True, "old": old, "backup": backup}


def _core_md5() -> bytes | None:
    match = CORE_MD5_RE.search(CORE_CONF.read_bytes())
    return match.group(0) if match else None


# ---- 让官方工具重算校验位 -------------------------------------------------

EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _tool_window(pid: int) -> int | None:
    found: list[int] = []

    @EnumProc
    def callback(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _window_class(hwnd) == TOOL_WINDOW_CLASS:
            found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else None


def _click_in_tool(hwnd: int, x: int, y: int, real_mouse: bool = False) -> None:
    """在配置工具窗口里点一下 (x, y)（窗口相对坐标）。"""
    point = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(point))
    if real_mouse:
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.2)
        user32.SetCursorPos(point.x + x, point.y + y)
        time.sleep(0.1)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.06)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        return
    lparam = (y << 16) | (x & 0xFFFF)
    user32.PostMessageW(hwnd, 0x0201, 0x0001, lparam)   # WM_LBUTTONDOWN
    time.sleep(0.08)
    user32.PostMessageW(hwnd, 0x0202, 0x0000, lparam)   # WM_LBUTTONUP


def reseal_core_conf(timeout: float = 25.0) -> bool:
    """启动官方配置工具 → 点「保存」→ 让它把 /MD5/ 按当前内容重算。

    调用前 core.conf 应该已经被 set_core_remote_ip() 改过。
    """
    exe = native.BASE / CONFIG_TOOL
    if not exe.exists():
        print(f"  [!] 找不到 {exe}")
        return False
    before = _core_md5()
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(native.BASE))
    except OSError as error:
        print(f"  [!] 启动配置工具失败：{error}")
        return False

    hwnd = None
    deadline = time.time() + 15
    while time.time() < deadline:
        hwnd = _tool_window(proc.pid)
        if hwnd:
            break
        time.sleep(0.3)
    if not hwnd:
        print("  [!] 配置工具窗口没出来。")
        try:
            proc.kill()
        except OSError:
            pass
        return False

    time.sleep(1.0)                      # 等它把配置读进来
    sealed = False
    for attempt, use_mouse in ((1, False), (2, True)):
        _click_in_tool(hwnd, *SAVE_BTN, real_mouse=use_mouse)
        end = time.time() + timeout / 2
        while time.time() < end:
            time.sleep(0.4)
            now = _core_md5()
            if now and now != before:
                sealed = True
                break
        if sealed:
            break
        if attempt == 1:
            print("  第一次没点上，换真实鼠标再点一次……")

    time.sleep(1.0)
    try:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        time.sleep(0.8)
    except Exception:
        pass
    try:
        proc.terminate()
    except OSError:
        pass
    return sealed


# ---- 每间教室的配置缓存（切过的教室下次秒切）-----------------------------

def _cache_path(name: str, ip: str | None = None) -> Path:
    safe = re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", name).strip("_") or "classroom"
    if ip:
        # 教师机 IP 会变的教室（「网络频道12」这类）按 IP 分开存，别互相覆盖
        safe = f"{safe}@{ip}"
    return CACHE_DIR / f"{safe}.conf"


def _remote_ip_in(raw: bytes) -> str | None:
    match = CORE_REMOTE_RE.search(raw)
    if not match:
        return None
    return match.group(0).split(b"/")[2].decode("ascii", "replace") or None


def _masked(raw: bytes) -> bytes:
    """把 RemoteIp / MD5 抹成占位符，用来判断两份配置「除了 IP 之外」是否一致。"""
    raw = CORE_REMOTE_RE.sub(b"/RemoteIp/*/", raw)
    return CORE_MD5_RE.sub(b"/MD5/*/", raw)


def _cache_bytes(name: str, ip: str | None = None) -> bytes | None:
    """取这间教室的缓存配置；只有「除了 RemoteIp/MD5 之外和现在的 core.conf 一致」才认。"""
    path = _cache_path(name, ip)
    if not path.exists() or not CORE_CONF.exists():
        return None
    cached = path.read_bytes()
    if ip and _remote_ip_in(cached) != ip:
        # 缓存里的 IP 和这次要用的不是同一个（手动填 IP 的教室会这样）
        return None
    if _masked(cached) != _masked(CORE_CONF.read_bytes()):
        print(f"  缓存 {path.name} 和现在的 core.conf 其它字段不一致，重新生成。")
        return None
    return cached


def _apply_cache(cached: bytes) -> None:
    _backup(CORE_CONF)
    CORE_CONF.write_bytes(cached)


def save_cached_conf(name: str, ip: str, manual: bool = False) -> Path | None:
    if core_remote_ip() != ip:
        return None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(name, ip if manual else None)
        path.write_bytes(CORE_CONF.read_bytes())
        return path
    except OSError:
        return None


def _user_conf_ip() -> str | None:
    """user.conf 里的 /MutiTeacherIp/（有就优先于 core.conf）。"""
    try:
        raw = USER_CONF.read_bytes()
    except OSError:
        return None
    match = CONF_TEACHER_RE.search(raw)
    if not match:
        return None
    return match.group(0).split(b"/")[2].decode("ascii", "replace") or None


def drop_user_conf() -> Path | None:
    """删掉 %APPDATA%\\Mmc\\user.conf，让 core.conf 说了算。

    学生端每次登录成功后都会用老师下发的参数重建一份 user.conf，里面带着
    ``/MutiTeacherIp/<教师机IP>/``，**它的优先级高于 core.conf**。所以换教室时
    光改 core.conf 不够 —— 旧的那份 user.conf 会继续把学生端拉回上一间教室
    （2026-10-27 第二次踩坑就是这个）。官方配置工具保存时也是把整个
    ``%APPDATA%\\Mmc`` 清空，这里只删这一个文件，别的日志留着。
    """
    if not USER_CONF.exists():
        return None
    backup = _backup(USER_CONF)
    try:
        USER_CONF.unlink()
    except OSError:
        return None
    return backup


def switch_classroom(name: str, ip: str, manual: bool = False) -> bool:
    """把学生端指向 ip 这间教室。返回 True 表示配置已经是目标状态。

    ``manual=True`` 用于「教师机 IP 每次要手填」的教室（如「网络频道12」）：
    配置缓存按 ``教室名@IP`` 分开存，换个 IP 不会误命中上一次的缓存。
    """
    core_ip = core_remote_ip()
    user_ip = _user_conf_ip()

    # user.conf 优先级更高：里面是别的教室就先删掉，否则改了 core.conf 也没用
    if user_ip and user_ip != ip:
        print(f"  user.conf 里还留着上一间教室的 IP {user_ip}（它的优先级更高），先删掉。")
        if drop_user_conf():
            print("  ✓ 已删除 user.conf（学生端登录后会自己重建）")
        else:
            print("  [!] user.conf 删不掉，可能会被它拉回上一间教室。")

    cached = _cache_bytes(name, ip if manual else None)

    if core_ip == ip:
        if cached is None or cached == CORE_CONF.read_bytes():
            print(f"  core.conf 的教师机 IP 已经是 {ip}，不用改。")
            return True
        # IP 对了但校验位和缓存里的不一样：多半是上次重算中途失败，用缓存修正
        print(f"  教师机 IP 已经是 {ip}，但 /MD5/ 和缓存对不上，用缓存修正一下……")
        _apply_cache(cached)
        print("  ✓ 已修正")
        return True

    print(f"  切换教室：core.conf 的 /RemoteIp/ {core_ip or '(空)'} → {ip}")
    if cached is not None:
        _apply_cache(cached)
        print("  ✓ 用这间教室的缓存配置（秒切，不启动配置工具）")
        return True

    result = set_core_remote_ip(ip)
    if result["backup"]:
        print(f"  core.conf 已备份：{result['backup'].name}")
    print("  正在让官方配置工具重算校验位（会闪一下它的窗口，约 3 秒）……")
    if not reseal_core_conf():
        print("  [!] 自动点「保存」没成功。")
        print(f"      手动救急：打开 {CONFIG_TOOL}，什么都不用改，直接点「保存」。")
        return False
    print("  ✓ 校验位已重算")
    saved = save_cached_conf(name, ip, manual=manual)
    if saved:
        print(f"  已记住这间教室的配置：{saved.name}（下次进这间教室秒切）")
    return True


def tcp_open(ip: str, port: int, timeout: float = 2.5) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


# --------------------------------------------------------------------------
# 二、重启官方学生端
# --------------------------------------------------------------------------

def student_pids() -> list[int]:
    psutil = _psutil()
    if psutil is None:
        return []
    pids = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info["name"] or "").lower() == STUDENT_EXE.lower():
                pids.append(proc.info["pid"])
        except Exception:
            continue
    return pids


def student_running() -> bool:
    return bool(student_pids())


def stop_student(timeout: float = 6.0) -> bool:
    """优雅结束 MmcStudent.exe，再清掉它可能留下的子进程。"""
    psutil = _psutil()
    pids = student_pids()
    if not pids:
        _kill_children()
        return True
    if psutil is None:
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, creationflags=0x08000000)
        return not student_pids()

    for pid in pids:
        try:
            psutil.Process(pid).terminate()
        except Exception:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not student_pids():
            break
        time.sleep(0.2)
    for pid in student_pids():
        try:
            psutil.Process(pid).kill()
        except Exception:
            pass
    for _ in range(20):
        if not student_pids():
            break
        time.sleep(0.15)
    _kill_children()
    return not student_pids()


def _kill_children() -> None:
    psutil = _psutil()
    if psutil is None:
        return
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info["name"] or "").lower() in [n.lower() for n in CHILD_EXES]:
                proc.kill()
        except Exception:
            continue


def start_student() -> bool:
    exe = native.BASE / STUDENT_EXE
    if not exe.exists():
        print(f"  [!] 找不到 {exe}")
        return False
    try:
        # 用 ShellExecute 交给外壳启动：这样学生端和本脚本**没有父子关系**，
        # 脚本退出（Ctrl+C / 关窗口）之后它还能继续上课。
        _shell_execute(exe, native.BASE)
    except OSError as error:
        print(f"  [!] 用 ShellExecute 启动失败（{error}），改用直接创建进程……")
        try:
            _popen_detached([str(exe)], str(native.BASE))
        except OSError as inner:
            print(f"  [!] 启动学生端失败：{inner}")
            if getattr(inner, "winerror", None) == 740:
                print("      MmcStudent 要求管理员权限，正在尝试提权启动（会弹 UAC）……")
                rc = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", str(exe), None, str(native.BASE), 1)
                if rc <= 32:
                    print(f"      提权也失败了（ShellExecute 返回 {rc}）。")
                    return False
            else:
                return False
    for _ in range(40):
        if student_running():
            return True
        time.sleep(0.25)
    print("  [!] 学生端进程没起来。")
    return False


def _shell_execute(exe: Path, cwd: Path, verb: str = "open") -> None:
    rc = ctypes.windll.shell32.ShellExecuteW(None, verb, str(exe), None, str(cwd), 1)
    if rc <= 32:
        raise OSError(f"ShellExecuteW 返回 {rc}")


# 备用：让官方学生端脱离当前进程的作业对象独立活着。
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _popen_detached(cmd: list[str], cwd: str) -> subprocess.Popen:
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    try:
        return subprocess.Popen(cmd, cwd=cwd, creationflags=flags,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        # 某些作业对象不允许 breakaway，退一步只要求独立进程组
        return subprocess.Popen(cmd, cwd=cwd,
                                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)


def log_size() -> int:
    try:
        return STUDENT_LOG.stat().st_size
    except OSError:
        return 0


def log_since(offset: int) -> str:
    try:
        with open(STUDENT_LOG, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return ""
    return data.decode("gbk", "replace")


def wait_login(offset: int, timeout: float = 25.0) -> tuple[str | None, str | None]:
    """等学生端在日志里打出 Login 成功那一行。

    返回 ``(登录IP | None, 最近一次 NODE 错误码 | None)``。
    """
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        text = log_since(offset)
        match = LOGIN_RE.search(text)
        if match:
            return match.group(1), last_error
        errors = LOG_NODE_ERR_RE.findall(text)
        if errors:
            last_error = errors[-1]
        time.sleep(0.5)
    return None, last_error


# --------------------------------------------------------------------------
# 三、广播窗口（接管官方学生端拉起来的那个）
# --------------------------------------------------------------------------

def _cmdline_of(pid: int) -> str:
    psutil = _psutil()
    if psutil is None:
        return ""
    try:
        return " ".join(psutil.Process(pid).cmdline() or [])
    except Exception:
        return ""


def render_windows() -> list[dict]:
    """所有屏幕广播播放窗口（类名 MultiRenderWindowClass）。"""
    found = []
    for hwnd in broadcast.all_windows_of_class(NATIVE_WINDOW_CLASS):
        pid = broadcast.pid_of_window(hwnd)
        cmdline = _cmdline_of(pid)
        found.append({
            "hwnd": hwnd,
            "pid": pid,
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "fullscreen": "#fullscreen#:1" in cmdline,
            "cmdline": cmdline,
            "ours": f"#local#:#" in cmdline,   # 我们自己拉的那种带参数
        })
    return found


def render_processes() -> list:
    psutil = _psutil()
    if psutil is None:
        return []
    out = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info["name"] or "").lower() == native.RENDER_EXE.lower():
                out.append(proc)
        except Exception:
            continue
    return out


class OfficialBroadcast:
    """控制官方播放器窗口：唤醒、切形态、关闭。

    关键点是 ``render_windows()`` 按窗口类名全局扫，所以**官方学生端自己拉起的
    播放器也能管**；需要重新开窗时用我们已知的参数拉起 ``ScreenRender.exe``。
    """

    def __init__(self, teacher: str, local: str,
                 mcast_group: str | None = None,
                 probe_group: bool = True,
                 probe_timeout: float = 1.5,
                 verify_port: str | None = None):
        self.teacher, self.local = teacher, local
        self.configured_group = native.multicast_group(teacher, mcast_group)
        self.mcast_group = self.configured_group
        self.probe_group = probe_group
        self.probe_timeout = probe_timeout
        self.verify_port = verify_port or native.screen_verify_port()
        self._proc: subprocess.Popen | None = None

    # ---- 查询 -----------------------------------------------------------

    def windows(self) -> list[dict]:
        return render_windows()

    def is_open(self) -> bool:
        """窗口在，或者播放器进程在等第一帧，都算「开着」。"""
        return bool(self.windows()) or bool(render_processes())

    def current_fullscreen(self) -> bool | None:
        """当前形态：True 全屏 / False 窗口 / None 没开。

        窗口还没出现时（老师还没开始广播，播放器在等流）就从命令行参数里看，
        这样热键切换在出画面之前也判断得准。
        """
        windows = self.windows()
        if windows:
            return any(w["fullscreen"] for w in windows)
        procs = render_processes()
        if not procs:
            return None
        return any("#fullscreen#:1" in _cmdline_of(p.pid) for p in procs)

    # ---- 打开 / 切换 ----------------------------------------------------

    def show(self, fullscreen: bool) -> bool:
        windows = self.windows()
        current = self.current_fullscreen()

        if current is not None and current == fullscreen:
            if windows:
                self._focus(windows[0]["hwnd"])
                print(f"  ▶ 广播窗口已经开着（{'全屏' if fullscreen else '窗口'}），已切到前台")
            else:
                print(f"  ▶ 播放器已经在等画面（{'全屏' if fullscreen else '窗口'}），"
                      "老师一开始广播就会出来")
            return True

        if current is not None:
            print(f"  切换成{'全屏' if fullscreen else '窗口化'}："
                  "官方播放器不接受外部改尺寸，只能关掉按新形态重开（约 1 秒）……")
            self._kill_all()
        return self._spawn(fullscreen)

    def close(self, retries: int = 3, grace: float = 0.9) -> bool:
        """关掉广播窗口（连同在等画面的播放器进程）。

        官方学生端可能会在窗口被关掉后立刻再拉一个，所以最多重试 3 次。
        """
        closed_any = False
        for attempt in range(retries):
            windows = self.windows()
            if windows:
                if attempt:
                    print(f"  官方学生端又把播放器拉起来了，再关一次（第 {attempt + 1} 次）……")
                closed_any = True
                self._kill(windows, grace)
                time.sleep(0.45)
                continue
            if render_processes():
                closed_any = True
                self._kill_leftovers()
                time.sleep(0.35)
                continue
            break

        if self.is_open():
            print("  [!] 关不掉：播放器一直被重新拉起。")
            print("      老师正在广播时，官方学生端可能不允许学生自行关闭画面。")
            return False
        if closed_any:
            print("  ■ 已关闭广播窗口")
        else:
            print("  广播窗口没开着。")
        return closed_any

    # ---- 内部 -----------------------------------------------------------

    def _focus(self, hwnd: int) -> None:
        try:
            user32.ShowWindow(hwnd, SW_RESTORE if not user32.IsWindowVisible(hwnd) else SW_SHOW)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def _kill(self, windows: list[dict], grace: float = 0.9) -> None:
        pids = {w["pid"] for w in windows}
        for window in windows:
            user32.PostMessageW(window["hwnd"], WM_CLOSE, 0, 0)

        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not self.windows():
                return
            time.sleep(0.1)

        psutil = _psutil()
        for pid in pids:
            try:
                if psutil:
                    psutil.Process(pid).kill()
                else:
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, creationflags=0x08000000)
            except Exception:
                continue
        for _ in range(15):
            if not self.windows():
                return
            time.sleep(0.1)

    def _kill_all(self) -> None:
        """窗口和「在等画面的播放器进程」一起清掉。"""
        windows = self.windows()
        if windows:
            self._kill(windows)
        self._kill_leftovers()

    def _resolve_group(self) -> None:
        """开窗前确认哪个组播组真的有流（组播组会随教师端频道变化）。"""
        if not self.probe_group:
            return
        candidates = broadcast.candidate_groups(self.teacher, self.configured_group)
        found = broadcast.probe_multicast_group(self.local, candidates, self.probe_timeout)
        if found and found != self.mcast_group:
            print(f"  ⚠ 配置的组播组 {self.mcast_group} 上没有数据，"
                  f"实测 {found} 有流，已改用 {found}")
            self.mcast_group = found
        elif found:
            print(f"  组播组 {found} 已确认有数据")
        else:
            print(f"  ⚠ 候选组播组 {'/'.join(candidates)} 都没收到数据 —— "
                  "老师可能还没开始广播，或本机不在该组播域内。")

    def _kill_leftovers(self) -> None:
        """清掉残留播放器，避免多个进程抢 UDP 7778。"""
        killed = []
        for proc in render_processes():
            try:
                proc.kill()
                killed.append(proc.pid)
            except Exception:
                continue
        if killed:
            print(f"  已清理残留播放器进程 {killed}")
            time.sleep(0.4)

    def _spawn(self, fullscreen: bool) -> bool:
        exe = native.BASE / native.RENDER_EXE
        if not exe.exists():
            print(f"  [!] 找不到 {exe}")
            return False
        if native.screen_render_is_broken():
            print(f"  [!] {native.RENDER_EXE} 的导入表是坏的（加载会报 WinError 193）。")
            print("      这台机器请改用：python watch.py  （自动降级 ffmpeg + SDL）")
            return False

        self._resolve_group()
        self._kill_leftovers()

        params = ("{#decoderName#:#h264#,#fullscreen#:%d,#local#:#%s#,#port#:%d,"
                  "#remote#:#%s#,#teacher_ip#:0,#verityPort#:%s}"
                  % (1 if fullscreen else 0, self.local, DEFAULT_UDP_PORT,
                     self.mcast_group, self.verify_port))
        try:
            self._proc = subprocess.Popen([str(exe), params], cwd=str(native.BASE))
        except OSError as error:
            print(f"  [!] 启动官方播放器失败：{error}")
            return False
        print(f"  ▶ 已{'全屏' if fullscreen else '窗口化'}打开广播窗口"
              f"（{self.mcast_group} → 本机 {self.local}）")
        return True


# --------------------------------------------------------------------------
# 四、热键
# --------------------------------------------------------------------------

def parse_hotkey(spec: str) -> tuple[int, int]:
    """把 'ctrl+alt+b' 解析成 (modifiers, vk)。"""
    mods, vk = MOD_NOREPEAT, None
    for part in spec.lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "super", "meta"):
            mods |= MOD_WIN
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            vk = VK_F1 + int(part[1:]) - 1
        else:
            raise ValueError(f"看不懂的按键：{part}")
    if vk is None:
        raise ValueError(f"热键 {spec!r} 里没有主键")
    return mods, vk


def hotkey_loop(window: OfficialBroadcast, args) -> int:
    try:
        show_mods, show_vk = parse_hotkey(args.show_key)
        hide_mods, hide_vk = parse_hotkey(args.hide_key)
        fs_mods, fs_vk = parse_hotkey(args.fullscreen_key)
    except ValueError as error:
        print(f"热键解析失败：{error}")
        return 1

    ok_show = user32.RegisterHotKey(None, 1, show_mods, show_vk)
    ok_hide = user32.RegisterHotKey(None, 2, hide_mods, hide_vk)
    ok_fs = user32.RegisterHotKey(None, 3, fs_mods, fs_vk)
    if not ok_show or not ok_hide or not ok_fs:
        print("  [!] 热键注册失败——可能被别的程序占用了，换一组再试：")
        print("      python official.py <教室> --show-key alt+1 --hide-key alt+2"
              " --fullscreen-key ctrl+alt+3")
        return 1

    print(f"  {args.show_key:<14} 窗口化广播")
    print(f"  {args.fullscreen_key:<14} 全屏广播")
    print(f"  {args.hide_key:<14} 关闭广播窗口")
    print("  Ctrl+C        退出")
    print("=" * 62)
    print("  热键已生效，可以最小化这个窗口。\n")

    def _stop_handler(signum, frame):
        stopping.set()

    for sig in (1, 15):        # SIGINT / SIGTERM，避免依赖 signal 模块名
        try:
            import signal
            signal.signal(getattr(signal, "SIGINT" if sig == 1 else "SIGTERM"), _stop_handler)
        except Exception:
            pass

    msg = wintypes.MSG()
    try:
        while not stopping.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY:
                    stamp = time.strftime("%H:%M:%S")
                    if msg.wParam == 1:
                        print(f"  [{stamp}] 热键：窗口化广播")
                        window.show(False)
                    elif msg.wParam == 2:
                        print(f"  [{stamp}] 热键：关闭广播窗口")
                        window.close()
                    elif msg.wParam == 3:
                        print(f"  [{stamp}] 热键：全屏广播")
                        window.show(True)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n  正在退出……")
    finally:
        for hotkey_id in (1, 2, 3):
            user32.UnregisterHotKey(None, hotkey_id)
    return 0


# --------------------------------------------------------------------------
# 五、状态 / 主流程
# --------------------------------------------------------------------------

def action_status(teacher: str | None, local: str | None) -> int:
    print("=" * 62)
    print("  官方学生端模式 · 状态")
    print("=" * 62)
    print(f"  安装目录  : {'✓' if native.BASE.is_dir() else '✗'} {native.BASE}")
    print(f"  core.conf : {'✓' if CORE_CONF.exists() else '✗'} {CORE_CONF}")
    print(f"  user.conf : {'✓' if USER_CONF.exists() else '（没有，走 core.conf）'} {USER_CONF}")

    conf_teacher = teacher_ip_from_conf()
    print(f"  当前生效的教师机 IP : {conf_teacher or '（没读到）'}")
    print(f"  core.conf 里的 RemoteIp : {core_remote_ip() or '（没读到）'}")
    cached = sorted(CACHE_DIR.glob("*.conf")) if CACHE_DIR.exists() else []
    print(f"  已缓存的教室配置        : "
          f"{', '.join(p.stem for p in cached) if cached else '（还没有，第一次进每间教室会慢一点）'}")
    if teacher and conf_teacher:
        same = teacher == conf_teacher
        print(f"  本次要用的教室          : {teacher}"
              f"   {'← 一致，不用改' if same else '← 不一致，启动时会改配置并重启学生端'}")

    print("\n[进程]")
    pids = student_pids()
    print(f"  {STUDENT_EXE:<16}: {'运行中 pid=' + ','.join(map(str, pids)) if pids else '未运行'}")

    print("\n[广播窗口]")
    windows = render_windows()
    if not windows:
        print(f"  {NATIVE_WINDOW_CLASS}: 当前没有（老师没广播，或者已经关掉了）")
    else:
        for window in windows:
            print(f"  hwnd=0x{window['hwnd']:X} pid={window['pid']} "
                  f"{'全屏' if window['fullscreen'] else '窗口'}"
                  f"{'' if window['visible'] else '（隐藏）'}")
            print(f"    {window['cmdline'][:110]}")

    if teacher:
        print("\n[连通性]")
        print(f"  教师机 {teacher}:{MANAGE_PORT} : "
              + ("✓ 通" if tcp_open(teacher, MANAGE_PORT) else "✗ 连不上（老师没开机 / 选错教室）"))
        group = native.multicast_group(teacher)
        print(f"  推导的组播组             : {group}:{DEFAULT_UDP_PORT}")
        print(f"  日志里出现过的组播组     : "
              f"{'/'.join(native.multicast_groups_from_log()) or '（无）'}")
    return 0


def launch_config_tool() -> int:
    exe = native.BASE / CONFIG_TOOL
    if not exe.exists():
        print(f"找不到 {exe}")
        return 1
    print(f"正在打开官方配置工具（需要管理员权限，会弹 UAC）：{CONFIG_TOOL}")
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), None, str(native.BASE), 1)
    if rc <= 32:
        print(f"启动失败（ShellExecute 返回 {rc}）。可以直接双击：{exe}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="official.py",
        description="直接用官方 MmcStudent.exe 上课：自动换教室 + 热键控制广播窗口。",
    )
    parser.add_argument("classroom", nargs="?", help="教室名（支持片段匹配）；不填则按本机 IP 自动识别")
    parser.add_argument("--ask", action="store_true", help="不自动识别教室，强制弹出教室菜单")
    parser.add_argument("--status", action="store_true", help="只读：看当前配置、进程、播放器窗口")
    parser.add_argument("--no-restart", action="store_true",
                        help="不改配置、不重启学生端，只挂热键")
    parser.add_argument("--no-hotkey", action="store_true", help="只切教室，不挂热键")
    parser.add_argument("--gui", action="store_true", help="打开官方配置工具（手动改，会弹 UAC）")
    parser.add_argument("--show-key", default="alt+u", help="窗口化广播热键（默认 alt+u）")
    parser.add_argument("--hide-key", default="alt+k", help="关闭广播热键（默认 alt+k）")
    parser.add_argument("--fullscreen-key", default="ctrl+alt+f", help="全屏广播热键（默认 ctrl+alt+f）")
    parser.add_argument("--mcast-group",
                        help="广播组播组（默认用 classrooms.conf 里的值/教师机 IP 推导，"
                             "开窗前会自动探测实际在跑的组）")
    parser.add_argument("--no-probe", action="store_true", help="不做组播组探测")
    parser.add_argument("--probe-timeout", type=float, default=1.5, help="组播组探测时长（秒）")
    parser.add_argument("--local", help="手动指定本机课堂网卡 IP")
    parser.add_argument("--teacher-ip",
                        help="临时指定教师机 IP（给「教师机 IP 会变」的教室用，"
                             "比如网络频道12；不写就按 classrooms.conf 里的值）")
    args = parser.parse_args(argv)

    if not native.BASE.is_dir():
        print(f"没找到噢易学生端安装目录：{native.BASE}")
        return 1

    if args.gui:
        return launch_config_tool()

    conf = HERE / start.CONF_NAME
    items = start.load_classrooms(conf)
    if not items:
        print(f"还没有配置任何教室。请编辑：{conf}")
        return 1

    if args.classroom:
        profile = start.choose_classroom(items, args.classroom)
    elif args.ask:
        profile = start.choose_classroom(items, None)
    else:
        profile = start.detect_classroom(items)
        if profile:
            print(f"  自动识别教室：{profile['name']}（本机网段与教师机 {profile['teacher']} 同段）")
        else:
            print("  没能从本机 IP 唯一确定教室，请手动选一次：")
            profile = start.choose_classroom(items, None)

    if profile is None:
        if args.classroom:
            print("（用 python start.py --list 可以查看全部已配置教室）")
            return 1
        print("已取消。")
        return 0

    teacher = args.teacher_ip or profile["teacher"]
    # 手填的 IP 和配置里的不一样 → 说明这间教室的教师机 IP 是浮动的，缓存要按 IP 分开存
    manual_ip = bool(args.teacher_ip) and args.teacher_ip != profile["teacher"]
    local = args.local or native.local_ip_for(teacher) or start.resolve_profile(profile)[1]
    mcast_group = args.mcast_group or profile["opts"].get("mcast-group")

    if args.status:
        return action_status(teacher, local)

    print("=" * 62)
    print("  噢易课堂 · 官方学生端模式")
    print("=" * 62)
    print(f"  教室      : {profile['name']}")
    print(f"  教师机    : {teacher}:{MANAGE_PORT}")
    print(f"  本机 IP   : {local or '（探测不到）'}")
    # 组播组优先用官方日志里老师实际下发的那个（各教室不一样，公式经常猜错）。
    # 只采信「第 3、4 段和教师机 IP 对得上」的组 —— 日志可能还留着上一间教室的。
    tail = ".".join(teacher.split(".")[2:])
    log_groups = [] if mcast_group else [
        g for g in native.multicast_groups_from_log()
        if ".".join(g.split(".")[2:]) == tail
    ]
    if mcast_group:
        shown_group, group_note = mcast_group, "（配置文件指定）"
    elif log_groups:
        shown_group, group_note = log_groups[0], "（官方日志里老师下发的）"
    else:
        shown_group, group_note = native.multicast_group(teacher), "（按教师机 IP 推测）"
    print(f"  广播组播  : {shown_group}:{DEFAULT_UDP_PORT}{group_note}")
    print(f"  连接方式  : 官方 {STUDENT_EXE}（收文件/在线状态/老师看屏幕都由它负责）")
    print("-" * 62)

    # 0) 别和自研 client 同时跑（会抢 9100 / 重复登录）
    if _our_client_running():
        print("  [!] 检测到 oseasy-helper 的 client 正在运行，它和官方学生端会抢 9100。")
        print("      请先关掉那个窗口（后台监听.bat）再运行本脚本。")
        return 1

    if args.no_restart:
        print("  （--no-restart：不改配置、不动学生端，只挂热键）\n")
        window = _make_window(teacher, local, mcast_group, args)
        if args.no_hotkey:
            return 0
        return hotkey_loop(window, args)

    # 1) 教师机通不通（只是提醒，不拦）
    reachable = tcp_open(teacher, MANAGE_PORT)
    if not reachable:
        print(f"  [!] 教师机 {teacher}:{MANAGE_PORT} 现在连不上。")
        print("      可能是老师还没开教师端，也可能是选错教室了 —— 继续执行，学生端会自己重试。")

    # 2) 改配置 + 重启学生端
    stale = (core_remote_ip() != teacher) or (_user_conf_ip() not in (None, teacher))
    if stale and student_running():
        # 学生端在跑的时候改配置会被它覆盖，先退掉
        print("  正在退出官方学生端……")
        if not stop_student():
            print("  [!] 学生端退不掉，配置可能被它覆盖。请手动退出后重试。")
            return 1

    if not switch_classroom(profile["name"], teacher, manual=manual_ip):
        print("  配置没切成，先不动学生端。")
        return 1

    offset = log_size()
    if not student_running():
        print(f"  启动官方学生端 {STUDENT_EXE} ……")
        if not start_student():
            return 1
        if reachable:
            print("  已启动，等它登录教师机……")
            logged_ip, node_error = wait_login(offset, timeout=25)
            if logged_ip:
                print(f"  ✓ 学生端已登录教师机（本机 {logged_ip}）")
            else:
                print("  [!] 25 秒内没看到登录成功的日志。")
                if node_error:
                    print(f"      文件节点报错 {node_error}"
                          "（10061=老师那边拒绝连接，10060=超时）。")
                print(f"      老师开课后学生端会自己重试；也可以看 {STUDENT_LOG} 确认。")
        else:
            print("  已启动；教师机暂时连不上，学生端会自己每隔几秒重试，"
                  "老师一开课就会连上。")
    else:
        print("  学生端本来就在运行，保持不动。")

    if args.no_hotkey:
        print("\n  （--no-hotkey：不挂热键）")
        return 0

    print()
    window = _make_window(teacher, local, mcast_group, args)
    return hotkey_loop(window, args)


def _make_window(teacher: str, local: str | None, mcast_group: str | None, args) -> OfficialBroadcast:
    if not local:
        local = native.local_ip_for(teacher) or ""
    return OfficialBroadcast(
        teacher, local,
        mcast_group=mcast_group,
        probe_group=not args.no_probe,
        probe_timeout=args.probe_timeout)


def _our_client_running() -> bool:
    psutil = _psutil()
    if psutil is None:
        return False
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if "oseasy_helper" in cmdline and " client" in cmdline:
                return True
        except Exception:
            continue
    return False


if __name__ == "__main__":
    raise SystemExit(main())
