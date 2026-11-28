import argparse
import hashlib
import json
from pathlib import Path
import re
import urllib.error

from github_api import api, credentials

ROOT = Path(__file__).resolve().parents[2]
SOURCE_NAMES = ['build_portable.py', 'migrate.py', 'remote.py', 'deploy.ps1', 'deploy.sh',
                'PUBLIC_README.md', 'README.md', 'tests/test_migration.py', 'tests/test_relay.py', 'tests/test_selfsteal.py', 'tests/test_security.py', 'tests/test_os_updates.py', 'tests/test_cleanup.py', 'tests/test_maintenance_cli.py', 'tests/test_resume.py', 'tests/test_selfsteal_features.py', 'selfsteal.example.json']
LEGACY_PATHS = {'scripts/migration/' + name for name in [
    'common.py', 'console.py', 'topology.py', 'relay.py', 'selfsteal.py', 'stack.py', 'snapshot.py',
    'portable.py', 'run.ps1.in', 'run.sh.in']}



def public_sources():
    sources = {'README.md': (ROOT / 'scripts/migration/PUBLIC_README.md').read_text(encoding='utf-8'),
               '.gitignore': (ROOT / '.gitignore').read_text(encoding='utf-8')}
    package = ROOT / 'scripts/migration/vps_migration'
    packaged = sorted(path.relative_to(ROOT / 'scripts/migration').as_posix() for path in package.rglob('*.py'))
    packaged += ['vps_migration/packaging/templates/run.ps1.in', 'vps_migration/packaging/templates/run.sh.in']
    for name in SOURCE_NAMES + packaged:
        path = 'scripts/migration/' + name
        sources[path] = (ROOT / path).read_text(encoding='utf-8')
    for name in ['run.py', 'test_runner.py', 'README.md', 'release.example.json']:
        path = 'scripts/qa/' + name
        sources[path] = (ROOT / path).read_text(encoding='utf-8')
    sources['scripts/README.md'] = (ROOT / 'scripts/README.md').read_text(encoding='utf-8')
    for name in ['github_api.py', 'publish.py', 'README.md']:
        path = 'scripts/release/' + name
        sources[path] = (ROOT / path).read_text(encoding='utf-8')
    for path in ['AGENTS.md', 'docs/agent-context.md', 'docs/README.md', 'config/README.md', 'images/README.md', 'docs/functional-requirements.md', 'docs/ports.md', 'docs/reports/guide-coverage-20261128.md', 'docs/reports/sub-store-compatibility-20261128.md', 'docs/reports/linux-audit-20261128.md', 'docs/reports/verified-status-20261128.md']:
        sources[path] = (ROOT / path).read_text(encoding='utf-8')
    for path, content in sources.items():
        if re.search(r'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----|(?i:github_pat_|ghp_)[A-Za-z0-9_]{20,}', content):
            raise RuntimeError('Private server marker in ' + path)
    return sources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    sources = public_sources()
    directory = ROOT / 'dist/migration'
    release = json.loads((directory / 'release.json').read_text(encoding='utf-8'))
    if release['repository'] != 'shambeer/VpsMigration':
        raise RuntimeError('Unexpected publication target')
    if hashlib.sha256((directory / 'vps-migration.pyz').read_bytes()).hexdigest() != release['sha256']:
        raise RuntimeError('Package hash mismatch')
    assets = ['vps-migration.pyz', 'vps-migration.pyz.sha256', 'run.sh', 'run.ps1']
    if release['release_assets'] != assets:
        raise RuntimeError('Unexpected release assets')
    if not args.publish:
        print(json.dumps({'repository': release['repository'], 'source_files': sorted(sources), 'assets': assets}))
        return
    token = credentials()
    base = 'https://api.github.com/repos/' + release['repository']
    try:
        repository = api(token, 'GET', base)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        repository = api(token, 'POST', 'https://api.github.com/user/repos', {
            'name': 'VpsMigration', 'description': 'Portable VPN deployment and migration with Linux and PowerShell launchers',
            'private': False, 'auto_init': True})
    if repository['private']:
        raise RuntimeError('Target exists but is private; visibility was not changed')
    branch = repository['default_branch']
    reference = api(token, 'GET', base + '/git/ref/heads/' + branch)
    parent = reference['object']['sha']
    current = api(token, 'GET', base + '/git/commits/' + parent)
    existing = api(token, 'GET', base + '/git/trees/' + current['tree']['sha'] + '?recursive=1')
    if existing.get('truncated'):
        raise RuntimeError('Unexpected large source tree')
    unexpected = [entry['path'] for entry in existing['tree'] if entry['type'] == 'blob' and entry['path'] not in sources and entry['path'] not in LEGACY_PATHS]
    if unexpected:
        raise RuntimeError('Target contains unrelated files; not overwriting')
    entries = [{'path': path, 'mode': '100644', 'type': 'blob', 'content': content} for path, content in sources.items()]
    for entry in existing['tree']:
        if entry['type'] == 'blob' and entry['path'] in LEGACY_PATHS and entry['path'] not in sources:
            entries.append({'path': entry['path'], 'mode': '100644', 'type': 'blob', 'sha': None})
    tree = api(token, 'POST', base + '/git/trees', {'base_tree': current['tree']['sha'], 'tree': entries})
    if tree['sha'] != current['tree']['sha']:
        commit = api(token, 'POST', base + '/git/commits', {'message': 'Publish portable VPN migrator and verified launchers',
            'tree': tree['sha'], 'parents': [parent]})
        api(token, 'PATCH', base + '/git/refs/heads/' + branch, {'sha': commit['sha'], 'force': False})
        parent = commit['sha']
    try:
        current_release = api(token, 'GET', base + '/releases/tags/' + release['tag'])
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        current_release = api(token, 'POST', base + '/releases', {
            'tag_name': release['tag'], 'target_commitish': parent, 'name': 'Portable VPN migrator', 'draft': True,
            'body': 'Linux curl and Windows PowerShell launchers with pinned SHA-256 package verification.\n\n'
                    'No server credentials, backups or original repository history are included.\n\n'
                    'Local checks passed. Full deployment and the two-server relay chain on clean Linux VPSs have not yet been tested.\n\n'
                    'Package SHA-256: `' + release['sha256'] + '`'})
    present = {item['name']: item for item in current_release.get('assets', [])}
    if set(present) - set(assets):
        raise RuntimeError('Unexpected release asset')
    for name in assets:
        content = (directory / name).read_bytes()
        if name in present:
            if present[name].get('digest') != 'sha256:' + hashlib.sha256(content).hexdigest():
                raise RuntimeError('Existing release asset differs: ' + name)
            continue
        if not current_release['draft']:
            raise RuntimeError('Cannot modify published release')
        api(token, 'POST', current_release['upload_url'].split('{', 1)[0] + '?name=' + name, binary=content)
    if current_release['draft']:
        current_release = api(token, 'PATCH', base + '/releases/' + str(current_release['id']), {'draft': False})
    report = {'repository_url': repository['html_url'], 'release_url': current_release['html_url'],
              'source_commit': parent, 'tag': release['tag'], 'sha256': release['sha256'], 'assets': assets}
    pointer = ROOT / '.private-root'
    private_root = Path(pointer.read_text(encoding='utf-8').strip()) if pointer.exists() else ROOT / 'private-vpn'
    if not private_root.is_absolute():
        raise RuntimeError('Private root must be an absolute path')
    destination = private_root / 'server-settings/metadata/repository/published-public-migrator.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
