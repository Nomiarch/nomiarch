import json
import os
import socket
import time
import urllib.error

from nomiarch.common import NomiarchError, request


SAMPLE = {"name": "sample-storage", "public_access": True, "https_only": False,
          "encryption_enabled": True}


def smoke(url, token, *, real_model=False):
    health = request(url + "/healthz")
    try:
        request(url + "/v1/evidence")
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
    else:
        raise NomiarchError("Unauthenticated evidence access was accepted")
    task_id = request(url + "/v1/tasks", token=token, data={"config": SAMPLE})["id"]
    until = time.monotonic() + 150
    while time.monotonic() < until:
        task = request(url + "/v1/tasks/" + task_id, token=token)
        if task["status"] in {"completed", "failed"}:
            break
        time.sleep(1)
    else:
        raise NomiarchError("Sample task did not complete within 150 seconds")
    if task["status"] != "completed":
        raise NomiarchError("Sample task failed: " + json.dumps(task["result"]))
    result = task["result"]
    if [c["status"] for c in result["findings"]] != ["fail", "fail", "pass"]:
        raise NomiarchError("Configuration findings were incorrect")
    if real_model and result["explanation"]["mode"] != "local-model":
        raise NomiarchError("The real-model acceptance test used a development adapter")
    if not isinstance(result["explanation"].get("summary"), str) or not result["explanation"]["summary"].strip():
        raise NomiarchError("The explanation service returned no usable text")
    evidence = request(url + "/v1/evidence", token=token)
    foundation = foundation_probe(url, token) if health.get('version') == '0.1.0.dev3' else None
    return {"health": health, "task": task, "evidence_checkpoint": evidence["checkpoint"],
            "unauthenticated_request": "denied", "foundation": foundation}


def foundation_probe(url, token):
    desired = {'cpus': 2, 'memory_gib': 8, 'disk_gib': 40, 'outbound_blocked': True}
    config = {'kind': 'foundation', 'name': 'a'*32, 'configuration_sha256': 'b'*64,
              'desired': desired, 'observed': dict(desired, outbound_blocked=False)}
    task_id = request(url + '/v1/tasks', token=token, data={'config': config})['id']
    until = time.monotonic() + 150
    while time.monotonic() < until:
        task = request(url + '/v1/tasks/' + task_id, token=token)
        if task['status'] in {'failed', 'completed'}: break
        time.sleep(1)
    else: raise NomiarchError('Foundation observation task timed out')
    result = task.get('result') or {}
    if (task['status'] != 'completed' or result.get('authority') != 'proposal-only'
            or [f['check'] for f in result.get('findings', []) if f['status'] == 'fail'] != ['outbound_blocked']):
        raise NomiarchError('Foundation observation evaluation failed')
    return {'task': task['id'], 'drift': 'detected', 'authority': 'proposal-only',
            'scope': 'Synthetic observation acceptance test; use the controller observer for live measurements'}


def boundary_probe():
    broker = os.environ["NOMIARCH_BROKER_URL"]
    token = os.environ["NOMIARCH_WORKER_TOKEN"]
    try:
        request(broker + "/v1/tools", token=token, data={"tool": "foundation.destroy", "input": {}})
    except urllib.error.HTTPError as e:
        if e.code != 403:
            raise
    else:
        raise NomiarchError("Foundation destroy was accepted by the tool gateway")
    probes = {}
    for address, port in [("1.1.1.1", 443), ("169.254.169.254", 80)]:
        try:
            with socket.create_connection((address, port), timeout=3):
                pass
        except OSError:
            probes[address] = "blocked-or-unreachable"
        else:
            raise NomiarchError("Worker reached forbidden destination " + address)
    # DNS exfiltration requires a separate check from TCP egress.
    try:
        socket.getaddrinfo("example.com", 443)
    except socket.gaierror:
        probes["external_dns"] = "blocked-or-unresolvable"
    else:
        raise NomiarchError("Worker resolved an external DNS name")
    return {"forbidden_tool": "denied", "connectivity_probes": probes,
            "scope": "Selected worker TCP/DNS probes; not a physical air-gap certification"}


if __name__ == "__main__":
    import sys
    if "--boundary" in sys.argv:
        print(json.dumps(boundary_probe()))
    else:
        print(json.dumps(smoke("http://127.0.0.1:8787", os.environ["NOMIARCH_OPERATOR_TOKEN"], real_model=True)))
