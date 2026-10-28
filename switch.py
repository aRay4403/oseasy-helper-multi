#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选教室 → 一键切换教师机 IP（官方学生端模式）。

跟 ``official.py`` 的分工：

* ``official.py`` 是「命令行 / 按本机网段自动识别」的入口；
* 本文件是**交互式选教室**的入口，多了一步「教师机 IP 会变的教室」处理 ——
  比如「网络频道12」：网段固定 ``10.144.61.0/24``，但教师机具体是哪台每次都可能不同，
  所以让你手动填（或者直接回车让它扫一遍这个网段）。

用法::

    python switch.py                    # 弹菜单选教室
    python switch.py 206                # 直接切某间（名字片段也行）
    python switch.py 网络频道12         # 会提示你填教师机 IP
    python switch.py 网络频道12 --teacher-ip 10.144.61.77
    python switch.py --list             # 只列出教室
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import socket
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import native       # noqa: E402
import netcfg       # noqa: E402
import official     # noqa: E402
import start        # noqa: E402

CONF = HERE / start.CONF_NAME
LAST_IP_FILE = HERE / "configs" / "last-teacher-ip.json"
TRUTHY = {"1", "true", "yes", "y", "on"}


# --------------------------------------------------------------------------
# 教室属性
# --------------------------------------------------------------------------

def _dw(text: str) -> int:
    """显示宽度（中文/全角算 2 格），用来对齐菜单。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _dw(text))


def is_manual(profile: dict) -> bool:
    """这间教室的教师机 IP 是不是每次要手填。"""
    opts = profile.get("opts") or {}
    if str(opts.get("manual-ip", "")).strip().lower() in TRUTHY:
        return True
    return bool(str(opts.get("ip-prefix", "")).strip())


def ip_prefix(profile: dict) -> str:
    """这间教室固定的网段前缀，带末尾的点，例如 ``10.144.61.``。"""
    raw = str((profile.get("opts") or {}).get("ip-prefix", "")).strip()
    if raw:
        return raw if raw.endswith(".") else raw + "."
    return ".".join(profile["teacher"].split(".")[:3]) + "."


def _normalize_ip(raw: str, prefix: str) -> str | None:
    """把用户输入变成完整 IP；只接受 ``prefix`` 网段内的地址。

    支持三种写法：``65``（只填末段）、``10.144.61.65``（完整）、
    前后空格随意。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        last = int(raw)
        return f"{prefix}{last}" if 1 <= last <= 254 else None
    try:
        addr = str(ipaddress.IPv4Address(raw))
    except ValueError:
        return None
    return addr if addr.startswith(prefix) else None


# --------------------------------------------------------------------------
# 记住上次用的 IP（手填教室下次直接回车就能沿用）
# --------------------------------------------------------------------------

def _load_last_ips() -> dict:
    try:
        return json.loads(LAST_IP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_last_ip(name: str, ip: str) -> None:
    data = _load_last_ips()
    data[name] = ip
    try:
        LAST_IP_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_IP_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------
# 扫网段找教师机
# --------------------------------------------------------------------------

def scan_teachers(prefix: str, timeout: float = 0.7) -> list[str]:
    """并发探一遍 ``prefix``1-254 的 9003 端口，返回能连上的（按 IP 排序）。"""
    base = prefix.rstrip(".")
    targets = [f"{base}.{i}" for i in range(1, 255)]

    def probe(ip: str) -> str | None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((ip, official.MANAGE_PORT))
            return ip
        except OSError:
            return None
        finally:
            sock.close()

    found: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as pool:
        for result in pool.map(probe, targets):
            if result:
                found.append(result)
    return sorted(found, key=lambda x: ipaddress.IPv4Address(x))


def _auto_find(prefix: str) -> str | None:
    print(f"    正在扫描 {prefix}1-254 的 {official.MANAGE_PORT} 端口……")
    started = time.time()
    found = scan_teachers(prefix)
    print(f"    扫了 {time.time() - started:.1f} 秒，找到 {len(found)} 台")
    if not found:
        return None
    if len(found) == 1:
        print(f"    ✓ 教师机：{found[0]}")
        return found[0]
    print("    这个网段有好几台开着 9003，挑一台：")
    for n, ip in enumerate(found, 1):
        print(f"      {n}) {ip}")
    try:
        raw = input("    用哪台 [1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw:
        return found[0]
    if raw.isdigit() and 1 <= int(raw) <= len(found):
        return found[int(raw) - 1]
    return _normalize_ip(raw, prefix) or found[0]


# --------------------------------------------------------------------------
# 交互
# --------------------------------------------------------------------------

def ask_teacher_ip(profile: dict) -> str | None:
    """让用户填这间教室的教师机 IP（回车 = 自动扫网段）。"""
    prefix = ip_prefix(profile)
    last = _load_last_ips().get(profile["name"])
    print()
    print(f"  教室「{profile['name']}」的教师机 IP 不是固定的，网段固定 {prefix}0/24。")
    print(f"    直接回车 = 自动扫描 {prefix}1-254 找教师机（约 2 秒）")
    print(f"    只填最后一段也行：65 → {prefix}65")
    if last:
        print(f"    上次用的是 {last}（想沿用就直接填 {last.split('.')[-1]}）")

    while True:
        try:
            raw = input("  教师机 IP [回车=自动扫描]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            found = _auto_find(prefix)
            if found:
                print()
                return found
            print(f"    没扫到。可能老师还没开教师端，也可能不在 {prefix}0/24 里。")
            print("    可以直接手填（也可以按 Ctrl+C 退出）。")
            continue
        ip = _normalize_ip(raw, prefix)
        if ip:
            print()
            return ip
        print(f"    格式不对：要 {prefix}x 这样的地址，比如 {prefix}65")


def _local_hint(item: dict) -> str:
    """菜单里显示的「本机会用哪个地址」。"""
    raw = _local_ip_from_config(item) or ""
    if raw.strip().lower() in ("ask", "?", "manual"):
        return "本机 手填"
    if raw:
        return f"本机 {raw}"
    return ""


def pick_classroom(items: list[dict]) -> dict | None:
    """弹菜单让用户挑教室；回车 = 本机网段自动识别出来的那间。"""
    here = start.detect_classroom(items)
    print()
    print("  噢易课堂 · 选择教室")
    print("  " + "-" * 66)
    for n, item in enumerate(items, 1):
        if is_manual(item):
            target = f"{ip_prefix(item)}x（每次手动填）"
        else:
            target = item["teacher"]
        mark = "   ← 本机所在" if here and item["name"] == here["name"] else ""
        print(f"  {n:>2}) {_pad(item['name'], 14)} {_pad(target, 26)} {_local_hint(item)}{mark}")
    print("  " + "-" * 66)
    hint = f"（直接回车 = {here['name']}）" if here else ""
    try:
        raw = input(f"  选哪个教室？{hint}\n  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw:
        return here
    if raw.lower() in ("q", "quit", "exit"):
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(items):
        return items[int(raw) - 1]
    return start.choose_classroom(items, raw)


def list_classrooms(items: list[dict]) -> None:
    here = start.detect_classroom(items)
    print()
    for n, item in enumerate(items, 1):
        target = (f"{ip_prefix(item)}x" if is_manual(item) else item["teacher"])
        note = []
        if is_manual(item):
            note.append("教师机 IP 手动填")
        if here and item["name"] == here["name"]:
            note.append("本机所在")
        suffix = f"   [{', '.join(note)}]" if note else ""
        print(f"  {n:>2}) {_pad(item['name'], 14)} {_pad(target, 20)} "
              f"{_local_hint(item)}{suffix}")
    print()


# --------------------------------------------------------------------------
# 本机网卡 IP：换教室时得跟着改到同一个网段
# --------------------------------------------------------------------------

def _local_ip_from_config(profile: dict) -> str | None:
    raw = str((profile.get("opts") or {}).get("local-ip", "")).strip()
    return raw or None


def _gateway_from_config(profile: dict) -> str | None:
    raw = str((profile.get("opts") or {}).get("gateway", "")).strip()
    return raw or None


def _last_local_ip(name: str) -> str | None:
    return _load_last_ips().get(f"{name}#local")


def _save_last_local(name: str, ip: str) -> None:
    data = _load_last_ips()
    data[f"{name}#local"] = ip
    try:
        LAST_IP_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_IP_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def ensure_local_ip(profile: dict, teacher: str, wanted: str | None = None,
                    interactive: bool = True) -> tuple[bool, str | None, str]:
    """让本机网卡落到这间教室该用的地址上。

    这台机器是**静态 IP**（DHCP 关着），而且教室的网口是「端口 + IP」绑定的：
    实测把 10.140.24.234 改成 10.140.24.250，网关立刻不通，改回来才好。
    所以每间教室能用哪个本机 IP 是**固定的**，只能照着配置来：
    ``classrooms.conf`` 里给每间写 ``local-ip=10.140.x.234``；写 ``local-ip=ask``
    的教室（网络频道12）每次问你要。改完 ping 不通网关会自动回滚，不会把自己网断掉。

    返回 ``(成功?, 本机IP, 说明)``。
    """
    conf = netcfg.pick_interface(teacher)
    if not conf:
        return False, None, "读不到网卡配置"

    subnet = ".".join(teacher.split(".")[:3]) + "."
    prefix = 24
    gateway = _gateway_from_config(profile) or (subnet + "1")

    raw_conf = _local_ip_from_config(profile) or ""
    ask = raw_conf.strip().lower() in ("ask", "?", "manual")
    candidate = (wanted or (None if ask else raw_conf) or "").strip() or None

    # 已经正好是目标地址：什么都不用做
    if candidate and candidate == conf["ip"]:
        print(f"  本机 IP 已经是 {candidate}，不用改。")
        return True, candidate, "already"
    # 没指定目标、而且本来就在这个网段：也不用动
    if not candidate and conf["ip"].startswith(subnet):
        print(f"  本机 IP {conf['ip']} 已经和教师机同网段，不用改。")
        return True, conf["ip"], "already"

    print()
    print(f"  本机 IP 现在是 {conf['ip']}，这间教室要用 {subnet}0/24 里的地址 —— 先改网卡。")
    print("  （教室网口是端口+IP 绑定的，改错就断网；脚本会 ping 网关验证，不通自动回滚）")

    if candidate and not candidate.startswith(subnet):
        print(f"  [!] 指定的本机 IP {candidate} 不在 {subnet}0/24，忽略。")
        candidate = None

    if not candidate and not interactive:
        return False, conf["ip"], f"网段不同，但没有指定本机 IP（要 {subnet}x）"

    remembered = _last_local_ip(profile["name"])
    default = remembered if (remembered and remembered.startswith(subnet)) \
        else netcfg.target_ip(conf["ip"], teacher)

    for attempt in range(3):
        if not candidate:
            if attempt == 0:
                print(f"    直接回车 = 先试 {default}")
                print(f"    只填最后一段也行：88 → {subnet}88")
            try:
                raw = input(f"  这间教室的本机 IP [回车={default}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return False, conf["ip"], "已取消"
            candidate = _normalize_ip(raw, subnet) or default

        if official.student_running():
            print("  先停掉学生端（它绑着旧 IP 的端口）……")
            official.stop_student()

        result = netcfg.apply_ip(conf, candidate, prefix, gateway)
        if result["ok"]:
            _save_last_local(profile["name"], candidate)
            return True, candidate, ""

        print(f"  [!] {candidate} 试了不通：{result['message']}")
        if result["rolled_back"]:
            print(f"      已回滚到 {conf['ip']}，网络还在。")
        if attempt == 2:
            return False, conf["ip"], result["message"]
        print("      这间教室的本机 IP 大概不是这个，换一个试试。")
        candidate = None

    return False, conf["ip"], "试了 3 次都不通"


def _relaunch_as_admin(argv: list[str]) -> bool:
    """以管理员身份重新跑一遍自己（改网卡 IP 必须提权）。"""
    import ctypes
    params = " ".join(f'"{a}"' if " " in a else a for a in [str(Path(__file__).resolve())] + argv)
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, str(HERE), 1)
        return rc > 32
    except Exception:
        return False


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    official._force_utf8()
    parser = argparse.ArgumentParser(
        prog="switch.py",
        description="选教室 → 一键切换教师机 IP（官方 MmcStudent 模式）。",
    )
    parser.add_argument("classroom", nargs="?", help="教室名（片段也行）；不填就弹菜单")
    parser.add_argument("--teacher-ip", help="直接指定教师机 IP，跳过输入（手填教室用）")
    parser.add_argument("--local-ip", help="直接指定本机要用的 IP（跳过输入）")
    parser.add_argument("--no-net", action="store_true", help="不动本机网卡（自己已经改好了）")
    parser.add_argument("--restore-net", action="store_true",
                        help="把本机网卡还原成上次改之前的样子，然后退出")
    parser.add_argument("--net-status", action="store_true", help="只显示本机网卡配置，然后退出")
    parser.add_argument("--list", action="store_true", help="只列出教室，不切换")
    parser.add_argument("--no-hotkey", action="store_true", help="只切教室，不挂热键")
    parser.add_argument("--no-restart", action="store_true", help="不改配置、不动学生端，只挂热键")
    parser.add_argument("--status", action="store_true", help="只读：看当前配置、进程、播放器窗口")
    args = parser.parse_args(argv)

    if args.net_status:
        for conf in netcfg.read_configs():
            flag = "DHCP" if conf["dhcp"] else "静态"
            print(f"  [{conf['index']}] {conf['alias']}  {conf['ip']}/{conf['prefix']}"
                  f"  网关={conf['gateway'] or '-'}  DNS={','.join(conf['dns']) or '-'}  {flag}")
        print(f"  管理员权限：{'有' if netcfg.is_admin() else '没有（改 IP 需要）'}")
        return 0

    if args.restore_net:
        return 0 if netcfg.restore_from_backup() else 1

    # 改网卡 IP 要管理员权限：没提权就带着原参数重开一个提权窗口
    if not args.no_net and not netcfg.is_admin():
        print("  改本机 IP 需要管理员权限，正在以管理员身份重新打开……（会弹 UAC）")
        if _relaunch_as_admin(argv if argv is not None else sys.argv[1:]):
            return 0
        print("  [!] 提权被取消。可以右键「以管理员身份运行」，"
              "或者加 --no-net 自己先改好 IP。")
        return 1

    if not native.BASE.is_dir():
        print(f"没找到噢易学生端安装目录：{native.BASE}")
        return 1

    items = start.load_classrooms(CONF)
    if not items:
        print(f"还没有配置任何教室。请编辑：{CONF}")
        return 1

    if args.list:
        list_classrooms(items)
        return 0

    profile = (start.choose_classroom(items, args.classroom) if args.classroom
               else pick_classroom(items))
    if profile is None:
        print("已取消。")
        return 0
    print()

    # 教师机 IP：手填教室必须问一次；其它教室用配置里的值
    teacher = profile["teacher"]
    if args.teacher_ip:
        if is_manual(profile):
            fixed = _normalize_ip(args.teacher_ip, ip_prefix(profile))
            if not fixed:
                print(f"  [!] {args.teacher_ip} 不在 {ip_prefix(profile)}0/24 网段内。")
                return 1
            teacher = fixed
        else:
            teacher = args.teacher_ip
    elif is_manual(profile):
        picked = ask_teacher_ip(profile)
        if not picked:
            print("已取消。")
            return 0
        teacher = picked

    _save_last_ip(profile["name"], teacher)

    # 本机 IP：教室网段不同就得跟着改，否则连不上教师机
    local_ip: str | None = None
    if not args.no_net and not args.status and not args.no_restart:
        ok, local_ip, note = ensure_local_ip(
            profile, teacher,
            wanted=args.local_ip,
            interactive=not args.local_ip,
        )
        if not ok:
            print(f"  [!] 本机 IP 没切成功：{note}")
            print("      可以自己手动改网卡，或者加 --no-net 跳过这一步。")
            print("      想还原成上次改之前的样子：python switch.py --restore-net")
            return 1

    passthrough: list[str] = []
    if args.no_hotkey:
        passthrough.append("--no-hotkey")
    if args.no_restart:
        passthrough.append("--no-restart")
    if args.status:
        passthrough.append("--status")

    cmd = [profile["name"]]
    if teacher != profile["teacher"] or is_manual(profile):
        cmd += ["--teacher-ip", teacher]
    if local_ip:
        cmd += ["--local", local_ip]
    cmd += passthrough
    return official.main(cmd)


if __name__ == "__main__":
    sys.exit(main())
