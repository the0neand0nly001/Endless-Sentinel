from conftest import csrf
def test_successful_login(client):assert client.post('/login',data={'username':'root','password':'correct horse','csrf_token':csrf(client)}).status_code==302
def test_failed_login_is_generic(client):
    r=client.post('/login',data={'username':'nobody','password':'wrong','csrf_token':csrf(client)},follow_redirects=True);assert b'Invalid username or password' in r.data and b'nobody' not in r.data
def test_private_redirects(client):assert client.get('/infrastructure').status_code==302
def test_logout_requires_csrf(client,login):login();assert client.post('/logout').status_code==302
def test_logout_with_csrf(client,login):login();assert client.post('/logout',data={'csrf_token':csrf(client,'/')}).status_code==302
def test_safe_next(client,login):assert login('/alerts').headers['Location'].endswith('/alerts')
def test_external_next_rejected(client,login):assert login('https://evil.invalid/').headers['Location'].endswith('/')
def test_custom_404(client):assert client.get('/missing').status_code==404 and b'outside the perimeter' in client.get('/missing').data
def test_protected_api(client):assert client.get('/api/status').status_code==302
def test_api_after_login(client,login):login();assert client.get('/api/status').is_json
def test_health_public(client):assert client.get('/healthz').json=={'status':'ok'}
