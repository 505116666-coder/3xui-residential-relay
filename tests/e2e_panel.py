"""Explicit, destructive-to-new-VM integration test using the real panel and network.
Run only on a fresh disposable Ubuntu/Debian VM; see docs/开发与测试.md.
No panel API, systemctl, Xray, TLS issuance, or network probes are mocked.
The failure case drops one successful add response to exercise actual rollback.
"""
import argparse
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
from unittest.mock import patch
import uuid

spec = importlib.util.spec_from_file_location('deploy', Path(__file__).resolve().parents[1] / 'deploy-3xui-dual.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-vm', action='store_true', required=True)
    parser.add_argument('--config', type=Path, required=True, help='0600 JSON: email, proxy {host,port,username,password}; optional target')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('Requires root on a fresh disposable Linux VM.')
    os.umask(0o077)
    arch = m.check_os()
    if any(p.exists() for p in (m.ROOT, m.APP, Path('/etc/x-ui'), Path('/etc/systemd/system/x-ui.service'))):
        parser.error('Existing deployment detected. Use a fresh VM; nothing was changed.')
    if args.config.stat().st_mode & 0o077:
        parser.error('Config must be private: chmod 600 <config>.')
    config = json.loads(args.config.read_text())
    proxy = config['proxy']
    proxy['host'] = m.socks_host(proxy['host'])
    if not isinstance(proxy['port'], int) or not 1 <= proxy['port'] <= 65535:
        parser.error('Invalid proxy port.')
    for value in (proxy['username'], proxy['password']):
        m.curl_quote(value)
        if not 1 <= len(value.encode()) <= 255:
            parser.error('Invalid credential length.')
    if ':' in proxy['username']:
        parser.error('Proxy username containing colon is unsupported.')
    with open('/run/lock/3xui-dual.lock','w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for key in list(os.environ):
            if key.lower() in ('http_proxy','https_proxy','all_proxy','no_proxy') or key.startswith('XUI_'):
                os.environ.pop(key)
        os.environ['LC_ALL'] = 'C'
        m.ROOT.mkdir(mode=0o700)
        m.save(m.ROOT/'owner','3xui-dual-v1')
        m.LOG.touch(mode=0o600)
        m.run(['apt-get','update'], timeout=600)
        m.run(['apt-get','install','-y','python3','curl','openssl','socat','tar','ca-certificates','iproute2'], timeout=900)
        ports = set([80])
        def new_port():
            port = m.random_port(ports)
            ports.add(port)
            return port
        s = {'managed_by':'3xui-dual-v1','schema_version':2,'arch':arch,'ip':m.server_ip(),
             'email':config['email'],'target':config.get('target','www.microsoft.com'),
             'username':'e2e-'+secrets.token_hex(4),'password':secrets.token_urlsafe(24),
             'base':'/'+secrets.token_hex(12)+'/', 'panel_port':new_port(),'api_port':new_port(),
             'socks_ip':proxy['host'],'socks_port':proxy['port'],'socks_user':proxy['username'],
             'socks_password':proxy['password'],'nodes':m.make_nodes(new_port(),new_port())}
        m.deploy(s)
        m.check_all(s)
        api = m.wait_panel(s,True)
        baseline = m.get_template(api)
        baseline_entries = m.inbound_snapshot(api.request('panel/api/inbounds/list'))
        node = {'tag':'dual-residential-'+secrets.token_hex(6),'name':'E2E temporary',
                'port':new_port(),'uuid':str(uuid.uuid4()),'subid':secrets.token_hex(8),'sid':secrets.token_hex(8)}
        node['private'],node['public'] = m.parse_x25519(m.run([m.xray_bin(s),'x25519']).stdout)
        original_request = m.API.request
        lost = False
        def dropped_response(self,path,data=None):
            nonlocal lost
            result = original_request(self,path,data)
            if path == 'panel/api/inbounds/add' and not lost:
                lost = True
                raise OSError('E2E: response lost after actual panel write')
            return result
        with patch.object(m.API,'request',dropped_response):
            try:
                m.apply_residential_add(s,node,proxy,copy.deepcopy(baseline),api)
            except OSError:
                pass
            else:
                raise AssertionError('Fault injection did not run')
        assert lost and not (m.ROOT/'pending-add.json').exists()
        api = m.wait_panel(s,True)
        assert m.get_template(api) == baseline
        assert m.inbound_snapshot(api.request('panel/api/inbounds/list')) == baseline_entries
        m.apply_residential_add(s,node,proxy,copy.deepcopy(baseline),api)
        m.check_all(s)
        item = s['additional_residential'][0]
        m.apply_change(s,item,name='E2E renamed')
        # Same upstream credentials are permitted; the rename above already tested update.
        # An optional second real upstream additionally exercises replacement.
        if config.get('replacement_proxy'):
            replacement = config['replacement_proxy']
            m.apply_change(s,s['additional_residential'][0],proxy=replacement)
        m.apply_change(s,s['additional_residential'][0],delete=True)
        m.check_all(s)
        assert m.get_template(m.wait_panel(s,True)) == baseline
        assert m.inbound_snapshot(m.wait_panel(s,True).request('panel/api/inbounds/list')) == baseline_entries
        m.save(m.ROOT/'e2e-result.json', {'ok':True,'script_version':m.SCRIPT_VERSION,
                                      'cases':['install','all-check','lost-add-response-rollback','add','rename','delete'],
                                      'replacement_tested':bool(config.get('replacement_proxy'))})
        print('PASS: actual panel lifecycle. Result: /root/3xui-dual/e2e-result.json')
        print('Dispose of this test VM after inspection; credentials and services remain on the test VM.')


if __name__ == '__main__':
    main()
