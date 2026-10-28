#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键启动器：先选教室，再选要做什么。

把两条路线合到一个入口：

  oseasy-helper 路线（自己实现协议）
      收广播 / 登录（教师端显示在线）/ 网络体检
  广播窗口路线（默认官方播放器，不可用时自动降级 ffmpeg + SDL）
      弹出广播窗口（热键常驻版请用 watch.py）
  官方学生端路线（调用本机已装的学生端）
      官方播放器看广播 / 提交作业 / 启动学生端 / 状态

教室相关的配置（教师机 IP、本机 IP）会自动套用，
本机 IP 每次启动重新探测，所以换教室不用改配置。

用法::

    python main.py               交互式：选教室 -> 选功能
    python main.py 5-217         指定教室，再选功能
    python main.py 5-217 recv    教室 + 功能一步到位
    python main.py --list        列出所有教室和可用功能
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import native  # noqa: E402
import start  # noqa: E402

# (代号, 菜单文字, 走哪条路线)
ACTIONS: list[tuple[str, str, str]] = [
    ("official",  "上课：官方学生端 + 热键   ★ 推荐",              "official"),
    ("recv",      "登录 + 看广播   （oseasy-helper 自己实现）",    "helper"),
    ("video",     "只看广播（本机直播流，用播放器打开 URL）",      "helper"),
    ("check",     "网络体检          （测教师机管理端口）",        "helper"),
    ("window",    "弹出广播窗口      （官方播放器，坏了自动降级）", "window"),
    ("broadcast", "官方播放器看广播  （ScreenRender.exe）",        "native"),
    ("submit",    "提交作业          （需学生端已在运行）",        "native"),
    ("student",   "启动官方学生端    （MmcStudent）",             "native"),
    ("status",    "本机学生端状态    （只读）",                   "native"),
]
ACTION_MAP = {code: (label, route) for code, label, route in ACTIONS}


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def choose_action() -> str | None:
    print("\n请选择要做什么：")
    tags = {"helper": "[自己实现]", "window": "[广播窗口]",
            "native": "[官方学生端]", "official": "[推荐]"}
    for i, (_, label, route) in enumerate(ACTIONS, 1):
        print(f"  {i}) {label}  {tags.get(route, '')}")
    print("  （输入编号；q 退出）")
    try:
        raw = input("选择 > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if raw.lower() in ("q", "quit", "exit", ""):
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(ACTIONS):
        return ACTIONS[int(raw) - 1][0]
    # 也允许直接输入代号
    if raw.lower() in ACTION_MAP:
        return raw.lower()
    print("  输入无法识别。")
    return None


def run_helper(profile_name: str, code: str, extra: list[str] | None = None) -> int:
    """走 oseasy-helper 路线，复用 start.py 的全部逻辑。"""
    mode = {"recv": "student", "video": "video"}.get(code)
    argv = [profile_name]
    if code == "check":
        argv.append("--check")
    else:
        argv += ["--mode", mode]
    return start.main(argv + list(extra or []))


def run_window(profile: dict, extra: list[str] | None = None) -> int:
    """按选中的教室弹出广播窗口（默认官方播放器，不可用自动降级）。"""
    import broadcast  # 延迟导入，避免和 native.py 循环依赖

    teacher, local = start.resolve_profile(profile)
    if not local:
        print("没能确定本机课堂网卡 IP，请用 start.py --local 手动指定。")
        return 1
    mcast = profile["opts"].get("mcast-group")
    print(f"  教师机  : {teacher}")
    print(f"  本机 IP : {local}")
    print(f"  组播组  : {native.multicast_group(teacher, mcast)}:7778\n")

    fullscreen = "--fullscreen" in (extra or [])
    renderer = ("ffmpeg" if "--ffmpeg" in (extra or [])
                else "native" if "--native-renderer" in (extra or []) else "auto")
    window = broadcast.BroadcastWindow(teacher, local, fullscreen=fullscreen,
                                       renderer=renderer, mcast_group=mcast)
    if not window.show():
        window.shutdown()
        return 1
    print("按 Ctrl+C 关闭窗口并退出。")
    try:
        import time
        while window.is_open():
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    window.shutdown()
    return 0


def run_native(code: str) -> int:
    """走官方学生端路线，复用 native.py 的逻辑。"""
    if not native.BASE.is_dir():
        print(f"没找到噢易学生端安装目录：{native.BASE}")
        return 1
    if code == "status":
        return native.action_status()
    if code == "student":
        return native.action_student(dry_run=False)
    if code == "submit":
        return native.action_submit(dry_run=False)
    if code == "broadcast":
        return native.action_broadcast(fullscreen=False, dry_run=False)
    return 1


def run_official(profile_name: str) -> int:
    """官方学生端模式：改配置换教室 + 重启 MmcStudent + 挂热键（见 official.py）。

    走 switch.py 而不是直接调 official.py —— 「网络频道12」这种教师机 IP
    每次要手填的教室，多一层 IP 输入。
    """
    import switch  # 延迟导入，避免循环依赖
    return switch.main([profile_name])


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="一键启动器：按教室套用配置，再选择要执行的功能。",
    )
    parser.add_argument("classroom", nargs="?", help="教室名（支持片段匹配）")
    parser.add_argument("action", nargs="?", help="功能代号（省略则进入菜单）")
    parser.add_argument("--list", action="store_true", help="列出教室与可用功能")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令不执行（helper 路线）")
    parser.add_argument("--no-probe", action="store_true", help="跳过教师机连通性预检（helper 路线）")
    parser.add_argument("--fullscreen", action="store_true", help="广播窗口铺满屏幕并置顶（window 路线）")
    args = parser.parse_args(argv)

    extra = []
    if args.dry_run:
        extra.append("--dry-run")
    if args.no_probe:
        extra.append("--no-probe")
    if args.fullscreen:
        extra.append("--fullscreen")

    conf = HERE / start.CONF_NAME
    items = start.load_classrooms(conf)

    if args.list:
        print(f"教室配置：{conf}")
        if items:
            for i in items:
                extra = "".join(f"  {k}={v}" for k, v in i["opts"].items())
                print(f"  - {i['name']:<20} 教师机 {i['teacher']}{extra}")
        else:
            print("  （还没有配置教室）")
        print("\n可用功能：")
        for code, label, route in ACTIONS:
            print(f"  {code:<10} {label}")
        return 0

    if not items:
        print(f"还没有配置任何教室。\n请打开这个文件，按里面的示例填一行：\n  {conf}")
        return 1

    # 1) 选教室：给了名字就按名字，否则按本机 IP 自动识别，识别不出来才弹菜单
    if args.classroom:
        profile = start.choose_classroom(items, args.classroom)
    else:
        profile = start.detect_classroom(items)
        if profile:
            print(f"  自动识别教室：{profile['name']}（教师机 {profile['teacher']}）")
        else:
            profile = start.choose_classroom(items, None)
    if profile is None:
        if args.classroom:
            print("（用 --list 可以查看全部已配置教室）")
            return 1
        print("已取消。")
        return 0

    # 2) 选功能
    code = args.action.lower() if args.action else None
    if code and code not in ACTION_MAP:
        print(f"没有这个功能：{args.action}\n用 --list 可以看全部功能。")
        return 1
    if not code:
        code = choose_action()
        if not code:
            print("已取消。")
            return 0

    label, route = ACTION_MAP[code]
    print(f"\n>>> 教室：{profile['name']}   功能：{label}\n")

    if route == "helper":
        return run_helper(profile["name"], code, extra)
    if route == "window":
        return run_window(profile, extra)
    if route == "official":
        return run_official(profile["name"])
    return run_native(code)


if __name__ == "__main__":
    raise SystemExit(main())
