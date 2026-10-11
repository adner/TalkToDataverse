"""Word-level finalization diff used by UtteranceFinalized."""

from __future__ import annotations

from difflib import SequenceMatcher

from .contracts import Revision, Word


def diff_words(
    streamed: tuple[Word, ...], final: tuple[Word, ...]
) -> tuple[int, tuple[Revision, ...]]:
    prefix = 0
    for left, right in zip(streamed, final, strict=False):
        if _normalized(left.w) != _normalized(right.w):
            break
        prefix += 1

    matcher = SequenceMatcher(
        a=[_normalized(word.w) for word in streamed],
        b=[_normalized(word.w) for word in final],
        autojunk=False,
    )
    revisions: list[Revision] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        replacement = final[new_start:new_end]
        if tag == "replace":
            revisions.append(
                Revision(op="replace", start=old_start, end=old_end, words=replacement)
            )
        elif tag == "delete":
            revisions.append(Revision(op="delete", start=old_start, end=old_end))
        else:
            revisions.append(Revision(op="insert", at=old_start, words=replacement))
    return prefix, tuple(revisions)


def _normalized(value: str) -> str:
    return value.casefold().strip()
