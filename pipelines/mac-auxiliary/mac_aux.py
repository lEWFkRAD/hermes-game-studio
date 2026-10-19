"""Explicitly configured queue client; no host discovery or SSH fallback."""
import json, os, pathlib, urllib.request, urllib.error, urllib.parse

def hermes_home():
    return pathlib.Path(os.environ.get('HERMES_HOME', str(pathlib.Path.home()/'.hermes'))).expanduser()

def client_path():
    return pathlib.Path(os.environ.get('MAC_AUX_CONFIG', str(hermes_home()/'mac-auxiliary-client.json'))).expanduser()

class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'Redirect refused', headers, fp)

def call(path, body=None, binary=False, timeout=30):
    cfg=json.loads(client_path().read_text())
    origin=cfg['origin'].rstrip('/')
    url=urllib.parse.urlsplit(origin)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
        raise ValueError('Configure an HTTP(S) origin without credentials, path or query')
    if url.scheme=='http' and url.hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('Plain HTTP requires loopback; use a tunnel or HTTPS')
    if not path.startswith('/') or path.startswith('//') or urllib.parse.urlsplit(path).netloc:
        raise ValueError('Relative API path required')
    key=cfg['api_key']
    if len(key)<32 or key.startswith('REPLACE'): raise ValueError('Configure a private api_key')
    req=urllib.request.Request(origin+path, data=None if body is None else json.dumps(body).encode(), headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
    with opener.open(req,timeout=timeout) as response:
        data=response.read(64*1024*1024+1)
    if len(data)>64*1024*1024: raise ValueError('Response too large')
    return data if binary else json.loads(data)
