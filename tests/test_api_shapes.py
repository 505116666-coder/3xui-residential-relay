"""Verify real HTTP form serialization, not only a mocked request interface."""
import copy
import json
import unittest
import urllib.parse
from unittest.mock import Mock, patch
from test_deploy import m, state


class APIShapeTests(unittest.TestCase):
    def test_posted_nested_configs_are_valid_json_and_nulls_are_omitted(self):
        s=state()
        entry=m.inbound(s,s['nodes'][1])
        for key in ('settings','streamSettings','sniffing'):
            entry[key]=json.loads(entry[key])
        entry.update(id=3,nodeId=None,enable=True)
        before=copy.deepcopy(entry)
        conn=Mock();response=conn.getresponse.return_value
        response.status=200;response.getheaders.return_value=[]
        response.read.return_value=b'{"success":true,"obj":{}}'
        with patch.object(m.http.client,'HTTPConnection',return_value=conn):
            m.API(s).request('panel/api/inbounds/update/3',entry)
        method,path,body,headers=conn.request.call_args.args
        self.assertEqual(method,'POST')
        self.assertEqual(headers['Content-Type'],'application/x-www-form-urlencoded')
        form=urllib.parse.parse_qs(body.decode())
        for key in ('settings','streamSettings','sniffing'):
            self.assertEqual(json.loads(form[key][0]),entry[key])
        self.assertNotIn('nodeId',form)
        self.assertEqual(entry,before)

    def test_legacy_json_text_is_not_double_encoded(self):
        entry=m.inbound(state(),state()['nodes'][1])
        form=urllib.parse.parse_qs(m.api_form(entry).decode())
        for key in ('settings','streamSettings','sniffing'):
            self.assertEqual(form[key][0],entry[key])

    def test_xray_setting_supports_both_nested_shapes(self):
        cfg=m.template(state())
        for nested in (cfg,json.dumps(cfg)):
            for value in ({'xraySetting':nested},json.dumps({'xraySetting':nested})):
                api=Mock();api.request.return_value=value
                self.assertEqual(m.get_template(api),cfg)

    def test_snapshot_ignores_only_json_encoding_differences(self):
        entry=dict(m.inbound(state(),state()['nodes'][1]),id=3)
        modern=copy.deepcopy(entry)
        for key in ('settings','streamSettings','sniffing'):
            modern[key]=json.loads(modern[key])
        self.assertEqual(m.inbound_snapshot([entry]),m.inbound_snapshot([modern]))

    def test_invalid_shapes_fail_without_echoing_sensitive_values(self):
        for value in (None,[],42,'invalid-secret'):
            with self.assertRaises(RuntimeError) as caught:m.json_object(value,'settings')
            self.assertNotIn('invalid-secret',str(caught.exception))
