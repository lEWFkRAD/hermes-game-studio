import importlib.util
import pathlib
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('controller',pathlib.Path(__file__).with_name('mac_controller.py'))
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();c.ROOT=pathlib.Path(self.temp.name);c.DB=c.ROOT/'queue.sqlite3'
        c.CFG={'models':{'text':{'qualified':True,'alias':'fixture-model','reserve_bytes':20*c.GIB}}}
        c.VOICE_LEASES.clear();c.initialize()
        self.payload={'messages':[{'role':'user','content':'Fixture only'}],'max_tokens':16}
        self.t={'pressure':1,'thermal_ok':True,'idle_seconds':600,'checked_at':time.time(),'available_bytes':22*c.GIB}
    def tearDown(self):self.temp.cleanup()
    def test_model_process_group_ownership(self):
        ps='100 100 python mac_runner.py\n101 100 /opt/homebrew/bin/llama-server --model x\n200 200 /opt/homebrew/bin/ollama runner --model y\n'
        self.assertEqual([200],c.unmanaged_model_pids(ps,100))
        self.assertEqual([101,200],c.unmanaged_model_pids(ps,None))
        alternate='300 300 /Users/example/auxiliary/runtimes/b10964/llama-b10964/llama-server --model x'
        self.assertEqual([300],c.unmanaged_model_pids(alternate,None))
        self.assertEqual([],c.unmanaged_model_pids(alternate,300))
        self.assertEqual([400],c.unmanaged_model_pids('400 400 /Users/example/auxiliary/runtimes/omlx/bin/omlx serve',None))
        self.assertEqual([],c.unmanaged_model_pids('400 400 omlx-server',400))
        t=dict(self.t,unmanaged=[200])
        self.assertEqual('unmanaged model process',c.eligibility(t,'text',True))
    def test_idempotency_and_conflict(self):
        a=c.submit('text',self.payload,'a');self.assertEqual(a,c.submit('text',self.payload,'a'))
        with self.assertRaises(ValueError):c.submit('text',dict(self.payload,max_tokens=17),'a')
    def test_restart_does_not_replay(self):
        a=c.submit('text',self.payload);c.sql("UPDATE jobs SET state='running' WHERE id=?",(a,));c.initialize()
        self.assertEqual('interrupted',c.sql('SELECT state FROM jobs',fetch=True)[0]['state'])
    def test_bounded_queue(self):
        for _ in range(16):c.submit('text',self.payload)
        with self.assertRaises(ValueError):c.submit('text',self.payload)
    def test_caps_and_unknown(self):
        for kind,p in [('music',self.payload),('text',dict(self.payload,max_tokens=99999)),('text',{'messages':['x'*25000]})]:
            with self.assertRaises(ValueError):c.submit(kind,p)
    def test_fail_closed(self):
        self.assertIsNone(c.eligibility(self.t,'text'))
        for change in [{'idle_seconds':None},{'idle_seconds':2},{'pressure':2},{'thermal_ok':False},{'available_bytes':10*c.GIB},{'unmanaged':[123]},{'checked_at':0}]:
            self.assertIsNotNone(c.eligibility(dict(self.t,**change),'text'))
    def test_configured_long_text_remains_bounded(self):
        large={'messages':[{'role':'user','content':' apple'*90000}],'max_tokens':128}
        with self.assertRaises(ValueError):c.validate('text',large)
        c.CFG['models']['text']['max_request_bytes']=1200000
        self.assertEqual(128,c.validate('text',large)['max_tokens'])
        with self.assertRaises(ValueError):c.validate('text',{'messages':[{'role':'user','content':'x'*1200001}]})
    def test_stop_signals_watchdog_once_then_preserves_kill_deadline(self):
        proc=Mock(pid=123);proc.poll.return_value=None
        with patch.object(c.os,'killpg',create=True) as group:
            c.stop(proc);proc.terminate.assert_called_once();group.assert_not_called()
        proc.reset_mock();proc.wait.side_effect=[c.subprocess.TimeoutExpired('watchdog',5),0]
        with patch.object(c.os,'killpg',create=True) as group,patch.object(c.signal,'SIGKILL',9,create=True):
            c.stop(proc);proc.terminate.assert_called_once();group.assert_called_once_with(123,c.signal.SIGKILL)
    def test_busy_and_voice_priority(self):
        (c.ROOT/'BUSY').touch();self.assertEqual('operator busy',c.eligibility(self.t,'text',True));(c.ROOT/'BUSY').unlink()
        c.VOICE_LEASES['test']=time.time()+10;self.assertEqual('voice priority',c.eligibility(self.t,'text',True))
        c.VOICE_LEASES['test']=time.time()-1;self.assertIsNone(c.eligibility(self.t,'text',True))
    def test_memory_parser_includes_hyphenated_file_backed_counter(self):
        vm='Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 10.\nFile-backed pages: 20.\nPages inactive: 999.\n'
        self.assertEqual(30*16384,c.available_from_vm_stat(vm))
        with self.assertRaises(KeyError):c.available_from_vm_stat(vm.replace('File-backed pages: 20.\n',''))

if __name__=='__main__':unittest.main()
