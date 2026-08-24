from app.service.access.model import AccessDecision


class AccessDeniedError(PermissionError):
    def __init__(self, decision: AccessDecision):
        super().__init__(decision.reason)
        self.decision = decision


class AccessConflictError(ValueError):
    """The submitted ACL projection no longer matches durable state."""


def require_access(decision: AccessDecision) -> None:
    if not decision.allowed:
        raise AccessDeniedError(decision)
