"""Host-owned, append-only transition primitives; never dispatch a model.

Callers hold the ticket and scheduler locks and fence all old writers. A
transition preserves the source journal and publishes a separate generation.
Only a fully materialized generation can be selected by its atomic pointer.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from ssdlc_tasks import TaskError, atomic_json, digest, file_hash


class TransitionError(TaskError):
    pass


def _regular(root, name):
    relative = Path(name)
    if (not name or relative.is_absolute() or '..' in relative.parts or '\\' in name
            or any(ord(c) < 32 for c in name)):
        raise TransitionError('Unsafe transition artifact path')
    current = root
    if current.is_symlink():
        raise TransitionError('Unsafe transition root')
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise TransitionError('Symlink in transition evidence')
    if not current.is_file():
        raise TransitionError('Missing transition evidence')
    return current


def source_binding(root, refs):
    """Literal source hashes, not a projection inferred from model output."""
    return {name: file_hash(_regular(root, name)) for name in sorted(refs)}


def validate_authorization(authorization, proposal):
    from ssdlc_operator_recovery import CONTRACT as RECOVERY_CONTRACT
    if ('owner_allowance_sha256' in proposal
            and proposal.get('authorization_contract') not in {'scope-amendment-v1', RECOVERY_CONTRACT}):
        raise TransitionError('Manual owner allowances cannot use delegated authorization')
    if proposal.get('authorization_contract') == 'feedback-in-scope-return-v1':
        from ssdlc_feedback_return import validate_authorization as repair
        return repair(authorization, proposal)
    if proposal.get('authorization_contract') == 'feedback-scope-delegation-v1':
        from ssdlc_feedback_delegation import validate_authorization as delegated
        return delegated(authorization, proposal)
    expected = {'schema_version', 'transition_id', 'proposal_sha256', 'authorized',
                'source_fenced', 'old_controller_sha256', 'new_controller_sha256'}
    if not isinstance(authorization, dict):
        raise TransitionError('Transition requires exact, fenced operator authorization')
    if 'owner_allowance_sha256' in proposal:
        expected.add('owner_allowance_sha256')
        if (proposal.get('authorization_contract') not in {'scope-amendment-v1', RECOVERY_CONTRACT}
                or 'owner_feedback_sha256' not in proposal
                or authorization.get('owner_allowance_sha256') != proposal['owner_allowance_sha256']):
            raise TransitionError('Manual owner allowance requires its exact separate operator authorization')
    if proposal.get('authorization_contract') == RECOVERY_CONTRACT:
        expected |= {'profile_sha256', 'operator_recovery_sha256', 'authorize_extensions'}
        if (authorization.get('profile_sha256') != proposal.get('profile_sha256')
                or authorization.get('operator_recovery_sha256') != proposal.get('operator_recovery_sha256')
                or authorization.get('authorize_extensions') is not True
                or 'owner_feedback_sha256' not in proposal):
            raise TransitionError('Operator recovery requires exact explicit profile/attempt/scope extension authorization')
    if proposal.get('authorization_contract') == 'feedback-profile-admission-v1':
        expected |= {'profile_sha256'}
        if authorization.get('profile_sha256') != proposal.get('profile_sha256') or proposal.get('scope_diff') != {}:
            raise TransitionError('Feedback profile admission requires exact unchanged scope and profile authorization')
    if proposal.get('authorization_contract') in {'operator-verification-repair-v1', 'operator-verification-repair-deadline-v1'}:
        expected |= {'profile_sha256', 'quality_contract_sha256', 'verification_repair_sha256', 'authorize_extensions'}
        if (any(authorization.get(key) != proposal.get(key) for key in
                ('profile_sha256', 'quality_contract_sha256', 'verification_repair_sha256'))
                or authorization.get('authorize_extensions') is not True):
            raise TransitionError('Verification repair requires exact explicit timeout, scope and attempt authorization')
        if proposal['authorization_contract'] == 'operator-verification-repair-deadline-v1':
            expected |= {'deadline_extension_sha256', 'authorize_deadline_extension'}
            if (authorization.get('deadline_extension_sha256') != proposal.get('deadline_extension_sha256')
                    or authorization.get('authorize_deadline_extension') is not True):
                raise TransitionError('Deadline extension requires its exact separate explicit authorization')
    if proposal.get('authorization_contract') == 'feedback-operator-repair-return-v1':
        expected |= {'repair_return_sha256'}
        if (authorization.get('repair_return_sha256') != proposal.get('repair_return_sha256')
                or proposal.get('scope_diff') != {}):
            raise TransitionError('Operator repair return must bind exact unchanged scope and feedback')
    if proposal.get('authorization_contract') == 'cumulative-limits-v1':
        expected |= {'profile_sha256', 'decisions_sha256', 'accept_unknown_history', 'authorize_extensions'}
        if authorization.get('profile_sha256') != proposal.get('profile_sha256'):
            raise TransitionError('Authorization does not bind the effective profile')
        from ssdlc_historical import validate_decisions
        validate_decisions(authorization, proposal)
    scope = proposal.get('scope_diff', {})
    if any(item.get('added') for item in scope.values()):
        expected = expected | {'scope_diff_sha256'}
        if authorization.get('scope_diff_sha256') != digest(scope):
            raise TransitionError('Scope expansion requires its exact explicit authorization')
    if (not isinstance(authorization, dict) or set(authorization) != expected
            or type(authorization['schema_version']) is not int or authorization['schema_version'] != 1
            or authorization['authorized'] is not True or authorization['source_fenced'] is not True
            or authorization['transition_id'] != proposal['transition_id']
            or authorization['proposal_sha256'] != digest(proposal)
            or authorization['old_controller_sha256'] != proposal['old_controller_sha256']
            or authorization['new_controller_sha256'] != proposal['new_controller_sha256']):
        raise TransitionError('Transition requires exact, fenced operator authorization')


def preserve_allowances(old, new):
    """Counter/ledger preservation is exact; extensions are a separate operation."""
    for key in ('ledger', 'limits', 'counters'):
        if old.get(key) != new.get(key):
            raise TransitionError('Transition changes cumulative ' + key)
    previous, projected = old.get('tasks', {}), new.get('tasks', {})
    if set(previous) != set(projected):
        raise TransitionError('Task identities need an explicit lineage mapping')
    for tid, record in previous.items():
        if record.get('attempts') != projected[tid].get('attempts'):
            raise TransitionError('Transition resets task attempts')


def sync_tree(root):
    """Flush staged payload and every directory before publishing its pointer."""
    directories = [Path(root)]
    for path in Path(root).rglob('*'):
        if path.is_symlink():
            raise TransitionError('Symlink in staged generation')
        if path.is_file():
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
        elif path.is_dir():
            directories.append(path)
    for path in reversed(directories):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class TransitionStore:
    """A private generation store, not an execution/approval authority.

    Hashes assume the host protects both authorization and reference files.
    The published pointer is intentionally separate from historical artifacts.
    Integrating readers must validate the pointer before using its projection.
    """
    def __init__(self, directory, fault=lambda point: None):
        self.root = Path(directory)
        self.fault = fault

    def _read(self, root, name):
        return json.loads(_regular(root, name).read_text())

    def publish(self, source, proposal, authorization, old_state, new_state, artifacts):
        identity = proposal.get('transition_id', '')
        if not re.fullmatch(r'[a-f0-9]{32}', identity):
            raise TransitionError('Invalid transition identity')
        validate_authorization(authorization, proposal)
        preserve_allowances(old_state, new_state)
        if digest(new_state) != proposal.get('projection_sha256'):
            raise TransitionError('Projection differs from reviewed proposal')
        if source_binding(source, proposal['source_files']) != proposal['source_files']:
            raise TransitionError('Historical source changed')
        if {name: hashlib.sha256(data).hexdigest() for name, data in artifacts.items()} != proposal['artifacts']:
            raise TransitionError('Transition artifacts differ from proposal')
        if set(artifacts) & {'intent.json', 'manifest.json', 'projection.json'}:
            raise TransitionError('Reserved transition artifact')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise TransitionError('Unsafe transition store')
        generation = self.root / identity
        intent = {'proposal': proposal, 'authorization': authorization}
        if generation.exists():
            if self._read(generation, 'intent.json') != intent:
                raise TransitionError('Transition identity reused with different inputs')
        else:
            temporary = Path(tempfile.mkdtemp(prefix='.intent-', dir=self.root))
            atomic_json(temporary / 'intent.json', intent)
            os.rename(temporary, generation)
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        self.fault('intent_published')
        # Partially staged generations have no authority. Immutable files are
        # checked on retry instead of overwritten or accepted from existence.
        files = dict(artifacts)
        files['projection.json'] = (json.dumps(new_state, sort_keys=True) + '\n').encode()
        for name, data in files.items():
            if name in ('intent.json', 'manifest.json'):
                raise TransitionError('Reserved transition artifact')
            target = generation / name
            if target.exists():
                if _regular(generation, name).read_bytes() != data:
                    raise TransitionError('Staged transition evidence changed')
                continue
            # Validate the path before creating parents or writing bytes.
            parts = Path(name)
            if (parts.is_absolute() or '..' in parts.parts or not name or '\\' in name
                    or any(ord(c) < 32 for c in name)):
                raise TransitionError('Unsafe staged artifact')
            current = generation
            for part in parts.parts:
                current = current / part
                if current.is_symlink():
                    raise TransitionError('Unsafe staged artifact')
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=target.parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        manifest = source_binding(generation, files)
        if (generation / 'manifest.json').exists():
            if self._read(generation, 'manifest.json') != manifest:
                raise TransitionError('Generation manifest changed')
        else:
            atomic_json(generation / 'manifest.json', manifest)
        sync_tree(generation)
        self.fault('generation_staged')
        if source_binding(source, proposal['source_files']) != proposal['source_files']:
            raise TransitionError('Source changed before generation publication')
        selected = {'schema_version': 1, 'transition_id': identity,
                    'intent_sha256': file_hash(_regular(generation, 'intent.json')),
                    'manifest_sha256': file_hash(_regular(generation, 'manifest.json'))}
        pointer = self.root / 'active.json'
        if pointer.exists() and self._read(self.root, 'active.json') != selected:
            if (proposal.get('authorization_contract') not in {'scope-amendment-v1', 'feedback-scope-delegation-v1',
                    'feedback-profile-admission-v1', 'operator-owner-recovery-v1',
                    'operator-verification-repair-v1',
                    'operator-verification-repair-deadline-v1',
                    'feedback-in-scope-return-v1', 'feedback-operator-repair-return-v1'}
                    or proposal.get('parent_selector') != self._read(self.root, 'active.json')):
                raise TransitionError('Another generation is already selected')
            self.load()  # Validate the exact parent, not merely its pointer bytes.
        self.fault('before_activation')
        atomic_json(pointer, selected)
        self.fault('after_activation')
        return self.load()

    def load(self):
        pointer = self._read(self.root, 'active.json')
        return self.load_pointer(pointer)

    def load_pointer(self, pointer):
        """Validate an exact sealed ancestor without changing the live selector."""
        if (not isinstance(pointer, dict) or set(pointer) != {'schema_version', 'transition_id', 'intent_sha256', 'manifest_sha256'}
                or type(pointer['schema_version']) is not int or pointer['schema_version'] != 1):
            raise TransitionError('Unsupported active pointer schema')
        identity = pointer.get('transition_id', '')
        if not re.fullmatch(r'[a-f0-9]{32}', identity):
            raise TransitionError('Invalid active generation')
        generation = self.root / identity
        if (file_hash(_regular(generation, 'intent.json')) != pointer['intent_sha256']
                or file_hash(_regular(generation, 'manifest.json')) != pointer['manifest_sha256']):
            raise TransitionError('Active generation reference changed')
        manifest = self._read(generation, 'manifest.json')
        if source_binding(generation, manifest) != manifest:
            raise TransitionError('Active generation artifacts changed')
        projection = self._read(generation, 'projection.json')
        intent = self._read(generation, 'intent.json')
        validate_authorization(intent['authorization'], intent['proposal'])
        if digest(projection) != intent['proposal']['projection_sha256']:
            raise TransitionError('Active projection differs from reviewed proposal')
        return projection


def runtime_root(ticket_root):
    """Resolve only an explicitly published generation; legacy paths stay put."""
    ticket_root = Path(ticket_root)
    if (ticket_root / 'execution/active.json').exists():
        from ssdlc_reconciliation import resolve_execution
        return resolve_execution(ticket_root)
    if (ticket_root / 'execution/pending.json').exists():
        raise TransitionError('Execution reconciliation is pending; dispatch and verification are fenced')
    store = ticket_root / 'continuations'
    if not store.exists():
        return ticket_root
    if not (store / 'active.json').exists():
        if (store / 'pending.json').exists():
            raise TransitionError('Continuation preparation is pending; execution is fenced')
        return ticket_root
    projection = TransitionStore(store).load()
    identity = projection.get('transition_id', '')
    if not re.fullmatch(r'[a-f0-9]{32}', identity):
        raise TransitionError('Active runtime identity is missing')
    root = ticket_root / 'runtime-generations' / identity
    marker = json.loads(_regular(root, 'generation.json').read_text())
    if marker != projection['runtime_binding']:
        raise TransitionError('Runtime generation binding changed')
    return root
