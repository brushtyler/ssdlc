"""Headless invocation of the agent CLIs with schema-validated final output."""

import json
import os
import subprocess
import time
from pathlib import Path

from .markdown import header, parse_version
from .schema import load_schema, schema_path, validate, SchemaError
from .workspace import git_config_env

MODES = ('write', 'review')
CREDENTIAL_VARS = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN',
                   'SSH_AUTH_SOCK', 'GIT_CONFIG_PARAMETERS')


def isolated_env(gh_config_dir, base=None):
    """Environment for agent CLIs and the test command: no GitHub credentials in reach of `gh`
    or `git`.

    - ``gh`` gets an empty config directory and no token variables;
    - git may only use local repositories (``GIT_ALLOW_PROTOCOL=file``), so neither SSH keys nor
      HTTPS credentials can be used, whatever the config files say;
    - as further layers, credential helpers, askpass programs (``GIT_ASKPASS`` overrides
      ``core.askPass``), preconfigured authentication headers and SSH are disabled.

    This prevents accidental use of the operator's credentials, not deliberate access: code
    running as the same system user can still read files. Real isolation needs a dedicated
    system user or a container.
    """
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k not in CREDENTIAL_VARS and not k.startswith('GIT_CONFIG_')}
    env.update(git_config_env([('credential.helper', ''), ('http.extraHeader', '')]))
    env.update({'GH_CONFIG_DIR': str(gh_config_dir), 'GIT_ALLOW_PROTOCOL': 'file',
                'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': 'false', 'SSH_ASKPASS': 'false',
                'GIT_SSH_COMMAND': 'false'})
    return env


class AgentError(RuntimeError):
    def __init__(self, message, log=None):
        super().__init__(message)
        self.log = log


def build_command(agent, mode, prompt, *, schema_file, out_file):
    """Return ``(argv, stdin)`` for one headless agent turn in the current directory."""
    if mode not in MODES:
        raise ValueError(mode)
    extra = list(agent.write_args if mode == 'write' else agent.review_args)
    if agent.kind == 'claude':
        schema = Path(schema_file).read_text(encoding='utf-8')
        argv = ['claude', '-p', '--model', agent.model, '--effort', agent.effort,
                '--output-format', 'json', '--json-schema', schema, *extra]
        return argv, prompt
    if agent.kind == 'codex':
        sandbox = 'workspace-write' if mode == 'write' else 'read-only'
        argv = ['codex', 'exec', '--ephemeral', '-m', agent.model,
                '-c', f'model_reasoning_effort="{agent.effort}"', '-s', sandbox,
                '--output-schema', str(schema_file), '-o', str(out_file), *extra, '-']
        return argv, prompt
    if agent.kind == 'agy':
        argv = ['agy', '-p', prompt, '--model', agent.model, '--effort', agent.effort,
                '--output-format', 'json', '--json-schema', str(schema_file),
                '--mode', 'accept-edits' if mode == 'write' else 'plan', *extra]
        return argv, None
    raise ValueError(agent.kind)


def extract_output(kind, stdout, out_file):
    """The structured final message of one agent turn, before schema validation."""
    if kind == 'codex':
        try:
            text = Path(out_file).read_text(encoding='utf-8')
        except OSError as exc:
            raise AgentError(f'codex wrote no final message: {exc}') from exc
        return json.loads(text)
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AgentError(f'{kind} output is not JSON: {exc}') from exc
    if kind == 'claude' and envelope.get('is_error'):
        raise AgentError(f'claude reported an error: {envelope.get("subtype")}: '
                         f'{str(envelope.get("result"))[:500]}')
    if kind == 'agy' and envelope.get('status') not in (None, 'SUCCESS'):
        raise AgentError(f'agy status {envelope.get("status")}: {str(envelope.get("response"))[:500]}')
    output = envelope.get('structured_output')
    if output is None:
        raise AgentError(f'{kind} returned no structured_output')
    return output


class AgentRunner:
    def __init__(self, log_dir, timeout, run=subprocess.run, env=None):
        self.log_dir = Path(log_dir)
        self.timeout = timeout
        self._run = run
        self._env = env
        self._versions = {}

    def version(self, agent):
        if agent.key not in self._versions:
            try:
                done = self._run(list(agent.version_cmd), capture_output=True, text=True, timeout=60)
                self._versions[agent.key] = parse_version(done.stdout + done.stderr)
            except (OSError, subprocess.SubprocessError):
                self._versions[agent.key] = None
        return self._versions[agent.key]

    def header(self, agent):
        return header(agent.display_name, self.version(agent), agent.model_label, agent.effort)

    def run(self, agent, mode, prompt, schema_name, workdir, label):
        """Run one turn in ``workdir`` and return its schema-valid output."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stem = self.log_dir / f'{time.strftime("%Y%m%dT%H%M%S")}-{label}-{agent.key}'
        out_file = stem.with_suffix('.last.json')
        argv, stdin = build_command(agent, mode, prompt, schema_file=schema_path(schema_name),
                                    out_file=out_file)
        stem.with_suffix('.prompt.md').write_text(prompt, encoding='utf-8')
        log = stem.with_suffix('.log')
        kwargs = {'env': self._env} if self._env is not None else {}
        try:
            done = self._run(argv, input=stdin, cwd=workdir, capture_output=True, text=True,
                             timeout=self.timeout, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f'{agent.key} timed out after {self.timeout}s', log) from exc
        except OSError as exc:
            raise AgentError(f'{agent.key} could not start: {exc}', log) from exc
        log.write_text(f'$ {argv[0]} ({mode})\nexit {done.returncode}\n--- stdout\n{done.stdout}'
                       f'\n--- stderr\n{done.stderr}', encoding='utf-8')
        if done.returncode != 0:
            raise AgentError(f'{agent.key} exited with {done.returncode}', log)
        try:
            output = extract_output(agent.kind, done.stdout, out_file)
            return validate(output, load_schema(schema_name))
        except AgentError as exc:
            exc.log = log
            raise
        except (SchemaError, json.JSONDecodeError) as exc:
            raise AgentError(f'{agent.key} output does not match {schema_name}: {exc}', log) from exc
