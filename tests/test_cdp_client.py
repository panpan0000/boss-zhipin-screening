#!/usr/bin/env python3
"""CDP 客户端的自测：用伪造的 CDP 服务端验证 HTTP 探测、WebSocket 握手、
掩码分帧、命令往返、以及错误路径。

**不需要真浏览器**，所以可以在 CI 里跑；更重要的是，当页面自动化失败时，
它能帮你区分「是我客户端写错了」还是「是页面改版了」。

    python3 tests/test_cdp_client.py
"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from zhipin_screening.cdp import CDP, WS, http_json, pick_target  # noqa: E402

HOST, PORT = "127.0.0.1", 18899
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PAGE_TEXT = "HELLO_ZHIPIN_推荐列表_OK"


# ------------------------------------------------------------ 伪造的 CDP 服务端
def _recv_until(sock, marker):
    buf = b""
    while marker not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return buf
        buf += chunk
    return buf


def _send_frame(sock, payload):
    """服务端 -> 客户端：不掩码（协议规定）。"""
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    sock.sendall(bytes(header) + payload)


def _read_frame(sock, buf):
    """客户端 -> 服务端：必带掩码。返回 (payload, 剩余 buf)。"""
    def need(n, b):
        while len(b) < n:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("connection closed")
            b += chunk
        return b

    buf = need(2, buf)
    ln = buf[1] & 0x7F
    off = 2
    if ln == 126:
        buf = need(4, buf)
        ln = struct.unpack(">H", buf[2:4])[0]
        off = 4
    elif ln == 127:
        buf = need(10, buf)
        ln = struct.unpack(">Q", buf[2:10])[0]
        off = 10
    buf = need(off + 4 + ln, buf)
    mask = buf[off:off + 4]
    payload = buf[off + 4:off + 4 + ln]
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return payload, buf[off + 4 + ln:]


def _handle(conn):
    try:
        head = _recv_until(conn, b"\r\n\r\n")
        if not head:
            return
        lines = head.split(b"\r\n")
        path = lines[0].split()[1].decode()

        # 回显型 CDP：把 params.n 的长度回给客户端，方便验证大帧
        if path == "/json/version":
            body = json.dumps({
                "Browser": "FakeChrome/1.0",
                "webSocketDebuggerUrl": f"ws://{HOST}:{PORT}/devtools/browser/FAKE",
            }).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            return

        if path == "/json/list":
            body = json.dumps([{
                "type": "page",
                "title": "BOSS直聘 - 推荐",
                "url": "https://www.zhipin.com/web/chat/recommend",
                "webSocketDebuggerUrl": f"ws://{HOST}:{PORT}/devtools/page/FAKE1",
            }]).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            return

        # WebSocket 升级
        headers = {}
        for ln in lines[1:]:
            if b":" in ln:
                k, v = ln.split(b":", 1)
                headers[k.strip().lower()] = v.strip()
        key = headers.get(b"sec-websocket-key")
        if not key:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return

        accept = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest()).decode()
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
        )

        buf = b""
        while True:
            payload, buf = _read_frame(conn, buf)
            msg = json.loads(payload)
            method = msg.get("method")
            if method == "Runtime.evaluate":
                _send_frame(conn, json.dumps({
                    "id": msg["id"],
                    "result": {"result": {"type": "string", "value": PAGE_TEXT}},
                }).encode())
            elif method == "Echo.size":
                # 回一个够大的字符串，撑出 64 位长度分支
                _send_frame(conn, json.dumps({
                    "id": msg["id"],
                    "result": {"size": msg.get("params", {}).get("n", 0)},
                }).encode())
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


class FakeCDP:
    """上下文管理器：起一个只存活于测试期间的假 CDP 服务端。"""

    def __enter__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((HOST, PORT))
        self.srv.listen(8)
        self.stop = threading.Event()

        def loop():
            while not self.stop.is_set():
                try:
                    conn, _ = self.srv.accept()
                except OSError:
                    return
                threading.Thread(target=_handle, args=(conn,), daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.srv.close()
        return False


# ------------------------------------------------------------------- 测试用例
def test_http_probe():
    v = http_json("/json/version", PORT, timeout=3)
    assert "Browser" in v, v


def test_pick_target_by_url_substring():
    target, pages = pick_target(PORT, "zhipin.com")
    assert target and "zhipin.com" in target["url"], target
    assert any("zhipin" in p.get("url", "") for p in pages)


def test_pick_target_falls_back():
    """匹配不上时不该返回 None，应退化到第一个 page。"""
    target, _ = pick_target(PORT, "no-such-domain.example")
    assert target is not None, "找不到匹配时应回退到第一个 page"


def test_runtime_evaluate_roundtrip():
    """核心路径：握手 + 掩码分帧 + 命令往返。"""
    target, _ = pick_target(PORT, "zhipin.com")
    cdp = CDP(target["webSocketDebuggerUrl"], timeout=10)
    try:
        assert cdp.eval("1+1") == PAGE_TEXT
    finally:
        cdp.close()


def test_large_command_framing():
    """大 payload 走 64 位长度分支 —— 简历正文常超 64KB。"""
    target, _ = pick_target(PORT, "zhipin.com")
    cdp = CDP(target["webSocketDebuggerUrl"], timeout=20)
    try:
        n = 80000
        res = cdp.call("Echo.size", {"n": n, "pad": "x" * n})
        assert res.get("size") == n, res
    finally:
        cdp.close()


def test_ws_handshake_rejects_non_ws_url():
    try:
        WS("http://127.0.0.1:%d/nope" % PORT, timeout=3)
    except ValueError:
        return
    raise AssertionError("非 ws:// 的地址应该被拒绝")


def test_bad_port_raises_cleanly():
    """端口不通要抛异常，让上层转成结构化的 NO_DEBUG_PORT，而不是卡死。"""
    try:
        http_json("/json/version", 19999, timeout=2)
    except Exception:
        return
    raise AssertionError("连不通的端口竟然没报错")


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failures = 0
    with FakeCDP():
        for name, fn in tests:
            try:
                fn()
                print(f"[PASS] {name}")
            except Exception as e:
                failures += 1
                print(f"[FAIL] {name}: {type(e).__name__}: {e}")
    print()
    if failures:
        print(f"{failures}/{len(tests)} 项失败 ✘")
        return 1
    print(f"{len(tests)}/{len(tests)} 全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
