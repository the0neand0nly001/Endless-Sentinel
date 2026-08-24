"""Proxmox VE API collector."""
from modules.state import pct, utcnow
def normalise_node(n):
    return {"id":n.get("node","unknown"),"name":n.get("node","unknown"),"kind":"node","status":"online" if n.get("status")=="online" else "offline","cpu_percent":round(float(n.get("cpu") or 0)*100,1),"cpu_cores":int(n.get("maxcpu") or 0),"ram_used":int(n.get("mem") or 0),"ram_total":int(n.get("maxmem") or 0),"ram_percent":pct(n.get("mem"),n.get("maxmem")),"uptime":int(n.get("uptime") or 0)}
class ProxmoxClient:
    def __init__(self,config): self.config=config
    def collect(self):
        if not self.config.get("enabled"): return {"enabled":False,"status":"disabled","name":"Proxmox","resources":[],"last_check":utcnow(),"last_success":None,"error":None}
        try:
            from proxmoxer import ProxmoxAPI
            api=ProxmoxAPI(self.config["host"],port=self.config.get("port",8006),user=self.config["user"],token_name=self.config["token_name"],token_value=self.config["token_value"],verify_ssl=self.config.get("verify_ssl",True))
            nodes=[normalise_node(x) for x in api.nodes.get()]; guests=[]
            for n in nodes:
                for kind,endpoint in (("vm",api.nodes(n["name"]).qemu),("lxc",api.nodes(n["name"]).lxc)):
                    try:
                        for g in endpoint.get(): guests.append({"id":f"{kind}-{g.get('vmid')}","name":g.get("name") or str(g.get("vmid")),"kind":kind,"node":n["name"],"status":"running" if g.get("status")=="running" else "stopped","cpu_percent":round(float(g.get("cpu") or 0)*100,1),"ram_used":int(g.get("mem") or 0),"ram_total":int(g.get("maxmem") or 0),"ram_percent":pct(g.get("mem"),g.get("maxmem")),"uptime":int(g.get("uptime") or 0)})
                    except Exception: pass
            now=utcnow(); status="healthy" if all(n["status"]=="online" for n in nodes) else "critical"
            return {"enabled":True,"status":status,"name":self.config["host"],"resources":nodes+guests,"last_check":now,"last_success":now,"error":None}
        except Exception as exc: return {"enabled":True,"status":"offline","name":self.config.get("host") or "Proxmox","resources":[],"last_check":utcnow(),"last_success":None,"error":str(exc)}
