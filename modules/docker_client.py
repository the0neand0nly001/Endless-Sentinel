"""Docker Engine collector."""
from datetime import datetime, timezone
from modules.state import utcnow
def evaluate_container(attrs,previous_restarts=0):
    state=attrs.get("State",{}); status=(state.get("Status") or "unknown").lower(); health=(state.get("Health") or {}).get("Status")
    if health=="unhealthy":status="unhealthy"
    restarts=int(attrs.get("RestartCount") or 0)
    return status,health or "none",restarts,restarts>previous_restarts
class DockerClient:
    def __init__(self,config):self.config=config;self.restarts={}
    def collect(self):
        if not self.config.get("enabled"):return {"enabled":False,"status":"disabled","name":"Docker","resources":[],"last_check":utcnow(),"last_success":None,"error":None}
        try:
            import docker
            tls=False
            if self.config.get("tls_verify"):
                tls=docker.tls.TLSConfig(client_cert=None,ca_cert=self.config.get("cert_path") or None,verify=True)
            cli=docker.DockerClient(base_url=self.config.get("host") or None,tls=tls); info=cli.info(); resources=[]
            for c in cli.containers.list(all=True):
                a=c.attrs; status,health,restarts,unexpected=evaluate_container(a,self.restarts.get(c.id,0)); self.restarts[c.id]=restarts
                started=a.get("State",{}).get("StartedAt"); uptime=None
                try: uptime=max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(started.replace("Z","+00:00"))).total_seconds())) if status=="running" else None
                except Exception: pass
                tags=a.get("Config",{}).get("Image") or "unknown"
                resources.append({"id":c.short_id,"name":c.name,"kind":"container","image":tags,"status":status,"health":health,"restarts":restarts,"unexpected_restart":unexpected,"started":started,"uptime":uptime})
            now=utcnow(); bad=any(r["status"] in ("unhealthy","restarting","dead") for r in resources)
            return {"enabled":True,"status":"degraded" if bad else "healthy","name":info.get("Name") or self.config.get("host") or "Docker","resources":resources,"last_check":now,"last_success":now,"error":None}
        except Exception as exc:return {"enabled":True,"status":"offline","name":self.config.get("host") or "Docker","resources":[],"last_check":utcnow(),"last_success":None,"error":str(exc)}
