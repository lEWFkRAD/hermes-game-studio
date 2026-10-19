"""Explicit transport configuration checks; no sockets or credentials."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import mac_aux

class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config=Path(self.temp.name)/'client.json'
        p=patch.object(mac_aux,'client_path',return_value=self.config);p.start();self.addCleanup(p.stop)
    def configure(self,origin='http://127.0.0.1:18080',key='x'*40):
        self.config.write_text(json.dumps({'origin':origin,'api_key':key}))
    def test_missing_configuration_never_discovers_a_host(self):
        with patch.object(mac_aux.urllib.request,'build_opener') as op:
            with self.assertRaises(FileNotFoundError):mac_aux.call('/status')
            op.assert_not_called()
    def test_rejects_plain_remote_and_embedded_credentials(self):
        for origin in ['http://example.com','https://user:secret@example.com','https://example.com/path']:
            self.configure(origin)
            with self.assertRaises(ValueError):mac_aux.call('/status')
    def test_placeholder_and_absolute_path_rejected(self):
        self.configure(key='REPLACE_WITH_A_PRIVATE_RANDOM_KEY')
        with self.assertRaises(ValueError):mac_aux.call('/status')
        self.configure()
        with self.assertRaises(ValueError):mac_aux.call('//example.com/status')
    def test_one_authenticated_request_without_retry(self):
        self.configure();op=MagicMock();op.open.return_value.__enter__.return_value.read.return_value=b'{"ok":true}'
        with patch.object(mac_aux.urllib.request,'build_opener',return_value=op):
            self.assertEqual({'ok':True},mac_aux.call('/status'))
        op.open.assert_called_once();req=op.open.call_args.args[0]
        self.assertEqual('http://127.0.0.1:18080/status',req.full_url)
        self.assertEqual('Bearer '+'x'*40,req.get_header('Authorization'))
    def test_timeout_is_not_resubmitted(self):
        self.configure();op=MagicMock();op.open.side_effect=TimeoutError('fixture')
        with patch.object(mac_aux.urllib.request,'build_opener',return_value=op):
            with self.assertRaises(TimeoutError):mac_aux.call('/jobs',{'idempotency_key':'fixture'})
        op.open.assert_called_once()
