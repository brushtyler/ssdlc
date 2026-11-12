"""devloop command line: python3 -m devloop {setup,status,tick,run,cleanup}."""

import argparse
import fcntl
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .agents import AgentRunner
from .config import ConfigError, load_config
from .dispatcher import Dispatcher, ensure_labels, plan, run_forever
from .github import GitHub
from .workspace import Workspace


def main_checkout():
    """The main checkout of the repository containing the current directory."""
    common = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                            capture_output=True, text=True, check=True).stdout.strip()
    return Path(common).parent


@contextmanager
def single_instance(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'w') as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit('devloop: another dispatcher is running (lock held)')
        yield


def log(message):
    print(f'{time.strftime("%H:%M:%S")} {message}', flush=True)


def build(args):
    cfg = load_config(args.config)
    root = main_checkout()
    state = root / cfg.state_dir
    gh = GitHub(cfg.repo)
    ws = Workspace(root, cfg.state_dir, cfg.base_branch)
    runner = AgentRunner(state / 'logs', cfg.agent_timeout_seconds)
    return cfg, state, Dispatcher(cfg, gh, ws, runner, log=log)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='devloop', description=__doc__)
    parser.add_argument('--config', help='TOML config (default: devloop/config.toml)')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('setup', help='create or update the agent:* labels')
    sub.add_parser('status', help='show devloop PRs, ready issues and the planned actions')
    tick = sub.add_parser('tick', help='plan and run one round of actions')
    tick.add_argument('--dry-run', action='store_true', help='only print the planned actions')
    run = sub.add_parser('run', help='tick forever')
    run.add_argument('--interval', type=int, default=600, help='seconds between ticks')
    sub.add_parser('cleanup', help='remove devloop worktrees left by failed steps')
    args = parser.parse_args(argv)

    try:
        cfg, state, dispatcher = build(args)
    except ConfigError as exc:
        sys.exit(f'devloop: invalid config: {exc}')

    if args.command == 'setup':
        ensure_labels(dispatcher.gh, cfg.labels)
        log('labels ready: ' + ', '.join(cfg.labels.all()))
        return
    if args.command == 'status':
        snap = dispatcher.snapshot()
        for pr in snap.prs:
            labels = ', '.join(sorted(l for l in pr.labels if l.startswith('agent:')))
            log(f'PR #{pr.number} (issue #{pr.issue}) [{labels}] rounds={len(pr.reviews)} ci={pr.checks}')
        for issue in snap.ready_issues:
            log(f'ready #{issue["number"]} labelled by {issue["labeler"]}: {issue["title"]}')
        for action in plan(snap, cfg):
            log(f'next: {action.kind} #{action.number}')
        return
    with single_instance(state / 'dispatcher.lock'):
        if args.command == 'tick':
            dispatcher.tick(dry_run=args.dry_run)
        elif args.command == 'run':
            run_forever(dispatcher, args.interval)
        elif args.command == 'cleanup':
            for name in dispatcher.ws.names():
                dispatcher.ws.remove(name)
                log(f'removed worktree {name}')


if __name__ == '__main__':
    main()
