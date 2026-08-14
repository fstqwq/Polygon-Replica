class YAMLError(Exception): ...


def safe_load(stream: str | bytes) -> object: ...


def safe_dump(
    data: object,
    *,
    allow_unicode: bool = ...,
    default_flow_style: bool | None = ...,
    sort_keys: bool = ...,
    width: int = ...,
) -> str: ...
