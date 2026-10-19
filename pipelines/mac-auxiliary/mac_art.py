"""Bounded image inputs and mesh validation shared by the Mac art lanes."""
import base64,json,re,struct

def png_bytes(raw):
    if len(raw)>4*1024**2 or len(raw)<33 or raw[:8]!=b'\x89PNG\r\n\x1a\n' or raw[12:16]!=b'IHDR':
        raise ValueError('PNG image up to 4 MiB required')
    width,height=struct.unpack('>II',raw[16:24])
    if not 1<=width<=2048 or not 1<=height<=2048:raise ValueError('PNG dimensions must be at most 2048 each')
    return raw

def image_spec(payload,required=False):
    encoded=payload.get('image_base64','');source=payload.get('source_job_id','')
    if encoded and source:raise ValueError('choose uploaded image OR completed image job')
    if source:
        if not isinstance(source,str) or not re.fullmatch(r'[0-9a-f]{32}',source):raise ValueError('invalid source job ID')
        return {'source_job_id':source}
    if encoded:
        if not isinstance(encoded,str) or len(encoded)>1000000:raise ValueError('inline PNG is limited to 750 KB')
        raw=base64.b64decode(encoded,validate=True)
        if len(raw)>750000:raise ValueError('inline PNG is limited to 750 KB')
        png_bytes(raw);return {'image_base64':encoded}
    if required:raise ValueError('image input required')
    return {}

def resolve_image(root,payload):
    if payload.get('source_job_id'):
        return png_bytes((root/'artifacts'/(payload['source_job_id']+'.png')).read_bytes())
    return png_bytes(base64.b64decode(payload['image_base64'],validate=True))

def mesh_summary(raw):
    if len(raw)<20 or len(raw)>64*1024**2:raise ValueError('GLB size outside limits')
    magic,version,length=struct.unpack('<III',raw[:12])
    if magic!=0x46546c67 or version!=2 or length!=len(raw):raise ValueError('invalid GLB header')
    size,kind=struct.unpack('<II',raw[12:20])
    if kind!=0x4e4f534a or size>4*1024**2 or 20+size>len(raw):raise ValueError('invalid GLB JSON chunk')
    doc=json.loads(raw[20:20+size]);meshes=doc.get('meshes',[])
    if not meshes:raise ValueError('GLB has no mesh')
    accessors=doc.get('accessors',[]);triangles=0;vertices=0
    for mesh in meshes:
        for p in mesh['primitives']:
            if p.get('mode',4)!=4:raise ValueError('triangle mesh required')
            a=accessors[p['attributes']['POSITION']];vertices+=a['count']
            triangles+=(accessors[p['indices']]['count'] if 'indices' in p else a['count'])//3
    if vertices<3 or triangles<1:raise ValueError('empty mesh geometry')
    if any('uri' in x for x in doc.get('buffers',[])+doc.get('images',[])):
        raise ValueError('mesh must embed all resources')
    return {'vertices':vertices,'triangles':triangles,'materials':len(doc.get('materials',[])),'textures':len(doc.get('images',[]))}
