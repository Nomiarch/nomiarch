import os

from nomiarch import __version__
from nomiarch.core.http import ApiError, authorize, serve
from nomiarch.core.store import Store


def application(store, tokens, mode):
    def app(method, path, headers, body):
        if method == "GET" and path == "/healthz":
            with store.connect() as db:
                db.execute("SELECT 1")
            return 200, {"status": "ready", "version": __version__, "mode": mode}
        if path.startswith("/internal/worker/"):
            authorize(headers, tokens["worker"])
            if method == "POST" and path == "/internal/worker/claim":
                return 200, {"task": store.claim()}
            if method == "POST" and path == "/internal/worker/complete":
                if type(body["ok"]) is not bool or not isinstance(body["result"], dict):
                    raise ValueError("Invalid task completion")
                store.complete(body["id"], body["lease"], body["result"], body["ok"])
                return 200, {"recorded": True}
        elif path == "/internal/audit" and method == "POST":
            authorize(headers, tokens["broker"])
            if body["kind"] not in {"tool.allowed", "tool.denied", "tool.failed"}:
                raise ValueError("Unsupported evidence event")
            store.audit(body["kind"], body.get("task", "probe"), body.get("details", {}))
            return 200, {"recorded": True}
        else:
            authorize(headers, tokens["operator"])
            if method == "POST" and path == "/v1/tasks":
                config = body.get("config")
                if not isinstance(config, dict) or len(config) > 20:
                    raise ValueError("config must be a small JSON object")
                idem = headers.get("Idempotency-Key")
                if idem and len(idem) > 128:
                    raise ValueError("Idempotency-Key exceeds 128 characters")
                return 202, {"id": store.submit(config, idem)}
            if method == "GET" and path.startswith("/v1/tasks/"):
                task = store.task(path.rsplit("/", 1)[1])
                if task is None:
                    raise ApiError(404, "Task not found")
                return 200, task
            if method == "GET" and path == "/v1/evidence":
                return 200, store.evidence()
        raise ApiError(404, "Endpoint not found")
    return app


def main():
    tokens = {k: os.environ["NOMIARCH_" + k.upper() + "_TOKEN"] for k in ("operator", "worker", "broker")}
    if len(set(tokens.values())) != 3 or any(len(t) < 32 for t in tokens.values()):
        raise ValueError("Three distinct service tokens of at least 32 characters are required")
    serve(application(Store(os.environ["NOMIARCH_DATA"]), tokens,
                      os.environ.get("NOMIARCH_MODE", "local-model")),
          os.environ.get("NOMIARCH_BIND", "127.0.0.1"), int(os.environ.get("NOMIARCH_PORT", "8787")))


if __name__ == "__main__":
    main()
