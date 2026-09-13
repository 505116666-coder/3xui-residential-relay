#!/usr/bin/env python3
"""Fresh Ubuntu/Debian deployment; embedded in deploy-3xui-dual.sh."""
import argparse
import base64
import contextlib
import copy
import fcntl
import getpass
import hashlib
import http.client
import http.cookies
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import struct
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import uuid

VERSION = 'v3.7.0'
ACME_COMMIT = '181425b3c8373ca23c0664948b97edf5ed84e9c5'
DIGESTS = {
    'amd64': '0f8dd7baef3458f6591574e24814f322cf7f5e1e27f0a594683745e50be84ec5',
    'arm64': '3caf1db1e8b10bb1fa1324c945522690bcf01c533ee75b377268f1c01a3ce896',
}
ROOT = Path('/root/3xui-dual')
APP = Path('/usr/local/x-ui')
CERT = ROOT / 'cert'
ACME = ROOT / 'acme'
STATE = ROOT / 'state.json'
LOG = ROOT / 'install.log'
BIN = APP / 'x-ui'
DIRECT_TAG = 'dual-server'
HOME_TAG = 'dual-residential'
PROBE_URLS = ['https://api.ipify.org', 'https://checkip.amazonaws.com']


def say(s):
    print(s, flush=True)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


def run(args, *, input=None, timeout=180, check=True, cwd=None):
    # Never log command arguments: CLI credentials and private material may be present.
    p = subprocess.run([str(x) for x in args], input=input, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout, cwd=cwd)
    if p.returncode and check:
        # Do not copy third-party diagnostics to public terminal or a traceback.
        with LOG.open('a') as f:
            f.write(f'\nCommand {Path(str(args[0])).name}, exit={p.returncode}\n')
            f.write(p.stdout + p.stderr)
        raise RuntimeError(f'{Path(str(args[0])).name} 执行失败；详情见 {LOG}（包含敏感信息，请勿公开）。')
    return p


def ask(label, default=None, secret=False):
    prompt = label + (f' [{default}]' if default is not None else '') + ': '
    if secret:
        with open('/dev/tty', 'w') as tty:
            return getpass.getpass(prompt, stream=tty)
    # Buffered r+ requires a seekable stream; SSH terminals are not seekable.
    # Keep separate read/write handles, including when stdin is a script or pipe.
    with open('/dev/tty', 'w') as tty_out, open('/dev/tty', 'r') as tty_in:
        tty_out.write(prompt)
        tty_out.flush()
        value = tty_in.readline()
        if not value:
            raise RuntimeError('终端输入已关闭。')
    return value.strip() or (str(default) if default is not None else '')


def public_ip(value):
    p = ipaddress.ip_address(value.strip())
    if p.version != 4 or not p.is_global:
        raise ValueError('此版本只接受公网 IPv4。')
    return str(p)


def server_ip():
    try:
        detected = public_ip(fetch_ip(family4=True))
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
        say('未能自动获取服务器公网 IPv4，请手动填写。')
        while True:
            try:
                return public_ip(ask('请输入服务器公网 IPv4'))
            except ValueError:
                say('请输入有效的公网 IPv4，例如服务器控制台显示的地址。')
    say(f'已自动使用服务器公网 IPv4：{detected}')
    return detected


def socks_host(value):
    """Validate a provider connection hostname or public IPv4, without pinning DNS."""
    value = value.strip()
    if not value:
        raise ValueError('请填写代理商提供的连接域名或 IPv4。')
    if any(c in value for c in '/:@[]\\') or any(c.isspace() for c in value):
        raise ValueError('这里只填域名或 IPv4，不带 socks5://、端口、账号或路径；端口下一步填写。')
    if re.fullmatch(r'[0-9.]+', value):
        return public_ip(value)
    if value.endswith('.'):
        value = value[:-1]
    try:
        host = value.encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise ValueError('连接域名格式无效。')
    labels = host.split('.')
    if len(host) > 253 or len(labels) < 2 or not all(
            re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels):
        raise ValueError('请输入有效的连接域名或公网 IPv4。')
    return host


def ask_socks_host():
    while True:
        try:
            return socks_host(ask('请输入住宅 SOCKS5 连接地址（代理商提供的域名或 IPv4）'))
        except ValueError as exc:
            say(str(exc))


def check_socks_dns(host, proxy_port):
    try:
        socket.getaddrinfo(host, proxy_port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        raise RuntimeError('住宅 SOCKS5 连接地址无法解析为 IPv4。请检查域名拼写及服务器 DNS；此版本要求代理域名有 A 记录。')


def port(value):
    n = int(value)
    if not 1024 <= n <= 65535 and n != 443:
        raise ValueError('节点端口允许 443 或 1024–65535。')
    return n


def random_port(excluded):
    for _ in range(500):
        candidate = 20000 + secrets.randbelow(40000)
        if candidate in excluded or candidate in (443, 8443):
            continue
        try:
            available_ports([candidate])
        except RuntimeError:
            continue
        return candidate
    raise RuntimeError('无法找到空闲端口，请检查服务器端口占用。')


def ask_port(label, excluded):
    default = random_port(excluded)
    while True:
        try:
            selected = port(ask(label, default))
            if selected in excluded:
                raise ValueError('端口与其他服务重复。')
            available_ports([selected])
            return selected
        except (ValueError, RuntimeError) as exc:
            say(f'{exc} 请重新填写端口。')


def make_nodes(dp, hp):
    return [{'tag': tag, 'name': name, 'port': p, 'uuid': str(uuid.uuid4()),
             'subid': secrets.token_hex(8), 'sid': secrets.token_hex(8)}
            for tag, name, p in [(DIRECT_TAG, '服务器直连', dp), (HOME_TAG, '住宅IP中转', hp)]]


def download(url, dest, digest=None):
    if dest.exists() and digest and hashlib.sha256(dest.read_bytes()).hexdigest() == digest:
        return
    run(['curl', '--noproxy', '*', '-fLsS', '--proto', '=https', '--tlsv1.2',
         '--retry', '3', '--connect-timeout', '20', '--max-time', '600',
         url, '-o', dest], timeout=650)
    if digest and hashlib.sha256(dest.read_bytes()).hexdigest() != digest:
        dest.unlink()
        raise RuntimeError('官方发行包 SHA-256 不匹配，已停止。')


def extract(archive, dest):
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        for m in tf.getmembers():
            target = (dest / m.name).resolve()
            if not str(target).startswith(str(dest.resolve()) + '/') or not (m.isfile() or m.isdir()):
                raise RuntimeError('发行包含不允许的路径或链接，停止解压。')
        tf.extractall(dest)


def curl_quote(value):
    if any(c in value for c in '\r\n\x00'):
        raise ValueError('字段不能含换行符或 NUL。')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def fetch_ip(proxy=None, auth=None, family4=False):
    # SOCKS5h keeps destination hostname resolution at the SOCKS server.
    config = 'silent\nshow-error\nfail\nconnect-timeout = 10\nmax-time = 25\n'
    config += 'noproxy = ""\n' if proxy else 'noproxy = "*"\n'
    if proxy:
        config += 'proxy = ' + curl_quote(proxy) + '\n'
    if auth:
        config += 'proxy-user = ' + curl_quote(auth) + '\n'
    if family4:
        config += 'ipv4\n'
    for url in PROBE_URLS:
        p = run(['curl', '--config', '-'], input=config + 'url = ' + curl_quote(url) + '\n',
                timeout=30, check=False)
        if p.returncode == 0:
            try:
                ip = ipaddress.ip_address(p.stdout.strip())
                if ip.is_global:
                    return str(ip)
            except ValueError:
                pass
    raise RuntimeError('出口检测失败：两个 HTTPS 检测地址均未返回公网 IP。检查网络、代理认证或供应商限制。')


def available_ports(ports):
    for p in ports:
        with socket.socket() as sock:
            try:
                sock.bind(('0.0.0.0', p))
            except OSError:
                raise RuntimeError(f'TCP {p} 已被占用。请选择其他节点/面板端口；证书验证需要空闲的 80。')


def check_os():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise RuntimeError('请在服务器上用 root 运行；不要在 Mac 本机执行安装。')
    info = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            info[k] = v.strip('"')
    major = int(info.get('VERSION_ID', '0').split('.')[0])
    if not ((info.get('ID') == 'ubuntu' and major >= 22) or
            (info.get('ID') == 'debian' and major >= 12)):
        raise RuntimeError('脚本支持 Ubuntu 22.04+ / Debian 12+，其他系统先不要运行。')
    if not Path('/run/systemd/system').exists():
        raise RuntimeError('需要 systemd 系统，不适用于容器或非 systemd 环境。')
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(os.uname().machine)
    if not arch:
        raise RuntimeError('仅支持 x86_64 / ARM64。')
    return arch


def json_object(value, label):
    """Panel releases return nested configuration as either JSON text or objects."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise RuntimeError(f'面板 {label} 不是有效 JSON。') from None
    if not isinstance(value, dict):
        raise RuntimeError(f'面板 {label} 应为 JSON 对象。')
    return copy.deepcopy(value)


def api_form(data):
    # The API response uses nested objects, but our requests use form encoding.
    # Never stringify a dict with Python repr, or send null numeric fields as "None".
    return urllib.parse.urlencode({
        key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
        for key, value in data.items() if value is not None
    }).encode()


class API:
    def __init__(self, state, tls=False):
        self.s, self.tls = state, tls
        self.cookies = {}
        self.token = ''

    def request(self, path, data=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.s['panel_port'], timeout=30)
        conn.connect()
        if self.tls:
            conn.sock = ssl.create_default_context().wrap_socket(conn.sock, server_hostname=self.s['ip'])
        if data is not None and path.startswith('panel/api/inbounds/update/'):
            data = {k: v for k, v in data.items() if k != 'clientStats'}
        body = api_form(data) if data is not None else None
        headers = {'Host': f"{self.s['ip']}:{self.s['panel_port']}",
                   'Cookie': '; '.join(f'{k}={v}' for k, v in self.cookies.items())}
        if data is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            headers['X-CSRF-Token'] = self.token
        conn.request('POST' if data is not None else 'GET', self.s['base'] + path, body, headers)
        response = conn.getresponse()
        for key, val in response.getheaders():
            if key.lower() == 'set-cookie':
                c = http.cookies.SimpleCookie()
                c.load(val)
                self.cookies.update({k: morsel.value for k, morsel in c.items()})
        raw = response.read()
        status = response.status
        conn.close()
        if status != 200:
            raise RuntimeError(f'面板 API {path} 返回 HTTP {status}。')
        obj = json.loads(raw)
        if obj.get('success') is not True:
            # API error messages may echo passwords; keep them private.
            with LOG.open('a') as f:
                f.write(f'\nAPI {path}: ' + raw.decode(errors='replace') + '\n')
            raise RuntimeError(f'面板 API {path} 操作失败，见私有日志。')
        return obj.get('obj')

    def login(self):
        self.token = self.request('csrf-token')
        self.request('login', {'username': self.s['username'], 'password': self.s['password']})
        self.token = self.request('csrf-token')
        return self


def wait_panel(s, tls=False):
    for _ in range(30):
        try:
            api = API(s, tls)
            api.token = api.request('csrf-token')
            return api.login()
        except (OSError, ValueError, http.client.HTTPException):
            time.sleep(1)
    raise RuntimeError('面板未就绪，使用 journalctl -u x-ui 检查。')


def xray_bin(s):
    return APP / 'bin' / ('xray-linux-' + s['arch'])


def parse_x25519(output):
    pairs = dict(line.split(':', 1) for line in output.splitlines() if ':' in line)
    pairs = {k.strip().lower().replace(' ', ''): v.strip() for k, v in pairs.items()}
    private = pairs.get('privatekey', '')
    public = pairs.get('password(publickey)') or pairs.get('password') or pairs.get('publickey', '')
    if not private or not public:
        raise RuntimeError('无法解析 Xray x25519 输出。')
    return private, public


def inbound(s, item):
    return {
        'remark': item['name'], 'enable': True, 'listen': '0.0.0.0', 'port': item['port'],
        'protocol': 'vless', 'tag': item['tag'], 'total': 0, 'expiryTime': 0,
        'trafficReset': 'never', 'shareAddrStrategy': 'custom', 'shareAddr': s['ip'],
        'settings': json.dumps({'clients': [{'id': item['uuid'], 'flow': 'xtls-rprx-vision',
            'email': item['tag'], 'enable': True, 'totalGB': 0, 'expiryTime': 0,
            'limitIp': 0, 'subId': item['subid']}], 'decryption': 'none', 'fallbacks': []}),
        'streamSettings': json.dumps({'network': 'tcp', 'security': 'reality', 'tcpSettings': {},
            'realitySettings': {'show': False, 'target': s['target'] + ':443', 'xver': 0,
                'serverNames': [s['target']], 'privateKey': item['private'],
                'minClientVer': '1.8.0',
                'shortIds': [item['sid']],
                'settings': {'publicKey': item['public'], 'fingerprint': 'chrome',
                             'serverName': s['target'], 'spiderX': '/'}}}),
        # Disabled sniffing preserves destination hostnames for residential-side DNS.
        'sniffing': json.dumps({'enabled': False, 'destOverride': [], 'routeOnly': True}),
    }


def template(s):
    # New ordinary inbounds use the server; residential TCP and UDP have an explicit route.
    return {
        'log': {'access': 'none', 'loglevel': 'warning'},
        'api': {'tag': 'api', 'services': ['HandlerService', 'LoggerService', 'StatsService', 'RoutingService']},
        'inbounds': [{'tag': 'api', 'listen': '127.0.0.1', 'port': s['api_port'],
                      'protocol': 'tunnel', 'settings': {'rewriteAddress': '127.0.0.1'}}],
        'outbounds': [
            {'tag': 'server-out', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4',
                'finalRules': [{'action': 'block', 'ip': ['geoip:private']}, {'action': 'allow'}]}},
            {'tag': 'blocked', 'protocol': 'blackhole', 'settings': {}},
            {'tag': 'residential-out', 'protocol': 'socks', 'settings': {'servers': [
                {'address': s['socks_ip'], 'port': s['socks_port'],
                 'users': [{'user': s['socks_user'], 'pass': s['socks_password']}]}]}},
        ],
        'routing': {'domainStrategy': 'AsIs', 'rules': [
            {'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'},
            {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'blocked'},
            {'type': 'field', 'inboundTag': [HOME_TAG], 'network': 'tcp,udp', 'outboundTag': 'residential-out'},
            {'type': 'field', 'inboundTag': [DIRECT_TAG], 'outboundTag': 'server-out'},
        ]},
        'policy': {'levels': {'0': {'statsUserUplink': True, 'statsUserDownlink': True}},
                   'system': {'statsInboundUplink': True, 'statsInboundDownlink': True,
                              'statsOutboundUplink': True, 'statsOutboundDownlink': True}},
        'stats': {},
    }


def update_template(api, value):
    api.request('panel/api/xray/update', {'xraySetting': json.dumps(value)})


def get_template(api):
    value = json_object(api.request('panel/api/xray/', {}), 'Xray 设置')
    return json_object(value['xraySetting'], 'xraySetting')


def validate_routes(value):
    out = value.get('outbounds', [])
    if not out or out[0].get('tag') != 'server-out' or out[0].get('protocol') != 'freedom':
        raise RuntimeError('默认出口应为服务器直连。')
    rules = value.get('routing', {}).get('rules', [])
    expected = [('tcp,udp', 'residential-out')]
    actual = [(r.get('network'), r.get('outboundTag')) for r in rules if HOME_TAG in r.get('inboundTag', [])]
    if actual != expected:
        raise RuntimeError('住宅 TCP/UDP 必须绑定住宅出站。')
    if value.get('routing', {}).get('domainStrategy') != 'AsIs':
        raise RuntimeError('路由 DNS 策略已改变。')
    if any('balancerTag' in r for r in rules) or value.get('routing', {}).get('balancers'):
        raise RuntimeError('出现负载均衡/回退配置，停止验收。')
    expected_rules = [
        {'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'},
        {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'blocked'},
        {'type': 'field', 'inboundTag': [HOME_TAG], 'network': 'tcp,udp', 'outboundTag': 'residential-out'},
        {'type': 'field', 'inboundTag': [DIRECT_TAG], 'outboundTag': 'server-out'},
    ]
    if rules[:len(expected_rules)] != expected_rules:
        raise RuntimeError('路由顺序或规则已被修改；请人工核对后再使用检查工具。')
    home = next((o for o in out if o.get('tag') == 'residential-out'), {})
    if home.get('protocol') != 'socks' or any(k in home for k in ('proxySettings', 'streamSettings')):
        raise RuntimeError('住宅出口类型或底层拨号方式已改变。')


@contextlib.contextmanager
def client(s):
    ports = []
    for _ in s['nodes']:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            ports.append(sock.getsockname()[1])
    if len(set(ports)) != len(ports):
        raise RuntimeError('临时测试端口冲突，请重新执行检查。')
    cfg = {'log': {'loglevel': 'warning'}, 'inbounds': [], 'outbounds': [],
           'routing': {'rules': []}}
    for n, p in zip(s['nodes'], ports):
        cfg['inbounds'].append({'tag': n['tag'], 'listen': '127.0.0.1', 'port': p,
                                'protocol': 'socks', 'settings': {'auth': 'noauth', 'udp': True, 'ip': '127.0.0.1'}})
        cfg['outbounds'].append({'tag': n['tag'], 'protocol': 'vless',
            'settings': {'vnext': [{'address': '127.0.0.1', 'port': n['port'],
                'users': [{'id': n['uuid'], 'encryption': 'none', 'flow': 'xtls-rprx-vision'}]}]},
            'streamSettings': {'network': 'tcp', 'security': 'reality',
                'realitySettings': {'serverName': s['target'], 'fingerprint': 'chrome',
                                   'publicKey': n['public'], 'shortId': n['sid'], 'spiderX': '/'}}})
        cfg['routing']['rules'].append({'type': 'field', 'inboundTag': [n['tag']], 'outboundTag': n['tag']})
    with tempfile.TemporaryDirectory(prefix='probe-', dir=ROOT) as td:
        path = Path(td) / 'client.json'
        save(path, cfg)
        env = dict(os.environ, XRAY_LOCATION_ASSET=str(APP / 'bin'))
        run([xray_bin(s), 'run', '-test', '-c', path])
        with (ROOT / 'probe.log').open('a') as log:
            proc = subprocess.Popen([str(xray_bin(s)), 'run', '-c', str(path)],
                                    stdout=log, stderr=log, env=env)
            try:
                for _ in range(30):
                    if proc.poll() is not None:
                        raise RuntimeError('测试客户端启动失败，见 probe.log。')
                    try:
                        for p in ports:
                            socket.create_connection(('127.0.0.1', p), timeout=1).close()
                        break
                    except OSError:
                        time.sleep(0.2)
                else:
                    raise RuntimeError('测试客户端未就绪。')
                yield ['socks5h://127.0.0.1:' + str(p) for p in ports]
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def restart(s, tls=False):
    run(['systemctl', 'restart', 'x-ui'])
    api = wait_panel(s, tls)
    for _ in range(30):
        try:
            for n in s['nodes']:
                socket.create_connection(('127.0.0.1', n['port']), timeout=1).close()
            return api
        except OSError:
            time.sleep(1)
    raise RuntimeError('节点端口未启动。')


def selftest(s, tls=False, failure=False):
    say('正在通过真实 VLESS/REALITY 握手检测两个入口（服务器本机回环测试）……')
    api = wait_panel(s, tls)
    original = get_template(api)
    validate_routes(original)
    home_out = next(o for o in original['outbounds'] if o['tag'] == 'residential-out')
    servers = home_out['settings']['servers']
    if len(servers) != 1 or servers[0]['address'] != s['socks_ip'] or servers[0]['port'] != s['socks_port']:
        raise RuntimeError('住宅出站地址与部署记录不同，请检查面板配置。')
    with client(s) as proxies:
        server_ip = fetch_ip(proxies[0])
        home_ip = fetch_ip(proxies[1])
        upstream = fetch_ip(f"socks5h://{s['socks_ip']}:{s['socks_port']}",
                            s['socks_user'] + ':' + s['socks_password'], family4=True)
        direct = fetch_ip(family4=True)
        if server_ip != direct:
            raise RuntimeError('服务器节点出口与服务器直接访问出口不一致。')
        if home_ip == direct:
            raise RuntimeError('住宅节点出口与服务器出口相同，停止验收。')
        if home_ip != upstream:
            say('两次住宅出口 IP 不同，记录检测结果；不以两次 IP 相同作为验收条件。')
        udp_ok = udp_probe(proxies[1])
        say('住宅 UDP 实测通过。' if udp_ok else '住宅 UDP 暂未测通（上游不支持或网络限制）；TCP 不受影响，UDP 仍固定走住宅出站。')
        if failure:
            say('正在模拟住宅上游连接失败，确认住宅入口失败且服务器入口仍可用……')
            # Reserve a bound, NON-listening socket. No firewall changes, DNS changes, or real credentials are modified.
            with socket.socket() as closed:
                closed.bind(('127.0.0.1', 0))
                bad = copy.deepcopy(original)
                for out in bad['outbounds']:
                    if out['tag'] == 'residential-out':
                        out['settings']['servers'] = [{'address': '127.0.0.1', 'port': closed.getsockname()[1]}]
                try:
                    update_template(api, bad)
                    api = restart(s, tls)
                    try:
                        fetch_ip(proxies[1])
                    except RuntimeError:
                        pass
                    else:
                        raise RuntimeError('严重：住宅上游失效后请求仍成功，拒绝发布结果。')
                    if udp_ok and udp_probe(proxies[1]):
                        raise RuntimeError('住宅上游失效后 UDP 仍成功，拒绝发布结果。')
                    if fetch_ip(proxies[0]) != direct:
                        raise RuntimeError('模拟故障期间服务器节点异常。')
                finally:
                    # The on-disk recovery copy survives interruption during this controlled test.
                    update_template(wait_panel(s, tls), original)
                    api = restart(s, tls)
                home_ip = fetch_ip(proxies[1])
                if home_ip == direct:
                    raise RuntimeError('恢复后住宅节点出口异常。')
    result = {'server_exit': server_ip, 'residential_exit': home_ip,
              'upstream_exit': upstream, 'failure_test_passed': failure, 'residential_udp_test_passed': udp_ok,
              'tested_at': time.strftime('%Y-%m-%d %H:%M:%S %z'),
              'scope': 'server-local real protocol test; external client connectivity still requires checking'}
    save(ROOT / 'test-result.json', result)
    say(f'出口检查通过：服务器 {server_ip}；住宅 {home_ip}')
    return result


def node_link(s, n):
    query = urllib.parse.urlencode({'encryption': 'none', 'security': 'reality',
        'sni': s['target'], 'fp': 'chrome', 'pbk': n['public'], 'sid': n['sid'],
        'type': 'tcp', 'flow': 'xtls-rprx-vision', 'spx': '/'})
    return f"vless://{n['uuid']}@{s['ip']}:{n['port']}?{query}#{urllib.parse.quote(n['name'])}"


def credentials(s):
    lines = ['3X-UI 一键中转住宅 IP · Didushan', f'面板版本：{VERSION}', f"面板：https://{s['ip']}:{s['panel_port']}{s['base']}",
             f"用户名：{s['username']}", f"密码：{s['password']}", '',
             '以下链接包含节点凭据，请勿公开：']
    for n in s['nodes'] + [entry['node'] for entry in s.get('additional_residential', [])]:
        lines += ['', n['name'], node_link(s, n)]
    lines += ['', '复制节点链接后，导入客户端，分别测试服务器和住宅出口。',
              '住宅代理不通时不会自动换成服务器 IP；UDP 能否使用取决于代理商和网络。']
    return '\n'.join(lines) + '\n'


def setup_certificate(s):
    say('申请公网 IPv4 的受信任 HTTPS 证书；外部 TCP 80 必须可达……')
    CERT.mkdir(exist_ok=True, mode=0o700)
    if not (ACME / 'acme.sh').exists():
        archive = ROOT / 'acme-source.tar.gz'
        download(f'https://codeload.github.com/acmesh-official/acme.sh/tar.gz/{ACME_COMMIT}', archive)
        src = ROOT / 'acme-source'
        extract(archive, src)
        script = next(src.glob('*/acme.sh'))
        run(['sh', script, '--install', '--home', ACME, '--config-home', ACME,
             '--nocron', '--noprofile', '--accountemail', s['email']], cwd=script.parent)
    acme = ['sh', ACME / 'acme.sh', '--home', ACME, '--config-home', ACME]
    cert_fresh = (CERT / 'fullchain.pem').exists() and run(
        ['openssl', 'x509', '-in', CERT / 'fullchain.pem', '-noout', '-checkend', '172800'],
        check=False).returncode == 0
    if not cert_fresh:
        run(acme + ['--register-account', '--server', 'letsencrypt', '-m', s['email']], timeout=300)
        # --days is renewal interval, NOT certificate validity. Renew well before the six-day expiry.
        run(acme + ['--issue', '--server', 'letsencrypt', '-d', s['ip'], '--standalone',
                    '--httpport', '80', '--keylength', 'ec-256',
                    '--certificate-profile', 'shortlived', '--days', '2'], timeout=600)
    hook = ROOT / 'reload-panel.sh'
    save(hook, '#!/bin/sh\nset -eu\nif systemctl is-active --quiet x-ui; then\n  systemctl restart x-ui\nfi\n')
    hook.chmod(0o700)
    run(acme + ['--install-cert', '-d', s['ip'], '--ecc', '--key-file', CERT / 'privkey.pem',
                '--fullchain-file', CERT / 'fullchain.pem', '--reloadcmd', str(hook)], timeout=300)
    run(['openssl', 'x509', '-in', CERT / 'fullchain.pem', '-noout', '-checkip', s['ip']])
    run(['openssl', 'x509', '-in', CERT / 'fullchain.pem', '-noout', '-checkend', '172800'])
    renew = ROOT / 'renew.sh'
    save(renew, '''#!/bin/sh
set -eu
umask 077
trap 'logger -t 3xui-dual "HTTPS certificate renewal/check failed; see journalctl -u 3xui-dual-renew.service"' EXIT
/bin/sh /root/3xui-dual/acme/acme.sh --cron --home /root/3xui-dual/acme --config-home /root/3xui-dual/acme
/usr/bin/openssl x509 -in /root/3xui-dual/cert/fullchain.pem -noout -checkend 172800
trap - EXIT
''')
    renew.chmod(0o700)
    save('/etc/systemd/system/3xui-dual-renew.service', '''[Unit]
Description=Renew and check 3x-ui IPv4 HTTPS certificate
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
UMask=0077
ExecStart=/bin/sh /root/3xui-dual/renew.sh
TimeoutStartSec=900
''')
    save('/etc/systemd/system/3xui-dual-renew.timer', '''[Unit]
Description=Check 3x-ui short-lived IP certificate every 12 hours
[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=900
Persistent=true
[Install]
WantedBy=timers.target
''')
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', '3xui-dual-renew.timer'])


def deploy(s):
    say('检查住宅 SOCKS5 认证和出口……')
    check_socks_dns(s['socks_ip'], s['socks_port'])
    s['baseline_home'] = fetch_ip(f"socks5h://{s['socks_ip']}:{s['socks_port']}",
                                s['socks_user'] + ':' + s['socks_password'], family4=True)
    s['baseline_server'] = fetch_ip(family4=True)
    if s['baseline_home'] == s['baseline_server']:
        raise RuntimeError('SOCKS5 出口与服务器相同，停止安装。')
    say(f"住宅代理连接地址：{s['socks_ip']}；实测出口 IP：{s['baseline_home']}")
    say('检查 REALITY 目标 TLS 1.3、h2 和证书……')
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.set_alpn_protocols(['h2'])
    with socket.create_connection((s['target'], 443), timeout=15) as raw:
        with ctx.wrap_socket(raw, server_hostname=s['target']) as tls:
            if tls.selected_alpn_protocol() != 'h2':
                raise RuntimeError('REALITY 目标不支持 h2，请更换目标。')
    save(STATE, s)
    available_ports([80, s['panel_port'], s['api_port']] + [n['port'] for n in s['nodes']])
    # Manage only UFW if it is already active; never flush firewall rules or change SSH rules.
    if shutil.which('firewall-cmd') and run(['firewall-cmd', '--state'], check=False).returncode == 0:
        raise RuntimeError('检测到 firewalld，请先手动放行所列端口，再处理防火墙适配；脚本不会替换防火墙。')
    if shutil.which('ufw') and 'Status: active' in run(['ufw', 'status'], check=False).stdout:
        for p in [80, s['panel_port']] + [n['port'] for n in s['nodes']]:
            status = run(['ufw', 'status'], check=False).stdout
            if re.search(r'^' + str(p) + r'/tcp(?:\s|$)', status, re.M):
                continue  # Existing rules belong to the user, including restrictive rules.
            manifest = ROOT / 'ufw-added.txt'
            recorded = manifest.read_text() if manifest.exists() else ''
            if str(p) not in recorded.splitlines():
                save(manifest, recorded + str(p) + '\n')
            run(['ufw', 'allow', str(p) + '/tcp', 'comment', 'Didushan-3xui-relay'])
    archive = ROOT / ('x-ui-linux-' + s['arch'] + '.tar.gz')
    say(f'安装固定版本 {VERSION}，验证官方发行包 SHA-256……')
    download(f'https://github.com/MHSanaei/3x-ui/releases/download/{VERSION}/{archive.name}',
             archive, DIGESTS[s['arch']])
    if not BIN.exists():
        unpack = ROOT / 'unpack'
        extract(archive, unpack)
        package = unpack / 'x-ui'
        if not (package / 'x-ui').is_file():
            raise RuntimeError('发行包目录结构与预期不同。')
        shutil.copytree(package, APP)
    BIN.chmod(0o700)
    xray_bin(s).chmod(0o700)
    Path('/etc/x-ui').mkdir(mode=0o700, exist_ok=True)
    Path('/etc/x-ui').chmod(0o700)
    # Keep configuration/database outside the replaceable binary directory, as upstream expects.
    run([BIN, 'setting', '-username', s['username'], '-password', s['password'],
         '-port', str(s['panel_port']), '-webBasePath', s['base'], '-listenIP', '127.0.0.1'], cwd=APP)
    # No public plaintext subscription listener. These version-pinned settings are
    # written offline before the panel starts, using the schema checked in upstream.
    db_path = Path('/etc/x-ui/x-ui.db')
    with sqlite3.connect(str(db_path)) as db:
        columns = {r[1] for r in db.execute('PRAGMA table_info(settings)')}
        if not {'key', 'value'}.issubset(columns):
            raise RuntimeError('数据库设置表结构不匹配，拒绝继续。')
        for key, value in [('subEnable', 'false'), ('subJsonEnable', 'false'), ('subListen', '127.0.0.1')]:
            if db.execute('SELECT 1 FROM settings WHERE key=?', (key,)).fetchone():
                db.execute('UPDATE settings SET value=? WHERE key=?', (value, key))
            else:
                db.execute('INSERT INTO settings (key,value) VALUES (?,?)', (key, value))
    save('/etc/systemd/system/x-ui.service', '''[Unit]
Description=3x-ui panel (dual-node deployment)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=root
WorkingDirectory=/usr/local/x-ui
Environment=XUI_DB_FOLDER=/etc/x-ui
Environment=XUI_BIN_FOLDER=/usr/local/x-ui/bin
UMask=0077
ExecStart=/usr/local/x-ui/x-ui
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
''')
    run(['systemctl', 'daemon-reload'])
    # Certificate issuance occurs before first web service start. No public HTTP panel.
    setup_certificate(s)
    run([BIN, 'setting', '-webCert', CERT / 'fullchain.pem', '-webCertKey', CERT / 'privkey.pem'], cwd=APP)
    run(['systemctl', 'start', 'x-ui'])
    api = wait_panel(s, True)
    if not s['nodes'][0].get('private'):
        for n in s['nodes']:
            result = run([xray_bin(s), 'x25519']).stdout
            n['private'], n['public'] = parse_x25519(result)
        save(STATE, s)
    desired = template(s)
    save(ROOT / 'routing-recovery.json', desired)
    # Install explicit residential routing BEFORE creating any public inbound.
    update_template(api, desired)
    existing = api.request('panel/api/inbounds/list')
    if any(n['tag'] not in {DIRECT_TAG, HOME_TAG} for n in existing):
        raise RuntimeError('发现其他入口；拒绝覆盖已被手动扩展的部署。')
    for n in s['nodes']:
        data = inbound(s, n)
        match = next((i for i in existing if i['tag'] == n['tag']), None)
        if match:
            api.request('panel/api/inbounds/update/' + str(match['id']), data)
        else:
            api.request('panel/api/inbounds/add', data)
    api = restart(s, True)
    run([xray_bin(s), 'run', '-test', '-c', APP / 'bin/config.json'], cwd=APP / 'bin')
    result = selftest(s, True, failure=True)
    # Only publish the panel after certificate validation and real-protocol tests pass.
    run(['systemctl', 'stop', 'x-ui'])
    run([BIN, 'setting', '-listenIP', '0.0.0.0'], cwd=APP)
    run(['systemctl', 'enable', '--now', 'x-ui'])
    run(['systemctl', 'disable', '--now', '3xui-dual-socks-bridge'], check=False)
    wait_panel(s, True)
    run(['systemctl', 'start', '3xui-dual-renew.service'], timeout=650)
    s['complete'] = True
    save(STATE, s)
    save(ROOT / '登录信息与两个节点.txt', credentials(s))
    say('\n安装完成，服务器自检通过。面板已开启 HTTPS，并设置自动续期。')
    say(credentials(s))
    completion(s)
    write_results(s)


def terminal_link(label, url):
    if sys.stdout.isatty() and os.environ.get('TERM') != 'dumb':
        return f'\033]8;;{url}\033\\{label}\033]8;;\033\\'
    return f'{label}：{url}'


# Seven-row terminal lettering; each lit cell is a solid block, not an image.
BANNER_FONT = {
    'D': ('11110', '10001', '10001', '10001', '10001', '10001', '11110'),
    'I': ('11111', '00100', '00100', '00100', '00100', '00100', '11111'),
    'U': ('10001', '10001', '10001', '10001', '10001', '10001', '01110'),
    'S': ('01111', '10000', '10000', '01110', '00001', '00001', '11110'),
    'H': ('10001', '10001', '10001', '11111', '10001', '10001', '10001'),
    'A': ('01110', '10001', '10001', '11111', '10001', '10001', '10001'),
    'N': ('10001', '11001', '11001', '10101', '10011', '10011', '10001'),
}


def banner():
    # Reserve the last column to prevent automatic wrapping in SSH terminals.
    width = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns - 1)
    color = sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'
    colors = (33, 39, 45, 51, 45, 39, 33)
    say('')
    if width >= 47:
        words = ('DIDUSHAN',)
    elif width >= 23:
        words = ('DIDU', 'SHAN')
    else:
        words = ()
        say('Didushan'[:width])
    for word in words:
        for row in range(7):
            pixels = '0'.join(BANNER_FONT[letter][row] for letter in word)
            # Distribute extra columns across glyph strokes as well as gaps,
            # enlarging the letters themselves instead of only their spacing.
            line = ''.join(('█' if pixel == '1' else ' ') *
                           (((i + 1) * width // len(pixels)) - (i * width // len(pixels)))
                           for i, pixel in enumerate(pixels)).rstrip()
            if color:
                line = f'\033[38;5;{colors[row]}m' + line + '\033[0m'
            say(line)
        say('')
    subtitle = '3X-UI 一键中转住宅 IP'
    if width >= 21:
        say(' ' * ((width - 21) // 2) + subtitle)
    say('')


def completion(s):
    say(terminal_link('作者 YouTube 频道', 'https://www.youtube.com/@Didushan') + '  |  ' + terminal_link('电报联系', 'https://t.me/didushan9'))


def copy_values(s):
    links = [line for line in credentials(s).splitlines() if line.startswith('vless://')]
    address = f"https://{s['ip']}:{s['panel_port']}{s['base']}"
    return [('服务器直连节点', links[0]), ('住宅中转节点', links[1]),
            ('全部面板信息', f"面板：{address}\n用户名：{s['username']}\n密码：{s['password']}"),
            ('面板地址', address), ('用户名', s['username']), ('密码', s['password'])] + [
            (entry['node']['name'], node_link(s, entry['node'])) for entry in s.get('additional_residential', [])]


def copy_result(s, choice):
    values = copy_values(s)
    if choice == 0:
        say('\n'.join(f'{i}. {v[0]}' for i,v in enumerate(values,1)))
        selected = ask('输入要复制的序号，直接回车退出', '0')
        if selected == '0': return
        if not selected.isdigit():
            say(f'请输入 1 到 {len(values)}。'); return
        choice = int(selected)
    if not 1 <= choice <= len(values):
        say(f'请输入 1 到 {len(values)}。'); return
    if not sys.stdout.isatty():
        say(values[choice-1][1]); return
    value = base64.b64encode(values[choice-1][1].encode()).decode()
    sys.stdout.write('\033]52;c;' + value + '\a');sys.stdout.flush()
    say('已向终端发送复制请求，请粘贴检查。若终端不支持，请直接选中终端中的结果复制。')


def write_results(s):
    save(ROOT / '登录信息与两个节点.txt', credentials(s))
    (ROOT / '结果.html').unlink(missing_ok=True)


def recv_exact(sock, size):
    data = b''
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise OSError('SOCKS connection closed')
        data += part
    return data


def udp_probe(proxy):
    """A real DNS UDP exchange through the temporary local SOCKS->VLESS client.
    No direct UDP fallback. TCP-only suppliers may fail this optional probe.
    """
    host = urllib.parse.urlsplit(proxy)
    try:
        with socket.create_connection((host.hostname, host.port), timeout=4) as control:
            control.settimeout(4)
            control.sendall(b'\x05\x01\x00')
            if recv_exact(control, 2) != b'\x05\x00':
                return False
            control.sendall(b'\x05\x03\x00\x01' + b'\x00' * 6)
            head = recv_exact(control, 4)
            if head[:3] != b'\x05\x00\x00':
                return False
            if head[3] == 1:
                address = socket.inet_ntoa(recv_exact(control, 4))
            elif head[3] == 3:
                address = recv_exact(control, recv_exact(control, 1)[0]).decode('ascii')
            elif head[3] == 4:
                address = socket.inet_ntop(socket.AF_INET6, recv_exact(control, 16))
            else:
                return False
            udp_port = struct.unpack('!H', recv_exact(control, 2))[0]
            if address in ('0.0.0.0', '::'):
                address = host.hostname
            family = socket.AF_INET6 if ':' in address else socket.AF_INET
            with socket.socket(family, socket.SOCK_DGRAM) as udp:
                udp.settimeout(3)
                udp.connect((address, udp_port))
                for resolver in ('1.1.1.1', '8.8.8.8'):
                    ident = secrets.token_bytes(2)
                    query = ident + b'\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00' + b'\x07example\x03com\x00\x00\x01\x00\x01'
                    udp.send(b'\x00\x00\x00\x01' + socket.inet_aton(resolver) + b'\x00\x35' + query)
                    try:
                        packet = udp.recv(4096)
                    except socket.timeout:
                        continue
                    if len(packet) < 10 or packet[:3] != b'\x00\x00\x00':
                        continue
                    offset = {1: 10, 4: 22}.get(packet[3])
                    if packet[3] == 3:
                        offset = 7 + packet[4]
                    if offset is None:
                        continue
                    dns = packet[offset:]
                    if len(dns) >= 12 and dns[:2] == ident and dns[2] & 128 and dns[3] & 15 == 0 and dns[6:8] != b'\x00\x00':
                        return True
    except (OSError, ValueError, IndexError, UnicodeError):
        pass
    return False


def migration_template(old, s):
    value = copy.deepcopy(old)
    outbounds = value['outbounds']
    home = next(o for o in outbounds if o.get('tag') == 'residential-out')
    servers = home['settings']['servers']
    if len(servers) != 1:
        raise RuntimeError('住宅出站有多个地址，不能自动迁移。')
    server = servers[0]
    if server['address'] == '127.0.0.1' and server['port'] == s.get('bridge_port'):
        server['address'], server['port'] = s['socks_ip'], s['socks_port']
    else:
        # Preserve a provider already changed in the panel.
        s['socks_ip'] = socks_host(server['address'])
        s['socks_port'] = server['port']
    users = server.get('users', [])
    if len(users) != 1:
        raise RuntimeError('住宅认证配置已改变，不能自动迁移。')
    s['socks_user'], s['socks_password'] = users[0]['user'], users[0]['pass']
    direct = next(o for o in outbounds if o.get('tag') == 'server-out')
    value['outbounds'] = [direct] + [o for o in outbounds if o is not direct]
    rules = value['routing']['rules']
    rules[:] = [r for r in rules if not (r.get('inboundTag') == [HOME_TAG] and r.get('network') == 'udp' and r.get('outboundTag') == 'blocked')]
    for r in rules:
        if r.get('inboundTag') == [HOME_TAG] and r.get('outboundTag') == 'residential-out':
            r['network'] = 'tcp,udp'
    validate_routes(value)
    return value


def migrate(s):
    if not s.get('complete'):
        raise RuntimeError('尚未完成的安装请使用 --resume。')
    api = wait_panel(s, True)
    backup = ROOT / 'migration-backup.json'
    if backup.exists():
        previous = json.loads(backup.read_text())
        if previous.get('pending'):
            raise RuntimeError('上次迁移中断，请先运行 --rollback-migration 恢复，再重试。')
    old = get_template(api)
    entries = api.request('panel/api/inbounds/list')
    managed = [i for i in entries if i.get('tag') in (DIRECT_TAG, HOME_TAG)]
    if len(managed) != 2:
        raise RuntimeError('未找到原来的两个入站，停止迁移。')
    old_state = copy.deepcopy(s)
    desired = migration_template(old, s)
    record = {'pending': True, 'state': old_state, 'template': old, 'inbounds': managed}
    save(backup, record)
    try:
        update_template(api, desired)
        for entry in managed:
            item = copy.deepcopy(entry)
            stream = json_object(item['streamSettings'], 'streamSettings')
            stream['realitySettings']['minClientVer'] = '1.8.0'
            item['streamSettings'] = json.dumps(stream)
            api.request('panel/api/inbounds/update/' + str(item['id']), item)
        restart(s, True)
        selftest(s, True, failure=True)
    except BaseException:
        rollback_migration(s)
        raise
    record['pending'] = False
    save(backup, record)
    # Service is no longer in the data path; remove only our own dependency.
    unit = Path('/etc/systemd/system/x-ui.service')
    if unit.exists():
        save(unit, unit.read_text().replace(' 3xui-dual-socks-bridge.service', ''))
    run(['systemctl', 'disable', '--now', '3xui-dual-socks-bridge'], check=False)
    run(['systemctl', 'daemon-reload'])
    s['schema_version'] = 2
    save(STATE, s)
    save(ROOT / 'routing-recovery.json', desired)
    shutil.copyfile(Path(__file__), ROOT / 'manager.py') if Path(__file__).resolve() != ROOT / 'manager.py' else None
    save(ROOT / '登录信息与两个节点.txt', credentials(s))
    say('迁移完成：住宅地址直接在面板管理，TCP/UDP 均走住宅出站，新增普通入站默认直连。')
    completion(s)
    write_results(s)


def rollback_migration(s):
    backup = ROOT / 'migration-backup.json'
    record = json.loads(backup.read_text())
    if not record.get('pending'):
        raise RuntimeError('没有待恢复的中断迁移。')
    api = wait_panel(record['state'], True)
    update_template(api, record['template'])
    for entry in record['inbounds']:
        api.request('panel/api/inbounds/update/' + str(entry['id']), entry)
    if record['state'].get('bridge_port'):
        run(['systemctl', 'start', '3xui-dual-socks-bridge'])
    restart(record['state'], True)
    save(STATE, record['state'])
    record['pending'] = False
    save(backup, record)
    say('已恢复迁移前的路由和入站配置。')


def residential_extension(original, node, proxy):
    """Preserve existing configuration; place the exact new route before custom rules."""
    validate_routes(original)
    value = copy.deepcopy(original)
    tag = node['tag']
    outbound_tag = tag + '-out'
    if any(o.get('tag') == outbound_tag for o in value['outbounds']) or any(
            tag in rule.get('inboundTag', []) for rule in value['routing']['rules']):
        raise RuntimeError('新增标签冲突，请重试。')
    value['outbounds'].append({'tag': outbound_tag, 'protocol': 'socks', 'settings': {'servers': [{
        'address': proxy['host'], 'port': proxy['port'],
        'users': [{'user': proxy['username'], 'pass': proxy['password']}]}]}})
    value['routing']['rules'].insert(4, {'type': 'field', 'inboundTag': [tag],
                                        'network': 'tcp,udp', 'outboundTag': outbound_tag})
    return value


def added_node_state(s, node):
    value = copy.deepcopy(s)
    value['nodes'] = [node]
    value['additional_residential'] = []
    return value


def rollback_add(s):
    journal = ROOT / 'pending-add.json'
    if not journal.exists():
        say('没有待恢复的住宅添加操作。')
        return
    record = json.loads(journal.read_text())
    tag = record['node']['tag']
    # An atomic state write is the commit point. Never undo a committed addition.
    current_state = json.loads(STATE.read_text())
    if any(e['node']['tag'] == tag for e in current_state.get('additional_residential', [])):
        journal.unlink()
        say('上次添加已完成，可用 --results 查看节点。')
        return
    api = wait_panel(s, True)
    current = get_template(api)
    without_rule = copy.deepcopy(record['desired'])
    without_rule['routing']['rules'] = [r for r in without_rule['routing']['rules']
                                       if tag not in r.get('inboundTag', [])]
    if current not in (record['original'], record['desired'], without_rule):
        raise RuntimeError('添加中断后配置又被修改，停止自动恢复以保留后续修改。')
    matches = [e for e in api.request('panel/api/inbounds/list') if e.get('tag') == tag]
    for entry in matches:
        clients = json_object(entry['settings'], 'settings').get('clients', [])
        if entry['port'] != record['node']['port'] or len(clients) != 1 or clients[0].get('id') != record['node']['uuid']:
            raise RuntimeError('新增入站已被修改，停止自动删除。')
    for entry in matches:
        api.request('panel/api/inbounds/del/' + str(entry['id']), {})
    update_template(api, record['original'])
    restart(s, True)
    if record.get('ufw_added'):
        p = record['node']['port']
        status = run(['ufw', 'status'], check=False).stdout
        if re.search(r'^' + str(p) + r'/tcp\s+.*# Didushan-3xui-relay\s*$', status, re.M):
            run(['ufw', '--force', 'delete', 'allow', str(p) + '/tcp'])
        manifest = ROOT / 'ufw-added.txt'
        if manifest.exists():
            save(manifest, ''.join(line + '\n' for line in manifest.read_text().splitlines() if line != str(p)))
    journal.unlink()
    say('已撤回本次新增，原有节点配置已恢复。')


def apply_residential_add(s, node, proxy, original, api):
    journal = ROOT / 'pending-add.json'
    if journal.exists():
        raise RuntimeError('上次添加未结束，请先运行 --rollback-add。')
    desired = residential_extension(original, node, proxy)
    record = {'node': node, 'original': original, 'desired': desired, 'ufw_added': False}
    # Persist recovery before any panel mutation; survives a disconnected SSH session.
    save(journal, record)
    try:
        disabled = inbound(s, node)
        disabled['enable'] = False
        api.request('panel/api/inbounds/add', disabled)
        entries = [e for e in api.request('panel/api/inbounds/list') if e.get('tag') == node['tag']]
        if len(entries) != 1:
            raise RuntimeError('新增入站未正确保存。')
        entry = entries[0]
        if entry['port'] != node['port'] or json_object(entry['settings'], 'settings')['clients'][0]['id'] != node['uuid']:
            raise RuntimeError('新增入站参数不一致。')
        update_template(api, desired)
        if get_template(api) != desired:
            raise RuntimeError('新增住宅路由未正确保存。')
        # Load the route before enabling the inlet, so it can never use default direct.
        api = restart(s, True)
        # The list response includes read-only statistics. Use the dedicated
        # endpoint rather than round-tripping that response through update's form binder.
        api.request('panel/api/inbounds/setEnable/' + str(entry['id']), {'enable': True})
        enabled = [e for e in api.request('panel/api/inbounds/list') if e.get('tag') == node['tag']]
        expected = copy.deepcopy(entry)
        expected['enable'] = True
        if len(enabled) != 1 or inbound_snapshot(enabled) != inbound_snapshot([expected]):
            raise RuntimeError('新增入站未正确启用，或节点参数被改变。')
        probe_state = added_node_state(s, node)
        api = restart(probe_state, True)
        run([xray_bin(s), 'run', '-test', '-c', APP / 'bin/config.json'], cwd=APP / 'bin')
        if get_template(api) != desired:
            raise RuntimeError('应用后路由与预期不一致。')
        with client(probe_state) as proxies:
            exit_ip = fetch_ip(proxies[0])
            if exit_ip == fetch_ip(family4=True):
                raise RuntimeError('新增住宅节点出口与服务器相同。')
            udp_ok = udp_probe(proxies[0])
        # Open only this new TCP listener; UDP is carried inside the VLESS stream.
        if shutil.which('ufw') and 'Status: active' in run(['ufw', 'status'], check=False).stdout:
            status = run(['ufw', 'status'], check=False).stdout
            if not re.search(r'^' + str(node['port']) + r'/tcp(?:\s|$)', status, re.M):
                record['ufw_added'] = True
                save(journal, record)
                manifest = ROOT / 'ufw-added.txt'
                recorded = manifest.read_text() if manifest.exists() else ''
                if str(node['port']) not in recorded.splitlines():
                    save(manifest, recorded + str(node['port']) + '\n')
                run(['ufw', 'allow', str(node['port']) + '/tcp', 'comment', 'Didushan-3xui-relay'])
        updated = copy.deepcopy(s)
        updated.setdefault('additional_residential', []).append({
            'node': node, 'proxy': proxy, 'exit_ip': exit_ip, 'udp_test_passed': udp_ok})
        save(ROOT / 'manager.py', Path(__file__).read_text())
        save(STATE, updated)
    except BaseException:
        rollback_add(s)
        raise
    s.clear()
    s.update(updated)
    journal.unlink()
    write_results(s)
    show_added_result(s, node, exit_ip, udp_ok)


def show_added_result(s, node, exit_ip, udp_ok):
    divider = '─' * max(1, min(64, shutil.get_terminal_size(fallback=(80, 24)).columns - 1))
    say('\n' + divider)
    say('  住宅 IP 添加成功')
    say(divider + '\n')
    say(f"  节点名称：{node['name']}")
    say(f"  节点端口：{node['port']}")
    say(f"  实测出口：{exit_ip}")
    say('  TCP 检测：通过')
    say('  UDP 检测：通过' if udp_ok else '  UDP 检测：暂未测通，仍绑定住宅代理')
    say('\n' + divider)
    say('  节点链接 · 复制下方完整链接，导入客户端')
    say(divider + '\n')
    say(node_link(s, node))
    say('\n' + divider)
    say('  使用提示\n')
    say(f"  1. 在云安全组或其他防火墙放行 TCP {node['port']}。")
    say('  2. 导入客户端后，连接新节点并确认出口 IP。')
    say('\n' + divider + '\n')
    completion(s)
    say('')


def inbound_snapshot(entries):
    # Traffic counters change while the wizard is open; compare configuration only.
    keys = ('id', 'tag', 'port', 'enable', 'listen', 'protocol', 'settings', 'streamSettings', 'sniffing', 'remark')
    snapshots = []
    for entry in entries:
        snapshot = {k: entry.get(k) for k in keys}
        for key in ('settings', 'streamSettings', 'sniffing'):
            if snapshot[key] not in (None, ''):
                snapshot[key] = json_object(snapshot[key], key)
        snapshots.append(snapshot)
    return sorted(snapshots, key=lambda e: e['id'])


def add_residential(s):
    if not s.get('complete'):
        raise RuntimeError('请先完成安装，再添加住宅 IP。')
    if (ROOT / 'pending-add.json').exists():
        raise RuntimeError('上次添加未结束，请先运行 --rollback-add，再用 --results 查看结果。')
    migration = ROOT / 'migration-backup.json'
    if migration.exists() and json.loads(migration.read_text()).get('pending'):
        raise RuntimeError('上次迁移未完成，请先运行 --rollback-migration。')
    api = wait_panel(s, True)
    original = get_template(api)
    validate_routes(original)
    entries = api.request('panel/api/inbounds/list')
    if shutil.which('firewall-cmd') and run(['firewall-cmd', '--state'], check=False).returncode == 0:
        raise RuntimeError('检测到 firewalld，当前新增功能尚不支持自动配置该防火墙。')
    name = ask('新住宅节点名称', '住宅中转-' + str(len(s.get('additional_residential', [])) + 2))
    if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
        raise ValueError('节点名称须为 1–64 个可显示字符。')
    host = ask_socks_host()
    while True:
        try:
            proxy_port = int(ask('住宅 SOCKS5 端口'))
            if not 1 <= proxy_port <= 65535:
                raise ValueError()
            break
        except ValueError:
            say('请输入 1–65535 的端口。')
    user = ask('住宅 SOCKS5 用户名')
    password = ask('住宅 SOCKS5 密码（隐藏输入）', secret=True)
    for value in (user, password):
        curl_quote(value)
        if not value or len(value.encode()) > 255:
            raise ValueError('SOCKS5 用户名和密码必须为 1–255 字节。')
    if ':' in user:
        raise ValueError('当前检测工具不支持含冒号的用户名。')
    proxy = {'host': host, 'port': proxy_port, 'username': user, 'password': password}
    say('检查新住宅代理的认证和出口……')
    check_socks_dns(host, proxy_port)
    upstream = fetch_ip(f'socks5h://{host}:{proxy_port}', user + ':' + password, family4=True)
    if upstream == fetch_ip(family4=True):
        raise RuntimeError('新住宅代理出口与服务器相同，未添加。')
    excluded = {80, s['panel_port'], s['api_port']} | {int(e['port']) for e in entries}
    selected = ask_port('新节点端口（回车使用随机端口）', excluded)
    tag = 'dual-residential-' + secrets.token_hex(6)
    if any(e.get('tag') == tag for e in entries):
        raise RuntimeError('入站标签冲突，请重试。')
    node = {'tag': tag, 'name': name, 'port': selected, 'uuid': str(uuid.uuid4()),
            'subid': secrets.token_hex(8), 'sid': secrets.token_hex(8)}
    node['private'], node['public'] = parse_x25519(run([xray_bin(s), 'x25519']).stdout)
    # Recheck after interactive input, before capturing the recovery baseline.
    if get_template(api) != original or inbound_snapshot(api.request('panel/api/inbounds/list')) != inbound_snapshot(entries):
        raise RuntimeError('填写期间面板配置发生变化，请重试添加。')
    say('正在添加独立节点并绑定住宅路由，应用配置会短暂重启代理服务……')
    apply_residential_add(s, node, proxy, original, api)


def main():
    parser = argparse.ArgumentParser(description='3X-UI 一键中转住宅 IP')
    parser.add_argument('--resume', action='store_true', help='仅恢复本脚本未完成的部署；重新应用其配置')
    parser.add_argument('--check', action='store_true', help='只检查本脚本部署；不注入故障')
    parser.add_argument('--migrate', action='store_true', help='升级本脚本已完成的部署，保留面板及节点凭据')
    parser.add_argument('--rollback-migration', action='store_true', help='恢复中断迁移的配置备份')
    parser.add_argument('--add-residential', action='store_true', help='添加一个住宅代理及独立中转节点')
    parser.add_argument('--rollback-add', action='store_true', help='撤回中断的住宅添加操作')
    parser.add_argument('--results', action='store_true', help='重新显示登录信息和节点，不改配置')
    parser.add_argument('--copy', type=int, nargs='?', const=0, help='复制菜单；可直接指定菜单序号')
    args = parser.parse_args()
    banner()
    if sum((args.resume, args.check, args.migrate, args.rollback_migration, args.results, args.add_residential, args.rollback_add, args.copy is not None)) > 1:
        parser.error('一次只能选择一种操作。')
    os.umask(0o077)
    arch = check_os()
    global DEPLOY_LOCK
    DEPLOY_LOCK = open('/run/lock/3xui-dual.lock', 'w')
    try:
        fcntl.flock(DEPLOY_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('另一个部署或检查进程正在运行。')
    # Unset inherited network proxies: install/probes must not silently use the SSH environment's proxy.
    for key in list(os.environ):
        if key.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy') or key.startswith('XUI_'):
            os.environ.pop(key)
    os.environ['LC_ALL'] = 'C'
    owned_empty = (ROOT / 'owner').is_file() and (ROOT / 'owner').read_text() == '3xui-dual-v1'
    if args.resume and not STATE.exists() and owned_empty and not APP.exists() and not Path('/etc/x-ui').exists():
        args.resume = False  # Earlier interruption during dependency installation / input wizard.
    if args.resume or args.check or args.migrate or args.rollback_migration or args.results or args.add_residential or args.rollback_add or args.copy is not None:
        s = json.loads(STATE.read_text())
        if s.get('managed_by') != '3xui-dual-v1' or s['arch'] != arch:
            raise RuntimeError('不属于本脚本管理的部署。')
        if args.rollback_add:
            rollback_add(s)
            return
        if args.add_residential:
            add_residential(s)
            return
        if (ROOT / 'pending-add.json').exists() and not (args.results or args.copy is not None):
            raise RuntimeError('有未结束的住宅添加操作，请先运行 --rollback-add。')
        # Keep the legacy state key for --resume compatibility; its value may now be a hostname.
        s['socks_ip'] = socks_host(s['socks_ip'])
        if args.results or args.copy is not None:
            if not s.get('complete'):
                raise RuntimeError('请先完成安装，再查看结果。')
            if args.copy is not None: copy_result(s, args.copy)
            else:
                save(ROOT / 'manager.py', Path(__file__).read_text())
                completion(s)
                say(credentials(s))
                write_results(s)
            return
        if args.rollback_migration:
            rollback_migration(s)
            return
        if args.migrate:
            migrate(s)
            return
        if args.check:
            selftest(s, True, failure=False)
            run(['openssl', 'x509', '-in', CERT / 'fullchain.pem', '-noout', '-checkend', '172800'])
            run(['systemctl', 'is-active', '--quiet', '3xui-dual-renew.timer'])
            say('HTTPS 验证与续期定时器检查通过。外部端口仍需客户端验证。')
            return
        if s.get('complete'):
            raise RuntimeError('已有成功部署；请用 --check，脚本不会重置你在面板里的后续修改。')
        run(['systemctl', 'stop', 'x-ui', '3xui-dual-socks-bridge'], check=False)
        run(['systemctl', 'stop', '3xui-dual-renew.timer', '3xui-dual-renew.service'], check=False)
    else:
        if (ROOT.exists() and not (owned_empty and not STATE.exists())) or APP.exists() or Path('/etc/x-ui').exists() or run(
                ['systemctl', 'cat', 'x-ui'], check=False).returncode == 0:
            raise RuntimeError('发现已有面板或部署目录，拒绝覆盖。本脚本中途失败可用 --resume。')
        ROOT.mkdir(mode=0o700, exist_ok=True)
        save(ROOT / 'owner', '3xui-dual-v1')
        LOG.touch(mode=0o600)
        say('安装依赖：curl、Python、OpenSSL、socat、CA 证书等……')
        run(['apt-get', 'update'], timeout=600)
        run(['apt-get', 'install', '-y', 'python3', 'curl', 'openssl', 'socat', 'tar',
             'ca-certificates', 'iproute2'], timeout=900)
        ip = server_ip()
        socks_ip = ask_socks_host()
        sp = int(ask('住宅 SOCKS5 端口'))
        if not 1 <= sp <= 65535:
            raise ValueError('SOCKS5 端口无效。')
        su = ask('住宅 SOCKS5 用户名')
        pw = ask('住宅 SOCKS5 密码（隐藏输入）', secret=True)
        for value in (su, pw):
            curl_quote(value)
            if not value or len(value.encode()) > 255:
                raise ValueError('SOCKS5 用户名和密码必须为 1–255 字节。')
        if ':' in su:
            raise ValueError('当前脚本的 curl 检测不支持含冒号的用户名。')
        email = ask('邮箱地址（申请面板 HTTPS 证书用，不需要邮箱密码）')
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            raise ValueError('邮箱格式无效。')
        target = ask('REALITY 目标域名（不需要你拥有）', 'dl.google.com').lower()
        if not re.fullmatch(r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', target) or '.' not in target:
            raise ValueError('目标必须是域名，不带协议、路径或端口。')
        dp = ask_port('服务器直连节点端口（回车使用随机默认值）', {80})
        hp = ask_port('住宅中转节点端口（回车使用随机默认值）', {80, dp})
        pp = ask_port('面板 HTTPS 端口', {80, dp, hp})
        ap = random_port({80, dp, hp, pp})
        s = {'managed_by': '3xui-dual-v1', 'version': VERSION, 'arch': arch, 'ip': ip,
             'socks_ip': socks_ip, 'socks_port': sp, 'socks_user': su, 'socks_password': pw,
             'email': email, 'target': target,
             'panel_port': pp, 'api_port': ap, 'username': 'admin_' + secrets.token_hex(4),
             'password': secrets.token_urlsafe(24), 'base': '/' + secrets.token_hex(12) + '/',
             'nodes': make_nodes(dp, hp)}
        save(STATE, s)
    shutil.copyfile(Path(__file__), ROOT / 'manager.py') if Path(__file__).resolve() != ROOT / 'manager.py' else None
    say(f"请在云安全组及已有防火墙放行 TCP：80、{s['panel_port']}、" + '、'.join(str(n['port']) for n in s['nodes']))
    if ask('上述端口已放行？输入 yes 开始/恢复部署', 'yes') != 'yes':
        raise RuntimeError('已暂停；放行后用 --resume 恢复。')
    try:
        deploy(s)
    except BaseException:
        # A half-finished or interrupted deployment is stopped; never advertise it as usable.
        run(['systemctl', 'disable', '--now', 'x-ui', '3xui-dual-socks-bridge'], check=False)
        raise


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        say('\n已中断。添加操作会尝试自动撤回；如未完成恢复，请用 --rollback-add。')
        sys.exit(130)
    except Exception as exc:
        say(f'\n未完成：{exc}')
        if '--add-residential' in sys.argv or '--rollback-add' in sys.argv:
            say('若提示添加中断，先用 --rollback-add 恢复；否则修复原因后重试 --add-residential。')
        else:
            say('修复原因后请按错误提示选择操作；安装中途失败可用 --resume。')
        sys.exit(1)
