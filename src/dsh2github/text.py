import re


def redact(text):
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b", "[凭据已隐藏]", str(text))
    text = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[凭据已隐藏]", text)
    return re.sub(r"(?im)((?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|password|密码|密钥)\s*[:=]\s*['\"]?)[^\s'\";,]{8,}", r"\1[凭据已隐藏]", text)


def label(text):
    return re.sub(r"([\\`*\[\]|<>])", r"\\\1", redact(text)).replace("\n", " ")


def chunks(text, limit=45000):
    # Keep comments below GitHub's byte limit, including multibyte Chinese.
    remaining = text
    while remaining:
        size, cut = 0, 0
        for character in remaining:
            weight = len(character.encode())
            if size + weight > limit:
                break
            size += weight
            cut += 1
        yield remaining[:cut]
        remaining = remaining[cut:]
