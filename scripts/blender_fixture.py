"""Blender-only synthetic fixture for manual smoke checks; original cube + texture."""
from pathlib import Path
import sys
import bpy
out=Path(sys.argv[sys.argv.index('--')+1]).resolve();out.mkdir(parents=True,exist_ok=False)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
obj=bpy.context.object
mat=bpy.data.materials.new('FixtureMint');mat.use_nodes=True
shader=mat.node_tree.nodes.get('Principled BSDF')
image=bpy.data.images.new('OriginalFixture',width=4,height=4)
image.pixels=[0.15,0.65,0.45,1.0]*16
image.pack()
tex=mat.node_tree.nodes.new('ShaderNodeTexImage');tex.image=image
mat.node_tree.links.new(tex.outputs['Color'],shader.inputs['Base Color'])
obj.data.materials.append(mat)
bpy.ops.wm.save_as_mainfile(filepath=str(out/'source.blend'))
bpy.ops.export_scene.gltf(filepath=str(out/'source.glb'),export_format='GLB',export_image_format='AUTO')
