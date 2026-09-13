import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_deploy import m

class UpdateTests(unittest.TestCase):
    def exercise(self, corrupt=False, fail_install=False, same=False):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); manager=root/'manager.py'
            old='old manager'; source='print("new manager")\n'
            manager.write_text(source if same else old)
            sha='a'*40
            def run(args, **kwargs):
                if args[0]=='curl':
                    url=args[-1]
                    if 'api.github' in url: out=json.dumps({'sha':sha})
                    elif url.endswith('SHA256SUMS'):
                        out=('0'*64 if corrupt else hashlib.sha256(source.encode()).hexdigest())+'  deploy-3xui-dual.py\n'
                    else:
                        self.assertIn('/'+sha+'/',url);out=source
                    return subprocess.CompletedProcess(args,0,out,'')
                if fail_install and args[1]==manager: raise RuntimeError('startup failed')
                return subprocess.CompletedProcess(args,0,'relay 1.1.2 / 3X-UI v3.7.0\n','')
            with patch.object(m,'ROOT',root), patch.object(m,'run',side_effect=run), patch.object(m,'ensure_no_pending'):
                if corrupt or fail_install:
                    with self.assertRaises(RuntimeError): m.update_manager()
                    self.assertEqual(manager.read_text(),old)
                else:
                    m.update_manager();self.assertEqual(manager.read_text(),source)
                    if same: self.assertFalse((root/'manager.previous.py').exists())
                    else: self.assertEqual((root/'manager.previous.py').read_text(),old)
    def test_verified_update_and_backup(self): self.exercise()
    def test_checksum_mismatch_preserves_old(self): self.exercise(corrupt=True)
    def test_failed_startup_restores_old(self): self.exercise(fail_install=True)
    def test_latest_version_does_not_write(self): self.exercise(same=True)
