"""Hermes tools for the Mac queue; no provider fallback."""
import base64,json,os,pathlib,re,subprocess,sys,uuid
from mac_aux import call,hermes_home
from mcp.server.fastmcp import FastMCP
mcp=FastMCP('Mac auxiliary workloads')

def request_key(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,100}',value):raise ValueError('stable request_id required')
    return value

@mcp.tool()
def mac_comfy_image(request_id:str,prompt:str,negative_prompt:str='',width:int=512,height:int=512,steps:int=20,seed:int=42,image_base64:str='',source_job_id:str='',denoise:float=.65)->dict:
    """ComfyUI SDXL text-to-image or image-to-image. PNG upload up to750KB OR prior completed PNG job as reference. Dimensions256-768 in64px increments, one image, max30steps. Saves the exact API workflow. Idle/voice/memory controls apply. Reuse request_id after timeouts; poll mac_job_result."""
    payload=dict(prompt=prompt,negative_prompt=negative_prompt,width=width,height=height,steps=steps,seed=seed,image_base64=image_base64,source_job_id=source_job_id,denoise=denoise if image_base64 or source_job_id else 1)
    return call('/jobs',{'kind':'image','payload':payload,'idempotency_key':request_key(request_id)})

@mcp.tool()
def mac_image_to_3d(request_id:str,image_base64:str='',source_job_id:str='',seed:int=42)->dict:
    """Queue native Metal TRELLIS image-to-textured-GLB. Provide PNG up to750KB OR a prior completed Mac image job ID. Resolution512, one object, may take many minutes. Foreground use or voice interrupts without replay. Poll mac_job_result, then mac_fetch_artifact. No inference fallback."""
    return call('/jobs',{'kind':'mesh','payload':dict(image_base64=image_base64,source_job_id=source_job_id,seed=seed),'idempotency_key':request_key(request_id)})

@mcp.tool()
def mac_auxiliary_status()->dict:
    """Read Mac availability and durable queued job states. Never loads a model."""
    return call('/status')

@mcp.tool()
def mac_music(request_id:str,prompt:str)->dict:
    """Queue one bounded instrumental music job; poll its durable ID."""
    return call('/jobs',{'kind':'music','payload':{'prompt':prompt},'idempotency_key':request_key(request_id)})

@mcp.tool()
def mac_job_result(job_id:str)->dict:
    """Get existing Mac job/result. Interrupted jobs are not replayed. This does not submit work."""
    if not re.fullmatch(r'[0-9a-f]{32}',job_id):raise ValueError('invalid job id')
    return call('/jobs/'+job_id)

@mcp.tool()
def mac_fetch_artifact(job_id:str)->dict:
    """Copy a completed image, audio, or textured GLB to local Hermes artifacts; also saves a ComfyUI workflow when present. Does not generate."""
    r=mac_job_result(job_id)
    if r['state']!='completed':return {'state':r['state'],'reason':r.get('reason')}
    name=(r.get('result') or {}).get('artifact','')
    if not re.fullmatch(re.escape(job_id)+r'\.(png|wav|glb)',name):raise ValueError('job has no supported artifact')
    folder=hermes_home()/'mac-auxiliary-artifacts';folder.mkdir(parents=True,exist_ok=True)
    dest=folder/name
    dest.write_bytes(call('/artifacts/'+name,binary=True))
    if dest.stat().st_size!=r['result']['bytes']:raise RuntimeError('artifact length mismatch')
    result={'path':str(dest),'bytes':dest.stat().st_size,'job_id':job_id}
    workflow=r['result'].get('workflow')
    if workflow:
        if workflow!=job_id+'.json':raise ValueError('invalid workflow name')
        p=folder/(job_id+'-workflow.json');p.write_text(json.dumps(call('/workflows/'+workflow),indent=2),encoding='utf-8')
        result['workflow_path']=str(p)
    return result

@mcp.tool()
def mac_background_mode(mode:str)->dict:
    """Use busy when the operator needs the Mac; auto allows work only when idle and memory-safe. Does not override safety gates."""
    if mode not in ('busy','auto'):raise ValueError('busy or auto required')
    return call('/control',{'mode':mode})

if __name__=='__main__':mcp.run(transport='stdio')
