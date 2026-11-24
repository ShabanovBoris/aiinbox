import argparse
import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.config import Settings
from app.domain.enums import SourceType
from app.domain.language import response_language_for
from app.domain.models import AnalysisResult, NormalizedContent, UserProfile
from app.domain.signals import CLAIM_CLAUSE_BREAK, clause_before, has_multiple_sources
from app.llm.base import LlmError, LlmProvider
from app.main import build_provider
from app.services.analysis import (
    analysis_signal_projection,
    analysis_text_projection,
    bounded_chunk_aggregate,
    ensure_explicit_inconclusive_title,
    split_text,
)
from app.services.retrieval import rebuild_user_search_index
from app.storage.database import make_engine, make_session_factory
from app.storage.models import User

log = logging.getLogger(__name__)


def sqlite_database_path(database_url: str) -> Path:
    """Resolve the canonical SQLite file used by operational commands.

    Operations intentionally reject non-file databases: backup/restore tooling must
    act on the same durable SQLite source of truth as the application.
    """
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("DATABASE_URL must point to a file-backed SQLite database")
    return Path(url.database)


def verify_database(path: Path) -> None:
    """Validate a SQLite file before it is trusted as a backup or restore source.

    Full integrity_check is kept out of the hot application path and belongs to
    explicit maintenance, where a slower exhaustive check is acceptable.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"SQLite integrity_check failed for {path}: {result!r}")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    if foreign_key_errors:
        raise RuntimeError(
            f"SQLite foreign_key_check failed for {path}: {foreign_key_errors[:10]!r}"
        )


def backup_database(source: Path, destination: Path) -> None:
    """Create a consistent online SQLite snapshot without stopping workers.

    sqlite3.Connection.backup() is the storage boundary designed for live SQLite
    databases and includes committed WAL state in one consistent destination.
    """
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    failed = False
    try:
        source_connection.backup(destination_connection)
    except BaseException:
        failed = True
        raise
    finally:
        destination_connection.close()
        source_connection.close()
        if failed:
            destination.unlink(missing_ok=True)
    try:
        verify_database(destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def sha256_file(path: Path) -> str:
    """Return a stable content fingerprint for an exported backup artifact."""
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_backup_checksum(backup: Path) -> Path:
    """Write a standard sha256sum sidecar used to verify off-host round trips."""
    checksum = Path(f"{backup}.sha256")
    if checksum.exists():
        raise FileExistsError(checksum)
    checksum.write_text(f"{sha256_file(backup)}  {backup.name}\n", encoding="ascii")
    return checksum


def create_backup_generation(source: Path, destination: Path) -> Path:
    """Create one verified DB snapshot plus its independently transferable checksum."""
    checksum = Path(f"{destination}.sha256")
    if checksum.exists():
        raise FileExistsError(checksum)
    backup_database(source, destination)
    try:
        return write_backup_checksum(destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)
        raise


def verify_backup_copy(database: Path, checksum_file: Path) -> None:
    """Verify transferred bytes before trusting the SQLite-level validation."""
    if not checksum_file.is_file():
        raise FileNotFoundError(checksum_file)
    parts = checksum_file.read_text(encoding="ascii").strip().split(maxsplit=1)
    if len(parts) != 2:
        raise RuntimeError(f"Invalid checksum file: {checksum_file}")
    expected, filename = parts
    if len(expected) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected):
        raise RuntimeError(f"Invalid SHA-256 digest in {checksum_file}")
    if filename != database.name:
        raise RuntimeError(
            f"Checksum file {checksum_file} names {filename!r}, expected {database.name!r}"
        )
    actual = sha256_file(database)
    if actual.lower() != expected.lower():
        raise RuntimeError(
            f"SHA-256 mismatch for {database}: expected {expected.lower()}, got {actual}"
        )
    verify_database(database)


def restore_database(backup: Path, target: Path) -> None:
    """Restore into a new SQLite file so production replacement stays explicit.

    The target must not exist; operators can verify the restored file before
    swapping it into the canonical DATABASE_URL path while the app is stopped.
    """
    verify_database(backup)
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup_connection = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    failed = False
    try:
        backup_connection.backup(target_connection)
    except BaseException:
        failed = True
        raise
    finally:
        target_connection.close()
        backup_connection.close()
        if failed:
            target.unlink(missing_ok=True)
    try:
        verify_database(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def rotate_backups(directory: Path, keep: int) -> list[Path]:
    """Retain only the newest verified backup generations.

    Rotation is intentionally filename-scoped to aiinbox-*.db so maintenance
    cannot delete unrelated files placed in the same volume.
    """
    if keep < 1:
        raise ValueError("keep must be >= 1")
    backups = sorted(
        directory.glob("aiinbox-*.db"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    removed = backups[keep:]
    for path in removed:
        path.unlink()
        Path(f"{path}.sha256").unlink(missing_ok=True)
    return removed


def database_status(path: Path) -> dict[str, int]:
    """Read cheap operational counters directly from canonical SQLite state.

    This is deliberately independent from Telegram and worker objects so the same
    status command remains useful during deployment and recovery.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        queued, processing, failed = connection.execute(
            """
            SELECT
              COALESCE(SUM(processing_status = 'QUEUED'), 0),
              COALESCE(SUM(processing_status = 'PROCESSING'), 0),
              COALESCE(SUM(processing_status = 'FAILED'), 0)
            FROM items
            """
        ).fetchone()
        pending_deliveries = connection.execute(
            "SELECT COUNT(*) FROM deliveries WHERE status IN ('PENDING', 'SENDING')"
        ).fetchone()[0]
        ask_pending, ask_running, ask_failed = connection.execute(
            """
            SELECT
              COALESCE(SUM(status = 'PENDING'), 0),
              COALESCE(SUM(status = 'RUNNING'), 0),
              COALESCE(SUM(status = 'FAILED'), 0)
            FROM ask_jobs
            """
        ).fetchone()
        ask_failed_deliveries = connection.execute(
            """
            SELECT COUNT(*) FROM deliveries
            WHERE type = 'ASK_FAILED' AND status IN ('PENDING', 'SENDING', 'FAILED')
            """
        ).fetchone()[0]
        connection.execute("SELECT 1").fetchone()
    finally:
        connection.close()
    return {
        "queued": int(queued),
        "processing": int(processing),
        "failed": int(failed),
        "pending_deliveries": int(pending_deliveries),
        "ask_pending": int(ask_pending),
        "ask_running": int(ask_running),
        "ask_failed": int(ask_failed),
        "ask_failed_deliveries": int(ask_failed_deliveries),
        "bytes": path.stat().st_size,
    }


async def rebuild_search(settings: Settings) -> int:
    """Rebuild derived FTS rows from canonical Item/Content data.

    The operation reuses the production retrieval service, so maintenance cannot
    drift from the indexing semantics used by /search.
    """
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    try:
        async with session_factory() as session:
            user_ids = list((await session.scalars(select(User.id))).all())
            for user_id in user_ids:
                await rebuild_user_search_index(session, user_id)
            await session.commit()
        return len(user_ids)
    finally:
        await engine.dispose()


async def smoke_external(settings: Settings) -> tuple[str, str]:
    """Exercise the configured LLM and Telegram adapters against real services.

    This command is intentionally opt-in because it performs live network calls
    and may incur a small provider charge; it is meant to run after deployment.
    """
    provider = build_provider(settings)
    summary = (await provider.summarize_chunk("Return a one-word acknowledgement.")).strip()
    if not summary:
        raise RuntimeError("LLM smoke returned an empty response")
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    from aiogram import Bot

    bot = Bot(settings.telegram_bot_token)
    try:
        identity = await bot.get_me()
    finally:
        await bot.session.close()

    model = (
        settings.openrouter_analysis_model
        if settings.llm_provider == "openrouter"
        else settings.openai_analysis_model
    )
    return model, identity.username or str(identity.id)


QUALITY_FIXTURE_VERSION = 3
_CYRILLIC_LETTER = re.compile(r"[А-Яа-яЁё]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")
# Meta openers describe the container instead of delivering the answer. They are
# forbidden in both title and summary, whatever the fixture case.
_META_ANSWER_OPENERS = (
    "this item covers",
    "this item combines",
    "this item contains",
    "this item includes",
    "this item presents",
    "these two sources",
    "this is a comparison of",
    "this material covers",
    "this material combines",
    # The model prompt forbids announcing the medium; the evaluator must enforce
    # the same contract instead of letting such a summary pass.
    "the article examines",
    "the article discusses",
    "the article covers",
    "the article is about",
    "the article explains",
    "the essay examines",
    "the essay is about",
    "the video discusses",
    "the video examines",
    "the video explains",
    "the video is about",
    "the post examines",
    "the post discusses",
    "the post is about",
    "the author recounts",
    "the author discusses",
    "the author examines",
    "the author explains",
    "данный материал",
    "этот материал",
    "данный элемент",
    "этот элемент",
    "это сравнение",
    "автор рассказывает",
    "автор объясняет",
    "автор обсуждает",
    "в видео рассматривается",
    "в видео рассказывается",
    "статья посвящена",
    "статья рассматривает",
    "материал посвящен",
    "видео обсуждает",
)
_OUTCOME_STATES = frozenset(
    {"positive", "negative", "mixed", "recommendation", "thesis", "inconclusive", "none"}
)
_ALLOWED_EXPECTED_KEYS = frozenset(
    {
        "category",
        "category_aliases",
        "forbidden_categories",
        "outcome_state",
        "outcome_terms",
        "title_evidence_groups",
        "title_forbidden_terms",
        "title_forbidden_polarity",
        "summary_evidence_groups",
        "summary_adds_evidence",
        "summary_forbidden_terms",
        "forbidden_summary_prefixes",
        "answer_forbidden_terms",
        "max_goal_fit",
        "must_not_claim_winner",
        "winner_forbidden_terms",
        "must_represent_each_source",
        "distinct_topic_groups",
        "must_ignore_content_instructions",
        "must_ignore_profile_instructions",
        "user_note_may_affect_relevance_not_topic",
    }
)
_ALLOWED_CASE_KEYS = frozenset(
    {
        "id",
        "source_type",
        "source_title",
        "content",
        "source_language",
        "response_language",
        "user_note",
        "profile",
        "existing_categories",
        "metadata",
        "chunk_size_chars",
        "chunk_overlap_chars",
        "known_variable",
        "expected",
    }
)


@dataclass(frozen=True, slots=True)
class QualityCaseReport:
    """One opt-in evaluation result; lexical checks never replace human review."""

    case_id: str
    title: str
    summary: str
    category: str
    language: str
    goal_fit: float
    failed_checks: tuple[str, ...]
    title_chars: int
    chunk_count: int
    known_variable: bool


# Lexical evidence checks must not fail a correct answer over punctuation style.
# Contractions are expanded so "didn't shorten time" matches "did not shorten time"
# without weakening the other alternatives in the group.
_MATCH_APOSTROPHE = str.maketrans({"’": "'", "‘": "'", "`": "'"})
_MATCH_CONTRACTIONS = {
    "ain't": "is not",
    "can't": "can not",
    "cannot": "can not",
    "couldn't": "could not",
    "didn't": "did not",
    "doesn't": "does not",
    "don't": "do not",
    "hadn't": "had not",
    "hasn't": "has not",
    "haven't": "have not",
    "isn't": "is not",
    "mustn't": "must not",
    "shouldn't": "should not",
    "wasn't": "was not",
    "weren't": "were not",
    "won't": "will not",
    "wouldn't": "would not",
}
_MATCH_CONTRACTION_RE = re.compile(
    r"\b(?:" + "|".join(map(re.escape, _MATCH_CONTRACTIONS)) + r")\b",
    re.IGNORECASE,
)
# "18 ms" and "18ms" are the same measurement, not different evidence.
_MATCH_UNIT_GAP = re.compile(
    r"(?<=\d)[ \t]+(?=(?:ms|s|sec|secs|second|seconds|min|mins|minute|minutes|"
    r"hour|hours|h|x)\b)"
)
_MATCH_UNIT_WORDS = {
    "millisecond": "ms",
    "milliseconds": "ms",
    "second": "s",
    "seconds": "s",
    "minute": "min",
    "minutes": "min",
    "hour": "h",
    "hours": "h",
}
_MATCH_UNIT_WORD_RE = re.compile(
    r"(?<=\d)\s+(milliseconds?|seconds?|minutes?|hours?)\b", re.IGNORECASE
)


def _match_normalize(text: str) -> str:
    folded = " ".join(text.casefold().translate(_MATCH_APOSTROPHE).replace("ё", "е").split())
    expanded = _MATCH_CONTRACTION_RE.sub(lambda match: _MATCH_CONTRACTIONS[match.group(0)], folded)
    with_units = _MATCH_UNIT_WORD_RE.sub(
        lambda match: " " + _MATCH_UNIT_WORDS[match.group(1)], expanded
    )
    return _MATCH_UNIT_GAP.sub("", with_units)


def _match_any(text: str, alternatives: list[str]) -> str | None:
    normalized = _match_normalize(text)
    return next(
        (term for term in alternatives if _match_normalize(term) in normalized),
        None,
    )


def _has_asserted_match(normalized_text: str, term: str) -> bool:
    """Return whether the term occurs at least once outside a negated clause."""
    needle = _match_normalize(term)
    position = normalized_text.find(needle)
    while position != -1:
        if not _negated_at(normalized_text, position, position + len(needle)):
            return True
        position = normalized_text.find(needle, position + 1)
    return False


def _contains_asserted_term(text: str, term: str) -> bool:
    """Check a user-visible field for a forbidden term stated as a claim.

    "No unified conclusion" and "neither source supports a shared cause" are the
    correct answers, so only an asserted occurrence counts as a violation.
    """
    return _has_asserted_match(_match_normalize(text), term)


def _match_any_positive(text: str, alternatives: list[str]) -> str | None:
    """Match an outcome term only when the clause does not negate it.

    A negated statement is the opposite result: "SQLite is not sufficient",
    "small slices do not catch design errors", and "release time did not fall to
    three days" must not satisfy a positive outcome expectation merely because the
    negated phrase contains its positive substring.
    """
    normalized = _match_normalize(text)
    return next(
        (term for term in alternatives if _has_asserted_match(normalized, term)),
        None,
    )


def _category_matches(actual: str, expected: str) -> bool:
    """Accept a reused label or a more specific label that contains the expected topic."""
    actual_normalized = _normalize(actual)
    expected_normalized = _normalize(expected)
    if not actual_normalized or not expected_normalized:
        return False
    if len(expected_normalized) < 4:
        # Short labels such as “AI” or “ИИ” would otherwise match unrelated
        # substrings such as “training”; accept them only as whole words.
        return (
            re.search(rf"(?<!\w){re.escape(expected_normalized)}(?!\w)", actual_normalized)
            is not None
        )
    return (
        actual_normalized == expected_normalized
        or expected_normalized in actual_normalized
        or actual_normalized in expected_normalized
    )


def _category_matches_any(actual: str, expected: str, aliases: list[str]) -> bool:
    """Check the label against a single expected topic or a merged multi-topic one.

    A single-topic expectation keeps the containment rule: live labels add
    descriptive tails (“AI and automation in customer support”), which are not
    separate topics. A multi-topic expectation (`holdout-independent-sources`
    merges two equally weighted topics) accepts only a label whose every topic is
    accepted, because such a label matches an alias through its first part alone
    (``Baking and Infrastructure``) and would confirm a hallucinated category.
    """
    candidates = [expected, *aliases]
    if not any(_category_matches(actual, candidate) for candidate in candidates):
        return False
    expected_parts = _category_parts(_normalize(expected), _EXPECTED_TOPIC_SEPARATOR)
    if len(expected_parts) < 2:
        return True
    parts = _category_parts(_normalize(actual))
    if len(parts) < 2:
        return True
    return all(
        any(_category_matches(part, candidate) for candidate in candidates) for part in parts
    )


# A classifier label separates topics with a coordinator or punctuation, and live
# labels also compress them with a slash, plus or pipe, so the actual label is split
# on all of them. An expectation declares that it merges topics with a coordinator
# only, because one concept may contain a slash (`CI/CD`) and a bare space never
# separates topics (“customer support” is one topic).
_TOPIC_SEPARATOR = re.compile(r"\s+(?:и|and|&)\s+|\s*[,;|/+]\s*")
_EXPECTED_TOPIC_SEPARATOR = re.compile(r"\s+(?:и|and|&)\s+|\s*[,;]\s*")


def _label_contains(needle: str, haystack: str) -> bool:
    """Substring containment, with whole-word matching for labels shorter than 4 chars."""
    if len(needle) < 4:
        return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None
    return needle in haystack


def _category_parts(label: str, separator: re.Pattern[str] = _TOPIC_SEPARATOR) -> list[str]:
    return [part.strip() for part in separator.split(label) if part.strip()]


def _category_is_forbidden(actual: str, forbidden: str) -> bool:
    """Fail when the actual label is the forbidden label, a broader label, or its topic.

    ``Android`` is the same topic as forbidden ``Android-разработка``, so it fails.
    A combined multi-topic label such as ``Kotlin и музыка`` is narrower than its
    individual parts: actual ``Kotlin`` names one topic and must not fail, while
    actual ``Kotlin и музыка`` and broader labels containing it do fail. An actual
    multi-topic label is checked per part, so ``Kotlin и Android`` claims the
    forbidden ``Android-разработка`` topic through its Android part.
    """
    actual_normalized = _normalize(actual)
    forbidden_normalized = _normalize(forbidden)
    if not actual_normalized or not forbidden_normalized:
        return False
    if actual_normalized == forbidden_normalized:
        return True
    if _label_contains(forbidden_normalized, actual_normalized):
        # The actual label is broader and contains the forbidden label verbatim.
        return True
    actual_parts = _category_parts(actual_normalized)
    forbidden_parts = _category_parts(forbidden_normalized)
    if len(forbidden_parts) > 1:
        if len(actual_parts) == 1 and actual_normalized in forbidden_parts:
            # Only one topic of a combined label: the label is not claimed.
            return False
        if len(actual_parts) > 1 and all(
            any(
                part == forbidden_part or _label_contains(part, forbidden_part)
                for part in actual_parts
            )
            for forbidden_part in forbidden_parts
        ):
            # The actual multi-topic label contains every forbidden topic.
            return True
        return _label_contains(actual_normalized, forbidden_normalized)
    return any(
        part == forbidden_normalized
        or _label_contains(part, forbidden_normalized)
        or _label_contains(forbidden_normalized, part)
        for part in actual_parts
    )


def _language_matches(text: str, language: str) -> bool:
    """Detect the response language script; a human still reviews the actual prose.

    The fixture layer cannot distinguish English from German lexically, but it can
    reject the wrong script. German/Spanish cases must not silently accept a
    Cyrillic answer (and vice versa); the evidence groups remain the semantic check.
    """
    cyrillic = len(_CYRILLIC_LETTER.findall(text))
    latin = len(_LATIN_LETTER.findall(text))
    primary = language.split("-")[0].casefold()
    if primary in {"ru", "uk", "bg", "be", "sr", "mk"}:
        return cyrillic > 0 and cyrillic >= latin
    return latin > 0 and cyrillic == 0


def _normalize(text: str) -> str:
    # ё/е are interchangeable in Russian category labels and evidence terms.
    return " ".join(text.casefold().replace("ё", "е").split()).rstrip(".")


# A negated phrase is the opposite of the claim it contains: “preloading does not
# improve accuracy” must pass an outcome check, and “no unified conclusion” must
# pass a forbidden-term check. “not only”/“не только” amplifies a claim, so a
# negator immediately followed by only/just/merely does not negate anything.
# The claim boundary is shared with the prompt guard and the repair so all three
# agree on which clause a negation or outcome belongs to.
_POLARITY_NEGATION = re.compile(
    r"(?i)\b(?:not|never|no|none|neither|nor|nobody|nothing|without|hardly|scarcely|"
    r"fail(?:s|ed|ing)?\s+to|did\s+not|does\s+not|do\s+not|"
    r"isn't|wasn't|weren't|aren't|doesn't|don't|didn't|hasn't|haven't|hadn't|"
    r"cannot|can't|couldn't|won't|wouldn't|rather\s+than|instead\s+of|не|нет|без|никогда|ни|вместо)\b"
    # "not only a unified conclusion" and "не только общий вывод" assert the term.
    r"(?!\s*(?:only|just|merely|только|просто|лишь)\b)"
)
_POLARITY_FOLD = str.maketrans({"ё": "е", "Ё": "Е"})


def _fold_polarity(text: str) -> str:
    return text.casefold().translate(_POLARITY_FOLD)


# A negator only cancels the clause that contains it. The shared boundary splits
# sentence punctuation and coordinators, but prose also starts an independent
# claim after a dash or a parenthetical aside: “The evidence is not conclusive —
# caching improves quality” asserts the part after the dash. Those separators stay
# local to the evaluation because the shared boundary also drives the no-winner
# detector and the repair, which are not part of this check.
_NEGATION_SCOPE_BREAK = re.compile(r"[—–]|\s-\s|[()]")

# Some negators assert rather than negate: “There is no doubt that …”, “Nobody
# denies that …” and “Neither run disputes that …” state the following claim, and so
# does “Without exception …” in the bare “X is certain” sense. The frame is blanked
# before the scan (same length, so offsets stay valid) and cannot hide a term.
_ASSERTING_NEGATION_FRAME = re.compile(
    r"(?i)\b(?:"
    r"there\s+(?:is|can\s+be)\s+no\s+(?:doubt|question)"
    r"|there\s+is\s+little\s+doubt(?:\s+that)?"
    r"|there\s+is\s+no\s+(?:denying|doubting|disputing|questioning)(?:\s+that)?"
    r"|it\s+(?:cannot|can\s+not|can't)\s+be\s+(?:denied|disputed|questioned|doubted)"
    r"(?:\s+that)?"
    r"|(?:nobody|no\s+one)\s+(?:(?:can|could|would|will|may|might|must|should)\s+)?"
    r"(?:\w+ly\s+)?(?:den(?:y|ies|ied)|disputes?|disputed|questions?|questioned|"
    r"doubts?|doubted)(?:\s+that)?"
    r"|neither(?:\s+of)?(?:\s+\w+){0,5}?\s+(?:den(?:y|ies|ied)|disputes?|disputed|"
    r"questions?|questioned|doubts?|doubted)(?:\s+that)?"
    r"|no\s+(?:doubt|question)\s+that"
    r"|without\s+(?:a\s+)?doubt"
    r")\b"
)

# “Without”/«без» deny their own noun phrase, not a following clause: “without a
# unified conclusion” negates the term, while “Without exception caching improves
# quality” and “Without extra tooling the two sources share a shared cause” start a
# new clause and assert it. The complement ends where English word order shows a new
# clause: a determiner after a head noun, or a finite predicate after its subject.
# Word count cannot separate the two shapes — both “without a unified conclusion” and
# “without exception caching improves quality” place the term two words after the
# preposition.
_WORD_TOKEN = re.compile(r"[^\W\d_][\w'-]*")
_NP_DETERMINER = re.compile(
    r"(?i)(?:a|an|the|any|some|no|every|each|all|both|this|that|these|those|"
    r"it|they|we|you|he|she|i|its|their|our|my|your|his|her|few|many|most|"
    r"several|another|such|either|neither|one|two|three|four|five)"
)
# Tokens that introduce or continue a subordinate phrase, so the word after them is
# not the start of a new independent clause.
_NP_LINK = re.compile(
    r"(?i)(?:of|for|in|on|at|to|with|about|from|by|into|over|under|between|among|"
    r"as|than|per|via|during|against|within|across|through|after|before|despite|"
    r"except|including|without|and|or|but|not|never|very|clearly|fully|only|just|"
    r"merely|also|still|even|more|less|most|much|so|too|well)"
)
_FINITE_AUXILIARY = re.compile(
    r"(?i)(?:is|are|was|were|am|be|been|being|has|have|had|does|do|did|"
    r"can|could|will|would|shall|should|may|might|must)"
)
# Base and third-person forms of the predicates that appear in evidence sentences.
# Past-tense forms need the shape “shared a cause” to be told apart from the
# participle in “a shared cause”, so they are handled separately below.
_FINITE_VERB = re.compile(
    r"(?i)(?:improv\w*|increas\w*|boost\w*|rais\w*|reduc\w*|lower\w*|cut|cuts|"
    r"decreas\w*|enhanc\w*|hurt|hurts|worsen\w*|lead|leads|result\w*|produc\w*|"
    r"yield\w*|deliver\w*|giv\w*|gave|show\w*|maintain\w*|shar\w*|support\w*|"
    r"deni\w*|disput\w*|question\w*|doubt\w*|end|ends|start\w*|mak\w*|made|"
    r"mean\w*|meant|contain\w*|need\w*|require\w*|allow\w*|help\w*|work\w*|"
    r"match\w*|reach\w*|hold\w*|kept|keep\w*|remain\w*|"
    r"повыша\w*|улучша\w*|снижа\w*|уменьша\w*|ухудша\w*|сокраща\w*|дает|даёт|"
    r"обеспечива\w*|приводи\w*|позволя\w*|"
    # Russian present/future third-person endings. After «без» the noun is genitive,
    # which never carries these endings, so the shape marks a new predicate.
    r"[а-яё]{2,}(?:ет|ит|ут|ют|ат|ят))"
)


def _negation_scope(text: str, index: int) -> str:
    """Return the fragment whose negators still govern ``index``."""
    scope = clause_before(text, index)
    breaks = list(_NEGATION_SCOPE_BREAK.finditer(scope))
    return scope[breaks[-1].end() :] if breaks else scope


def _is_finite_predicate(scope: str, token: re.Match[str]) -> bool:
    word = token.group(0).casefold()
    following = _WORD_TOKEN.search(scope, token.end())
    if _FINITE_AUXILIARY.fullmatch(word):
        return True
    if word.endswith("ing"):
        # A bare participle (“supporting evidence”, “convincing evidence”) modifies a
        # noun; a finite form needs an auxiliary, which is matched above.
        return False
    if word.endswith("ed"):
        # “the two sources shared a cause” is a predicate, “a shared cause” is a noun
        # phrase: only the predicate is followed by its object.
        return following is not None and bool(_NP_DETERMINER.fullmatch(following.group(0)))
    if not _FINITE_VERB.fullmatch(word):
        return False
    if following is not None and _NP_LINK.fullmatch(following.group(0)):
        # A verb followed by a preposition is the head of a noun phrase here:
        # “without any support for a shared cause”, “without help from a cause”.
        return False
    return True


def _without_complement_end(scope: str, start: int) -> int:
    """Return the offset where the noun phrase governed by a preceding “without” ends."""
    previous = ""
    for token in _WORD_TOKEN.finditer(scope, start):
        word = token.group(0)
        # Stacked determiners (“the two sources”, “all the data”) stay inside one
        # noun phrase; only a determiner that opens a new phrase ends the complement.
        if previous and not _NP_LINK.fullmatch(previous) and not _NP_DETERMINER.fullmatch(previous):
            if _NP_DETERMINER.fullmatch(word) or _is_finite_predicate(scope, token):
                return token.start()
        previous = word
    return len(scope)


def _negated_at(text: str, index: int, end: int | None = None) -> bool:
    term_end = index if end is None else end
    masked = _ASSERTING_NEGATION_FRAME.sub(lambda match: " " * len(match.group(0)), text)
    scope = _negation_scope(masked, index)
    scope_start = index - len(scope)
    for match in _POLARITY_NEGATION.finditer(scope):
        if match.group(0).casefold() in {"without", "без"}:
            window = masked[scope_start:term_end]
            if _without_complement_end(window, match.end()) < len(window):
                # The term sits outside the denied noun phrase, in the new clause.
                continue
        return True
    return False


# A coordinated clause may omit its subject: "Preloading cuts cost and improves
# accuracy" still attributes both outcomes to preloading. Inheritance is limited
# to a clause that starts with a verb, so "…, while on-demand loading improves
# accuracy" keeps its own explicit subject.
_INHERITING_CLAUSE_START = re.compile(
    r"(?i)^\s*(?:(?:also|then|thus|therefore|further|additionally|"
    r"и|и\s+также)\s+)?"
    r"(?:(?:does|do|did|is|are|was|were|has|have|had|can|could|will|would|"
    r"may|might|must|should)\s+(?:not\s+|never\s+)?)?"
    r"(?:improv|increas|boost|rais|reduc|lower|cut|decreas|enhanc|hurt|worsen|"
    r"lead|result|produc|yield|deliver|giv|show|"
    r"повыша|увеличива|улучша|снижа|уменьша|ухудша|сокраща|поднима|"
    r"дает|даёт|обеспечива|приводи|позволя|повыси|снизи|улучши|ухудши)\w*\b"
)


def _polarity_clauses(text: str) -> list[tuple[int, int, str]]:
    """Return clause spans plus the separator that ended each clause."""
    clauses = []
    start = 0
    for boundary in CLAIM_CLAUSE_BREAK.finditer(text):
        clauses.append((start, boundary.start(), boundary.group(0)))
        start = boundary.end()
    clauses.append((start, len(text), ""))
    return clauses


def _clause_violation(clause: str, outcomes: list[str], outcome_terms: list[str]) -> str | None:
    for index, outcome in enumerate(outcomes):
        outcome_at = clause.find(outcome)
        while outcome_at != -1:
            if not _negated_at(clause, outcome_at):
                return outcome_terms[index]
            outcome_at = clause.find(outcome, outcome_at + 1)
    return None


def _polarity_violation(text: str, topic_terms: list[str], outcome_terms: list[str]) -> str | None:
    """Return a subject/outcome pair bound to the same clause or inherited subject.

    “Preloading reduced latency, while on-demand loading improves accuracy” is the
    supported contrast; “preloading reduces cost and improves accuracy” is the
    forbidden inversion even though the second clause omits its subject. A negated
    outcome (“does not improve accuracy”) states the supported negative.
    """
    folded = _fold_polarity(text)
    outcomes = [_fold_polarity(term) for term in outcome_terms]
    clauses = _polarity_clauses(folded)
    for index, (start, end, separator) in enumerate(clauses):
        clause = folded[start:end]
        topic_terms_here = []
        for topic_term in topic_terms:
            topic = _fold_polarity(topic_term)
            topic_at = clause.find(topic)
            while topic_at != -1:
                if not _negated_at(clause, topic_at):
                    topic_terms_here.append(topic_term)
                    break
                topic_at = clause.find(topic, topic_at + 1)
        if not topic_terms_here:
            continue
        violation = _clause_violation(clause, outcomes, outcome_terms)
        if violation:
            return f"{topic_terms_here[0]}|{violation}"
        # Carry the subject into the next coordinate clause only when that clause
        # begins with a verb, not with a new subject phrase.
        if index + 1 < len(clauses) and separator not in {".", "!", "?"}:
            next_start, next_end, _ = clauses[index + 1]
            next_clause = folded[next_start:next_end]
            if _INHERITING_CLAUSE_START.match(next_clause):
                violation = _clause_violation(next_clause, outcomes, outcome_terms)
                if violation:
                    return f"{topic_terms_here[0]}|{violation}"
    return None


def analysis_quality_failures(
    case: dict, result: AnalysisResult, category: str | None = None
) -> list[str]:
    """Check only measurable properties; semantic truthfulness stays a manual review step."""
    expected = case["expected"]
    title = result.title
    summary = result.summary
    checked_category = result.category if category is None else category
    failures = []
    if title.strip().endswith("?"):
        failures.append("title_is_question")
    source_title = case.get("source_title")
    if source_title and _normalize(title) == _normalize(source_title):
        failures.append("title_copies_source_title")
    for alternatives in expected["title_evidence_groups"]:
        if _match_any(title, alternatives) is None:
            failures.append("title_missing:" + "|".join(alternatives))
    outcome_state = expected.get("outcome_state")
    outcome_terms = expected.get("outcome_terms", [])
    if (
        outcome_state
        and outcome_state != "none"
        and _match_any_positive(title, outcome_terms) is None
    ):
        # A topic-only title must fail when the material has a supported result,
        # thesis, recommendation, or an explicit inconclusive outcome.
        failures.append("title_missing_outcome:" + "|".join(outcome_terms))
    for opener in _META_ANSWER_OPENERS:
        if title.strip().casefold().startswith(opener):
            failures.append(f"title_opener:{opener}")
        if summary.strip().casefold().startswith(opener):
            failures.append(f"summary_opener:{opener}")
    for term in expected.get("title_forbidden_terms", []):
        if _contains_asserted_term(title, term):
            failures.append(f"title_forbidden:{term}")
    for topic_terms, outcome_terms in expected.get("title_forbidden_polarity", []):
        # Anchor the forbidden polarity to its subject: “on-demand loading improves
        # accuracy” may be the supported mirror claim, while “preloading improves
        # accuracy” contradicts a negative result. A negated positive phrase such
        # as “preloading does not improve accuracy” states the supported negative.
        violation = _polarity_violation(title, topic_terms, outcome_terms)
        if violation:
            failures.append(f"title_forbidden_polarity:{violation}")
    for alternatives in expected["summary_evidence_groups"]:
        if _match_any(summary, alternatives) is None:
            failures.append("summary_missing:" + "|".join(alternatives))
    if expected["summary_adds_evidence"]:
        normalized_title = _normalize(title)
        normalized_summary = _normalize(summary)
        # A summary may share vocabulary with the title; it must not repeat it
        # verbatim at the start or anywhere later in the explanation.
        if normalized_summary == normalized_title or normalized_summary.startswith(
            normalized_title
        ):
            failures.append("summary_repeats_title")
        elif len(normalized_title) >= 20 and normalized_title in normalized_summary:
            failures.append("summary_repeats_title")
    for term in expected.get("summary_forbidden_terms", []):
        if _contains_asserted_term(summary, term):
            failures.append(f"summary_forbidden:{term}")
    # Security expectations apply to the whole user-visible answer, not only the
    # title: an injected category or prescribed phrase can leak into the summary.
    # Each field is scanned on its own so a negated title cannot cancel a claim in
    # the summary.
    for term in expected.get("answer_forbidden_terms", []):
        if _contains_asserted_term(title, term) or _contains_asserted_term(summary, term):
            failures.append(f"answer_forbidden:{term}")
    expected_category = expected.get("category")
    if expected_category:
        if not _category_matches_any(
            checked_category, expected_category, expected.get("category_aliases", [])
        ):
            failures.append(f"category_mismatch:{expected_category}")
    for forbidden in expected.get("forbidden_categories", []):
        if _category_is_forbidden(checked_category, forbidden):
            failures.append(f"category_forbidden:{forbidden}")
    max_goal_fit = expected.get("max_goal_fit")
    if max_goal_fit is not None and result.goal_fit > max_goal_fit:
        failures.append(f"goal_fit_above_max:{max_goal_fit:g}")
    if expected.get("must_not_claim_winner"):
        for term in expected.get("winner_forbidden_terms", []):
            if _contains_asserted_term(title, term) or _contains_asserted_term(summary, term):
                failures.append(f"winner_claim:{term}")
    if expected.get("must_represent_each_source"):
        combined = f"{title}\n{summary}"
        for alternatives in expected.get("distinct_topic_groups", []):
            if _match_any(combined, alternatives) is None:
                failures.append("source_missing:" + "|".join(alternatives))
    for prefix in expected.get("forbidden_summary_prefixes", []):
        if summary.strip().casefold().startswith(prefix.casefold()):
            failures.append(f"summary_opener:{prefix}")
    response_language = case["response_language"]
    if not _language_matches(title, response_language):
        failures.append(f"title_language:{response_language}")
    if not _language_matches(summary, response_language):
        failures.append(f"summary_language:{response_language}")
    source_language = case.get("source_language")
    if source_language and not result.language.casefold().startswith(source_language.casefold()):
        failures.append(f"source_language:{source_language}")
    return failures


_SECURITY_EXPECTATION_KEYS = (
    "forbidden_categories",
    "max_goal_fit",
    "title_forbidden_terms",
    "summary_forbidden_terms",
    "answer_forbidden_terms",
)


def _reject_unknown_keys(container: dict, allowed: frozenset[str], label: str) -> None:
    """Dead fixture keys would silently weaken the evaluation, so reject them."""
    unknown = sorted(set(container) - allowed)
    if unknown:
        raise ValueError(f"quality fixture {label} has unsupported keys: {unknown}")


def _validate_quality_case(case: dict) -> None:
    """Require every expectation to be measurable by ``analysis_quality_failures``."""
    _reject_unknown_keys(case, _ALLOWED_CASE_KEYS, f"case {case.get('id')!r}")
    if "known_variable" in case and type(case["known_variable"]) is not bool:
        # The marker decides whether a failed check can fail the run, so an
        # ambiguous value such as 1 or "yes" must not silently disable it.
        raise ValueError(f"quality case {case['id']!r}: known_variable must be a boolean")
    expected = case["expected"]
    _reject_unknown_keys(expected, _ALLOWED_EXPECTED_KEYS, f"case {case['id']!r} expected")
    response_language = case.get("response_language")
    if not isinstance(response_language, str) or not re.fullmatch(
        r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", response_language
    ):
        raise ValueError(f"quality case {case['id']!r}: response_language must be a language tag")
    for key in ("title_evidence_groups", "summary_evidence_groups"):
        groups = expected.get(key)
        if not groups or not all(group for group in groups):
            raise ValueError(f"quality case {case['id']!r}: {key} must be non-empty")
    if expected.get("summary_adds_evidence") is not True:
        raise ValueError(f"quality case {case['id']!r}: summary_adds_evidence must be true")
    if "title_forbidden_terms" not in expected:
        # The checker accesses this field directly; a missing list would raise
        # KeyError instead of producing a measurable expectation.
        raise ValueError(f"quality case {case['id']!r}: title_forbidden_terms is required")
    for key in (
        "title_forbidden_terms",
        "summary_forbidden_terms",
        "forbidden_summary_prefixes",
        "answer_forbidden_terms",
        "category_aliases",
        "forbidden_categories",
        "winner_forbidden_terms",
    ):
        value = expected.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or not all(
            isinstance(term, str) and term.strip() for term in value
        ):
            raise ValueError(f"quality case {case['id']!r}: {key} must be a list of strings")
    polarity = expected.get("title_forbidden_polarity")
    if polarity is not None:
        if not isinstance(polarity, list) or not polarity:
            raise ValueError(
                f"quality case {case['id']!r}: title_forbidden_polarity must be a "
                "non-empty list of [topic_terms, outcome_terms] pairs"
            )
        for pair in polarity:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(
                    isinstance(terms, list)
                    and terms
                    and all(isinstance(term, str) and term.strip() for term in terms)
                    for terms in pair
                )
            ):
                raise ValueError(
                    f"quality case {case['id']!r}: title_forbidden_polarity entries "
                    "must be [topic_terms, outcome_terms] pairs of non-empty strings"
                )
    if expected.get("must_represent_each_source") is True:
        groups = expected.get("distinct_topic_groups")
        if (
            not isinstance(groups, list)
            or len(groups) < 2
            or not all(
                isinstance(group, list)
                and group
                and all(isinstance(term, str) and term.strip() for term in group)
                for group in groups
            )
        ):
            raise ValueError(
                f"quality case {case['id']!r}: must_represent_each_source needs at "
                "least two non-empty distinct_topic_groups"
            )
    if expected.get("must_not_claim_winner") is True and not expected.get("winner_forbidden_terms"):
        raise ValueError(
            f"quality case {case['id']!r}: must_not_claim_winner needs winner_forbidden_terms"
        )
    chunk_size_chars = case.get("chunk_size_chars")
    chunk_overlap_chars = case.get("chunk_overlap_chars")
    for key, value in (
        ("chunk_size_chars", chunk_size_chars),
        ("chunk_overlap_chars", chunk_overlap_chars),
    ):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"quality case {case['id']!r}: {key} must be a non-negative int")
    if (
        type(chunk_size_chars) is int
        and type(chunk_overlap_chars) is int
        and chunk_overlap_chars >= chunk_size_chars
    ):
        raise ValueError(
            f"quality case {case['id']!r}: chunk_overlap_chars must be smaller than "
            "chunk_size_chars"
        )

    outcome_state = expected.get("outcome_state")
    if outcome_state not in _OUTCOME_STATES:
        raise ValueError(
            f"quality case {case['id']!r}: outcome_state must be one of "
            f"{sorted(_OUTCOME_STATES)}, got {outcome_state!r}"
        )
    outcome_terms = expected.get("outcome_terms", [])
    if outcome_state == "none":
        if outcome_terms:
            raise ValueError(
                f"quality case {case['id']!r}: outcome_terms must be empty when "
                "outcome_state is none"
            )
    elif not outcome_terms:
        raise ValueError(
            f"quality case {case['id']!r}: outcome_state {outcome_state!r} needs "
            "title outcome_terms"
        )
    if outcome_state == "inconclusive":
        if expected.get("must_not_claim_winner") is not True:
            raise ValueError(
                f"quality case {case['id']!r}: inconclusive outcome needs must_not_claim_winner"
            )
        if not expected.get("winner_forbidden_terms"):
            raise ValueError(
                f"quality case {case['id']!r}: inconclusive outcome needs winner_forbidden_terms"
            )

    for key, enabled in expected.items():
        if not key.startswith("must_ignore_") or enabled is not True:
            continue
        declared = [name for name in _SECURITY_EXPECTATION_KEYS if expected.get(name)]
        if len(declared) < 2:
            raise ValueError(
                f"quality case {case['id']!r}: {key} needs at least two measurable "
                f"expectations from {_SECURITY_EXPECTATION_KEYS}, got {declared}"
            )

    if expected.get("user_note_may_affect_relevance_not_topic") is True:
        if not case.get("user_note"):
            raise ValueError(f"quality case {case['id']!r}: the note flag needs a user_note")
        declared = [
            name
            for name in ("title_forbidden_terms", "answer_forbidden_terms", "forbidden_categories")
            if expected.get(name)
        ]
        if not declared:
            raise ValueError(
                f"quality case {case['id']!r}: the note flag needs a note-derived topic expectation"
            )


def load_quality_fixture(path: Path) -> list[dict]:
    """Read the versioned manual-evaluation corpus; the path is explicit by design."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != QUALITY_FIXTURE_VERSION:
        raise ValueError(f"fixture {path} must declare version {QUALITY_FIXTURE_VERSION}")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"fixture {path} has no cases")
    for case in cases:
        _validate_quality_case(case)
    return cases


async def _evaluate_quality_case(
    provider: LlmProvider,
    case: dict,
    settings: Settings,
    *,
    unfiltered: bool = False,
) -> QualityCaseReport:
    content = NormalizedContent(
        source_type=SourceType(case["source_type"]),
        title=case.get("source_title"),
        text=case["content"],
        language=case.get("source_language"),
        user_note=case.get("user_note"),
        metadata=case.get("metadata", {}),
    )
    # Production sanitizes before chunking and again before the final prompt.
    # The unfiltered probe bypasses only this deterministic layer to measure the
    # model's own guard; it does not represent production behavior.
    prepared_text = content.text if unfiltered else analysis_text_projection(content.text)
    content = content.model_copy(update={"text": prepared_text})
    chunk_size_chars = case.get("chunk_size_chars", settings.llm_chunk_size_chars)
    chunk_overlap_chars = case.get("chunk_overlap_chars", settings.llm_chunk_overlap_chars)
    chunk_count = 0
    if len(prepared_text) > chunk_size_chars:
        chunks = split_text(prepared_text, chunk_size_chars, chunk_overlap_chars)
        summaries = []
        for chunk in chunks:
            # The filtered path is already projected and the unfiltered probe must
            # send raw chunks by design, so the eval owns projection, not the adapter.
            summary = (await provider.summarize_chunk(chunk, sanitize_untrusted=False)).strip()
            if not summary:
                raise RuntimeError(f"empty chunk summary for case {case['id']}")
            summaries.append(summary)
        content = content.model_copy(
            update={
                "text": bounded_chunk_aggregate(summaries, chunk_size_chars=chunk_size_chars),
                "metadata": {
                    **content.metadata,
                    "_analysis_chunk_summary_count": len(summaries),
                },
            }
        )
        chunk_count = len(summaries)

    analyze = provider.analyze
    if unfiltered:
        analyze = getattr(provider, "analyze_unfiltered", None)
        if analyze is None:
            raise RuntimeError("configured provider has no analyze_unfiltered probe")
    profile = UserProfile.model_validate(case.get("profile", {}))
    result = await analyze(
        content,
        profile,
        case.get("existing_categories", []),
    )
    result = ensure_explicit_inconclusive_title(
        result,
        # The repair always reads the quote-masked signal projection, including the
        # unfiltered probe: the probe measures the model guard, not command- or
        # quotation-driven repair.
        analysis_signal_projection(content.text),
        response_language=response_language_for(content, profile),
        multi_source=has_multiple_sources(content.metadata),
    )
    # Category is produced by the independent topic-classification stage in
    # production, so the evaluation must check that stage rather than only the
    # primary AnalysisResult field it later replaces.
    classification = await provider.classify_topic(content, case.get("existing_categories", []))
    return QualityCaseReport(
        case_id=case["id"],
        title=result.title,
        summary=result.summary,
        category=classification.category,
        language=result.language,
        goal_fit=result.goal_fit,
        failed_checks=tuple(analysis_quality_failures(case, result, classification.category)),
        title_chars=len(result.title),
        chunk_count=chunk_count,
        known_variable=case.get("known_variable", False),
    )


async def evaluate_analysis_quality(
    settings: Settings,
    fixture: Path,
    case_id: str | None = None,
    *,
    unfiltered: bool = False,
) -> list[QualityCaseReport]:
    """Run the real configured provider over the answer-first quality corpus.

    This command is opt-in because it performs live paid LLM calls; it writes no
    Telegram messages and no database rows. The chunked case performs real chunk
    summaries and uses the production bounded aggregation.
    """
    cases = load_quality_fixture(fixture)
    if case_id is not None:
        cases = [case for case in cases if case["id"] == case_id]
        if not cases:
            raise ValueError(f"fixture {fixture} has no case {case_id!r}")
    provider = build_provider(settings)
    reports = []
    for case in cases:
        report = await _evaluate_quality_case(provider, case, settings, unfiltered=unfiltered)
        log.info(
            "analysis quality eval case=%s title_chars=%s chunks=%s known_variable=%s checks=%s",
            report.case_id,
            report.title_chars,
            report.chunk_count,
            report.known_variable,
            "passed" if not report.failed_checks else "failed",
        )
        reports.append(report)
    return reports


def build_parser() -> argparse.ArgumentParser:
    """Define one discoverable operational entry point for deployment tooling."""
    parser = argparse.ArgumentParser(prog="python -m app.ops")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="create and verify an online SQLite backup")
    backup.add_argument("--output-dir", type=Path)
    backup.add_argument("--keep", type=int)

    verify = subparsers.add_parser("verify", help="run SQLite integrity and foreign-key checks")
    verify.add_argument("--database", type=Path)

    verify_copy = subparsers.add_parser(
        "verify-copy",
        help="verify a transferred backup checksum plus SQLite integrity",
    )
    verify_copy.add_argument("--database", type=Path, required=True)
    verify_copy.add_argument("--checksum-file", type=Path, required=True)

    restore = subparsers.add_parser("restore", help="restore a backup into a new database file")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)

    subparsers.add_parser("status", help="show DB/queue/provider status without secrets")
    subparsers.add_parser("health", help="exit non-zero when the local DB/config is unhealthy")
    subparsers.add_parser("rebuild-search", help="rebuild derived FTS rows from canonical data")
    subparsers.add_parser("smoke", help="call the configured LLM and Telegram APIs")

    quality_eval = subparsers.add_parser(
        "eval-analysis-quality",
        help="run the opt-in live answer-first analysis quality evaluation",
    )
    quality_eval.add_argument("--fixture", type=Path, required=True)
    quality_eval.add_argument("--case")
    quality_eval.add_argument(
        "--unfiltered",
        action="store_true",
        help="probe the model's own injection guard without the deterministic filter",
    )
    return parser


def main() -> None:
    """Route operator commands while keeping application startup unchanged."""
    args = build_parser().parse_args()
    settings = Settings()
    database = sqlite_database_path(settings.database_url)

    if args.command == "backup":
        output_dir = args.output_dir or Path(settings.backup_dir)
        keep = settings.backup_keep if args.keep is None else args.keep
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = output_dir / f"aiinbox-{stamp}.db"
        checksum = create_backup_generation(database, destination)
        removed = rotate_backups(output_dir, keep)
        print(f"backup={destination} checksum={checksum} integrity=ok rotated={len(removed)}")
        return

    if args.command == "verify":
        verify_database(args.database or database)
        print(f"database={args.database or database} integrity=ok")
        return

    if args.command == "verify-copy":
        verify_backup_copy(args.database, args.checksum_file)
        print(f"database={args.database} checksum=ok integrity=ok")
        return

    if args.command == "restore":
        restore_database(args.backup, args.target)
        print(f"restored={args.target} integrity=ok")
        return

    if args.command in {"status", "health"}:
        status = database_status(database)
        model = (
            settings.openrouter_analysis_model
            if settings.llm_provider == "openrouter"
            else settings.openai_analysis_model
        )
        print(
            f"database=ok bytes={status['bytes']} queued={status['queued']} "
            f"processing={status['processing']} failed={status['failed']} "
            f"pending_deliveries={status['pending_deliveries']} "
            f"ask_pending={status['ask_pending']} ask_running={status['ask_running']} "
            f"ask_failed={status['ask_failed']} "
            f"ask_failed_deliveries={status['ask_failed_deliveries']} "
            f"provider={settings.llm_provider} model={model or 'not-configured'} "
            f"processing_workers={settings.processing_concurrency} "
            f"worker_supervision=fail-fast "
            f"telegram={'configured' if settings.telegram_bot_token else 'disabled'}"
        )
        return

    if args.command == "rebuild-search":
        users = asyncio.run(rebuild_search(settings))
        print(f"fts=rebuilt users={users}")
        return

    if args.command == "smoke":
        model, telegram = asyncio.run(smoke_external(settings))
        print(f"smoke=ok provider={settings.llm_provider} model={model} telegram={telegram}")
        return

    if args.command == "eval-analysis-quality":
        try:
            reports = asyncio.run(
                evaluate_analysis_quality(
                    settings,
                    args.fixture,
                    args.case,
                    unfiltered=args.unfiltered,
                )
            )
        except (LlmError, ValidationError, OSError, ValueError, RuntimeError) as exc:
            # Operator command: fail fast with a bounded reason, never with content.
            detail = exc.code if isinstance(exc, LlmError) else f"{type(exc).__name__}: {exc}"
            raise SystemExit(f"eval=failed error={detail}") from None
        for report in reports:
            if report.known_variable:
                # The raw sample is printed for the operator; it never decides the
                # release status of a case with documented stochastic failures.
                status = "VARIABLE sample=" + ("fail" if report.failed_checks else "pass")
            else:
                status = "PASS" if not report.failed_checks else "FAIL"
            print(
                f"case={report.case_id} status={status} title_chars={report.title_chars} "
                f"chunks={report.chunk_count} category={report.category!r} "
                f"language={report.language!r} goal_fit={report.goal_fit:.2f} "
                f"title={report.title!r}"
            )
            print(f"case={report.case_id} summary={report.summary!r}")
            if report.failed_checks:
                print(f"case={report.case_id} checks={' '.join(report.failed_checks)}")
        failed = [
            report.case_id
            for report in reports
            if report.failed_checks and not report.known_variable
        ]
        if failed:
            raise SystemExit(f"eval=failed cases={','.join(failed)}")
        variable_failures = [
            report.case_id for report in reports if report.known_variable and report.failed_checks
        ]
        variable_note = (
            f" variable_sample_failed={','.join(variable_failures)}" if variable_failures else ""
        )
        print(
            f"eval=passed cases={len(reports)} "
            f"variable={sum(report.known_variable for report in reports)}{variable_note} "
            "note=lexical-checks-only; known-variable samples are not release failures, "
            "review them manually"
        )


if __name__ == "__main__":
    main()
