import contextlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_deploy import m


class ShortcutTests(unittest.TestCase):
    def test_command_default_and_argument_forwarding(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); command=root/'relay'; manager=root/'manager.py'
            manager.write_text('import json,sys; print(json.dumps(sys.argv[1:]))')
            text=m.SHORTCUT_TEXT.replace('/root/3xui-dual/manager.py',shlex.quote(str(manager)))
            with patch.object(m,'SHORTCUT',command), patch.object(m,'SHORTCUT_TEXT',text):
                self.assertTrue(m.install_shortcut())
            self.assertEqual(command.stat().st_mode & 0o777,0o700)
            for args, expected in [([],['--menu']),(['--check'],['--check']),(['--copy','3'],['--copy','3'])]:
                out=subprocess.check_output([str(command),*args],text=True)
                self.assertEqual(json.loads(out),expected)

    def test_unrelated_command_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            command=Path(td)/'relay';command.write_text('unrelated program')
            with patch.object(m,'SHORTCUT',command):
                self.assertFalse(m.install_shortcut())
            self.assertEqual(command.read_text(),'unrelated program')

    def test_symlink_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            command=Path(td)/'relay';target=Path(td)/'other';target.write_text('unchanged');command.symlink_to(target)
            with patch.object(m,'SHORTCUT',command):
                self.assertFalse(m.install_shortcut())
            self.assertTrue(command.is_symlink());self.assertEqual(target.read_text(),'unchanged')

    def test_existing_shortcut_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as td:
            command=Path(td)/'relay'
            command.write_text(m.SHORTCUT_TEXT);command.chmod(0o700)
            before=command.stat().st_mtime_ns
            with patch.object(m,'SHORTCUT',command), patch.object(m,'say') as say:
                self.assertTrue(m.install_shortcut())
                say.assert_not_called()
            self.assertEqual(command.stat().st_mtime_ns,before)

    def test_migration_removes_only_exact_legacy_wrapper(self):
        for legacy_text in (m.SHORTCUT_TEXT, 'unrelated program'):
            with tempfile.TemporaryDirectory() as td:
                root=Path(td);command=root/'3xui-relay';legacy=root/'relay'
                legacy.write_text(legacy_text)
                with patch.object(m,'SHORTCUT',command), patch.object(m,'LEGACY_SHORTCUT',legacy):
                    self.assertTrue(m.install_shortcut())
                self.assertTrue(command.exists())
                self.assertEqual(legacy.exists(),legacy_text!=m.SHORTCUT_TEXT)

    def test_conflict_preserves_legacy_command(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);command=root/'3xui-relay';legacy=root/'relay'
            command.write_text('other program');legacy.write_text(m.SHORTCUT_TEXT)
            with patch.object(m,'SHORTCUT',command), patch.object(m,'LEGACY_SHORTCUT',legacy):
                self.assertFalse(m.install_shortcut())
            self.assertEqual(legacy.read_text(),m.SHORTCUT_TEXT)
