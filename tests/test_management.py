"""Management regression: failures, recovery and multi-node health coverage."""
import contextlib
import copy
import json
import unittest
from unittest.mock import patch
import test_add_residential
from test_deploy import m


class ManagementTests(unittest.TestCase):
    setUp = test_add_residential.AddResidentialTests.setUp

    def add(self):
        m.apply_residential_add(self.s, self.node, self.proxy, copy.deepcopy(self.panel.config), self.panel)
        self.item = self.s['additional_residential'][0]
        self.fetch.side_effect = None
        self.fetch.return_value = '9.9.9.9'
        @contextlib.contextmanager
        def probe(s):
            yield [s['nodes'][0]['tag']]
        self.stack.enter_context(patch.object(m, 'client', probe))

    def test_rename_preserves_connection_and_panel_settings(self):
        self.add()
        self.fetch.side_effect = ['9.9.9.9', '8.8.8.8']
        old = copy.deepcopy(self.item['node'])
        m.apply_change(self.s, self.item, name='改名住宅')
        new = self.s['additional_residential'][0]['node']
        self.assertEqual({k:v for k,v in old.items() if k != 'name'}, {k:v for k,v in new.items() if k != 'name'})
        self.assertEqual(new['name'], '改名住宅')
        self.assertEqual(self.panel.entries[-1]['remark'], '改名住宅')
        self.assertFalse((self.root/'pending-change.json').exists())

    def test_replace_original_proxy_preserves_link(self):
        self.add()
        old_link = m.node_link(self.s, self.s['nodes'][1])
        self.fetch.side_effect = ['9.9.9.9', '8.8.8.8', '9.9.9.9', '8.8.8.8']
        with patch.object(m, 'check_socks_dns'):
            m.apply_change(self.s, next(m.managed_residential(self.s)), proxy=self.proxy)
        self.assertEqual(old_link, m.node_link(self.s, self.s['nodes'][1]))
        self.assertEqual(self.s['socks_password'], 'new-secret')
        m.check_node_route(self.panel.config, next(m.managed_residential(self.s)))

    def test_delete_only_selected_node(self):
        self.add()
        m.apply_change(self.s, self.item, delete=True)
        self.assertEqual(len(self.panel.entries), 2)
        self.assertEqual(self.s['additional_residential'], [])
        self.assertEqual(self.panel.config, m.template(self.s))

    def test_failed_probe_restores_state_routes_and_inbound(self):
        self.add()
        old = copy.deepcopy(self.s)
        config = copy.deepcopy(self.panel.config)
        self.fetch.side_effect = RuntimeError('probe failed')
        with self.assertRaises(RuntimeError):
            m.apply_change(self.s, self.item, name='失败修改')
        self.assertEqual(self.s, old)
        self.assertEqual(json.loads(m.STATE.read_text()), old)
        self.assertEqual(self.panel.config, config)
        self.assertEqual(self.panel.entries[-1]['remark'], self.node['name'])
        self.assertFalse((self.root/'pending-change.json').exists())

    def test_lost_delete_response_restores_deleted_inbound(self):
        self.add()
        request = self.panel.request
        def lost(path, data=None):
            result = request(path, data)
            if '/del/' in path: raise OSError('lost response')
            return result
        with patch.object(self.panel, 'request', side_effect=lost), self.assertRaises(OSError):
            m.apply_change(self.s, self.item, delete=True)
        self.assertEqual(len(self.panel.entries), 3)
        self.assertEqual(self.panel.entries[-1]['tag'], self.node['tag'])
        self.assertEqual(len(self.s['additional_residential']), 1)
        self.assertFalse((self.root/'pending-change.json').exists())

    def test_committed_operation_survives_interrupted_journal_cleanup(self):
        self.add()
        self.fetch.side_effect = ['9.9.9.9','8.8.8.8']
        unlink = m.Path.unlink
        def fail_once(path, *args, **kwargs):
            if path.name == 'pending-change.json': raise OSError('interruption')
            return unlink(path,*args,**kwargs)
        with patch.object(m.Path, 'unlink', fail_once), self.assertRaises(OSError):
            m.apply_change(self.s,self.item,name='提交成功')
        m.rollback_change(self.s)
        self.assertEqual(json.loads(m.STATE.read_text())['additional_residential'][0]['node']['name'],'提交成功')
        self.assertEqual(self.panel.entries[-1]['remark'],'提交成功')

    def test_all_check_continues_after_failed_home(self):
        self.add()
        def fetch(proxy=None, **kwargs):
            if proxy == m.HOME_TAG: raise RuntimeError('failure')
            return '9.9.9.9' if proxy == self.node['tag'] else '8.8.8.8'
        self.fetch.side_effect = fetch
        with self.assertRaises(RuntimeError): m.check_all(self.s)
        result = json.loads((self.root/'check-all.json').read_text())
        self.assertEqual([e['ok'] for e in result['nodes']], [True,False,True])

    def test_tampered_additional_route_fails_check(self):
        self.add()
        self.panel.config['routing']['rules'][4]['outboundTag'] = 'server-out'
        with self.assertRaises(RuntimeError): m.check_node_route(self.panel.config,self.item)

    def test_diagnostics_never_contains_credentials_or_names(self):
        self.add()
        result = json.dumps(m.diagnostics(self.s))
        for secret in ['new-secret','new-user','gateway.example.com',self.node['name'],self.s['ip']]:
            self.assertNotIn(secret,result)

    def test_pending_change_blocks_new_addition(self):
        m.save(self.root/'pending-change.json',{})
        with self.assertRaisesRegex(RuntimeError,'rollback-change'): m.add_residential(self.s)

    def test_original_node_deletion_refused(self):
        with self.assertRaises(RuntimeError): m.apply_change(self.s,next(m.managed_residential(self.s)),delete=True)

    def test_menu_returns_after_invalid_option(self):
        with patch.object(m,'ask',side_effect=['99','0']): m.menu(self.s)
        self.assertIn('请输入菜单中的序号',self.output.getvalue())

    def test_delete_refuses_custom_outbound_reference(self):
        self.add()
        self.panel.config['outbounds'].append({'tag':'custom','protocol':'freedom',
                                              'proxySettings':{'tag':self.node['tag']+'-out'}})
        with self.assertRaisesRegex(RuntimeError,'引用'):
            m.apply_change(self.s,self.item,delete=True)
        self.assertEqual(len(self.panel.entries),3)
        self.assertFalse((self.root/'pending-change.json').exists())

    def test_rollback_preserves_later_panel_changes(self):
        self.add()
        real_rollback = m.rollback_change
        self.fetch.side_effect = RuntimeError('failed')
        with patch.object(m,'rollback_change'), self.assertRaises(RuntimeError):
            m.apply_change(self.s,self.item,name='interrupted')
        self.panel.config['outbounds'].append({'tag':'user-later','protocol':'blackhole'})
        with self.assertRaisesRegex(RuntimeError,'后续修改'):
            real_rollback(self.s)
        self.assertTrue((self.root/'pending-change.json').exists())
        self.assertEqual(self.panel.config['outbounds'][-1]['tag'],'user-later')

    def test_curl_socks_error_is_actionable_and_does_not_echo_stderr(self):
        import subprocess
        import importlib.util
        spec=importlib.util.spec_from_file_location('probe_module',m.__file__)
        probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
        with patch.object(probe,'run',return_value=subprocess.CompletedProcess([],97,'','secret-password')):
            with self.assertRaisesRegex(RuntimeError,'SOCKS 握手失败') as error:
                probe.fetch_ip('socks5h://example.com:1080','user:secret-password')
            self.assertNotIn('secret-password',str(error.exception))
