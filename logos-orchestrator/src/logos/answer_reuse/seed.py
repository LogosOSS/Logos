"""Load answers into the answer cache for one reuse context.

Answer reuse only answers questions it has stored answers for, and a new
installation has none. This command stores a file of labelled questions and
their answers for one context, owned by a team or by a single API key, the
same owner a request's ``logos-context`` is scoped to.

The file is JSON Lines, one question per line::

    {"question": "Where is my package?", "intent": "track_order", "answer": "Track it: {{Order Tracking URL}}."}

Run it inside the orchestrator container, reading the file from stdin::

    docker exec -i logos-orchestrator python -m logos.answer_reuse.seed --team-id 3 --context shop-a < answers.jsonl

The owner is checked first: answers seeded for a team or key that does not
exist, or for a key that belongs to a team (whose requests use the team's
answers), could never be found. A question already stored for the context
is skipped, so loading a file twice is harmless. A running orchestrator sees new answers within
``CACHE_TTL_SECONDS``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Optional, Sequence, TextIO

from logos.answer_reuse.context import CONTEXT_HEADER, cache_scope, context_id_from_headers
from logos.answer_reuse.matching import embedding_text, normalize

# The model the classifier embeds requests with. Stored questions must be
# embedded by the same model, or similarities between them mean nothing.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"


@dataclass(frozen=True)
class SeedEntry:
    """One labelled question and the answer to reuse for it."""

    question: str
    intent: str
    answer: str


def parse_entries(lines: Iterable[str]) -> List[SeedEntry]:
    """Read seed entries from JSON Lines; blank lines are ignored.

    Raises:
        ValueError: A line is not a JSON object with non-empty string fields
            ``question``, ``intent`` and ``answer``, or its question is empty
            once placeholders are removed. The message names the line.
    """
    entries = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {number}: not valid JSON ({exc.msg})") from None
        if not isinstance(item, dict):
            raise ValueError(f"line {number}: expected a JSON object")
        for field in ("question", "intent", "answer"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"line {number}: '{field}' must be a non-empty string")
        if not normalize(item["question"]):
            raise ValueError(f"line {number}: the question is empty once placeholders are removed")
        entries.append(SeedEntry(question=item["question"], intent=item["intent"].strip(), answer=item["answer"]))
    return entries


def seed(
    entries: Sequence[SeedEntry],
    scope: str,
    encode: Callable[[List[str]], Sequence[Sequence[float]]],
    insert: Callable[..., Optional[int]],
) -> tuple[int, int]:
    """Embed the questions and store each entry under ``scope``.

    Params:
        entries: The entries to store.
        scope: The owner-qualified scope, as :func:`cache_scope` builds it.
        encode: Embeds a list of texts into normalized vectors.
        insert: Stores one entry; takes the keyword arguments of
            :meth:`DBManager.insert_answer_cache_entry`.

    Returns:
        How many entries were stored and how many were already there.
    """
    if not entries:
        return 0, 0
    vectors = encode([embedding_text(entry.question) for entry in entries])
    stored = 0
    for entry, vector in zip(entries, vectors):
        entry_id = insert(
            context_id=scope,
            question_raw=entry.question,
            question_normalized=normalize(entry.question),
            embedding=[float(value) for value in vector],
            intent=entry.intent,
            answer_template=entry.answer,
            source="seed",
        )
        if entry_id is not None:
            stored += 1
    return stored, len(entries) - stored


def _arguments(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m logos.answer_reuse.seed",
        description="Load answers into the answer cache for one reuse context.",
    )
    owner = parser.add_mutually_exclusive_group(required=True)
    owner.add_argument("--team-id", type=int, help="The team whose API keys may reuse the answers.")
    owner.add_argument("--key-id", type=int, help="The API key that may reuse the answers (for keys without a team).")
    parser.add_argument("--context", required=True, help="The logos-context value requests will send.")
    parser.add_argument("file", nargs="?", default="-", help="JSON Lines file; '-' or omitted reads stdin.")
    return parser.parse_args(argv)


def _encode_with_model(texts: List[str]) -> Sequence[Sequence[float]]:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBEDDING_MODEL)
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


def _open_database():
    from logos.dbutils.dbmanager import DBManager

    return DBManager()


def owner_problem(db: Any, team_id: Optional[int], key_id: Optional[int]) -> Optional[str]:
    """Why answers stored for this owner would never be found, or None.

    A request's answers are scoped to its key's team, and to the key itself
    only when the key has no team. Seeding a key that belongs to a team, or
    an owner that does not exist, would store answers no request can reach.
    """
    if team_id is not None:
        if db.get_team(team_id) is None:
            return f"there is no team {team_id}"
        return None
    key = db.get_api_key_by_id(key_id)
    if key is None:
        return f"there is no active API key {key_id}"
    if key.get("team_id") is not None:
        return (
            f"API key {key_id} belongs to team {key['team_id']}, so its requests use the team's answers; "
            f"use --team-id {key['team_id']}"
        )
    return None


def main(
    argv: Optional[Sequence[str]] = None,
    stdin: TextIO = sys.stdin,
    encode: Callable[[List[str]], Sequence[Sequence[float]]] = _encode_with_model,
    open_database: Callable[[], Any] = _open_database,
) -> int:
    """Run the command; returns the process exit code."""
    arguments = _arguments(argv)
    try:
        context_id = context_id_from_headers({CONTEXT_HEADER: arguments.context})
        if context_id is None:
            raise ValueError("--context must not be blank")
        if arguments.file == "-":
            entries = parse_entries(stdin)
        else:
            with open(arguments.file, encoding="utf-8") as handle:
                entries = parse_entries(handle)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    scope = cache_scope(context_id, arguments.team_id, arguments.key_id)
    with open_database() as db:
        problem = owner_problem(db, arguments.team_id, arguments.key_id)
        if problem is not None:
            print(f"error: {problem}", file=sys.stderr)
            return 2
        stored, skipped = seed(entries, scope, encode, db.insert_answer_cache_entry)
    print(f"{scope}: stored {stored}, already present {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
