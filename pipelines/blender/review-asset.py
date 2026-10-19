"""Review an immutable GLB using its original materials and neutral clay views.

blender --background --factory-startup --threads 4 --python review-asset.py -- \
    --input model.glb --out-dir review --up-axis +Y --front-axis +Z

Axes describe the original glTF coordinates, before Blender's Y-up conversion.
No source file is saved or modified. No mesh smoothing, repairs, or decimation.
Outputs structure.json and ten PNGs: textured/clay front, three-quarter, back,
side, and game-camera. The game-camera is an adjustable elevated orthographic
review approximation, not a screenshot or performance test of the game engine.
"""
import argparse
from collections import Counter
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


def glb_document(path):
    with path.open('rb') as stream:
        magic, version, length = struct.unpack('<4sII', stream.read(12))
        if magic != b'glTF' or version != 2 or length != path.stat().st_size:
            raise ValueError('Input must be a complete GLB version 2 file')
        while stream.tell() < length:
            count, kind = struct.unpack('<II', stream.read(8))
            chunk = stream.read(count)
            if kind == 0x4e4f534a:
                return json.loads(chunk.rstrip(b' \0'))
    raise ValueError('GLB JSON chunk missing')


def axis_vector(label):
    result = [0.0, 0.0, 0.0]
    result['XYZ'.index(label[-1])] = -1.0 if label.startswith('-') else 1.0
    # glTF (X,Y,Z) maps to Blender (X,-Z,Y).
    return Vector((result[0], -result[2], result[1]))


def bounds(objects):
    minimum = Vector((float('inf'),) * 3)
    maximum = Vector((float('-inf'),) * 3)
    for obj in objects:
        for vertex in obj.data.vertices:
            point = obj.matrix_world @ vertex.co
            for axis in range(3):
                minimum[axis] = min(minimum[axis], point[axis])
                maximum[axis] = max(maximum[axis], point[axis])
    return {'min': list(minimum), 'max': list(maximum),
            'dimensions': list(maximum - minimum)}


def mesh_stats(obj):
    mesh = obj.data
    mesh.calc_loop_triangles()
    parents = list(range(len(mesh.vertices)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for edge in mesh.edges:
        a, b = find(edge.vertices[0]), find(edge.vertices[1])
        if a != b:
            parents[b] = a
    component_sizes = sorted(Counter(find(v.index) for v in mesh.vertices).values(), reverse=True)
    uv_rows = []
    for layer in mesh.uv_layers:
        coords = [tuple(loop.uv) for loop in layer.data]
        finite = all(math.isfinite(number) for point in coords for number in point)
        uv_rows.append({'name': layer.name, 'loopCount': len(coords), 'allFinite': finite,
                        'bounds': {'min': [min(point[i] for point in coords) for i in range(2)],
                                   'max': [max(point[i] for point in coords) for i in range(2)]}
                        if finite and coords else None})
    return {'object': obj.name, 'mesh': mesh.name,
            'vertices': len(mesh.vertices), 'edges': len(mesh.edges),
            'polygons': len(mesh.polygons), 'triangles': len(mesh.loop_triangles),
            'connectedComponents': len(component_sizes),
            'largestComponentVertexCounts': component_sizes[:20],
            'uvLayers': uv_rows,
            'colorAttributes': [{'name': item.name, 'domain': item.domain, 'dataType': item.data_type}
                                for item in mesh.color_attributes],
            'materialSlots': [slot.material.name if slot.material else None for slot in obj.material_slots],
            'vertexGroups': len(obj.vertex_groups),
            'armatureModifiers': [mod.object.name if mod.object else None
                                  for mod in obj.modifiers if mod.type == 'ARMATURE']}


def aim(obj, point):
    obj.rotation_euler = (Vector(point) - obj.location).to_track_quat('-Z', 'Y').to_euler()


def material(name, rgb, roughness):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    shader = mat.node_tree.nodes.get('Principled BSDF')
    shader.inputs['Base Color'].default_value = (*rgb, 1)
    shader.inputs['Roughness'].default_value = roughness
    return mat


def area(scene, name, position, power, size, target):
    data = bpy.data.lights.new(name, 'AREA')
    data.energy = power
    data.shape = 'DISK'
    data.size = size
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    obj.location = position
    aim(obj, target)


def original_material_stats(materials):
    """Describe imported image usage before any clay override is applied."""
    rows = []
    for mat in sorted(materials, key=lambda item: item.name):
        images = []
        if mat.node_tree:
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    image = node.image
                    images.append({'node': node.name, 'image': image.name,
                                   'dimensions': list(image.size), 'channels': image.channels,
                                   'colorSpace': image.colorspace_settings.name,
                                   'packed': image.packed_file is not None,
                                   'source': image.source, 'hasPixelData': image.has_data,
                                   'connections': [{'from': link.from_socket.name,
                                                    'toNode': link.to_node.name,
                                                    'toSocket': link.to_socket.name}
                                                   for output in node.outputs for link in output.links]})
        rows.append({'name': mat.name, 'imageNodes': images})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--out-dir', required=True, type=Path)
    choices = ['+X', '-X', '+Y', '-Y', '+Z', '-Z']
    parser.add_argument('--up-axis', choices=choices, default='+Y')
    parser.add_argument('--front-axis', choices=choices, default='+Z')
    parser.add_argument('--size', type=int, default=768)
    parser.add_argument('--samples', type=int, default=32)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--game-elevation', type=float, default=43.0,
                        help='Approximate game view elevation in degrees (default 43)')
    parser.add_argument('--game-azimuth', type=float, default=0.0,
                        help='Approximate game view azimuth relative to designated front')
    parser.add_argument('--inspect-only', action='store_true')
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
    source, output = args.input.resolve(strict=True), args.out_dir.resolve()
    if args.size < 64 or args.samples < 1 or args.threads < 1:
        parser.error('size must be at least 64; samples and threads must be positive')
    if args.up_axis[-1] == args.front_axis[-1]:
        parser.error('Up and front axes must be perpendicular')
    if not 5 <= args.game_elevation <= 85 or not math.isfinite(args.game_azimuth):
        parser.error('game-elevation must be 5..85 and game-azimuth must be finite')
    if source.parent == output:
        parser.error('Choose an output directory separate from the source GLB directory')
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'structure.json').exists():
        parser.error('Output already contains structure.json; choose a new review directory')
    source_hash = digest(source)
    doc = glb_document(source)
    # A self-contained GLB gives the source hash authority over every material image.
    # Reject remote/external resources instead of rendering bytes outside that hash.
    for item in doc.get('images', []) + doc.get('buffers', []):
        if item.get('uri') and not item['uri'].startswith('data:'):
            raise ValueError('Review requires embedded GLB resources; external URI: ' + item['uri'])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(source))
    scene = bpy.context.scene
    imported = list(scene.objects)
    meshes = [obj for obj in imported if obj.type == 'MESH']
    if not meshes or not any(len(obj.data.vertices) for obj in meshes):
        raise ValueError('GLB contains no nonempty mesh objects')
    original_bounds = bounds(meshes)
    rows = [mesh_stats(obj) for obj in meshes]
    used_materials = {slot.material for obj in meshes for slot in obj.material_slots if slot.material}
    armatures = [obj for obj in imported if obj.type == 'ARMATURE']
    report = {
        'schemaVersion': 2, 'reviewTool': 'mac-asset-production-v1',
        'createdUtc': datetime.now(timezone.utc).isoformat(),
        'status': 'inspected', 'blenderVersion': bpy.app.version_string,
        'source': {'path': str(source), 'bytes': source.stat().st_size, 'sha256': source_hash},
        'glTF': {'meshes': len(doc.get('meshes', [])),
                 'primitives': sum(len(m.get('primitives', [])) for m in doc.get('meshes', [])),
                 'materials': len(doc.get('materials', [])), 'textures': len(doc.get('textures', [])),
                 'images': [{'name': item.get('name'), 'mimeType': item.get('mimeType'),
                             'embedded': 'bufferView' in item or item.get('uri', '').startswith('data:'),
                             'externalUri': item.get('uri') if not item.get('uri', '').startswith('data:') else None}
                            for item in doc.get('images', [])],
                 'skins': len(doc.get('skins', [])), 'animations': len(doc.get('animations', []))},
        'originalMaterialDefinitions': doc.get('materials', []),
        'originalTextureDefinitions': doc.get('textures', []),
        'importedMaterials': original_material_stats(used_materials),
        'totals': {'meshObjects': len(meshes), 'uniqueMeshDatablocks': len({obj.data for obj in meshes}),
                   'vertices': sum(row['vertices'] for row in rows),
                   'triangles': sum(row['triangles'] for row in rows),
                   'connectedComponents': sum(row['connectedComponents'] for row in rows),
                   'materialsUsed': len(used_materials),
                   'meshObjectsWithUv': sum(bool(row['uvLayers']) for row in rows)},
        'meshes': rows,
        'rig': {'armatures': [{'object': obj.name, 'bones': len(obj.data.bones)} for obj in armatures],
                'actions': [action.name for action in bpy.data.actions]},
        'boundsBlenderBeforePresentationTransform': original_bounds,
        'orientation': {'sourceUpAxis': args.up_axis, 'sourceFrontAxis': args.front_axis,
                        'defaultAssumption': 'glTF +Y up / +Z front; front is not inferred from semantic anatomy',
                        'presentationUpBlender': '+Z', 'presentationFrontBlender': '-Y'},
        'notes': ['Counts describe imported base meshes at frame 1; object instances count separately.',
                  'Connected components use existing vertex/edge indices without welding UV or normal seams.',
                  'Clay images suppress all original materials and textures to reveal geometry.',
                  'Textured images use the imported original materials under the same neutral studio lighting.',
                  'Game-camera images approximate an elevated orthographic game view; they are not engine screenshots.',
                  'Metrics and renders support visual review; they do not certify topology, rigging, or game readiness.',
                  'No smoothing, repairs, decimation, rigging, or source-file writes performed.'],
        'renders': []}
    up, front = axis_vector(args.up_axis), axis_vector(args.front_axis)
    right = up.cross(front)
    source_basis = Matrix((right, front, up)).transposed()
    desired_basis = Matrix(((1, 0, 0), (0, -1, 0), (0, 0, 1)))
    rotation = (desired_basis @ source_basis.transposed()).to_4x4()
    roots = [obj for obj in imported if obj.parent not in imported]
    for obj in roots:
        obj.matrix_world = rotation @ obj.matrix_world
    bpy.context.view_layer.update()
    oriented = bounds(meshes)
    report['boundsBlenderAfterOrientation'] = oriented
    report_path = output / 'structure.json'
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    if not args.inspect_only:
        minimum, maximum = Vector(oriented['min']), Vector(oriented['max'])
        extent = max(oriented['dimensions'])
        if extent <= 0:
            raise ValueError('Mesh bounding box has zero extent')
        center = (minimum + maximum) / 2
        normalization = Matrix.Scale(2 / extent, 4) @ Matrix.Translation((-center.x, -center.y, -minimum.z))
        for obj in roots:
            obj.matrix_world = normalization @ obj.matrix_world
        bpy.context.view_layer.update()
        presentation_bounds = bounds(meshes)
        report['presentationBoundsBlender'] = presentation_bounds
        # Imported lights/cameras are excluded so source content cannot affect studio lighting.
        for obj in imported:
            if obj.type in {'LIGHT', 'CAMERA'}:
                obj.hide_render = True
        bpy.ops.mesh.primitive_plane_add(size=200, location=(0, 0, -.012))
        bpy.context.object.data.materials.append(material('Review floor', (.12, .14, .15), .85))
        scene.render.engine = 'CYCLES'
        scene.cycles.device = 'CPU'
        scene.cycles.samples = args.samples
        scene.cycles.use_denoising = True
        scene.render.threads_mode = 'FIXED'
        scene.render.threads = args.threads
        scene.render.resolution_x = scene.render.resolution_y = args.size
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_mode = 'RGB'
        scene.render.image_settings.color_depth = '8'
        scene.view_settings.view_transform = 'AgX'
        scene.world = bpy.data.worlds.new('Neutral review world')
        scene.world.use_nodes = True
        scene.world.node_tree.nodes['Background'].inputs['Color'].default_value = (.3, .3, .3, 1)
        scene.world.node_tree.nodes['Background'].inputs['Strength'].default_value = .45
        target = Vector((0, 0, presentation_bounds['dimensions'][2] / 2))
        area(scene, 'Key', (-3.5, -4.5, 5), 600, 4, target)
        area(scene, 'Fill', (4, -2, 3), 260, 4, target)
        area(scene, 'Rim', (1, 4, 4.5), 500, 3, target)
        camera_data = bpy.data.cameras.new('Review orthographic camera')
        camera_data.type = 'ORTHO'
        camera_data.clip_start, camera_data.clip_end = .01, 200
        camera = bpy.data.objects.new('Review camera', camera_data)
        scene.collection.objects.link(camera)
        scene.camera = camera
        studio_elevation = math.degrees(math.atan2(1.2, 6))
        views = [('front', 0, studio_elevation), ('three-quarter', 45, studio_elevation),
                 ('back', 180, studio_elevation), ('side', 90, studio_elevation),
                 ('game-camera', args.game_azimuth, args.game_elevation)]

        def offset_for(azimuth, elevation):
            azimuth, elevation = math.radians(azimuth), math.radians(elevation)
            return Vector((6 * math.cos(elevation) * math.sin(azimuth),
                           -6 * math.cos(elevation) * math.cos(azimuth),
                           6 * math.sin(elevation)))
        corners = [Vector((x, y, z))
                   for x in (presentation_bounds['min'][0], presentation_bounds['max'][0])
                   for y in (presentation_bounds['min'][1], presentation_bounds['max'][1])
                   for z in (presentation_bounds['min'][2], presentation_bounds['max'][2])]
        framing = []
        for _, degrees, elevation in views:
            offset = offset_for(degrees, elevation)
            inverse_rotation = (-offset).to_track_quat('-Z', 'Y').inverted()
            projected = [inverse_rotation @ (corner - target) for corner in corners]
            framing.extend(2 * max(abs(point[axis]) for point in projected) for axis in (0, 1))
        # Shared framing and lighting make all appearances and views comparable.
        camera_data.ortho_scale = max(framing) * 1.20
        for appearance in ('textured', 'clay'):
            if appearance == 'clay':
                clay = material('Review clay', (.48, .49, .47), .72)
                for mesh in {obj.data for obj in meshes}:
                    mesh.materials.clear()
                    mesh.materials.append(clay)
                    for polygon in mesh.polygons:
                        polygon.material_index = 0
            for name, degrees, elevation in views:
                camera.location = target + offset_for(degrees, elevation)
                aim(camera, target)
                path = output / (appearance + '-' + name + '.png')
                scene.render.filepath = str(path)
                bpy.ops.render.render(write_still=True)
                report['renders'].append({'appearance': appearance, 'view': name, 'path': str(path),
                                           'sha256': digest(path), 'azimuthDegrees': degrees,
                                           'elevationDegrees': elevation, 'cameraBlender': list(camera.location)})
                report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        report['renderSettings'] = {'engine': 'Cycles CPU', 'samples': args.samples, 'threads': args.threads,
                                    'imageSize': args.size, 'orthographicScale': camera_data.ortho_scale,
                                    'viewTransform': 'AgX'}
        report['status'] = 'rendered'
    report['source']['sha256AfterReview'] = digest(source)
    report['source']['unchanged'] = report['source']['sha256AfterReview'] == source_hash
    if not report['source']['unchanged']:
        raise RuntimeError('Source GLB hash changed during review')
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('REVIEW_RESULT ' + str(report_path), flush=True)


if __name__ == '__main__':
    main()
