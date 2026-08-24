import os
from werkzeug.security import generate_password_hash
from app import create_app
def test_login_rate_limit_429(notifier):
    os.environ['LOGIN_RATE_LIMIT_MINUTE']='2';os.environ['LOGIN_RATE_LIMIT_HOUR']='20'
    app=create_app({'TESTING':True,'WTF_CSRF_ENABLED':False,'SECRET_KEY':'x','ADMIN_USERNAME':'root','ADMIN_PASSWORD_HASH':generate_password_hash('pw'),'NOTIFIER':notifier,'RATELIMIT_STORAGE_URI':'memory://'},start_poller=False);c=app.test_client()
    assert c.post('/login',data={'username':'x','password':'x'}).status_code==200
    assert c.post('/login',data={'username':'x','password':'x'}).status_code==200
    assert c.post('/login',data={'username':'x','password':'x'}).status_code==429
    assert notifier.sec[-1][1]['event']=='login_rate_limited'
