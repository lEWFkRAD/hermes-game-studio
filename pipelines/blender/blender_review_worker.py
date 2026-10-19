"""Run only in a separate Blender --background process; never save the source."""
import argparse
import json
import sys
from pathlib import Path

import bpy


def audit_scene():
    scene = bpy.context.scene
    meshes = [obj for obj in scene.objects if obj.type == 'MESH']
    materials = {slot.material for obj in meshes for slot in obj.material_slots if slot.material}
    textures = {}
    for material in materials:
        if not material.use_nodes:
            continue
        for node in material.node_tree.nodes:
            if node.type != 'TEX_IMAGE' or not node.image:
                continue
            image = node.image
            textures[image.name] = {
                'name': image.name, 'size': list(image.size),
                'color_space': image.colorspace_settings.name,
                'packed': bool(image.packed_file),
                'path': bpy.path.abspath(image.filepath),
                'loaded': bool(image.has_data),
            }
    depsgraph = bpy.context.evaluated_depsgraph_get()
    triangles = 0
    for obj in meshes:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            mesh.calc_loop_triangles()
            triangles += len(mesh.loop_triangles)
        finally:
            evaluated.to_mesh_clear()
    return {
        'source_blend': bpy.data.filepath, 'blender_version': bpy.app.version_string,
        'scene': scene.name, 'camera': scene.camera.name if scene.camera else None,
        'mesh_objects': len(meshes), 'evaluated_triangles': triangles,
        'collections': sorted(collection.name for collection in bpy.data.collections),
        'meshes_without_uvs': sorted(obj.name for obj in meshes if not obj.data.uv_layers),
        'meshes_without_materials': sorted(obj.name for obj in meshes if not obj.data.materials),
        'materials': sorted(material.name for material in materials),
        'textures': sorted(textures.values(), key=lambda value: value['name']),
        'texture_rgba8_base_bytes': sum(t['size'][0] * t['size'][1] * 4 for t in textures.values()),
        'note': 'Base-level RGBA8 estimate excludes mipmaps and GPU/internal precision. UV presence is not UV quality validation.',
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--samples', type=int, default=32)
    parser.add_argument('--size', type=int, default=1024)
    parser.add_argument('--audit-only', action='store_true')
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    if not 1 <= args.samples <= 256 or not 128 <= args.size <= 2048:
        raise ValueError('Samples must be 1–256 and size 128–2048.')
    args.output.mkdir(parents=True, exist_ok=True)
    report = audit_scene()
    (args.output / 'audit.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    if args.audit_only:
        return
    scene = bpy.context.scene
    if scene.camera is None:
        raise ValueError('Source scene needs an active camera for a review render.')
    # Disable existing output-node file writes in this disposable process.
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'CPU'
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = True
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = 8
    ratio = args.size / max(scene.render.resolution_x, scene.render.resolution_y, 1)
    scene.render.resolution_x = max(1, round(scene.render.resolution_x * ratio))
    scene.render.resolution_y = max(1, round(scene.render.resolution_y * ratio))
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.filepath = str(args.output / 'preview.png')
    bpy.ops.render.render(write_still=True)


if __name__ == '__main__':
    main()
