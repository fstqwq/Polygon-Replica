from __future__ import annotations

from sortedcontainers import SortedList


ReadyBatchKey = tuple[int, int, int, int, int]


class ReadyBatchIndex:
    """Exact ordered index containing at most one key per ready Batch."""

    def __init__(self) -> None:
        self._keys: SortedList[ReadyBatchKey] = SortedList()
        self._key_by_batch_id: dict[int, ReadyBatchKey] = {}

    def clear(self) -> None:
        self._keys.clear()
        self._key_by_batch_id.clear()

    def first(self) -> ReadyBatchKey | None:
        return None if not self._keys else self._keys[0]

    def key_for(self, batch_id: int) -> ReadyBatchKey | None:
        return self._key_by_batch_id.get(int(batch_id))

    def update(self, batch_id: int, key: ReadyBatchKey | None) -> bool:
        numeric_id = int(batch_id)
        previous = self._key_by_batch_id.get(numeric_id)
        if previous == key:
            return False
        if previous is not None:
            self._keys.remove(previous)
            self._key_by_batch_id.pop(numeric_id)
        if key is not None:
            if key[-1] != numeric_id:
                raise ValueError("ready Batch key id mismatch")
            self._keys.add(key)
            self._key_by_batch_id[numeric_id] = key
        return True

    def remove(self, batch_id: int) -> bool:
        return self.update(int(batch_id), None)

    def __len__(self) -> int:
        return len(self._keys)
