import re

from .models.email import Email

_REPLY_PREFIX = re.compile(r"^\s*(re|fwd|fw)\s*:\s*", re.IGNORECASE)


def group(emails: list[Email]) -> dict[int, int]:
    """Map each email id to a stable conversation id (the smallest id in its
    group). Links by Message-ID / In-Reply-To / References, with a same-subject
    fallback."""
    parent: dict[str, str] = {}

    def find(token: str) -> str:
        parent.setdefault(token, token)
        while parent[token] != token:
            parent[token] = parent[parent[token]]
            token = parent[token]
        return token

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    # Every email gets a token: its Message-ID, or a synthetic one so a message
    # with no Message-ID still stands on its own.
    tokens: dict[int, str] = {}
    for mail in emails:
        tokens[mail.id] = mail.message_id.strip() or f"eid:{mail.id}"
        find(tokens[mail.id])

    # Reference links: join a message to its parent and ancestors.
    for mail in emails:
        token = tokens[mail.id]
        if mail.in_reply_to.strip():
            union(token, mail.in_reply_to.strip())
        for ref in mail.references.split():
            union(token, ref)

    # Fallback: messages sharing a normalized subject join the same thread.
    by_subject: dict[str, str] = {}
    for mail in emails:
        subject = _normalize_subject(mail.subject)
        if not subject:
            continue
        if subject in by_subject:
            union(tokens[mail.id], by_subject[subject])
        else:
            by_subject[subject] = tokens[mail.id]

    # Each group's conversation id is the smallest email id it contains.
    root_min_id: dict[str, int] = {}
    for mail in emails:
        root = find(tokens[mail.id])
        if root not in root_min_id or mail.id < root_min_id[root]:
            root_min_id[root] = mail.id

    return {mail.id: root_min_id[find(tokens[mail.id])] for mail in emails}


def _normalize_subject(subject: str) -> str:
    text = subject.strip()
    if text.lower() == "(no subject)":
        return ""
    while True:
        stripped = _REPLY_PREFIX.sub("", text)
        if stripped == text:
            break
        text = stripped
    return text.strip().lower()
