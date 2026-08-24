"""Thread-safe state, alert evaluation, and the resilient polling coordinator."""
from __future__ import annotations
import copy, logging, threading, time
from datetime import datetime, timezone

log = logging.getLogger(__name__)
def utcnow(): return datetime.now(timezone.utc).isoformat()
def pct(used, total):
    try: return round(float(used) / float(total) * 100, 1) if float(total) > 0 else 0.0
    except (TypeError, ValueError, ZeroDivisionError): return 0.0

class MonitorState:
    def __init__(self):
        self._lock=threading.RLock(); self._refresh_lock=threading.Lock()
        self.data={"last_poll":None,"polling":False,"integrations":{},"alerts":[],"active_alerts":{}}
    def snapshot(self):
        with self._lock: return copy.deepcopy(self.data)
    def set_polling(self, value):
        with self._lock: self.data["polling"]=value
    def update_integration(self, name, value):
        with self._lock:
            old=self.data["integrations"].get(name, {})
            if value.get("error") and old.get("last_success") and not value.get("resources"):
                value["resources"]=old.get("resources",[]); value["last_success"]=old["last_success"]
            self.data["integrations"][name]=value
    def finish_poll(self):
        with self._lock: self.data["last_poll"]=utcnow(); self.data["polling"]=False
    def record_alert(self, alert):
        with self._lock:
            alert={"timestamp":utcnow(),**alert}; key=alert["key"]
            if alert.get("recovery"): self.data["active_alerts"].pop(key,None)
            else: self.data["active_alerts"][key]=alert
            self.data["alerts"]=[alert,*self.data["alerts"]][:200]
    def public(self, stale_seconds=120):
        out=self.snapshot(); now=time.time()
        for item in out["integrations"].values():
            try: item["stale"] = bool(item.get("last_success") and now-datetime.fromisoformat(item["last_success"]).timestamp()>stale_seconds)
            except Exception: item["stale"]=True
        out["active_alert_count"]=len(out.pop("active_alerts",{}))
        states=[v.get("status") for v in out["integrations"].values() if v.get("enabled")]
        out["overall"]="critical" if "critical" in states or "offline" in states else "warning" if any(x in states for x in ("warning","degraded")) else "healthy" if states else "disabled"
        return out

class Poller:
    def __init__(self,state,clients,notifier,interval=30,thresholds=None):
        self.state=state; self.clients=clients; self.notifier=notifier; self.interval=max(5,int(interval)); self.thresholds=thresholds or {}; self.stop_event=threading.Event(); self.thread=None
    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.thread=threading.Thread(target=self._run,name="sentinel-poller",daemon=True); self.thread.start()
    def stop(self): self.stop_event.set()
    def refresh(self):
        if not self.state._refresh_lock.acquire(blocking=False): return False
        threading.Thread(target=self._poll_guarded,daemon=True).start(); return True
    def _poll_guarded(self):
        try: self.poll_once()
        finally: self.state._refresh_lock.release()
    def _run(self):
        while not self.stop_event.is_set():
            if self.state._refresh_lock.acquire(blocking=False):
                try: self.poll_once()
                finally: self.state._refresh_lock.release()
            self.stop_event.wait(self.interval)
    def poll_once(self):
        self.state.set_polling(True)
        try:
            for name,client in self.clients.items():
                try: result=client.collect()
                except Exception as exc:
                    log.exception("integration_poll_failed",extra={"integration":name}); result={"enabled":True,"status":"offline","error":str(exc),"resources":[],"last_check":utcnow(),"last_success":None}
                self.state.update_integration(name,result); self._evaluate(name,result)
        finally: self.state.finish_poll()
    def _evaluate(self,name,result):
        problems={}
        for r in result.get("resources",[]):
            rid=str(r.get("id") or r.get("name") or "unknown")
            for metric in ("cpu_percent","ram_percent"):
                if r.get(metric) is None: continue
                value=float(r[metric]); base=metric.split("_")[0].upper(); crit=float(self.thresholds.get(base+"_CRITICAL_PERCENT",95)); warn=float(self.thresholds.get(base+"_WARNING_PERCENT",80))
                sev="critical" if value>=crit else "warning" if value>=warn else None
                if sev: problems[f"{name}:{rid}:{metric}"]={"key":f"{name}:{rid}:{metric}","severity":sev,"integration":name,"resource":r.get("name",rid),"value":value,"threshold":crit if sev=="critical" else warn,"host":result.get("name",name),"message":f"{base} usage is {value:.1f}%"}
            if r.get("status") in ("offline","failed","unknown","unhealthy","restarting","crashloop"):
                problems[f"{name}:{rid}:status"]={"key":f"{name}:{rid}:status","severity":"critical","integration":name,"resource":r.get("name",rid),"value":r.get("status"),"threshold":"healthy","host":result.get("name",name),"message":f"Resource state is {r.get('status')}"}
        old={k:v for k,v in self.state.snapshot()["active_alerts"].items() if k.startswith(name+":")}
        for key,a in problems.items():
            if key not in old: self.state.record_alert(a); self.notifier.operational(a)
        for key,a in old.items():
            if key not in problems:
                recovery={**a,"severity":"recovery","recovery":True,"message":"Resource returned to a healthy state"}; self.state.record_alert(recovery); self.notifier.operational(recovery)
