"""One runtime definition shared by every provider; JSON is valid Kubernetes input."""
import base64

NS = "nomiarch"


def render(manifest, tokens, root="/var/lib/nomiarch"):
    objects = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS,
                "labels": {"pod-security.kubernetes.io/enforce": "restricted",
                           "pod-security.kubernetes.io/enforce-version": "latest"}}}]

    def obj(kind, name, spec=None, api="v1", **extra):
        value = {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": NS}, **extra}
        if spec is not None:
            value["spec"] = spec
        objects.append(value)
        return value

    obj("Secret", "identities", type="Opaque", data={k: base64.b64encode(v.encode()).decode() for k, v in tokens.items()})
    obj("ConfigMap", "policy", data={"tools.rego": manifest["policy"]})
    # Dedicated single-node cluster: explicit static volumes, no shared database.
    for role, subpath, readonly in [("core", "core", False), ("model", "model", True)]:
        pv = {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "nomiarch-" + role},
              "spec": {"capacity": {"storage": "1Gi" if role == "core" else "20Gi"},
                       "accessModes": ["ReadWriteOnce"], "persistentVolumeReclaimPolicy": "Retain",
                       "storageClassName": "", "hostPath": {"path": root + "/" + subpath, "type": "Directory"},
                       "claimRef": {"namespace": NS, "name": role},
                       "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                           {"key": "kubernetes.io/hostname", "operator": "In", "values": ["nomiarch"]}]}]}}}}
        objects.append(pv)
        obj("PersistentVolumeClaim", role, {"accessModes": ["ReadWriteOnce"], "storageClassName": "",
            "volumeName": "nomiarch-" + role, "resources": {"requests": {"storage": "1Gi" if role == "core" else "20Gi"}}})

    security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]}}

    def env(values, identities=()):
        entries = [{"name": k, "value": str(v)} for k, v in values.items()]
        entries += [{"name": "NOMIARCH_" + k.upper() + "_TOKEN", "valueFrom": {
            "secretKeyRef": {"name": "identities", "key": k}}} for k in identities]
        return entries

    def deployment(role, module, values, identities, mounts=(), volumes=(), sidecars=()):
        container = {"name": role, "image": manifest["core_image"], "imagePullPolicy": "Never",
            "command": ["python3", "-m", "nomiarch." + module], "env": env(values, identities),
            "securityContext": security, "resources": {"requests": {"cpu": "100m", "memory": "96Mi"},
            "limits": {"cpu": "1", "memory": "256Mi"}}, "volumeMounts": list(mounts)}
        if role in {"core", "broker"}:
            port = 8787 if role == "core" else 8788
            container["ports"] = [{"containerPort": port}]
            container["readinessProbe"] = {"httpGet": {"path": "/healthz", "port": port}, "periodSeconds": 5}
            container["livenessProbe"] = {"httpGet": {"path": "/healthz", "port": port}, "periodSeconds": 10}
            obj("Service", role, {"selector": {"app": role}, "ports": [{"port": port, "targetPort": port}]})
        obj("Deployment", role, {"replicas": 1, "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": role}}, "template": {
                "metadata": {"labels": {"app": role}}, "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                        "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [container, *sidecars], "volumes": list(volumes)}}}, api="apps/v1")

    deployment("core", "core.server", {"NOMIARCH_BIND": "0.0.0.0", "NOMIARCH_PORT": "8787",
        "NOMIARCH_DATA": "/data", "NOMIARCH_MODE": "local-model"}, ("operator", "worker", "broker"),
        [{"name": "data", "mountPath": "/data"}], [{"name": "data", "persistentVolumeClaim": {"claimName": "core"}}])
    opa = {"name": "policy", "image": manifest["core_image"], "imagePullPolicy": "Never",
        "command": ["/usr/local/bin/opa", "run", "--server", "--addr=127.0.0.1:8181", "/policy/tools.rego"],
        "securityContext": security, "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
        "limits": {"cpu": "500m", "memory": "256Mi"}},
        "volumeMounts": [{"name": "policy", "mountPath": "/policy", "readOnly": True}]}
    deployment("broker", "runtime.broker", {"NOMIARCH_BIND": "0.0.0.0", "NOMIARCH_PORT": "8788",
        "NOMIARCH_CORE_URL": "http://core:8787", "NOMIARCH_MODEL_URL": "http://model:8080"}, ("worker", "broker"),
        volumes=[{"name": "policy", "configMap": {"name": "policy"}}], sidecars=[opa])
    deployment("worker", "runtime.worker", {"NOMIARCH_CORE_URL": "http://core:8787", "NOMIARCH_BROKER_URL": "http://broker:8788"}, ("worker",))
    obj("Service", "model", {"selector": {"app": "model"}, "ports": [{"port": 8080, "targetPort": 8080}]})
    obj("Deployment", "model", {"replicas": 1, "strategy": {"type": "Recreate"},
        "selector": {"matchLabels": {"app": "model"}}, "template": {"metadata": {"labels": {"app": "model"}}, "spec": {
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "model", "image": manifest["model_image"], "imagePullPolicy": "Never",
                "args": ["--model", "/models/model.gguf", "--host", "0.0.0.0", "--port", "8080",
                         "--ctx-size", "2048", "--threads", "2", "--alias", "local"],
                "securityContext": security, "ports": [{"containerPort": 8080}],
                "resources": {"requests": {"cpu": "500m", "memory": "768Mi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
                "startupProbe": {"httpGet": {"path": "/health", "port": 8080}, "failureThreshold": 120, "periodSeconds": 5},
                "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 10},
                "volumeMounts": [{"name": "model", "mountPath": "/models", "readOnly": True}, {"name": "tmp", "mountPath": "/tmp"}]}],
            "volumes": [{"name": "model", "persistentVolumeClaim": {"claimName": "model", "readOnly": True}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}]}}}, api="apps/v1")

    obj("NetworkPolicy", "default-deny", {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}, api="networking.k8s.io/v1")
    # Exact same-namespace service paths; model/policy cannot contact the host/API.
    for source, dest, port in [("worker", "core", 8787), ("worker", "broker", 8788),
                                ("broker", "core", 8787), ("broker", "model", 8080)]:
        obj("NetworkPolicy", source + "-to-" + dest, {"podSelector": {"matchLabels": {"app": source}},
            "policyTypes": ["Egress"], "egress": [{"to": [{"podSelector": {"matchLabels": {"app": dest}}}],
                                                 "ports": [{"protocol": "TCP", "port": port}]}]}, api="networking.k8s.io/v1")
        obj("NetworkPolicy", dest + "-from-" + source, {"podSelector": {"matchLabels": {"app": dest}},
            "policyTypes": ["Ingress"], "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": source}}}],
                                                    "ports": [{"protocol": "TCP", "port": port}]}]}, api="networking.k8s.io/v1")
    obj("NetworkPolicy", "internal-dns", {"podSelector": {"matchExpressions": [{"key": "app", "operator": "In", "values": ["worker", "broker"]}]},
        "policyTypes": ["Egress"], "egress": [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                                         "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                                               "ports": [{"protocol": p, "port": 53} for p in ("TCP", "UDP")]}]}, api="networking.k8s.io/v1")
    return {"apiVersion": "v1", "kind": "List", "items": objects}


def dns_config():
    # No external forwarding. Kubernetes service discovery remains available.
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "coredns", "namespace": "kube-system"},
            "data": {"Corefile": ".:53 {\n errors\n health\n ready\n kubernetes cluster.local in-addr.arpa ip6.arpa {\n pods insecure\n fallthrough in-addr.arpa ip6.arpa\n }\n cache 30\n loop\n reload\n loadbalance\n}\n"}}
