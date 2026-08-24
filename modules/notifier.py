"""Discord notification delivery with separate channels and cooldown deduplication."""
import logging, threading, time
from datetime import datetime, timezone
import requests
log=logging.getLogger(__name__)
class DiscordNotifier:
    def __init__(self, operational_url="", security_url="", cooldown=900, security_cooldown=900, session=None):
        self.operational_url=operational_url.strip(); self.security_url=security_url.strip(); self.cooldown=int(cooldown); self.security_cooldown=int(security_cooldown); self.http=session or requests; self.sent={}; self.lock=threading.Lock()
        if self.operational_url and self.operational_url==self.security_url: log.error("Discord webhook configuration rejected: operational and security URLs must differ"); self.security_url=""
    def _send(self,channel,key,payload):
        url=self.operational_url if channel=="operational" else self.security_url
        if not url:return False
        cooldown=self.cooldown if channel=="operational" else self.security_cooldown; token=f"{channel}:{key}"; now=time.monotonic()
        with self.lock:
            if now-self.sent.get(token,-1e12)<cooldown:return False
            self.sent[token]=now
        try:
            response=self.http.post(url,json=payload,timeout=8); response.raise_for_status(); return True
        except Exception: log.warning("discord_delivery_failed",extra={"channel":channel,"event_key":key},exc_info=True); return False
    def operational(self,a):
        color={"warning":16753920,"critical":15158332,"recovery":4437377}.get(a.get("severity"),4437377)
        fields=[{"name":"Severity","value":str(a.get("severity","unknown")).title(),"inline":True},{"name":"Integration","value":str(a.get("integration","unknown")),"inline":True},{"name":"Resource","value":str(a.get("resource","unknown")),"inline":True},{"name":"Current value","value":str(a.get("value","n/a")),"inline":True},{"name":"Threshold","value":str(a.get("threshold","n/a")),"inline":True},{"name":"Host / cluster","value":str(a.get("host","unknown")),"inline":True}]
        return self._send("operational",a["key"],{"embeds":[{"title":"Endless Sentinel operational alert","description":a.get("message","Infrastructure state changed"),"color":color,"fields":fields,"timestamp":datetime.now(timezone.utc).isoformat()}]})
    def security(self,key,event):
        safe={k:str(event.get(k,"unknown"))[:250] for k in ("timestamp","source_ip","username","user_agent","event")}
        fields=[{"name":k.replace("_"," ").title(),"value":v,"inline":k not in ("user_agent",)} for k,v in safe.items()]
        return self._send("security",key,{"embeds":[{"title":"Endless Sentinel security alert","description":"Authentication security event detected.","color":15158332,"fields":fields,"timestamp":datetime.now(timezone.utc).isoformat()}]})
