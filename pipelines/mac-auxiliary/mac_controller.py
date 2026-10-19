"""Mac-only bounded auxiliary queue. No provider fallback or arbitrary commands.

Private SQLite payloads/results; public status contains no prompt content.
Every heavy job gets a fresh owned process group, terminated before another job.
Interrupted jobs are terminal, never automatically replayed.
"""
import ctypes
import base64
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import shutil
from mac_art import image_spec,resolve_image,mesh_summary
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(os.environ.get('MAC_AUX_ROOT', str(Path.home() / 'auxiliary')))
GIB = 1024**3
DB = ROOT / 'queue.sqlite3'
LOCK = threading.RLock()
WAKE = threading.Event()
ACTIVE = None
VOICE = 0
VOICE_LEASES = {}
CFG = {}

def sql(query, args=(), fetch=False):
    with LOCK, closing(sqlite3.connect(DB, timeout=10)) as db, db:
        db.row_factory = sqlite3.Row
        cur = db.execute(query, args)
        return [dict(r) for r in cur.fetchall()] if fetch else cur.rowcount

def initialize():
    ROOT.mkdir(exist_ok=True)
    if os.name=='posix':os.chmod(ROOT, 0o700)
    sql('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, dedup TEXT UNIQUE, kind TEXT, payload TEXT, state TEXT, created REAL, expires REAL, reason TEXT, result TEXT)')
    if os.name=='posix':os.chmod(DB, 0o600)
    sql("UPDATE jobs SET state='interrupted',reason='controller restarted; no automatic replay' WHERE state='running'")
    leases=ROOT/'voice-leases.json'
    if leases.exists():VOICE_LEASES.update(json.loads(leases.read_text()))

def save_leases():
    p=ROOT/'voice-leases.new';p.write_text(json.dumps(VOICE_LEASES));p.replace(ROOT/'voice-leases.json')

def run(cmd):
    return subprocess.check_output(cmd, text=True, timeout=4).strip()

def available_from_vm_stat(vm):
    page=int(re.search(r'page size of (\d+)',vm)[1])
    pages={k:int(v) for k,v in re.findall(r'^([\w -]+):\s+(\d+)\.',vm,re.M)}
    return page*(pages['Pages free']+pages['File-backed pages'])

def unmanaged_model_pids(processes, owned_group):
    found=[]
    for line in processes.splitlines():
        parts=line.strip().split(None,2)
        if len(parts)!=3:continue
        pid,group,cmd=int(parts[0]),int(parts[1]),parts[2]
        if group==owned_group:continue
        if (re.search(r'(?:^|/)llama-server(?:\s|$)',cmd) or 'ollama runner' in cmd or
            ('comfy/ComfyUI' in cmd and 'main.py' in cmd) or 'acestep.api_server' in cmd or
            '/trellis-server' in cmd or '/trellis-cli' in cmd or
            re.search(r'(?:^|/)omlx(?:\s+serve|$)',cmd) or cmd.strip()=='omlx-server'):
            found.append(pid)
    return found

def telemetry():
    try:
        vm = run(['vm_stat'])
        # Reclaimable estimate, not a claim that inactive pages are all cost-free.
        # File-backed pages are reclaimable cache; exclude anonymous inactive
        # memory, which may require swap. Do not double-count speculative pages.
        available = available_from_vm_stat(vm)
        pressure = int(run(['sysctl','-n','kern.memorystatus_vm_pressure_level']))
        therm = run(['pmset','-g','therm'])
        thermal = ('No thermal warning level' in therm and 'No performance warning level' in therm)
        q = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        q.CGEventSourceSecondsSinceLastEventType.argtypes=[ctypes.c_int,ctypes.c_uint32]
        q.CGEventSourceSecondsSinceLastEventType.restype=ctypes.c_double
        idle=q.CGEventSourceSecondsSinceLastEventType(0,0xffffffff)
        boot=int(re.search(r'sec = (\d+)',run(['sysctl','kern.boottime']))[1])
        if not 0 <= idle <= min(time.time()-boot,86400): idle=None
        processes=run(['ps','-axo','pid=,pgid=,command='])
        owned=ACTIVE[1].pid if ACTIVE else None
        unmanaged=unmanaged_model_pids(processes,owned)
        page=int(re.search(r'page size of (\d+)',vm)[1])
        swapouts=page*int(re.search(r'^Swapouts:\s+(\d+)',vm,re.M)[1])
        return {'available_bytes':available,'swapout_bytes':swapouts,'pressure':pressure,'thermal_ok':thermal,'idle_seconds':idle,'checked_at':time.time(),'unmanaged':unmanaged}
    except Exception as e:
        return {'error':type(e).__name__,'checked_at':time.time()}

def eligibility(t, kind, running=False):
    if ROOT.joinpath('BUSY').exists(): return 'operator busy'
    with LOCK:
        for k,v in list(VOICE_LEASES.items()):
            if v<time.time():VOICE_LEASES.pop(k,None)
        if VOICE_LEASES: return 'voice priority'
    if t.get('error') or time.time()-t.get('checked_at',0)>10: return 'telemetry unavailable'
    if t.get('pressure') != 1: return 'memory pressure'
    if not t.get('thermal_ok'): return 'thermal state constrained or unavailable'
    idle=t.get('idle_seconds')
    if idle is None or idle < 300: return 'foreground activity or unknown idle'
    if not running and t.get('available_bytes',0) < CFG['models'][kind]['reserve_bytes']:
        return 'insufficient memory reserve'
    if t.get('unmanaged'):return 'unmanaged model process'
    return None

def validate(kind, payload):
    if kind not in CFG['models'] or not CFG['models'][kind].get('qualified'): raise ValueError('workload not qualified')
    if kind in ('text','vision','cron'):
        if not isinstance(payload,dict) or not isinstance(payload.get('messages'),list): raise ValueError('messages required')
        byte_limit=CFG['models'][kind].get('max_request_bytes',{'text':24000,'cron':12000,'vision':1500000}[kind])
        if len(json.dumps(payload).encode()) > byte_limit: raise ValueError('request too large')
        if len(payload['messages'])>24: raise ValueError('too many messages')
        limit=payload.get('max_tokens',payload.get('max_completion_tokens',512))
        if not isinstance(limit,int) or not 1<=limit<=1024: raise ValueError('output cap is 1024 tokens')
        # Server tokenizes precisely before generation; this limit bounds ingress.
        payload=dict(payload,model=CFG['models'][kind]['alias'],max_tokens=limit,stream=False)
        payload.pop('max_completion_tokens',None)
        payload['chat_template_kwargs']={'enable_thinking':False}
        payload['n']=1
        for message in payload['messages']:
            if not isinstance(message,dict):raise ValueError('invalid message')
            content=message.get('content')
            if isinstance(content,list):
                for part in content:
                    if part.get('type')=='image_url' and not part.get('image_url',{}).get('url','').startswith(('data:image/png;base64,','data:image/jpeg;base64,')):
                        raise ValueError('images must be inline PNG or JPEG')
    elif kind in ('image','music'):
        if not isinstance(payload,dict):raise ValueError('object required')
        prompt=payload.get('prompt')
        if not isinstance(prompt,str) or not 1<=len(prompt)<=600:raise ValueError('prompt must be 1-600 characters')
        seed=payload.get('seed',1)
        if not isinstance(seed,int) or not 0<=seed<2**32:raise ValueError('invalid seed')
        original=payload;payload={'prompt':prompt,'seed':seed}
        if kind=='image':
            payload.update(image_spec(original))
            for key,default in [('width',512),('height',512)]:
                value=original.get(key,default)
                if not isinstance(value,int) or value<256 or value>768 or value%64:raise ValueError('image dimensions: multiples of 64 from 256 to 768')
                payload[key]=value
            steps=original.get('steps',20);denoise=original.get('denoise',.65 if image_spec(original) else 1)
            if not isinstance(steps,int) or not 1<=steps<=30:raise ValueError('steps must be 1-30')
            if not isinstance(denoise,(int,float)) or not .1<=denoise<=1:raise ValueError('denoise must be 0.1-1')
            negative=original.get('negative_prompt','blurry, low quality')
            if not isinstance(negative,str) or len(negative)>1000:raise ValueError('negative prompt too long')
            payload.update(steps=steps,denoise=denoise,negative_prompt=negative)
    elif kind=='mesh':
        if not isinstance(payload,dict):raise ValueError('object required')
        seed=payload.get('seed',42)
        if not isinstance(seed,int) or not 0<=seed<2**31:raise ValueError('invalid seed')
        payload=dict(image_spec(payload,True),seed=seed)
    return payload

def image_workflow(payload,input_name=None):
    workflow={
      '1':{'class_type':'CheckpointLoaderSimple','inputs':{'ckpt_name':CFG['models']['image']['checkpoint']}},
      '2':{'class_type':'CLIPTextEncode','inputs':{'text':payload['prompt'],'clip':['1',1]}},
      '3':{'class_type':'CLIPTextEncode','inputs':{'text':payload.get('negative_prompt','blurry, low quality'),'clip':['1',1]}},
      '4':{'class_type':'EmptyLatentImage','inputs':{'width':payload.get('width',512),'height':payload.get('height',512),'batch_size':1}},
      '5':{'class_type':'KSampler','inputs':{'model':['1',0],'positive':['2',0],'negative':['3',0],'latent_image':['4',0],'seed':payload['seed'],'steps':payload.get('steps',20),'cfg':7,'sampler_name':'euler','scheduler':'normal','denoise':payload.get('denoise',1)}},
      '6':{'class_type':'VAEDecode','inputs':{'samples':['5',0],'vae':['1',2]}},
      '7':{'class_type':'SaveImage','inputs':{'images':['6',0],'filename_prefix':'mac_auxiliary'}}}
    if input_name:
        workflow['8']={'class_type':'LoadImage','inputs':{'image':input_name}}
        workflow['9']={'class_type':'ImageScale','inputs':{'image':['8',0],'width':payload['width'],'height':payload['height'],'upscale_method':'lanczos','crop':'center'}}
        workflow['10']={'class_type':'VAEEncode','inputs':{'pixels':['9',0],'vae':['1',2]}}
        workflow['5']['inputs']['latent_image']=['10',0]
        del workflow['4']
    return workflow

def generate(model,job,payload,proc):
    if job['kind'] in ('text','vision','cron'):
        return request(model['port'],'/v1/chat/completions',payload,model.get('timeout',240))
    if job['kind']=='mesh':
        raw=resolve_image(ROOT,payload);boundary='macaux_'+job['id']
        body=('--'+boundary+'\r\nContent-Disposition: form-data; name="seed"\r\n\r\n'+str(payload['seed'])+'\r\n--'+boundary+'\r\nContent-Disposition: form-data; name="resolution"\r\n\r\n512\r\n--'+boundary+'\r\nContent-Disposition: form-data; name="image"; filename="input.png"\r\nContent-Type: image/png\r\n\r\n').encode()+raw+('\r\n--'+boundary+'--\r\n').encode()
        conn=http.client.HTTPConnection('127.0.0.1',model['port'],timeout=model['timeout'])
        try:
            conn.request('POST','/generate',body,{'Content-Type':'multipart/form-data; boundary='+boundary})
            r=conn.getresponse();data=r.read(64*1024**2+1)
            if r.status!=200:raise RuntimeError('3D backend HTTP '+str(r.status))
        finally:conn.close()
        summary=mesh_summary(data);dest=ROOT/'artifacts'/(job['id']+'.glb');dest.parent.mkdir(exist_ok=True);dest.write_bytes(data)
        return dict(summary,artifact=dest.name,bytes=len(data),model='TRELLIS.2 Q8 Metal',resolution=512,input_sha256=hashlib.sha256(raw).hexdigest())
    if job['kind']=='image':
        input_name=None
        if payload.get('image_base64') or payload.get('source_job_id'):
            input_name='mac_aux_'+job['id']+'.png'
            ((Path(model['comfy_root'])/'input')/input_name).write_bytes(resolve_image(ROOT,payload))
        workflow=image_workflow(payload,input_name)
        record=ROOT/'workflows';record.mkdir(exist_ok=True);(record/(job['id']+'.json')).write_text(json.dumps(workflow,indent=2))
        pid=request(model['port'],'/prompt',{'prompt':workflow})['prompt_id']
        while proc.poll() is None:
            h=request(model['port'],'/history/'+pid)
            if pid in h:
                h=h[pid]
                if h.get('status',{}).get('status_str')=='error':raise RuntimeError('image generation failed')
                outputs=[i for o in h.get('outputs',{}).values() for i in o.get('images',[])]
                if not outputs:raise RuntimeError('no image output')
                i=outputs[0];root=(Path(model['comfy_root'])/'output').resolve()
                source=(root/i.get('subfolder','')/i['filename']).resolve()
                if root not in source.parents:raise RuntimeError('invalid output path')
                dest=ROOT/'artifacts'/(job['id']+'.png');dest.parent.mkdir(exist_ok=True)
                shutil.copyfile(source,dest)
                return {'artifact':dest.name,'bytes':dest.stat().st_size,'model':'SDXL','width':payload.get('width',512),'height':payload.get('height',512),'workflow':job['id']+'.json','mode':'image-to-image' if input_name else 'text-to-image'}
            time.sleep(1)
        raise RuntimeError('image backend interrupted')
    if job['kind']=='music':
        task=request(model['port'],'/release_task',{'prompt':payload['prompt'],'lyrics':'[Instrumental]','audio_duration':20,'batch_size':1,'inference_steps':8,'seed':payload['seed'],'use_random_seed':False,'thinking':False,'use_cot_metas':False,'use_cot_caption':False,'use_cot_language':False,'audio_format':'wav'})
        if task.get('code')!=200:raise RuntimeError('music task rejected')
        taskid=task['data']['task_id']
        while proc.poll() is None:
            data=request(model['port'],'/query_result',{'task_id_list':[taskid]})['data']
            if data and data[0]['status']==2:raise RuntimeError('music generation failed')
            if data and data[0]['status']==1:
                info=json.loads(data[0]['result'])[0];url=info['file']
                if not url.startswith('/v1/audio?path='):raise RuntimeError('invalid audio result URL')
                conn=http.client.HTTPConnection('127.0.0.1',model['port'],timeout=20)
                try:
                    conn.request('GET',url);r=conn.getresponse();raw=r.read(20*1024*1024+1)
                    if r.status!=200 or len(raw)>20*1024*1024 or not raw.startswith(b'RIFF'):raise RuntimeError('invalid or excessive WAV output')
                finally:conn.close()
                dest=ROOT/'artifacts'/(job['id']+'.wav');dest.parent.mkdir(exist_ok=True);dest.write_bytes(raw)
                return {'artifact':dest.name,'bytes':len(raw),'model':'ACE-Step1.5-turbo','requested_seconds':20,'mode':'instrumental'}
            time.sleep(1)
        raise RuntimeError('music backend interrupted')
    raise RuntimeError('backend not implemented')

def submit(kind,payload,key=None):
    payload=validate(kind,payload)
    body=json.dumps(payload,separators=(',',':'))
    if key and (not isinstance(key,str) or len(key)>120): raise ValueError('invalid idempotency key')
    with LOCK:
        if key:
            old=sql('SELECT * FROM jobs WHERE dedup=?',(key,),True)
            if old:
                if old[0]['kind']!=kind or old[0]['payload']!=body: raise ValueError('idempotency key conflicts')
                return old[0]['id']
        if sql("SELECT count(*) n FROM jobs WHERE state IN ('queued','running')",fetch=True)[0]['n']>=16: raise ValueError('queue full')
        jid=uuid.uuid4().hex
        sql('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)',(jid,key,kind,body,'queued',time.time(),time.time()+3600,'waiting',None))
    WAKE.set()
    return jid

def stop(proc):
    if proc.poll() is not None:return
    # Signal the watchdog once; it forwards TERM to the entire owned group.
    # Sending TERM to the group here would hit the backend twice.
    try:proc.terminate()
    except ProcessLookupError:return
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=5)

def request(port,path,payload=None,timeout=120,json_response=True):
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=timeout)
    try:
        c.request('GET' if payload is None else 'POST',path,body=None if payload is None else json.dumps(payload),headers={'Content-Type':'application/json'})
        r=c.getresponse();body=r.read(8*1024*1024)
        if r.status!=200:raise RuntimeError('backend HTTP '+str(r.status))
        return json.loads(body) if json_response else body.decode()
    finally:c.close()

def execute(job):
    global ACTIVE
    model=CFG['models'][job['kind']]
    before=telemetry()
    if 'swapout_bytes' not in before:raise RuntimeError('swap telemetry unavailable')
    log=ROOT/'runtime.log'
    if log.exists() and log.stat().st_size>4*1024*1024:log.rename(ROOT/'runtime.previous.log')
    with log.open('ab',buffering=0) as out:
        proc=subprocess.Popen([sys.executable,str(Path(__file__).with_name('mac_runner.py')),json.dumps(model['cmd'])],cwd=model.get('cwd',str(ROOT)),env=dict(os.environ,**model.get('env',{})),stdout=out,stderr=out,start_new_session=True)
    with LOCK:ACTIVE=(job['id'],proc)
    finished=threading.Event(); reason=[]
    def guard():
        deadline=time.monotonic()+model.get('timeout',240)
        while not finished.wait(2):
            status=sql('SELECT state FROM jobs WHERE id=?',(job['id'],),True)[0]['state']
            current=telemetry()
            why=eligibility(current,job['kind'],True)
            if current.get('swapout_bytes',before['swapout_bytes'])-before['swapout_bytes']>256*1024**2:why='swap growth budget exceeded'
            if status!='running':why='request canceled'
            if time.monotonic()>deadline:why='runtime limit'
            if why:
                reason.append(why);stop(proc);return
    watcher=threading.Thread(target=guard,daemon=True);watcher.start()
    try:
        for _ in range(120):
            if proc.poll() is not None:raise RuntimeError(reason[0] if reason else 'backend exited')
            try:
                request(model['port'],model.get('health','/health'),timeout=1,json_response=not model.get('health_plain',False));break
            except Exception:time.sleep(1)
        else:raise RuntimeError('model startup timeout')
        payload=json.loads(job['payload'])
        # llama.cpp applies its own context validation, never silently truncate.
        result=generate(model,job,payload,proc)
        if reason:raise RuntimeError(reason[0])
        sql("UPDATE jobs SET state='completed',reason=NULL,result=? WHERE id=? AND state='running'",(json.dumps(result),job['id']))
    except Exception as e:
        sql("UPDATE jobs SET state='interrupted',reason=? WHERE id=? AND state='running'",((reason[0] if reason else str(e))[:160],job['id']))
    finally:
        finished.set();watcher.join(timeout=12);stop(proc)
        with LOCK:ACTIVE=None

def worker():
    while True:
        sql("UPDATE jobs SET state='expired',reason='queue deadline' WHERE state='queued' AND expires<?",(time.time(),))
        # Retention is bounded; completed content is private and short-lived.
        sql("DELETE FROM jobs WHERE state NOT IN ('queued','running') AND created<?",(time.time()-86400,))
        for job in sql("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 16",fetch=True):
            why=eligibility(telemetry(),job['kind'])
            if why:
                sql('UPDATE jobs SET reason=? WHERE id=?',(why,job['id']));continue
            with LOCK:
                if not sql("UPDATE jobs SET state='running',reason=NULL WHERE id=? AND state='queued'",(job['id'],)):continue
            try:execute(job)
            except Exception as e:
                sql("UPDATE jobs SET state='failed',reason=? WHERE id=?",(type(e).__name__,job['id']))
            break
        WAKE.wait(5);WAKE.clear()

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,code,value):
        b=json.dumps(value).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers()
        try:self.wfile.write(b)
        except (BrokenPipeError,ConnectionResetError):pass
    def authenticated(self):
        import hmac
        return hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+CFG['api_key'])
    def do_GET(self):
        if self.path=='/health':
            self.reply(200,{'status':'ok','service':'mac-auxiliary-controller','model_loaded':ACTIVE is not None});return
        if not self.authenticated():self.reply(401,{'error':'unauthorized'});return
        if self.path=='/v1/models':
            self.reply(200,{'object':'list','data':[{'id':v['alias'],'object':'model'} for k,v in CFG['models'].items() if k in ('text','vision','cron') and v.get('qualified')]});return
        if self.path=='/status':
            self.reply(200,{'telemetry':telemetry(),'active':ACTIVE[0] if ACTIVE else None,'voice':len(VOICE_LEASES),'busy':(ROOT/'BUSY').exists(),'queue':sql('SELECT id,kind,state,reason,created FROM jobs ORDER BY created DESC LIMIT 20',fetch=True)});return
        if self.path.startswith('/workflows/'):
            name=self.path.split('/')[-1]
            if not re.fullmatch(r'[0-9a-f]{32}\.json',name):self.reply(400,{'error':'invalid workflow'});return
            p=ROOT/'workflows'/name
            if not p.is_file():self.reply(404,{'error':'not found'});return
            self.reply(200,json.loads(p.read_text()));return
        if self.path.startswith('/artifacts/'):
            name=self.path.split('/')[-1]
            if not re.fullmatch(r'[0-9a-f]{32}\.(png|wav|glb)',name):self.reply(400,{'error':'invalid artifact'});return
            p=ROOT/'artifacts'/name
            if not p.is_file():self.reply(404,{'error':'not found'});return
            body=p.read_bytes();self.send_response(200);self.send_header('Content-Type',{'png':'image/png','wav':'audio/wav','glb':'model/gltf-binary'}[name.rsplit('.',1)[-1]]);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
        if self.path.startswith('/jobs/'):
            r=sql('SELECT id,kind,state,reason,result FROM jobs WHERE id=?',(self.path.split('/')[-1],),True)
            if r and r[0]['result']:r[0]['result']=json.loads(r[0]['result'])
            self.reply(200 if r else 404,r[0] if r else {'error':'not found'});return
        self.reply(404,{'error':'not found'})
    def do_POST(self):
        if not self.authenticated():self.reply(401,{'error':'unauthorized'});return
        jid=None
        try:
            n=int(self.headers.get('Content-Length','0'))
            if not 0<n<=1500000:raise ValueError('body size outside limits')
            self.connection.settimeout(15)
            body=json.loads(self.rfile.read(n))
            if self.path=='/speech':
                text=body.get('text')
                if not isinstance(text,str) or not 1<=len(text)<=600:raise ValueError('speech text must be 1-600 characters')
                # The existing speech service acquires/renews/releases the voice lease.
                result=request(8033,'/tts',{'text':text,'output':'-'},timeout=120)
                self.reply(200,result);return
            if self.path=='/transcribe':
                raw=base64.b64decode(body.get('audio_base64',''),validate=True)
                if len(raw)>750000 or raw[:4]!=b'RIFF' or raw[8:12]!=b'WAVE':raise ValueError('WAV audio up to 750KB required')
                conn=http.client.HTTPConnection('127.0.0.1',8034,timeout=120)
                try:
                    conn.request('POST','/stt',raw,{'Content-Type':'audio/wav'})
                    response=conn.getresponse();result=json.loads(response.read())
                    self.reply(response.status,result);return
                finally:conn.close()
            if self.path=='/control':
                if body.get('mode')=='busy':(ROOT/'BUSY').touch()
                elif body.get('mode')=='auto':(ROOT/'BUSY').unlink(missing_ok=True)
                else:raise ValueError('mode must be busy or auto')
                WAKE.set();self.reply(200,{'mode':body['mode']});return
            if self.path=='/voice/renew':
                with LOCK:
                    lease=body.get('lease')
                    if lease not in VOICE_LEASES:self.reply(409,{'error':'lease lost'});return
                    VOICE_LEASES[lease]=time.time()+180
                    save_leases()
                self.reply(200,{'status':'renewed'});return
            if self.path=='/voice/begin':
                lease=uuid.uuid4().hex
                with LOCK:
                    VOICE_LEASES[lease]=time.time()+180;save_leases()
                deadline=time.monotonic()+15
                while ACTIVE and time.monotonic()<deadline:time.sleep(.1)
                if ACTIVE:
                    with LOCK:
                        VOICE_LEASES.pop(lease,None);save_leases()
                    self.reply(503,{'error':'heavy model did not release'});return
                self.reply(200,{'lease':lease});return
            if self.path=='/voice/end':
                with LOCK:
                    VOICE_LEASES.pop(body.get('lease'),None);save_leases()
                self.reply(200,{'status':'released'});return
            if self.path=='/jobs':
                jid=submit(body.get('kind'),body.get('payload'),body.get('idempotency_key'));self.reply(202,{'id':jid});return
            if self.path.startswith('/cancel/'):
                sql("UPDATE jobs SET state='canceled',reason='client canceled' WHERE id=? AND state IN ('queued','running')",(self.path.split('/')[-1],));self.reply(200,{'status':'canceled'});return
            if self.path!='/v1/chat/completions':self.reply(404,{'error':'not found'});return
            kind=next((k for k,v in CFG['models'].items() if v['alias']==body.get('model')),None)
            if kind not in ('text','vision','cron'):raise ValueError('unknown chat model')
            # Interactive clients must not wait in a hidden background queue.
            why=eligibility(telemetry(),kind)
            if why:self.reply(503,{'error':{'message':why,'type':'mac_deferred'}});return
            jid=submit(kind,body,self.headers.get('Idempotency-Key'))
            deadline=time.monotonic()+min(1830,max(300,CFG['models'][kind].get('timeout',240)+30))
            while time.monotonic()<deadline:
                r=sql('SELECT state,result,reason FROM jobs WHERE id=?',(jid,),True)[0]
                if r['state']=='completed':
                    # Complete the handoff before an interactive client starts its next model.
                    if ACTIVE and ACTIVE[0]==jid:
                        time.sleep(.1);continue
                    result=json.loads(r['result'])
                    if body.get('stream'):
                        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                        for choice in result['choices']:
                            chunk=dict(result,object='chat.completion.chunk',choices=[{'index':choice['index'],'delta':choice['message'],'finish_reason':choice['finish_reason']}])
                            self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                        self.wfile.write(b'data: [DONE]\n\n');return
                    self.reply(200,result);return
                if r['state'] not in ('queued','running'):self.reply(503,{'error':{'message':r['reason'],'type':'mac_interrupted'}});return
                time.sleep(.25)
            sql("UPDATE jobs SET state='canceled',reason='client deadline' WHERE id=? AND state IN ('queued','running')",(jid,));self.reply(504,{'error':'deadline'})
        except (ValueError,TypeError) as e:self.reply(400,{'error':str(e)})
        except (BrokenPipeError,ConnectionResetError,TimeoutError):
            if jid:sql("UPDATE jobs SET state='canceled',reason='client disconnected' WHERE id=? AND state IN ('queued','running')",(jid,))

def main():
    global CFG
    import fcntl
    os.umask(0o077)
    CFG=json.loads((ROOT/'controller.json').read_text())
    if CFG.get('bind') not in ('127.0.0.1','localhost'):
        raise ValueError('Bind to loopback; use an SSH tunnel for remote clients')
    if len(CFG.get('api_key','')) < 32 or CFG['api_key'].startswith('REPLACE'):
        raise ValueError('Set a private random api_key of at least 32 characters')
    initialize()
    lock=(ROOT/'controller.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    threading.Thread(target=worker,daemon=True).start()
    def shutdown(*_):
        if ACTIVE:stop(ACTIVE[1])
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,shutdown)
    ThreadingHTTPServer((CFG['bind'],CFG['port']),Handler).serve_forever()

if __name__=='__main__':main()
