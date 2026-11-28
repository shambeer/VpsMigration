import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'private-vpn/qa'


def built_in_steps():
    steps = [{'id': 'qa_runner', 'group': 'local', 'kind': 'local',
              'command': ['{python}', '-X', 'utf8', '-m', 'unittest', 'discover',
                          '-s', 'scripts/qa', '-p', 'test_*.py'], 'timeout': 60}]
    for path in sorted((ROOT / 'scripts/migration/tests').glob('test_*.py')):
        steps.append({'id': path.stem, 'group': 'local', 'kind': 'local',
                      'command': ['{python}', '-X', 'utf8', '-m', 'unittest', 'discover',
                                  '-s', 'scripts/migration/tests', '-p', path.name], 'timeout': 180})
    steps.append({'id': 'portable', 'group': 'local', 'kind': 'local',
                  'command': ['{python}', '-X', 'utf8', 'scripts/migration/build_portable.py'], 'timeout': 120})
    return steps


def validate_steps(steps):
    if not isinstance(steps, list) or not steps:
        raise ValueError('План должен содержать непустой список steps')
    identifiers = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError('Каждый шаг должен быть объектом')
        identifier = step.get('id', '')
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', identifier) or identifier in identifiers:
            raise ValueError('Некорректный или повторный id проверки')
        if step.get('kind') not in ['local', 'server', 'mutating', 'publish']:
            raise ValueError('Некорректный kind проверки ' + identifier)
        if not isinstance(step.get('group'), str) or not step['group']:
            raise ValueError('Не задана группа ' + identifier)
        command = step.get('command')
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError('command должен быть массивом аргументов')
        timeout = step.get('timeout', 180)
        if type(timeout) is not int or not 1 <= timeout <= 7200:
            raise ValueError('Некорректный timeout')
        dependencies = step.get('depends_on', [])
        if not isinstance(dependencies, list) or any(value not in identifiers for value in dependencies):
            raise ValueError('Зависимости должны ссылаться на предыдущие проверки')
        identifiers.add(identifier)
    return steps


def signature(steps):
    digest = hashlib.sha256(json.dumps(steps, sort_keys=True).encode())
    paths = {path for path in (ROOT / 'scripts/migration').rglob('*')
             if path.is_file() and path.suffix in ['.py', '.in', '.json', '.ps1', '.sh']}
    paths.update((ROOT / 'scripts/qa').rglob('*.py'))
    paths.update((ROOT / 'scripts/release').rglob('*.py'))
    for step in steps:
        for argument in step['command']:
            candidate = Path(argument.replace('{root}', str(ROOT)))
            if not candidate.is_absolute():
                candidate = ROOT / candidate
            if candidate.is_file():
                paths.add(candidate)
    for path in sorted(paths):
        digest.update(str(path).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def run_step(step, directory):
    log = directory / (step['id'] + '.log')
    command = [part.replace('{python}', sys.executable).replace('{root}', str(ROOT)) for part in step['command']]
    started = time.monotonic()
    record = {'id': step['id'], 'status': 'failed', 'log': str(log)}
    with log.open('wb') as output:
        try:
            process = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                     timeout=step.get('timeout', 180), shell=False)
            record['exit_code'] = process.returncode
            record['status'] = 'passed' if process.returncode == 0 else 'failed'
        except subprocess.TimeoutExpired:
            record['reason'] = 'timeout'
        except OSError:
            record['reason'] = 'launch_error'
    record['seconds'] = round(time.monotonic() - started, 2)
    return record


def execute(steps, directory, allow_server=False, approved=(), previous=None):
    fingerprint = signature(steps)
    selected = steps
    prior = {}
    if previous is not None:
        if previous.get('plan_sha256') != fingerprint:
            raise ValueError('План или код изменился: нужен полный запуск выбранной группы')
        prior = {row['id']: row for row in previous['results']}
        if set(prior) != {step['id'] for step in steps}:
            raise ValueError('Неполный результат предыдущего запуска')
        selected = [step for step in steps if prior[step['id']]['status'] != 'passed']
    results = dict(prior)
    report = {'format': 1, 'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'plan_sha256': fingerprint, 'results': [], 'executed': 0}
    for step in selected:
        record = {'id': step['id'], 'status': 'blocked'}
        if step['kind'] == 'server' and not allow_server:
            record['reason'] = 'requires_allow_server'
        elif step['kind'] in ['mutating', 'publish'] and step['id'] not in approved:
            record['reason'] = 'requires_explicit_approval'
        elif any(results.get(name, {}).get('status') != 'passed' for name in step.get('depends_on', [])):
            record['reason'] = 'dependency_failed'
        else:
            record = run_step(step, directory)
            report['executed'] += 1
        results[step['id']] = record
        report['results'] = [results[item['id']] for item in steps if item['id'] in results]
        save(directory / 'summary.json', report)
    report['results'] = [results[step['id']] for step in steps]
    report['passed'] = sum(row['status'] == 'passed' for row in report['results'])
    report['failed'] = sum(row['status'] == 'failed' for row in report['results'])
    report['blocked'] = sum(row['status'] == 'blocked' for row in report['results'])
    report['reused'] = len(steps) - len(selected)
    report['success'] = report['passed'] == len(steps)
    save(directory / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description='Проверки с приватными журналами и коротким JSON-итогом')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--group', default='local')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--failed-only', type=Path, metavar='SUMMARY_JSON')
    parser.add_argument('--allow-server', action='store_true')
    parser.add_argument('--approve', action='append', default=[], metavar='STEP_ID')
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()
    try:
        steps = json.loads(args.plan.read_text(encoding='utf-8'))['steps'] if args.plan else built_in_steps()
        validate_steps(steps)
        steps = [step for step in steps if step['group'] == args.group]
        if not steps:
            raise ValueError('Группа не найдена')
        validate_steps(steps)
        if args.list:
            print(json.dumps([{'id': step['id'], 'kind': step['kind']} for step in steps]))
            return 0
        directory = args.output.resolve() / (datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        directory.mkdir(parents=True)
        previous = json.loads(args.failed_only.read_text(encoding='utf-8')) if args.failed_only else None
        report = execute(steps, directory, args.allow_server, args.approve, previous)
        print(json.dumps({key: report[key] for key in ['success', 'passed', 'failed', 'blocked', 'executed', 'reused']}
                         | {'summary': str(directory / 'summary.json')}, ensure_ascii=False))
        return 0 if report['success'] else 1
    except (ValueError, KeyError, TypeError, OSError):
        print(json.dumps({'success': False, 'error': 'Некорректный план, результат или путь; проверьте входные файлы'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
