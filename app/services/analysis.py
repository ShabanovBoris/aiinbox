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
    inconclusive_title_suffix,
    states_inconclusive_comparison,
    title_states_inconclusive,
)
from app.llm.base import LlmError, LlmProvider
from app.llm.untrusted import without_control_instructions
from app.storage.models import Content, Item

# Increment when chunk-summary semantics change so durable evidence is not silently
# reused after a prompt change that asks the model to preserve different information.
CHUNK_SUMMARY_GENERATOR_VERSION = 5


def analysis_text_projection(text: str) -> str:
    """The single sanitized evidence text shared by analysis and evaluation.

    Prompt construction, chunking, and the deterministic inconclusive-title repair
    must all see the same text; otherwise a command removed from the model input
    can still change the generated title, and the evaluation would not reproduce
    production behavior.
    """
    return without_control_instructions(text)


def split_text(text: str, chunk_size_chars: int, overlap_chars: int = 0) -> list[str]:
    """Split into bounded chunks while preferring complete paragraph boundaries."""
    if chunk_size_chars <= 0 or not 0 <= overlap_chars < chunk_size_chars:
        raise ValueError("chunk_size_chars must be positive and overlap smaller than chunk size")
    if not text:
        return []
    # ❌ Удален fixed-step character slicing: он игнорировал требуемые PRODUCT_SPEC
    # paragraph boundaries и разрывал абзац даже когда рядом был безопасный split.
    chunks = []
    start = 0
    while start < len(text):
        hard_end = min(start + chunk_size_chars, len(text))
        end = hard_end
        if hard_end < len(text):
            # Do not cut inside the repeated overlap from the previous chunk;
            # the chosen boundary must add fresh content before advancing.
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
# A long title keeps the final clause because it usually carries the verdict and
# the condition that scopes it ("…, but the gain reversed under peak load"), while
# the claim clause keeps its head (subject/verb/negation) even when a short label
# precedes it or the clause itself must shrink.
_TITLE_CLAUSE_BREAK = re.compile(
    r"(?<=[,;:])\s+|\s+(?=\b(?:but|however|yet|while|whereas|но|однако)\b)"
)
# A clause cut at a word budget can end on a function word ("…accuracy in",
# "…coverage and"). Such a fragment is not a shorter claim: it is broken syntax in
# the main user-facing field, so the dangling words are dropped. Negations are
# never dropped because they invert the claim. Every language the repair can write
# needs its own function words, otherwise a German or Spanish cut ends on "…und".
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
# A list introducer cannot end a claim. A budget cut can land right after it
# ("…live production traffic including" + a kept later member), which leaves broken
# coordination in the title ("…including and the gain reversed"), so the introducer
# goes with the truncated list instead of staying in the user-facing field.
_TITLE_DANGLING_INTRODUCER = (
    r"including|incl\.|such|включая|включаючи|например|наприклад|einschließlich|"
    r"incluyendo|incluindo|inclusief|włączając"
)
_TITLE_DANGLING_TAIL = re.compile(
    rf"(?i:\s+(?:{_TITLE_FUNCTION_WORDS}|{_TITLE_DANGLING_INTRODUCER}))\s*$"
    # A single alternative label ("…winner was A", "…вариант А") is content, not
    # an article. Only the lower-case article/conjunction is a dangling tail.
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


# A budget cut can land inside the last coordinated conjunct ("…полное покрытие и
# меньше"), which reads as an unfinished comparison rather than a shorter claim.
# The cut conjunct is dropped whole unless it carries the negation. Coordination and
# negation are language-specific: Spanish "en" ("in") is not the Dutch coordinator
# "en", and a German "…verbesserte sich nicht" must be kept exactly like a negated
# English member instead of being cut into a claim that says accuracy improved.
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
# A Latin single letter stays case-sensitive so the English pronoun "I" and an
# alternative label ("option A") are never read as coordination.
_TITLE_CASE_SENSITIVE_COORDINATORS = frozenset({"y", "o", "e", "i"})
# A subordinating conjunction opens an explanatory clause. A coordinator inside it
# belongs to that clause ("… when the team doubled the window and added unrelated
# requests"), so it is not a top-level member of the claim and must not displace the
# result the earlier member states. The first word of the clause is
# language-specific for the same reason as coordination. Temporal conjunctions state
# the condition that scopes a reversal ("… the gain reversed after the team doubled
# the window"), so they belong here too.
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
# "after", "before" and "as" are also prepositions ("after the migration", "as
# expected"), and treating a prepositional phrase as a clause would hide a later
# result. Such a conjunction only counts when a clause follows it: a subject and a
# finite verb, because "after the migration" has a bare noun where "after the team
# doubled the window" has subject and verb. The subject need not carry a determiner
# ("as costs rose", "after batching was doubled"), while a single noun, a
# determiner-headed participle or a participle with its own noun ("after the
# migration", "as expected", "after the planned maintenance", "after the first
# failed attempt") is still a prepositional phrase.
_TITLE_GUARDED_SUBORDINATORS = frozenset({"after", "before", "as"})
# Unambiguous finite forms; a regular past tense is matched separately because it
# is also the shape of an attributive participle ("the planned maintenance") and of
# a few nouns ("a speed test").
_TITLE_FINITE_VERB = (
    r"(?:is|are|was|were|be|been|has|have|had|does|do|did|will|would|can|could|"
    r"may|might|must|should|grew|rose|fell|won|lost|cut|ran|led|beat|hit|went|"
    r"came|became|made|took|got|kept|held|shrank|doubled|tripled)"
)
# A determiner never stands alone as a subject, so it cannot open the bare-subject
# branch: "after the planned maintenance" is not "after" + "the" + verb "planned".
_TITLE_DETERMINER = r"(?:the|a|an|its|their|our|his|her|this|that|these|those)"
_TITLE_CLAUSE_SUBJECT = rf"(?:{_TITLE_DETERMINER}\s+(?!\w+ed\b)\w+|(?!{_TITLE_DETERMINER}\b)\w+)"
# "after the first failed attempt" is determiner + ordinal + participle + noun, so
# a weak past tense only counts when no content word follows it directly ("after
# the team doubled the batch window" keeps its object, "failed attempt" does not).
# Both readings err towards a prepositional phrase, which leaves the later result
# in the title instead of hiding it behind a condition. The cost is a real clause
# with a bare direct object ("after the team planned maintenance"), documented in
# D-058.
_TITLE_WEAK_VERB = rf"\w+ed\b(?!\s+(?!(?:{_TITLE_FUNCTION_WORDS}|{_TITLE_DETERMINER})\b)\w)"
_TITLE_CLAUSE_SUBJECT_VERB = re.compile(
    rf"(?i)^\s*{_TITLE_CLAUSE_SUBJECT}\s+(?:{_TITLE_FINITE_VERB}\b|{_TITLE_WEAK_VERB})"
)
# A coordinated member that states a reversal negates the headline result the claim
# opened with, so losing it inverts the claim. A contracted negation ("didn't hold")
# states the same thing as "did not hold", so both count.
_TITLE_REVERSAL_CUES = (
    r"revers\w*|"
    r"no\s+(?:gain|improvement|effect|change|reduction|difference)|"
    r"\w+n['’]t|"
    r"(?:did|does|do|could|would|can|will|should)\s+not|never|not|"
    r"смени\w*|обратн\w*|не\s+удал\w*|не|ни|без"
)
# A limitation or a second result caps or qualifies the claim without inverting it, so
# it may yield to a reversal but not to neutral detail or to a plain result. The list
# names the failure and degradation predicates of both supported outcome languages, so
# a member that states them is a result even without a negation in front of it
# ("сборка провалилась", "the build degraded"). The break form excludes the compounds
# that are not failures ("breakthrough", "breakfast", "breakout", "breakpoint"), and
# "завис" excludes "зависеть" so only the stalled form counts. A Russian worsening
# ("ухудш*", "деград*") is a limitation like the English degradation it translates: it
# caps the claim instead of inverting it, so a real reversal still outranks it.
_TITLE_LIMITATION_CUES = (
    r"limit\w*|fail\w*|degrad\w*|worse\w*|break(?!through|fast|out|point)\w*|crash\w*|stall\w*|"
    r"огранич\w*|провал\w*|ухудш\w*|деград\w*|сломал\w*|рухнул\w*|зависл\w*|завис\b"
)
# A measured decline or loss is a result too, even when the verb is an everyday one:
# "the gain declined after …" must not be displaced by neutral detail, and it must
# not displace a reversal that scopes the headline ("reduced cost … and the gain
# reversed"), which is why it is a separate list.
_TITLE_DECLINE_CUES = (
    r"declin\w*|drop\w*|fell|fall\w*|slow\w*|decreas\w*|reduc\w*|lower\w*|"
    r"shr[ae]nk|shrink\w*|lost|lose\w*|"
    r"снизил\w*|снижа\w*|снижен\w*|упал\w*|упасть|просел\w*|уменьшил\w*|"
    r"сократил\w*|замедлил\w*|потерял\w*|ухудшил\w*"
)
# A member that opens with a possessive refers back to the subject of the member
# before it. Splicing that flipped member out leaves "its latency improved" without
# the subject it belongs to, so the dependent member goes with it.
_TITLE_DEPENDENT_MEMBER = re.compile(
    r"\s*(?:its|their|his|her|его|её|ее|их|свой|своя|свою|свои|своей|своих)\b",
    re.IGNORECASE,
)
_TITLE_REVERSAL_MEMBER_CUE = re.compile(rf"(?i)\b(?:{_TITLE_REVERSAL_CUES})\b")
_TITLE_LIMITATION_MEMBER_CUE = re.compile(rf"(?i)\b(?:{_TITLE_LIMITATION_CUES})\b")
_TITLE_RESULT_MEMBER_CUE = re.compile(
    rf"(?i)\b(?:{_TITLE_REVERSAL_CUES}|{_TITLE_LIMITATION_CUES}|{_TITLE_DECLINE_CUES})\b"
)
# Negation polarity decides what a cue word means. Negating a predicate that already
# names a negative outcome asserts the positive one ("the build didn't fail", "was
# not limited"), so the cue word must not mark that member as a result; "didn't hold"
# and "did not improve" stay negative results. Negating a coverage verb scopes what
# the study covered ("didn't include mobile users", "didn't cover cold starts"), which
# caps the claim like a limitation instead of inverting it, so it is folded into the
# limitation cue. The predicate lists name English and Russian outcomes because both
# are supported response languages; a negation in any other supported language still
# reads as the language table defines it, but no predicate of that language is listed
# yet, so the rule leaves those titles unchanged.
_EN_NEGATION_PREFIX = r"(?:\b(?:not|never|cannot)\b|\w+n['’]t)"
_TITLE_NEGATED_OUTCOMES = (
    r"fail|break|crash|stall|degrad|declin|drop|fall|fell|worse|worsen|lose|lost|limit|"
    r"shrink|slow|"
    r"ухудш|деградир|упал|упасть|просел|провал|огранич|сломал|рухнул|зависл"
)
# The qualifier construction also reads the noun form of the Russian degradation stem:
# "не совсем показала деградацию" names the degradation a light verb reports, while
# the one-word free gap only reads the verb form. Keeping the noun stem out of the
# free gap leaves "не устранила деградацию" (the degradation persisted) the negative
# result it states instead of reading it as a restatement.
_TITLE_QUALIFIED_OUTCOMES = rf"{_TITLE_NEGATED_OUTCOMES}|деград"
_TITLE_NEGATED_SCOPE_VERBS = r"include|cover|contain|encompass|включ|охват|покры"
# A negation may be separated from its predicate by one word that leaves the
# negation intact ("didn't really fail", "did not outright fail", "was not a
# failure", "сборка не была провалом"), so the prefix still reads the predicate with
# that polarity. Focus, degree and litotes words are a separate construction and are
# listed below; they are read first, because they scope the negation instead of
# leaving it intact. A longer qualifier sequence is a separate
# construction too, because a two- or three-word window can hold the whole copular or
# light-verb phrase in front of the failure ("was not a complete failure", "did not
# show any degradation", "не показала никаких ухудшений"); widening this free gap to
# that window read the failure noun as the object of a content verb ("did not resolve
# the degradation" reports that the degradation persisted) as a restatement and let
# the member leave the title.
_TITLE_NEGATION_GAP = r"(?:\w+\s+)?"
# A focus word after the negation means the negation scopes *that* word, not the
# predicate: "not only failed" and "didn't merely decline" still report the failure,
# "не без ограничений" still reports the limitation, "не сильно упала" still reports
# the decline.
_TITLE_FOCUS_WORDS = (
    r"only|merely|just|simply|without|"
    r"только|лишь|раз|без|сильно|слишком|столь"
)
# A frequency or degree word qualifies how far the negation reaches, so the member
# reports the failure without its full extent ("didn't fully fail", "не полностью
# проваливалась") instead of asserting the positive restatement a plain negated
# predicate reads ("didn't fail"): the predicate keeps its own limitation cue, the
# negation and the degree word are read as scope. The word only scopes
# the negation when a failure or decline predicate follows; before a positive
# predicate ("didn't fully recover") the negation is the whole claim and stays a
# reversal.
_TITLE_DEGREE_WORDS = (
    r"always|consistently|typically|fully|completely|entirely|outright|necessarily|"
    r"всегда|полностью|совсем"
)
# Two or three words between the negation and the failure predicate are a copular or
# light-verb phrase that qualifies the same negation ("was not a complete failure",
# "did not entirely show degradation", "did not show any degradation", "не была
# полным провалом", "не показала никаких ухудшений"). The phrase may also be the
# object of a content verb, where the failure noun is what the source measured and
# the member stays a negative result ("did not resolve the degradation"). A
# one-word window cannot tell those apart from its shape alone, so the longer
# sequence is read as the limitation it always states: the member keeps the
# predicate's own cue, keeps its wording, and still yields to a real reversal.
_TITLE_QUALIFIER_GAP = r"(?:\w+\s+){2,3}"


@cache
def _title_negation_words(language: str) -> str:
    """The negation alternatives of ``language``, without the gap after them.

    The English prefix is always recognised, because a title in another language may
    quote an English technical phrase ("the build didn't fail"); ``language`` adds its
    own table. "no" is left out of the English branch: the reversal cue list already
    reads "no improvement" as a result, and widening the prefix would silently drop
    that member.
    """
    if language == "en":
        return _EN_NEGATION_PREFIX
    return rf"(?:{_EN_NEGATION_PREFIX}|\b(?:{_TITLE_NEGATIONS[language]})\b)"


@cache
def _title_negation_prefix(language: str) -> str:
    """The negation words that precede a result predicate in ``language``.

    One word may stand between the two, because it can leave the negation intact
    ("did not outright fail", "was not a failure", "не была провалом") and the member
    then states a positive restatement instead of the earliest reversal. Narrowing
    the gap to a fixed adverb list read those forms as a reversal and let them
    displace the real reversal the source reports; focus words are
    still stripped first, so the narrower focus construction keeps its own meaning.
    A two- or three-word phrase in front of the failure is read by
    ``_title_qualified_negation`` instead.
    """
    return rf"{_title_negation_words(language)}\s+{_TITLE_NEGATION_GAP}"


@cache
def _title_focus_negation(language: str) -> re.Pattern[str]:
    """A negation that scopes a following focus word instead of the predicate.

    Removing the negation and its focus word leaves the predicate's own cue, so "not
    only failed" keeps the limitation cue of "fail" and no longer reads as a reversal
    through the bare negation. Reading the whole phrase as a reversal let a positive
    restatement displace the reversal that follows, and a title cut inside "not only
    …" promised a continuation that the trim then dropped.
    """
    return re.compile(
        rf"{_title_negation_words(language)}\s+(?:{_TITLE_FOCUS_WORDS})\s+", re.IGNORECASE
    )


@cache
def _title_degree_negation(language: str) -> re.Pattern[str]:
    """A negation whose extent one or more following degree words limit.

    The lookahead requires a failure or decline predicate, so the match covers
    "didn't fully fail", "didn't always fully fail" and "не всегда проваливалась":
    removing the negation and the degree words leaves the predicate's own limitation
    cue, which is what the member states. A degree word that is itself followed by a
    copular or light-verb phrase is read by ``_title_qualified_negation``. A positive
    predicate keeps the negation as its reversal, because "didn't fully recover" would
    otherwise lose the only cue it has.
    """
    return re.compile(
        rf"{_title_negation_words(language)}\s+(?:(?:{_TITLE_DEGREE_WORDS})\s+)+"
        rf"(?=(?:{_TITLE_NEGATED_OUTCOMES})\w*)",
        re.IGNORECASE,
    )


@cache
def _title_qualified_negation(language: str) -> re.Pattern[str]:
    """A negation separated from its predicate by a qualifier phrase.

    "was not a complete failure", "did not entirely show degradation", "did not show
    any degradation", "не была полным провалом" and "не показала никаких ухудшений"
    all state the failure the negation qualifies, and so does a phrase where the
    failure noun is the object of a content verb ("did not resolve the degradation"
    reports that the degradation persisted). The phrase is read as a limitation: the
    negation and the qualifier words leave the cue text, the predicate keeps its own
    cue, and the member keeps its wording. Reading the one-word gap as the only
    qualifier left these members as reversals through their bare negation, so the
    qualified failure displaced the reversal the source reports.
    """
    return re.compile(
        rf"{_title_negation_words(language)}\s+{_TITLE_QUALIFIER_GAP}"
        rf"(?=(?:{_TITLE_QUALIFIED_OUTCOMES})\w*)",
        re.IGNORECASE,
    )


@cache
def _title_negated_positive(language: str) -> re.Pattern[str]:
    return re.compile(
        rf"{_title_negation_prefix(language)}(?:{_TITLE_NEGATED_OUTCOMES})\w*", re.IGNORECASE
    )


@cache
def _title_negated_scope(language: str) -> re.Pattern[str]:
    return re.compile(
        rf"{_title_negation_prefix(language)}(?:{_TITLE_NEGATED_SCOPE_VERBS})\w*", re.IGNORECASE
    )


def _result_cue_text(text: str, language: str) -> str:
    """``text`` with negation polarity applied before result-cue matching.

    Every cue lookup goes through this function, because a member reads differently
    once its negation is classified: reading the bare words made "the build didn't
    fail" the earliest result and let it displace the later real reversal, so a title
    asserted that the build survived and omitted the gain that reversed.
    The negation is read in the response language, so a Russian "сборка не деградировала"
    is the same positive outcome as "the build didn't fail" instead of the earliest
    reversal. "не улучшилась" and "не снизилась" negate a positive
    measurement, so they stay negative results exactly as "didn't improve" does. A
    focus construction is read first: "not only failed" and "не без ограничений" lose
    the negation and the focus word, so the member keeps the failure or the limitation
    as its own result instead of becoming a reversal through the bare negation. A
    frequency or degree word scopes the negation the same way when a failure or
    decline predicate follows, so "didn't fully fail" keeps the failure as a
    limitation, while "didn't fully recover" keeps the negation as its own reversal.
    A determiner, auxiliary or light verb between the degree word and the predicate
    does not change that reading ("was not entirely a failure", "did not entirely
    show degradation"), and a two- or three-word qualifier phrase in front of the
    predicate keeps the predicate's cue as a limitation too ("did not show any
    degradation", "was not a complete failure", "не показала никаких ухудшений"),
    so the member yields to a real reversal instead of displacing it.
    """
    cue_text = _title_focus_negation(language).sub(" ", text)
    cue_text = _title_degree_negation(language).sub(" ", cue_text)
    cue_text = _title_qualified_negation(language).sub(" ", cue_text)
    cue_text = _title_negated_positive(language).sub(" ", cue_text)
    return _title_negated_scope(language).sub(" limited", cue_text)


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
    """Coordination separators of ``language`` for a word-budget cut.

    ``language`` is one of the ``_INCONCLUSIVE_SUFFIX`` keys: the repair never asks
    for a language it cannot write, so the per-language tables are the contract.
    """
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
    """The first subordinating conjunction that actually opens a clause.

    "when"/"когда" always open one; "after"/"before"/"as" are also prepositions, so
    they count only when a subject and a finite verb follow ("after the team doubled
    the window" but not "after the migration"): a prepositional phrase is not an
    explanatory clause and must not hide a later result.
    """
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
    """Drop a one-word prepositional fragment a budget cut left behind.

    "…в тестовой среде с неизменным" is a cut inside "с неизменным профилем" and
    reads as broken syntax. The phrase is dropped only when it cannot be complete:
    a single word after the preposition, or nothing but function words.
    """
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
    """The first words of ``text`` that fit ``budget`` characters.

    A text that fits is returned untouched: the cleanup below only repairs a
    fragment a cut produced, so a complete final conjunct is never dropped.
    """
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
    """Drop a neutral coordinated member inside the clause's subordinate clause.

    The subordinate clause states the condition that scopes the result, so it stays;
    a member coordinated inside it ("and added unrelated requests") is detail. A
    negation inside it is a limitation, never detail, so then the clause stays whole.
    The function only ever truncates, so its result is a prefix of ``clause``.
    """
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
    """The earliest coordinated member whose loss would strengthen the claim.

    A reversal negates the headline result, a limitation caps it; either one outranks
    a later second result or detail, because the title without it reads as a stronger
    outcome than the source states. A reversal outranks a limitation, so "the scope
    was limited … and the gain reversed" keeps the reversal. A plain decline is
    direction-neutral ("the gain declined" but also "cost declined"), so it only wins
    over neutral detail, which the trim handles by re-trimming without that detail.
    ``end`` bounds a member that the subordinate clause follows, so text inside the
    clause cannot mark the member before it as the result. ``language`` decides which
    negation words flip a member's polarity before its cue words are read.
    """
    for cue in (_TITLE_REVERSAL_MEMBER_CUE, _TITLE_LIMITATION_MEMBER_CUE):
        for index, cut in enumerate(cuts):
            # ``end`` bounds the member the condition clause follows; a member that
            # starts after the clause is outside it and keeps its own tail.
            bound = end if end is not None and cut.start() < end else None
            if cue.search(
                _result_cue_text(_coordinated_member(claim, cuts, index, bound), language)
            ):
                return index
    return None


def _polarity_flipped_member(member: str, language: str) -> bool:
    """Whether negation alone produced this member's cue words.

    "the build didn't fail" and "сборка не деградировала" contain a cue word only
    because their negation was not applied yet; once the negation is read, the member
    asserts that an expected failure did not happen. That is not the outcome the
    source measured, so it is detail: when the same claim keeps a real reversal, the
    member goes and the title states the result the source reports.
    """
    return (
        _TITLE_RESULT_MEMBER_CUE.search(member) is not None
        and _TITLE_RESULT_MEMBER_CUE.search(_result_cue_text(member, language)) is None
    )


def _trim_single_claim(claim: str, budget: int, language: str) -> str:
    """Keep the result a one-clause claim states inside ``budget``.

    A single clause has no later clause to fall back on, but it can still coordinate
    its outcome ("… and the gain reversed under peak load"). The member that states
    the result or the limitation is kept whole while the earlier text keeps its head;
    cutting a result member instead states a stronger result than the source, because
    the dropped words are "reversed", "declined" or "nicht".
    """
    cuts = list(_title_conjunct(language).finditer(claim))
    subordinate = _subordinate_clause(claim, language)
    clause_end = subordinate.start() if subordinate is not None else None
    if clause_end is not None:
        # A coordinator inside the subordinate clause belongs to that clause, not to
        # the claim's top level: "… when the team doubled the window and added
        # unrelated requests" explains the reversal instead of adding a second
        # result. Treating it as a member kept "and added …" and dropped the
        # reversal and its condition.
        top_level = [cut for cut in cuts if cut.start() < clause_end]
    else:
        top_level = cuts
    # The dominant member is read over every coordinator: after polarity is applied,
    # the member before the condition clause can state a positive outcome ("the build
    # didn't fail") while the reversal the source reports sits behind it.
    dominant = _dominant_result_member(claim, cuts, language, clause_end)
    if dominant is not None and clause_end is not None and cuts[dominant].start() >= clause_end:
        # The reversal follows the condition clause, so it is the claim's result and
        # its own tail stays; the earlier member contributes no result once its
        # negation is read.
        top_level = cuts
        keep = dominant
    elif top_level:
        keep = len(top_level) - 1
        if dominant is not None and dominant < keep:
            # Two coordinated members can each state a result. Keeping the later one
            # head-cut the prefix, so the reversal and its condition disappeared while
            # the title still claimed a measured gain; the earlier
            # reversal or limitation is what the claim's headline depends on.
            keep = dominant
    else:
        # The clause opens the claim, so every coordinator sits after it. The earliest
        # reversal or limitation is still the result the title must keep; without one,
        # the latest member that states any result wins over the clause's head cut.
        if dominant is not None:
            top_level = cuts[dominant:]
            keep = 0
        else:
            for index in range(len(cuts) - 1, -1, -1):
                member_end = cuts[index + 1].start() if index + 1 < len(cuts) else len(claim)
                if _TITLE_RESULT_MEMBER_CUE.search(
                    _result_cue_text(claim[cuts[index].end() : member_end], language)
                ):
                    top_level = cuts[index:]
                    break
            else:
                return _head_words(claim, budget, language)
            keep = 0
    last = top_level[keep]
    member_end = top_level[keep + 1].start() if keep + 1 < len(top_level) else len(claim)
    compacted = claim
    if subordinate is not None and subordinate.start() > last.end():
        # The clause states the condition that scopes the result, so it survives. A
        # coordinated member of its own ("and added unrelated requests") is neutral
        # detail and is dropped instead of being promoted to the kept result.
        compacted = _compact_subordinate_detail(claim, language)
    tail = claim[last.end() : min(member_end, len(compacted))].rstrip(" ,;:")
    if all(_TITLE_FUNCTION_WORD.fullmatch(word) for word in tail.split()):
        # A final member made only of service words is a cut fragment, not a result.
        return _head_words(claim, budget, language)
    if (
        _TITLE_RESULT_MEMBER_CUE.search(_result_cue_text(tail, language)) is None
        and _TITLE_RESULT_MEMBER_CUE.search(_result_cue_text(claim[: last.start()], language))
        is not None
    ):
        # The final coordinated member adds neutral detail, while the claim before it
        # states the reversal or the limitation. Keeping the detail cut the reversal
        # away and credited the subject with work it never did, so the neutral member
        # is dropped and the remaining claim is trimmed again.
        return _trim_single_claim(claim[: last.start()].rstrip(" ,;:"), budget, language)
    final = f"{last.group()}{tail}"
    prefix = claim[: last.start()].rstrip(" ,;:")
    # A member whose cue came only from a negated negative predicate states no result
    # once polarity is read ("the build didn't fail"), so it is detail and goes. It is
    # spliced out on its own instead of truncating the prefix: the members after it
    # may state the limitation the source reports, and the word budget drops the
    # conjunct nearest the result first, which would keep the positive restatement and
    # lose that limitation.
    flipped = [
        index
        for index in range(keep)
        if _polarity_flipped_member(
            _coordinated_member(claim, top_level, index, clause_end), language
        )
    ]
    # A member that opens with a possessive refers back to the flipped member's
    # subject ("the build didn't fail and its latency improved"): removing only the
    # subject leaves the pronoun without an antecedent, so the dependent member goes
    # with it. The kept result member is never dropped this way.
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
        # The result does not fit even alone. A title without it is weaker, while a
        # truncated predicate would overstate or invert it.
        return _head_words(prefix, budget, language)
    # Drop whole trailing conjuncts of the earlier text first, so the cut lands on a
    # member boundary instead of inside a prepositional phrase.
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
        # A single long claim has no later clause to preserve. Cutting its head
        # would turn "Preloading did not improve accuracy" into "improve accuracy",
        # so the claim keeps its head and its final result member.
        return _trim_single_claim(clauses[0], budget, language) or body[:budget]
    # A negated claim is the one clause whose head must survive even when it does
    # not fit whole: dropping it changes the result to its opposite.
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
            # A middle clause that does not fit is dropped whole; the first clause
            # and a negated claim still get their heads preserved.
            continue
        if spare <= 0:
            break
        partial = _head_words(clause, spare, language)
        if partial:
            kept = f"{partial}, {kept}"
        if index == 0 or index == negative_index:
            break
    if len(kept) > budget:
        # Never tail-cut a claim: the last words may be the predicate of a longer
        # clause, so a head trim keeps at least the subject and its negation.
        kept = _head_words(kept, budget, language) or kept[:budget]
    if not kept:
        # A single unbreakable token still has to fit the schema cap.
        kept = body[:budget]
    # A clause boundary can still leave "…and"/"…при" at the end of the last kept
    # clause; the title must read as a claim, not as a cut-off fragment.
    return _strip_dangling_tail(kept)


def _fit_title_with_verdict(title: str, suffix: str, language: str) -> str:
    """Keep the mandatory verdict and the condition that scopes it inside the cap.

    The verdict is the conclusion the repair exists to preserve, so an over-long
    title drops whole middle clauses and keeps the claim's subject/verb/negation,
    rather than leaving a truncated predicate plus the suffix.
    """
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
    """Carry an explicit no-winner verdict into the title.

    The prompt requires this, but live evaluation showed a stochastic omission that
    silently drops a conclusion the source states. The repair appends only that
    verdict and keeps every measured detail the model wrote; it never invents a
    result or a winner. A title that already states the verdict is unchanged.
    Independent sources get no invented shared verdict, and the suffix follows the
    actual response language (an untranslatable language leaves the title alone
    rather than leaking English into it). The repair is append-only by design: if the
    model's title already claims a winner while the evidence is unresolved, the
    verdict is still appended and the contradiction is treated as a model deviation
    left visible for manual review, because a deterministic guard that
    suppresses the verdict whenever the title claims a winner would also let an
    injected or hallucinated win erase the source's verdict.
    """
    if multi_source or not states_inconclusive_comparison(evidence):
        return result
    if title_states_inconclusive(result.title):
        return result
    language = inconclusive_title_language(result.title, response_language, evidence)
    suffix = inconclusive_title_suffix(result.title, response_language, evidence)
    if language is None or not suffix:
        return result
    title = _fit_title_with_verdict(result.title, suffix, language)
    return result.model_copy(update={"title": title})


class Analyzer:
    """Own bounded analysis and isolated topic-classification provider boundaries.

    Profile relevance and canonical topic evidence stay separate so either LLM
    stage can resume without repeating the other.
    """

    def __init__(
        self, provider: LlmProvider, chunk_size_chars: int = 12_000, overlap_chars: int = 0
    ):
        self.provider = provider
        # Chunking belongs to the application boundary; both final LLM stages
        # receive the same persisted, bounded evidence projection.
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
        # The provider call can take seconds; finish the read transaction first so
        # another worker can claim/update its SQLite outbox rows while analysis runs.
        await session.commit()
        result = await self.provider.analyze(content, profile, categories)
        return ensure_explicit_inconclusive_title(
            result,
            content.text,
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
        # Strip output-control commands before chunking: a checkpoint generated
        # from an injected chunk would otherwise preserve the distortion, and a
        # final aggregate filter cannot recover evidence that was never summarized.
        prepared_text = analysis_text_projection(content.text)
        # The model, deterministic repair, chunking, and the opt-in evaluation all
        # read this one sanitized projection. Returning the raw short text here let
        # a removed command still drive the repair decision.
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
        # Close the lookup transaction before any provider call; each completed
        # replacement is then committed as its own restart-safe checkpoint.
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
                summary = (await self.provider.summarize_chunk(chunk)).strip()
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
                    # Replacing the derived row keeps old prompt generations out
                    # of FTS/Ask instead of accumulating duplicate summaries.
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
            # ❌ Удалены устаревшие CHUNK_SUMMARY: FTS и Ask fallback должны видеть
            # только текущие checkpoints, а не старые версии или прежнюю разбивку.
            for row in stale_rows:
                await session.delete(row)
            await session.commit()
        return summaries


_OMISSION_MARKER = " … "
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
# A checkpoint summary is already compressed prose. When the aggregate budget is
# smaller than the checkpoint, drop whole filler sentences instead of cutting a
# result sentence in half. These cues only rank sentences for retention; they never
# create or rewrite evidence.
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


# A late practical answer ("Use lifecycle-aware collection to prevent duplicate
# requests.") has no measurement cue, but it is still the conclusion the summary
# must keep. This only ranks retention; it never rewrites the sentence.
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
        # A final verdict or recap earns its position only when it carries
        # evidence; a cue-less closing sentence must not displace a measured result.
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
    """The latest sentence that carries a result, verdict, limitation, or recommendation.

    A cue-less closing recap is not an answer: on a small budget it must not displace
    the measured result that precedes it.
    """
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
    # When both cannot fit, the answer wins: a question with no conclusion is worse
    # than a conclusion without its setup.
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
    # Equal cues: keep the later sentence. A late conclusion must outrank earlier
    # setup when a compressed checkpoint cannot retain everything.
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
    # The greedy estimate can be off by a marker or two; drop the weakest optional
    # sentence until the exact rendered text fits. Every mandatory sentence stays.
    while len(rendered) > limit and chosen - mandatory:
        weakest = min(chosen - mandatory, key=lambda index: (priorities[index], index))
        chosen.remove(weakest)
        rendered = _render_sentences(sentences, chosen)
    return rendered if len(rendered) <= limit else None


def _bounded_summary(text: str, limit: int) -> str:
    """Keep the evidence that must survive when one checkpoint has to shrink.

    Character slicing silently drops a conclusion or caveat that sits inside a
    long checkpoint. Sentence-aware selection keeps the opening and the latest
    outcome plus cued result/limitation sentences, and marks every omitted gap
    instead of pretending the summary was complete.
    """
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
    """Aggregate ordered checkpoints within one bounded analyzer input.

    Shared with the opt-in quality evaluation so manual review exercises exactly
    the production aggregation instead of a reimplementation.
    """
    if not summaries:
        return ""
    separator = "\n\n"
    labels = [f"CHUNK {index + 1}/{len(summaries)}:\n" for index in range(len(summaries))]
    label_budget = sum(map(len, labels)) + len(separator) * (len(summaries) - 1)
    summary_budget = chunk_size_chars - label_budget
    if summary_budget < len(summaries):
        # Small test/config budgets cannot fit useful labels; retain equal-share
        # truncation so each chunk still contributes when the bound permits.
        budget = max(1, chunk_size_chars - len(separator) * (len(summaries) - 1))
        per_summary = max(1, budget // len(summaries))
        aggregate = separator.join(_bounded_summary(summary, per_summary) for summary in summaries)
        return aggregate[:chunk_size_chars]

    per_summary = summary_budget // len(summaries)
    # These order labels are prompt framing only; durable rows keep plain
    # summaries so search/export never expose analyzer bookkeeping.
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
