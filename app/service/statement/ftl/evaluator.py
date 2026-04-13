from __future__ import annotations

from app.service.statement.ftl.parser import _split_default_expr
from app.service.statement.ftl.tokenizer import _tokenize_expr


UNDEFINED = object()


def _to_number(value: object) -> float | int:
    if value is UNDEFINED or value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return 0
    try:
        if "." in text:
            return float(text)
        return int(text)
    except Exception:
        return 0


def _num_to_text(value: object) -> str:
    if value is UNDEFINED or value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.12f}".rstrip("0").rstrip(".")
    return str(value)


def _truthy(value: object) -> bool:
    if value is UNDEFINED or value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def _resolve_member(value: object, key: str) -> object:
    if value is UNDEFINED or value is None:
        return UNDEFINED
    if isinstance(value, dict):
        return value.get(key, UNDEFINED)
    try:
        if hasattr(value, key):
            return getattr(value, key)
    except Exception:
        return UNDEFINED
    return UNDEFINED


def _apply_builtin(value: object, builtin: str) -> object:
    token = str(builtin or "").strip().lower()
    if token in {"size", "length"}:
        if value is UNDEFINED or value is None:
            return 0
        try:
            return len(value)  # type: ignore[arg-type]
        except Exception:
            return len(str(value))
    if token == "c":
        return _num_to_text(value)
    if token == "string":
        if value is UNDEFINED or value is None:
            return ""
        return str(value)
    return value


def _compare_values(left: object, right: object, op: str) -> bool:
    if op == "=":
        if left is UNDEFINED and right is UNDEFINED:
            return True
        return left == right
    if op == "!=":
        if left is UNDEFINED and right is UNDEFINED:
            return False
        return left != right
    if left is UNDEFINED or right is UNDEFINED:
        return False
    l_num = _to_number(left)
    r_num = _to_number(right)
    if isinstance(left, (int, float, bool)) or isinstance(right, (int, float, bool)):
        if op == "<":
            return l_num < r_num
        if op == "<=":
            return l_num <= r_num
        if op == ">":
            return l_num > r_num
        if op == ">=":
            return l_num >= r_num
        return False
    l_text = str(left)
    r_text = str(right)
    if op == "<":
        return l_text < r_text
    if op == "<=":
        return l_text <= r_text
    if op == ">":
        return l_text > r_text
    if op == ">=":
        return l_text >= r_text
    return False


class _ExprParser:
    def __init__(self, tokens: list[tuple[str, object]], scope: dict[str, object]):
        self.tokens = tokens
        self.scope = scope
        self.index = 0

    def parse(self) -> object:
        return self._parse_or()

    def _peek(self) -> tuple[str, object]:
        if self.index >= len(self.tokens):
            return ("eof", "")
        return self.tokens[self.index]

    def _take(self) -> tuple[str, object]:
        token = self._peek()
        if self.index < len(self.tokens):
            self.index += 1
        return token

    def _accept_op(self, op: str) -> bool:
        kind, value = self._peek()
        if kind == "op" and value == op:
            self.index += 1
            return True
        return False

    def _accept_builtin(self) -> str:
        kind, value = self._peek()
        if kind == "builtin":
            self.index += 1
            return str(value)
        return ""

    def _expect_op(self, op: str) -> None:
        if not self._accept_op(op):
            raise ValueError(f"expected '{op}'")

    def _parse_or(self) -> object:
        left = self._parse_and()
        while self._accept_op("||"):
            right = self._parse_and()
            left = bool(_truthy(left) or _truthy(right))
        return left

    def _parse_and(self) -> object:
        left = self._parse_cmp()
        while self._accept_op("&&"):
            right = self._parse_cmp()
            left = bool(_truthy(left) and _truthy(right))
        return left

    def _parse_cmp(self) -> object:
        left = self._parse_add()
        while True:
            kind, value = self._peek()
            if kind != "op" or value not in {"=", "!=", "<", "<=", ">", ">="}:
                break
            self.index += 1
            right = self._parse_add()
            left = _compare_values(left, right, str(value))
        return left

    def _parse_add(self) -> object:
        left = self._parse_mul()
        while True:
            kind, value = self._peek()
            if kind != "op" or value not in {"+", "-"}:
                break
            self.index += 1
            right = self._parse_mul()
            if value == "+":
                if isinstance(left, str) or isinstance(right, str):
                    left = str("" if left is UNDEFINED else left) + str("" if right is UNDEFINED else right)
                else:
                    left = _to_number(left) + _to_number(right)
            else:
                left = _to_number(left) - _to_number(right)
        return left

    def _parse_mul(self) -> object:
        left = self._parse_unary()
        while True:
            kind, value = self._peek()
            if kind != "op" or value not in {"*", "/", "%"}:
                break
            self.index += 1
            right = self._parse_unary()
            if value == "*":
                left = _to_number(left) * _to_number(right)
            elif value == "/":
                divisor = _to_number(right)
                left = int(_to_number(left) / divisor) if divisor else 0
            else:
                divisor = _to_number(right)
                left = (_to_number(left) % divisor) if divisor else 0
        return left

    def _parse_unary(self) -> object:
        if self._accept_op("!"):
            return not _truthy(self._parse_unary())
        if self._accept_op("-"):
            return -_to_number(self._parse_unary())
        return self._parse_postfix()

    def _parse_postfix(self) -> object:
        value = self._parse_primary()
        while True:
            if self._accept_op("."):
                kind, token = self._take()
                if kind != "ident":
                    raise ValueError("member accessor expects identifier")
                value = _resolve_member(value, str(token))
                continue
            if self._accept_op("??"):
                value = value is not UNDEFINED and value is not None
                continue
            builtin = self._accept_builtin()
            if builtin:
                value = _apply_builtin(value, builtin)
                continue
            break
        return value

    def _parse_primary(self) -> object:
        kind, value = self._peek()
        if kind == "number":
            self.index += 1
            return value
        if kind == "string":
            self.index += 1
            return value
        if kind == "ident":
            self.index += 1
            name = str(value)
            if name == "true":
                return True
            if name == "false":
                return False
            if name == "null":
                return None
            return self.scope.get(name, UNDEFINED)
        if self._accept_op("("):
            inner = self._parse_or()
            self._expect_op(")")
            return inner
        raise ValueError("invalid expression")


def _eval_expr(expr: str, scope: dict[str, object]) -> object:
    parser = _ExprParser(_tokenize_expr(expr), scope)
    return parser.parse()


def _iter_values(value: object) -> list[object]:
    if value is UNDEFINED or value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    return [value]


def _eval_interpolation(expr: str, scope: dict[str, object]) -> str:
    left, fallback = _split_default_expr(expr)
    value = _eval_expr(left, scope)
    if value is UNDEFINED or value is None:
        if fallback is None:
            return ""
        if fallback == "":
            return ""
        return str(_eval_expr(fallback, scope))
    if isinstance(value, (int, float, bool)):
        return _num_to_text(value)
    return str(value)

