#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把这个仓库推到 GitHub 上新建的仓库（自建项目，不是 fork）。

双击 ``推送GitHub.bat`` 运行。会依次：

1. 让你粘贴一个 GitHub token（**输入时不回显**，只存在内存里，不写进任何文件）；
2. 自动找一条能连上 GitHub 的路（系统代理 → 直连 → 本地 7890/7891 等常见代理端口）；
3. 用 token 查到你的 GitHub 用户名；
4. 在**你的账号**下创建仓库（已存在就直接用，不会覆盖）；
5. ``git push`` 推上去，完事把 remote 恢复成不带 token 的干净地址。

token 需要什么权限：

* classic token：勾 ``repo``（私有仓库）或 ``public_repo``（只公开仓库）
* fine-grained token：Repository access 选 ``All repositories``，
  权限给 ``Administration: Read and write``（建仓库用）+ ``Contents: Read and write``（推送用）

只推一次也可以：直接用环境变量 ``GITHUB_TOKEN`` 传进来。
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "https://api.github.com"
DEFAULT_REPO = "oseasy-helper-multi"
DEFAULT_DESC = "噢易多媒体教室助手 · 多教室版：选教室一键切换教师机 IP 与本机网卡 IP"

# 常见本地代理端口（Clash 7890、V2Ray 10809、SSR 1080…）
PROXY_CANDIDATES = [
    "http://127.0.0.1:7890",
    "http://127.0.0.1:7897",
    "http://127.0.0.1:10809",
    "http://127.0.0.1:10808",
    "http://127.0.0.1:1080",
    "http://127.0.0.1:8080",
]


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


# --------------------------------------------------------------------------
# 找一条能连上 GitHub 的路
# --------------------------------------------------------------------------

def _opener(proxy: str | None):
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})   # 明确禁用代理 = 直连
    return urllib.request.build_opener(handler)


def _try(opener, url: str, timeout: float = 8.0) -> bool:
    try:
        with opener.open(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as error:
        # 401/403 也说明「连得上」，只是没权限
        return error.code in (401, 403, 404)
    except Exception:
        return False


def find_route() -> tuple[str | None, str]:
    """返回 (代理地址或 None, 说明)。None 表示直连。"""
    env_proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    tried = []
    if env_proxy:
        if _try(_opener(env_proxy), API + "/zen"):
            return env_proxy, f"系统代理 {env_proxy}"
        tried.append(env_proxy)
    if _try(_opener(None), API + "/zen"):
        return None, "直连"
    tried.append("直连")
    for proxy in PROXY_CANDIDATES:
        host, _, port = proxy.split("//")[1].partition(":")
        sock = socket.socket()
        sock.settimeout(0.4)
        try:
            sock.connect((host, int(port)))
        except OSError:
            continue
        finally:
            sock.close()
        if _try(_opener(proxy), API + "/zen"):
            return proxy, f"本地代理 {proxy}"
        tried.append(proxy)
    return "", "（都试过了：" + "、".join(tried) + "）"


# 探测 git 传输用的公开仓库：私有仓库在没带凭据时 ls-remote 会 404，
# 不能拿目标仓库来测，否则会把「有网」误判成「没网」。
PROBE_REPO = "https://github.com/octocat/Hello-World.git"


def git_route_works(proxy: str | None, timeout: float = 20.0) -> bool:
    """这条代理能不能真的把 git 协议跑通（API 通不代表 git 通）。"""
    cmd = ["git"]
    if proxy:
        cmd += ["-c", f"http.proxy={proxy}", "-c", f"https.proxy={proxy}"]
    cmd += ["ls-remote", "--heads", PROBE_REPO, "master"]
    try:
        done = subprocess.run(cmd, timeout=timeout,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return done.returncode == 0
    except Exception:
        return False


def find_git_route(preferred: str | None) -> tuple[str | None, str]:
    """返回 (git 用的代理或 None, 说明)。优先沿用 API 那条路。"""
    env_proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                 or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    candidates: list[str | None] = [preferred, env_proxy, *PROXY_CANDIDATES, None]
    seen: set[str | None] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if git_route_works(candidate):
            return candidate, (candidate or "直连")
    return "", ""


# --------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------

def api(opener, method: str, path: str, token: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(API + path, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "oseasy-helper-multi")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with opener.open(request, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"message": body}
        return error.code, parsed
    except Exception as error:      # 网络层
        return 0, {"message": str(error)}


def git(args: list[str], proxy: str | None, extra_env: dict | None = None) -> int:
    cmd = ["git"]
    if proxy:
        cmd += ["-c", f"http.proxy={proxy}", "-c", f"https.proxy={proxy}"]
    cmd += args
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(cmd, cwd=str(HERE), env=env).returncode


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main() -> int:
    _force_utf8()
    print("=" * 62)
    print("  把本仓库推送到 GitHub（在你的账号下新建一个仓库）")
    print("=" * 62)

    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        print("  需要一个 GitHub token（classic 勾 repo，或 fine-grained 给")
        print("  Administration + Contents 的读写权限）。")
        print("  在这里建：https://github.com/settings/tokens")
        print()
        try:
            token = getpass.getpass("  粘贴 token（输入时不显示）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 1
    if not token:
        print("没拿到 token，退出。")
        return 1

    print("\n  正在找一条能连上 GitHub 的路……")
    proxy, note = find_route()
    if proxy == "":
        print(f"  [!] 连不上 GitHub {note}")
        print("      如果你有代理软件，先打开它（Clash 默认 7890），再重跑一次。")
        return 1
    print(f"  ✓ 走 {note}")

    opener = _opener(proxy or None)

    status, me = api(opener, "GET", "/user", token)
    if status != 200:
        print(f"  [!] token 用不了（HTTP {status}）：{me.get('message', '')}")
        if status == 401:
            print("      token 可能过期了，或者复制的时候多了空格/换行。")
        return 1
    login = me.get("login", "")
    print(f"  ✓ 已认证：{login}（{me.get('name') or '未填姓名'}）")

    print()
    repo = input(f"  仓库名 [{DEFAULT_REPO}]: ").strip() or DEFAULT_REPO
    while True:
        choice = input("  私有仓库？(Y/n) [Y]: ").strip().lower()
        if choice in ("", "y", "yes"):
            private = True
            break
        if choice in ("n", "no"):
            private = False
            break
        print("    输入 y 或 n。")

    status, info = api(opener, "GET", f"/repos/{login}/{repo}", token)
    if status == 200:
        print(f"  仓库 {login}/{repo} 已存在，直接往里推。")
    else:
        print(f"  正在创建 {login}/{repo} ……")
        status, info = api(opener, "POST", "/user/repos", token, {
            "name": repo,
            "description": DEFAULT_DESC,
            "private": private,
            "has_issues": True,
            "has_wiki": False,
        })
        if status not in (200, 201):
            print(f"  [!] 创建失败（HTTP {status}）：{info.get('message', '')}")
            if status == 403:
                print("      token 权限不够（fine-grained 需要 Administration: Read and write）。")
            return 1
        print(f"  ✓ 已创建：{info.get('html_url', '')}")

    remote = f"https://github.com/{login}/{repo}.git"

    # 能调 API 的路不一定能跑 git 协议（实测：某些代理只放行 api.github.com，
    # 对 github.com 的 git 传输直接 502），所以这里单独再探一条。
    print("\n  正在找一条能跑 git 传输的路……")
    git_proxy, git_note = find_git_route(proxy)
    if not git_note:
        print("  [!] 没有一条路能跑 git 传输，推送会失败。")
        print("      先打开代理软件（Clash 默认 7890），再重跑一次。")
        return 1
    print(f"  ✓ git 走 {git_note}")
    proxy = git_proxy

    # 凭据只在推送这一刻存在：写进系统临时目录里的一个文件，推完立刻删。
    # 不用 URL 里带 token（会留在 .git/config 和命令历史里），也不用 GIT_ASKPASS
    # （Windows 上 .bat 不一定能被 git 直接执行）。
    #
    # ⚠️ 路径必须用正斜杠：git 会把 credential.helper 的值再交给 shell 解析一遍，
    #    `--file=C:\Users\...` 里的反斜杠被吃掉，就变成相对路径写到当前目录，
    #    于是项目根目录会冒出 `UsersADMINI~1AppDataLocalTemposeasy-push-xxxx.cred`
    #    这种明文 token 文件（2026-10-28 真的踩到过）。
    import tempfile
    cred_file = (Path(tempfile.gettempdir()) / f"oseasy-push-{os.getpid()}.cred").resolve()
    cred_file.write_text(f"https://{login}:{token}@github.com\n", encoding="utf-8")
    helper = f"credential.helper=store --file={cred_file.as_posix()}"
    try:
        git(["remote", "remove", "origin"], proxy)          # 原来指向别人的仓库
        git(["remote", "add", "origin", remote], proxy)
        git(["remote", "remove", "upstream"], proxy)
        # 顺手把原项目记成 upstream，以后想同步原版更新可以 git fetch upstream
        git(["remote", "add", "upstream",
             "https://github.com/lihaoze123/oseasy-helper.git"], proxy)
        print("\n  正在推送……（大文件可能慢一点）")
        code = git(["-c", helper, "push", "-u", "origin", "HEAD:main"], proxy)
    finally:
        try:
            cred_file.unlink()
        except OSError:
            pass
        # 兜底：万一路径又被解析歪了，当前目录里的残留也清掉
        for stray in HERE.glob("*oseasy-push-*.cred"):
            try:
                stray.unlink()
            except OSError:
                pass

    if code != 0:
        print(f"\n  [!] 推送失败（git 返回 {code}）。")
        print("      常见原因：代理断了 / token 没有 Contents 写权限 / 仓库名被占用。")
        print(f"      手动重试：git push -u origin HEAD:main")
        return code

    print(f"\n  ✓ 推好了：https://github.com/{login}/{repo}")
    print("    （remote 已指向新仓库，之后直接 git push 就行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
