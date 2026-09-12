"""Real Xray integration. Run explicitly with XRAY_TEST_BIN; uses no real proxy credentials.
Uses loopback TLS target, HTTP and DNS responders. No external network required.
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import struct
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('deploy', Path(__file__).resolve().parents[1] / 'deploy-3xui-dual.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
BIN = Path(os.environ['XRAY_TEST_BIN']).resolve()
def free():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(root/'key.pem'),'-out',str(root/'cert.pem'),'-days','1','-subj','/CN=dl.google.com'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body=b'8.8.8.8';self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args): pass
    http = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=http.serve_forever,daemon=True).start()
    target = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.minimum_version=ssl.TLSVersion.TLSv1_3;tls.set_alpn_protocols(['h2','http/1.1']);tls.load_cert_chain(root/'cert.pem',root/'key.pem')
    target.socket=tls.wrap_socket(target.socket,server_side=True)
    threading.Thread(target=target.serve_forever,daemon=True).start()
    upstream_port, direct_port, home_port, api_port = [free() for _ in range(4)]
    dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dns.bind(('127.0.0.1', 0))
    dns.settimeout(.2)
    stop = threading.Event()
    def answer_dns():
        while not stop.is_set():
            try: query, peer = dns.recvfrom(4096)
            except socket.timeout: continue
            response = query[:2] + b'\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00' + query[12:]
            response += b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x5d\xb8\xd8\x22'
            dns.sendto(response, peer)
    worker = threading.Thread(target=answer_dns, daemon=True); worker.start()
    s = {'ip':'8.8.4.4','socks_ip':'127.0.0.1','socks_port':upstream_port,'socks_user':'test-user',
         'socks_password':'test-password','api_port':api_port,'target':'dl.google.com', 'nodes':m.make_nodes(direct_port,home_port)}
    for n in s['nodes']:
        n['private'],n['public'] = m.parse_x25519(subprocess.check_output([BIN,'x25519'],text=True))
    server = m.template(s)
    server['log'] = {'loglevel':'debug'}
    server['inbounds'] = []
    server['outbounds'][0]['settings'] = {'redirect':f'127.0.0.1:{http.server_port}', 'finalRules':[{'action':'allow'}]}
    for n in s['nodes']:
        item = m.inbound(s,n)
        if n['tag'] == m.DIRECT_TAG: item['tag'] = 'new-ordinary-inbound'
        stream=json.loads(item['streamSettings']);stream['realitySettings']['show']=True;stream['realitySettings']['target']=f'127.0.0.1:{target.server_port}';item['streamSettings']=json.dumps(stream)
        server['inbounds'].append({k:json.loads(v) if k in ('settings','streamSettings','sniffing') else v for k,v in item.items() if k in ('tag','listen','port','protocol','settings','streamSettings','sniffing')})
    upstream = {'inbounds':[{'listen':'127.0.0.1','port':upstream_port,'protocol':'socks','settings':{'auth':'password','accounts':[{'user':s['socks_user'],'pass':s['socks_password']}],'udp':True,'ip':'127.0.0.1'}}],
       'outbounds':[{'tag':'tcp','protocol':'freedom','settings':{'redirect':f'127.0.0.1:{http.server_port}', 'finalRules':[{'action':'allow'}]}}, {'tag':'udp','protocol':'freedom','settings':{'redirect':f'127.0.0.1:{dns.getsockname()[1]}', 'finalRules':[{'action':'allow'}]}}],
       'routing':{'rules':[{'type':'field','network':'udp','outboundTag':'udp'}]}}
    processes=[]
    env = dict(os.environ,XRAY_LOCATION_ASSET=str(BIN.parent))
    log = (root/'xray.log').open('w')
    try:
        for name,cfg in [('upstream',upstream),('server',server)]:
            path=root/(name+'.json');path.write_text(json.dumps(cfg))
            subprocess.run([BIN,'run','-test','-c',path],env=env,check=True,stdout=log,stderr=log)
            processes.append(subprocess.Popen([BIN,'run','-c',path],env=env,stdout=log,stderr=log))
        time.sleep(1)
        original_save=m.save
        def debug_save(path,value):
            if isinstance(value,dict) and 'outbounds' in value:
                value.setdefault('log',{})['loglevel']='debug'
            original_save(path,value)
        with patch.object(m,'save',side_effect=debug_save), patch.object(m,'PROBE_URLS',['http://1.1.1.1/']), patch.object(m,'ROOT',root), patch.object(m,'LOG',root/'install.log'), patch.object(m,'APP',BIN.parent), patch.object(m,'xray_bin',return_value=Path(os.environ.get('XRAY_CLIENT_BIN',str(BIN)))):
            with m.client(s) as proxies:
                print('TCP via direct:',m.fetch_ip(proxies[0]))
                print('TCP via authenticated SOCKS:',m.fetch_ip(proxies[1]))
                assert m.udp_probe(proxies[1]), 'UDP through REALITY and authenticated SOCKS failed'
                print('UDP via REALITY -> authenticated SOCKS -> local DNS: PASS')
                processes[0].terminate();processes[0].wait()
                assert not m.udp_probe(proxies[1]), 'UDP escaped to direct after upstream outage'
                try: m.fetch_ip(proxies[1])
                except RuntimeError: pass
                else: raise AssertionError('TCP escaped to direct after upstream outage')
                print('Upstream outage: TCP and UDP fail closed; direct:',m.fetch_ip(proxies[0]))
    except BaseException:
        log.flush();print((root/'xray.log').read_text()[-6500:]);print((root/'probe.log').read_text()[-6500:]);raise
    finally:
        for proc in processes:
            if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
        stop.set();worker.join();dns.close();log.close();http.shutdown();target.shutdown()
