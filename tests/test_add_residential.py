"""Exercise additions and interrupted transactions with a stateful fake panel."""
import contextlib
import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_deploy import m, state


class Panel:
    def __init__(self, s):
        self.config = m.template(s)
        self.entries = [dict(m.inbound(s,n),id=i+1) for i,n in enumerate(s['nodes'])]
        self.calls = []
        self.fail_add_reply = False
        self.object_fields = False
    def request(self, path, data=None):
        self.calls.append((path,copy.deepcopy(data)))
        if path == 'panel/api/inbounds/list':
            entries=copy.deepcopy(self.entries)
            if self.object_fields:
                for entry in entries:
                    for key in ('settings','streamSettings','sniffing'):
                        if isinstance(entry.get(key),str):entry[key]=json.loads(entry[key])
            return entries
        if path == 'panel/api/xray/':return {'xraySetting':copy.deepcopy(self.config)}
        if path == 'panel/api/xray/update':self.config=json.loads(data['xraySetting']);return
        if path == 'panel/api/inbounds/add':
            self.entries.append(dict(copy.deepcopy(data),id=max(e['id'] for e in self.entries)+1))
            if self.fail_add_reply:raise OSError('response lost after server applied add')
            return self.entries[-1]
        if '/update/' in path:
            self.entries=[copy.deepcopy(data) if e['id']==data['id'] else e for e in self.entries];return
        if '/del/' in path:
            entry=next(e for e in self.entries if e['id']==int(path.rsplit('/',1)[1]))
            self.entries.remove(entry)
            self.config['routing']['rules']=[r for r in self.config['routing']['rules'] if entry['tag'] not in r.get('inboundTag',[])]
            return
        raise AssertionError(path)


class AddResidentialTests(unittest.TestCase):
    def setUp(self):
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.s=state();self.s.update(complete=True,managed_by='3xui-dual-v1')
        self.original=copy.deepcopy(self.s);self.panel=Panel(self.s)
        self.node=dict(self.s['nodes'][1],tag='dual-residential-test',name='新增住宅',port=54321,uuid='new-uuid')
        self.proxy={'host':'gateway.example.com','port':1080,'username':'new-user','password':'new-secret'}
        self.output=io.StringIO()
        for target,name,value in [(m,'ROOT',self.root),(m,'STATE',self.root/'state.json'),(m.sys,'stdout',self.output)]:
            self.stack.enter_context(patch.object(target,name,value))
        m.save(m.STATE,self.s)
        self.stack.enter_context(patch.object(m,'wait_panel',return_value=self.panel))
        self.restart=self.stack.enter_context(patch.object(m,'restart',return_value=self.panel))
        self.run=self.stack.enter_context(patch.object(m,'run',return_value=subprocess.CompletedProcess([],0,'','')))
        self.stack.enter_context(patch.object(m.shutil,'which',return_value=None))
        self.fetch=self.stack.enter_context(patch.object(m,'fetch_ip',side_effect=['9.9.9.9','8.8.8.8']))
        self.udp=self.stack.enter_context(patch.object(m,'udp_probe',return_value=False))
        @contextlib.contextmanager
        def client(s):
            self.assertEqual(s['nodes'],[self.node]);yield ['new-node-proxy']
        self.stack.enter_context(patch.object(m,'client',client))
    def apply(self):
        m.apply_residential_add(self.s,self.node,self.proxy,copy.deepcopy(self.panel.config),self.panel)
    def assert_original(self):
        self.assertEqual(self.s,self.original)
        self.assertEqual(json.loads(m.STATE.read_text()),self.original)
        self.assertEqual(self.panel.config,m.template(self.original))
        self.assertEqual(len(self.panel.entries),2)
        self.assertFalse((self.root/'pending-add.json').exists())
    def test_success_preserves_old_nodes_and_persists_new_link(self):
        old_entries=copy.deepcopy(self.panel.entries)
        self.apply()
        self.assertEqual(self.panel.entries[:2],old_entries)
        self.assertEqual(self.s['nodes'],self.original['nodes'])
        for key in ['username','password','base','socks_ip','socks_password']:
            self.assertEqual(self.s[key],self.original[key])
        self.assertEqual(len(self.s['additional_residential']),1)
        self.assertFalse((self.root/'pending-add.json').exists())
        self.assertEqual(json.loads(m.STATE.read_text()),self.s)
        self.assertEqual(m.STATE.stat().st_mode & 0o777,0o600)
        self.assertEqual(m.credentials(self.s).count('vless://'),3)
        self.assertNotIn('new-secret',self.output.getvalue())
        self.assertNotIn('已备份',self.output.getvalue())
        self.assertIn(m.node_link(self.s,self.node),(self.root/'登录信息与两个节点.txt').read_text())
        calls=self.panel.calls
        add=next(data for path,data in calls if path.endswith('/add'))
        self.assertFalse(add['enable'])
        route_index=next(i for i,(path,_) in enumerate(calls) if path=='panel/api/xray/update')
        enable_index=next(i for i,(path,_) in enumerate(calls) if '/inbounds/update/' in path)
        self.assertLess(route_index,enable_index)
    def test_route_preserves_custom_rules_and_existing_extensions(self):
        cfg=m.template(self.s)
        cfg['routing']['rules'].append({'type':'field','network':'tcp,udp','outboundTag':'server-out'})
        snapshot=copy.deepcopy(cfg)
        first=m.residential_extension(cfg,self.node,self.proxy)
        second_node=dict(self.node,tag='second-home',port=54322)
        second=m.residential_extension(first,second_node,self.proxy)
        self.assertEqual(cfg,snapshot)
        self.assertEqual(second['outbounds'][:-2],cfg['outbounds'])
        self.assertEqual(second['routing']['rules'][:4],cfg['routing']['rules'][:4])
        self.assertEqual(second['routing']['rules'][-1],cfg['routing']['rules'][-1])
        self.assertEqual(second['routing']['rules'][4]['inboundTag'],['second-home'])
        self.assertEqual(second['routing']['rules'][5]['inboundTag'],[self.node['tag']])
        self.assertTrue(all(r['network']=='tcp,udp' for r in second['routing']['rules'][4:6]))
    def test_bad_auth_before_mutation(self):
        with patch.object(m,'ask',side_effect=['name','1080','user','pass']),patch.object(m,'ask_socks_host',return_value='gateway.example.com'),patch.object(m,'check_socks_dns'):
            self.fetch.side_effect=RuntimeError('authentication failed')
            with self.assertRaisesRegex(RuntimeError,'authentication failed'):m.add_residential(self.s)
        self.assert_original()
        self.assertFalse(any('/add' in path for path,_ in self.panel.calls))
    def test_lost_add_response_rolls_back_only_new_inbound(self):
        self.panel.fail_add_reply=True
        with self.assertRaises(OSError):self.apply()
        self.assert_original()
    def test_node_failure_rolls_back(self):
        self.fetch.side_effect=RuntimeError('node unreachable')
        with self.assertRaisesRegex(RuntimeError,'node unreachable'):self.apply()
        self.assert_original()
    def test_direct_exit_rejected_and_rolled_back(self):
        self.fetch.side_effect=['8.8.8.8','8.8.8.8']
        with self.assertRaisesRegex(RuntimeError,'与服务器相同'):self.apply()
        self.assert_original()
    def test_interruption_rolls_back(self):
        self.fetch.side_effect=KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.apply()
        self.assert_original()
    def test_state_write_failure_rolls_back(self):
        save=m.save
        def fail_state(path,value):
            if Path(path)==m.STATE:raise OSError('disk full')
            save(path,value)
        with patch.object(m,'save',side_effect=fail_state),self.assertRaises(OSError):self.apply()
        self.assert_original()
    def test_killed_transaction_can_be_recovered_later(self):
        desired=m.residential_extension(self.panel.config,self.node,self.proxy)
        m.save(self.root/'pending-add.json',{'node':self.node,'original':self.panel.config,'desired':desired})
        self.panel.config=desired
        self.panel.entries.append(dict(m.inbound(self.s,self.node),id=3))
        m.rollback_add(self.s)
        self.assert_original()
    def test_recovery_refuses_to_overwrite_later_changes(self):
        desired=m.residential_extension(self.panel.config,self.node,self.proxy)
        m.save(self.root/'pending-add.json',{'node':self.node,'original':self.panel.config,'desired':desired})
        self.panel.config=desired
        self.panel.config['log']['loglevel']='debug'
        with self.assertRaisesRegex(RuntimeError,'配置又被修改'):m.rollback_add(self.s)
        self.assertTrue((self.root/'pending-add.json').exists())
        self.assertEqual(self.panel.config['log']['loglevel'],'debug')
    def test_committed_transaction_not_undone_after_disconnect(self):
        m.save(self.root/'pending-add.json',{'node':self.node})
        current=copy.deepcopy(self.s);current['additional_residential']=[{'node':self.node,'proxy':self.proxy}]
        m.save(m.STATE,current)
        m.rollback_add(self.s)
        self.assertEqual(json.loads(m.STATE.read_text()),current)
        self.assertFalse((self.root/'pending-add.json').exists())
    def test_cli_add_dispatch_never_calls_install(self):
        with patch.object(m.sys,'argv',['manager.py','--add-residential']),patch.object(m,'check_os',return_value='amd64'),patch.object(m,'open',create=True),patch.object(m.fcntl,'flock'),patch.object(m,'add_residential') as add,patch.object(m,'deploy') as deploy:
            m.main()
        add.assert_called_once_with(self.s);deploy.assert_not_called()

    def test_snapshot_ignores_live_traffic_but_detects_config_changes(self):
        original=copy.deepcopy(self.panel.entries)
        changed=copy.deepcopy(original);changed[0].update(up=999,down=222)
        self.assertEqual(m.inbound_snapshot(original),m.inbound_snapshot(changed))
        changed[0]['port']=54322
        self.assertNotEqual(m.inbound_snapshot(original),m.inbound_snapshot(changed))
    def test_pending_transaction_prevents_duplicate_add(self):
        m.save(self.root/'pending-add.json',{'node':self.node})
        with self.assertRaisesRegex(RuntimeError,'先运行 --rollback-add'):m.add_residential(self.s)
        self.assertEqual(len(self.panel.entries),2)
    def test_added_node_can_be_copied_by_menu_index(self):
        self.apply()
        self.output.seek(0);self.output.truncate()
        m.copy_result(self.s,7)
        self.assertEqual(self.output.getvalue().strip(),m.node_link(self.s,self.node))
    def test_ufw_failure_removes_only_recorded_new_rule(self):
        def command(args,**kwargs):
            if args[:2]==['ufw','status']:
                return subprocess.CompletedProcess([],0,'Status: active\n54321/tcp ALLOW Anywhere # Didushan-3xui-relay\n' if (self.root/'ufw-added.txt').exists() else 'Status: active\n','')
            if args[:2]==['ufw','allow']:raise RuntimeError('ufw failed after rule created')
            return subprocess.CompletedProcess([],0,'','')
        self.run.side_effect=command
        with patch.object(m.shutil,'which',return_value='/usr/sbin/ufw'),self.assertRaisesRegex(RuntimeError,'ufw failed'):
            self.apply()
        self.assert_original()
        self.assertIn(['ufw','--force','delete','allow','54321/tcp'],[c.args[0] for c in self.run.call_args_list])
        self.assertEqual((self.root/'ufw-added.txt').read_text(),'')

    def test_object_fields_complete_add_and_output_link(self):
        self.panel.object_fields=True
        self.test_success_preserves_old_nodes_and_persists_new_link()

    def test_object_fields_allow_recovery_of_previous_failed_add(self):
        self.panel.object_fields=True
        self.test_killed_transaction_can_be_recovered_later()

    def test_object_fields_roll_back_after_node_failure(self):
        self.panel.object_fields=True
        self.test_node_failure_rolls_back()
