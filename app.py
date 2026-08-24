"""Endless Sentinel Flask application."""
from __future__ import annotations
import json, logging, os, secrets, threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_limiter import Limiter
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash
from modules.auth import Admin, clean_text, safe_next
from modules.docker_client import DockerClient
from modules.k3s_client import K3sClient
from modules.notifier import DiscordNotifier
from modules.proxmox_client import ProxmoxClient
from modules.state import MonitorState, Poller, utcnow

BASE=Path(__file__).resolve().parent; ENV_FILE=Path(os.getenv("ENV_FILE",BASE/".env")); load_dotenv(ENV_FILE)
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format='%(asctime)s %(levelname)s %(name)s %(message)s')
security_log=logging.getLogger("endless_sentinel.security")
def env_bool(name,default=False): return os.getenv(name,str(default)).strip().lower() in ("1","true","yes","on")
def env_int(name,default):
    try:return int(os.getenv(name,default))
    except ValueError:return int(default)
def client_ip(): return request.remote_addr or "unknown"
def integration_config():
    return {
      "proxmox":{"enabled":env_bool("PROXMOX_ENABLED",False),"host":os.getenv("PROXMOX_HOST",""),"port":env_int("PROXMOX_PORT",8006),"user":os.getenv("PROXMOX_USER",""),"token_name":os.getenv("PROXMOX_TOKEN_NAME",""),"token_value":os.getenv("PROXMOX_TOKEN_VALUE",""),"verify_ssl":env_bool("PROXMOX_VERIFY_SSL",True)},
      "k3s":{"enabled":env_bool("K3S_ENABLED",False),"kubeconfig":os.getenv("KUBECONFIG_PATH",""),"context":os.getenv("K3S_CONTEXT","")},
      "docker":{"enabled":env_bool("DOCKER_ENABLED",False),"host":os.getenv("DOCKER_HOST","unix:///var/run/docker.sock"),"tls_verify":env_bool("DOCKER_TLS_VERIFY",False),"cert_path":os.getenv("DOCKER_CERT_PATH","")}}

def create_app(test_config=None,start_poller=None):
    app=Flask(__name__); app.config.update(SECRET_KEY=os.getenv("APP_SECRET_KEY") or secrets.token_urlsafe(48),ADMIN_USERNAME=os.getenv("ADMIN_USERNAME","admin"),ADMIN_PASSWORD_HASH=os.getenv("ADMIN_PASSWORD_HASH",""),SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE="Lax",SESSION_COOKIE_SECURE=env_bool("SESSION_COOKIE_SECURE",False),PERMANENT_SESSION_LIFETIME=timedelta(minutes=env_int("SESSION_LIFETIME_MINUTES",480)),WTF_CSRF_TIME_LIMIT=3600,RATELIMIT_STORAGE_URI=os.getenv("RATELIMIT_STORAGE_URI","memory://"),TESTING=False)
    if test_config:app.config.update(test_config)
    trust=env_int("TRUST_PROXY_COUNT",0)
    if trust>0:app.wsgi_app=ProxyFix(app.wsgi_app,x_for=trust,x_proto=trust,x_host=trust)
    csrf=CSRFProtect(app); login=LoginManager(app); login.login_view="login"; login.login_message="Please sign in to continue."; login.login_message_category="warning"
    limiter=Limiter(key_func=client_ip,app=app,storage_uri=app.config["RATELIMIT_STORAGE_URI"],default_limits=[])
    notifier=app.config.get("NOTIFIER") or DiscordNotifier(os.getenv("DISCORD_WEBHOOK_URL",""),os.getenv("SECURITY_DISCORD_WEBHOOK_URL",""),env_int("ALERT_COOLDOWN_SECONDS",900),env_int("SECURITY_ALERT_COOLDOWN_SECONDS",900))
    state=app.config.get("STATE") or MonitorState(); cfg=integration_config()
    clients=app.config.get("CLIENTS") or {"proxmox":ProxmoxClient(cfg["proxmox"]),"k3s":K3sClient(cfg["k3s"]),"docker":DockerClient(cfg["docker"])}
    thresholds={k:env_int(k,d) for k,d in (("CPU_WARNING_PERCENT",80),("CPU_CRITICAL_PERCENT",95),("RAM_WARNING_PERCENT",80),("RAM_CRITICAL_PERCENT",95))}
    poller=Poller(state,clients,notifier,env_int("POLL_INTERVAL_SECONDS",30),thresholds); app.extensions.update(sentinel_state=state,sentinel_poller=poller,sentinel_notifier=notifier,sentinel_limiter=limiter)
    failures={}; lock=threading.Lock()
    @login.user_loader
    def load_user(uid):return Admin(app.config["ADMIN_USERNAME"]) if uid=="admin" else None
    @app.context_processor
    def common():return {"now":datetime.now(timezone.utc),"integration_flags":{k:v["enabled"] for k,v in integration_config().items()}}
    def security_event(kind,username):
        return {"event":kind,"timestamp":utcnow(),"source_ip":client_ip(),"username":clean_text(username.lower(),80),"user_agent":clean_text(request.user_agent.string,180)}
    minute=env_int("LOGIN_RATE_LIMIT_MINUTE",5); hour=env_int("LOGIN_RATE_LIMIT_HOUR",20)
    @app.route("/login",methods=["GET","POST"],endpoint="login")
    @limiter.limit(f"{minute} per minute; {hour} per hour",methods=["POST"])
    def login_route():
        if current_user.is_authenticated:return redirect(url_for("dashboard"))
        if request.method=="POST":
            username=clean_text(request.form.get("username",""),80); password=request.form.get("password","")
            valid=username.casefold()==app.config["ADMIN_USERNAME"].casefold() and bool(app.config["ADMIN_PASSWORD_HASH"]) and check_password_hash(app.config["ADMIN_PASSWORD_HASH"],password)
            if valid:
                login_user(Admin(app.config["ADMIN_USERNAME"]),remember=False,duration=app.config["PERMANENT_SESSION_LIFETIME"]); session.permanent=True
                with lock:failures.pop(client_ip(),None)
                flash("Signed in successfully.","success"); return redirect(safe_next(request.args.get("next"),request.host_url) or url_for("dashboard"))
            event=security_event("login_failed",username); security_log.warning(json.dumps(event,separators=(",",":")))
            with lock: failures[client_ip()]=failures.get(client_ip(),0)+1; count=failures[client_ip()]
            if count>=env_int("LOGIN_FAILURE_ALERT_THRESHOLD",5):notifier.security(f"failed:{client_ip()}",event)
            flash("Invalid username or password.","error")
        return render_template("login.html",title="Sign in | Endless Sentinel",description="Secure administrator sign-in for Endless Sentinel.")
    @app.post("/logout")
    @login_required
    def logout():logout_user();flash("You have been signed out.","success");return redirect(url_for("login"))
    @app.get("/")
    @login_required
    def dashboard():return render_template("dashboard.html",title="Dashboard | Endless Sentinel",description="Current overall health and resource status for your monitored homelab.",data=state.public(env_int("STALE_DATA_SECONDS",120)))
    @app.get("/infrastructure")
    @login_required
    def infrastructure():return render_template("infrastructure.html",title="Infrastructure | Endless Sentinel",description="Detailed Proxmox, Kubernetes and Docker infrastructure status.",data=state.public(env_int("STALE_DATA_SECONDS",120)))
    @app.get("/alerts")
    @login_required
    def alerts():return render_template("alerts.html",title="Alerts | Endless Sentinel",description="Active and recent Endless Sentinel operational alerts.",data=state.public(env_int("STALE_DATA_SECONDS",120)))
    @app.route("/settings",methods=["GET","POST"])
    @login_required
    def settings():
        current=integration_config()
        if request.method=="POST":
            name=request.form.get("integration","")
            if name not in current:abort(400)
            if current[name]["enabled"]:flash("Enabled integrations are read-only here. Reconfigure them with setup.sh to protect active credentials.","warning");return redirect(url_for("settings"))
            updates={"proxmox":{"PROXMOX_ENABLED":"true","PROXMOX_HOST":request.form.get("host",""),"PROXMOX_PORT":request.form.get("port","8006"),"PROXMOX_USER":request.form.get("user",""),"PROXMOX_TOKEN_NAME":request.form.get("token_name",""),"PROXMOX_TOKEN_VALUE":request.form.get("token_value",""),"PROXMOX_VERIFY_SSL":"true" if request.form.get("verify_ssl") else "false"},"k3s":{"K3S_ENABLED":"true","KUBECONFIG_PATH":request.form.get("kubeconfig",""),"K3S_CONTEXT":request.form.get("context","")},"docker":{"DOCKER_ENABLED":"true","DOCKER_HOST":request.form.get("docker_host",""),"DOCKER_TLS_VERIFY":"true" if request.form.get("tls_verify") else "false","DOCKER_CERT_PATH":request.form.get("cert_path","")}}[name]
            if not _atomic_env_update(ENV_FILE,updates):flash("Configuration could not be written. Check file permissions.","error")
            else:flash(f"{name.title()} was configured. Restart Endless Sentinel to activate it.","success")
            return redirect(url_for("settings"))
        safe={k:{kk:vv for kk,vv in v.items() if kk not in ("token_value",)} for k,v in current.items()}
        return render_template("settings.html",title="Integration setup | Endless Sentinel",description="Securely enable integrations that were disabled during installation.",config=safe)
    @app.get("/api/status")
    @login_required
    def api_status():return jsonify(state.public(env_int("STALE_DATA_SECONDS",120)))
    @app.post("/api/refresh")
    @login_required
    def api_refresh():return (jsonify({"accepted":True}),202) if poller.refresh() else (jsonify({"accepted":False,"message":"A refresh is already running."}),409)
    @app.get("/healthz")
    def healthz():return jsonify({"status":"ok"})
    @app.errorhandler(429)
    def too_many(error):
        if request.path=="/login":notifier.security(f"ratelimit:{client_ip()}",security_event("login_rate_limited",request.form.get("username","")))
        return render_template("429.html",title="Too many attempts | Endless Sentinel",description="Login requests have been temporarily rate limited.",retry_after=getattr(error,"description","Please try again later.")),429
    @app.errorhandler(404)
    def missing(error):return render_template("404.html",title="Page not found | Endless Sentinel",description="The requested Endless Sentinel page could not be found."),404
    @app.errorhandler(500)
    def failed(error):return render_template("500.html",title="Server error | Endless Sentinel",description="Endless Sentinel encountered an unexpected server error."),500
    @app.errorhandler(CSRFError)
    def csrf_failed(error):flash("The form expired or was invalid. Please try again.","error");return redirect(request.referrer or url_for("login"))
    if start_poller is None:start_poller=not app.config["TESTING"] and os.getenv("SENTINEL_START_POLLER","true").lower()=="true"
    if start_poller:poller.start()
    return app

def _atomic_env_update(path,updates):
    try:
        lines=path.read_text().splitlines() if path.exists() else []; keys=set(updates); out=[]
        for line in lines:
            key=line.split("=",1)[0] if "=" in line and not line.lstrip().startswith("#") else None
            if key in keys:out.append(f"{key}={updates[key]}");keys.remove(key)
            else:out.append(line)
        out.extend(f"{k}={updates[k]}" for k in keys); tmp=path.with_suffix(path.suffix+".tmp");tmp.write_text("\n".join(out)+"\n");os.chmod(tmp,0o600);os.replace(tmp,path);return True
    except OSError:logging.getLogger(__name__).exception("environment_update_failed");return False

app=create_app()
if __name__=="__main__":app.run(host=os.getenv("BIND_ADDRESS","0.0.0.0"),port=env_int("WEB_PORT",8080))
