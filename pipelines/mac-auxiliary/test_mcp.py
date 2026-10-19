"""Exercise the MCP adapter with a minimal registration stub; no server/network."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
class MCP:
    def __init__(self,*args):self.tools={}
    def tool(self):
        def register(fn):self.tools[fn.__name__]=fn;return fn
        return register
stub=types.ModuleType('mcp.server.fastmcp');stub.FastMCP=MCP
spec=importlib.util.spec_from_file_location('asset_mcp',Path(__file__).with_name('mac_aux_mcp.py'))
with patch.dict(sys.modules,{'mcp.server.fastmcp':stub}):
    adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
class MCPTests(unittest.TestCase):
    def test_published_tools_are_asset_scoped(self):
        self.assertNotIn('mac_speak',adapter.mcp.tools)
        self.assertNotIn('mac_submit_work',adapter.mcp.tools)
        self.assertIn('mac_music',adapter.mcp.tools)
    def test_mesh_request_preserves_stable_key(self):
        with patch.object(adapter,'call',return_value={'id':'a'*32}) as call:
            adapter.mac_image_to_3d('prop-001',source_job_id='b'*32)
        self.assertEqual('prop-001',call.call_args.args[1]['idempotency_key'])
        self.assertEqual('b'*32,call.call_args.args[1]['payload']['source_job_id'])
    def test_invalid_identifier_cannot_fetch(self):
        with patch.object(adapter,'call') as call:
            with self.assertRaises(ValueError):adapter.mac_job_result('../escape')
        call.assert_not_called()
