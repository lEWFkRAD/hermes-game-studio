"""Package a self-contained GLB as an editable, explicitly unapproved candidate.

blender --background --factory-startup --threads 4 --python prepare-editable.py -- \
    --input raw.glb --out-dir NEW_DIRECTORY --height 1.5 --up-axis +Y --front-axis +Z

No welding, decimation, remeshing, smoothing, texture synthesis, or rig creation.
The imported geometry is normalized through object transforms beneath a new
identity root. The input is immutable; export/reimport checks guard the result.
"""
import argparse
from array import array
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

import bpy
from mathutils import Matrix, Vector


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_glb(path):
    data = path.read_bytes()
    if len(data) < 20:
        raise ValueError('Truncated GLB')
    magic, version, length = struct.unpack_from('<4sII', data)
    if magic != b'glTF' or version != 2 or length != len(data):
        raise ValueError('Expected complete GLB 2.0')
    document, binary = None, None
    offset = 12
    while offset < len(data):
        count, kind = struct.unpack_from('<II', data, offset)
        offset += 8
        payload = data[offset:offset + count]
        if len(payload) != count:
            raise ValueError('Truncated GLB chunk')
        if kind == 0x4e4f534a:
            document = json.loads(payload.rstrip(b' \0'))
        elif kind == 0x004e4942:
            binary = payload
        offset += count
    if document is None:
        raise ValueError('Missing GLB JSON')
    for item in document.get('images', []) + document.get('buffers', []):
        if item.get('uri') and not item['uri'].startswith('data:'):
            raise ValueError('External resources are not allowed: ' + item['uri'])
    return document, binary


def decode_data_uri(uri):
    header, separator, payload = uri.partition(',')
    if not separator or ';base64' not in header:
        raise ValueError('Only base64 embedded data URIs are supported')
    return base64.b64decode(payload, validate=True)


def decoded_image_signature(path):
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        image.colorspace_settings.name = 'Non-Color'
        dimensions = list(image.size)
        pixels = array('f', [0]) * len(image.pixels)
        image.pixels.foreach_get(pixels)
        return {'dimensions': dimensions, 'decodedFloat32RgbaSha256': hashlib.sha256(pixels.tobytes()).hexdigest()}
    finally:
        bpy.data.images.remove(image)


def extract_images(document, binary, directory):
    directory.mkdir()
    records = []
    for index, item in enumerate(document.get('images', [])):
        if 'bufferView' in item:
            view = document['bufferViews'][item['bufferView']]
            buffer_info = document['buffers'][view.get('buffer', 0)]
            if 'uri' in buffer_info:
                buffer_bytes = decode_data_uri(buffer_info['uri'])
            elif view.get('buffer', 0) == 0 and binary is not None:
                buffer_bytes = binary
            else:
                raise ValueError('Unsupported embedded image buffer')
            start = view.get('byteOffset', 0)
            payload = buffer_bytes[start:start + view['byteLength']]
            if len(payload) != view['byteLength']:
                raise ValueError('Truncated embedded image')
        else:
            payload = decode_data_uri(item['uri'])
        mime = item.get('mimeType')
        if mime is None and item.get('uri', '').startswith('data:'):
            mime = item['uri'][5:].split(';', 1)[0]
        extension = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}.get(mime)
        if not extension:
            raise ValueError('Unsupported lossless-extraction image MIME: ' + str(mime))
        path = directory / ('image-' + str(index).zfill(3) + extension)
        path.write_bytes(payload)
        records.append({'index': index, 'name': item.get('name'), 'mimeType': mime,
                        'path': str(path), 'bytes': len(payload), 'sha256': digest(path),
                        'extraction': 'Exact original embedded bytes; no decoding/reencoding',
                        **decoded_image_signature(path)})
    return records


def material_semantics(document, images):
    def texture(info):
        if info is None:
            return None
        tex = document['textures'][info['index']]
        if 'source' not in tex:
            raise ValueError('Extension-only texture source requires dedicated review')
        image = images[tex['source']]
        return {'pixels': image['decodedFloat32RgbaSha256'], 'dimensions': image['dimensions'],
                'texCoord': info.get('texCoord', 0), 'extensions': info.get('extensions', {})}

    result = []
    for mat in document.get('materials', []):
        pbr = mat.get('pbrMetallicRoughness', {})
        normal, occlusion = mat.get('normalTexture'), mat.get('occlusionTexture')
        result.append({'baseColorFactor': pbr.get('baseColorFactor', [1, 1, 1, 1]),
                       'metallicFactor': pbr.get('metallicFactor', 1),
                       'roughnessFactor': pbr.get('roughnessFactor', 1),
                       'baseColorTexture': texture(pbr.get('baseColorTexture')),
                       'metallicRoughnessTexture': texture(pbr.get('metallicRoughnessTexture')),
                       'normalTexture': texture(normal), 'normalScale': normal.get('scale', 1) if normal else None,
                       'occlusionTexture': texture(occlusion),
                       'occlusionStrength': occlusion.get('strength', 1) if occlusion else None,
                       'emissiveTexture': texture(mat.get('emissiveTexture')),
                       'emissiveFactor': mat.get('emissiveFactor', [0, 0, 0]),
                       'alphaMode': mat.get('alphaMode', 'OPAQUE'),
                       'alphaCutoff': mat.get('alphaCutoff', .5),
                       'doubleSided': mat.get('doubleSided', False),
                       'extensions': mat.get('extensions', {})})
    return result


def measure_scene():
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH']
    minimum, maximum = Vector((float('inf'),) * 3), Vector((float('-inf'),) * 3)
    rows = []
    for obj in meshes:
        obj.data.calc_loop_triangles()
        for vertex in obj.data.vertices:
            point = obj.matrix_world @ vertex.co
            if not all(math.isfinite(value) for value in point):
                raise ValueError('Nonfinite geometry')
            for i in range(3):
                minimum[i], maximum[i] = min(minimum[i], point[i]), max(maximum[i], point[i])
        rows.append({'object': obj.name, 'vertices': len(obj.data.vertices),
                     'triangles': len(obj.data.loop_triangles),
                     'uvLayers': [{'name': item.name, 'loops': len(item.data)} for item in obj.data.uv_layers],
                     'materialSlots': [slot.material.name if slot.material else None for slot in obj.material_slots],
                     'vertexGroups': len(obj.vertex_groups)})
    if not rows:
        raise ValueError('No mesh geometry')
    return {'boundsBlender': {'min': list(minimum), 'max': list(maximum), 'dimensions': list(maximum - minimum)},
            'meshes': rows, 'meshObjects': len(meshes),
            'vertices': sum(row['vertices'] for row in rows),
            'triangles': sum(row['triangles'] for row in rows),
            'uvLayerCount': sum(len(row['uvLayers']) for row in rows),
            'uvLoopCount': sum(layer['loops'] for row in rows for layer in row['uvLayers']),
            'rig': {'armatures': [{'name': obj.name, 'bones': len(obj.data.bones)}
                                  for obj in bpy.context.scene.objects if obj.type == 'ARMATURE'],
                    'actions': [action.name for action in bpy.data.actions]}}


def topology_fingerprint(meshes):
    h = hashlib.sha256()
    for obj in sorted(meshes, key=lambda item: item.name):
        mesh = obj.data
        h.update(obj.name.encode())
        for vertex in mesh.vertices:
            h.update(struct.pack('<3f', *vertex.co))
        for polygon in mesh.polygons:
            h.update(struct.pack('<I', len(polygon.vertices)))
            for vertex in polygon.vertices:
                h.update(struct.pack('<I', vertex))
        for uv in mesh.uv_layers:
            for loop in uv.data:
                h.update(struct.pack('<2f', *loop.uv))
    return h.hexdigest()


def source_axis(label):
    xyz = [0, 0, 0]
    xyz['XYZ'.index(label[-1])] = -1 if label.startswith('-') else 1
    return Vector((xyz[0], -xyz[2], xyz[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--height', type=float, default=1.5)
    parser.add_argument('--up-axis', choices=['+X', '-X', '+Y', '-Y', '+Z', '-Z'], default='+Y')
    parser.add_argument('--front-axis', choices=['+X', '-X', '+Y', '-Y', '+Z', '-Z'], default='+Z')
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
    if not math.isfinite(args.height) or args.height <= 0 or args.up_axis[-1] == args.front_axis[-1]:
        parser.error('Height must be positive/finite and source up/front perpendicular')
    source, output = args.input.resolve(strict=True), args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_hash = digest(source)
    doc, binary = parse_glb(source)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    original_images = extract_images(doc, binary, output / 'original-textures')
    original_materials = material_semantics(doc, original_images)
    bpy.ops.import_scene.gltf(filepath=str(source))
    bpy.context.scene.frame_set(1)
    imported = list(bpy.context.scene.objects)
    meshes = [obj for obj in imported if obj.type == 'MESH']
    original = measure_scene()
    original_fingerprint = topology_fingerprint(meshes)
    up, front = source_axis(args.up_axis), source_axis(args.front_axis)
    source_basis = Matrix((up.cross(front), front, up)).transposed()
    orientation = (Matrix(((1, 0, 0), (0, -1, 0), (0, 0, 1))) @ source_basis.transposed()).to_4x4()
    imported_roots = [obj for obj in imported if obj.parent not in imported]
    for obj in imported_roots:
        obj.matrix_world = orientation @ obj.matrix_world
    bpy.context.view_layer.update()
    oriented = measure_scene()['boundsBlender']
    low, high = Vector(oriented['min']), Vector(oriented['max'])
    if high.z <= low.z:
        raise ValueError('Input has no positive height along configured up axis')
    scale = args.height / (high.z - low.z)
    center = (low + high) / 2
    offset = Vector((-center.x, -center.y, -low.z))
    normalization = Matrix.Scale(scale, 4) @ Matrix.Translation(offset)
    candidate_root = bpy.data.objects.new('Isaac_candidate_root', None)
    bpy.context.scene.collection.objects.link(candidate_root)
    candidate_root['assetStatus'] = 'candidate-only-not-approved-game-art'
    candidate_root['sourceSha256'] = source_hash
    candidate_root['targetHeight'] = args.height
    candidate_root['rigStatus'] = 'preserved-from-input-no-rig-or-clips-created'
    for obj in imported_roots:
        world = obj.matrix_world.copy()
        obj.parent = candidate_root
        obj.matrix_world = normalization @ world
    bpy.context.view_layer.update()
    normalized = measure_scene()
    normalized_fingerprint = topology_fingerprint(meshes)
    if normalized_fingerprint != original_fingerprint:
        raise RuntimeError('Imported positions/indices/UVs changed; refusing candidate export')
    bpy.context.scene.unit_settings.system = 'METRIC'
    bpy.context.scene.unit_settings.scale_length = 1.0
    bpy.ops.object.select_all(action='DESELECT')
    for obj in [candidate_root, *imported]:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = candidate_root
    for image in bpy.data.images:
        if image.source == 'FILE' and image.has_data and image.packed_file is None:
            image.pack()
    blend_path, glb_path = output / 'editable.blend', output / 'candidate.glb'
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
    bpy.ops.export_scene.gltf(filepath=str(glb_path), export_format='GLB', use_selection=True,
                             export_yup=True, export_apply=False, export_materials='EXPORT',
                             export_texcoords=True, export_normals=True, export_animations=True,
                             export_cameras=False, export_lights=False, export_extras=True,
                             export_image_format='AUTO')
    if topology_fingerprint(meshes) != original_fingerprint:
        raise RuntimeError('Export changed imported mesh data')
    exported_doc, exported_binary = parse_glb(glb_path)
    exported_images = extract_images(exported_doc, exported_binary, output / 'roundtrip-textures')
    exported_materials = material_semantics(exported_doc, exported_images)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(glb_path))
    bpy.context.scene.frame_set(1)
    bpy.context.view_layer.update()
    roundtrip = measure_scene()
    rb = roundtrip['boundsBlender']
    tolerance = max(args.height * 1e-5, 1e-6)
    root = bpy.data.objects.get('Isaac_candidate_root')
    image_signatures = lambda rows: sorted((tuple(row['dimensions']), row['decodedFloat32RgbaSha256']) for row in rows)
    checks = {
        'sourceFileUnchanged': digest(source) == source_hash,
        'importedMeshTopologyAndUvUnchanged': normalized_fingerprint == original_fingerprint,
        'heightMatches': abs(rb['dimensions'][2] - args.height) <= tolerance,
        'baseAtZero': abs(rb['min'][2]) <= tolerance,
        'centeredX': abs(rb['min'][0] + rb['max'][0]) <= tolerance,
        'centeredDepth': abs(rb['min'][1] + rb['max'][1]) <= tolerance,
        'identityRootAtBase': root is not None and all(abs(root.matrix_world[r][c] - (1 if r == c else 0)) <= tolerance
                                                       for r in range(4) for c in range(4)),
        'triangleCountRetained': roundtrip['triangles'] == original['triangles'],
        'meshObjectCountRetained': roundtrip['meshObjects'] == original['meshObjects'],
        'uvLayerAndLoopCountsRetained': (roundtrip['uvLayerCount'], roundtrip['uvLoopCount']) ==
                                       (original['uvLayerCount'], original['uvLoopCount']),
        'materialCountRetained': len(doc.get('materials', [])) == len(exported_doc.get('materials', [])),
        'materialSemanticsRetained': original_materials == exported_materials,
        'imageCountAndDecodedPixelsRetained': image_signatures(original_images) == image_signatures(exported_images),
        'skinAndAnimationCountsRetained': all(len(doc.get(key, [])) == len(exported_doc.get(key, []))
                                            for key in ('skins', 'animations')),
    }
    report = {'schema': 'editable-asset-candidate-v1', 'createdUtc': datetime.now(timezone.utc).isoformat(),
              'status': 'candidate-only-validation-passed' if all(checks.values()) else 'candidate-only-validation-failed',
              'acceptedForGame': False, 'blenderVersion': bpy.app.version_string,
              'source': {'path': str(source), 'sha256': source_hash, 'sha256After': digest(source)},
              'tool': {'path': str(Path(__file__).resolve()), 'sha256': digest(Path(__file__))},
              'contract': {'height': args.height, 'exportUp': '+Y', 'exportFront': '+Z',
                           'baseY': 0, 'pivot': 'Identity parent root at center of base; children preserve original mesh coordinates',
                           'sourceUp': args.up_axis, 'sourceFront': args.front_axis},
              'normalization': {'uniformScale': scale, 'translationBeforeScaleBlender': list(offset),
                                 'tolerance': tolerance, 'originalGeometryFingerprint': original_fingerprint,
                                 'normalizedGeometryFingerprint': normalized_fingerprint},
              'original': original, 'normalized': normalized, 'roundtrip': roundtrip,
              'originalTextures': original_images, 'exportedTextures': exported_images,
              'originalMaterialSemantics': original_materials, 'exportedMaterialSemantics': exported_materials,
              'outputs': [{'path': str(path), 'bytes': path.stat().st_size, 'sha256': digest(path)}
                          for path in (blend_path, glb_path)],
              'checks': checks,
              'limitations': ['Candidate preparation is not artistic acceptance or engine integration.',
                              'No topology cleanup, welding, decimation, smoothing, rigging or animation creation.',
                              'Rig and UV counts are preservation checks, not animation/UV quality qualification.',
                              'Material check covers core metallic-roughness parameters/maps and retained extensions.',
                              'Original texture files are byte-exact; reexported texture pixels are checked after decoding.',
                              'Base-mesh bounds do not certify animated pose bounds.']}
    receipt = output / 'preparation.json'
    receipt.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print('PREPARATION_RESULT ' + str(receipt), flush=True)
    print(json.dumps(checks), flush=True)
    if not all(checks.values()):
        raise RuntimeError('Candidate roundtrip validation failed; inspect preparation.json')


if __name__ == '__main__':
    main()
