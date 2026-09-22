import os
import time

from nomiarch.common import request


def perform(task, core, broker, token):
    def tool(name, value):
        return request(broker + "/v1/tools", token=token, timeout=110,
                       data={"task": task["id"], "tool": name, "input": value})
    try:
        checked = tool("config.evaluate", task["config"])
        explanation = tool("model.summarize", checked)
        result, ok = {**checked, "explanation": explanation}, True
    except Exception as e:
        result, ok = {"error": type(e).__name__, "detail": "Tool, policy, model, or evidence service failed"}, False
    request(core + "/internal/worker/complete", token=token,
            data={"id": task["id"], "lease": task["lease"], "result": result, "ok": ok})


def main():
    core, broker, token = (os.environ[k] for k in
                            ("NOMIARCH_CORE_URL", "NOMIARCH_BROKER_URL", "NOMIARCH_WORKER_TOKEN"))
    while True:
        try:
            task = request(core + "/internal/worker/claim", token=token, data={})["task"]
            if task:
                perform(task, core, broker, token)
            else:
                time.sleep(1)
        except Exception:
            # The durable lease allows recovery after a transient outage/restart.
            time.sleep(2)


if __name__ == "__main__":
    main()
