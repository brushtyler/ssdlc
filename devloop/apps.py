"""GitHub App identities: one app per agent and one for the dispatcher.

Each actor (``dispatcher`` or an agent key) may have a GitHub App. Its installation tokens are
minted per action with only the permissions that action needs, so a reviewer never holds a
token that can push. An actor without an app falls back to the ambient ``gh`` login, as before.

Tokens stay in this process: they reach ``gh`` and ``git`` through the environment of a single
command and are never given to the agent CLIs. Only the standard library and the ``openssl``
binary are used (RS256 is not in the standard library).
"""

import base64
import calendar
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .github import GitHub, GitHubError

API = 'https://api.github.com'
DISPATCHER = 'dispatcher'

# Permissions of the token minted for each kind of action (metadata: read is always implied).
IMPLEMENT = {'contents': 'write', 'pull_requests': 'write'}
REVIEW = {'contents': 'read', 'pull_requests': 'write'}
PR_COMMENT = {'pull_requests': 'write'}
ISSUE_COMMENT = {'issues': 'write'}

_REFRESH_MARGIN = 300  # seconds of validity a cached token must still have


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def sign_rs256(key_path, data, run=subprocess.run):
    try:
        done = run(['openssl', 'dgst', '-sha256', '-sign', str(key_path)], input=data,
                   capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitHubError(f'openssl could not sign the app JWT: {exc}') from exc
    if done.returncode != 0:
        raise GitHubError(f'openssl could not sign the app JWT: {done.stderr.decode()[:300]}')
    return done.stdout


def http_json(method, path, auth, body=None, timeout=30):
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': auth, 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'devloop'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors='replace')[:300]
        raise GitHubError(f'{method} {path}: HTTP {exc.code}: {detail}') from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GitHubError(f'{method} {path}: {exc}') from exc
    return json.loads(raw) if raw else None


def find_key(key_dir, slug):
    """The newest private key GitHub generated for ``slug`` (``<slug>.<date>.private-key.pem``).

    The key must not be readable by group or others."""
    keys = sorted(Path(key_dir).expanduser().glob(f'{slug}.*.private-key.pem'))
    if not keys:
        raise FileNotFoundError(f'no private key for {slug} in {key_dir}')
    key = keys[-1]
    if key.stat().st_mode & 0o077:
        raise PermissionError(f'{key} is readable by group or others (chmod 600)')
    return key


class AppAuth:
    """One GitHub App installed on the repository."""

    def __init__(self, app_id, slug, key_path, repo, *, http=http_json, sign=sign_rs256,
                 clock=time.time):
        self.app_id = app_id
        self.slug = slug
        self.key_path = key_path
        self.repo = repo
        self._http = http
        self._sign = sign
        self._clock = clock
        self._installation = None
        self._user_id = None
        self._tokens = {}

    @property
    def login(self):
        return f'{self.slug}[bot]'

    def jwt(self):
        now = int(self._clock())
        head = _b64(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
        claims = _b64(json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': str(self.app_id)}).encode())
        signing_input = f'{head}.{claims}'
        return f'{signing_input}.{_b64(self._sign(self.key_path, signing_input.encode()))}'

    def installation_id(self):
        if self._installation is None:
            found = self._http('GET', f'/repos/{self.repo}/installation', f'Bearer {self.jwt()}')
            if found['app_id'] != self.app_id:
                raise GitHubError(f'installation {found["id"]} belongs to app {found["app_id"]}, '
                                  f'not {self.app_id}')
            self._installation = found['id']
        return self._installation

    def token(self, permissions=None):
        """An installation token for this repository only, limited to ``permissions`` (all the
        installation's permissions when None). Reused while it has a few minutes left."""
        key = tuple(sorted((permissions or {}).items()))
        cached = self._tokens.get(key)
        if cached and cached[1] - self._clock() > _REFRESH_MARGIN:
            return cached[0]
        body = {'repositories': [self.repo.split('/', 1)[1]]}
        if permissions:
            body['permissions'] = dict(permissions)
        minted = self._http('POST', f'/app/installations/{self.installation_id()}/access_tokens',
                            f'Bearer {self.jwt()}', body)
        expires = calendar.timegm(time.strptime(minted['expires_at'], '%Y-%m-%dT%H:%M:%SZ'))
        self._tokens[key] = (minted['token'], expires)
        return minted['token']

    def user_id(self, token):
        """The id of the app's bot user, for its noreply commit address (read with ``token``)."""
        if self._user_id is None:
            user = self._http('GET', f'/users/{self.login}', f'token {token}')
            self._user_id = user['id']
        return self._user_id

    def describe(self):
        """What GitHub says about the app and its installation, for ``devloop apps``."""
        app = self._http('GET', '/app', f'Bearer {self.jwt()}')
        if app['slug'] != self.slug:
            raise GitHubError(f'app {self.app_id} is {app["slug"]!r}, configured as {self.slug!r}')
        inst = self._http('GET', f'/app/installations/{self.installation_id()}', f'Bearer {self.jwt()}')
        return {'login': self.login, 'installation': inst['id'],
                'repository_selection': inst['repository_selection'],
                'permissions': inst['permissions']}


class Identities:
    """Who acts on GitHub for each actor, with an app or with the ambient ``gh`` login."""

    def __init__(self, repo, apps, run=subprocess.run):
        self.repo = repo
        self.apps = dict(apps)
        self._run = run
        self._ambient = GitHub(repo, run=run)

    @classmethod
    def from_config(cls, cfg, run=subprocess.run):
        key_dir = os.environ.get('DEVLOOP_APP_KEY_DIR') or cfg.app_key_dir
        apps = {actor: AppAuth(app.app_id, app.slug, find_key(key_dir, app.slug), cfg.repo)
                for actor, app in cfg.apps.items()}
        return cls(cfg.repo, apps, run=run)

    def github(self, actor, permissions=None):
        app = self.apps.get(actor)
        if app is None:
            return self._ambient
        return GitHub(self.repo, run=self._run, token=lambda: app.token(permissions))

    def login(self, actor):
        """The app's bot login, or None when the actor uses the ambient login."""
        app = self.apps.get(actor)
        return app.login if app else None

    def agent_logins(self):
        return {app.login for actor, app in self.apps.items() if actor != DISPATCHER}

    def git_auth(self, actor, permissions):
        """Commit identity and push credentials for ``actor``, or None for the ambient ones."""
        app = self.apps.get(actor)
        if app is None:
            return None
        token = app.token(permissions)
        return {'name': app.login, 'email': f'{app.user_id(token)}+{app.login}@users.noreply.github.com',
                'url': f'https://github.com/{self.repo}.git', 'token': token}
