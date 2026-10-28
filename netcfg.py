#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""改本机网卡 IP（换教室用）。

教室之间网段不同，而本机是**静态 IP**（DHCP 关着），所以换教室除了改教师机地址，
还得把本机网卡改到同一个网段，否则根本连不上。

只做三件事：读配置、设静态 IP、恢复自动获取。改之前会备份到
``configs\\net-backup.json``，``switch.py --restore-net`` 可以一键还原。

⚠️ 改 IP 需要管理员权限；改完网络会短暂中断几秒。
"""

from __future__ import annotations

import ctypes
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKUP_FILE = HERE / "configs" / "net-backup.json"

# 默认路由那块的网卡（有 IPv4 地址、有网关的物理网卡）
_PS_READ = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$items = @()
foreach ($c in Get-NetIPConfiguration) {
    if (-not $c.IPv4Address) { continue }
    $idx = $c.InterfaceIndex
    $items += [pscustomobject]@{
        alias  = $c.InterfaceAlias
        index  = $idx
        ip     = $c.IPv4Address.IPAddress
        prefix = $c.IPv4Address.PrefixLength
        gateway= $c.IPv4DefaultGateway.NextHop
        dns    = @(($c.DNSServer | Where-Object {$_.AddressFamily -eq 2}).ServerAddresses)
        dhcp   = [int](Get-NetIPInterface -InterfaceIndex $idx -AddressFamily IPv4).Dhcp
    }
}
ConvertTo-Json -InputObject @($items) -Compress
"""


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run(cmd: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _powershell(script: str) -> str:
    result = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    if result.returncode != 0:
        raise OSError(result.stderr.decode("mbcs", "replace") or "PowerShell 执行失败")
    return result.stdout.decode("utf-8", "replace")


def read_configs() -> list[dict]:
    """列出所有有 IPv4 地址的网卡：``[{alias,index,ip,prefix,gateway,dns,dhcp}]``。"""
    raw = _powershell(_PS_READ).strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for item in data:
        if not item.get("ip"):
            continue
        out.append({
            "alias": item.get("alias") or "",
            "index": int(item.get("index") or 0),
            "ip": item["ip"],
            "prefix": int(item.get("prefix") or 24),
            "gateway": item.get("gateway") or "",
            "dns": [d for d in (item.get("dns") or []) if d],
            "dhcp": bool(item.get("dhcp")),
        })
    return out


def pick_interface(teacher: str | None = None) -> dict | None:
    """挑一块用来上课的网卡：优先「有网关的」，其次名字像物理网卡的。"""
    configs = read_configs()
    if not configs:
        return None
    real = [c for c in configs
            if not c["ip"].startswith("127.")
            and not c["alias"].lower().startswith(("loopback", "环回"))]
    pool = real or configs
    if teacher:
        subnet = ".".join(teacher.split(".")[:3]) + "."
        same = [c for c in pool if c["ip"].startswith(subnet)]
        if same:
            return same[0]
    with_gw = [c for c in pool if c["gateway"]]
    return (with_gw or pool)[0]


def mask_of(prefix: int) -> str:
    prefix = max(0, min(32, int(prefix)))
    bits = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF if prefix else 0
    return ".".join(str((bits >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _netsh_set_static(index: int, ip: str, prefix: int, gateway: str | None) -> tuple[bool, str]:
    cmd = ["netsh", "interface", "ipv4", "set", "address",
           f"name={index}", "static", ip, mask_of(prefix)]
    if gateway:
        cmd.append(gateway)
    result = _run(cmd)
    text = (result.stdout + result.stderr).decode("mbcs", "replace").strip()
    ok = result.returncode == 0 and "失败" not in text and "error" not in text.lower()
    return ok, text


def _netsh_set_dhcp(index: int) -> tuple[bool, str]:
    result = _run(["netsh", "interface", "ipv4", "set", "address", f"name={index}", "dhcp"])
    text = (result.stdout + result.stderr).decode("mbcs", "replace").strip()
    ok = result.returncode == 0 and "失败" not in text
    return ok, text


def ping(ip: str, timeout_ms: int = 800, count: int = 2) -> bool:
    try:
        result = _run(["ping", "-n", str(count), "-w", str(timeout_ms), ip], timeout=12)
    except (OSError, subprocess.SubprocessError):
        return False
    text = (result.stdout + result.stderr).decode("mbcs", "replace")
    # 中英文都覆盖
    if "TTL=" in text.upper():
        return True
    return "无法访问" not in text and "超时" not in text and "timed out" not in text.lower() \
        and "unreachable" not in text.lower() and result.returncode == 0


# --------------------------------------------------------------------------
# 备份 / 还原
# --------------------------------------------------------------------------

def save_backup(conf: dict) -> Path | None:
    try:
        BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if BACKUP_FILE.exists():
            try:
                data = json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
        data[str(conf["index"])] = {
            "alias": conf["alias"], "index": conf["index"], "ip": conf["ip"],
            "prefix": conf["prefix"], "gateway": conf["gateway"],
            "dns": conf["dns"], "dhcp": conf["dhcp"],
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        BACKUP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        return BACKUP_FILE
    except OSError:
        return None


def load_backup(index: int | None = None) -> dict | None:
    try:
        data = json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if index is not None:
        return data.get(str(index))
    return data.get(str(next(iter(data), ""))) if data else None


# --------------------------------------------------------------------------
# 对外：把网卡切到某个网段
# --------------------------------------------------------------------------

def target_ip(current_ip: str, classroom_teacher: str, wanted: str | None = None) -> str:
    """算出这间教室该用的本机 IP。

    没显式指定 ``wanted`` 时：**保留当前 IP 的最后一段**，只把前 3 段换成教师机网段。
    （比如 10.140.24.234 去 10.140.17.x 的教室 → 10.140.17.234）
    """
    if wanted:
        return wanted
    last = current_ip.split(".")[-1] if current_ip.count(".") == 3 else "234"
    return ".".join(classroom_teacher.split(".")[:3]) + "." + last


def apply_ip(conf: dict, ip: str, prefix: int = 24,
             gateway: str | None = None, verbose: bool = True) -> dict:
    """把 ``conf`` 这块网卡改成 ``ip``。

    返回 ``{ok, message, backup, rolled_back}``。改完会 ping 网关验证，
    ping 不通就自动回滚成原配置（这是「别把自己网断掉」的保险）。
    """
    if not is_admin():
        return {"ok": False, "message": "需要管理员权限才能改网卡 IP。",
                "backup": None, "rolled_back": False}

    index = conf["index"]
    backup = save_backup(conf)

    if verbose:
        print(f"  改本机 IP：{conf['ip']}/{conf['prefix']}"
              f" → {ip}/{prefix}" + (f"（网关 {gateway}）" if gateway else ""))
    ok, text = _netsh_set_static(index, ip, prefix, gateway)
    if not ok:
        return {"ok": False, "message": text or "netsh 执行失败",
                "backup": backup, "rolled_back": False}

    # 等网卡把新地址挂上
    time.sleep(2.0)
    now = None
    for _ in range(10):
        for item in read_configs():
            if item["index"] == index:
                now = item
                break
        if now and now["ip"] == ip:
            break
        time.sleep(0.5)

    if not now or now["ip"] != ip:
        if verbose:
            print("  [!] 网卡没有切到新地址，回滚……")
        _rollback(conf, verbose=verbose)
        return {"ok": False, "message": "网卡没生效", "backup": backup, "rolled_back": True}

    # 验证：能 ping 通网关就算活；教师机不在线时网关也该在
    check_ip = gateway or ".".join(ip.split(".")[:3]) + ".1"
    if not ping(check_ip):
        if verbose:
            print(f"  [!] 改完 ping 不通网关 {check_ip}，回滚……")
        _rollback(conf, verbose=verbose)
        return {"ok": False, "message": f"ping 不通网关 {check_ip}",
                "backup": backup, "rolled_back": True}

    if verbose:
        print(f"  ✓ 本机 IP 已切到 {ip}（网关 {check_ip} 可达）")
    return {"ok": True, "message": "", "backup": backup, "rolled_back": False}


def _rollback(conf: dict, verbose: bool = True) -> bool:
    if conf.get("dhcp"):
        ok, text = _netsh_set_dhcp(conf["index"])
    else:
        ok, text = _netsh_set_static(conf["index"], conf["ip"], conf["prefix"],
                                     conf.get("gateway") or None)
    if verbose:
        print(f"  {'✓' if ok else '[!]'} 已恢复原配置 {conf['ip']}/{conf['prefix']}"
              + ("" if ok else f"：{text}"))
    time.sleep(1.5)
    return ok


def restore_from_backup(index: int | None = None, verbose: bool = True) -> bool:
    """把网卡还原成 ``configs\\net-backup.json`` 里记的配置。"""
    saved = load_backup(index)
    if not saved:
        if verbose:
            print("  没有找到网卡配置备份（configs\\net-backup.json）。")
        return False
    if not is_admin():
        if verbose:
            print("  需要管理员权限才能改网卡。")
        return False
    if verbose:
        print(f"  还原网卡「{saved['alias']}」→ {saved['ip']}/{saved['prefix']}"
              f"（网关 {saved.get('gateway') or '无'}）")
    if saved.get("dhcp"):
        ok, text = _netsh_set_dhcp(int(saved["index"]))
    else:
        ok, text = _netsh_set_static(int(saved["index"]), saved["ip"],
                                     int(saved["prefix"]), saved.get("gateway") or None)
    if verbose:
        print(f"  {'✓ 已还原' if ok else '[!] 还原失败：' + text}")
    return ok


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        sys.exit(0 if restore_from_backup() else 1)
    for conf in read_configs():
        flag = "DHCP" if conf["dhcp"] else "静态"
        print(f"  [{conf['index']}] {conf['alias']:<16} {conf['ip']}/{conf['prefix']:<3}"
              f" 网关={conf['gateway'] or '-':<16} DNS={','.join(conf['dns']) or '-'}  {flag}")
    print("  管理员:", is_admin())
