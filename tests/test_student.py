"""Protocol tests with real sockets; no classroom network or vendor runtime.

覆盖登录报文、屏幕缩略图应答，以及「其它指令收到后只记日志、不应答」。
"""
import contextlib
import io
import json
import socket
import struct
import threading
from types import SimpleNamespace
import unittest

from oseasy_helper import client, video


def listener():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    sock.settimeout(5)
    return sock


def unused_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def make_args(**overrides):
    base = dict(local='127.0.0.1', teacher='127.0.0.1',
                mock_thumbnail=True, thumbnail_source='mock',
                reconnect_delay=0.1, once=False, with_video=False,
                mac='02:00:00:00:00:01', name='TEST-PC', user='test')
    base.update(overrides)
    return SimpleNamespace(**base)


def events_of(output):
    return [json.loads(line)['event']
            for line in output.getvalue().splitlines() if line.startswith('{')]


class PacketTests(unittest.TestCase):
    def test_login_packet_shape(self):
        packet = client.login_packet('PC', 'user', '02:00:00:00:00:01',
                                     '127.0.0.1', '2026-01-01 00:00:00')
        payload = packet[4:]
        self.assertEqual(struct.unpack_from('<II', payload), (6, 0))
        self.assertEqual(struct.unpack_from('<I', payload, 12)[0], len(payload) - 16)
        self.assertIn('/PC/user/', payload[16:].decode('utf-16-le'))

    def test_login_fields_reject_slashes(self):
        with self.assertRaises(ValueError):
            client.login_packet('PC/1', 'user', '02:00:00:00:00:01', '127.0.0.1', 'now')

    def test_thumbnail_reply_echoes_request_kind(self):
        jpeg = b'\xff\xd8jpeg'
        reply = client.thumbnail_reply({'command': 89, 'kind': 0x100}, jpeg)
        self.assertEqual(struct.unpack_from('<IIII', reply, 4), (43, 0x100, 0, len(jpeg)))
        self.assertEqual(reply[20:], jpeg)

    def test_thumbnail_reply_ignores_other_commands_and_empty_jpeg(self):
        self.assertIsNone(client.thumbnail_reply({'command': 88, 'kind': 1}, b'x'))
        self.assertIsNone(client.thumbnail_reply({'command': 89, 'kind': 1}, None))
        self.assertIsNone(client.thumbnail_reply({'command': 89}, b'x'))

    def test_message_parses_header(self):
        payload = struct.pack('<IIII', 89, 0x100, 0, 4) + b'abcd'
        fields = client.message(payload)
        self.assertEqual((fields['command'], fields['kind']), (89, 0x100))
        with self.assertRaises(ValueError):
            client.message(payload[:10])


class SessionTests(unittest.TestCase):
    def test_login_answers_thumbnail_and_ignores_other_commands(self):
        with listener() as management:
            stop = threading.Event()
            args = make_args(port=management.getsockname()[1])
            output, errors = io.StringIO(), []

            def run():
                try:
                    client.run(args, stop)
                except BaseException as error:      # noqa: BLE001 - reported below
                    errors.append(error)

            worker = threading.Thread(target=run)
            with contextlib.redirect_stdout(output), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.ExitStack() as stack:
                worker.start()
                try:
                    login = stack.enter_context(management.accept()[0])
                    login.settimeout(5)
                    first = client.read_frame(login, stop)
                    self.assertIn('/TEST-PC/test/', first[16:].decode('utf-16-le'))

                    # 教师端要缩略图：cmd 89，第二个字段是它自己定的值，必须原样回显。
                    request = struct.pack('<IIII', 89, 0x100, 0, 0)
                    login.sendall(struct.pack('<I', len(request)) + request)
                    reply = client.read_frame(login, stop)
                    self.assertEqual(struct.unpack_from('<II', reply), (43, 0x100))
                    self.assertEqual(reply[16:18], b'\xff\xd8')

                    # 其它指令（87、25 等）一律不回复，只记一笔日志。
                    for command in (87, 25):
                        body = struct.pack('<IIII', command, 0, 1, 0)
                        login.sendall(struct.pack('<I', len(body)) + body)
                    login.shutdown(socket.SHUT_RDWR)
                    login.close()
                finally:
                    stop.set()
                    worker.join(7)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])

        names = events_of(output)
        self.assertIn('login_sent', names)
        self.assertIn('thumbnail_sent', names)
        self.assertEqual(names.count('command_ignored'), 2)

    def test_silent_management_reconnects_after_video_startup_failure(self):
        with listener() as management, listener() as occupied:
            stop, errors = threading.Event(), []
            args = make_args(port=management.getsockname()[1],
                             mock_thumbnail=False,
                             management_idle_timeout=0.1, with_video=True,
                             udp_port=unused_port(socket.SOCK_DGRAM),
                             http_port=occupied.getsockname()[1])
            output = io.StringIO()

            def run():
                try:
                    client.run(args, stop)
                except BaseException as error:      # noqa: BLE001 - reported below
                    errors.append(error)

            worker = threading.Thread(target=run)
            with contextlib.redirect_stdout(output), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.ExitStack() as stack:
                worker.start()
                try:
                    first = stack.enter_context(management.accept()[0])
                    first.settimeout(4)
                    self.assertEqual(struct.unpack_from('<I', client.read_frame(first, stop))[0], 6)
                    # 第一个连接保持不说话 -> 超时后必须自己重连。
                    second = stack.enter_context(management.accept()[0])
                    second.settimeout(4)
                    self.assertEqual(struct.unpack_from('<I', client.read_frame(second, stop))[0], 6)
                finally:
                    stop.set()
                    worker.join(7)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])

        names = events_of(output)
        self.assertIn('video_unavailable', names)
        self.assertIn('management_retry', names)


class VideoTests(unittest.TestCase):
    def test_video_port_conflict_cleans_up_threads(self):
        with listener() as occupied:
            args = SimpleNamespace(local='127.0.0.1', teacher='127.0.0.1',
                                   udp_port=unused_port(socket.SOCK_DGRAM),
                                   http_port=occupied.getsockname()[1])
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(OSError):
                with video.service(args):
                    self.fail('An occupied port must fail at startup')
            self.assertFalse(any(t.name.startswith('video-') for t in threading.enumerate()))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.bind(('0.0.0.0', args.udp_port))


if __name__ == '__main__':
    unittest.main()
