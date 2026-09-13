"""Build the single-file installer from the canonical Python source."""
import argparse
import hashlib
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--check', action='store_true')
parser.add_argument('--sync-workspace', action='store_true')
args = parser.parse_args()
source = root / 'deploy-3xui-dual.py'
installer = root / '3xui-residential-relay.sh'
legacy_installer = root / 'deploy-3xui-dual.sh'
version = re.search(r"^SCRIPT_VERSION = '([^']+)'", source.read_text(), re.M).group(1)
if 'relay ' + version + ' / 3X-UI' not in (root / 'scripts/launcher.sh').read_text():
    raise SystemExit('Launcher version differs from Python source')
content = (root / 'scripts/launcher.sh').read_text() + source.read_text().rstrip() + '\n\nPYTHON_3XUI_DUAL_EOF\npython3 "$script_tmp" "$@"\n'
if args.check:
    if any(not p.exists() or p.read_text() != content for p in (installer, legacy_installer)):
        raise SystemExit('Installer is stale: run python3 scripts/build.py')
else:
    installer.write_text(content)
    legacy_installer.write_text(content)
    files = [source, installer, legacy_installer, root / 'uninstall-3xui-relay.sh']
    (root / 'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in files))
    if args.sync_workspace:
        for p in files:
            (root.parent / p.name).write_bytes(p.read_bytes())
print('Installer source consistency: OK')
