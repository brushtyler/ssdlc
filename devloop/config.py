"""Dispatcher configuration (TOML)."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / 'config.toml'
AGENT_KINDS = ('claude', 'codex', 'agy')


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    key: str
    kind: str
    display_name: str
    model: str
    model_label: str
    effort: str
    version_cmd: tuple
    write_args: tuple = ()
    review_args: tuple = ()
    commit_trailer: str = ''


@dataclass(frozen=True)
class Labels:
    ready: str = 'agent:ready'
    working: str = 'agent:working'
    review: str = 'agent:review'
    changes: str = 'agent:changes'
    ready_to_merge: str = 'agent:ready-to-merge'
    needs_human: str = 'agent:needs-human'

    def all(self):
        return (self.ready, self.working, self.review, self.changes,
                self.ready_to_merge, self.needs_human)


@dataclass(frozen=True)
class Config:
    repo: str
    base_branch: str
    branch_prefix: str
    state_dir: str
    test_cmd: str
    language: str
    implementer: str
    reviewer: str
    agents: dict
    trusted_users: tuple
    max_open_prs: int = 2
    max_review_rounds: int = 3
    max_actions_per_tick: int = 3
    implement_test_retries: int = 1
    agent_timeout_seconds: int = 3600
    test_timeout_seconds: int = 1800
    blocked_labels: tuple = ('bloccata',)
    priority_labels: tuple = ('prerequisito',)
    labels: Labels = field(default_factory=Labels)

    def agent(self, key):
        return self.agents[key]


def _require(table, key, kind, where):
    if key not in table:
        raise ConfigError(f'{where}: missing {key!r}')
    value = table[key]
    if not isinstance(value, kind) or isinstance(value, bool) and kind is int:
        raise ConfigError(f'{where}.{key}: expected {kind.__name__}')
    return value


def _str_list(table, key, where, default=()):
    value = table.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f'{where}.{key}: expected a list of strings')
    return tuple(value)


def parse_config(data):
    agents = {}
    for key, table in (data.get('agents') or {}).items():
        where = f'agents.{key}'
        kind = _require(table, 'kind', str, where)
        if kind not in AGENT_KINDS:
            raise ConfigError(f'{where}.kind: must be one of {AGENT_KINDS}')
        agents[key] = AgentConfig(
            key=key, kind=kind,
            display_name=_require(table, 'display_name', str, where),
            model=_require(table, 'model', str, where),
            model_label=_require(table, 'model_label', str, where),
            effort=_require(table, 'effort', str, where),
            version_cmd=_str_list(table, 'version_cmd', where, (kind, '--version')),
            write_args=_str_list(table, 'write_args', where),
            review_args=_str_list(table, 'review_args', where),
            commit_trailer=table.get('commit_trailer', ''))
    roles = data.get('roles') or {}
    implementer = _require(roles, 'implementer', str, 'roles')
    reviewer = _require(roles, 'reviewer', str, 'roles')
    for role, key in (('implementer', implementer), ('reviewer', reviewer)):
        if key not in agents:
            raise ConfigError(f'roles.{role}: unknown agent {key!r}')
    if implementer == reviewer:
        raise ConfigError('roles: the reviewer must be a different agent from the implementer')
    trusted = _str_list(data, 'trusted_users', 'config')
    if not trusted:
        raise ConfigError('trusted_users: at least one GitHub login is required')
    ints = {}
    for key in ('max_open_prs', 'max_review_rounds', 'max_actions_per_tick',
                'implement_test_retries', 'agent_timeout_seconds', 'test_timeout_seconds'):
        if key in data:
            value = _require(data, key, int, 'config')
            if value < (0 if key == 'implement_test_retries' else 1):
                raise ConfigError(f'{key}: out of range')
            ints[key] = value
    return Config(
        repo=_require(data, 'repo', str, 'config'),
        base_branch=data.get('base_branch', 'main'),
        branch_prefix=data.get('branch_prefix', 'agent/issue-'),
        state_dir=data.get('state_dir', '.ssdlc/devloop'),
        test_cmd=_require(data, 'test_cmd', str, 'config'),
        language=data.get('language', 'English'),
        implementer=implementer, reviewer=reviewer, agents=agents,
        trusted_users=trusted,
        blocked_labels=_str_list(data, 'blocked_labels', 'config', ('bloccata',)),
        priority_labels=_str_list(data, 'priority_labels', 'config', ('prerequisito',)),
        **ints)


def load_config(path=None):
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, 'rb') as fh:
        return parse_config(tomllib.load(fh))
