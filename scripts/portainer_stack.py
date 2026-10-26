#!/usr/bin/env python3
"""Create / redeploy / inspect the live-clip stack on Portainer (git-backed stack).

    python3 scripts/portainer_stack.py create            # from .env.portainer
    python3 scripts/portainer_stack.py redeploy sha-abc1234   # env synced from .env.portainer
    python3 scripts/portainer_stack.py wait sha-abc1234   # after a redeploy whose request timed out
    python3 scripts/portainer_stack.py status
    python3 scripts/portainer_stack.py logs [mediamtx|web] [N]

Reads PORTAINER_URL / PORTAINER_API_KEY / PORTAINER_ENDPOINT_ID from ~/.hermes/scripts/probook.env.
Exits non-zero unless the containers really run the requested image (a redeploy that keeps the
old IMAGE_TAG returns 200 and changes nothing; that bit us on probook).
"""
import json, os, sys, time, urllib.parse, urllib.request

STACK, REPO, COMPOSE = 'live-clip', 'https://github.com/narasitk77/live-clip', 'docker-compose.portainer.yml'
SECRET_KEYS = {'SRT_PASSPHRASE', 'WEB_PASSWORD', 'MEDIAMTX_API_PASS', 'S3_SECRET_KEY', 'S3_ACCESS_KEY'}


def read_env(path):
    out = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out


CFG = read_env(os.path.expanduser('~/.hermes/scripts/probook.env'))
URL = CFG['PORTAINER_URL'].rstrip('/')
EP = CFG.get('PORTAINER_ENDPOINT_ID', '2')
HDR = {'X-API-Key': CFG['PORTAINER_API_KEY'], 'Content-Type': 'application/json'}


def api(method, path, body=None, timeout=60):
    req = urllib.request.Request(URL + path, method=method, headers=HDR,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        sys.exit(f'{method} {path} -> HTTP {e.code}: {e.read().decode()[:400]}')


def find_stack():
    return next((s for s in api('GET', '/api/stacks') if s['Name'] == STACK), None)


def containers():
    cs = api('GET', f'/api/endpoints/{EP}/docker/containers/json?all=1')
    return [c for c in cs if c.get('Labels', {}).get('com.docker.compose.project') == STACK]


def wait_running(expect_tag=None, timeout=420):
    t0 = time.time()
    while time.time() - t0 < timeout:
        cs = containers()
        names = {c['Names'][0].lstrip('/'): c for c in cs}
        ok = all(n in names and names[n]['State'] == 'running' for n in ('live-clip-mediamtx', 'live-clip-web'))
        if ok and expect_tag:
            ok = all(names[n]['Image'].endswith(':' + expect_tag) for n in ('live-clip-mediamtx', 'live-clip-web'))
        if ok:
            health = names['live-clip-web']['Status']
            if 'health' not in health or 'healthy' in health and 'unhealthy' not in health:
                return names
        time.sleep(10)
    sys.exit(f'TIMEOUT: containers not running the expected image after {timeout}s: '
             + json.dumps({n: (c['Image'], c['Status']) for n, c in {c['Names'][0]: c for c in containers()}.items()}))


def show(names):
    for n, c in sorted(names.items()):
        print(f'  {n:22} {c["Image"]:60} {c["Status"]}')


def cmd_create():
    env = read_env('.env.portainer')
    missing = [k for k in ('SRT_PASSPHRASE', 'MEDIAMTX_API_PASS', 'WEB_USER', 'WEB_PASSWORD', 'SRT_PUBLIC_HOST') if not env.get(k)]
    if missing:
        sys.exit('missing in .env.portainer: ' + ', '.join(missing))
    if find_stack():
        sys.exit(f'stack {STACK} already exists; use redeploy')
    body = {'name': STACK, 'repositoryURL': REPO, 'repositoryReferenceName': 'refs/heads/main',
            'composeFile': COMPOSE, 'repositoryAuthentication': False, 'tlsskipVerify': False,
            'env': [{'name': k, 'value': v} for k, v in env.items()]}
    s = api('POST', f'/api/stacks/create/standalone/repository?endpointId={EP}', body, timeout=600)
    print('stack created: id', s['Id'])
    show(wait_running(env.get('IMAGE_TAG') or None))


def cmd_redeploy(tag):
    """Redeploy with IMAGE_TAG=tag. Env comes from .env.portainer when that file exists (the
    single source of truth on the ops machine), otherwise the stack's current env is kept."""
    s = find_stack() or sys.exit('stack not found')
    env = read_env('.env.portainer') if os.path.exists('.env.portainer') else \
        {e['name']: e['value'] for e in s.get('Env') or []}
    env['IMAGE_TAG'] = tag
    body = {'env': [{'name': k, 'value': v} for k, v in env.items()], 'prune': False, 'pullImage': True,
            'repositoryReferenceName': 'refs/heads/main', 'repositoryAuthentication': False}
    try:
        api('PUT', f'/api/stacks/{s["Id"]}/git/redeploy?endpointId={EP}', body, timeout=120)
    except (SystemExit, OSError) as e:  # a client timeout while Portainer keeps pulling is normal
        print('redeploy request ended early (Portainer continues on its own; NOT re-sending):', e)
    print('waiting for both containers to run', tag)
    show(wait_running(tag))
    s2 = api('GET', f'/api/stacks/{s["Id"]}')
    got = {e['name']: e['value'] for e in s2.get('Env') or []}.get('IMAGE_TAG')
    if got != tag:
        sys.exit(f'stack env IMAGE_TAG is {got!r}, expected {tag!r}')
    print('stack env IMAGE_TAG =', got)


def cmd_wait(tag):
    """Wait until both containers run `tag` (use after a redeploy whose request timed out)."""
    show(wait_running(tag))


def cmd_status():
    s = find_stack() or sys.exit('stack not found')
    print('stack', s['Id'], s['Name'], '| status', s.get('Status'))
    for e in sorted(s.get('Env') or [], key=lambda e: e['name']):
        v = e['value']
        print(f'  {e["name"]}={"***" if e["name"] in SECRET_KEYS and v else v}')
    show({c['Names'][0].lstrip('/'): c for c in containers()})


def cmd_logs(which='mediamtx', n='40'):
    name = f'live-clip-{which}'
    req = urllib.request.Request(f'{URL}/api/endpoints/{EP}/docker/containers/{name}/logs?stdout=1&stderr=1&tail={n}',
                                 headers=HDR)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    # docker multiplexed stream: 8-byte header per frame
    out, i = [], 0
    while i + 8 <= len(raw):
        ln = int.from_bytes(raw[i + 4:i + 8], 'big')
        out.append(raw[i + 8:i + 8 + ln].decode(errors='replace'))
        i += 8 + ln
    print(''.join(out) if out else raw.decode(errors='replace'))


if __name__ == '__main__':
    a = sys.argv[1:] or ['status']
    {'create': lambda: cmd_create(), 'redeploy': lambda: cmd_redeploy(a[1]), 'wait': lambda: cmd_wait(a[1]), 'status': cmd_status,
     'logs': lambda: cmd_logs(*a[1:])}[a[0]]()
