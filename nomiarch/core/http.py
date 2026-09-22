import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets

from nomiarch.common import canonical


class ApiError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def serve(app, host, port):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # Never log tokens, bodies, or model input via the HTTP logger.

        def dispatch(self):
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ApiError(400, "Transfer-Encoding is unsupported")
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 65536:
                    raise ApiError(413, "Request body limit is 64 KiB")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ApiError(400, "Incomplete body")
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise ApiError(400, "Expected JSON object")
                code, value = app(self.command, self.path, self.headers, body)
            except ApiError as e:
                code, value = e.code, {"error": e.message}
            except (ValueError, TypeError, KeyError) as e:
                code, value = 400, {"error": str(e)}
            except Exception:
                code, value = 503, {"error": "Required service or durable storage unavailable"}
            encoded = canonical(value)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = dispatch
        do_POST = dispatch

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.serve_forever()


def authorize(headers, token):
    supplied = headers.get("Authorization", "")
    if not token or not secrets.compare_digest(supplied, "Bearer " + token):
        raise ApiError(401, "Valid service identity required")
