"""Minimal mpv JSON-IPC client (Unix domain socket on Linux/Pi, Named Pipe on Windows)."""
import json
import os
import socket
import threading
import time


class MpvError(Exception):
    pass


class MpvIPC:
    def __init__(self, path: str) -> None:
        self.path = str(path)
        self.sock = None
        self.pipe = None
        self.lock = threading.Lock()
        self.rid = 0
        self.buf = b""

    def connect(self, timeout: float = 10.0) -> bool:
        end = time.time() + timeout
        is_pipe = os.name == "nt" or self.path.startswith(r"\\.\pipe\\")
        while time.time() < end:
            if is_pipe:
                try:
                    self.pipe = open(self.path, "r+b", buffering=0)
                    self.sock = self.pipe  # keep .sock truthy for existing checks
                    self.buf = b""
                    return True
                except OSError:
                    time.sleep(0.2)
            else:
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
                    s.connect(self.path)
                    s.settimeout(2.0)
                    self.sock, self.pipe, self.buf = s, None, b""
                    return True
                except (OSError, AttributeError):
                    time.sleep(0.2)
        return False

    def close(self) -> None:
        try:
            if self.pipe:
                self.pipe.close()
            elif self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None
        self.pipe = None

    def _readline(self) -> bytes:
        while b"\n" not in self.buf:
            if self.pipe is not None:
                chunk = self.pipe.read(1)
            else:
                chunk = self.sock.recv(65536)  # type: ignore[union-attr]
            if not chunk:
                raise MpvError("mpv bağlantısı kapandı")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def command(self, *args):
        if not self.sock:
            raise MpvError("bağlı değil")
        with self.lock:
            self.rid += 1
            rid = self.rid
            payload = json.dumps({"command": list(args), "request_id": rid}).encode() + b"\n"
            try:
                if self.pipe is not None:
                    self.pipe.write(payload)
                    self.pipe.flush()
                else:
                    self.sock.sendall(payload)
                for _ in range(200):
                    msg = json.loads(self._readline())
                    if msg.get("request_id") == rid:
                        if msg.get("error") != "success":
                            raise MpvError(str(msg.get("error")))
                        return msg.get("data")
            except (OSError, ValueError) as e:
                raise MpvError(str(e))
        raise MpvError("yanıt alınamadı")

    def get(self, prop: str, default=None):
        try:
            return self.command("get_property", prop)
        except MpvError:
            return default

    def set(self, prop: str, value) -> bool:
        try:
            self.command("set_property", prop, value)
            return True
        except MpvError:
            return False
