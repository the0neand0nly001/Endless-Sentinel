"""Kubernetes/k3s node and pod collector."""
import os
from modules.state import utcnow
def node_ready(node): return any(c.type=="Ready" and c.status=="True" for c in (node.status.conditions or []))
def evaluate_pod(pod):
    statuses=pod.status.container_statuses or []; waiting=[s.state.waiting.reason for s in statuses if s.state and s.state.waiting]
    if "CrashLoopBackOff" in waiting:return "crashloop"
    phase=(pod.status.phase or "unknown").lower()
    if phase in ("failed","pending","unknown"):return phase
    if statuses and not all(s.ready for s in statuses):return "unhealthy"
    return "running"
class K3sClient:
    def __init__(self,config):self.config=config
    def collect(self):
        if not self.config.get("enabled"):return {"enabled":False,"status":"disabled","name":"k3s","resources":[],"last_check":utcnow(),"last_success":None,"error":None}
        try:
            from kubernetes import client,config
            path=self.config.get("kubeconfig")
            if path: config.load_kube_config(config_file=path,context=self.config.get("context") or None)
            elif os.getenv("KUBERNETES_SERVICE_HOST"):config.load_incluster_config()
            else:config.load_kube_config(context=self.config.get("context") or None)
            api=client.CoreV1Api(); resources=[]
            for n in api.list_node().items: resources.append({"id":"node:"+n.metadata.name,"name":n.metadata.name,"kind":"node","status":"online" if node_ready(n) else "offline","capacity":dict(n.status.capacity or {}),"allocatable":dict(n.status.allocatable or {})})
            for p in api.list_pod_for_all_namespaces().items:
                statuses=p.status.container_statuses or []
                resources.append({"id":f"pod:{p.metadata.namespace}:{p.metadata.name}","name":p.metadata.name,"namespace":p.metadata.namespace,"node":p.spec.node_name,"kind":"pod","status":evaluate_pod(p),"phase":p.status.phase,"ready":bool(statuses) and all(s.ready for s in statuses),"restarts":sum(s.restart_count or 0 for s in statuses)})
            now=utcnow(); bad=any(r["status"] in ("offline","failed","pending","unknown","unhealthy","crashloop") for r in resources)
            return {"enabled":True,"status":"degraded" if bad else "healthy","name":self.config.get("context") or "k3s","resources":resources,"last_check":now,"last_success":now,"error":None}
        except Exception as exc:return {"enabled":True,"status":"offline","name":self.config.get("context") or "k3s","resources":[],"last_check":utcnow(),"last_success":None,"error":str(exc)}
