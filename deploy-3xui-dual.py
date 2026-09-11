#!/usr/bin/env python3
"""Fresh Ubuntu/Debian deployment; embedded in deploy-3xui-dual.sh."""
import argparse
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
        body = urllib.parse.urlencode(data).encode() if data is not None else None
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
                'shortIds': [item['sid']],
                'settings': {'publicKey': item['public'], 'fingerprint': 'chrome',
                             'serverName': s['target'], 'spiderX': '/'}}}),
        # Disabled sniffing preserves destination hostnames for residential-side DNS.
        'sniffing': json.dumps({'enabled': False, 'destOverride': [], 'routeOnly': True}),
    }


def template(s):
    # Explicit default-deny: no unmatched request can fall through to freedom.
    return {
        'log': {'access': 'none', 'loglevel': 'warning'},
        'api': {'tag': 'api', 'services': ['HandlerService', 'LoggerService', 'StatsService', 'RoutingService']},
        'inbounds': [{'tag': 'api', 'listen': '127.0.0.1', 'port': s['api_port'],
                      'protocol': 'tunnel', 'settings': {'rewriteAddress': '127.0.0.1'}}],
        'outbounds': [
            {'tag': 'blocked', 'protocol': 'blackhole', 'settings': {}},
            {'tag': 'server-out', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4',
                'finalRules': [{'action': 'block', 'ip': ['geoip:private']}, {'action': 'allow'}]}},
            {'tag': 'residential-out', 'protocol': 'socks', 'settings': {'servers': [
                {'address': '127.0.0.1', 'port': s['bridge_port'],
                 'users': [{'user': s['socks_user'], 'pass': s['socks_password']}]}]}},
        ],
        'routing': {'domainStrategy': 'AsIs', 'rules': [
            {'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'},
            {'type': 'field', 'inboundTag': [HOME_TAG], 'network': 'udp', 'outboundTag': 'blocked'},
            {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'blocked'},
            {'type': 'field', 'inboundTag': [HOME_TAG], 'network': 'tcp', 'outboundTag': 'residential-out'},
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
    value = api.request('panel/api/xray/', {})
    if isinstance(value, str):
        value = json.loads(value)
    return value['xraySetting']


def validate_routes(value):
    out = value.get('outbounds', [])
    if not out or out[0].get('protocol') != 'blackhole':
        raise RuntimeError('默认出口不再是阻断，停止验收。')
    rules = value.get('routing', {}).get('rules', [])
    expected = [('udp', 'blocked'), ('tcp', 'residential-out')]
    actual = [(r.get('network'), r.get('outboundTag')) for r in rules if HOME_TAG in r.get('inboundTag', [])]
    if actual != expected:
        raise RuntimeError('住宅路由已改变，请检查 UDP 阻断及 TCP 固定出口。')
    if value.get('routing', {}).get('domainStrategy') != 'AsIs':
        raise RuntimeError('路由 DNS 策略已改变。')
    if any('balancerTag' in r for r in rules) or value.get('routing', {}).get('balancers'):
        raise RuntimeError('出现负载均衡/回退配置，停止验收。')
    expected_rules = [
        {'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'},
        {'type': 'field', 'inboundTag': [HOME_TAG], 'network': 'udp', 'outboundTag': 'blocked'},
        {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'blocked'},
        {'type': 'field', 'inboundTag': [HOME_TAG], 'network': 'tcp', 'outboundTag': 'residential-out'},
        {'type': 'field', 'inboundTag': [DIRECT_TAG], 'outboundTag': 'server-out'},
    ]
    if rules != expected_rules:
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
                                'protocol': 'socks', 'settings': {'auth': 'noauth', 'udp': False}})
        cfg['outbounds'].append({'tag': n['tag'], 'protocol': 'vless',
            'settings': {'vnext': [{'address': '127.0.0.1', 'port': n['port'],
                'users': [{'id': n['uuid'], 'encryption': 'none', 'flow': 'xtls-rprx-vision'}]}]},
            'streamSettings': {'network': 'tcp', 'security': 'reality',
                'realitySettings': {'serverName': s['target'], 'fingerprint': 'chrome',
                                   'password': n['public'], 'shortId': n['sid'], 'spiderX': '/'}}})
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
    if len(servers) != 1 or servers[0]['address'] != '127.0.0.1' or servers[0]['port'] != s['bridge_port']:
        raise RuntimeError('住宅出口不再指向本脚本的本机转发服务。')
    run(['systemctl', 'is-active', '--quiet', '3xui-dual-socks-bridge'])
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
              'upstream_exit': upstream, 'failure_test_passed': failure,
              'tested_at': time.strftime('%Y-%m-%d %H:%M:%S %z'),
              'scope': 'server-local real protocol test; external client connectivity still requires checking'}
    save(ROOT / 'test-result.json', result)
    say(f'出口检查通过：服务器 {server_ip}；住宅 {home_ip}')
    return result


def credentials(s):
    lines = [f'3x-ui {VERSION}', f"面板：https://{s['ip']}:{s['panel_port']}{s['base']}",
             f"用户名：{s['username']}", f"密码：{s['password']}", '',
             '以下链接包含节点凭据，请勿公开：']
    for n in s['nodes']:
        query = urllib.parse.urlencode({'encryption': 'none', 'security': 'reality',
            'sni': s['target'], 'fp': 'chrome', 'pbk': n['public'], 'sid': n['sid'],
            'type': 'tcp', 'flow': 'xtls-rprx-vision', 'spx': '/'})
        lines += ['', n['name'], f"vless://{n['uuid']}@{s['ip']}:{n['port']}?{query}#{urllib.parse.quote(n['name'])}"]
    lines += ['', '住宅入口仅转发 TCP；UDP 阻断，不会回退到服务器出口。',
              '客户端请关闭 Mux；住宅节点使用远端 DNS/DoH，避免本地 DNS 和分流绕过。',
              '还需从你的电脑/手机导入测试；服务器回环测试不证明云防火墙已放行。',
              '检查：python3 /root/3xui-dual/manager.py --check',
              '证书续期：systemctl status 3xui-dual-renew.timer',
              '续期日志：journalctl -u 3xui-dual-renew.service --no-pager -n 60',
              '敏感文件及数据库均只应由 root 读取。']
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
    available_ports([80, s['panel_port'], s['api_port'], s['bridge_port']] + [n['port'] for n in s['nodes']])
    # Manage only UFW if it is already active; never flush firewall rules or change SSH rules.
    if shutil.which('firewall-cmd') and run(['firewall-cmd', '--state'], check=False).returncode == 0:
        raise RuntimeError('检测到 firewalld，请先手动放行所列端口，再处理防火墙适配；脚本不会替换防火墙。')
    if shutil.which('ufw') and 'Status: active' in run(['ufw', 'status'], check=False).stdout:
        for p in [80, s['panel_port']] + [n['port'] for n in s['nodes']]:
            run(['ufw', 'allow', str(p) + '/tcp'])
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
Wants=network-online.target 3xui-dual-socks-bridge.service
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
    save('/etc/systemd/system/3xui-dual-socks-bridge.service', f'''[Unit]
Description=Loopback TCP relay to residential SOCKS5 provider
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
DynamicUser=yes
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
RestrictAddressFamilies=AF_INET
ExecStart=/usr/bin/socat TCP4-LISTEN:{s['bridge_port']},bind=127.0.0.1,reuseaddr,fork TCP4:{s['socks_ip']}:{s['socks_port']},connect-timeout=10
Restart=on-failure
RestartSec=3
LimitNOFILE=65536
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
    # Install fail-closed routing BEFORE creating any public inbound.
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
    run(['systemctl', 'enable', '--now', '3xui-dual-socks-bridge'])
    wait_panel(s, True)
    run(['systemctl', 'start', '3xui-dual-renew.service'], timeout=650)
    s['complete'] = True
    save(STATE, s)
    save(ROOT / '登录信息与两个节点.txt', credentials(s))
    say('\n部署及服务器本机协议测试通过。面板证书已验证，续期任务已启用。')
    say(credentials(s))
    say(f'结果已保存：{ROOT}/登录信息与两个节点.txt')


def main():
    parser = argparse.ArgumentParser(description='3x-ui 双节点安装（全新服务器）')
    parser.add_argument('--resume', action='store_true', help='仅恢复本脚本未完成的部署；重新应用其配置')
    parser.add_argument('--check', action='store_true', help='只检查本脚本部署；不注入故障')
    args = parser.parse_args()
    if args.resume and args.check:
        parser.error('--resume 和 --check 不能同时使用')
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
    if args.resume or args.check:
        s = json.loads(STATE.read_text())
        if s.get('managed_by') != '3xui-dual-v1' or s['arch'] != arch:
            raise RuntimeError('不属于本脚本管理的部署。')
        # Keep the legacy state key for --resume compatibility; its value may now be a hostname.
        s['socks_ip'] = socks_host(s['socks_ip'])
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
        email = ask('证书 ACME 账户邮箱')
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            raise ValueError('邮箱格式无效。')
        target = ask('REALITY 目标域名（不需要你拥有）', 'dl.google.com').lower()
        if not re.fullmatch(r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', target) or '.' not in target:
            raise ValueError('目标必须是域名，不带协议、路径或端口。')
        dp = ask_port('服务器直连节点端口（回车使用随机默认值）', {80})
        hp = ask_port('住宅中转节点端口（回车使用随机默认值）', {80, dp})
        pp = ask_port('面板 HTTPS 端口', {80, dp, hp})
        ap = random_port({80, dp, hp, pp})
        bp = random_port({80, dp, hp, pp, ap})
        s = {'managed_by': '3xui-dual-v1', 'version': VERSION, 'arch': arch, 'ip': ip,
             'socks_ip': socks_ip, 'socks_port': sp, 'socks_user': su, 'socks_password': pw,
             'email': email, 'target': target,
             'panel_port': pp, 'api_port': ap, 'bridge_port': bp, 'username': 'admin_' + secrets.token_hex(4),
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
        say('\n已中断。部署未完成时服务已停止。')
        sys.exit(130)
    except Exception as exc:
        say(f'\n未完成：{exc}')
        say('修复原因后可运行原脚本 --resume；不要删除已有数据库来强行重装。')
        sys.exit(1)
