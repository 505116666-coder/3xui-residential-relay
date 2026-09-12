"""Output escaping and destructive cleanup tested only inside temporary fake servers."""
import base64
import io
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_deploy import m, state


class OutputTests(unittest.TestCase):
    def test_completion_is_offline_and_preserves_author_links(self):
        out = io.StringIO()
        with patch.object(m.sys, 'stdout', out), patch.object(m.subprocess, 'run') as run, patch.object(m, 'save') as save:
            m.completion(state())
        run.assert_not_called()
        save.assert_not_called()
        self.assertIn('https://www.youtube.com/@Didushan', out.getvalue())
        self.assertIn('https://t.me/didushan9', out.getvalue())
        self.assertNotIn('统计', out.getvalue())

    def test_banner_fills_terminal_without_wrapping(self):
        import unicodedata
        for columns in (10, 20, 24, 40, 48, 80, 120, 200):
            out = io.StringIO()
            with patch.object(m.sys, 'stdout', out), patch.object(m.shutil, 'get_terminal_size', return_value=os.terminal_size((columns, 24))):
                m.banner()
            lines = out.getvalue().splitlines()
            widths = [sum(2 if unicodedata.east_asian_width(c) in ('F', 'W') else 1 for c in line) for line in lines]
            self.assertTrue(all(w < columns for w in widths))
            block_lines = [line for line in lines if '█' in line]
            if columns >= 24:
                self.assertEqual(len(block_lines), 7 if columns >= 48 else 14)
                self.assertEqual(max(map(len, block_lines)), columns - 1)
            else:
                self.assertIn('Didushan', out.getvalue())

    def test_banner_uses_blue_cyan_only_in_supported_terminal(self):
        for terminal, expected in [('xterm-256color', True), ('dumb', False)]:
            out = io.StringIO()
            with patch.object(m.sys, 'stdout', out), patch.object(out, 'isatty', return_value=True), patch.dict(os.environ, TERM=terminal):
                m.banner()
            self.assertEqual('\033[38;5;33m' in out.getvalue(), expected)
            self.assertEqual('\033[38;5;51m' in out.getvalue(), expected)

    def test_result_keeps_text_backup_and_removes_old_html(self):
        s=state()
        with tempfile.TemporaryDirectory() as td, patch.object(m,'ROOT',Path(td)), patch.object(m, 'say') as say:
            (Path(td)/'结果.html').write_text('obsolete')
            m.write_results(s)
            p=Path(td)/'登录信息与两个节点.txt'
            self.assertEqual(p.read_text(),m.credentials(s))
            self.assertFalse((Path(td)/'结果.html').exists())
            self.assertEqual(p.stat().st_mode & 0o777,0o600)
            say.assert_not_called()

    def test_terminal_copy_encodes_exact_value(self):
        s=state(); out=io.StringIO()
        with patch.object(m.sys,'stdout',out),patch.object(out,'isatty',return_value=True):
            m.copy_result(s,3)
        encoded=out.getvalue().split('\033]52;c;',1)[1].split('\a',1)[0]
        self.assertEqual(base64.b64decode(encoded).decode(),m.copy_values(s)[2][1])


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        for directory in ['root','etc/systemd/system','usr/local','run/lock','tmp','bin']:
            (self.base/directory).mkdir(parents=True,exist_ok=True)
        self.root=self.base/'root/3xui-dual';self.unit=self.base/'etc/systemd/system/x-ui.service'
        script=(Path(__file__).resolve().parents[1]/'uninstall-3xui-relay.sh').read_text()
        # Never run against host paths, and never invoke host service/firewall tools.
        script=script.replace('[[ "$(uname -s)" == Linux && "$EUID" -eq 0 ]]','[[ 1 == 1 ]]')
        script=re.sub(r'/root|/etc|/usr/local|/run/lock|/tmp',lambda match:str(self.base/match.group(0).lstrip('/')),script)
        self.script=self.base/'uninstall.sh';self.script.write_text(script)
        for name,body in {'systemctl':'exit 0','flock':'exit 0','ufw':'exit 0'}.items():
            p=self.base/'bin'/name;p.write_text('#!/bin/bash\n'+body+'\n');p.chmod(0o755)
        self.env=dict(os.environ,PATH=str(self.base/'bin')+':/usr/bin:/bin')
    def tearDown(self): self.temp.cleanup()
    def run_script(self):
        return subprocess.run(['bash',str(self.script)],env=self.env,capture_output=True,text=True,errors='replace')
    def managed(self,kind):
        self.root.mkdir()
        if kind=='marker':(self.root/'owner').write_text('3xui-dual-v1\n')
        if kind=='state':(self.root/'state.json').write_text('{"managed_by": "3xui-dual-v1"}')
        if kind=='unit':self.unit.write_text('Description=3x-ui panel (dual-node deployment)\n')
        (self.root/'private').write_text('dummy')
        for path in ['usr/local/x-ui','etc/x-ui']:
            p=self.base/path;p.mkdir();(p/'test').write_text('dummy')
    def test_early_failure_and_repeat(self):
        for _ in range(2):self.assertEqual(self.run_script().returncode,0)
    def test_partial_complete_and_migration_cleanup(self):
        for kind in ['marker','state','unit']:
            with self.subTest(kind=kind):
                self.managed(kind)
                (self.root/'migration-backup.json').write_text('{}')
                result=self.run_script();self.assertEqual(result.returncode,0,result.stderr+result.stdout)
                self.assertFalse(self.root.exists());self.assertFalse(self.unit.exists())
                self.assertFalse((self.base/'etc/x-ui').exists())
                self.assertEqual(self.run_script().returncode,0)
    def test_unrelated_panel_is_preserved(self):
        p=self.base/'etc/x-ui';p.mkdir();(p/'important').write_text('keep')
        self.assertNotEqual(self.run_script().returncode,0);self.assertTrue((p/'important').exists())
    def test_replaced_service_is_preserved(self):
        self.managed('marker');self.unit.write_text('Description=other panel\n')
        self.assertNotEqual(self.run_script().returncode,0);self.assertTrue(self.root.exists())
    def test_service_stop_failure_preserves_files(self):
        self.managed('marker');(self.base/'bin/systemctl').write_text('#!/bin/bash\n[[ "$1" != stop ]]\n')
        self.assertNotEqual(self.run_script().returncode,0);self.assertTrue(self.root.exists())

    def test_unknown_deployment_files_are_preserved(self):
        self.root.mkdir();(self.root/'unknown').write_text('keep')
        self.assertNotEqual(self.run_script().returncode,0)
        self.assertTrue((self.root/'unknown').exists())

    def test_only_recorded_tagged_firewall_rules_are_removed(self):
        self.managed('marker');(self.root/'ufw-added.txt').write_text('23456\n34567\n')
        log=self.base/'ufw-log'
        tool=self.base/'bin/ufw'
        tool.write_text('#!/bin/bash\nif [[ "$1" == status ]]; then\n echo "23456/tcp ALLOW Anywhere # Didushan-3xui-relay"\n echo "34567/tcp ALLOW Anywhere"\nelse\n echo "$*" >> "'+str(log)+'"\nfi\n')
        result=self.run_script();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(log.read_text().strip(),'--force delete allow 23456/tcp')
