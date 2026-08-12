from app.impl.runtime.dependency import runtime
from app.service.problem_package.service import MaterializationRow, PublishedRevision


def ensure_published_materialization(
    *,
    revision: PublishedRevision,
    actor_user_id: int,
    actor_username: str,
) -> MaterializationRow:
    return runtime().published_materialization_workflow.ensure(
        revision=revision,
        actor_user_id=actor_user_id,
        actor_username=actor_username,
    )
