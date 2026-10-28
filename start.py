#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""噢易教室助手 · 多教室启动器

把「教室名 -> 教师机 IP（以及该教室的端口等参数）」写进 classrooms.conf，
本机课堂网卡 IP 每次启动自动探测，所以换教室只需要选一个教室名。

用法::

    python start.py                  交互式选择教室
    python start.py 5-217            按名称片段匹配教室
    python start.py 5-217 --check    网络体检：网卡、路由、端口全测一遍
    python start.py 5-217 --local 10.140.20.145     手动指定本机 IP
    python start.py --list           列出已配置的教室
    python start.py 5-217 --mode client              换用 client / video 子命令
    python start.py 5-217 --dry-run  只打印将要执行的命令，不真正运行

start.py 不认识的参数会原样透传给 oseasy-helper，且优先级高于配置文件::

    python start.py 5-217 --http-port 17779
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF_NAME = "classrooms.conf"
DEFAULT_MODE = "student"
DEFAULT_MANAGE_PORT = 9003

# native.multicast_group 用来展示/推导组播组。延迟到模块末尾 import 也行，
# 但 native.py 不 import start.py，这里直接顶层导入没有循环依赖。
try:
    import native
except ImportError:  # 只跑 start.py 自身单元测试时允许没有 native.py
    native = None  # type: ignore[assignment]

# 配置文件里可以写的 key=value，会转成对应的命令行参数
CONFIG_OPTS = {
    "port", "udp-port", "http-port",
    "name", "user", "mac",
    "reconnect-delay", "management-idle-timeout",
    "mcast-group",
    # 教师机 IP 会变的教室（如「网络频道12」）：每次让用户手动填具体 IP，网段固定
    "manual-ip", "ip-prefix",
    # 本机网卡（静态 IP，教室网口按「端口+IP」绑定）：这间教室该用的本机 IP / 网关
    "local-ip", "gateway",
    # 部署相关的连接参数，由封装层消化，不转给原项目的 client
    "node-port", "data-port", "receive-dir",
}
# 只有 student / client 认识这些
LOGIN_ONLY_OPTS = {"name", "user", "mac",
                   "reconnect-delay", "management-idle-timeout"}
# 这些是「封装层自己消化」的配置项，不能转成命令行参数传给原项目的 client
WRAPPER_ONLY_OPTS = {"manual-ip", "ip-prefix", "local-ip", "gateway",
                     "node-port", "data-port", "receive-dir"}

# 网卡名里出现这些词，大概率是虚拟/隧道网卡，排序时降权
VIRTUAL_HINTS = (
    "vmware", "virtualbox", "vbox", "hyper-v", "vethernet", "wsl",
    "loopback", "tap", "tun", "vpn", "zerotier", "tailscale", "docker",
    "bluetooth", "npcap", "teredo", "isatap",
)
# 网卡名里出现这些词，大概率是真实物理网卡，排序时加权
PHYSICAL_HINTS = ("ethernet", "以太网", "wlan", "wi-fi", "wifi", "无线", "本地连接")


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def python_exe() -> str:
    """返回一个装了依赖的 Python，用来拉起子进程（oseasy_helper / ffmpeg 包装）。

    优先用项目自带的 .venv（里面有 psutil），找不到再退回当前解释器。
    """
    for name in ("python.exe", "python"):
        candidate = HERE / ".venv" / "Scripts" / name
        if candidate.exists():
            return str(candidate)
    return sys.executable


# --------------------------------------------------------------------------
# 配置解析
# --------------------------------------------------------------------------

def load_classrooms(path: Path) -> list[dict]:
    """解析 classrooms.conf，返回 [{name, teacher, local, opts}, ...]。"""
    if not path.exists():
        return []
    items: list[dict] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        name, _, rest = line.partition("=")
        name = name.strip()
        tokens = [t.strip() for t in rest.split(",") if t.strip()]
        if not name or not tokens:
            continue
        teacher = tokens[0]
        local = None
        opts: dict[str, str] = {}
        for tok in tokens[1:]:
            if "=" in tok:
                key, _, value = tok.partition("=")
                key = key.strip().lower().replace("_", "-")
                if key not in CONFIG_OPTS:
                    print(f"  [警告] 「{name}」里有不认识的配置项：{key}=（已忽略）")
                    continue
                opts[key] = value.strip()
            else:
                local = tok  # 裸 IP = 本机固定 IP

        try:
            teacher = str(ipaddress.IPv4Address(teacher))
            if local:
                local = str(ipaddress.IPv4Address(local))
        except ValueError:
            print(f"  [跳过] 「{name}」的 IP 格式不对：{teacher!r}")
            continue
        items.append({"name": name, "teacher": teacher, "local": local, "opts": opts})
    return items


# --------------------------------------------------------------------------
# 网卡探测
# --------------------------------------------------------------------------

def route_source_ip(teacher: str, port: int = DEFAULT_MANAGE_PORT) -> str | None:
    """不发包，问操作系统：去教师机会走本机哪块网卡。

    UDP connect() 只设置对端地址、不产生任何流量，所以拿它查路由是安全的。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((teacher, port))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def subnet_relation(local_ip: str, teacher: str) -> int:
    """2 = 同 /24 网段，1 = 同 /16 网段，0 = 不同网段。"""
    try:
        addr = ipaddress.IPv4Address(local_ip)
    except ValueError:
        return 0
    for bits, mark in ((24, 2), (16, 1)):
        if addr in ipaddress.IPv4Network(f"{teacher}/{bits}", strict=False):
            return mark
    return 0


def net_relation_label(rel: int) -> str:
    return {2: "同网段 ✓", 1: "同 /16 网段", 0: "不同网段 ✗"}[rel]


def detect_local_ips(teacher: str | None = None) -> list[dict]:
    """列出本机可用的 IPv4 网卡，按「能不能连上教师机」排序。"""
    try:
        import psutil
    except ImportError:
        print("  找不到 psutil，无法自动探测网卡，请用 --local 手动指定本机 IP。")
        return []

    route_src = route_source_ip(teacher) if teacher else None
    stats = psutil.net_if_stats()
    found: list[dict] = []
    for iface, addrs in psutil.net_if_addrs().items():
        if not stats.get(iface) or not stats[iface].isup:
            continue
        low = iface.lower()
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue
            ip = addr.address
            if ip.startswith(("127.", "169.254.")) or ip == "0.0.0.0":
                continue
            score = 0
            if any(h in low for h in VIRTUAL_HINTS):
                score -= 50
            if any(h in low for h in PHYSICAL_HINTS):
                score += 30
            rel = subnet_relation(ip, teacher) if teacher else 0
            score += {2: 200, 1: 100, 0: 0}[rel]
            is_src = bool(route_src and ip == route_src)
            if is_src:
                score += 300
            found.append({"ip": ip, "iface": iface, "score": score,
                          "rel": rel, "is_route_src": is_src})
    found.sort(key=lambda d: (-d["score"], d["ip"]))
    return found


def choose_local(candidates: list[dict], preferred: str | None, forced: str | None) -> str | None:
    """决定用哪个本机 IP。"""
    if forced:
        return forced
    if not candidates:
        return None
    if preferred:  # 配置里写了「本机固定 IP」，优先用同前缀的
        prefix = preferred.rsplit(".", 1)[0] + "."
        hit = [c for c in candidates if c["ip"].startswith(prefix)]
        if hit:
            return hit[0]["ip"]
    if len(candidates) == 1:
        return candidates[0]["ip"]

    print("\n检测到多个可用网卡，请选择本机课堂网卡 IP：")
    for i, c in enumerate(candidates, 1):
        flag = "  <- 推荐" if i == 1 else ""
        print(f"  {i}) {c['ip']:<16} [{c['iface']}]  {net_relation_label(c['rel'])}{flag}")
    print("  （直接回车 = 用第 1 个）")
    try:
        raw = input("选择 > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw:
        return candidates[0]["ip"]
    if raw.isdigit() and 1 <= int(raw) <= len(candidates):
        return candidates[int(raw) - 1]["ip"]
    try:
        return str(ipaddress.IPv4Address(raw))
    except ValueError:
        print("  输入无法识别，改用推荐网卡。")
        return candidates[0]["ip"]


def subnet_warning(local: str, teacher: str) -> list[str]:
    """本机 IP 与教师机不在同一网络时，给出可操作的提示。"""
    rel = subnet_relation(local, teacher)
    if rel == 2:
        return []
    if rel == 1:
        return [f"  [注意] 本机 {local} 与教师机 {teacher} 只是同 /16 网段。",
                "         一般可以工作；若登录不上或收不到视频，检查是否该换另一块网卡。"]
    return [
        "",
        "  " + "!" * 54,
        f"  [!] 本机 IP {local} 与教师机 {teacher} 不在同一网络。",
        "  [!] 大概率你不在这个教室的网络里，连不上是正常的。",
        "",
        "      这个工具要求三件事同时成立：",
        "        1. 本机拿到教室网段的 IP",
        "        2. 教师机能连到本机的 9003 管理端口",
        "        3. 视频组播与教师机在同一个二层网络里",
        "",
        "      建议：接上教室网线 / 连上教室 Wi-Fi，确认本机 IP 变成",
        "            教室网段后重新运行。",
        "  " + "!" * 54,
        "",
    ]


# --------------------------------------------------------------------------
# 连通性
# --------------------------------------------------------------------------

def probe(ip: str, port: int, timeout: float = 2.5, local: str | None = None):
    """返回 (是否可达, 错误描述)。"""
    sock = socket.socket()
    try:
        if local:
            try:
                sock.bind((local, 0))
            except OSError:
                pass
        sock.settimeout(timeout)
        sock.connect((ip, port))
        return True, ""
    except OSError as error:
        return False, f"{type(error).__name__}: {error}"
    finally:
        sock.close()


def passthrough_value(passthrough: list[str], name: str) -> str | None:
    for i, item in enumerate(passthrough):
        if item == name and i + 1 < len(passthrough):
            return passthrough[i + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


def as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 交互
# --------------------------------------------------------------------------

def choose_classroom(items: list[dict], query: str | None) -> dict | None:
    if not items:
        return None
    if query:
        q = query.lower()
        exact = [i for i in items if i["name"].lower() == q]
        if exact:
            return exact[0]
        hits = [i for i in items if q in i["name"].lower()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            print(f"「{query}」匹配到多个教室，请更精确一些：")
            for i in hits:
                print(f"  - {i['name']}")
            return None
        print(f"没有找到名字包含「{query}」的教室。")
        return None

    print("\n请选择教室：")
    for n, i in enumerate(items, 1):
        print(f"  {n}) {i['name']}   (教师机 {i['teacher']})")
    print("  （输入编号，或直接输入教室名片段；q 退出）")
    try:
        raw = input("选择 > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if raw.lower() in ("q", "quit", "exit"):
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(items):
        return items[int(raw) - 1]
    return choose_classroom(items, raw)


def detect_classroom(items: list[dict]) -> dict | None:
    """按本机网卡 IP 猜当前在哪个教室。

    规则：本机某个 IPv4 与教师机 IP **前 3 段相同**（同一 /24）→ 就是这间。
    不用端口探测判教室：实测知行楼 5-214 和 5-216 的教师机 9003/9004
    **同时都可达**，但人只在一间，只有本机 IP 的网段是可信的。

    唯一命中才返回，否则返回 None（交给菜单让用户选）。
    """
    candidates = [d["ip"] for d in detect_local_ips()]
    if not candidates:
        return None

    hits: list[dict] = []
    for item in items:
        try:
            teacher_prefix = ".".join(str(ipaddress.IPv4Address(item["teacher"])).split(".")[:3])
        except ValueError:
            continue
        if any(ip.split(".")[:3] == teacher_prefix.split(".") for ip in candidates):
            if item not in hits:
                hits.append(item)
    return hits[0] if len(hits) == 1 else None


def resolve_profile(profile: dict) -> tuple[str, str | None]:
    """把选中的教室解析成 (教师机 IP, 本机课堂网卡 IP)。

    本机 IP 每次都重新探测，所以换教室不用改配置。
    """
    teacher = profile["teacher"]
    local = choose_local(detect_local_ips(teacher), profile["local"], None)
    return teacher, local


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="start.py",
        description="噢易教室助手多教室启动器：按教室名自动套用教师机 IP、端口与本机 IP。",
        epilog="start.py 不认识的参数会原样透传给 oseasy-helper，并覆盖配置文件里的同名项。",
    )
    p.add_argument("classroom", nargs="?", help="教室名（支持片段匹配，省略则进入交互选择）")
    p.add_argument("--list", action="store_true", help="列出 classrooms.conf 里配置的教室")
    p.add_argument("--local", help="手动指定本机课堂网卡 IP（覆盖自动探测）")
    p.add_argument("--mode", default=DEFAULT_MODE, choices=("student", "client", "video"),
                   help="使用哪个子命令（默认 student：登录+视频）")
    p.add_argument("--config", help=f"配置文件路径（默认 {CONF_NAME}）")
    p.add_argument("--check", action="store_true", help="网络体检：网卡、路由、端口全测一遍后退出")
    p.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令，不运行")
    p.add_argument("--no-probe", action="store_true", help="启动前不做教师机连通性预检")
    return p


def run_check(profile: dict, local: str, manage_port: int) -> int:
    teacher = profile["teacher"]
    print("=" * 60)
    print("  网络体检")
    print("=" * 60)
    print(f"  教室    : {profile['name']}")
    print(f"  教师机  : {teacher}")
    print(f"  管理端口: {manage_port}")
    print(f"  组播组  : {native.multicast_group(teacher, profile['opts'].get('mcast-group'))}"
          f"  (端口 7778)")

    print("\n[1] 本机网卡")
    candidates = detect_local_ips(teacher)
    if not candidates:
        print("    没有探测到可用网卡。")
    for c in candidates:
        tags = [net_relation_label(c["rel"])]
        if c["is_route_src"]:
            tags.append("系统通往教师机的出口")
        if c["ip"] == local:
            tags.append("<< 当前选用")
        print(f"    {c['ip']:<16} [{c['iface']}]  {' / '.join(tags)}")
    print(f"    当前选用本机 IP：{local}")

    print("\n[2] 网段关系")
    rel = subnet_relation(local, teacher)
    print(f"    {local} <-> {teacher}  : {net_relation_label(rel)}")
    src = route_source_ip(teacher, manage_port)
    print(f"    系统通往教师机的出口 IP：{src or '（无路由）'}")
    if src and src != local:
        print(f"    [警告] 出口 IP 与选用 IP 不一致，建议改用 {src}")

    print("\n[3] 教师机端口")
    ok, err = probe(teacher, manage_port, local=local)
    print(f"    {'管理端口':<14} {teacher}:{manage_port}  ->  {'可达 ✓' if ok else '不通 ✗  ' + err}")

    print("\n[4] 结论")
    if not ok:
        print("    管理端口连不上：不在教室网络 / 教师机没开噢易教师端 / 教师机 IP 填错。")
        return 1
    print("    管理端口可达，可以登录教师机（在线状态）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args, passthrough = build_parser().parse_known_args(argv)

    conf = Path(args.config) if args.config else HERE / CONF_NAME
    items = load_classrooms(conf)

    if args.list:
        if not items:
            print(f"还没有配置任何教室。请编辑：{conf}")
            return 1
        print(f"已配置 {len(items)} 个教室（{conf}）：")
        for i in items:
            extra = "".join(f"  {k}={v}" for k, v in i["opts"].items())
            local = f"  本机固定 {i['local']}" if i["local"] else ""
            print(f"  - {i['name']:<20} 教师机 {i['teacher']}{local}{extra}")
        return 0

    if not items:
        print(f"还没有配置任何教室。\n请打开这个文件，按里面的示例填一行：\n  {conf}")
        return 1

    if args.classroom:
        profile = choose_classroom(items, args.classroom)
    else:
        profile = detect_classroom(items)
        if profile:
            print(f"  自动识别教室：{profile['name']}（教师机 {profile['teacher']}）")
        else:
            profile = choose_classroom(items, None)
    if profile is None:
        if args.classroom:
            print("（用 --list 可以查看全部已配置教室）")
            return 1
        print("已取消。")
        return 0

    teacher = profile["teacher"]
    opts = dict(profile["opts"])
    # 命令行透传 > 配置文件 > 默认值
    manage_port = as_int(passthrough_value(passthrough, "--port") or opts.get("port"),
                         DEFAULT_MANAGE_PORT)

    candidates = detect_local_ips(teacher)
    local = choose_local(candidates, profile["local"], args.local)
    if not local:
        print("\n没能确定本机课堂网卡 IP，请手动指定，例如：\n"
              f"  python start.py {profile['name']} --local 10.140.20.145")
        return 1

    if args.check:
        return run_check(profile, local, manage_port)

    extra: list[str] = []
    for key, value in opts.items():
        if key in WRAPPER_ONLY_OPTS:
            continue  # 封装层自己用的开关，原项目 client 不认识
        if args.mode == "video" and key in LOGIN_ONLY_OPTS:
            continue
        if passthrough_value(passthrough, f"--{key}") is not None:
            continue  # 命令行已指定，覆盖配置文件
        extra += [f"--{key}", value]

    print("\n" + "=" * 58)
    print(f"  教室      : {profile['name']}")
    print(f"  教师机    : {teacher}:{manage_port}")
    print(f"  本机 IP   : {local}")
    print(f"  组播组    : {native.multicast_group(teacher, opts.get('mcast-group'))}:7778"
          + ("（配置文件指定）" if opts.get("mcast-group") else ""))
    print(f"  子命令    : {args.mode}")
    print("=" * 58)
    for line in subnet_warning(local, teacher):
        print(line)

    if not args.no_probe:
        ok, err = probe(teacher, manage_port, local=local)
        print(f"  管理端口 {manage_port} : {'可达 ✓' if ok else '连不上 ✗'}")
        if not ok:
            print("    提示：连不上通常意味着不在该教室网络里、教师机没开噢易教师端，")
            print("          或教师机 IP 填错。程序仍会启动并每 5 秒自动重连。")

    cmd = [sys.executable, "-m", "oseasy_helper", args.mode,
           "--teacher", teacher, "--local", local, *extra, *passthrough]

    if args.dry_run:
        print("\n[dry-run] 将要执行：")
        print("  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        return 0

    print("\n启动中…… 按 Ctrl+C 停止\n")
    sys.path.insert(0, str(HERE))
    from oseasy_helper.cli import main as cli_main  # noqa: E402

    try:
        return cli_main(cmd[3:])
    except KeyboardInterrupt:
        print("\n已停止。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
