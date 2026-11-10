"""Initialize agent selectors from the saved app preference without overriding edits."""

import re
from collections.abc import MutableMapping, Sequence

_PART = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_FQN = re.compile(rf"^{_PART}\.{_PART}\.{_PART}$")


def _identity(value: str) -> tuple[str, ...] | None:
    if not _FQN.fullmatch(value):
        return None
    return tuple(
        part[1:-1].replace('""', '"') if part.startswith('"') else part.upper()
        for part in re.findall(_PART, value)
    )


def matching_agent(options: Sequence[str], preferred: str) -> str | None:
    identity = _identity(preferred)
    if identity is None:
        return None
    return next((option for option in options if _identity(option) == identity), None)


def initialize_agent_selection(
    state: MutableMapping,
    key: str,
    options: Sequence[str],
    preferred: str,
    *,
    multiple: bool = False,
) -> None:
    marker = f"{key}:default_preference"
    if marker in state and state[marker] == preferred and key in state:
        current = state[key]
        if multiple:
            retained = [
                match for value in current if (match := matching_agent(options, value)) is not None
            ]
            if current != retained:
                state[key] = retained
        elif current is not None:
            match = matching_agent(options, current)
            if current != match:
                state[key] = match
        return
    match = preferred if preferred in options else matching_agent(options, preferred)
    if multiple:
        state[key] = [match] if match else []
    else:
        state[key] = match
    state[marker] = preferred
