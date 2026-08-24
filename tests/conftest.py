import os,sys
from pathlib import Path
import pytest
from werkzeug.security import generate_password_hash
sys.path.insert(0,str(Path(__file__).parents[1]))
os.environ.update(SENTINEL_START_POLLER="false",PROXMOX_ENABLED="false",K3S_ENABLED="false",DOCKER_ENABLED="false",LOGIN_RATE_LIMIT_MINUTE="50",LOGIN_RATE_LIMIT_HOUR="100")
from app import create_app
class FakeNotifier:
    def __init__(self):self.ops=[];self.sec=[]
    def operational(self,a):self.ops.append(a);return True
    def security(self,k,e):self.sec.append((k,e));return True
@pytest.fixture
def notifier():return FakeNotifier()
@pytest.fixture
def app(notifier,tmp_path):
    app=create_app({"TESTING":True,"WTF_CSRF_ENABLED":True,"SECRET_KEY":"test-secret","ADMIN_USERNAME":"root","ADMIN_PASSWORD_HASH":generate_password_hash("correct horse"),"NOTIFIER":notifier,"RATELIMIT_STORAGE_URI":"memory://"},start_poller=False)
    return app
@pytest.fixture
def client(app):return app.test_client()
def csrf(client,path="/login"):
    import re
    return re.search(rb'name="csrf_token" value="([^"]+)"',client.get(path).data).group(1).decode()
@pytest.fixture
def login(client):
    def go(next_path=None):
        path="/login"+("?next="+next_path if next_path else "");return client.post(path,data={"username":"root","password":"correct horse","csrf_token":csrf(client,path)},follow_redirects=False)
    return go
