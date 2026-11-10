"""The seed command: loading answers into the answer cache for one context."""

from __future__ import annotations

import inspect
import io
import json
import sys
from types import SimpleNamespace

import pytest

from logos.answer_reuse import seed as seed_module
from logos.answer_reuse.matching import embedding_text
from logos.answer_reuse.seed import EMBEDDING_MODEL, SeedEntry, main, owner_problem, parse_entries, seed


def _line(question="Where is my package {{Order Number}}?", intent="track_order", answer="Track it here."):
    return json.dumps({"question": question, "intent": intent, "answer": answer})


# parsing


def test_each_line_becomes_one_entry_and_blank_lines_are_ignored():
    entries = parse_entries([_line(), "", "   \n", _line("Cancel my order", "cancel_order", "Done.")])

    assert entries == [
        SeedEntry("Where is my package {{Order Number}}?", "track_order", "Track it here."),
        SeedEntry("Cancel my order", "cancel_order", "Done."),
    ]


@pytest.mark.parametrize(
    "line, message",
    [
        ("{not json", "line 2: not valid JSON"),
        ("[1, 2]", "line 2: expected a JSON object"),
        (json.dumps({"question": "Where?", "answer": "Here."}), "line 2: 'intent' must be a non-empty string"),
        (_line(answer="  "), "line 2: 'answer' must be a non-empty string"),
        (_line(question="{{Order Number}}"), "line 2: the question is empty once placeholders are removed"),
    ],
)
def test_a_bad_line_is_reported_with_its_number(line, message):
    with pytest.raises(ValueError, match=message):
        parse_entries([_line(), line])


# storing


class _Encoder:
    def __init__(self):
        self.texts = []

    def __call__(self, texts):
        self.texts.extend(texts)
        return [[0.6, 0.8, 0.0] for _ in texts]


class _Inserts:
    """Stores entries like the database: a repeated normalized question is skipped."""

    def __init__(self, present=()):
        self.rows = []
        self.present = set(present)

    def __call__(self, **row):
        key = (row["context_id"], row["question_normalized"])
        if key in self.present:
            return None
        self.present.add(key)
        self.rows.append(row)
        return len(self.rows)


def test_entries_are_embedded_like_live_questions_and_stored_as_seed():
    encoder, inserts = _Encoder(), _Inserts()
    entry = SeedEntry("Where is my package {{Order Number}}?", "track_order", "Track {{Order Number}} here.")

    assert seed([entry], "team:3:shop-a", encoder, inserts) == (1, 0)

    assert encoder.texts == [embedding_text(entry.question)]
    assert inserts.rows == [
        {
            "context_id": "team:3:shop-a",
            "question_raw": "Where is my package {{Order Number}}?",
            "question_normalized": "Where is my package ?",
            "embedding": [0.6, 0.8, 0.0],
            "intent": "track_order",
            "answer_template": "Track {{Order Number}} here.",
            "source": "seed",
        }
    ]


def test_questions_already_stored_are_counted_not_stored_again():
    inserts = _Inserts(present={("team:3:shop-a", "Cancel my order")})
    entries = [SeedEntry("Cancel my order", "cancel_order", "Done."), SeedEntry("Where is it?", "track_order", "Here.")]

    assert seed(entries, "team:3:shop-a", _Encoder(), inserts) == (1, 1)


def test_no_entries_loads_no_model():
    encoder = _Encoder()

    assert seed([], "team:3:shop-a", encoder, _Inserts()) == (0, 0)
    assert encoder.texts == []


def test_the_embedding_model_is_the_one_the_classifier_uses():
    from logos.classification.laura_embedding_classifier import LauraEmbeddingClassifier

    default = inspect.signature(LauraEmbeddingClassifier.__init__).parameters["model_name"].default
    assert EMBEDDING_MODEL == default


# the owner


class _FakeDB:
    def __init__(self, teams=(3,), keys=None):
        self.teams = set(teams)
        self.keys = keys if keys is not None else {10: {"id": 10, "team_id": None}, 11: {"id": 11, "team_id": 3}}
        self.inserts = _Inserts()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_team(self, team_id):
        return {"id": team_id} if team_id in self.teams else None

    def get_api_key_by_id(self, key_id):
        return self.keys.get(key_id)

    def insert_answer_cache_entry(self, **row):
        return self.inserts(**row)


@pytest.mark.parametrize(
    "team_id, key_id, problem",
    [
        (3, None, None),
        (4, None, "there is no team 4"),
        (None, 10, None),
        (None, 12, "there is no active API key 12"),
        (None, 11, "API key 11 belongs to team 3, so its requests use the team's answers; use --team-id 3"),
    ],
)
def test_only_an_owner_whose_requests_can_reach_the_answers_is_accepted(team_id, key_id, problem):
    assert owner_problem(_FakeDB(), team_id, key_id) == problem


# the command


def _run(argv, stdin_text="", db=None):
    db = db or _FakeDB()
    code = main(argv, stdin=io.StringIO(stdin_text), encode=_Encoder(), open_database=lambda: db)
    return code, db


def test_the_command_loads_stdin_for_a_team_context(capsys):
    code, db = _run(["--team-id", "3", "--context", "shop-a"], "\n".join([_line(), _line("Cancel my order")]))

    assert code == 0
    assert [row["context_id"] for row in db.inserts.rows] == ["team:3:shop-a", "team:3:shop-a"]
    assert capsys.readouterr().out.strip() == "team:3:shop-a: stored 2, already present 0"


def test_the_command_reads_a_file_for_a_key_context(tmp_path, capsys):
    path = tmp_path / "answers.jsonl"
    path.write_text(_line() + "\n", encoding="utf-8")

    code, db = _run(["--key-id", "10", "--context", " shop-a ", str(path)])

    assert code == 0
    assert db.inserts.rows[0]["context_id"] == "key:10:shop-a"
    assert "key:10:shop-a: stored 1" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv, stdin_text, message",
    [
        (["--team-id", "3", "--context", "   "], "", "--context must not be blank"),
        (["--team-id", "3", "--context", "shop\na"], "", "control character"),
        (["--team-id", "3", "--context", "shop-a"], "{not json", "line 1: not valid JSON"),
        (["--team-id", "3", "--context", "shop-a", "/no/such/file.jsonl"], "", "No such file"),
        (["--team-id", "4", "--context", "shop-a"], _line(), "there is no team 4"),
        (["--key-id", "11", "--context", "shop-a"], _line(), "use --team-id 3"),
    ],
)
def test_the_command_stores_nothing_when_its_input_is_unusable(argv, stdin_text, message, capsys):
    code, db = _run(argv, stdin_text)

    assert code == 2
    assert db.inserts.rows == []
    assert message in capsys.readouterr().err


def test_the_command_needs_exactly_one_owner():
    with pytest.raises(SystemExit):
        _run(["--context", "shop-a"])
    with pytest.raises(SystemExit):
        _run(["--team-id", "3", "--key-id", "10", "--context", "shop-a"])


# the real model and database, replaced


def test_the_model_embeds_normalized_vectors(monkeypatch):
    calls = []

    class _Model:
        def __init__(self, name):
            calls.append(("load", name))

        def encode(self, texts, **options):
            calls.append(("encode", texts, options))
            return [[1.0, 0.0]]

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=_Model))

    assert seed_module._encode_with_model(["query: Where is it?"]) == [[1.0, 0.0]]
    assert calls == [
        ("load", EMBEDDING_MODEL),
        ("encode", ["query: Where is it?"], {"normalize_embeddings": True, "show_progress_bar": False}),
    ]


def test_the_database_is_the_orchestrator_database(monkeypatch):
    from logos.dbutils import dbmanager

    monkeypatch.setattr(dbmanager, "DBManager", lambda: "the database")

    assert seed_module._open_database() == "the database"
