from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


class NomiarchError(Exception):
    """An expected, operator-readable error."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def private_dir(path):
    path = Path(path).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not isinstance(data, bytes):
        data = data.encode()
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "wb") as f:
            if os.name != "nt":
                os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        if os.name == "posix":
            d = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(d)
            finally:
                os.close(d)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, data):
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise NomiarchError(f"Cannot read JSON from {path}: {e}") from e


@contextlib.contextmanager
def file_lock(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as f:
        os.chmod(path, 0o600)
        if os.name == "nt":
            import msvcrt
            # Lock one fixed byte; Windows byte-range locks need a real byte.
            if path.stat().st_size == 0:
                f.write("0")
                f.flush()
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise NomiarchError(f"Another controller owns {path.parent}") from e
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as e:
            raise NomiarchError(f"Another controller owns {path.parent}") from e
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Runner:
    def __init__(self, deadline=None, log_dir=None):
        self.deadline = deadline
        self.log_dir = Path(log_dir) if log_dir else None

    def run(self, argv, *, cwd=None, input=None, timeout=600, env=None, check=True):
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.time())
        if timeout <= 0:
            raise NomiarchError("Run lifetime expired; cleanup is required")
        args = [str(x) for x in argv]
        try:
            p = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=os.name != "nt",
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as e:
            raise NomiarchError(f"Cannot start {args[0]}: {e}") from e
        try:
            out, err = p.communicate(input, timeout=timeout)
        except BaseException as e:
            # Kill the process group, including provider and SSH child processes.
            # A remote cloud operation may continue; teardown must reconcile it.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
            else:
                os.killpg(p.pid, signal.SIGTERM)
            try:
                p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    p.kill()
                else:
                    os.killpg(p.pid, signal.SIGKILL)
                p.communicate()
            if isinstance(e, subprocess.TimeoutExpired):
                raise NomiarchError(f"{Path(args[0]).name} exceeded its time limit; a remote operation may still need cleanup") from e
            raise
        if self.log_dir:
            self.log_dir.mkdir(exist_ok=True, parents=True, mode=0o700)
            # Logs can contain infrastructure state: private files, never Git.
            atomic_write(self.log_dir / f"{time.time_ns()}.log", out + b"\n" + err)
        if check and p.returncode:
            raise NomiarchError(f"{Path(args[0]).name} failed ({p.returncode}): "
                                f"{err.decode(errors='replace')[-1200:]}")
        return out.decode(errors="replace")


def request(url, *, token=None, data=None, timeout=15, method=None, headers=None):
    headers = dict(headers or {})
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=canonical(data) if data is not None else None,
                                 headers=headers, method=method)
    # A workstation proxy must never receive local credentials or model inputs.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise NomiarchError("Upstream response exceeds limit")
        return json.loads(raw)
