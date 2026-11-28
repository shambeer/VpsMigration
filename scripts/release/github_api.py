import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request



def credentials():
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        return token
    environment = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
    response = subprocess.run(['git', 'credential', 'fill'],
        input='protocol=https\nhost=github.com\npath=shambeer/VpsMigration.git\n\n',
        text=True, capture_output=True, env=environment, timeout=45)
    values = dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
    if response.returncode or not values.get('password'):
        raise RuntimeError('Нет доступной сохранённой авторизации GitHub; секреты не выводились')
    return values['password']


def api(token, method, url, value=None, binary=None):
    if urllib.parse.urlsplit(url).hostname not in ['api.github.com', 'uploads.github.com']:
        raise RuntimeError('Неожиданный адрес API')
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'VpsMigration-release-publisher'}
    data = binary
    if value is not None:
        data = json.dumps(value).encode()
        headers['Content-Type'] = 'application/json'
    elif binary is not None:
        headers['Content-Type'] = 'application/octet-stream'
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=60) as response:
        content = response.read()
    return json.loads(content) if content else None

