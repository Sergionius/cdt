"""Branch-local values; no other context state is transactional."""

from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from copy import deepcopy
from threading import local

import typer


class ScopedValues(MutableMapping):
    def __init__(self, values: Mapping | None = None):
        self.root = deepcopy(dict(values or {}))
        self._local = local()

    @property
    def current(self) -> dict:
        return getattr(self._local, "values", self.root)

    @property
    def branch_id(self) -> str | None:
        return getattr(self._local, "branch_id", None)

    def __getitem__(self, key):
        return self.current[key]

    def __setitem__(self, key, value):
        self.current[key] = value

    def __delitem__(self, key):
        del self.current[key]

    def __iter__(self) -> Iterator:
        return iter(self.current)

    def __len__(self) -> int:
        return len(self.current)

    def copy(self) -> dict:
        return deepcopy(self.current)

    def merge(self, base: dict, branches: dict[str, dict]) -> None:
        missing = object()
        changes = {}
        owners = {}
        for branch_id, snapshot in branches.items():
            for key in sorted(base.keys() | snapshot.keys()):
                value = snapshot.get(key, missing)
                if value == base.get(key, missing):
                    continue
                if key in changes and changes[key] != value:
                    raise typer.BadParameter(
                        f"Parallel values conflict for key {key!r} between steps {owners[key]} and {branch_id}"
                    )
                changes[key] = value
                owners[key] = branch_id
        merged = deepcopy(base)
        for key, value in changes.items():
            if value is missing:
                merged.pop(key, None)
            else:
                merged[key] = deepcopy(value)
        self.root = merged

    @contextmanager
    def scope(self, branch_id: str, values: dict):
        try:
            self._local.values = values
            self._local.branch_id = branch_id
            yield
        finally:
            del self._local.values
            del self._local.branch_id
