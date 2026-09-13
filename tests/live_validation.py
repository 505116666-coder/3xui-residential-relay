"""Validate an existing deployment with a temporary loopback-only residential node.
Requires explicit operator invocation on the user's server; never reinstalls.
"""
import contextlib
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import uuid

source = Path(sys.argv[1] if len(sys.argv) > 1 else '/root/relay-1.1.0.py')
spec = importlib.util.spec_from_file_location('relay',source)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
os.umask(0o077)
m.check_os()
lock = open('/run/lock/3xui-dual.lock','w')
fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
for key in list(os.environ):
    if key.lower() in ('http_proxy','https_proxy','all_proxy','no_proxy') or key.startswith('XUI_'):
        os.environ.pop(key)
os.environ['LC_ALL']='C'
m.ensure_no_pending()
s=json.loads(m.STATE.read_text())
assert s.get('complete') and s.get('managed_by')=='3xui-dual-v1'
baseline=copy.deepcopy(s)
api=m.wait_panel(s,True)
config=m.get_template(api)
entries=m.inbound_snapshot(api.request('panel/api/inbounds/list'))
m.save(m.ROOT/'live-validation-baseline.json',{'state':baseline,'config':config,'entries':entries})
report={'script_version':m.SCRIPT_VERSION,'cases':[],'ok':False,'original_preserved':False}
def passed(name):
    report['cases'].append(name)
    m.save(m.ROOT/'live-validation-result.json',report)
    print('PASS:',name,flush=True)
proxy=copy.deepcopy(list(m.managed_residential(s))[-1]['proxy'])
node={'tag':'dual-residential-live-'+secrets.token_hex(6),'name':'TEMP validation',
      'port':m.random_port({80,s['panel_port'],s['api_port']} | {e['port'] for e in entries}),
      'uuid':str(uuid.uuid4()),'subid':secrets.token_hex(8),'sid':secrets.token_hex(8)}
node['private'],node['public']=m.parse_x25519(m.run([m.xray_bin(s),'x25519']).stdout)
real_inbound=m.inbound
def loopback_inbound(state,item):
    value=real_inbound(state,item)
    if item['tag']==node['tag']:value['listen']='127.0.0.1'
    return value
m.inbound=loopback_inbound
m.show_added_result=lambda *args: print('Temporary loopback node added',flush=True)
real_request=m.API.request
lost=False
try:
    m.check_all(s)
    passed('existing_nodes')
    def lose_response(self,path,data=None):
        global lost
        value=real_request(self,path,data)
        if path=='panel/api/inbounds/add' and not lost:
            lost=True
            raise OSError('Validation: lost successful add response')
        return value
    m.API.request=lose_response
    try:
        m.apply_residential_add(s,node,proxy,copy.deepcopy(config),api)
    except OSError:
        assert lost
    else:
        raise AssertionError('Fault injection did not trigger')
    finally:
        m.API.request=real_request
    assert not (m.ROOT/'pending-add.json').exists()
    assert m.get_template(m.wait_panel(s,True))==config
    assert m.inbound_snapshot(m.wait_panel(s,True).request('panel/api/inbounds/list'))==entries
    passed('actual_add_response_loss_rollback')
    m.apply_residential_add(s,node,proxy,copy.deepcopy(config),m.wait_panel(s,True))
    passed('actual_add')
    item=next(e for e in s['additional_residential'] if e['node']['tag']==node['tag'])
    m.apply_change(s,item,name='TEMP renamed')
    passed('actual_rename')
    item=next(e for e in s['additional_residential'] if e['node']['tag']==node['tag'])
    other=next(m.managed_residential(baseline))['proxy']
    if other!=proxy:
        m.apply_change(s,item,proxy=other)
        passed('actual_replace_upstream')
    m.apply_change(s,next(e for e in s['additional_residential'] if e['node']['tag']==node['tag']),delete=True)
    passed('actual_delete')
except BaseException as exc:
    report['error_type']=type(exc).__name__
    print('VALIDATION FAILED:',str(exc),flush=True)
finally:
    m.API.request=real_request
    try:
        current=json.loads(m.STATE.read_text())
        if (m.ROOT/'pending-change.json').exists():m.rollback_change(current)
        if (m.ROOT/'pending-add.json').exists():m.rollback_add(current)
        current=json.loads(m.STATE.read_text())
        matches=[e for e in current.get('additional_residential',[]) if e['node']['tag']==node['tag']]
        if matches:m.apply_change(current,matches[0],delete=True)
        # Only remove the exact UFW rule added by this temporary test, if any.
        manifest=m.ROOT/'ufw-added.txt'
        if manifest.exists() and str(node['port']) in manifest.read_text().splitlines():
            status=m.run(['ufw','status'],check=False).stdout
            import re
            if re.search(r'^'+str(node['port'])+r'/tcp\s+.*# Didushan-3xui-relay\s*$',status,re.M):
                m.run(['ufw','--force','delete','allow',str(node['port'])+'/tcp'])
            m.save(manifest,''.join(x+'\n' for x in manifest.read_text().splitlines() if x!=str(node['port'])))
        assert json.loads(m.STATE.read_text())==baseline
        api=m.wait_panel(baseline,True)
        assert m.get_template(api)==config
        assert m.inbound_snapshot(api.request('panel/api/inbounds/list'))==entries
        report['original_preserved']=True
        m.check_all(baseline)
        passed('original_state_routes_inbounds_and_connectivity_preserved')
        report['ok']='error_type' not in report
    except BaseException as exc:
        report['cleanup_error_type']=type(exc).__name__
        print('CLEANUP NEEDS ATTENTION:',str(exc),flush=True)
    m.save(m.ROOT/'live-validation-result.json',report)
    print(json.dumps(report,ensure_ascii=False),flush=True)
