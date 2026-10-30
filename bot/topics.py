"""Editing the list of feed topics: add to it, remove from it, replace it."""

from __future__ import annotations

import re

ADD_PREFIXES = ("+", "add ", "добавить ")
REMOVE_PREFIXES = ("-", "−", "del ", "remove ", "удалить ")


def parse(text: str) -> list[str]:
    """'#ии, нейросети для монтажа' -> ['#ии', 'нейросети для монтажа']."""
    return [t.strip() for t in re.split(r"[,\n;]", text or "") if t.strip()]


def _strip_prefix(text: str, prefixes: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return text[len(prefix):]
    return None


def add(current: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    """Returns (updated list, actually added). Exact duplicates (case-insensitive) are skipped."""
    seen = {t.lower() for t in current}
    added = []
    for topic in new:
        if topic.lower() not in seen:
            seen.add(topic.lower())
            added.append(topic)
    return current + added, added


def remove(current: list[str], items: list[str]) -> tuple[list[str], list[str]]:
    """Returns (updated list, actually removed). '-нейросети' also removes '#нейросети' if there is no exact match."""
    drop: set[str] = set()
    for item in items:
        exact = [t for t in current if t.lower() == item.lower()]
        loose = [t for t in current if t.lstrip("#").lower() == item.lstrip("#").lower()]
        drop.update(exact or loose)
    return [t for t in current if t not in drop], [t for t in current if t in drop]


def apply_command(current: list[str], args: str) -> tuple[list[str] | None, str]:
    """Interpret '/topics <args>'. Returns (new list or None for "back to defaults", note for the user)."""
    args = args.strip()
    if args.lower() in ("reset", "сброс"):
        return None, "Вернул темы по умолчанию."
    if (rest := _strip_prefix(args, ADD_PREFIXES)) is not None:
        if not parse(rest):
            return current, "Напишите темы после «+» через запятую."
        updated, added = add(current, parse(rest))
        note = "Добавил: " + ", ".join(added) if added else "Эти темы уже есть."
        return updated, note
    if (rest := _strip_prefix(args, REMOVE_PREFIXES)) is not None:
        if not parse(rest):
            return current, "Напишите темы после «-» через запятую."
        updated, removed = remove(current, parse(rest))
        if not removed:
            return current, "Таких тем в списке нет."
        if not updated:
            return current, "Нельзя удалить все темы — подборке нужна хотя бы одна."
        return updated, "Удалил: " + ", ".join(removed)
    new = parse(args)
    return (new, "Заменил список тем.") if new else (current, "")
