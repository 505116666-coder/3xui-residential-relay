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

    def test_config_preserves_credentials_and_uses_loopback(self):
        s = state()
        cfg = m.template(s)
        m.validate_routes(cfg)
        home = cfg['outbounds'][2]['settings']['servers'][0]
        self.assertEqual(home['address'], '127.0.0.1')
        self.assertEqual(home['users'][0]['pass'], s['socks_password'])
        self.assertEqual(json.loads(json.dumps(cfg)), cfg)

    def test_catchall_before_residential_is_rejected(self):
        cfg = m.template(state())
        cfg['routing']['rules'].insert(1, {'type': 'field', 'network': 'tcp', 'outboundTag': 'server-out'})
        with self.assertRaises(RuntimeError):
            m.validate_routes(cfg)

    def test_udp_allow_and_default_freedom_are_rejected(self):
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
             patch.object(m, 'run'), \
             patch.object(m, 'fetch_ip', side_effect=exits), \
             patch.object(m, 'restart', return_value=api), \
             patch.object(m, 'update_template') as update:
            with self.assertRaisesRegex(RuntimeError, '住宅上游失效'):
                m.selftest(s, failure=True)
        self.assertEqual(update.call_count, 2)
        self.assertEqual(update.call_args_list[-1].args[1], original)
        self.assertNotEqual(update.call_args_list[0].args[1], original)

    def test_secrets_are_written_private(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'secrets.json'
            m.save(path, {'password': 'sensitive'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
