from dataclasses import dataclass
from typing import Generic, TypeVar

Acknowledgement = TypeVar("Acknowledgement")


@dataclass(frozen=True, slots=True)
class HostContact:
    hostname: str


@dataclass(frozen=True, slots=True)
class CallbackOutcome(Generic[Acknowledgement]):
    acknowledgement: Acknowledgement
    terminal_batch_ids: tuple[int, ...]
    touched_verification_ids: tuple[str, ...]
    host_contacts: tuple[HostContact, ...]
