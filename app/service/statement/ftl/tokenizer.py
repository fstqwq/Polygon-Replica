from __future__ import annotations

import ast


def _tokenize_expr(expr: str) -> list[tuple[str, object]]:
    tokens: list[tuple[str, object]] = []
    text = str(expr or "")
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if text.startswith("&&", i):
            tokens.append(("op", "&&"))
            i += 2
            continue
        if text.startswith("||", i):
            tokens.append(("op", "||"))
            i += 2
            continue
        if text.startswith("!=", i):
            tokens.append(("op", "!="))
            i += 2
            continue
        if text.startswith("<=", i):
            tokens.append(("op", "<="))
            i += 2
            continue
        if text.startswith(">=", i):
            tokens.append(("op", ">="))
            i += 2
            continue
        if text.startswith("??", i):
            tokens.append(("op", "??"))
            i += 2
            continue
        if ch in {'"', "'"}:
            quote = ch
            j = i + 1
            escaped = False
            while j < n:
                cj = text[j]
                if escaped:
                    escaped = False
                    j += 1
                    continue
                if cj == "\\":
                    escaped = True
                    j += 1
                    continue
                if cj == quote:
                    break
                j += 1
            if j >= n:
                raise ValueError("unterminated string")
            raw = text[i : j + 1]
            tokens.append(("string", str(ast.literal_eval(raw))))
            i = j + 1
            continue
        if ch.isdigit():
            j = i + 1
            while j < n and text[j].isdigit():
                j += 1
            if j < n and text[j] == ".":
                j += 1
                while j < n and text[j].isdigit():
                    j += 1
                tokens.append(("number", float(text[i:j])))
            else:
                tokens.append(("number", int(text[i:j])))
            i = j
            continue
        if ch == "?" and i + 1 < n and (text[i + 1].isalpha() or text[i + 1] == "_"):
            j = i + 2
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("builtin", text[i + 1 : j]))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in {"_", "-"}):
                j += 1
            tokens.append(("ident", text[i:j]))
            i = j
            continue
        if ch in {"=", "<", ">", "+", "-", "*", "/", "%", "(", ")", ".", "!"}:
            tokens.append(("op", ch))
            i += 1
            continue
        raise ValueError(f"unsupported token: {ch}")
    return tokens

