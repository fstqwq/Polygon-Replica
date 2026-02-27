from __future__ import annotations

from app.services.sandbox.base import SandboxBackend
from app.services.sandbox.native_backend import NativeSandboxBackend


def create_sandbox_backend() -> SandboxBackend:
    return NativeSandboxBackend()
