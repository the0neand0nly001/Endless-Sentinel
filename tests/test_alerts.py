from modules.notifier import DiscordNotifier
from modules.state import MonitorState,Poller
class Response:
    def raise_for_status(self):pass
class HTTP:
    def __init__(self):self.urls=[]
    def post(self,url,**kwargs):self.urls.append(url);return Response()
def test_webhook_routing_and_dedup():
    h=HTTP();n=DiscordNotifier('https://ops','https://security',900,900,h);a={'key':'x','severity':'critical','integration':'docker','resource':'c','value':'down','threshold':'healthy','host':'h'};assert n.operational(a);assert not n.operational(a);n.security('login',{'event':'failed'});assert h.urls==['https://ops','https://security']
def test_identical_webhooks_rejected():assert DiscordNotifier('x','x').security_url==''
def test_poll_failure_does_not_crash():
    class Bad:
        def collect(self):raise RuntimeError('offline')
    class N:
        def operational(self,a):pass
    s=MonitorState();Poller(s,{'docker':Bad()},N()).poll_once();assert s.snapshot()['integrations']['docker']['status']=='offline'
