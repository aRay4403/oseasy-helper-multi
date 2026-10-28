#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调用本机噢易学生端的原生功能。

不重新实现协议，而是直接驱动本机已安装的官方学生端及其组件：

    屏幕广播接收 -> 官方 ScreenRender.exe（或由学生端自动拉起）
    提交作业     -> 官方 SumbitFile.exe（向学生端主窗口发消息，弹出提交框）
    启动学生端   -> 官方 MmcStudent.exe

用法::

    python native.py status      只读：教师机 IP、组播参数、进程与窗口状态
    python native.py student     启动官方学生端
    python native.py submit      打开「提交作业」对话框（需学生端已在运行）
    python native.py broadcast   用官方播放器接收屏幕广播
    python native.py sync        把 user.conf 里的教师机 IP 同步进 classrooms.conf
    python native.py --dry-run <动作>   只打印命令，不执行
"""
from __future__ import annotations

import argparse
import ctypes
import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(r"C:\Program Files (x86)\Os-Easy\os-easy multicast teaching system")
MMC_DIR = Path(os.environ.get("APPDATA", "")) / "Mmc"
USER_CONF = MMC_DIR / "user.conf"
STUDENT_LOG = MMC_DIR / "Student.log"
CLASSROOMS_CONF = Path(__file__).resolve().parent / "classrooms.conf"

STUDENT_WND_CLASS = "@UIStudentMainWnd"
STUDENT_EXES = ("MmcStudent.exe", "Student.exe")
SUBMIT_EXE = "SumbitFile.exe"
RENDER_EXE = "ScreenRender.exe"

DEFAULT_UDP_PORT = 7778
DEFAULT_VERIFY_PORT = 7788
STUDENT_KEYWORDS = ("mmcstudent", "student", "lisshelper", "assisthelper",
                    "mmpc", "multiclient", "screenrender", "sumbitfile")


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------
# 读取本机学生端配置
# --------------------------------------------------------------------------

def read_user_conf() -> dict:
    """解析 %APPDATA%\\Mmc\\user.conf 里的 /键/值/ 行。"""
    if not USER_CONF.exists():
        return {}
    text = USER_CONF.read_text(encoding="utf-8", errors="replace")
    return {m.group(1): m.group(2)
            for m in re.finditer(r"/([A-Za-z0-9_]+)/([^/]*)/", text)}


def teacher_ip_from_conf() -> str | None:
    conf = read_user_conf()
    for key in ("MutiTeacherIp", "MultiTeacherIp", "TeacherIp"):
        value = conf.get(key)
        if value:
            try:
                return str(ipaddress.IPv4Address(value))
            except ValueError:
                pass
    return None


def screen_verify_port() -> str:
    """学生端屏幕校验端口；``user.conf`` 里是 0 或空时回退到默认 7788。

    官方客户端只有真正连过教师机才会把端口写进 ``user.conf``；没写过、
    写过 0、或者写的是上一间教室的值时，都不能原样传给 ``ScreenRender.exe``
    （实测会把 ``verityPort: 0`` 传下去，丢掉校验握手）。
    """
    value = (read_user_conf().get("ScreenUdpVerityPort") or "").strip()
    if value.isdigit() and int(value) > 0:
        return value
    return str(DEFAULT_VERIFY_PORT)


def homework_server_ip() -> str | None:
    """从 Student.log 里取最后一条 home_work_server_ip。"""
    if not STUDENT_LOG.exists():
        return None
    text = STUDENT_LOG.read_text(encoding="gbk", errors="replace")
    hits = re.findall(r'"home_work_server_ip"\s*:\s*"([^"]+)"', text)
    return hits[-1] if hits else None


def local_ip_for(teacher: str) -> str | None:
    """不发包，问系统去教师机走哪块网卡。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((teacher, 9003))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def multicast_group(teacher: str, override: str | None = None) -> str | None:
    """广播组播组地址。

    默认由教师机 IP 推导：229.1.<第3段>.<第4段>（知行楼那几间实测如此）。
    但组播组实际由教师端的「网络频道」决定，不同教室推导规则可能不同
    （例如「网络频道12」那间实测为 229.9.61.65）。
    所以支持用 override 显式指定，从 classrooms.conf 的
    `mcast-group=229.x.y.z` 进来。
    """
    if override:
        override = str(override).strip()
        try:
            addr = int(ipaddress.IPv4Address(override))
        except ValueError:
            print(f"  [警告] mcast-group {override!r} 不是合法 IPv4 地址，已按教师机 IP 推导。")
        else:
            if not 0xE0000000 <= addr <= 0xEFFFFFFF:
                print(f"  [警告] mcast-group {override!r} 不在组播段 224.0.0.0~239.255.255.255，"
                      "已按教师机 IP 推导。")
            else:
                return str(ipaddress.IPv4Address(override))
    try:
        o = str(ipaddress.IPv4Address(teacher)).split(".")
    except ValueError:
        return None
    return f"229.1.{o[2]}.{o[3]}"


def multicast_groups_from_log(limit: int = 4) -> list[str]:
    """从官方学生端日志里捞出现过的组播组（当候选，不做结论）。

    ``Student.log`` 是 GBK，里面 ``CVeriReceiver ... Ip[229.x.y.z]`` 记录了
    官方客户端当时用的组播组。注意它**可能是上一节课的**，只当候选之一。
    """
    if not STUDENT_LOG.exists():
        return []
    try:
        raw = STUDENT_LOG.read_bytes()
    except OSError:
        return []
    text = None
    for encoding in ("gbk", "utf-8", "utf-16-le"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return []
    groups: list[str] = []
    for match in re.finditer(r"\b(22[4-9]|23[0-9])\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text):
        group = match.group(0)
        if group not in groups:
            groups.append(group)
    # 越靠后的越新，倒序取最近的若干个
    return list(reversed(groups))[:limit]


def running_student_processes() -> list[dict]:
    try:
        import psutil
    except ImportError:
        return []
    out = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (proc.info["name"] or "")
            if name.lower().replace(".exe", "") in [k for k in STUDENT_KEYWORDS]:
                out.append(proc.info)
            elif any(k in name.lower() for k in ("mmcstudent", "lisshelper", "assisthelper")):
                out.append(proc.info)
        except Exception:
            continue
    return out


def student_window_handle() -> int:
    try:
        return ctypes.windll.user32.FindWindowW(STUDENT_WND_CLASS, None) or 0
    except Exception:
        return 0


# --------------------------------------------------------------------------
# 动作
# --------------------------------------------------------------------------

def action_status() -> int:
    print("=" * 60)
    print("  本机噢易学生端状态")
    print("=" * 60)

    print(f"  安装目录      : {'✓ ' + str(BASE) if BASE.is_dir() else '✗ 未找到'}")
    if not BASE.is_dir():
        print("  没找到噢易学生端安装目录，后续动作无法进行。")
        return 1
    print(f"  配置文件      : {'✓' if USER_CONF.exists() else '✗'} {USER_CONF}")
    print(f"  管理员权限    : {'是' if is_admin() else '否（提交作业/启动学生端通常需要）'}")

    conf = read_user_conf()
    teacher = teacher_ip_from_conf()
    print("\n[配置]")
    print(f"  教师机 IP     : {teacher or '（user.conf 里没读到）'}")
    print(f"  传输方式      : {conf.get('TransferType', '?')}")
    print(f"  组播开关      : {conf.get('Multicast', '?')}")
    verify_port = screen_verify_port()
    print(f"  广播校验端口  : {verify_port}")

    if teacher:
        group = multicast_group(teacher)
        local = local_ip_for(teacher)
        print(f"  广播组播组    : {group}:{DEFAULT_UDP_PORT}")
        print(f"  本机出口 IP   : {local or '（无路由）'}")

    hw = homework_server_ip()
    print(f"  作业服务器    : {hw or '（日志里没有）'}"
          + ("   ← 未启用，提交作业可能不可用" if hw in ("0.0.0.0", None) else ""))

    print("\n[进程]")
    procs = running_student_processes()
    if procs:
        for p in procs:
            print(f"  pid={p['pid']:<7} {p['name']}")
    else:
        print("  （学生端相关进程都没在运行）")

    hwnd = student_window_handle()
    print("\n[窗口]")
    if hwnd:
        print(f"  学生端主窗口 {STUDENT_WND_CLASS}  : 存在 (hwnd=0x{hwnd:X})")
        print("  → 提交作业可用：python native.py submit")
    else:
        print(f"  学生端主窗口 {STUDENT_WND_CLASS}  : 不存在")
        print("  → 提交作业需要先启动学生端：python native.py student")

    print("\n[官方播放器]")
    if screen_render_is_broken():
        print(f"  {RENDER_EXE:<16}: ✗ 导入表已损坏 → 加载必报 WinError 193")
        print("  → 广播窗口请改用：python broadcast.py show  （ffmpeg + SDL）")
    else:
        print(f"  {RENDER_EXE:<16}: ✓ 导入表看起来正常")

    print("\n[可用动作]")
    print("  python native.py student     启动官方学生端")
    print("  python native.py submit      打开提交作业对话框")
    print("  python native.py broadcast   接收屏幕广播（坏则自动走 ffmpeg）")
    return 0


def screen_render_is_broken() -> bool:
    """ScreenRender.exe 的导入表是否被掏空了（本机就是这种情况）。

    判断依据：一个正常的 MSVC 程序，.rdata 里必然有 KERNEL32.dll / USER32.dll
    这些导入 DLL 名字符串。一个都找不到，说明导入表已经损坏，加载必然失败。
    """
    path = BASE / RENDER_EXE
    if not path.exists():
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return not any(data.count(name.encode()) for name in
                   ("KERNEL32.dll", "USER32.dll", "GDI32.dll",
                    "MSVCP140.dll", "VCRUNTIME140.dll"))


def _launch(exe_name: str, args: list[str] | None, dry_run: bool) -> int:
    path = BASE / exe_name
    if not path.exists():
        print(f"找不到 {path}")
        return 1
    cmd = [str(path), *(args or [])]
    print("将要执行：")
    print("  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    if dry_run:
        return 0
    try:
        subprocess.Popen(cmd, cwd=str(BASE))
    except OSError as error:
        print(f"启动失败：{error}")
        if getattr(error, "winerror", None) == 193:
            print()
            print("  WinError 193 = 这个 exe 不是有效的 Win32 程序，加载器拒绝加载它。")
            if screen_render_is_broken():
                print(f"  实测 {exe_name} 的导入表已被破坏（找不到任何导入 DLL 名字符串），")
                print("  代码段和 IAT 却是好的——这不是参数/权限/位数问题，")
                print("  重装噢易之前这个文件永远起不来。")
            print("  替代方案：python broadcast.py show    （ffmpeg + SDL 弹窗，本机可用）")
            print("            或直接跑 python watch.py   用热键控制广播窗口")
        return 1
    print("已启动。")
    return 0


def action_student(dry_run: bool) -> int:
    for name in STUDENT_EXES:
        if (BASE / name).exists():
            return _launch(name, None, dry_run)
    print("安装目录里既没有 MmcStudent.exe 也没有 Student.exe。")
    return 1


def action_submit(dry_run: bool) -> int:
    if not student_window_handle() and not dry_run:
        print(f"学生端主窗口 {STUDENT_WND_CLASS} 不在，无法提交作业。")
        print("先运行：python native.py student")
        return 1
    return _launch(SUBMIT_EXE, None, dry_run)


def action_broadcast(fullscreen: bool, dry_run: bool, mcast_group: str | None = None) -> int:
    teacher = teacher_ip_from_conf()
    if not teacher:
        print("没能从 user.conf 读到教师机 IP，无法推导组播地址。")
        return 1
    local = local_ip_for(teacher)
    if not local:
        print(f"没能确定通往教师机 {teacher} 的本机网卡。")
        return 1

    group = multicast_group(teacher, mcast_group)
    print(f"  教师机   : {teacher}")
    print(f"  本机 IP  : {local}")
    print(f"  组播组   : {group}:{DEFAULT_UDP_PORT}")

    if screen_render_is_broken():
        print(f"  [!] {RENDER_EXE} 的导入表已被破坏，本机加载必然报 WinError 193。")
        print("      自动改用自带 ffmpeg + SDL 弹窗，画面来自 oseasy_helper 的直播流。")
        print()
        if dry_run:
            print("  （--dry-run：不真正启动）")
            return 0
        import broadcast  # 延迟导入：broadcast.py 也 import native，避免循环依赖
        window = broadcast.BroadcastWindow(teacher, local, fullscreen=fullscreen,
                                           mcast_group=mcast_group)
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

    conf = read_user_conf()
    verify = screen_verify_port()
    params = ("{#decoderName#:#h264#,#fullscreen#:%d,#local#:#%s#,#port#:%d,"
              "#remote#:#%s#,#teacher_ip#:0,#verityPort#:%s}"
              % (1 if fullscreen else 0, local, DEFAULT_UDP_PORT,
                 group, verify))
    print(f"  显示模式 : {'全屏（尊重老师设置）' if fullscreen else '窗口'}")
    return _launch(RENDER_EXE, [params], dry_run)


def action_sync(write: bool) -> int:
    teacher = teacher_ip_from_conf()
    if not teacher:
        print("没能从 user.conf 读到教师机 IP。")
        return 1
    print(f"本机学生端记录的教师机 IP：{teacher}")

    if not CLASSROOMS_CONF.exists():
        print(f"没有找到 {CLASSROOMS_CONF}")
        return 1
    text = CLASSROOMS_CONF.read_text(encoding="utf-8", errors="replace")
    entries = [ln for ln in text.splitlines()
               if ln.strip() and not ln.strip().startswith("#") and "=" in ln]
    matched = [ln for ln in entries if teacher in ln]
    print(f"classrooms.conf 里已有 {len(entries)} 个教室，其中 {len(matched)} 个用了这个 IP：")
    for ln in matched:
        print(f"  {ln.strip()}")
    if not matched:
        print("  该教师机 IP 还没有出现在配置里。")
        print(f"  建议手动加一行，或把 --write 加上让脚本追加（教室名默认「当前教室」）。")
        if write:
            with CLASSROOMS_CONF.open("a", encoding="utf-8") as fh:
                fh.write(f"\n当前教室 = {teacher}\n")
            print("  已追加：当前教室 = " + teacher)
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="native.py",
        description="调用本机已安装的噢易学生端原生功能（广播接收 / 提交作业 / 启动学生端）。",
    )
    parser.add_argument("action", nargs="?",
                        choices=("status", "student", "submit", "broadcast", "sync"),
                        default="status", help="要执行的动作（默认 status）")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不真正启动程序")
    parser.add_argument("--fullscreen", action="store_true",
                        help="广播以全屏显示（默认窗口模式）")
    parser.add_argument("--write", action="store_true", help="sync 时把教师机 IP 追加进 classrooms.conf")
    parser.add_argument("--mcast-group",
                        help="广播组播组地址（默认由教师机 IP 推导 229.1.<3>.<4>；"
                             "如「网络频道12」实测是 229.9.61.65，就用这个显式指定）")
    args = parser.parse_args(argv)

    if not BASE.is_dir():
        print(f"没找到噢易学生端安装目录：{BASE}")
        return 1

    if args.action == "status":
        return action_status()
    if args.action == "student":
        return action_student(args.dry_run)
    if args.action == "submit":
        return action_submit(args.dry_run)
    if args.action == "broadcast":
        return action_broadcast(args.fullscreen, args.dry_run, args.mcast_group)
    if args.action == "sync":
        return action_sync(args.write)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
