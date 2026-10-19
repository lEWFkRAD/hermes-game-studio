import base64,json,pathlib,struct,tempfile,unittest
import mac_art as a
import mac_controller as c

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==')

class ArtTests(unittest.TestCase):
    def test_input_exclusivity_and_paths(self):
        self.assertEqual(PNG,a.png_bytes(PNG))
        for data in [{},{'source_job_id':'../secret'},{'source_job_id':'a'*32,'image_base64':base64.b64encode(PNG).decode()}]:
            with self.assertRaises(ValueError):a.image_spec(data,True)
        with self.assertRaises(ValueError):a.png_bytes(PNG[:16]+struct.pack('>II',999999,1)+PNG[24:])
    def test_image_bounds_and_reference_workflow(self):
        c.CFG={'models':{'image':{'qualified':True,'checkpoint':'fixture.safetensors'}}}
        p=c.validate('image',dict(prompt='cup',source_job_id='a'*32,width=768,height=768))
        w=c.image_workflow(p,'owned.png')
        self.assertEqual(['10',0],w['5']['inputs']['latent_image'])
        self.assertEqual('owned.png',w['8']['inputs']['image']);self.assertNotIn('4',w)
        for extra in [dict(width=2048),dict(steps=1000),dict(denoise=-1)]:
            with self.assertRaises(ValueError):c.validate('image',dict(prompt='cup',**extra))
    def glb(self,external=False):
        d={'asset':{'version':'2.0'},'accessors':[{'count':3}],'meshes':[{'primitives':[{'attributes':{'POSITION':0}}]}]}
        if external:d['images']=[{'uri':'https://example.com/tracker'}]
        j=json.dumps(d).encode();j+=b' '*((-len(j))%4)
        return struct.pack('<IIIII',0x46546c67,2,20+len(j),len(j),0x4e4f534a)+j
    def test_mesh_structure_and_embedded_resources(self):
        self.assertEqual(1,a.mesh_summary(self.glb())['triangles'])
        with self.assertRaises(ValueError):a.mesh_summary(self.glb()[:-1])
        with self.assertRaises(ValueError):a.mesh_summary(self.glb(True))

if __name__=='__main__':unittest.main()
