# 3x-ui 双节点一键部署

## 一条命令安装

在全新 Ubuntu 22.04+ / Debian 12+ 服务器的 root SSH 终端运行：

```bash
curl -fL 'https://raw.githubusercontent.com/Didushan/3xui-dual-node/main/deploy-3xui-dual.sh' -o /root/deploy-3xui-dual.sh && bash /root/deploy-3xui-dual.sh
```

本仓库只包含通用代码。住宅代理信息在服务器上交互输入，面板和节点凭据在服务器生成。实际服务器部署和住宅链路需运行时验收；本地测试不代表已完成生产环境验证。


最终创建两条 **VLESS + REALITY + TCP + XTLS Vision** 节点：

| 节点 | 路径 | 最终出口 |
|---|---|---|
| 服务器直连 | 客户端 → VPS → 网站 | VPS IPv4 |
| 住宅 IP 中转 | 客户端 → VPS → 本机 TCP 转发 → 住宅 SOCKS5 → 网站 | 住宅代理出口 |

面板使用 **服务器公网 IPv4 + 受信任 HTTPS 证书**。你不需要购买域名。

## 使用前

- 仅支持全新 **Ubuntu 22.04+ / Debian 12+、systemd、x86_64 或 ARM64** 服务器。
- 必须有公网 IPv4；暂不支持仅 IPv6 或需要端口映射的 NAT VPS。
- 已装 3x-ui/x-ui 时拒绝覆盖。不会卸载已有软件、清空防火墙或改变 SSH 配置。
- 准备 SOCKS5 入口 IPv4、端口、用户名、密码，以及证书 ACME 账户邮箱。脚本不会上传住宅密码到第三方服务；认证时会发送给住宅代理供应商。
- REALITY 默认目标为 `dl.google.com`，可在终端修改。它不需要属于你。脚本会验证 TLS 1.3、h2 和目标证书，再执行实际节点握手测试；这些检查不能保证目标长期可用。
- 为避免升级破坏配置，本脚本固定使用 **3x-ui v3.7.0**，校验官方发行包 SHA-256。acme.sh 源码也固定到已核对的提交。没有自动更新面板或 Xray。

## 运行方法

**只需上传 `deploy-3xui-dual.sh` 一个文件**，不必上传 Python 源文件。

1. 用你的 SSH/SFTP 工具，把本机的 `deploy-3xui-dual.sh` 上传到服务器 `/root/deploy-3xui-dual.sh`。
2. 在服务器 SSH 终端执行：

```bash
bash /root/deploy-3xui-dual.sh
```

3. 按提示填写信息。住宅密码隐藏输入。面板用户名、密码、随机路径和两条节点 UUID 自动生成。
4. 放行云安全组及服务器已有防火墙端口：**TCP 80、节点端口（默认 443 和 8443）、面板端口（终端中显示）**。TCP 80 需长期允许 ACME 定期验证，平时不运行网站或 HTTP 面板。
5. 输入 `yes` 后部署。已启用的 UFW 会添加这些端口的允许规则；自定义 nftables/iptables 和云安全组仍需你自行放行。不会通过关闭整个防火墙解决问题。

非 root 用户请先通过 `sudo -i` 切换到 root。不要在 Mac 本机执行安装脚本。

## 安装结果和文件

只有配置、证书验证及服务器本机协议测试全部通过，脚本才会输出最终成功信息和两条节点链接。

服务器上的结果路径：

```text
/root/3xui-dual/登录信息与两个节点.txt
```

其中包含面板完整 HTTPS 地址、用户名、密码、两条 `vless://` 链接。该文件权限为 `600`，可以通过 SFTP 下载保存；不要在视频录制时展示真实凭据。

其他服务器路径：

| 文件 | 内容 |
|---|---|
| `/root/3xui-dual/state.json` | 私有部署状态，含住宅密码、节点私钥等 |
| `/root/3xui-dual/test-result.json` | 最近一次出口检查结果 |
| `/root/3xui-dual/routing-recovery.json` | 初始正确路由配置，含住宅密码 |
| `/root/3xui-dual/install.log` | 失败诊断日志，可能含敏感内容 |
| `/root/3xui-dual/probe.log` | 临时 Xray 客户端检查日志 |
| `/root/3xui-dual/cert/` | HTTPS 证书和私钥 |
| `/etc/x-ui/x-ui.db` | 面板 SQLite 数据库 |
| `/usr/local/x-ui/` | 官方面板和 Xray 程序 |

`/root/3xui-dual` 和 `/etc/x-ui` 只允许 root 访问。请备份数据库及私有部署文件。

## 故障隔离与检查

- 两个节点使用不同端口、UUID 和入口标签。
- 默认未匹配流量阻断；住宅 TCP 入口固定走 SOCKS5，住宅 UDP 阻断。
- 不添加负载均衡、自动切换和直连兜底。
- 住宅节点测试使用 `socks5h`，域名交给住宅代理解析；服务器路由采用 `AsIs`，关闭嗅探重写。
- 为兼容新版 Xray 对公网明文 SOCKS 出站的限制，使用 **socat 仅监听 127.0.0.1 的 TCP 转发服务**。Xray 仍负责向供应商发送 SOCKS5 用户名密码。此服务不是第三条对外节点，也不对公网开放。
- 安装验收会临时把住宅上游指向本机不可连接端口，验证住宅请求失败、直连节点正常，并在 `finally` 中恢复。若恢复失败或安装被中断，面板/Xray 与转发服务停止，不输出成功结果。强制断电或 `kill -9` 无法执行清理，此时用 `--resume` 恢复原配置后重测。

部署结束后检查：

```bash
python3 /root/3xui-dual/manager.py --check
```

此命令不注入故障，会实际连接两个 VLESS/REALITY 入口并检查出口。路由被手动修改时，它可能拒绝验收；它不会把你的修改自动改回去。

**本机协议测试不能证明公网入口已放行。**最后必须用自己的电脑/手机分别导入两条链接，访问 IP 查询网站验证出口。使用支持的较新客户端，关闭 Mux。住宅模式应选全局代理或明确覆盖目标应用，配置远端 DNS/DoH；客户端自己的直连规则、本地 DNS、未接管的应用流量不受服务器脚本控制。

固定/粘性住宅 IP 会检查两次出口相同。轮换型住宅代理可在输入时选择 `y`；这时不能靠两次 IP 相同判断，需要结合路由检查和故障注入结果。脚本只验证出口地址，不验证地理位置、ASN 或“住宅”标签真实性。

## HTTPS 自动续期

IP 证书约 6 天有效。脚本把续期周期设为约 2 天，每 12 小时运行检查（含随机延迟），重启面板使证书生效，并检查证书是否至少还剩 48 小时。ACME 服务端的续期建议也可能影响实际续期时间。

```bash
systemctl status 3xui-dual-renew.timer
journalctl -u 3xui-dual-renew.service --no-pager -n 60
systemctl start 3xui-dual-renew.service
```

失败会让 systemd 单元标记失败并写入日志，不会自动发邮件或外部通知。续期会短暂重启面板及其 Xray 进程；节点可能短暂重连。不要把 TCP 80 占用或关闭，否则后续续期可能失败。

## 安装失败后

先按错误提示检查云安全组、端口占用、SOCKS5 凭据、REALITY 目标以及日志。修复后运行：

```bash
bash /root/deploy-3xui-dual.sh --resume
```

`--resume` 仅用于本脚本尚未完成的部署，会重用已生成的凭据、重建本脚本管理的配置，不重复创建节点。成功部署后拒绝 `--resume`，避免覆盖你在面板中的后续修改。需要改输入内容时，可先用 root 编辑私有 `state.json` 再恢复；保持 JSON 格式正确。不要为了重试直接删除数据库。

常用服务检查：

```bash
systemctl status x-ui
systemctl status 3xui-dual-socks-bridge
journalctl -u x-ui --no-pager -n 80
```

## 实际限制

- 普通 SOCKS5 不为 VPS 到住宅代理之间的连接增加加密。REALITY 保护客户端到 VPS 这一段；访问 HTTPS 网站时，网站自身的 TLS 仍然保护内容。
- 住宅入口仅支持 TCP，语音、游戏等依赖 UDP 的功能可能无法使用；浏览器 QUIC 通常会回退 TCP。
- 每个中转连接消耗 VPS 和住宅代理的流量，也增加一段链路；速度由线路和供应商共同决定。
- VPS 公网 IP 变化时需要更新节点链接并重新申请对应 IP 证书。
- 默认关闭面板的独立订阅服务，本版本提供两条节点链接。以后启用订阅时需另外配置 HTTPS。
- 已核对的新版 Xray 默认要求 REALITY 客户端版本至少 v26.3.27。优先使用近期 v2rayN/v2rayNG 及配套 Xray；其他内核客户端需要实际验证兼容性，不能仅凭它标称支持 REALITY 判断。本脚本保留内核默认要求。
- 面板采用官方发行包加 systemd 安装；本脚本未安装交互式 `x-ui` 菜单。日常管理通过网页与上述 systemctl 命令。

## 核对依据

- [3x-ui v3.7.0 官方发行包](https://github.com/MHSanaei/3x-ui/releases/tag/v3.7.0)
- [IP 证书和短期证书配置](https://github.com/MHSanaei/3x-ui/blob/v3.7.0/docs/content/docs/en/config/ssl-certificates.mdx)
- [3x-ui 配置接口及公网明文出站校验](https://github.com/MHSanaei/3x-ui/blob/v3.7.0/internal/web/service/xray_setting.go)
- [REALITY 官方说明](https://github.com/XTLS/REALITY)
- [acme.sh 固定源码版本](https://github.com/acmesh-official/acme.sh/tree/181425b3c8373ca23c0664948b97edf5ed84e9c5)

本地已验证内容见 `验证记录.txt`。没有你的服务器会话和实际住宅凭据，本地检查不等于已完成远程部署；远程证书申请及真实住宅出口验收由脚本运行时执行。
