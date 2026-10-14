"""Public-repo hygiene: no private names, local paths or real emails in any file.

Forbidden words are assembled from parts so this file does not match itself.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = {
    "local user path": re.compile("/Us" + "ers/"),
    "home directory path": re.compile("/ho" + "me/[A-Za-z]"),
    "private KB reference": re.compile("K" + "B-[0-9]+"),
    "private project name": re.compile("observe" + "ai", re.I),
    "demo project name": re.compile("defense" + "demo", re.I),
    "tenancy name": re.compile("em" + "demo", re.I),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
ALLOWED_EMAIL_DOMAINS = re.compile(
    r"(^|\.)example\.(com|org|net)$|\.(invalid|test|localhost|example)$|(^|\.)noreply\.|^system\.gserviceaccount\.com$"
)
ALLOWED_EMAIL_LOCALS = {"noreply", "no-reply"}
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


def _files():
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    for rel in out:
        path = ROOT / rel
        if rel.startswith(".venv/") or not path.is_file():
            continue
        yield rel, path


def _text(path):
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _email_is_allowed(match):
    domain = match.group(1).lower()
    local = match.group(0).split("@")[0].lower()
    if domain.endswith(IMAGE_SUFFIXES):
        return True
    return local in ALLOWED_EMAIL_LOCALS or bool(ALLOWED_EMAIL_DOMAINS.search(domain))


def test_no_private_terms_in_tracked_files():
    hits = []
    for rel, path in _files():
        text = _text(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern in FORBIDDEN.items():
                if pattern.search(line):
                    hits.append(f"{rel}:{lineno}: {name}")
    assert not hits, "private terms found (values not printed):\n" + "\n".join(hits)


def test_no_real_email_addresses():
    hits = []
    for rel, path in _files():
        text = _text(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(not _email_is_allowed(m) for m in EMAIL.finditer(line)):
                hits.append(f"{rel}:{lineno}")
    assert not hits, "email addresses outside example/noreply domains:\n" + "\n".join(hits)
