#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台登录教师机（让教师端显示「在线」）+ 热键弹出/关闭广播窗口。

一条命令搞定你要的流程：

    运行 -> 选教室 -> 后台自动登录教师机
                    -> 按热键弹出广播窗口
                    -> 按另一个热键关闭广播窗口

* 后台登录：oseasy-helper 自己的实现（client 模式，不开视频）。只负责登录 9003
  和应答教师端的屏幕缩略图请求 —— 教师端「在线」就是靠后者。
* 广播窗口：默认拉起噢易官方的 ``ScreenRender.exe``；万一它不可用（导入表被
  掏空 -> ``WinError 193``），自动降级到本机自带 ``ffmpeg.exe`` + ``SDL2.dll``，
  画面来自 ``oseasy_helper video`` 在 ``http://127.0.0.1:<http_port>/live.ts``
  上提供的直播流。两条链路的实测出画时间分别是 1.27 秒 / 2.03 秒。

用法::

    python watch.py                    交互选教室
    python watch.py 5-217              指定教室
    python watch.py 5-217 --fullscreen  首次弹出就用全屏
    python watch.py 5-217 --no-login   只挂热键，不登录教师机
    python watch.py 5-217 --ffmpeg     广播窗口强制走 ffmpeg + SDL

三个热键（窗口开着也能互相切换）::

    alt+u      窗口化广播      ——没开就窗口化打开，开着就还原成窗口
    ctrl+alt+f 全屏广播        ——没开就全屏打开，开着就切成全屏
    alt+k      关闭广播窗口

想换键位::

    python watch.py 5-217 --show-key ctrl+alt+1 --hide-key ctrl+alt+2
                            --fullscreen-key ctrl+alt+3

屏幕缩略图（教师端判断「在线」就靠它）默认发真实桌面，可用 client 的参数覆盖::

    python watch.py 5-216 --thumbnail-source mock      # 改发 MOCK 占位图
    python watch.py 5-216 --no-mock-thumbnail          # 完全不应答（教师端显示离线）
    python watch.py 5-216 --thumbnail-width 320 --thumbnail-quality 9

注意：``watch.py`` 不认识的参数会原样透传给后台的 ``client``，
所以 ``client --help`` 里的选项都能用。

按 Ctrl+C 退出，会自动结束后台登录和广播窗口。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
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
from oseasy_helper import client as oseasy_client  # noqa: E402

DEFAULT_UDP_PORT = 7778
DEFAULT_HTTP_PORT = 17778
DEFAULT_VERIFY_PORT = 7788

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_CLOSE, PM_REMOVE = 0x0312, 0x0010, 0x0001
VK_F1 = 0x70

FRIENDLY_EVENTS = {
    "login_sent": "已向教师机登录",
    "management_message": None,
    "management_retry": "管理连接重连中",
    "video_listening": None,
    "thumbnail_sent": "教师端已收到屏幕缩略图 → 界面应显示「在线」",
    "command_ignored": "收到不需要应答的教师端指令（已忽略）",
    "disconnected": "连接断开",
    "stopped": "后台登录已停止",
}

user32 = ctypes.windll.user32
stopping = threading.Event()


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
        except Exception:
            pass


# --------------------------------------------------------------------------
# 热键
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


# --------------------------------------------------------------------------
# 广播窗口
# --------------------------------------------------------------------------
# 真正的实现放在 broadcast.py：默认调官方 ScreenRender.exe，不可用时自动
# 降级到 ffmpeg + SDL（画面来自 http://127.0.0.1:<http_port>/live.ts）。

BroadcastWindow = broadcast.BroadcastWindow


# --------------------------------------------------------------------------
# 后台登录
# --------------------------------------------------------------------------

class ClientSession:
    """在后台跑 oseasy-helper client，让教师端看到这台机器在线。

    只做登录（TCP 9003）和应答屏幕缩略图请求，其它指令一律不应答。
    """

    def __init__(self, teacher: str, local: str, extra: list[str] | None = None):
        self.teacher, self.local = teacher, local
        self.extra = list(extra or [])
        self.proc: subprocess.Popen | None = None
        self.thumbnail_announced = False

    def start(self) -> None:
        cmd = [start.python_exe(), "-m", "oseasy_helper", "client",
               "--teacher", self.teacher, "--local", self.local]
        cmd += self.extra
        self.proc = subprocess.Popen(
            cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = event.get("event", "")
            if name not in FRIENDLY_EVENTS:
                continue
            label = FRIENDLY_EVENTS[name]
            if label is None:
                continue
            # 缩略图每 5 秒回一次，只在第一次提示，避免刷屏。
            if name == "thumbnail_sent":
                if self.thumbnail_announced:
                    continue
                self.thumbnail_announced = True
            print(f"  [{time.strftime('%H:%M:%S')}] {label}", flush=True)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.alive() and self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="watch.py",
        description="后台登录教师机（让教师端显示在线）；热键弹出 / 关闭广播窗口。",
    )
    parser.add_argument("classroom", nargs="?", help="教室名（支持片段匹配）；不填则按本机 IP 自动识别")
    parser.add_argument("--ask", action="store_true",
                        help="不自动识别教室，强制弹出教室菜单")
    parser.add_argument("--show-key", default="alt+u", help="窗口化广播的热键（默认 alt+u）")
    parser.add_argument("--hide-key", default="alt+k", help="关闭广播窗口的热键（默认 alt+k）")
    parser.add_argument("--fullscreen-key", default="ctrl+alt+f",
                        help="全屏广播的热键（默认 ctrl+alt+f）")
    parser.add_argument("--fullscreen", action="store_true",
                        help="首次弹出广播窗口时就用全屏（默认窗口化）")
    parser.add_argument("--no-login", dest="no_login", action="store_true",
                        help="只挂热键，不登录教师机（教师端会显示离线）")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT,
                        help=f"本机直播端口（默认 {DEFAULT_HTTP_PORT}）")
    parser.add_argument("--native-renderer", action="store_true",
                        help="广播窗口强制用官方 ScreenRender.exe")
    parser.add_argument("--ffmpeg", action="store_true",
                        help="广播窗口强制用 ffmpeg + SDL")
    parser.add_argument("--mcast-group",
                        help="广播组播组地址（默认用 classrooms.conf 里该教室的 mcast-group，"
                             "没有再按教师机 IP 推导 229.1.<3>.<4>；开窗前会自动探测实际组）")
    parser.add_argument("--no-probe", action="store_true",
                        help="不做组播组探测，直接用配置/推导出的组")
    parser.add_argument("--probe-timeout", type=float, default=1.5,
                        help="组播组探测时长（秒，默认 1.5）")
    args, client_extra = parser.parse_known_args(argv)

    conf = HERE / start.CONF_NAME
    items = start.load_classrooms(conf)
    if not items:
        print(f"还没有配置任何教室。请编辑：{conf}")
        return 1

    profile = None
    if args.classroom:
        profile = start.choose_classroom(items, args.classroom)
    elif not args.ask:
        profile = start.detect_classroom(items)
        if profile:
            print(f"  自动识别教室：{profile['name']}（本机网段与教师机 {profile['teacher']} 同段）")
        else:
            print("  没能从本机 IP 唯一确定教室，请手动选一次：")
            profile = start.choose_classroom(items, None)
    else:
        profile = start.choose_classroom(items, None)

    if profile is None:
        if args.classroom:
            print("（用 --list 可以查看全部已配置教室）")
            return 1
        print("已取消。")
        return 0

    teacher, local = start.resolve_profile(profile)
    if not local:
        print("没能确定本机课堂网卡 IP，请用 start.py --local 手动指定。")
        return 1

    verify_port = native.screen_verify_port()

    # 组播组：命令行 > classrooms.conf 的 mcast-group > 教师机 IP 推导
    mcast_group = args.mcast_group or profile["opts"].get("mcast-group")

    try:
        show_mods, show_vk = parse_hotkey(args.show_key)
        hide_mods, hide_vk = parse_hotkey(args.hide_key)
        fs_mods, fs_vk = parse_hotkey(args.fullscreen_key)
    except ValueError as error:
        print(f"热键解析失败：{error}")
        return 1

    renderer = ("ffmpeg" if args.ffmpeg
                else "native" if args.native_renderer else "auto")
    resolved = broadcast.pick_renderer(renderer)
    renderer_label = ("官方 ScreenRender.exe" if resolved == "native"
                      else "ffmpeg + SDL（官方播放器不可用）")

    print("=" * 62)
    print("  噢易课堂助手 · 后台模式")
    print("=" * 62)
    print(f"  教室      : {profile['name']}")
    print(f"  教师机    : {teacher}")
    print(f"  本机 IP   : {local}")
    print(f"  广播组播  : {native.multicast_group(teacher, mcast_group)}:{DEFAULT_UDP_PORT}"
          + ("（配置文件指定）" if mcast_group else ""))
    print(f"  广播窗口  : {renderer_label}")
    print(f"  屏幕缩略图: {oseasy_client.thumbnail_label(client_extra)}")
    print("-" * 62)
    print(f"  {args.show_key:<14} 窗口化广播")
    print(f"  {args.fullscreen_key:<14} 全屏广播")
    print(f"  {args.hide_key:<14} 关闭广播窗口")
    print("  Ctrl+C        退出")
    print("=" * 62)

    session = None
    if not args.no_login:
        session = ClientSession(teacher, local, client_extra)
        session.start()
        print("  后台登录已启动，等待教师端确认……\n")
    else:
        print("  （--no-login：不登录教师机，教师端会显示离线）\n")

    window = BroadcastWindow(
        teacher, local,
        http_port=args.http_port,
        udp_port=DEFAULT_UDP_PORT,
        verify_port=verify_port,
        fullscreen=args.fullscreen,
        renderer=renderer,
        mcast_group=mcast_group,
        probe_group=not args.no_probe,
        probe_timeout=args.probe_timeout)

    ok_show = user32.RegisterHotKey(None, 1, show_mods, show_vk)
    ok_hide = user32.RegisterHotKey(None, 2, hide_mods, hide_vk)
    ok_fs = user32.RegisterHotKey(None, 3, fs_mods, fs_vk)
    if not ok_show or not ok_hide or not ok_fs:
        print("  [!] 热键注册失败——可能被别的程序占用了，换一组再试：")
        print("      python watch.py <教室> --show-key alt+1 --hide-key alt+2"
              " --fullscreen-key ctrl+alt+3")
        if session:
            session.stop()
        return 1
    print("  热键已生效，可以最小化这个窗口。\n")

    def _stop_handler(signum, frame):
        stopping.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop_handler)
        except (ValueError, OSError):
            pass

    msg = wintypes.MSG()
    restarts = 0
    try:
        while not stopping.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY:
                    stamp = time.strftime('%H:%M:%S')
                    if msg.wParam == 1:
                        print(f"  [{stamp}] 热键：窗口化广播")
                        window.set_fullscreen(False)
                    elif msg.wParam == 2:
                        print(f"  [{stamp}] 热键：关闭广播窗口")
                        window.close()
                    elif msg.wParam == 3:
                        print(f"  [{stamp}] 热键：全屏广播")
                        window.set_fullscreen(True)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if session and not session.alive() and not stopping.is_set():
                restarts += 1
                if restarts > 10:
                    print("  [!] 后台登录反复退出，已放弃重启。")
                    print("      请单独运行 `start.py <教室> --check` 排查。")
                    break
                print(f"  [!] 后台登录进程退出了，正在重启（第 {restarts} 次）……")
                time.sleep(1)
                if not stopping.is_set():
                    session.start()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n  正在退出……")
    finally:
        stopping.set()
        for hotkey_id in (1, 2, 3):
            user32.UnregisterHotKey(None, hotkey_id)
        window.shutdown()
        if session:
            session.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
