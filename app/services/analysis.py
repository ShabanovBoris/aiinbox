import hashlib
import re
from functools import cache

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ContentKind
from app.domain.language import response_language_for
from app.domain.models import (
    AnalysisResult,
    NormalizedContent,
    TopicClassificationResult,
    UserProfile,
)
from app.domain.signals import (
    has_multiple_sources,
    inconclusive_title_language,
    inconclusive_title_statement,
    inconclusive_title_suffix,
    states_inconclusive_comparison,
    title_claims_comparison_winner,
    title_states_inconclusive,
)
from app.llm.base import LlmError, LlmProvider
from app.llm.untrusted import mask_quoted_evidence, without_control_instructions
from app.storage.models import Content, Item

# Increment when chunk-summary semantics change so durable evidence is not silently reused after a
# prompt change that asks the model to preserve different information.
CHUNK_SUMMARY_GENERATOR_VERSION = 5


def analysis_text_projection(text: str) -> str:
    """The model/evidence text shared by analysis and evaluation."""
    return without_control_instructions(text)


def analysis_signal_projection(text: str) -> str:
    """The deterministic signal text: quoted evidence cannot drive a hint or repair.

    Apply it to the raw content text once: a second pass would read the blank mask of
    a leading quotation as an indented block, so the projection is not idempotent.
    """
    return without_control_instructions(mask_quoted_evidence(text), keep_quoted_evidence=False)


def split_text(text: str, chunk_size_chars: int, overlap_chars: int = 0) -> list[str]:
    """Split into bounded chunks while preferring complete paragraph boundaries."""
    if chunk_size_chars <= 0 or not 0 <= overlap_chars < chunk_size_chars:
        raise ValueError("chunk_size_chars must be positive and overlap smaller than chunk size")
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        hard_end = min(start + chunk_size_chars, len(text))
        end = hard_end
        if hard_end < len(text):
            # Do not cut inside the repeated overlap from the previous chunk; the chosen boundary
            # must add fresh content before advancing.
            boundary_from = start + overlap_chars
            paragraph_end = text.rfind("\n\n", boundary_from + 1, hard_end)
            if paragraph_end >= 0:
                end = paragraph_end + 2

        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap_chars
    return chunks


_TITLE_LIMIT = 300
# A long title keeps the final clause because it usually carries the verdict and the condition that
# scopes it, while the claim clause keeps its head even when it must shrink.
_TITLE_CLAUSE_BREAK = re.compile(
    r"(?<=[,;:])\s+|\s+(?=\b(?:but|however|yet|while|whereas|но|однако)\b)"
)
# A clause cut at a word budget can end on a function word ("…accuracy in", "…coverage and").
_TITLE_FUNCTION_WORDS = (
    r"the|an|of|in|into|to|and|or|but|while|whereas|because|with|without|for|on|"
    r"at|by|from|as|that|than|which|whose|when|where|before|after|under|over|"
    r"through|during|between|among|per|via|is|are|was|were|be|been|"
    r"в|во|на|и|для|при|по|что|с|со|к|ко|из|от|до|под|над|о|об|за|у|же|ли|бы|"
    r"как|чем|или|но|про|без|через|между|"
    r"der|die|das|den|dem|des|und|oder|aber|mit|für|von|zu|auf|bei|aus|nach|"
    r"über|unter|zwischen|dass|wie|als|ist|sind|war|waren|wird|werden|"
    r"el|los|las|del|con|para|por|pero|que|como|son|fue|fueron|"
    r"le|les|du|avec|pour|par|et|ou|mais|comme|sont|dans|sur|"
    r"il|gli|di|che|come|sono|os|dos|com|mas|são|foi|foram|"
    r"het|van|met|voor|door|dat|als|zijn|oraz|ale|że|jak|jest|są|był|przez|dla|"
    r"veya|ama|ile|için|bir|beide|beiden|beider|alle|allen|todos|todas|toutes|"
    r"tous|tutti|tutte"
)
# A list introducer cannot end a claim.
_TITLE_DANGLING_INTRODUCER = (
    r"including|incl\.|such|включая|включаючи|например|наприклад|einschließlich|"
    r"incluyendo|incluindo|inclusief|włączając"
)
_TITLE_DANGLING_TAIL = re.compile(
    rf"(?i:\s+(?:{_TITLE_FUNCTION_WORDS}|{_TITLE_DANGLING_INTRODUCER}))\s*$"
    # A single alternative label ("…winner was A", "…вариант А") is content, not an article.
    r"|\s+a\s*$|\s+а\s*$"
)


def _strip_dangling_tail(text: str) -> str:
    """Drop trailing function words a word-budget cut left behind."""
    cleaned = text.rstrip(" ,;:")
    while cleaned:
        match = _TITLE_DANGLING_TAIL.search(cleaned)
        if match is None:
            return cleaned
        cleaned = cleaned[: match.start()].rstrip(" ,;:")
    return cleaned


# A budget cut can land inside the last coordinated conjunct ("…полное покрытие и меньше"), which
# reads as an unfinished comparison rather than a shorter claim.
_TITLE_COORDINATORS: dict[str, tuple[str, ...]] = {
    "en": ("and", "or", "but", "nor"),
    "ru": ("и", "а", "или", "но"),
    "uk": ("і", "й", "та", "або", "але", "чи"),
    "de": ("und", "oder", "aber", "sondern"),
    "es": ("y", "o", "pero", "sino"),
    "fr": ("et", "ou", "mais"),
    "it": ("e", "o", "ma", "oppure"),
    "pt": ("e", "ou", "mas"),
    "nl": ("en", "of", "maar"),
    "pl": ("i", "oraz", "albo", "lub", "ale"),
    "tr": ("ve", "veya", "ama", "fakat"),
}
# A Latin single letter stays case-sensitive so the English pronoun "I" and an alternative label
# ("option A") are never read as coordination.
_TITLE_CASE_SENSITIVE_COORDINATORS = frozenset({"y", "o", "e", "i"})
# A subordinating conjunction opens an explanatory clause.
_TITLE_SUBORDINATORS: dict[str, tuple[str, ...]] = {
    "en": (
        "when",
        "whenever",
        "if",
        "unless",
        "because",
        "since",
        "although",
        "though",
        "once",
        "until",
        "after",
        "before",
        "as",
    ),
    "ru": (
        "когда",
        "если",
        "пока",
        "поскольку",
        "хотя",
        "чтобы",
        "после того как",
        "до того как",
    ),
    "uk": ("коли", "якщо", "поки", "оскільки", "хоча", "щоб", "після того як", "до того як"),
    "de": ("wenn", "weil", "obwohl", "sobald", "falls", "nachdem", "bevor"),
    "es": ("cuando", "mientras", "porque", "aunque", "después de que", "antes de que"),
    "fr": ("quand", "lorsque", "parce", "puisque", "après que", "avant que"),
    "it": ("quando", "mentre", "perché", "dopo che", "prima che"),
    "pt": ("quando", "enquanto", "porque", "depois que", "antes que"),
    "nl": ("toen", "wanneer", "omdat", "terwijl", "nadat", "voordat"),
    "pl": ("kiedy", "gdy", "ponieważ", "jeśli", "jeżeli", "po tym jak", "zanim"),
    "tr": ("çünkü", "eğer", "sonra", "önce"),
}
# "after", "before" and "as" are also prepositions ("after the migration", "as expected"), and
# treating a prepositional phrase as a clause would hide a later result.
_TITLE_GUARDED_SUBORDINATORS = frozenset({"after", "before", "as"})
# Unambiguous finite forms; a regular past tense is matched separately because it is also the shape
# of an attributive participle ("the planned maintenance") and of a few nouns ("a speed test").
_TITLE_FINITE_VERB = (
    r"(?:is|are|was|were|be|been|has|have|had|does|do|did|will|would|can|could|"
    r"may|might|must|should|grew|rose|fell|won|lost|cut|ran|led|beat|hit|went|"
    r"came|became|made|took|got|kept|held|shrank|doubled|tripled)"
)
# A determiner never stands alone as a subject, so it cannot open the bare-subject branch: "after
# the planned maintenance" is not "after" + "the" + verb "planned".
_TITLE_DETERMINER = r"(?:the|a|an|its|their|our|his|her|this|that|these|those)"
_TITLE_CLAUSE_SUBJECT = rf"(?:{_TITLE_DETERMINER}\s+(?!\w+ed\b)\w+|(?!{_TITLE_DETERMINER}\b)\w+)"
# "After the first failed attempt" is determiner + ordinal + participle + noun, so a weak past tense
# counts only when no content word follows it directly (D-058).
_TITLE_WEAK_VERB = rf"\w+ed\b(?!\s+(?!(?:{_TITLE_FUNCTION_WORDS}|{_TITLE_DETERMINER})\b)\w)"
_TITLE_CLAUSE_SUBJECT_VERB = re.compile(
    rf"(?i)^\s*{_TITLE_CLAUSE_SUBJECT}\s+(?:{_TITLE_FINITE_VERB}\b|{_TITLE_WEAK_VERB})"
)
# A coordinated member that states a reversal negates the headline result the claim opened with, so
# losing it inverts the claim.
_TITLE_REVERSAL_CUES = (
    r"revers\w*|"
    r"no\s+(?:gain|improvement|effect|change|reduction|difference)|"
    r"\w+n['’]t|"
    r"(?:did|does|do|could|would|can|will|should)\s+not|never|not|"
    r"смени\w*|обратн\w*|не\s+удал\w*|не|ни|без"
)
# A limitation or a second result caps or qualifies the claim without inverting it, so it may yield
# to a reversal but not to neutral detail or to a plain result.
_TITLE_LIMITATION_CUES = (
    r"limit\w*|fail\w*|degrad\w*|worse\w*|break(?!through|fast|out|point)\w*|crash\w*|stall\w*|"
    r"огранич\w*|провал\w*|ухудш\w*|деград\w*|сломал\w*|рухнул\w*|зависл\w*|завис\b"
)
# A measured decline or loss is a result too, even with an everyday verb: it must not be displaced
# by neutral detail and must not displace a reversal.
_TITLE_DECLINE_CUES = (
    r"declin\w*|drop\w*|fell|fall\w*|slow\w*|decreas\w*|reduc\w*|lower\w*|"
    r"shr[ae]nk|shrink\w*|lost|lose\w*|"
    r"снизил\w*|снижа\w*|снижен\w*|упал\w*|упасть|просел\w*|уменьшил\w*|"
    r"сократил\w*|замедлил\w*|потерял\w*|ухудшил\w*"
)
# A member that opens with a possessive refers back to the subject of the member before it.
_TITLE_DEPENDENT_MEMBER = re.compile(
    r"\s*(?:its|their|his|her|его|её|ее|их|свой|своя|свою|свои|своей|своих)\b",
    re.IGNORECASE,
)
_TITLE_REVERSAL_MEMBER_CUE = re.compile(rf"(?i)\b(?:{_TITLE_REVERSAL_CUES})\b")
_TITLE_LIMITATION_MEMBER_CUE = re.compile(rf"(?i)\b(?:{_TITLE_LIMITATION_CUES})\b")
_TITLE_RESULT_MEMBER_CUE = re.compile(
    rf"(?i)\b(?:{_TITLE_REVERSAL_CUES}|{_TITLE_LIMITATION_CUES}|{_TITLE_DECLINE_CUES})\b"
)
# Negation polarity decides what a cue word means. "no" is left out of the prefix: the reversal
# and limitation cues already read "no improvement" and "no degradation" as results, and widening
# the prefix would silently drop that member.
_EN_NEGATION_PREFIX = r"(?:\b(?:not|never|cannot)\b|\w+n['’]t)"
# The outcomes whose negation asserts the positive one make the member detail rather than the result
# the source measured.
_TITLE_NEGATED_OUTCOMES = (
    r"fail|break|crash|stall|degrad|declin|drop|fall|fell|worse|worsen|lose|lost|limit|"
    r"shrink|slow|"
    r"ухудш|деградир|упал|упасть|просел|провал|огранич|сломал|рухнул|зависл"
)
# The qualifier construction also reads the Russian noun form of degradation; the one-word gap reads
# only the verb form, so "не устранила деградацию" stays a negative result.
_TITLE_QUALIFIED_OUTCOMES = rf"{_TITLE_NEGATED_OUTCOMES}|деград"
_TITLE_NEGATED_SCOPE_VERBS = r"include|cover|contain|encompass|включ|охват|покры"
# One free word between negation and predicate keeps the negation intact ("didn't really fail", "was
# not a failure").
_TITLE_NEGATION_GAP = r"(?:\w+\s+)?"
# A focus or degree word scopes the negation instead of leaving it intact: "not only failed" and
# "didn't fully fail" still report the failure, "не без ограничений" the limitation.
_TITLE_FOCUS_WORDS = (
    r"only|merely|just|simply|without|"
    r"только|лишь|раз|без|сильно|слишком|столь"
)
_TITLE_DEGREE_WORDS = (
    r"always|consistently|typically|fully|completely|entirely|outright|necessarily|"
    r"всегда|полностью|совсем"
)
# Two or three words in front of a failure predicate are a copular or light-verb phrase that
# qualifies the same negation ("did not show any degradation").
_TITLE_QUALIFIER_GAP = r"(?:\w+\s+){2,3}"


@cache
def _title_negation_words(language: str) -> str:
    """Negation alternatives of ``language``; English is always recognised."""
    if language == "en":
        return _EN_NEGATION_PREFIX
    return rf"(?:{_EN_NEGATION_PREFIX}|\b(?:{_TITLE_NEGATIONS[language]})\b)"


# One negation, one predicate: the same words read with a different polarity by the shape of the
# phrase between them.
_NEGATION_SHAPES = {
    "focus": r"{words}\s+(?:{focus})\s+",
    "degree": r"{words}\s+(?:(?:{degree})\s+)+(?=(?:{outcomes})\w*)",
    "qualified": r"{words}\s+{qualifier}(?=(?:{qualified})\w*)",
    "positive": r"{words}\s+{gap}(?:{outcomes})\w*",
    "scope": r"{words}\s+{gap}(?:{scope})\w*",
}


@cache
def _title_negation_shape(language: str, shape: str) -> re.Pattern[str]:
    """The ``shape`` negation construction of ``language`` as one pattern."""
    return re.compile(
        _NEGATION_SHAPES[shape].format(
            words=_title_negation_words(language),
            focus=_TITLE_FOCUS_WORDS,
            degree=_TITLE_DEGREE_WORDS,
            qualifier=_TITLE_QUALIFIER_GAP,
            outcomes=_TITLE_NEGATED_OUTCOMES,
            qualified=_TITLE_QUALIFIED_OUTCOMES,
            gap=_TITLE_NEGATION_GAP,
            scope=_TITLE_NEGATED_SCOPE_VERBS,
        ),
        re.IGNORECASE,
    )


def _result_cue_text(text: str, language: str) -> str:
    """``text`` with negation polarity applied before result-cue matching."""
    for shape in ("focus", "degree", "qualified", "positive"):
        text = _title_negation_shape(language, shape).sub(" ", text)
    return _title_negation_shape(language, "scope").sub(" limited", text)


_TITLE_NEGATIONS: dict[str, str] = {
    "en": r"not|never|no|n't",
    "ru": r"не|ни|нет",
    "uk": r"не|ні|нема",
    "de": r"nicht|kein(?:e|en|em|er|es)?|nie",
    "es": r"no|nunca|ningún|ningun",
    "fr": r"ne|pas|aucun\w*|jamais",
    "it": r"non|nessun\w*|mai",
    "pt": r"não|nao|nenhum\w*|nunca",
    "nl": r"niet|geen|nooit",
    "pl": r"nie|żaden|zaden|nigdy",
    "tr": r"değil|degil|hiç|hic|asla",
}


@cache
def _title_conjunct(language: str) -> re.Pattern[str]:
    """Coordination separators of ``language`` for a word-budget cut."""
    words = _TITLE_COORDINATORS[language]
    caseless = [word for word in words if word not in _TITLE_CASE_SENSITIVE_COORDINATORS]
    sensitive = [word for word in words if word in _TITLE_CASE_SENSITIVE_COORDINATORS]
    alternatives = []
    if caseless:
        alternatives.append(rf"(?i:\s+(?:{'|'.join(caseless)})\s+)")
    if sensitive:
        alternatives.append(rf"\s+(?:{'|'.join(sensitive)})\s+")
    return re.compile("|".join(alternatives))


@cache
def _title_negation(language: str) -> re.Pattern[str]:
    """Negations of ``language``; the member a cut would truncate is never dropped."""
    return re.compile(rf"(?i)\b(?:{_TITLE_NEGATIONS[language]})\b")


@cache
def _title_subordinator(language: str) -> re.Pattern[str]:
    """Subordinating conjunctions of ``language`` for the single-claim cut."""
    return re.compile(rf"(?i)\b(?:{'|'.join(_TITLE_SUBORDINATORS[language])})\b")


def _subordinate_clause(claim: str, language: str) -> re.Match[str] | None:
    """The first subordinating conjunction that actually opens a clause."""
    for match in _title_subordinator(language).finditer(claim):
        if match.group().casefold() not in _TITLE_GUARDED_SUBORDINATORS:
            return match
        if _TITLE_CLAUSE_SUBJECT_VERB.match(claim[match.end() :]) is not None:
            return match
    return None


def _strip_partial_conjunct(text: str, language: str) -> str:
    """Drop the final coordinated conjunct only when it contains no negation."""
    for match in reversed(list(_title_conjunct(language).finditer(text))):
        if _title_negation(language).search(text[match.end() :]):
            continue
        return text[: match.start()].rstrip(" ,;:")
    return text


_TITLE_PHRASE_PREPOSITION = re.compile(
    r"(?i:in|on|at|of|for|with|without|by|from|to|into|over|under|between|among|"
    r"during|after|before|through|within|across|"
    r"в|во|на|с|со|при|по|к|ко|из|от|до|под|над|о|об|за|у|про|без|через|между|для|"
    r"среди|mit|für|von|zu|auf|bei|aus|nach|über|unter|zwischen|con|para|por|en|de|"
    r"del|sin|sobre|entre|durante|hasta|avec|pour|par|dans|sur|sans|pendant|chez|"
    r"com|em|sem|met|voor|door|van|op|bij|uit|naar|zonder|na|do|od|dla|przez|bez|"
    r"için|gibi|kadar)"
)
_TITLE_FUNCTION_WORD = re.compile(rf"(?i:(?:{_TITLE_FUNCTION_WORDS}))")


def _drop_incomplete_phrase(text: str) -> str:
    """Drop a one-word prepositional fragment a budget cut left behind."""
    words = text.split()
    for index in range(len(words) - 1, 0, -1):
        if not _TITLE_PHRASE_PREPOSITION.fullmatch(words[index]):
            continue
        trailing = words[index + 1 :]
        if len(trailing) == 1 or all(_TITLE_FUNCTION_WORD.fullmatch(word) for word in trailing):
            return _strip_dangling_tail(" ".join(words[:index]))
        return text
    return text


def _head_words(text: str, budget: int, language: str) -> str:
    """The first words of ``text`` that fit ``budget`` characters."""
    words = text.split()
    kept: list[str] = []
    size = 0
    for word in words:
        extra = len(word) + (1 if kept else 0)
        if size + extra > budget:
            break
        kept.append(word)
        size += extra
    if len(kept) == len(words):
        return " ".join(kept)
    head = _strip_dangling_tail(" ".join(kept))
    return _drop_incomplete_phrase(_strip_partial_conjunct(head, language))


def _compact_subordinate_detail(clause: str, language: str) -> str:
    """Drop a neutral coordinated member inside the clause's subordinate clause."""
    subordinate = _subordinate_clause(clause, language)
    if subordinate is None:
        return clause
    explanation = clause[subordinate.start() :]
    inner = _title_conjunct(language).search(explanation)
    if inner is None or _title_negation(language).search(explanation):
        return clause
    return clause[: subordinate.start() + inner.start()].rstrip(" ,;:")


def _coordinated_member(
    claim: str, cuts: list[re.Match[str]], index: int, end: int | None = None
) -> str:
    """The text a coordinator introduces, ending where the next member starts."""
    member_end = cuts[index + 1].start() if index + 1 < len(cuts) else len(claim)
    return claim[cuts[index].end() : member_end if end is None else min(member_end, end)]


def _dominant_result_member(
    claim: str, cuts: list[re.Match[str]], language: str, end: int | None = None
) -> int | None:
    """The earliest coordinated member whose loss would strengthen the claim."""
    for cue in (_TITLE_REVERSAL_MEMBER_CUE, _TITLE_LIMITATION_MEMBER_CUE):
        for index, cut in enumerate(cuts):
            # ``end`` bounds the member the condition clause follows; a member that starts after the
            # clause is outside it and keeps its own tail.
            bound = end if end is not None and cut.start() < end else None
            if cue.search(
                _result_cue_text(_coordinated_member(claim, cuts, index, bound), language)
            ):
                return index
    return None


def _polarity_flipped_member(member: str, language: str) -> bool:
    """Whether negation alone produced this member's cue words."""
    return (
        _TITLE_RESULT_MEMBER_CUE.search(member) is not None
        and _TITLE_RESULT_MEMBER_CUE.search(_result_cue_text(member, language)) is None
    )


def _trim_single_claim(claim: str, budget: int, language: str) -> str:
    """Keep the result a one-clause claim states inside ``budget``.

    One pass picks the member: the earliest reversal or limitation outranks a later
    result, and a trailing member that adds only neutral detail yields to the result
    before it.
    """
    cuts = list(_title_conjunct(language).finditer(claim))
    subordinate = _subordinate_clause(claim, language)
    clause_end = subordinate.start() if subordinate is not None else None
    top_level = [cut for cut in cuts if clause_end is None or cut.start() < clause_end]
    dominant = _dominant_result_member(claim, cuts, language, clause_end)
    limit = len(claim)
    if dominant is not None and clause_end is not None and cuts[dominant].start() >= clause_end:
        # The reversal follows the condition clause, so it is the claim's result and
        # its own tail stays; the earlier member contributes no result once its
        # negation is read.
        top_level, keep = cuts, dominant
    elif top_level:
        # A coordinator inside the subordinate clause belongs to that clause, not to
        # the claim's top level, so only the pre-clause cuts take part here.
        keep = len(top_level) - 1 if dominant is None else min(dominant, len(top_level) - 1)
    elif dominant is not None:
        top_level, keep = cuts[dominant:], 0
    else:
        for index in range(len(cuts) - 1, -1, -1):
            member_end = cuts[index + 1].start() if index + 1 < len(cuts) else len(claim)
            if _TITLE_RESULT_MEMBER_CUE.search(
                _result_cue_text(claim[cuts[index].end() : member_end], language)
            ):
                top_level, keep = cuts[index:], 0
                break
        else:
            return _head_words(claim, budget, language)
    while True:
        last = top_level[keep]
        member_end = top_level[keep + 1].start() if keep + 1 < len(top_level) else limit
        compacted = (
            _compact_subordinate_detail(claim, language)
            if subordinate is not None and subordinate.start() > last.end()
            else claim
        )
        tail = claim[last.end() : min(member_end, len(compacted))].rstrip(" ,;:")
        if all(_TITLE_FUNCTION_WORD.fullmatch(word) for word in tail.split()):
            # A final member made only of service words is a cut fragment, not a result.
            return _head_words(claim, budget, language)
        neutral = (
            _TITLE_RESULT_MEMBER_CUE.search(_result_cue_text(tail, language)) is None
            and _TITLE_RESULT_MEMBER_CUE.search(_result_cue_text(claim[: last.start()], language))
            is not None
        )
        if not neutral:
            break
        # The final member adds neutral detail while the claim before it states the
        # reversal or the limitation, so the detail is dropped and the selection
        # repeats on the shorter claim.
        limit = last.start()
        top_level = top_level[:keep]
        if not top_level:
            return _head_words(claim[:limit].rstrip(" ,;:"), budget, language)
        keep = len(top_level) - 1 if dominant is None else min(dominant, len(top_level) - 1)
    final = f"{last.group()}{tail}"
    prefix = claim[: last.start()].rstrip(" ,;:")
    # A member whose cue came only from a negated negative predicate states no result once polarity
    # is read ("the build didn't fail"), so it is detail and goes.
    flipped = [
        index
        for index in range(keep)
        if _polarity_flipped_member(
            _coordinated_member(claim, top_level, index, clause_end), language
        )
    ]
    # A member that opens with a possessive refers back to the flipped member's subject, so removing
    # the subject would leave the pronoun without an antecedent.
    dependent: set[int] = set()
    for index in flipped:
        following = index + 1
        while following < keep and _TITLE_DEPENDENT_MEMBER.match(
            _coordinated_member(claim, top_level, following, clause_end)
        ):
            dependent.add(following)
            following += 1
    for index in sorted(set(flipped) | dependent, reverse=True):
        prefix = (
            prefix[: top_level[index].start()] + prefix[top_level[index + 1].start() :]
        ).rstrip(" ,;:")
    if len(final) >= budget:
        # The result does not fit even alone. A title without it is weaker, while a truncated
        # predicate would overstate or invert it.
        return _head_words(prefix, budget, language)
    # Drop whole trailing conjuncts of the earlier text first, so the cut lands on a member boundary
    # instead of inside a prepositional phrase.
    for cut in reversed(top_level[:keep]):
        if len(prefix) <= budget - len(final):
            break
        prefix = claim[: cut.start()].rstrip(" ,;:")
    head = _head_words(prefix, budget - len(final), language)
    return f"{head}{final}" if head else _head_words(claim, budget, language)


def _trim_title_body(body: str, budget: int, language: str) -> str:
    """Keep the claim and the evidence that scopes it inside ``budget``."""
    clauses = [
        clause.strip().rstrip(" ,;:")
        for clause in _TITLE_CLAUSE_BREAK.split(body)
        if clause.strip(" ,;:")
    ]
    if len(clauses) == 1:
        # A single long claim has no later clause to preserve.
        return _trim_single_claim(clauses[0], budget, language) or body[:budget]
    # A negated claim is the one clause whose head must survive even when it does not fit whole:
    # dropping it changes the result to its opposite.
    negative_index = next(
        (
            index
            for index, clause in enumerate(clauses[:-1])
            if _title_negation(language).search(clause)
        ),
        None,
    )
    kept = _strip_dangling_tail(_compact_subordinate_detail(clauses[-1], language))
    for index in range(len(clauses) - 2, -1, -1):
        clause = clauses[index]
        candidate = f"{clause}, {kept}"
        if len(candidate) <= budget:
            kept = candidate
            continue
        spare = budget - len(kept) - 2
        if index > 0 and index != negative_index:
            # A middle clause that does not fit is dropped whole; the first clause and a negated
            # claim still get their heads preserved.
            continue
        if spare <= 0:
            break
        partial = _head_words(clause, spare, language)
        if partial:
            kept = f"{partial}, {kept}"
        if index == 0 or index == negative_index:
            break
    if len(kept) > budget:
        # Never tail-cut a claim: the last words may be the predicate of a longer clause, so a head
        # trim keeps at least the subject and its negation.
        kept = _head_words(kept, budget, language) or kept[:budget]
    if not kept:
        # A single unbreakable token still has to fit the schema cap.
        kept = body[:budget]
    # A clause boundary can still leave "…and"/"…при" at the end of the last kept clause; the title
    # must read as a claim, not as a cut-off fragment.
    return _strip_dangling_tail(kept)


def _fit_title_with_verdict(title: str, suffix: str, language: str) -> str:
    """Keep the mandatory verdict and the condition that scopes it inside the cap."""
    budget = _TITLE_LIMIT - len(suffix)
    body = title.strip().rstrip(" .;,")
    if budget <= 0:
        return suffix.removeprefix("; ").strip()[:_TITLE_LIMIT]
    if len(body) > budget:
        body = _trim_title_body(body, budget, language)
    if not body:
        return suffix.removeprefix("; ").strip()[:_TITLE_LIMIT]
    return (body + suffix)[:_TITLE_LIMIT]


def ensure_explicit_inconclusive_title(
    result: AnalysisResult,
    evidence: str,
    *,
    response_language: str | None = None,
    multi_source: bool = False,
) -> AnalysisResult:
    """Carry an explicit no-winner verdict into the title. The prompt requires this, but live
    evaluation showed a stochastic omission that silently drops a conclusion the source states.
    """
    if multi_source or not states_inconclusive_comparison(evidence):
        return result
    claims_winner = title_claims_comparison_winner(result.title)
    # A title that states the verdict and also claims a winner keeps the defect the replacement
    # exists to remove, so only a title without the claim is a no-op.
    if title_states_inconclusive(result.title) and not claims_winner:
        return result
    language = inconclusive_title_language(result.title, response_language, evidence)
    suffix = inconclusive_title_suffix(result.title, response_language, evidence)
    if language is None or not suffix:
        return result
    if claims_winner:
        statement = inconclusive_title_statement(language)
        return result.model_copy(update={"title": statement[:_TITLE_LIMIT]})
    title = _fit_title_with_verdict(result.title, suffix, language)
    return result.model_copy(update={"title": title})


class Analyzer:
    """Own bounded analysis and isolated topic-classification provider boundaries."""

    def __init__(
        self, provider: LlmProvider, chunk_size_chars: int = 12_000, overlap_chars: int = 0
    ):
        self.provider = provider
        # Chunking belongs to the application boundary; both final LLM stages receive the same
        # persisted, bounded evidence projection.
        self.chunk_size_chars = chunk_size_chars
        self.overlap_chars = overlap_chars

    async def analyze(
        self,
        content: NormalizedContent,
        session: AsyncSession,
        user_id: int,
        profile: UserProfile,
        item_id: int | None = None,
    ) -> AnalysisResult:
        content = await self.prepare_content(content, session, item_id)
        categories = await existing_categories(session, user_id)
        # The provider call can take seconds; finish the read transaction first so another worker
        # can claim/update its SQLite outbox rows while analysis runs.
        await session.commit()
        result = await self.provider.analyze(content, profile, categories)
        return ensure_explicit_inconclusive_title(
            result,
            analysis_signal_projection(content.text),
            response_language=response_language_for(content, profile),
            multi_source=has_multiple_sources(content.metadata),
        )

    async def prepare_content(
        self,
        content: NormalizedContent,
        session: AsyncSession,
        item_id: int | None = None,
    ) -> NormalizedContent:
        """Prepare one bounded, durable evidence projection shared by both LLM stages."""
        # Strip output-control commands before chunking: a checkpoint generated from an injected
        # chunk would otherwise preserve the distortion.
        prepared_text = analysis_text_projection(content.text)
        # The model, chunking and the opt-in evaluation read this sanitized projection.
        content = content.model_copy(update={"text": prepared_text})
        if len(prepared_text) > self.chunk_size_chars:
            if item_id is None:
                raise ValueError("item_id is required for durable long-content analysis")
            chunks = split_text(prepared_text, self.chunk_size_chars, self.overlap_chars)
            summaries = await self._durable_chunk_summaries(session, item_id, chunks)
            content = content.model_copy(
                update={
                    "text": bounded_chunk_aggregate(
                        summaries, chunk_size_chars=self.chunk_size_chars
                    ),
                    "metadata": {
                        **content.metadata,
                        "_analysis_chunk_summary_count": len(summaries),
                    },
                }
            )
        return content

    @staticmethod
    def topic_classification_content(
        content: NormalizedContent,
    ) -> NormalizedContent | None:
        """Keep user intent out of the canonical category evidence boundary."""
        metadata = {
            key: content.metadata[key]
            for key in ("visual_notes", "source_count", "successful_source_count")
            if key in content.metadata
        }
        source_count = content.metadata.get("source_count")
        successful_source_count = content.metadata.get("successful_source_count")
        if (
            type(source_count) is int
            and source_count > 0
            and type(successful_source_count) is int
            and successful_source_count == 0
        ):
            if content.source_context and content.source_context.strip():
                return content.model_copy(
                    update={"text": "", "user_note": None, "metadata": metadata}
                )
            return None
        return content.model_copy(update={"user_note": None, "metadata": metadata})

    async def classify_topic(
        self,
        content: NormalizedContent,
        session: AsyncSession,
        user_id: int,
    ) -> TopicClassificationResult | None:
        """Run the independent category stage after its primary analysis checkpoint."""
        topic_content = self.topic_classification_content(content)
        if topic_content is None:
            return None
        categories = await existing_categories(session, user_id)
        # End the category read transaction before the independent provider call.
        await session.commit()
        return await self.provider.classify_topic(topic_content, categories)

    async def _durable_chunk_summaries(
        self, session: AsyncSession, item_id: int, chunks: list[str]
    ) -> list[str]:
        """Persist each expensive summary before requesting final analysis."""
        rows = (
            await session.scalars(
                select(Content)
                .where(
                    Content.item_id == item_id,
                    Content.kind == ContentKind.CHUNK_SUMMARY,
                )
                .order_by(Content.id)
            )
        ).all()
        # Close the lookup transaction before any provider call; each completed replacement is then
        # committed as its own restart-safe checkpoint.
        await session.commit()

        rows_by_index: dict[int, list[Content]] = {}
        for row in rows:
            metadata = row.metadata_json or {}
            chunk_index = metadata.get("chunk_index")
            if metadata.get("stage") == "chunk" and type(chunk_index) is int and chunk_index >= 0:
                rows_by_index.setdefault(chunk_index, []).append(row)

        summaries = []
        used_row_ids = set()
        for index, chunk in enumerate(chunks):
            chunk_sha256 = hashlib.sha256(chunk.encode()).hexdigest()
            candidates = rows_by_index.get(index, [])
            row = next(
                (
                    candidate
                    for candidate in reversed(candidates)
                    if (metadata := candidate.metadata_json or {}).get("stage") == "chunk"
                    and metadata.get("chunk_index") == index
                    and metadata.get("chunk_size_chars") == self.chunk_size_chars
                    and metadata.get("overlap_chars") == self.overlap_chars
                    and metadata.get("chunk_sha256") == chunk_sha256
                    and metadata.get("generator_version") == CHUNK_SUMMARY_GENERATOR_VERSION
                    and candidate.text.strip()
                ),
                None,
            )
            if row is None:
                # Chunks already went through ``analysis_text_projection``, so a second filter here
                # could delete a quotation whose attribution was split across the boundary.
                summary = (
                    await self.provider.summarize_chunk(chunk, sanitize_untrusted=False)
                ).strip()
                if not summary:
                    raise LlmError("INVALID_LLM_OUTPUT", "empty chunk summary")
                summary = summary[: self.chunk_size_chars]
                metadata = {
                    "stage": "chunk",
                    "chunk_index": index,
                    "chunk_size_chars": self.chunk_size_chars,
                    "overlap_chars": self.overlap_chars,
                    "chunk_sha256": chunk_sha256,
                    "generator_version": CHUNK_SUMMARY_GENERATOR_VERSION,
                }
                if candidates:
                    # Replacing the derived row keeps old prompt generations out of FTS/Ask instead
                    # of accumulating duplicate summaries.
                    row = candidates[-1]
                    row.text = summary
                    row.metadata_json = metadata
                else:
                    row = Content(
                        item_id=item_id,
                        kind=ContentKind.CHUNK_SUMMARY,
                        text=summary,
                        metadata_json=metadata,
                    )
                    session.add(row)
                    rows_by_index.setdefault(index, []).append(row)
                await session.commit()
            used_row_ids.add(row.id)
            summaries.append(row.text)

        stale_rows = [row for row in rows if row.id not in used_row_ids]
        if stale_rows:
            for row in stale_rows:
                await session.delete(row)
            await session.commit()
        return summaries


_OMISSION_MARKER = " … "
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
# A checkpoint summary is already compressed prose.
_EVIDENCE_CUE = re.compile(
    r"\d|\b(?:result|finding|found|shown?|measured|increas\w*|decreas\w*|reduc\w*|"
    r"improv\w*|worse|better|no conclusion|inconclusive|unresolved|caveat|however|"
    r"but|only|except|limit\w*|revers\w*|fail\w*|final|answer|conclusion|"
    r"winner|won|ahead|evenly\s+matched|neither|mixed|undetermined|undecided|"
    r"no verdict|"
    r"итог\w*|результат\w*|вывод\w*|выясн\w*|показа\w*|сниз\w*|увелич\w*|улучш\w*|"
    r"хуже|лучше|не удалось|однако|только|кроме|огранич\w*|обратн\w*|"
    r"побед\w*|переміг\w*|перемог\w*|зрештою|загалом|підсумк\w*|"
    r"в\s+конце\s+концов|в\s+кінці\s+кінців|"
    r"неопределен\w*|смешанн\w*|противореч\w*|невизначен\w*|суперечлив\w*|змішан\w*)\b",
    re.IGNORECASE,
)


# A late practical answer ("Use lifecycle-aware collection to prevent duplicate requests.") has no
# measurement cue, but it is still the conclusion the summary must keep.
_FINAL_ANSWER_CUE = re.compile(
    r"(?i)^(?:use|avoid|prefer|choose|adopt|apply|enable|disable|keep|switch|"
    r"migrate|replace|store|cache|call|wrap|collect|implement|configure|set|"
    r"do\s+not|don't|never)\b"
    r"|\b(?:should|recommend\w*|best\s+practice|to\s+prevent|to\s+avoid|"
    r"to\s+reduce|чтобы\s+избежать|следует|рекоменд\w*|используй\w*|избегай\w*)\b"
)


def _sentences(text: str) -> list[str]:
    """Split compact summary prose into sentences without losing trailing content."""
    parts = [part.strip() for part in _SENTENCE_BOUNDARY.split(text.strip()) if part.strip()]
    return parts or [text.strip()]


def _sentence_priority(sentence: str, index: int, count: int) -> int:
    score = 4 if index == 0 else 0
    cued = _EVIDENCE_CUE.search(sentence) is not None
    if index == count - 1 and cued:
        # A final verdict or recap earns its position only when it carries evidence; a cue-less
        # closing sentence must not displace a measured result.
        score += 4
    if cued:
        score += 3
    return score


def _render_sentences(sentences: list[str], chosen: set[int]) -> str:
    """Join selected sentences in source order and mark every omitted gap."""
    pieces: list[str] = []
    skipped = False
    for index, sentence in enumerate(sentences):
        if index in chosen:
            if skipped and pieces:
                pieces.append(_OMISSION_MARKER.strip())
                skipped = False
            pieces.append(sentence)
        else:
            skipped = True
    return " ".join(pieces)


def _answer_sentence_index(sentences: list[str]) -> int | None:
    """The latest sentence that carries a result, verdict, limitation, or recommendation."""
    count = len(sentences)
    if count <= 1:
        return None
    if _EVIDENCE_CUE.search(sentences[-1]) or _FINAL_ANSWER_CUE.search(sentences[-1]):
        return count - 1
    for index in range(count - 2, 0, -1):
        if _EVIDENCE_CUE.search(sentences[index]):
            return index
    return None


def _select_sentences(sentences: list[str], limit: int) -> str | None:
    """Keep complete sentences, preferring cued outcomes and the latest caveat."""
    count = len(sentences)
    answer_index = _answer_sentence_index(sentences)
    # The first sentence carries the question/goal; the answer carries the result.
    mandatory = {0}
    if answer_index is not None and answer_index != 0:
        mandatory.add(answer_index)
    used = len(_render_sentences(sentences, mandatory))
    if used > limit:
        if answer_index is None or answer_index == 0:
            return None
        mandatory = {answer_index}
        used = len(_render_sentences(sentences, mandatory))
        if used > limit:
            return None

    priorities = {
        index: _sentence_priority(sentences[index], index, count) for index in range(count)
    }
    chosen = set(mandatory)
    # Equal cues: keep the later sentence. A late conclusion must outrank earlier setup when a
    # compressed checkpoint cannot retain everything.
    ordered_candidates = sorted(
        (index for index in range(count) if index not in chosen),
        key=lambda index: (priorities[index], index),
        reverse=True,
    )
    for index in ordered_candidates:
        # +1 for the joining space and marker space keeps the bound conservative.
        if used + len(sentences[index]) + 1 + len(_OMISSION_MARKER) <= limit:
            chosen.add(index)
            used += len(sentences[index]) + 1

    rendered = _render_sentences(sentences, chosen)
    # The greedy estimate can be off by a marker or two; drop the weakest optional sentence until
    # the exact rendered text fits.
    while len(rendered) > limit and chosen - mandatory:
        weakest = min(chosen - mandatory, key=lambda index: (priorities[index], index))
        chosen.remove(weakest)
        rendered = _render_sentences(sentences, chosen)
    return rendered if len(rendered) <= limit else None


def _bounded_summary(text: str, limit: int) -> str:
    """Keep the evidence that must survive when one checkpoint has to shrink."""
    if len(text) <= limit:
        return text
    if limit <= len(_OMISSION_MARKER) + 2:
        return text[:limit]
    selected = _select_sentences(_sentences(text), limit)
    if selected is not None:
        return selected
    body = limit - len(_OMISSION_MARKER)
    head = (body + 1) // 2
    tail = body - head
    return text[:head].rstrip() + _OMISSION_MARKER + text[-tail:].lstrip()


def bounded_chunk_aggregate(summaries: list[str], *, chunk_size_chars: int) -> str:
    """Aggregate ordered checkpoints within one bounded analyzer input."""
    if not summaries:
        return ""
    separator = "\n\n"
    labels = [f"CHUNK {index + 1}/{len(summaries)}:\n" for index in range(len(summaries))]
    label_budget = sum(map(len, labels)) + len(separator) * (len(summaries) - 1)
    summary_budget = chunk_size_chars - label_budget
    if summary_budget < len(summaries):
        # Small test/config budgets cannot fit useful labels; retain equal-share truncation so each
        # chunk still contributes when the bound permits.
        budget = max(1, chunk_size_chars - len(separator) * (len(summaries) - 1))
        per_summary = max(1, budget // len(summaries))
        aggregate = separator.join(_bounded_summary(summary, per_summary) for summary in summaries)
        return aggregate[:chunk_size_chars]

    per_summary = summary_budget // len(summaries)
    # These order labels are prompt framing only; durable rows keep plain summaries so search/export
    # never expose analyzer bookkeeping.
    return separator.join(
        f"{label}{_bounded_summary(summary, per_summary)}"
        for label, summary in zip(labels, summaries)
    )


async def existing_categories(session: AsyncSession, user_id: int) -> list[str]:
    # Категории — динамические строки, отдельная таблица не нужна (DISTINCT достаточно).
    rows = await session.scalars(
        select(Item.category).where(Item.user_id == user_id, Item.category.is_not(None)).distinct()
    )
    return sorted(rows.all())
