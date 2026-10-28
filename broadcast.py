#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""屏幕广播窗口：默认调官方播放器，坏了就自动降级到 ffmpeg + SDL。

两条链路
--------
**native（默认）** —— 直接拉起噢易官方的 ``ScreenRender.exe``，参数格式是从
``MainLogic.dll`` / ``MultiRender.exe`` 里扫出来的（和 OsEasy-ToolBox 的
``Fake_SCR.py`` 完全一致）::

    ScreenRender.exe {#decoderName#:#h264#,#fullscreen#:0,#local#:#<本机IP>#,
                      #port#:7778,#remote#:#<组播地址>#,#teacher_ip#:0,
                      #verityPort#:7788}

窗口类名是 ``MultiRenderWindowClass``。实测：合成组播喂进去 **1.27 秒**出画面。

**ffmpeg（降级）** —— 本机自带 ``ffmpeg.exe`` 支持 ``-f sdl``，配合同目录的
``SDL2.dll`` 弹窗（窗口类 ``SDL_app``），画面来自 ``oseasy_helper video`` 在
``http://127.0.0.1:<http_port>/live.ts`` 提供的直播流。实测 **2.03 秒**出画面。

为什么需要降级路径
------------------
本机装好的 ``ScreenRender.exe`` 曾经是坏的：文件里一个导入 DLL 名字符串都没有
（``KERNEL32.dll`` / ``USER32.dll`` / ``MSVCP140.dll`` … 全为 0 次），导入目录
RVA 指向了和导入表无关的数据。Windows 加载器一看导入表不合法，直接返回
``ERROR_BAD_EXE_FORMAT``，也就是::

    [WinError 193] %1 不是有效的 Win32 应用程序。

``MultiRender.exe`` 一启动就 ``0xC0000005``，自带的 ``vlc.exe`` 连 ``--version``
都段错误。这三个都来自同一批损坏文件。

后来用桌面上的 ``Os-Easy`` 副本把 ``ScreenRender.exe`` / ``MultiRender.exe``
替换修好了（原件备份在 ``_backup_broken/``），所以 ``native`` 现在是默认。
但别的机器不一定这么幸运，所以 ``pick_renderer("auto")`` 会先查
``native.screen_render_is_broken()``，一旦发现导入表被掏空就自动走 ffmpeg。

用法::

    python broadcast.py status                    只读：自检两条链路
    python broadcast.py show                      弹出广播窗口（Ctrl+C 关闭）
    python broadcast.py show --ffmpeg             强制 ffmpeg + SDL
    python broadcast.py show --native-renderer    强制官方播放器
"""
from __future__ import annotations

import argparse
import ctypes
import os
import select
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import native  # noqa: E402
import start  # noqa: E402

DEFAULT_UDP_PORT = 7778
DEFAULT_HTTP_PORT = 17778
SDL_WINDOW_CLASS = "SDL_app"
NATIVE_WINDOW_CLASS = "MultiRenderWindowClass"
WINDOW_CLASSES = {"ffmpeg": SDL_WINDOW_CLASS, "native": NATIVE_WINDOW_CLASS}
WINDOW_TITLE = "OsEasy 屏幕广播"

WM_CLOSE = 0x0010

# ffmpeg 起播参数。实测（320x240 合成流）：
#   默认参数                      -> 6.53 秒才出窗口
#   nobuffer + analyzeduration 1s -> 1.98 秒
#   下面这组                      -> 1.32 秒  ← 最快
#   analyzeduration 0 + 32K       -> 2.50 秒（探测不稳，反而更慢）
FFMPEG_LOW_LATENCY = ["-fflags", "nobuffer",
                      "-analyzeduration", "300000", "-probesize", "100000"]

user32 = ctypes.windll.user32

# SetWindowPos 相关
HWND_TOPMOST = ctypes.c_void_p(-1)
HWND_NOTOPMOST = ctypes.c_void_p(-2)
SWP_SHOWWINDOW = 0x0040
SWP_FRAMECHANGED = 0x0020
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
        except Exception:
            pass


def ffmpeg_path() -> Path:
    return native.BASE / "ffmpeg.exe"


def pick_renderer(prefer: str = "auto") -> str:
    """决定用哪个播放器。

    ``auto``   —— 官方播放器可用就用官方（更原生），不可用自动降级到 ffmpeg
    ``native`` —— 强制官方 ScreenRender.exe
    ``ffmpeg`` —— 强制 ffmpeg + SDL

    判断依据是 ``native.screen_render_is_broken()``：看 ScreenRender.exe 的
    .rdata 里还有没有导入 DLL 名字符串。没有就说明导入表被掏空，加载必报 193。
    """
    if prefer == "ffmpeg":
        return "ffmpeg"
    if prefer == "native":
        return "native"
    return "ffmpeg" if native.screen_render_is_broken() else "native"


def windows_of(pid: int, window_class: str = SDL_WINDOW_CLASS) -> list[int]:
    """枚举某个进程创建的、指定类名的窗口句柄。"""
    handles: list[int] = []
    prototype = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            name = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, name, 128)
            if name.value == window_class:
                handles.append(hwnd)
        return True

    user32.EnumWindows(prototype(callback), 0)
    return handles


def pid_of_window(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def all_windows_of_class(window_class: str = NATIVE_WINDOW_CLASS) -> list[int]:
    """枚举**所有进程**里该类顶层窗口的句柄（包括隐藏的）。

    官方学生端（MmcStudent.exe）收到老师的屏幕广播时会自己拉起
    ``ScreenRender.exe``，那个进程不是我们启动的，``windows_of(pid)`` 找不到。
    热键要能管到它，就必须按窗口类名全局扫。
    """
    handles: list[int] = []
    prototype = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        name = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, name, 128)
        if name.value == window_class:
            handles.append(hwnd)
        return True

    user32.EnumWindows(prototype(callback), 0)
    return handles


# --------------------------------------------------------------------------
# 组播组自动探测
# --------------------------------------------------------------------------
# 组播组**不是固定的**：同一个教室（网络频道12）10-15 官方日志写的是
# 229.9.61.65，10-22 实测流跑在 229.1.61.65 上。死读配置就会「老师明明在
# 广播、这边一片黑」。所以每次开窗前先做一次 1.5 秒的探测：把候选组都加入
# 一遍，谁真的有包就听谁的。

def probe_multicast_group(local: str, candidates: list[str],
                          timeout: float = 1.5,
                          port: int = DEFAULT_UDP_PORT) -> str | None:
    """在 local 网卡上同时加入 candidates，返回第一个真收到数据的组播组。"""
    socks: list[tuple[str, socket.socket]] = []
    for group in candidates:
        if not group:
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", port))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(group) + socket.inet_aton(local))
        except OSError:
            sock.close()
            continue
        socks.append((group, sock))

    if not socks:
        return None

    found = None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            readable, _, _ = select.select([s for _, s in socks], [], [], left)
            for sock in readable:
                try:
                    sock.recvfrom(65535)
                except OSError:
                    continue
                for group, s in socks:
                    if s is sock:
                        found = group
                        break
                break
            if found:
                break
    finally:
        for _, sock in socks:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP,
                                socket.inet_aton("0.0.0.0") + socket.inet_aton(local))
            except OSError:
                pass
            sock.close()
    return found


def candidate_groups(teacher: str, configured: str | None) -> list[str]:
    """候选组播组：配置值 → 旧公式 → 官方日志里出现过的。"""
    groups: list[str] = []
    for group in (configured,
                  native.multicast_group(teacher),
                  *native.multicast_groups_from_log()):
        if group and group not in groups:
            groups.append(group)
    return groups


# --------------------------------------------------------------------------
# 直播流服务（oseasy_helper video）
# --------------------------------------------------------------------------

class VideoStream:
    """后台把教师机组播的 H.264 转成 http://127.0.0.1:<port>/live.ts。"""

    def __init__(self, teacher: str, local: str,
                 http_port: int = DEFAULT_HTTP_PORT,
                 udp_port: int = DEFAULT_UDP_PORT,
                 mcast_group: str | None = None):
        self.teacher, self.local = teacher, local
        self.mcast_group = native.multicast_group(teacher, mcast_group)
        self.http_port, self.udp_port = http_port, udp_port
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self.error: str | None = None
        self._lines: list[str] = []
        self._ready = threading.Event()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.alive():
            return
        cmd = [start.python_exe(), "-m", "oseasy_helper", "video",
               "--teacher", self.teacher, "--local", self.local,
               "--mcast-group", self.mcast_group,
               "--udp-port", str(self.udp_port), "--http-port", str(self.http_port)]
        self._lines.clear()
        self._ready.clear()
        self.error = None
        self.proc = subprocess.Popen(
            cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self._lines.append(line)
            stripped = line.strip()
            if stripped.startswith("http://"):
                self.url = stripped
                self._ready.set()
            elif "Error" in stripped or "Traceback" in stripped or "ModuleNotFound" in stripped:
                self.error = stripped
                self._ready.set()

    def ensure(self, timeout: float = 15.0) -> str | None:
        """确保直播流在跑，返回播放地址；失败返回 None。"""
        if self.alive() and self.url:
            return self.url
        self.start()
        self._ready.wait(timeout)
        if self.url and self.alive():
            return self.url
        if self.error:
            print(f"  直播流启动失败：{self.error}")
        else:
            tail = " / ".join(self._lines[-3:]) if self._lines else "（没有输出）"
            print(f"  直播流没能在 {timeout:.0f} 秒内就绪：{tail}")
        return None

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.url = None


# --------------------------------------------------------------------------
# 广播窗口
# --------------------------------------------------------------------------

class BroadcastWindow:
    """按需弹出 / 关闭广播播放窗口。

    ``renderer="auto"``（默认）优先用官方 ScreenRender.exe，它不可用时自动
    降级到 ffmpeg + SDL。也可以显式指定 ``"native"`` / ``"ffmpeg"``。
    """

    def __init__(self, teacher: str, local: str,
                 http_port: int = DEFAULT_HTTP_PORT,
                 udp_port: int = DEFAULT_UDP_PORT,
                 verify_port: str | None = None,
                 fullscreen: bool = False,
                 title: str = WINDOW_TITLE,
                 renderer: str = "auto",
                 mcast_group: str | None = None,
                 probe_group: bool = True,
                 probe_timeout: float = 1.5):
        self.teacher, self.local = teacher, local
        self.fullscreen = fullscreen
        self.title = title
        self.renderer = pick_renderer(renderer)
        self.window_class = WINDOW_CLASSES[self.renderer]
        # 组播组：显式指定优先，否则由教师机 IP 推导（不同教室规则可能不同）
        self.mcast_group = native.multicast_group(teacher, mcast_group)
        self.configured_group = self.mcast_group
        self.probe_group = probe_group
        self.probe_timeout = probe_timeout
        self.stream = VideoStream(teacher, local, http_port, udp_port, mcast_group)
        self.proc: subprocess.Popen | None = None
        self._verify_port = verify_port
        self._last_error: str | None = None
        # hwnd -> (原样式, x, y, w, h)，全屏前存下来，切回窗口化时还原
        self._saved: dict[int, tuple[int, int, int, int, int]] = {}

    # ---- 状态 -----------------------------------------------------------

    def is_open(self) -> bool:
        if self.proc is None:
            return False
        if self.proc.poll() is None:
            return True
        # 用户点了窗口的叉，或者进程自己退了
        self.proc = None
        return False

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def window_handles(self) -> list[int]:
        if self.proc is None:
            return []
        return windows_of(self.proc.pid, self.window_class)

    # ---- 打开 -----------------------------------------------------------

    def show(self) -> bool:
        self._last_error = None
        if self.is_open():
            # 已经开着：保持当前形态，只把它带到前台。
            # 注意这里**不要**顺手做形态切换 —— 热键那边已经分开处理了，
            # 而且刚重启出来的新窗口如果被再切一次，反而会多一次重启。
            self._focus()
            print("  广播窗口已经开着了。")
            return True

        self._resolve_group()
        self._kill_stale_players()
        if self.renderer == "native":
            return self._show_native()
        return self._show_ffmpeg()

    def _resolve_group(self) -> None:
        """开窗前确认哪个组播组真的有流。

        组播组会随教师端「网络频道」变化，配置里的值可能过期。探测不到就
        保持配置值不动（教师可能还没开始广播），不报错。
        """
        if not self.probe_group:
            return
        candidates = candidate_groups(self.teacher, self.configured_group)
        found = probe_multicast_group(self.local, candidates, self.probe_timeout)
        if found and found != self.mcast_group:
            print(f"  ⚠ 配置的组播组 {self.mcast_group} 上没有数据，"
                  f"实测 {found} 有流，已改用 {found}")
            self.mcast_group = found
            self.stream.mcast_group = found
        elif found:
            print(f"  组播组 {found} 已确认有数据")
        else:
            print(f"  ⚠ 候选组播组 {'/'.join(candidates)} 都没收到数据 —— "
                  "老师可能还没开始广播，或本机不在该组播域内。")

    def _kill_stale_players(self) -> None:
        """清掉上一次残留的播放器（同一个本机 IP 的），避免抢 7778 端口。"""
        if self.renderer != "native":
            return
        try:
            import psutil
        except ImportError:
            return
        killed = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if (proc.info["name"] or "").lower() != native.RENDER_EXE.lower():
                    continue
                cmdline = " ".join(proc.info["cmdline"] or [])
                if f"#local#:#{self.local}#" in cmdline:
                    proc.kill()
                    killed.append(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if killed:
            print(f"  已清理残留播放器进程 {killed}")
            time.sleep(0.5)

    def _show_native(self) -> bool:
        exe = native.BASE / native.RENDER_EXE
        if not exe.exists():
            self._last_error = f"找不到 {exe}"
            print(f"  {self._last_error}")
            return False
        params = ("{#decoderName#:#h264#,#fullscreen#:%d,#local#:#%s#,#port#:%d,"
                  "#remote#:#%s#,#teacher_ip#:0,#verityPort#:%s}"
                  % (1 if self.fullscreen else 0, self.local, DEFAULT_UDP_PORT,
                     self.mcast_group,
                     self._verify_port or "7788"))
        try:
            self.proc = subprocess.Popen([str(exe), params], cwd=str(native.BASE))
        except OSError as error:
            self._last_error = str(error)
            print(f"  启动官方播放器失败：{error}")
            if getattr(error, "winerror", None) == 193:
                print("  这个 exe 的导入表被掏空了，本机加载不了。")
                print("  可以试：python broadcast.py show --ffmpeg   改走 ffmpeg + SDL")
            return False
        threading.Thread(target=self._report_window, daemon=True).start()
        print("  ▶ 已弹出广播窗口（官方 ScreenRender.exe）")
        return True

    def _show_ffmpeg(self) -> bool:
        exe = ffmpeg_path()
        if not exe.exists():
            self._last_error = f"找不到 {exe}"
            print(f"  {self._last_error}")
            return False

        if not self.stream.alive():
            print("  正在准备直播流（首次大概 1~3 秒）……")
        url = self.stream.ensure()
        if not url:
            self._last_error = "直播流没起来"
            return False

        # SDL 的 -window_size / -window_fullscreen 实测都被忽略（后者还段错误），
        # 所以这里不传尺寸参数，窗口大小由 SDL 自己定（约 1936x1119）。
        # 自定义标题能让 windows_of() 按类名 SDL_app 找到这个窗口。
        cmd = [str(exe), "-hide_banner", "-loglevel", "warning",
               *FFMPEG_LOW_LATENCY, "-i", url,
               "-f", "sdl", self.title]
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(native.BASE),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            self._last_error = str(error)
            print(f"  启动广播窗口失败：{error}")
            return False

        if self.renderer == "native" and self.fullscreen:
            threading.Thread(target=self._apply_fullscreen, daemon=True).start()
        threading.Thread(target=self._report_window, daemon=True).start()
        print(f"  ▶ 已弹出广播窗口  {url}")
        return True

    def _report_window(self, timeout: float = 8.0) -> None:
        """窗口要等第一帧解码出来才可见，这里给个反馈。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_open():
                return
            if any(user32.IsWindowVisible(h) for h in self.window_handles()):
                return
            time.sleep(0.15)
        if self.is_open():
            print("  窗口在等画面：老师那边可能还没开始广播，"
                  "一开始广播就会自动显示。")

    # ---- 关闭 -----------------------------------------------------------

    def close(self) -> bool:
        if self.proc is None:
            print("  广播窗口没开着。")
            return False
        for hwnd in self.window_handles():
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        for _ in range(6):          # 给它 0.6 秒优雅退出
            if not self.is_open():
                print("  ■ 已关闭广播窗口")
                return True
            time.sleep(0.1)
        try:
            self.proc.terminate()   # 不行就强杀
        except Exception:
            pass
        self.proc = None
        print("  ■ 已关闭广播窗口")
        return True

    def shutdown(self) -> None:
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    self.proc.terminate()
            except Exception:
                pass
            self.proc = None
        self.stream.stop()

    # ---- 窗口化 / 全屏切换 ---------------------------------------------

    def set_fullscreen(self, fullscreen: bool) -> bool:
        """切换窗口形态；窗口没开时按目标形态启动它。

        实测结论（都试过才知道）：
        * 官方播放器的窗口抗拒一切外部改尺寸/移动（``SetWindowPos``、
          ``SW_MAXIMIZE`` 全无效，永远停在初始的 1440x810），它的全屏是
          启动参数 ``#fullscreen#:1`` 决定的，实测给出 1920x1080 无边框
          置顶的真全屏。
        * ffmpeg/SDL 窗口更倔 —— 不止尺寸，连位置都不让外部改，SDL 的
          ``-window_size`` / ``-window_fullscreen`` 也被忽略（后者还会段错误）。
          所以这条路线**做不到真全屏**，只能尽量开大，并如实告知。

        两条路的共同点是：切换都要重启播放窗口进程。直播流
        （``oseasy_helper video``）不重启，ffmpeg 那条路只是重起播放器。
        """
        if not self.is_open():
            self.fullscreen = fullscreen
            return self.show()

        if fullscreen == self.fullscreen:
            self._focus()
            return True          # 已经是目标形态，什么都不做

        if self.renderer != "native" and fullscreen:
            print("  ffmpeg + SDL 这条路线没法真正全屏（SDL 窗口不听外部控制），")
            print("  已尽量把窗口开大。要全屏请修好官方播放器后用 --native-renderer，")
            print("  或者直接双击桌面/老师那边的广播窗口。")

        print(f"  切换成{'全屏' if fullscreen else '窗口化'}需要重启播放窗口"
              f"（约 1~2 秒）……")
        self.close()
        self.fullscreen = fullscreen
        return self.show()

    # ---- 窗口操作 -------------------------------------------------------

    def _focus(self) -> None:
        for hwnd in self.window_handles():
            user32.SetForegroundWindow(hwnd)
            return

    def _apply_fullscreen(self, tries: int = 40) -> None:
        for _ in range(tries):
            if not self.is_open():
                return
            handles = self.window_handles()
            if handles:
                self._make_fullscreen(handles[0])
                return
            time.sleep(0.1)

    def _make_fullscreen(self, hwnd: int) -> bool:
        try:
            if hwnd not in self._saved:
                style = user32.GetWindowLongW(hwnd, GWL_STYLE)
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                self._saved[hwnd] = (style, rect.left, rect.top,
                                     rect.right - rect.left,
                                     rect.bottom - rect.top)
            user32.SetWindowLongW(hwnd, GWL_STYLE,
                                  user32.GetWindowLongW(hwnd, GWL_STYLE)
                                  & ~(WS_CAPTION | WS_THICKFRAME))
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0,
                                user32.GetSystemMetrics(0),
                                user32.GetSystemMetrics(1),
                                SWP_SHOWWINDOW | SWP_FRAMECHANGED)
            return True
        except Exception:
            return False

    def _make_windowed(self, hwnd: int) -> bool:
        try:
            saved = self._saved.pop(hwnd, None)
            if saved:
                style, x, y, w, h = saved
                user32.SetWindowLongW(hwnd, GWL_STYLE, style)
            else:
                # 没存过（窗口本来就是窗口化的）：补回标题栏 + 可拉伸边框
                user32.SetWindowLongW(hwnd, GWL_STYLE,
                                      user32.GetWindowLongW(hwnd, GWL_STYLE)
                                      | (WS_CAPTION | WS_THICKFRAME))
                sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
                w, h = int(sw * 0.6), int(sh * 0.6)
                x, y = (sw - w) // 2, (sh - h) // 2
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, x, y, w, h,
                                SWP_SHOWWINDOW | SWP_FRAMECHANGED)
            return True
        except Exception:
            return False


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def action_status(teacher: str | None, local: str | None,
                  mcast_group: str | None = None) -> int:
    print("=" * 66)
    print("  广播窗口可用性自检")
    print("=" * 66)
    print(f"  噢易目录      : {'✓' if native.BASE.is_dir() else '✗'} {native.BASE}")

    ff = ffmpeg_path()
    print(f"  ffmpeg.exe    : {'✓' if ff.exists() else '✗'} {ff}")
    print(f"  SDL2.dll      : {'✓' if (native.BASE / 'SDL2.dll').exists() else '✗'}"
          f"  (ffmpeg -f sdl 需要它)")

    for name in ("ScreenRender.exe", "MultiRender.exe", "vlc.exe"):
        path = native.BASE / name
        if not path.exists():
            print(f"  {name:<14}: （不存在）")
            continue
        print(f"  {name:<14}: 存在 {path.stat().st_size:,} 字节")

    print("\n[官方播放器 ScreenRender.exe]")
    diag = _diagnose_broken_pe(native.BASE / "ScreenRender.exe")
    if diag:
        print(f"  {diag}")

    chosen = pick_renderer("auto")
    print(f"\n[将使用]  {chosen}"
          + ("（官方 ScreenRender.exe）" if chosen == "native" else "（ffmpeg + SDL）"))
    print("  想强制切换： --native-renderer  /  --ffmpeg")

    if teacher and local:
        print("\n[直播流]")
        group = native.multicast_group(teacher, mcast_group)
        print(f"  广播组播      : {group}:7778")
        stream = VideoStream(teacher, local, mcast_group=mcast_group)
        url = stream.ensure()
        if url:
            print(f"  ✓ {url}")
        stream.stop()
    else:
        print("\n（想测直播流，请加 --teacher <教师机IP> --local <本机IP>）")
    return 0


def _diagnose_broken_pe(path: Path) -> str | None:
    """看看 ScreenRender.exe 是不是导入表被掏空了。"""
    if not path.exists():
        return None
    data = path.read_bytes()
    names = sum(data.count(n.encode()) for n in
                ("KERNEL32.dll", "USER32.dll", "GDI32.dll", "MSVCP140.dll", "VCRUNTIME140.dll"))
    if names == 0:
        return "导入表已损坏（找不到任何导入 DLL 名字符串）→ 加载必然返回 WinError 193"
    return f"导入表看起来正常（找到 {names} 处导入 DLL 名字符串）"


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="broadcast.py",
        description="用 ffmpeg + SDL 弹出 / 关闭屏幕广播窗口。")
    parser.add_argument("action", nargs="?", choices=("status", "show"), default="status")
    parser.add_argument("--teacher", help="教师机 IP（默认从 user.conf 读）")
    parser.add_argument("--local", help="本机课堂网卡 IP（默认自动探测）")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT)
    parser.add_argument("--fullscreen", action="store_true", help="铺满屏幕并置顶")
    parser.add_argument("--native-renderer", action="store_true",
                        help="强制用官方 ScreenRender.exe")
    parser.add_argument("--ffmpeg", action="store_true",
                        help="强制用 ffmpeg + SDL 弹窗")
    parser.add_argument("--mcast-group",
                        help="广播组播组地址（默认由教师机 IP 推导 229.1.<3>.<4>；"
                             "组播组会随教师端频道变化，开窗前会自动探测覆盖）")
    parser.add_argument("--no-probe", action="store_true",
                        help="不做组播组探测，直接用配置/推导出的组")
    parser.add_argument("--probe-timeout", type=float, default=1.5,
                        help="组播组探测时长（秒，默认 1.5）")
    args = parser.parse_args(argv)

    teacher = args.teacher or native.teacher_ip_from_conf()
    local = args.local or (native.local_ip_for(teacher) if teacher else None)

    if args.action == "status":
        return action_status(teacher, local, args.mcast_group)

    if not teacher or not local:
        print("需要教师机 IP 和本机 IP。用 --teacher / --local 指定，"
              "或先跑 python start.py --check 看看。")
        return 1

    renderer = "ffmpeg" if args.ffmpeg else ("native" if args.native_renderer else "auto")
    window = BroadcastWindow(
        teacher, local, args.http_port, args.udp_port,
        fullscreen=args.fullscreen, renderer=renderer, mcast_group=args.mcast_group,
        probe_group=not args.no_probe, probe_timeout=args.probe_timeout)
    if not window.show():
        window.shutdown()
        return 1
    print("按 Ctrl+C 关闭窗口并退出。")
    try:
        while window.is_open():
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    window.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
