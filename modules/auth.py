"""Authentication helpers."""
from urllib.parse import urljoin,urlparse
from flask_login import UserMixin
class Admin(UserMixin):
    id="admin"
    def __init__(self,username):self.username=username
def safe_next(target,host_url):
    if not target:return None
    ref=urlparse(host_url); test=urlparse(urljoin(host_url,target))
    return target if test.scheme in ("http","https") and ref.netloc==test.netloc else None
def clean_text(value,limit=160): return "".join(c for c in (value or "") if c.isprintable())[:limit]
