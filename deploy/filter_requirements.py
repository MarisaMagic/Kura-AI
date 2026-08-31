"""Drop Windows-only pins and normalize encoding for Linux pip."""

from pathlib import Path

SKIP_PREFIXES = ("pywin32==", "win32_setctime==")


def decode(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if len(raw) > 3 and raw[1:2] == b"\x00":
        return raw.decode("utf-16-le")
    return raw.decode("utf-8")


def main() -> None:
    src = Path("/tmp/requirements.txt")
    dst = Path("/tmp/requirements-linux.txt")
    lines = [
        line
        for line in decode(src.read_bytes()).splitlines()
        if line.strip() and not line.startswith(SKIP_PREFIXES)
    ]
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"linux requirements: {len(lines)} packages")


if __name__ == "__main__":
    main()
