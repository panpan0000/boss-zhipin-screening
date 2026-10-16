#!/usr/bin/env python3
"""纯标准库的 Chrome DevTools Protocol 客户端。

只用标准库实现（含 WebSocket 分帧），不依赖 websocket-client /
Playwright / Selenium —— 这是本项目核心零依赖的原因。

对上层暴露：
    http_json(path, port)      探测调试端口、列出标签页
    pick_target(port, match)   按 URL 子串挑标签页，挑不到回退到第一个 page
    WS                         最小可用的 WebSocket（文本帧 / 分片 / 掩码）
    CDP                        命令往返 + Runtime.evaluate

本模块不感知任何具体站点，可以脱离本项目单独使用：
只要能开调试端口，就能读任意页面的 DOM 文本。
"""
import base64
import json
import os
import socket
import struct
import time
import urllib.request


# ---------------------------------------------------------------- HTTP helper
def http_json(path, port, timeout=5):
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


# ------------------------------------------------------------ WebSocket (WS)
class WS:
    """刚好够用的一坨 WebSocket:文本帧、分片、掩码。"""

    def __init__(self, ws_url, timeout=30):
        if not ws_url.startswith("ws://"):
            raise ValueError("只支持 ws:// (调试端口在本机)")
        hostport, _, path = ws_url[5:].partition("/")
        host, _, port = hostport.partition(":")
        port = int(port or 80)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET /{path} HTTP/1.1\r\n"
                f"Host: {hostport}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("握手期间连接被关闭")
            buf += chunk
        head, _, self.buf = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n")[0]
        if b"101" not in status:
            raise RuntimeError("握手失败: " + status.decode("utf-8", "replace"))

    # -- 发送 ---------------------------------------------------------------
    def send_text(self, s):
        data = s.encode()
        header = bytearray([0x81])  # FIN + opcode=1 (text)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        header += bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header))

    # -- 接收 ---------------------------------------------------------------
    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise RuntimeError("连接被关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_message(self):
        data = b""
        while True:
            b0, b1 = self._read_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read_exact(8))[0]

            mk = self._read_exact(4) if masked else None
            payload = self._read_exact(ln)
            if mk:
                payload = bytes(b ^ mk[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:
                raise RuntimeError("服务端关闭了 WebSocket")
            if opcode == 0x9:  # ping -> 忽略
                continue
            data += payload
            if fin:
                return data.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- CDP 会话
class CDP:
    def __init__(self, ws_url, timeout=30):
        self.ws = WS(ws_url, timeout=timeout)
        self._id = 0

    def call(self, method, params=None, timeout=30):
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv_message())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} 出错: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"{method} 超时")

    def eval(self, expression):
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": False,
            },
        )
        if res.get("exceptionDetails"):
            raise RuntimeError("页面内 JS 抛错: " + json.dumps(res["exceptionDetails"], ensure_ascii=False)[:400])
        return res.get("result", {}).get("value")

    def close(self):
        self.ws.close()


# ---------------------------------------------------------------- 目标选择
def pick_target(port, url_match):
    targets = http_json("/json/list", port)
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if url_match:
        hit = [t for t in pages if url_match in (t.get("url") or "")]
        if hit:
            return hit[0], pages
    return (pages[0] if pages else None), pages
