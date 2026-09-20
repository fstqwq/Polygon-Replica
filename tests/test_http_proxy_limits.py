import http.client
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from starlette.types import Receive, Scope, Send


async def echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    body = bytearray()
    while True:
        message = await receive()
        body.extend(message.get("body", b""))
        if not message.get("more_body", False):
            break
    headers = dict(scope["headers"])
    await send({"type": "http.response.start", "status": 200, "headers": [
        (b"x-observed-host", headers[b"host"]),
        (b"x-observed-scheme", scope["scheme"].encode("ascii")),
    ]})
    for start in range(0, len(body), 4096):
        await send({"type": "http.response.body", "body": bytes(body[start:start + 4096]),
                    "more_body": True})
    await send({"type": "http.response.body", "body": b""})


class TestHTTPProxyLimits(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        nginx = shutil.which("nginx")
        if nginx is None:
            raise RuntimeError("nginx is required for the executor HTTP proxy tests")
        root = Path(__file__).resolve().parents[1]
        temporary = tempfile.TemporaryDirectory(prefix="polygon-proxy-test-")
        cls.addClassCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            backend_port = listener.getsockname()[1]
            cls._start([
                sys.executable, "-m", "uvicorn", "tests.test_http_proxy_limits:echo_app",
                "--fd", str(listener.fileno()), "--http", "httptools", "--loop", "uvloop",
                "--lifespan", "off", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1",
                "--no-access-log",
            ], directory / "backend.log", root, (listener.fileno(),))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        config = directory / "nginx.conf"
        config.write_text(f'''
daemon off;
master_process off;
pid "{directory / 'nginx.pid'}";
error_log stderr;
events {{ worker_connections 64; }}
http {{
    access_log off;
    client_body_temp_path "{directory / 'body'}";
    proxy_temp_path "{directory / 'proxy'}";
    server {{
        listen 127.0.0.1:{cls.port} default_server;
        include "{root / 'scripts/nginx/request-limits.conf'}";
        client_max_body_size 1m;
        location / {{
            proxy_pass http://127.0.0.1:{backend_port};
            proxy_http_version 1.1;
            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
            proxy_read_timeout 30s;
            proxy_send_timeout 30s;
        }}
    }}
}}
''', encoding="utf-8")
        cls._start([nginx, "-p", str(directory), "-c", str(config)],
                   directory / "nginx.log", root)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=0.2)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                response.read()
                if response.status == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(0.05)
        raise RuntimeError("proxy did not become ready:\n" + "\n".join(
            path.read_text(encoding="utf-8") for path in directory.glob("*.log")
        ))

    @classmethod
    def _start(cls, command: list[str], log_path: Path, cwd: Path,
               pass_fds: tuple[int, ...] = ()) -> None:
        log = log_path.open("wb")
        cls.addClassCleanup(log.close)
        process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                   pass_fds=pass_fds)
        cls.addClassCleanup(cls._stop, process)

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _exchange(self, request: bytes, *, incomplete: bool = False) -> bytes:
        timeout = 15 if incomplete else 5
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as connection:
            connection.sendall(request)
            if not incomplete:
                connection.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                try:
                    chunk = connection.recv(65536)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

    def test_oversized_request_line_and_headers_are_rejected(self) -> None:
        cases = (
            (b"GET /" + b"x" * 16384 + b" HTTP/1.1\r\nHost: polygon.example\r\n\r\n", 414),
            (b"GET / HTTP/1.1\r\nHost: polygon.example\r\nX-Large: " + b"x" * 16384 + b"\r\n\r\n", 400),
            (b"GET / HTTP/1.1\r\nHost: polygon.example\r\n" +
             b"".join(b"X-Header-%d: " % index + b"x" * 7000 + b"\r\n" for index in range(6)) +
             b"\r\n", 400),
        )
        for request, status in cases:
            with self.subTest(status=status, request_bytes=len(request)):
                self.assertTrue(self._exchange(request).startswith(f"HTTP/1.1 {status} ".encode()))

    def test_incomplete_header_is_closed_by_header_timeout(self) -> None:
        start = time.monotonic()
        response = self._exchange(b"GET / HTTP/1.1\r\nHost: polygon.example\r\nX-Pending: ", incomplete=True)
        self.assertGreaterEqual(time.monotonic() - start, 8, "incomplete headers closed before the configured deadline")
        self.assertTrue(not response or response.startswith(b"HTTP/1.1 408 "), response)

    def test_large_upload_and_streamed_response_preserve_body_host_and_scheme(self) -> None:
        body = bytes(range(256)) * 1024
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("POST", "/upload", body=body, headers={
            "Host": "polygon.example:8443", "X-Forwarded-Proto": "https",
        })
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("X-Observed-Host"), "polygon.example:8443")
        self.assertEqual(response.getheader("X-Observed-Scheme"), "https")
        self.assertEqual(response.read(), body)
