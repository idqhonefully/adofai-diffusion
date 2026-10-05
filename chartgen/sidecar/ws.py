"""最小 RFC6455 WebSocket 服务端（**只用标准库**）。

设计取舍：本项目已有「手写 SMF writer → 零第三方依赖」的先例，
受限沙箱也拦管道（见 `server.py` 顶部），所以这里自己实现握手与帧。

覆盖（够用即可，不做扩展协商）：

  · 握手：`Sec-WebSocket-Accept = base64(sha1(key + GUID))`
  · 帧：文本 / 二进制 / ping / pong / close；**客户端掩码必须解**
  · 长度：7 / 16 / 64 位三种
  · 分片：continuation 拼接（我们自己只发单帧）
  · 上限：单帧 / 单消息都设限，**不信任对端**

不做的：permessage-deflate、二进制消息流式处理（攒满再交）、子协议协商。
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_FRAME = 8 * 1024 * 1024
MAX_MSG = 8 * 1024 * 1024


class WsError(Exception):
    """协议层错误 —— 一律以 close(1002) 收场，不抛栈给上层。"""


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def is_upgrade(headers) -> bool:
    """`headers` 是 `email.message.Message` 或 dict-like。"""
    up = (headers.get("Upgrade") or "").lower()
    conn = (headers.get("Connection") or "").lower()
    return up == "websocket" and "upgrade" in conn


def handshake_response(headers) -> bytes:
    key = headers.get("Sec-WebSocket-Key")
    if not key:
        raise WsError("缺少 Sec-WebSocket-Key")
    return ("HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key(key)}\r\n"
            "\r\n").encode("ascii")


# ---------------------------------------------------------------- 帧
def encode(payload: bytes, opcode: int = OP_TEXT, fin: bool = True) -> bytes:
    """服务端帧 **不加掩码**。"""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", b0, n)
    elif n < (1 << 16):
        head = struct.pack("!BBH", b0, 126, n)
    else:
        head = struct.pack("!BBQ", b0, 127, n)
    return head + payload


def recv_frame(rfile):
    """读一帧 ⇒ `(fin, opcode, payload)`；对端关闭 ⇒ `(True, OP_CLOSE, b"")`。"""
    h = rfile.read(2)
    if len(h) < 2:
        return True, OP_CLOSE, b""
    b0, b1 = h[0], h[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", _need(rfile, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _need(rfile, 8))[0]
    if n > MAX_FRAME:
        raise WsError(f"单帧过大：{n} 字节")
    mask = _need(rfile, 4) if masked else b""
    data = _need(rfile, n) if n else b""
    if masked:
        data = bytes(c ^ mask[i & 3] for i, c in enumerate(data))
    if b0 & 0x70:
        raise WsError("RSV 位被置起来了（未协商扩展）")
    return fin, opcode, data


def _need(rfile, n: int) -> bytes:
    buf = rfile.read(n)
    if buf is None or len(buf) < n:
        raise WsError("连接提前结束")
    return buf


# ---------------------------------------------------------------- 连接
class Conn:
    """一条已握手的连接。`send_text` / `recv_text` 只管消息，帧细节在里面。"""

    def __init__(self, rfile, wfile, sock=None, max_msg: int = MAX_MSG):
        self.rfile = rfile
        self.wfile = wfile
        self.sock = sock
        self.max_msg = max_msg
        self.closed = False
        self.bytes_in = 0
        self.bytes_out = 0
        self.opened_at = time.time()
        self._lock = threading.Lock()      # HTTP 线程也可能往这条连接上推 import

    # ---- 发
    def send(self, payload, opcode: int = OP_TEXT) -> None:
        if self.closed:
            return
        data = encode(payload, opcode)
        self.bytes_out += len(data)
        with self._lock:
            self.wfile.write(data)
            self.wfile.flush()

    def send_text(self, text: str) -> None:
        self.send(text, OP_TEXT)

    def ping(self) -> None:
        self.send(b"", OP_PING)

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        try:
            self.send(struct.pack("!H", code) + reason.encode("utf-8"), OP_CLOSE)
        except Exception:                                       # noqa: BLE001
            pass
        self.closed = True

    # ---- 收
    def recv_text(self) -> str | None:
        """收一条**完整**文本消息；对端关闭 / 只发了控制帧 ⇒ `None`。"""
        chunks: list[bytes] = []
        total = 0
        while True:
            fin, opcode, data = recv_frame(self.rfile)
            self.bytes_in += len(data)
            if opcode == OP_CLOSE:
                self.closed = True
                return None
            if opcode == OP_PING:
                self.send(data, OP_PONG)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CONT:
                if not chunks:
                    raise WsError("没有起始帧的 continuation")
            elif opcode in (OP_TEXT, OP_BIN):
                if chunks:
                    raise WsError("上一条消息没结束就又开了新消息")
                chunks = [data]
                total = len(data)
                if fin:
                    return self._finish(opcode, chunks, total)
                continue
            else:
                raise WsError(f"未知操作码 {opcode}")
            total += len(data)
            if total > self.max_msg:
                self.close(1009, "消息过大")
                raise WsError("消息过大")
            chunks.append(data)
            if fin:
                return self._finish(OP_TEXT, chunks, total)

    def _finish(self, opcode: int, chunks, total: int) -> str | None:
        if total > self.max_msg:
            self.close(1009, "消息过大")
            raise WsError("消息过大")
        raw = b"".join(chunks)
        if opcode == OP_BIN:
            return raw.decode("utf-8", errors="replace")
        return raw.decode("utf-8", errors="replace")


def token_ok(a: str, b: str) -> bool:
    """常数时间比较，避免时序侧信道（本项目也一样照做）。"""
    return bool(a) and bool(b) and hmac_ok(a, b)


def hmac_ok(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def new_token() -> str:
    return base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("=")
