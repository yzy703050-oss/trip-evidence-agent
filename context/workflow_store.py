"""User-isolated workflow snapshots with atomic writes and process-safe CAS."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import threading

from context.session_store import atomic_json_write, storage_component

_guard = threading.Lock()
_locks = {}


class WorkflowConflictError(ValueError):
    """A newer workflow revision exists; never replay queries to resolve this."""


@contextmanager
def _file_lock(path):
    key = path.resolve()
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+b') as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'\0'); handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class WorkflowStore:
    def __init__(self, root, user_id):
        self.directory = Path(root) / storage_component(user_id) / 'workflows'

    def _path(self, workflow_id):
        return self.directory / f'{storage_component(workflow_id)}.json'

    @staticmethod
    def _read(path):
        if not path.exists(): return None
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or type(value.get('revision')) is not int or not isinstance(value.get('tasks'), list):
            raise ValueError('invalid workflow snapshot')
        return value

    def load(self, workflow_id):
        path = self._path(workflow_id)
        with _file_lock(path.with_suffix('.lock')):
            value = self._read(path)
            if value is not None and value.get('id') != workflow_id:
                raise ValueError('workflow snapshot identity mismatch')
            return value

    def save(self, workflow, *, expected_revision):
        value = deepcopy(workflow)
        path = self._path(value['id'])
        with _file_lock(path.with_suffix('.lock')):
            current = self._read(path)
            if current is None:
                if expected_revision is not None: raise WorkflowConflictError('workflow no longer exists')
                revision = 1
            else:
                if type(expected_revision) is not int or current['revision'] != expected_revision:
                    raise WorkflowConflictError('workflow revision changed')
                revision = expected_revision + 1
            value['revision'] = revision
            atomic_json_write(path, value)
        return value

    def list_active(self):
        if not self.directory.exists(): return []
        values = [self.load(path.stem) for path in sorted(self.directory.glob('*.json'))]
        return [value for value in values if value and value['status'] in {'running', 'needs_input', 'partial'}]
