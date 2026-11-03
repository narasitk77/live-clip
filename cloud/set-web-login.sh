#!/bin/bash
# Change the login of the web page and WhistleCutter on the cloud VM, from this Mac:
#   cloud/set-web-login.sh <VM IP> [username, default tsd]
# Asks for the new password twice without echoing it; it travels to the VM over ssh stdin only
# (never in argv, never printed). Updates /opt/live-clip/.env and the sender sheet, recreates
# only the web container (stages keep streaming), proves the new login works and the old one is
# refused, then refreshes ./senders-cloud.txt. Everyone logged in must reconnect with the new login.
set -euo pipefail
ip=${1:?VM IP}; user=${2:-tsd}
[[ $user =~ ^[a-z0-9_-]{1,32}$ ]] || { echo "FAIL: username: a-z 0-9 - _ only" >&2; exit 1; }
cd "$(dirname "$0")/.."
ssh=(ssh -o BatchMode=yes root@"$ip")
[ -f ~/.ssh/liveclip_ed25519 ] && ssh=(ssh -i ~/.ssh/liveclip_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes root@"$ip")

read -rsp "new password for $user (12-64 chars: A-Z a-z 0-9 . _ @ ! % + = -): " p1; echo
read -rsp "again: " p2; echo
[ "$p1" = "$p2" ] || { echo "FAIL: the two entries differ, nothing changed" >&2; exit 1; }
# 12+ is what app.py demands to start at all; the charset keeps .env and compose interpolation literal
[[ $p1 =~ ^[A-Za-z0-9._@!%+=-]{12,64}$ ]] || { echo "FAIL: 12-64 chars from A-Z a-z 0-9 . _ @ ! % + = - only, nothing changed" >&2; exit 1; }
[[ $p1 == *change-me* ]] && { echo "FAIL: app.py refuses to start with 'change-me' in the password, nothing changed" >&2; exit 1; }

"${ssh[@]}" 'cat > /root/.set-web-login.py' <<'PY'
import os, re, subprocess, sys, time
user, pw = sys.stdin.readline().rstrip('\n'), sys.stdin.readline().rstrip('\n')
os.chdir('/opt/live-clip')
env = open('.env').read()
found = re.findall(r'^(WEB_USER|WEB_PASSWORD)=(.*)$', env, re.M)
loose = re.findall(r'^\s*(?:export\s+)?(WEB_USER|WEB_PASSWORD)\s*[:=]', env, re.M)
# compose reads the LAST line of a key; rewriting the first of two would change nothing that runs
if sorted(k for k, _ in found) != ['WEB_PASSWORD', 'WEB_USER'] or sorted(loose) != ['WEB_PASSWORD', 'WEB_USER']:
    sys.exit('FAIL: .env must have exactly one plain WEB_USER= and one WEB_PASSWORD= line, nothing changed')
old = dict(found)
for k, v in (('WEB_USER', user), ('WEB_PASSWORD', pw)):
    env = re.sub(rf'^{k}=.*$', lambda m: f'{k}={v}', env, count=1, flags=re.M)
with open('.env.tmp', 'w') as f:
    f.write(env)
os.chmod('.env.tmp', 0o600)
os.replace('.env.tmp', '.env')
sheet = '/root/live-clip-senders.txt'
text = open(sheet).read()
text, n = re.subn(r'^user .*$', lambda m: f'user {user}   password {pw}', text, count=1, flags=re.M)
if n != 1:
    print('WARN: no "user ..." line in the sender sheet; .env is changed anyway', file=sys.stderr)
open(sheet, 'w').write(text)
# --no-deps: plain `up web` also recreates mediamtx if it has any pending change, dropping both stages
subprocess.run(['docker', 'compose', 'up', '-d', '--no-deps', 'web'], check=True)

# inside the container: its env must be exactly the login just typed, which must work; the old must not
CHECK = r'''
import base64, os, sys, urllib.error, urllib.request
want, want_pw, ou, op = (sys.stdin.readline().rstrip('\n') for _ in range(4))
if (os.environ['WEB_USER'], os.environ['WEB_PASSWORD']) != (want, want_pw):
    sys.exit('the running web container does not have the new login yet')
def status(u, p):
    req = urllib.request.Request('http://127.0.0.1:8080/api/state',
                                 headers={'Authorization': 'Basic ' + base64.b64encode(f'{u}:{p}'.encode()).decode()})
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code
new = status(want, want_pw)
same = (ou, op) == (want, want_pw)
before = 401 if same else status(ou, op)
if new != 200 or before != 401:
    sys.exit(f'new login -> HTTP {new}, old login -> HTTP {before}')
print(f"web login is now {want}; the old login is refused" if not same else 'login unchanged')
'''
err = ''
for _ in range(40):
    r = subprocess.run(['docker', 'compose', 'exec', '-T', 'web', 'python3', '-c', CHECK],
                       input=f"{user}\n{pw}\n{old['WEB_USER']}\n{old['WEB_PASSWORD']}\n", capture_output=True, text=True)
    if r.returncode == 0:
        print(r.stdout.strip())
        break
    err = (r.stderr or r.stdout).strip().splitlines()[-1:] or ['?']
    time.sleep(1)
else:
    sys.exit(f'FAIL: {err[0]}  (docker compose logs web)')
PY
printf '%s\n%s\n' "$user" "$p1" | "${ssh[@]}" 'python3 /root/.set-web-login.py; rc=$?; rm -f /root/.set-web-login.py; exit $rc'
(umask 077; "${ssh[@]}" cat /root/live-clip-senders.txt > senders-cloud.txt)
echo "senders-cloud.txt refreshed. Web + WhistleCutter: user $user and the password you just typed."
