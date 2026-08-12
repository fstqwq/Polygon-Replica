_SCRIPT_ID_MODULUS = 1048576


def domjudge_script_id(script_hash: str) -> int:
    token = str(script_hash or '').strip().lower()
    if len(token) != 32 or any(ch not in '0123456789abcdef' for ch in token):
        raise RuntimeError('invalid script hash')
    return (int(token, 16) % _SCRIPT_ID_MODULUS) + 1


def domjudge_parse_script_id(raw_id: object) -> int:
    try:
        token = int(str(raw_id or '').strip())
    except Exception as exc:
        raise RuntimeError('invalid script id') from exc
    if token <= 0:
        raise RuntimeError('invalid script id')
    return token


def domjudge_script_hash_field(kind: str) -> str:
    token = str(kind or '').strip().lower()
    mapping = {
        'compile': 'compile_hash',
        'run': 'run_hash',
        'compare': 'compare_hash',
    }
    field = mapping.get(token)
    if not field:
        raise RuntimeError('invalid script kind')
    return field
