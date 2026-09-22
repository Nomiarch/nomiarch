"""A loopback-only development harness, explicitly outside the VM support profile."""
import contextlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error

from nomiarch.common import NomiarchError, atomic_write, file_lock, private_dir, read_json, request, write_json
from nomiarch.smoke import smoke


def unused_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def environment(root, port=8787, model_url=None):
    root = private_dir(root)
    with file_lock(root / "controller.lock", blocking=False):
        if not (root / "tokens.json").exists():
            write_json(root / "tokens.json", {k: secrets.token_urlsafe(32) for k in ("operator", "worker", "broker")})
        tokens = read_json(root / "tokens.json")
        broker_port = unused_port()
        base = dict({k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TMPDIR"}},
                    PYTHONPATH=str(Path(__file__).resolve().parent.parent),
                    NOMIARCH_BIND="127.0.0.1", NOMIARCH_CORE_URL=f"http://127.0.0.1:{port}",
                    NOMIARCH_BROKER_URL=f"http://127.0.0.1:{broker_port}")
        core_env = dict(base, NOMIARCH_DATA=str(root / "data"), NOMIARCH_PORT=str(port),
                        NOMIARCH_MODE="development-local-model" if model_url else "development-deterministic",
                        **{"NOMIARCH_" + k.upper() + "_TOKEN": v for k, v in tokens.items()})
        broker_env = dict(base, NOMIARCH_PORT=str(broker_port), NOMIARCH_POLICY_MODE="development",
                          NOMIARCH_MODEL_MODE="local-model" if model_url else "development",
                          NOMIARCH_MODEL_URL=model_url or "http://127.0.0.1:8080",
                          NOMIARCH_WORKER_TOKEN=tokens["worker"], NOMIARCH_BROKER_TOKEN=tokens["broker"])
        worker_env = dict(base, NOMIARCH_WORKER_TOKEN=tokens["worker"])
        processes, logs = [], []
        try:
            for role, env in [("core.server", core_env), ("runtime.broker", broker_env), ("runtime.worker", worker_env)]:
                logpath = root / (role + ".log")
                atomic_write(logpath, "")
                log = logpath.open("ab")
                logs.append(log)
                processes.append(subprocess.Popen([sys.executable, "-m", "nomiarch." + role], env=env,
                                                  stdout=log, stderr=log, start_new_session=True))
            for _ in range(100):
                if any(p.poll() is not None for p in processes):
                    raise NomiarchError(f"A development service exited; inspect logs in {root}")
                try:
                    request(base["NOMIARCH_CORE_URL"] + "/healthz", timeout=1)
                    request(base["NOMIARCH_BROKER_URL"] + "/healthz", timeout=1)
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise NomiarchError("Development services did not start")
            yield base, tokens
        finally:
            for p in processes:
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGTERM)
            for p in processes:
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
            for log in logs:
                log.close()


def run(root, *, demo=False, port=8787, model_url=None):
    with environment(root, port, model_url) as (urls, tokens):
        if demo:
            result = smoke(urls["NOMIARCH_CORE_URL"], tokens["operator"], real_model=bool(model_url))
            try:
                request(urls["NOMIARCH_BROKER_URL"] + "/v1/tools", token=tokens["worker"],
                        data={"tool": "foundation.destroy", "input": {}})
            except urllib.error.HTTPError as e:
                if e.code != 403:
                    raise
                result["forbidden_tool"] = "denied"
            else:
                raise NomiarchError("A forbidden tool was accepted")
            result["profile"] = "development; no VM isolation or real-model proof unless --model-url is supplied"
            write_json(Path(root) / "demo-report.json", result)
            print(json.dumps(result, indent=2))
        else:
            print(f"Core: {urls['NOMIARCH_CORE_URL']}\nOperator token file: {Path(root).resolve() / 'tokens.json'}\n"
                  "Development profile. Press Ctrl-C to stop; data is retained.", flush=True)
            while True:
                time.sleep(1)
