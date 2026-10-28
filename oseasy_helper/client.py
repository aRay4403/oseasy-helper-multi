"""Experimental login and screen-thumbnail responder; ignores remote control commands.

只做两件事：登录教师机（TCP 9003）、应答「要缩略图」的请求（cmd 89 → cmd 43）。
教师端判「在线」靠的就是后者。其它指令（含 cmd 25 / 87）一律只记一笔日志，不应答。
"""
import getpass
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

import psutil

from . import video

MAX_FRAME = 8 * 1024 * 1024


def read_frame(sock, stop, *, limit=MAX_FRAME, tick=None, idle=60):
    """Read a length-prefixed message, independent of TCP packet boundaries."""
    def exact(size):
        result = bytearray()
        last_data = time.monotonic()
        while len(result) < size:
            if stop.is_set():
                raise InterruptedError('Listener stopped')
            if tick:
                tick()
            try:
                chunk = sock.recv(size - len(result))
            except socket.timeout:
                if idle is not None and time.monotonic() - last_data >= idle:
                    raise TimeoutError('Incomplete frame / idle connection')
                continue
            if not chunk:
                raise EOFError('Connection closed before transfer end')
            result.extend(chunk)
            last_data = time.monotonic()
        return bytes(result)

    length, = struct.unpack('<I', exact(4))
    if not 4 <= length <= limit:
        raise ValueError(f'Invalid frame length: {length}')
    return exact(length)


def _emit(event, **fields):
    """把事件打成一行 JSON 写到 stdout —— watch.py 靠这个显示进度。"""
    try:
        print(json.dumps(dict(time=time.strftime('%Y-%m-%dT%H:%M:%S'), event=event, **fields),
                         ensure_ascii=False), flush=True)
    except (OSError, ValueError):
        pass


def interface_mac(local):
    for addresses in psutil.net_if_addrs().values():
        if any(a.family == socket.AF_INET and a.address == local for a in addresses):
            for address in addresses:
                if address.family == psutil.AF_LINK:
                    mac = address.address.replace('-', ':').upper()
                    if re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', mac):
                        return mac
    raise ValueError('No MAC address found for --local')


def login_packet(name, user, mac, local, timestamp):
    # Observed PC login: ten slash-separated fields; empty first/fourth fields.
    fields = ['', name, user, timestamp, '', '1', '0', '0', mac, local]
    if any('/' in field or any(ord(c) < 32 for c in field) for field in fields):
        raise ValueError('Login fields cannot contain slashes or control characters')
    body = ('/'.join(fields) + '/').encode('utf-16-le')
    payload = struct.pack('<IIII', 6, 0, 0, len(body)) + body
    return struct.pack('<I', len(payload)) + payload


def message(payload):
    if len(payload) < 16:
        raise ValueError('Truncated management header')
    command, kind, extra, size = struct.unpack_from('<IIII', payload)
    # Some commands carry data beyond the first declared field. Preserve its
    # length in diagnostics instead of guessing how the extra fields work.
    if size > len(payload) - 16:
        raise ValueError('Management body is shorter than its declared size')
    return dict(command=command, kind=kind, extra=extra,
                declared_bytes=size, body_bytes=len(payload) - 16)


def thumbnail_reply(fields, jpeg):
    """Reply to a screen-thumbnail request with a JPEG.

    Request: ``command 89`` with a subtype in the second header field. The
    studied build sent ``0x40``; this deployment sends ``0x100``. The official
    client **echoes that field** in its reply, captured on this machine::

        teacher -> (89, 0x100, 0, 0)
        student -> (43, 0x100, 0, 6503) + 6503 bytes of JPEG

    Echoing instead of hardcoding one value keeps both builds working. The
    earlier ``kind != 64`` guard never matched here, so no thumbnail was ever
    sent and the teacher console kept showing the seat as offline.
    """
    if fields.get('command') != 89 or not jpeg:
        return None
    kind = fields.get('kind')
    if kind is None:
        return None
    payload = struct.pack('<IIII', 43, kind, 0, len(jpeg)) + jpeg
    return struct.pack('<I', len(payload)) + payload


# --------------------------------------------------------------------------
# Screen thumbnail (for the teacher console)
# --------------------------------------------------------------------------

FFMPEG_CANDIDATES = (
    r'C:\Program Files (x86)\Os-Easy\os-easy multicast teaching system\ffmpeg.exe',
    r'C:\Program Files\Os-Easy\os-easy multicast teaching system\ffmpeg.exe',
)
# 320x180 @ q9 lands at ~6 KB, matching the official client's ~6.5 KB reply.
THUMBNAIL_WIDTH = 320
THUMBNAIL_QUALITY = 9


def find_ffmpeg():
    """Locate an ffmpeg able to grab the desktop (the OsEasy install ships one)."""
    override = os.environ.get('OSEASY_FFMPEG')
    if override and Path(override).is_file():
        return override
    for path in FFMPEG_CANDIDATES:
        if Path(path).is_file():
            return path
    return shutil.which('ffmpeg')


def screen_jpeg(ffmpeg=None, width=THUMBNAIL_WIDTH, quality=THUMBNAIL_QUALITY):
    """Grab the desktop as a JPEG; ``None`` when ffmpeg is unavailable."""
    exe = ffmpeg or find_ffmpeg()
    if not exe:
        return None
    command = [exe, '-hide_banner', '-loglevel', 'error', '-y',
               '-f', 'gdigrab', '-i', 'desktop', '-frames:v', '1',
               '-vf', f'scale={width}:-2', '-q:v', str(quality),
               '-f', 'mjpeg', '-']
    try:
        result = subprocess.run(command, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.stdout[:2] == b'\xff\xd8' else None


def mock_jpeg():
    return Path(__file__).with_name('mock-thumbnail.jpg').read_bytes()


def thumbnail_provider(args):
    """Pick what to send back when the teacher asks for a thumbnail.

    ``--thumbnail-source screen`` (default) grabs the real desktop, matching the
    official client. ``mock`` sends the bundled MOCK/TEST image instead.
    ``--no-mock-thumbnail`` disables the reply entirely.
    """
    if not getattr(args, 'mock_thumbnail', True):
        return None
    if getattr(args, 'thumbnail_source', 'screen') == 'mock':
        return mock_jpeg
    ffmpeg = getattr(args, 'thumbnail_ffmpeg', None) or find_ffmpeg()

    def capture():
        return (screen_jpeg(ffmpeg,
                            width=getattr(args, 'thumbnail_width', None) or THUMBNAIL_WIDTH,
                            quality=getattr(args, 'thumbnail_quality', None) or THUMBNAIL_QUALITY)
                or mock_jpeg())

    return capture


def _client_option(opts, flag):
    """Read ``--flag value`` or ``--flag=value`` from a raw argv list."""
    for i, item in enumerate(opts):
        if item == flag:
            return opts[i + 1] if i + 1 < len(opts) else ''
        if item.startswith(flag + '='):
            return item.split('=', 1)[1]
    return None


def thumbnail_label(argv):
    """Human-readable summary of the thumbnail setting, for startup banners.

    Accepts the raw argv list handed to ``client`` so callers like ``watch.py``
    can show the effective mode without re-parsing the whole CLI.
    """
    opts = list(argv or [])
    if '--no-mock-thumbnail' in opts:
        return '关闭（教师端将显示离线）'
    source = _client_option(opts, '--thumbnail-source') or 'screen'
    if source != 'screen':
        return f'{source} 占位图（不是你的真实桌面）'
    width = _client_option(opts, '--thumbnail-width') or THUMBNAIL_WIDTH
    quality = _client_option(opts, '--thumbnail-quality') or THUMBNAIL_QUALITY
    return f'真实桌面 {width}px 质量{quality}（每 5 秒一张发给教师机）'


def management_session(args, stop, emit, mac, thumbnail):
    """One login session; requests are not evidence of teacher UI acceptance."""
    with socket.socket() as sock:
        sock.bind((args.local, 0))
        sock.settimeout(5)
        sock.connect((args.teacher, args.port))
        sock.settimeout(0.5)
        sock.sendall(login_packet(getattr(args, 'name', None) or socket.gethostname(),
                                  getattr(args, 'user', None) or getpass.getuser(), mac,
                                  args.local, time.strftime('%Y-%m-%d %H:%M:%S')))
        emit('login_sent', note='TCP connected; teacher acceptance is not yet verified')
        while not stop.is_set():
            fields = message(read_frame(sock, stop, idle=getattr(args, 'management_idle_timeout', 60)))
            emit('management_message', **fields)
            # Only grab the screen when the teacher actually asks for it; the
            # request arrives every few seconds and a grab costs ~0.3 s.
            if thumbnail is not None and fields['command'] == 89:
                jpeg = thumbnail()
                reply = thumbnail_reply(fields, jpeg)
                if reply is not None:
                    sock.sendall(reply)
                    emit('thumbnail_sent', bytes=len(jpeg),
                         source=getattr(args, 'thumbnail_source', 'screen'))
            # 其它指令（cmd 25 文件传输、cmd 87 目录协商等）只记一笔日志，不应答。
            if fields['command'] in (25, 87):
                emit('command_ignored', command=fields['command'], kind=fields['kind'])


def run(args, stop=None):
    stop = stop if stop is not None else threading.Event()
    mac = getattr(args, 'mac', None) or interface_mac(args.local)
    thumbnail = thumbnail_provider(args)
    # Validate identity fields before opening any network services.
    login_packet(getattr(args, 'name', None) or socket.gethostname(),
                 getattr(args, 'user', None) or getpass.getuser(), mac, args.local, '')
    with ExitStack() as services:
        try:
            if getattr(args, 'with_video', False):
                try:
                    services.enter_context(video.service(args, stop, _emit))
                except OSError as error:
                    # A video port conflict must not take the login session down with it.
                    _emit('video_unavailable', error=str(error))
            while not stop.is_set():
                try:
                    management_session(args, stop, _emit, mac, thumbnail)
                except (OSError, EOFError, ValueError) as error:
                    if stop.is_set():
                        break
                    _emit('disconnected', error=str(error))
                    if getattr(args, 'once', False):
                        break
                    delay = getattr(args, 'reconnect_delay', 5)
                    _emit('management_retry', retry_seconds=delay)
                    stop.wait(delay)
        finally:
            stop.set()
