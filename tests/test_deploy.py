"""Local tests. No server installation or external credentials required."""
import contextlib
import copy
import importlib.util
import json
import os
import pty
import socket
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

SOURCE = Path(__file__).resolve().parents[1] / 'deploy-3xui-dual.py'
spec = importlib.util.spec_from_file_location('deploy', SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def state():
    return {'ip': '8.8.4.4', 'arch': 'amd64', 'socks_ip': '1.1.1.1', 'socks_port': 1080,
            'socks_user': 'user"\\name', 'socks_password': 'p:a"\\$`密碼',
            'bridge_port': 41080, 'panel_port': 42080, 'api_port': 43080,
            'target': 'www.microsoft.com', 'rotating': False,
            'username': 'test', 'password': 'test-password', 'base': '/random/',
            'nodes': [{'tag': t, 'name': n, 'port': p, 'uuid': '12345678-1234-4321-8123-123456789012',
                       'subid': '0123456789abcdef', 'sid': 'abcd', 'private': 'private', 'public': 'public'}
                      for t, n, p in [(m.DIRECT_TAG, '服务器直连', 443), (m.HOME_TAG, '住宅IP中转', 8443)]]}


class DeploymentTests(unittest.TestCase):
    def test_both_nodes_receive_selected_ports(self):
        nodes = m.make_nodes(54321, 54322)
        self.assertEqual([(n['tag'], n['port']) for n in nodes],
                         [(m.DIRECT_TAG, 54321), (m.HOME_TAG, 54322)])
        self.assertNotEqual(nodes[0]['uuid'], nodes[1]['uuid'])

    def test_random_port_skips_reserved_and_occupied(self):
        with patch.object(m.secrets, 'randbelow', side_effect=[1, 2, 3]), \
             patch.object(m, 'available_ports', side_effect=[RuntimeError('busy'), None]):
            self.assertEqual(m.random_port({20001}), 20003)

    def test_custom_port_reprompts_on_collision(self):
        with patch.object(m, 'random_port', return_value=23456), \
             patch.object(m, 'ask', side_effect=['54321', '54322']), \
             patch.object(m, 'available_ports'):
            self.assertEqual(m.ask_port('port', {54321}), 54322)

    def test_different_residential_exits_allow_failure_test(self):
        s = state()
        s.pop('rotating')
        @contextlib.contextmanager
        def fake_client(_):
            yield ['server', 'home']
        exits = ['8.8.8.8', '9.9.9.9', '1.1.1.1', '8.8.8.8',
                 RuntimeError('upstream unavailable'), '8.8.8.8', '9.9.9.10']
        with patch.object(m, 'wait_panel'), \
             patch.object(m, 'get_template', return_value=m.template(s)), \
             patch.object(m, 'client', fake_client), patch.object(m, 'udp_probe', return_value=False), \
             patch.object(m, 'run'), \
             patch.object(m, 'fetch_ip', side_effect=exits), \
             patch.object(m, 'restart'), patch.object(m, 'update_template'), \
             patch.object(m, 'save') as save:
            m.selftest(s, failure=True)
        self.assertTrue(save.call_args.args[1]['failure_test_passed'])

    def test_detected_server_ip_does_not_prompt(self):
        with patch.object(m, 'fetch_ip', return_value='8.8.4.4') as detect, \
             patch.object(m, 'ask') as ask:
            self.assertEqual(m.server_ip(), '8.8.4.4')
        detect.assert_called_once_with(family4=True)
        ask.assert_not_called()

    def test_failed_server_detection_allows_manual_input(self):
        with patch.object(m, 'fetch_ip', side_effect=RuntimeError('network unavailable')), \
             patch.object(m, 'ask', side_effect=['invalid', '8.8.4.4']) as ask:
            self.assertEqual(m.server_ip(), '8.8.4.4')
        self.assertEqual(ask.call_count, 2)

    def test_socks_host_accepts_domains_and_ipv4(self):
        for value, expected in [('gateway.example.com', 'gateway.example.com'),
                                ('  NAT-US-28.example.com. ', 'nat-us-28.example.com'),
                                ('1.1.1.1', '1.1.1.1'),
                                ('代理.example.com', 'xn--mnq481g.example.com')]:
            with self.subTest(value=value):
                self.assertEqual(m.socks_host(value), expected)

    def test_socks_host_rejects_combined_urls_and_injection(self):
        for value in ['', 'socks5://gateway.example.com:1080', 'gateway.example.com:1080',
                      'user:pass@gateway.example.com', 'gateway.example.com/path',
                      '999.1.2.3', '10.0.0.1', 'host..example.com', '-host.example.com',
                      'host.example.com,exec=bad', '%n.example.com', 'host\n.example.com']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.socks_host(value)

    def test_invalid_proxy_address_reprompts_without_ending_wizard(self):
        with patch.object(m, 'ask', side_effect=['socks5://gateway.example.com', 'gateway.example.com']):
            self.assertEqual(m.ask_socks_host(), 'gateway.example.com')

    def test_proxy_dns_uses_ipv4_and_gives_clear_failure(self):
        with patch.object(m.socket, 'getaddrinfo', return_value=[]) as lookup:
            m.check_socks_dns('gateway.example.com', 1080)
        lookup.assert_called_once_with('gateway.example.com', 1080, socket.AF_INET, socket.SOCK_STREAM)
        with patch.object(m.socket, 'getaddrinfo', side_effect=socket.gaierror('no A record')):
            with self.assertRaisesRegex(RuntimeError, '无法解析为 IPv4'):
                m.check_socks_dns('gateway.example.com', 1080)

    def test_prompt_works_on_nonseekable_terminal(self):
        # A real PTY reproduces SSH terminal behavior; StringIO cannot catch r+ failures.
        real_open = open
        for typed, expected in [('203.0.113.7\n', '203.0.113.7'), ('\n', 'default')]:
            with self.subTest(typed=typed):
                master, slave = pty.openpty()
                try:
                    slave_path = os.ttyname(slave)
                    def open_terminal(path, mode='r', *args, **kwargs):
                        return real_open(slave_path if path == '/dev/tty' else path,
                                         mode, *args, **kwargs)
                    os.write(master, typed.encode())
                    with patch.object(m, 'open', open_terminal, create=True):
                        self.assertEqual(m.ask('Server IPv4', 'default'), expected)
                finally:
                    os.close(slave)
                    os.close(master)

    def test_actual_xray_26728_key_labels(self):
        self.assertEqual(m.parse_x25519('PrivateKey: secret\nPassword (PublicKey): pub\nHash32: hash\n'),
                         ('secret', 'pub'))
        self.assertEqual(m.parse_x25519('Private key: secret\nPublic key: pub\n'), ('secret', 'pub'))

    def test_config_preserves_credentials_and_shows_provider(self):
        s = state()
        cfg = m.template(s)
        m.validate_routes(cfg)
        home = cfg['outbounds'][2]['settings']['servers'][0]
        self.assertEqual(home['address'], s['socks_ip'])
        self.assertEqual(home['users'][0]['pass'], s['socks_password'])
        self.assertEqual(json.loads(json.dumps(cfg)), cfg)

    def test_catchall_before_residential_is_rejected(self):
        cfg = m.template(state())
        cfg['routing']['rules'].insert(1, {'type': 'field', 'network': 'tcp', 'outboundTag': 'server-out'})
        with self.assertRaises(RuntimeError):
            m.validate_routes(cfg)

    def test_wrong_residential_route_and_default_are_rejected(self):
        cfg = m.template(state())
        cfg['routing']['rules'][1]['outboundTag'] = 'server-out'
        with self.assertRaises(RuntimeError):
            m.validate_routes(cfg)
        cfg = m.template(state())
        cfg['outbounds'].reverse()
        with self.assertRaises(RuntimeError):
            m.validate_routes(cfg)

    def test_links_have_matching_ids_and_reality_parameters(self):
        s = state()
        links = [line for line in m.credentials(s).splitlines() if line.startswith('vless://')]
        self.assertEqual(len(links), 2)
        for link, node in zip(links, s['nodes']):
            parsed = urllib.parse.urlsplit(link)
            params = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(parsed.port, node['port'])
            self.assertEqual(params['pbk'], [node['public']])
            self.assertEqual(params['flow'], ['xtls-rprx-vision'])
            self.assertEqual(params['sid'], [node['sid']])
        self.assertNotIn(s['socks_password'], '\n'.join(links))

    def test_curl_credentials_are_stdin_only_and_remote_dns(self):
        responses = [subprocess.CompletedProcess([], 0, '8.8.8.8', '')]
        with patch.object(m, 'run', side_effect=responses) as call:
            self.assertEqual(m.fetch_ip('socks5h://1.1.1.1:1080', 'u:p"\\x'), '8.8.8.8')
        args, kw = call.call_args
        self.assertEqual(args[0], ['curl', '--config', '-'])
        self.assertIn('socks5h://', kw['input'])
        self.assertIn('noproxy = ""', kw['input'])
        self.assertIn('proxy-user = "u:p\\"\\\\x"', kw['input'])
        with self.assertRaises(ValueError):
            m.curl_quote('injection\nurl = http://example.com')

    def test_probe_failure_never_retries_direct(self):
        with patch.object(m, 'run', return_value=subprocess.CompletedProcess([], 7, '', '')) as call:
            with self.assertRaises(RuntimeError):
                m.fetch_ip('socks5h://127.0.0.1:1080')
        self.assertEqual(call.call_count, 2)
        for c in call.call_args_list:
            self.assertIn('proxy = "socks5h://127.0.0.1:1080"', c.kwargs['input'])

    def test_failure_injection_restores_template_even_on_failed_assertion(self):
        s = state()
        original = m.template(s)
        api = object()
        @contextlib.contextmanager
        def fake_client(_):
            yield ['server', 'home']
        # Initial probes pass; injected upstream outage unexpectedly still succeeds.
        exits = ['8.8.8.8', '9.9.9.9', '9.9.9.9', '8.8.8.8', '8.8.8.8']
        with patch.object(m, 'wait_panel', return_value=api), \
             patch.object(m, 'get_template', return_value=original), \
             patch.object(m, 'client', fake_client), \
             patch.object(m, 'udp_probe', return_value=False), \
             patch.object(m, 'run'), \
             patch.object(m, 'fetch_ip', side_effect=exits), \
             patch.object(m, 'restart', return_value=api), \
             patch.object(m, 'update_template') as update:
            with self.assertRaisesRegex(RuntimeError, '住宅上游失效'):
                m.selftest(s, failure=True)
        self.assertEqual(update.call_count, 2)
        self.assertEqual(update.call_args_list[-1].args[1], original)
        self.assertNotEqual(update.call_args_list[0].args[1], original)

    def test_migration_replaces_bridge_and_preserves_extensions(self):
        s = state()
        old = m.template(s)
        old['outbounds'] = [old['outbounds'][1], old['outbounds'][0], old['outbounds'][2]]
        old['outbounds'][2]['settings']['servers'][0].update(address='127.0.0.1', port=s['bridge_port'])
        old['routing']['rules'][2]['network'] = 'tcp'
        old['routing']['rules'].insert(1, {'type':'field', 'inboundTag':[m.HOME_TAG], 'network':'udp', 'outboundTag':'blocked'})
        extra = {'tag':'another-home', 'protocol':'socks', 'settings':{'servers':[{'address':'gateway.example.com','port':1080}]}}
        old['outbounds'].append(extra)
        migrated = m.migration_template(old, s)
        m.validate_routes(migrated)
        self.assertIn(extra, migrated['outbounds'])
        self.assertEqual(migrated['outbounds'][0]['tag'], 'server-out')
        self.assertEqual(migrated['outbounds'][2]['settings']['servers'][0]['address'], s['socks_ip'])
        self.assertEqual(old['outbounds'][0]['tag'], 'blocked')

    def test_reality_accepts_existing_v2rayn_core(self):
        item = m.inbound(state(), state()['nodes'][0])
        self.assertEqual(json.loads(item['streamSettings'])['realitySettings']['minClientVer'], '1.8.0')

    def test_udp_protocol_failure_is_optional(self):
        with patch.object(m.socket, 'create_connection', side_effect=OSError('unsupported')):
            self.assertFalse(m.udp_probe('socks5h://127.0.0.1:10080'))

    def test_rollback_restores_template_and_both_inbounds(self):
        s = state()
        record = {'pending':True, 'state':s, 'template':m.template(s),
                  'inbounds':[dict(m.inbound(s,n),id=i+1) for i,n in enumerate(s['nodes'])]}
        with tempfile.TemporaryDirectory() as td, patch.object(m,'ROOT',Path(td)), \
             patch.object(m,'STATE',Path(td)/'state.json'), patch.object(m,'wait_panel') as wait, \
             patch.object(m,'update_template') as update, patch.object(m,'restart') as restart, \
             patch.object(m,'run'):
            m.save(Path(td)/'migration-backup.json',record)
            m.rollback_migration(s)
            update.assert_called_once_with(wait.return_value,record['template'])
            self.assertEqual(wait.return_value.request.call_count,2)
            restart.assert_called_once_with(s,True)
            self.assertFalse(json.loads((Path(td)/'migration-backup.json').read_text())['pending'])

    def test_udp_fallback_is_rejected_and_template_restored(self):
        s = state()
        @contextlib.contextmanager
        def fake_client(_): yield ['server','home']
        with patch.object(m,'wait_panel'), patch.object(m,'get_template',return_value=m.template(s)), \
             patch.object(m,'client',fake_client), patch.object(m,'run'), patch.object(m,'restart'), \
             patch.object(m,'fetch_ip',side_effect=['8.8.8.8','9.9.9.9','9.9.9.9','8.8.8.8',RuntimeError('offline')]), \
             patch.object(m,'udp_probe',side_effect=[True,True]), patch.object(m,'update_template') as update:
            with self.assertRaisesRegex(RuntimeError,'UDP 仍成功'):
                m.selftest(s,failure=True)
        self.assertEqual(update.call_count,2)
        self.assertEqual(update.call_args.args[1],m.template(s))

    def test_secrets_are_written_private(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'secrets.json'
            m.save(path, {'password': 'sensitive'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
