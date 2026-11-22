"""Deterministic evidence signals shared by prompt construction and result checks.

These helpers only recognise what the captured evidence explicitly states. They
never decide the answer, invent a conclusion, or rewrite measured content.
"""

import re
import sys
from bisect import bisect_right
from collections.abc import Mapping

_FOLD = str.maketrans({"ё": "е", "Ё": "Е"})
_VERDICT_ACTIONS = r"pick|choose|select|declare|establish|determine|produce|identify|name"

# An explicit inconclusive verdict in either word order. A bare "inconclusive"
# counts only as a label/standalone verdict, so explanatory prose such as "the
# article explains why comparisons can be inconclusive" does not claim a result.
_INCONCLUSIVE_OUTCOME = re.compile(
    r"(?i)(?:"
    rf"\b(?:did|do|does|could|can|was|were|unable|failed)\s+(?:not\s+|to\s+)"
    rf"(?:{_VERDICT_ACTIONS})\s+(?:a\s+|the\s+|any\s+)?"
    r"(?:clear\s+|single\s+|overall\s+|definitive\s+)?winner\b"
    # Passive order: "A winner could not be determined."
    r"|\b(?:a\s+|the\s+|any\s+)?winner\s+(?:could|can|would|might)\s+not\s+be\s+"
    r"(?:determined|established|chosen|declared|selected|picked|identified|named)\b"
    r"|\bwinner\s+(?:(?:was|is|were|are|remains?|remained)\s+)?"
    r"(?:not\s+(?:determined|established|chosen|declared|selected|picked|"
    r"identified|named)|undetermined|undecided|unresolved)\b"
    r"|\bno\s+(?:clear\s+|single\s+|overall\s+|definitive\s+)?winner\b"
    # "The winner was never announced" states the absence of a verdict, not a win.
    r"|\b(?:a\s+|the\s+)?winner\s+(?:was|is|has\s+been)?\s+never\s+"
    r"(?:announced|declared|determined|established|chosen|selected|named|"
    r"identified|picked)\b"
    r"|\b(?:results?|findings?|comparisons?|outcomes?|verdicts?|trials?|"
    r"studies|study|analyses|analysis|runs?|rounds?|tests?|attempts?|"
    r"experiments?|pilots?|evaluations?|replications?)"
    r"\b[^.;]{0,30}?\b(?:is|was|were|are|remains?|remained|seemed?|proved?)\s+"
    r"(?:\w+\s+){0,2}inconclusive\b"
    r"|\binconclusive\s+(?:results?|findings?|outcomes?|comparisons?|verdicts?)\b"
    r"|(?:^|(?<=[.!?]\s))inconclusive\b"
    # "mixed with legacy data" describes combining datasets, not a verdict.
    r"|\b(?:results?|findings?|comparisons?|outcomes?|verdicts?)\s+"
    r"(?:were|are|was|is|remained?|remains?)\s+mixed\b(?!\s+(?:with|into|in)\b)"
    r"|\bmixed\s+results?\b"
    # A null result is an explicit no-winner statement even without "winner".
    r"|\bno\s+(?:meaningful|significant|clear|measurable|statistically\s+significant)\s+"
    r"(?:difference|improvement|advantage)\b"
    r"|\bno\s+difference\b"
    r"|\bno\s+(?:of\s+)?(?:the\s+)?(?:\w+\s+){0,2}?"
    r"(?:method|approach|model|option|variant|system|tool|strategy|algorithm|"
    r"technique|configuration|baseline|run|trial|test|result|condition)s?\s+"
    r"(?:was|is|were|are|performed|did|fared)\s+"
    r"(?:clearly\s+|significantly\s+)?(?:better|superior|best)\b"
    r"|\b(?:comparisons?|outcomes?|verdicts?|results?)\b[^.;]{0,30}?"
    r"\b(?:remains?|was|is)\s+(?:an?\s+)?(?:open|unsettled|undecided|unresolved)"
    r"(?![\w-]|\s+(?:source|sources|ended|ending|access|data|standards?|format|"
    r"letter)\b)\b"
    r"|\b(?:too\s+(?:few|small|limited|little)|not\s+enough)\b[^.;]{0,60}?"
    r"\bto\s+(?:pick|choose|decide|determine|declare)\b"
    # A named comparison that stays open is an explicit no-winner statement. A
    # bare "question remained unresolved" is unrelated uncertainty unless the
    # sentence actually names the thing being compared.
    r"|\b(?:comparison|verdict)\b[^.;]{0,30}?"
    r"\b(?:remained?|remains?|stayed?|stays?|was|is|were|are)\s+(?:an?\s+)?"
    r"(?:open|unresolved|unsettled|undecided)(?![\w-]|\s+(?:source|sources|ended|"
    r"ending|access|data|standards?|format|letter)\b)\b"
    r"|\b(?:question|issue|matter)\b[^.;]{0,80}?"
    r"\b(?:whether|which|who)\b[^.;]{0,80}?"
    r"\b(?:better|worse|superior|inferior|winner|outperform\w*|best|faster|"
    r"compare\w*)\b[^.;]{0,40}?"
    r"\b(?:remained?|remains?|stayed?|stays?|was|is|were|are)\s+(?:an?\s+)?"
    r"(?:open|unresolved|unsettled|undecided)(?![\w-]|\s+(?:source|sources|ended|"
    r"ending|access|data|standards?|format|letter)\b)\b"
    r"|\bwhether\b[^.;]{0,80}?"
    r"\b(?:better|worse|superior|inferior|winner|outperform\w*|best|faster)\b"
    r"[^.;]{0,40}?\b(?:remains?|remained?|stays?|stayed?|is|was)\s+(?:an?\s+)?"
    r"(?:open|unresolved|unsettled|undecided)(?![\w-]|\s+(?:source|sources|ended|"
    r"ending|access|data|standards?|format|letter)\b)\b"
    r"|\b(?:current|latest|recent|follow-up|second|confirmatory)\s+"
    r"(?:replication|run|study|trial|experiment|test|attempt|evaluation|analysis)\b"
    r"[^.;]{0,30}?\b(?:was|is|remained?|remains?|proved|seemed)\s+"
    r"(?:\w+\s+){0,2}inconclusive\b"
    r"|\bneither\s+(?:of\s+)?(?:the\s+|these\s+|those\s+|both\s+)?(?:\w+\s+){0,2}?"
    r"(?:was|is|were|are|proved|emerged)\s+"
    r"(?:clearly\s+|significantly\s+|measurably\s+|consistently\s+)?"
    r"(?:better|superior|ahead)\b"
    # An open "which is better" question is a comparison verdict; "unclear which
    # GPU the pilot used" is unrelated uncertainty.
    r"|\bunclear\s+(?:which|who|whose)\b[^.;]{0,80}?"
    r"\b(?:better|worse|superior|inferior|faster|slower|wins?|won|winner|"
    r"outperform\w*|best)\b"
    r"|\bevenly\s+matched\b|\btoo\s+close\s+to\s+call\b"
    # Russian forms after ё folding.
    r"|\bпобедител\w*\s+(?:так\s+и\s+)?не\s+"
    r"(?:выбран|определен|установлен|найден|назван)\w*\b"
    r"|\bне\s+удалось\s+(?:выбрать|определить|установить|назвать)\s+победителя\b"
    r"|\b(?:результат\w*|вывод\w*|сравнени\w*|итог\w*)\s+"
    r"(?:оказал\w*ся\s+|были\s+|был\w*\s+|остал\w*ся\s+)?"
    r"(?:смешанн\w*|неопределенн\w*|противоречив\w*)\b"
    r"|\b(?:смешанн\w*|неопределенн\w*|противоречив\w*)\s+результат\w*\b"
    # A bare predicate needs a comparison subject ("the forecast was uncertain"
    # is unrelated uncertainty, not a verdict about two alternatives).
    r"|\b(?:результат\w*|вывод\w*|итог\w*|исход\w*|сравнени\w*|выбор\w*)\b"
    r"[^.;]{0,40}?\b(?:остает\w*ся|остал\w*ся|был\w*|были|являет\w*ся)\s+"
    r"неопределенн\w*\b"
    # An open question and an undecided comparison in Russian and Ukrainian.
    r"|\bвопрос\w*\s+(?:остал\w*ся|остает\w*ся|остается|был\w*|являет\w*ся)\s+"
    r"(?:открыт\w*|нерешенн\w*|неразрешенн\w*)"
    r"|\b(?:неясно|непонятно),?\s+(?:какой|какая|какое|кто|что)\b"
    r"|\b(?:оба|оба\s+подхода|методы)\s+(?:оказал\w*ся|были|был)\s+"
    r"(?:примерно\s+)?равн\w*\b"
    r"|\bпитання\s+(?:залишил\w*ся|залишаєть\w*ся|бул\w*)\s+відкрит\w*"
    r"|\b(?:незрозуміло|неясно),?\s+(?:який|яка|яке|хто|що)\b"
    # German and Spanish forms so a non-English verdict still reaches the guard.
    r"|\bkein(?:e|er)?\s+(?:klare[rn]?\s+)?(?:sieger|gewinner)\b"
    r"|\b(?:sieger|gewinner)\s+(?:konnte|lies|ließ)\s+(?:sich\s+)?nicht\s+"
    r"(?:ermittelt|bestimmt|gew(?:ae|ä)hlt|gewahlt)\s+werden\b"
    r"|\bergebnisse?\s+(?:waren|sind)\s+(?:gemischt|uneindeutig)\b"
    r"|\bno\s+se\s+(?:pudo|podria|podría)\s+(?:determinar|establecer)\s+"
    r"(?:un\s+)?ganador\b"
    r"|\bsin\s+(?:un\s+)?ganador\b"
    r"|\bresultados\s+(?:fueron|son)\s+mixtos\b"
    # Ukrainian forms: "Переможця не визначено", "результати були невизначеними".
    r"|\bперемож\w*\s+(?:так\s+і\s+)?не\s+"
    r"(?:(?:був|була|було|були)\s+)?"
    r"(?:визнач|обра|встанов|назва)\w*\b"
    r"|\bне\s+вдалося\s+(?:визначити|обрати|встановити|назвати)\s+переможця\b"
    r"|\b(?:результат\w*|виснов\w*|порівнян\w*)\s+"
    r"(?:були|був|була|було|є|залишил\w*ся)?\s*"
    r"(?:невизначен\w*|суперечлив\w*|змішан\w*)\b"
    r"|\b(?:невизначен\w*|суперечлив\w*|змішан\w*)\s+результат\w*\b"
    r"|\bжодн\w*\s+перемож\w*\s+не\s+(?:був|була|було|були)?\s*"
    r"(?:визнач|обра|встанов|назва)\w*\b"
    r")"
)
# The deterministic repair must write the verdict in the actual response
# language. A missing translation is better than an English suffix inside a
# German, Spanish, or Japanese title.
_INCONCLUSIVE_SUFFIX = {
    "en": "; no winner was established",
    "ru": "; победитель не определён",
    "uk": "; переможця не визначено",
    "de": "; kein Sieger wurde ermittelt",
    "es": "; no se determinó un ganador",
    "fr": "; aucun gagnant n'a été établi",
    "it": "; nessun vincitore è stato stabilito",
    "pt": "; nenhum vencedor foi determinado",
    "nl": "; geen winnaar werd bepaald",
    "pl": "; nie wyłoniono zwycięzcy",
    "tr": "; kazanan belirlenmedi",
}
# One claim boundary is shared by the no-winner detector, the reversal classifier,
# and the evaluation oracle. A sentence pronoun ("it", "this result") may carry the
# previous claim, but a coordinator starts a new claim with its own subject.
CLAIM_CLAUSE_BREAK = re.compile(
    r"(?i)[.;:,!?]|\b(?:but|however|although|though|whereas|yet|and|or|while|"
    r"но|однако|а|и|или)\b"
)


# A claim clause is a sentence fragment, so the scan is bounded: a verdict-heavy
# document must stay linear instead of rescanning everything before each match.
_CLAUSE_WINDOW = 400


def clause_before(text: str, index: int) -> str:
    """Return the claim clause ending at ``index`` using the shared boundary."""
    prefix = text[max(0, index - _CLAUSE_WINDOW) : index]
    breaks = list(CLAIM_CLAUSE_BREAK.finditer(prefix))
    return prefix[breaks[-1].end() :] if breaks else prefix


_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_UKRAINIAN = re.compile(
    r"(?i)[ІіЇїЄєҐґ]|\b(?:перемож\w*|визнач\w*|невизнач\w*|порівнян\w*|"
    r"жодн\w*|суперечлив\w*|змішан\w*)\b"
)
# When no profile/content language is supplied, the deterministic suffix must not
# assume that every Latin-script title is English. These markers only select a
# translation; they never rewrite the title itself.
_GERMAN = re.compile(
    r"(?i)\b(?:der|die|das|und|kein\w*|wurde|wurden|waren|war|ist|sind|"
    r"ergebnis\w*|sieger\w*|gewinner\w*|gemischt|vergleich\w*|ermittelt|"
    r"bestimmt)\b"
)
_SPANISH = re.compile(
    r"(?i)\b(?:el|la|los|las|resultados?|ganador\w*|fueron|son|comparación|"
    r"mixtos|determinó)\b"
)
_FRENCH = re.compile(
    r"(?i)\b(?:les?|des?|résultats?|gagnant\w*|aucun\w*|ont|était|étaient|"
    r"comparaison|établi)\b"
)
_ENGLISH = re.compile(
    r"(?i)\b(?:the|and|but|was|were|are|is|no|not|winner|results?|comparison|"
    r"mixed|inconclusive|established|determined)\b"
)
# A verdict may itself be negated ("were not inconclusive", "did not remain
# mixed") or superseded by a later decisive win. "Neither method won", "None of
# the runs won" and "Ни один подход не победил" negate the win they contain.
_NEGATED_VERDICT = re.compile(
    r"(?i)(?:\b(?:not|never|no|no\s+longer|none|neither|nor|nobody|nothing|"
    r"не|ни|никто|ничего|жодн\w*)\b|n't)\s*(?:\w+\s+){0,4}$"
)
# "The results were not inconclusive" negates the adjective inside the matched
# phrase, so a prefix-only check would still report a verdict the source rejects.
_NEGATED_INCONCLUSIVE = re.compile(
    r"(?i)\b(?:not|never|no\s+longer)\s+(?!only\b)(?:\w+\s+){0,3}"
    r"(?:inconclusive|mixed|undecided|unresolved|unsettled|open)\b"
    r"|n't\s+(?:\w+\s+){0,3}(?:inconclusive|mixed|undecided|unresolved)\b"
    r"|\bне\s+(?:\w+\s+){0,3}"
    r"(?:неопределенн|смешанн|противоречив|нерешенн|неразрешенн)\w*\b"
)
_DECISIVE_WIN = re.compile(
    r"(?i)(?:"
    # A bare "won" is the ordinary way a final round settles a comparison, but
    # "won praise", "won an award" and "won the contract" are not outcomes.
    r"(?<!['’])\bwon\b(?!['’]t)"
    r"(?!\s+(?:the\s+|a\s+|an\s+)?(?:praise|awards?|prizes?|recognition|attention|"
    r"acclaim|support|approval|backing|funding|contracts?|elections?|votes?|seats?|"
    r"traction|users?|customers?|market\s+share|hearts|fans|admirers|bidding)\b)"
    r"|\b(?:clearly|decisively|overall|comfortably|consistently)\s+won\b"
    r"|\b(?:was|is|were|are|proved|proves|emerged)\s+(?:as\s+)?(?:the\s+|a\s+)?"
    r"(?:clear\s+|overall\s+|decisive\s+|outright\s+)?winner\b"
    r"|\b(?:became|becomes)\s+(?:the\s+)?"
    r"(?:clear\s+|overall\s+|decisive\s+|outright\s+)?winner\b"
    r"|\bwinner\s+(?:was|is|became)\b"
    # "The winner was never announced" is an explicit absence, not a win.
    r"(?!\s+(?:never|not|unclear|unknown|undetermined|undecided|unresolved|"
    r"missing|absent)\b)"
    r"|\b(?:declared|established|determined|produced|identified|selected|named|"
    r"announced|found|measured|revealed)\s+(?:a\s+|the\s+)?(?:\w+\s+){0,2}winner\b"
    r"|\b(?:declared|named|announced)\s+(?:\w+\s+){1,3}?as\s+(?:the\s+)?"
    r"(?:clear\s+|overall\s+|decisive\s+|outright\s+)?winner\b"
    r"|\ba\s+(?:clear\s+|single\s+|overall\s+|decisive\s+|outright\s+)?winner\s+"
    r"(?:emerged|was\s+(?:established|determined|selected))\b"
    r"|\b(?:final|overall|aggregate)\s+results?\s+"
    r"(?:favou?red|showed|confirmed)\s+(?:that\s+)?(?:\w+\s+){0,4}?"
    r"(?:ahead|winner|better|superior|best|first|outperform\w*)\b"
    r"|\b(?:comparison|question)\s+(?:was\s+)?resolved\s+in\s+favou?r\s+of\b"
    r"|\boutperform(?:ed|s)?\b"
    r"|\bcame\s+out\s+ahead\b"
    r"|\b(?:clearly|strongly|consistently)\s+favou?red\b"
    r"|\bfavou?red\s+(?:the\s+)?(?:method|approach|strategy|option|model|version|"
    r"variant|system|tool|design|configuration|baseline|[a-z]\d*)\b"
    # "beat expectations" and "beat the record" are not comparison outcomes.
    r"|\bbeat(?:s|en)?\b(?!\s+(?:\w+\s+){0,2}(?:expectations?|estimates?|"
    r"forecasts?|projections?|consensus|records?|odds|targets?)\b)"
    r"|\b(?:clearly|decisively|comfortably|consistently)\s+superior\b"
    r"|\b(?:was|is|were|are|proved|proven|performed)\s+"
    r"(?:clearly\s+|consistently\s+|significantly\s+|slightly\s+)?superior\b"
    r"|\bclearly\s+(?:better|superior)\b"
    # A later, better-powered result settles what an earlier round left open.
    r"|\b(?:larger|bigger|full|final|follow-up|subsequent|later|second|confirmatory)\s+"
    r"(?:\w+\s+){0,2}?(?:trial|study|experiment|test|run|round|replication|"
    r"evaluation|analysis)\b[^.;]{0,50}?"
    r"\b(?:found|showed|confirmed|demonstrated|revealed)\b[^.;]{0,40}?"
    r"\b(?:superior|better|best|first|ahead|higher)\b"
    # "found a better way to run the test" is a recommendation, not a comparison
    # result; the better/superior claim needs a comparison predicate.
    r"|\b(?:found|showed|confirmed|demonstrated|revealed)\s+(?:that\s+)?"
    r"(?:\w+\s+){1,4}?\b(?:was|were|performed|fared|ranked|scored|did)\s+"
    r"(?:clearly\s+|significantly\s+)?(?:superior|better|best)\b"
    # "the test was better designed" evaluates design quality, not a comparison.
    r"(?!\s+(?:\w+ed|known|suited|understood|documented|maintained|written|"
    r"structured|organized|explained|described|named|equipped|prepared|"
    r"positioned)\b)"
    r"|\b(?:found|showed|confirmed|demonstrated|revealed)\s+(?:that\s+)?"
    r"(?:\w+\s+){1,3}?\b(?:superior|outperformed?)\b"
    r"|\bperformed\s+(?:better|best)\b"
    r"|\brank(?:ed|s)?\s+(?:\w+\s+){0,3}?(?:first|higher|above|ahead)\b"
    r"|\bplaced\s+(?:\w+\s+){0,3}?first\b"
    r"|\bcame\s+(?:in\s+)?first\b|\bfinished\s+(?:in\s+)?first\b"
    r"|\bput\s+(?:\w+\s+){0,3}?ahead\b"
    # A confirmed superlative ("the final benchmark found A was the fastest") is
    # a final standing, not merely an adjective. The frame requirement below keeps
    # "the fastest way to…" from becoming a comparison result.
    r"|\b(?:found|showed|confirmed|demonstrated|revealed)\b[^.;]{0,40}?"
    r"\b(?:was|were|is|are)\s+(?:the\s+)?"
    r"(?:fastest|slowest|best|most\s+(?:accurate|efficient|reliable|effective|"
    r"stable|scalable|secure))\b"
    r"|\b(?:was|were|is|are)\s+(?:the\s+)?"
    r"(?:fastest|slowest|best|most\s+(?:accurate|efficient|reliable|effective|"
    r"stable|scalable|secure))\b"
    r"|\b(?:scored|received|got|gave|earned)\b[^.;]{0,25}?\bhigher\s+score\b"
    r"|\bwas\s+(?:clearly\s+|significantly\s+|consistently\s+)?better\s+than\b"
    r"(?!\s+(?:expected|anticipated|predicted|forecast|usual|before|previously|"
    r"ever|planned|budgeted)\b)"
    r"|\bпобедил\w*\b|\bобогнал\w*\b|\bвыиграл\w*\b"
    r"|\bпобед\w*\s+достал\w*ся\b|\bпризнан\w*\s+победител\w*\b"
    r"|\bоказал\w*ся\s+лучше\b|\bоднозначно\s+лучше\b"
    r"|\bпоказал\w*\s+лучш\w*\s+результат\w*\b|\bзанял\w*\s+первое\s+место\b"
    r"|\bпоказал\w*\s+вищ\w*\s+результат\w*\b"
    r")"
)
# A win in a named sub-scope ("on small samples", "under peak load") is partial
# and does not settle the whole comparison unless the sentence also speaks overall.
_PARTIAL_WIN_SCOPE = re.compile(
    r"(?i)\b(?:on|in|for|under|with|at)\s+(?:the\s+|a\s+|an\s+)?"
    r"(?:small|large|larger|bigger|complex|simple|short|long|easy|hard|specific|"
    r"particular|narrow|single|subset|one|first|second|initial|early|later|final)\s+"
    r"(?:samples?|cases?|tasks?|workloads?|inputs?|queries|datasets?|benchmarks?|"
    r"rounds?|runs?|tests?|trials?|setups?|scenarios?|conditions?)\b"
    # A win on one measured dimension is partial when the verdict is still open
    # ("A won on speed, B won on stability"); an overall marker overrides it.
    r"|\b(?:on|in|for)\s+(?:the\s+)?(?:accuracy|speed|latency|throughput|stability|"
    r"memory|cost|recall|precision|f1|quality|security|coverage|size|energy|"
    r"runtime|time|price|reliability)\b"
    r"|\bfor\s+(?:small|large|complex|simple|short|long|easy|hard)\s+"
    r"(?:inputs?|tasks?|queries|cases?|samples?)\b"
    # "the tool was better at scale" is a scoped observation, not an overall win.
    r"|\bat\s+scale\b|\bin\s+production\b"
    # "won the small sample" names the sub-scope without a preposition.
    r"|\b(?:the\s+|a\s+|an\s+)?(?:small|large|larger|bigger|complex|simple|short|"
    r"long|easy|hard|specific|particular|narrow|single|subset|one|first|second|"
    r"initial|early|final)\s+"
    r"(?:samples?|cases?|tasks?|workloads?|inputs?|queries|datasets?|benchmarks?|"
    r"rounds?|runs?|tests?|trials?|setups?|scenarios?|conditions?)\b"
    r"|\b(?:lost|loses|lagged|trailed|fell\s+behind|was\s+slower|did\s+worse|"
    r"performed\s+worse|underperformed)\b"
)
# A ranking verb is only a comparison result when its sentence names a comparison
# or experiment. "Costs ranked first among user concerns" is not a winner.
_COMPARISON_CONTEXT = re.compile(
    r"(?i)\b(?:comparisons?|compared|versus|vs\.?|benchmarks?|trials?|studies|study|"
    r"experiments?|tests?|evaluations?|analys[ie]s|rounds?|runs?|replications?|"
    r"follow-ups?|methods?|approaches?|models?|systems?|tools?|strategies|"
    r"strategy|algorithms?|techniques?|baselines?|variants?|options?|"
    r"configurations?|agents?|prompts?|samples?|datasets?|workloads?|"
    r"results?|findings?|outcomes?|verdicts?|metrics?)\b"
)
# These predicates can describe a race, a quiz, a priority list, or a ranking of
# concerns. They need the comparison context above before they settle a verdict.
_AMBIGUOUS_WIN = re.compile(r"(?i)^(?:rank|ranked|ranks|placed|came|finished|put)\b")
_OVERALL_MARKER = re.compile(
    # "The overall comparison was inconclusive" names the comparison itself, not an
    # overall result, so the noun after the qualifier decides whether it frames one.
    r"(?i)\b(?:overall|final|aggregate|decisive|conclusive)\s+"
    r"(?:results?|outcome|winner|verdict|conclusions?|score|picture|pattern|"
    r"round|analysis|comparison\s+results?|trials?|runs?|tests?|experiments?|"
    r"studies|study|benchmarks?|replications?|evaluations?)\b"
    r"|\bconfirmed\b|\bacross\s+(?:all|both)\b|\bin\s+general\b|"
    r"\bon\s+average\b|\bacross\s+the\s+board\b|"
    r"\b(?:on|across)\s+(?:all|every)\s+(?:metrics?|tasks?|workloads?|benchmarks?|cases?)\b"
)
# A later win settles an earlier open verdict only when it is framed as a result
# of that comparison: a final/overall/larger/follow-up experiment. "Costs ranked
# first among user concerns", "our team won the office quiz" and "the tool was
# better at scale" are unrelated or scoped wins, not a resolution.
_LATER_RESULT_FRAME = re.compile(
    r"(?i)\b(?:overall|final|aggregate|decisive|conclusive)\s+"
    r"(?:\w+\s+){0,2}?(?:results?|outcome|winner|verdict|conclusions?|score|"
    r"picture|pattern|round|analysis|comparisons?\s+results?|trials?|runs?|"
    r"tests?|experiments?|studies|study|benchmarks?|replications?|evaluations?)\b"
    r"|\b(?:larger|bigger|full|follow-up|subsequent|later|second|confirmatory|"
    r"current|latest|extended|replication)\s+(?:\w+\s+){0,2}?"
    r"(?:results?|benchmarks?|trials?|studies|study|experiments?|tests?|runs?|"
    r"rounds?|replications?|evaluations?|analys[ie]s|comparisons?)\b"
    r"|\b(?:across\s+(?:all|both)|in\s+general|on\s+average|across\s+the\s+board)\b"
    r"|\b(?:after|following|across|over)\s+(?:\w+\s+){0,4}?"
    r"(?:runs?|rounds?|trials?|tests?|experiments?|replications?|evaluations?)\b"
    r"|\bfollow-up\b|\breplication\b"
)
# Some win forms are an explicit head-to-head verdict, not a bare "won" that a
# quiz or a priority list can also produce.
_UNAMBIGUOUS_WIN = re.compile(
    r"(?i)\b(?:clearly|decisively|comfortably|consistently)\s+"
    r"(?:superior|better|favou?red|won)\b"
    r"|\bbeat(?:s|en)?\b(?!\s+(?:\w+\s+){0,2}(?:expectations?|estimates?|"
    r"forecasts?|projections?|consensus|records?|odds|targets?)\b)"
    r"|\boutperform(?:ed|s)?\b"
    r"|\b(?:became|becomes|announced|declared|named|selected|established|"
    r"determined|produced|identified)\b[^.;]{0,30}?\b(?:the\s+)?"
    r"(?:clear\s+|overall\s+|decisive\s+|outright\s+)?winner\b"
    r"|\bwinner\s+(?:was|is|became)\b"
    r"|\bcomparison\b[^.;]{0,30}?\b(?:was\s+)?resolved\s+in\s+favou?r\s+of\b"
    r"|\bwas\s+(?:clearly\s+|significantly\s+|consistently\s+)?superior\b"
    r"|\b(?:won|wins|better|superior|best|first|ahead)\s+(?:overall|on\s+average)\b"
)
_CONTRAST_COORDINATOR = re.compile(
    r"(?i)\b(?:but|while|whereas|yet|although|though|and|однако|но|а|и|і)\b"
)
# For meta prose, "and" may join two embedded questions under one verb, so only
# a strong contrast ends the explanatory scope.
_META_STRONG_CONTRAST = re.compile(r"(?i)\b(?:but|while|whereas|yet|although|though|однако|но|а)\b")
# A decisive win in another experiment or source does not resolve the comparison
# that the inconclusive verdict was about.
_SEPARATE_COMPARISON = re.compile(
    r"(?i)\b(?:separate|different|another|independent|unrelated|distinct)\s+"
    r"(?:experiments?|studies|study|trials?|benchmarks?|comparisons?|tests?|"
    r"datasets?|tasks?|workloads?|sources?|videos?|articles?)\b"
    r"|\b(?:in|on)\s+(?:a\s+)?(?:separate|different|another)\s+"
    r"(?:experiment|study|trial|benchmark|test|dataset|task|workload)\b"
)
_SOURCE_MARKER = re.compile(r"(?im)^\s*(?:source|источник)\s*(\d+)\s*[:.]")
# A verdict attributed to prior work is background, not this source's conclusion.
# It must not append uncertainty to a title that reports the current result.
_PRIOR_WORK = re.compile(
    r"(?i)\b(?:prior|earlier|previous|past|existing|previously\s+published)\s+"
    r"(?:studies|study|research|work|results|findings|experiments|trials|"
    r"benchmarks|comparisons|analyses|reports|authors|teams|rounds|runs)\b"
    r"|\b(?:researchers|others|authors)\s+(?:previously|earlier|had\s+previously)\b"
    r"|\b(?:более\s+ранн\w*|предыдущ\w*|прежн\w*|прошл\w*|ранн\w*|попередн\w*|"
    r"раніше)\s+(?:исследован\w*|работ\w*|эксперимент\w*|испытан\w*|обзор\w*|"
    r"досліджень|робіт|експеримент\w*|даних)\b"
)
# "Mixed results from earlier studies are reviewed" attributes the verdict to
# prior work that follows the phrase; the current source reports no result of its own.
_PRIOR_WORK_AFTER = re.compile(
    r"(?i)^[^.;]{0,40}?(?:"
    r"\b(?:from|in|by|of|across)\s+(?:\w+\s+){0,3}?"
    r"(?:prior|earlier|previous|past|existing)\s+"
    r"(?:studies|study|research|work|results|findings|experiments|trials|"
    r"benchmarks|comparisons|analyses|reports|authors|teams|rounds|runs)\b"
    r"|\b(?:более\s+ранн\w*|предыдущ\w*|прежн\w*|прошл\w*|попередн\w*|раніше)\s+"
    r"(?:исследован\w*|работ\w*|эксперимент\w*|испытан\w*|обзор\w*|досліджень|робіт)"
    r")"
)
# Explanatory prose about a phenomenon ("the article explains why mixed results
# are common") describes the topic; it is not this material's own verdict.
_META_WHY_BEFORE = re.compile(
    r"(?i)\b(?:explain\w*|discuss\w*|describ\w*|review\w*|examin\w*|analyz\w*|"
    r"объясня\w*|описыва\w*|рассматрива\w*|обсужда\w*|разбира\w*|поясню\w*|"
    r"розповіда\w*|опису\w*|поясню\w*|"
    r"explica\w*|describ\w*|analiz\w*|discut\w*)\b[^.;]{0,50}?"
    r"\b(?:why|how|почему|зачем|как|чому|як|что|por\s+qu[eé])\b"
)
# "The article explains how comparisons work and why mixed results are common"
# coordinates two embedded questions under one explanatory verb. The second
# segment starts with a question word, so the meta verb still governs it; a plain
# "and" joining a main claim ("explains caching and mixed results were found")
# does not.
_META_COORDINATED = re.compile(
    r"(?i)\b(?:and|и)\s+(?:why|how|whether|почему|зачем|как|чому|як)\b"
    r"(?:(?!\b(?:and|but|however|но|и|а)\b)[^.;])*$"
)
_META_VERB = re.compile(
    r"(?i)\b(?:explain\w*|discuss\w*|describ\w*|review\w*|examin\w*|analyz\w*|"
    r"объясня\w*|описыва\w*|рассматрива\w*|обсужда\w*|разбира\w*|поясню\w*|"
    r"розповіда\w*|опису\w*|explica\w*|describ\w*|analiz\w*|discut\w*)\b"
)
_HYPOTHETICAL_WIN = re.compile(r"(?i)\b(?:whether|if|если|ли)\b")
_HEDGED_WIN = re.compile(
    r"(?i)\b(?:might|may|could|would|possibly|perhaps|potentially|"
    r"предположительно|возможно|вероятно)\b"
)
_RETRACTED_CLAIM = re.compile(
    r"(?i)\b(?:retract(?:ed|s)?|withdr(?:ew|awn|aws)|correct(?:ed|s)?|"
    r"revok(?:ed|es)?|overturn(?:ed|s)?)\b[^.;]{0,60}?"
    r"\b(?:that|this|the|its|their|earlier)?\s*"
    r"(?:win|winner|result|finding|conclusion|verdict|claim)\b"
    r"|\b(?:that|this|the)\s+(?:win|result|finding|conclusion|verdict|claim)\b"
    r"[^.;]{0,60}?\b(?:was|were|is|are|has\s+been|had\s+been)\s+"
    r"(?:\w+\s+){0,2}?(?:retracted|withdrawn|corrected|revoked|overturned)\b"
)
_RETRACTED_VERDICT = re.compile(
    r"(?i)\b(?:retract(?:ed|s)?|withdr(?:ew|awn|aws)|correct(?:ed|s)?|"
    r"revok(?:ed|es)?)\b[^.;]{0,60}?"
    r"\b(?:inconclusive|undetermined|mixed|no[- ]winner|uncertain\w*|"
    r"неопределенн\w*|смешанн\w*)\b"
    r"|\b(?:retract(?:ed|s)?|withdr(?:ew|awn|aws))\b[^.;]{0,40}?"
    r"\b(?:that|this|the)\s+(?:conclusion|finding|verdict|result)\b"
    r"|\b(?:inconclusive|undetermined|mixed|no[- ]winner)\b[^.;]{0,60}?"
    r"\b(?:was|were|is|are|has\s+been|had\s+been)\s+"
    r"(?:retracted|withdrawn|corrected|revoked)\b"
)
_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")


def _fold(text: str) -> str:
    """Case-fold and normalize ё for matching only; output is never rewritten."""
    return text.casefold().translate(_FOLD)


_clause_before = clause_before


def _negated_at(text: str, index: int) -> bool:
    return _NEGATED_VERDICT.search(_clause_before(text, index)) is not None


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    return [
        (match.start(), match.end()) for match in _SENTENCE.finditer(text) if match.group().strip()
    ]


def _sentence_at(spans: list[tuple[int, int]], starts: list[int], index: int) -> int | None:
    position = bisect_right(starts, index) - 1
    if position >= 0:
        start, end = spans[position]
        if start <= index < end:
            return position
    return None


def _source_markers(text: str) -> list[tuple[int, int]]:
    """Numbered source-block starts, built once per detection call."""
    return [(match.start(), int(match.group(1))) for match in _SOURCE_MARKER.finditer(text)]


def _source_at(markers: list[tuple[int, int]], index: int) -> int:
    """Which numbered source block a position belongs to; 0 means unlabelled."""
    position = bisect_right(markers, (index, sys.maxsize))
    return markers[position - 1][1] if position else 0


# Every decisive form contains one of these literals, so a sentence without them
# skips the large alternation entirely (the detector reads casefolded text).
_DECISIVE_HINTS = (
    "won",
    "winner",
    "beat",
    "superior",
    "better",
    "best",
    "fastest",
    "slowest",
    "outperform",
    "ahead",
    "favor",
    "favour",
    "resolved",
    "first",
    "higher",
    "score",
    "result",
    "побед",
    "обогнал",
    "выиграл",
    "признан",
    "лучше",
    "результат",
    "место",
)


def _usable_wins_in_sentence(
    text: str,
    spans: list[tuple[int, int]],
    sentence_index: int,
    markers: list[tuple[int, int]],
) -> list[tuple[re.Match[str], int]]:
    """Decisive win mentions in one sentence, each with its numbered source.

    ``text`` is the case-folded evidence, which the cheap literal pre-check relies on.
    """
    sentence_start, sentence_end = spans[sentence_index]
    sentence = text[sentence_start:sentence_end]
    if not any(hint in sentence for hint in _DECISIVE_HINTS):
        return []
    usable: list[tuple[re.Match[str], int]] = []
    for match in _DECISIVE_WIN.finditer(sentence):
        local_start = match.start()
        clause = _clause_before(sentence, local_start)
        # "ranked first", "finished first" and "put A ahead" also describe quizzes,
        # races and priority lists; only a comparison context makes them a result.
        if _AMBIGUOUS_WIN.search(match.group()) and not _COMPARISON_CONTEXT.search(sentence):
            continue
        # A negation can sit inside the matched comparison ("found that no method
        # was better"), so the check covers the match itself, not just its prefix.
        if _negated_at(sentence, local_start):
            continue
        if _NEGATED_VERDICT.search(f"{clause} {match.group()} "):
            continue
        if _HYPOTHETICAL_WIN.search(clause) or _HEDGED_WIN.search(clause):
            continue
        if _SEPARATE_COMPARISON.search(sentence):
            continue
        # A win that only holds for a named sub-scope is a partial result, not a
        # resolution of the whole comparison, unless the sentence speaks overall.
        # The scope may follow the win ("won on accuracy") or precede it.
        scope = f"{clause} {sentence[match.end() : match.end() + 80]}"
        if _PARTIAL_WIN_SCOPE.search(scope) and not _OVERALL_MARKER.search(sentence):
            continue
        # Tie the win to the comparison it claims to settle. A later result must be
        # framed as final/overall/larger/follow-up, or be an explicit winner
        # declaration; otherwise a quiz, a priority list, or a scoped observation
        # would silently cancel the source's open verdict.
        win_scope = f"{clause} {match.group()} {sentence[match.end() : match.end() + 80]}"
        if not (_UNAMBIGUOUS_WIN.search(win_scope) or _LATER_RESULT_FRAME.search(win_scope)):
            continue
        # A retraction after this win cancels it; a retraction before it targets an
        # earlier claim and does not.
        tail = sentence[match.end() :]
        if sentence_index + 1 < len(spans):
            next_start, next_end = spans[sentence_index + 1]
            tail += " " + text[next_start:next_end]
        if _RETRACTED_CLAIM.search(tail):
            continue
        usable.append((match, _source_at(markers, sentence_start + local_start)))
    return usable


def _is_split_win(sentence: str, wins: list[re.Match[str]]) -> bool:
    """Whether one sentence reports wins in different scopes ("A won X, but B won Y").

    Coordinated wins with no overall framing describe a split result, not a
    decisive outcome; they must not cancel an explicit no-winner verdict.
    """
    if len(wins) < 2 or _OVERALL_MARKER.search(sentence):
        return False
    between = sentence[wins[0].start() : wins[-1].end()]
    return _CONTRAST_COORDINATOR.search(between) is not None


def _decisive_sentence_indexes(
    text: str,
    spans: list[tuple[int, int]],
    wins_by_sentence: list[list[tuple[re.Match[str], int]]],
) -> dict[int, list[int]]:
    """Sentence indexes per source that state a non-split decisive win.

    Precomputing this keeps a document with many verdict mentions linear instead of
    rescanning every later sentence for each one.
    """
    indexes: dict[int, list[int]] = {}
    for index, wins in enumerate(wins_by_sentence):
        by_source: dict[int, list[re.Match[str]]] = {}
        for match, source in wins:
            by_source.setdefault(source, []).append(match)
        sentence = text[spans[index][0] : spans[index][1]]
        for source, matches in by_source.items():
            if _is_split_win(sentence, matches):
                continue
            indexes.setdefault(source, []).append(index)
    return indexes


def _has_later_decisive_win(
    text: str,
    spans: list[tuple[int, int]],
    wins_by_sentence: list[list[tuple[re.Match[str], int]]],
    decisive_indexes: dict[int, list[int]],
    index: int,
    floor: int,
    mention_source: int,
) -> bool:
    """Whether a sentence at or after ``index`` states a decisive comparison result.

    ``floor`` is the end of the verdict mention: a win stated before the verdict in
    the same sentence ("although A won the small sample, the comparison was
    inconclusive") does not supersede the verdict that follows it.
    """
    sentence_start, sentence_end = spans[index]
    own = [
        match
        for match, source in wins_by_sentence[index]
        if source == mention_source and sentence_start + match.start() >= floor
    ]
    if own and not _is_split_win(text[sentence_start:sentence_end], own):
        return True
    later = decisive_indexes.get(mention_source)
    if not later:
        return False
    return bisect_right(later, index) < len(later)


_NULL_DIFFERENCE = re.compile(
    r"(?i)^no\s+(?:meaningful|significant|clear|measurable|statistically\s+significant)?"
    r"\s*difference\b"
)
# "Mixed results are common in A/B testing" describes a general phenomenon, not
# this source's verdict. The predicate must follow the phrase so "the results were
# mixed because the samples differed" stays a result.
_GENERIC_PHENOMENON = re.compile(
    r"(?i)\b(?:mixed|inconclusive|contradictory|conflicting)\s+"
    r"(?:results?|findings?)\s+(?:are|were|is|was)\s+"
    r"(?:common|typical|expected|normal|usual|frequent|likely|rare|widespread|"
    r"unsurprising)\b"
)
# Generic Russian/Ukrainian outcome nouns describe uncertainty about anything
# ("результат лечения", "результат анализа крови") unless the sentence names the
# comparison or experiment that produced them.
_CYRILLIC_GENERIC_RESULT = re.compile(
    r"(?i)\b(?:результат\w*|вывод\w*|итог\w*|исход\w*|результат\w*|"
    r"виснов\w*|підсум\w*)\b"
)
_CYRILLIC_COMPARISON_CONTEXT = re.compile(
    r"(?i)\b(?:сравнени\w*|сопоставлен\w*|эксперимент\w*|исследован\w*|"
    r"испытан\w*|тест\w*|замер\w*|метод\w*|подход\w*|вариант\w*|модел\w*|"
    r"алгоритм\w*|конфигурац\w*|систем\w*|инструмент\w*|"
    r"порівнян\w*|експеримент\w*|досліджен\w*|випробуван\w*|тест\w*|"
    r"замір\w*|метод\w*|підхід\w*|варіант\w*|модел\w*|алгоритм\w*|"
    r"A/B|а/б)\b"
)


def _unrelated_null_difference(
    text: str, spans: list[tuple[int, int]], sentence_index: int, match: re.Match[str]
) -> bool:
    """Whether "no difference" describes an unrelated attribute, not a comparison.

    A measured attribute alone ("no difference in time zones between servers") is
    not a comparison of approaches. The sentence must name a comparison, experiment,
    method, or alternative for the null result to be a verdict.
    """
    if not _NULL_DIFFERENCE.search(match.group()):
        return False
    sentence_start, sentence_end = spans[sentence_index]
    sentence = text[sentence_start:sentence_end]
    return (
        _COMPARISON_CONTEXT.search(sentence) is None
        and _CYRILLIC_COMPARISON_CONTEXT.search(sentence) is None
    )


def _unrelated_generic_result(
    text: str, spans: list[tuple[int, int]], starts: list[int], match: re.Match[str]
) -> bool:
    """Whether a Russian/Ukrainian generic outcome noun lacks a comparison subject.

    "Результат лечения был неопределённым" is uncertainty about a treatment, not a
    no-winner verdict. "Результат сравнения был неопределённым" names the comparison
    and keeps the verdict.
    """
    if not _CYRILLIC_GENERIC_RESULT.search(match.group()):
        return False
    sentence_index = _sentence_at(spans, starts, match.start())
    if sentence_index is None:
        return False
    sentence_start, sentence_end = spans[sentence_index]
    return _CYRILLIC_COMPARISON_CONTEXT.search(text[sentence_start:sentence_end]) is None


def _background_verdict(
    text: str,
    spans: list[tuple[int, int]],
    starts: list[int],
    match: re.Match[str],
) -> bool:
    """Whether a verdict phrase only describes prior work or a general phenomenon."""
    sentence_index = _sentence_at(spans, starts, match.start())
    if sentence_index is None:
        return _PRIOR_WORK.search(_clause_before(text, match.start())) is not None
    sentence_start, sentence_end = spans[sentence_index]
    # A generic statement ("mixed results are common in A/B testing") describes a
    # phenomenon, not this source's verdict. Only the first clause after the match
    # is inspected, so a separate specific verdict joined by a semicolon stays.
    clause_tail = re.split(r"[.;:,!?]", text[match.end() : sentence_end], maxsplit=1)[0]
    if _GENERIC_PHENOMENON.search(f"{match.group()}{clause_tail}"):
        return True
    # The meta phrase may sit in an earlier clause of the same sentence
    # ("объясняется, почему …"), but it must introduce this exact clause.
    prefix = text[sentence_start : match.start()]
    if _PRIOR_WORK.search(_clause_before(text, match.start())):
        return True
    # A meta verb may introduce this clause across a comma ("объясняется,
    # почему …"), but it must not reach across a contrast or a semicolon.
    meta_tail = prefix.rsplit(";", 1)[-1]
    meta_scope = _CONTRAST_COORDINATOR.split(meta_tail)[-1]
    if _META_WHY_BEFORE.search(meta_scope):
        return True
    # "explains how X works and why mixed results are common" keeps both embedded
    # questions under the explanatory verb; the second question must be the last
    # clause, so a following main claim is not swallowed by the same verb.
    strong_scope = _META_STRONG_CONTRAST.split(meta_tail)[-1]
    if _META_VERB.search(strong_scope) and _META_COORDINATED.search(strong_scope):
        return True
    return _PRIOR_WORK_AFTER.search(text[match.end() : sentence_end]) is not None


def states_inconclusive_comparison(text: str) -> bool:
    """Whether the captured evidence leaves an explicit inconclusive verdict.

    A negated mention ("the results were not inconclusive") cancels only itself.
    A later decisive win supersedes the verdict only when it belongs to the same
    comparison and source: a hypothetical ("whether A outperformed B"), a hedged
    possibility, a retracted win, a split win in another scope, or a win in a
    separate experiment must not turn an open comparison into a settled one. A
    verdict that only reports prior work or explains a phenomenon is background,
    not this source's conclusion. An explicitly retracted inconclusive conclusion
    is also not a verdict the source still stands behind.
    """
    folded = _fold(text)
    spans = _sentence_spans(folded)
    starts = [start for start, _ in spans]
    markers = _source_markers(folded)
    wins_by_sentence: list[list[tuple[re.Match[str], int]]] | None = None
    decisive_indexes: dict[int, list[int]] = {}
    for match in _INCONCLUSIVE_OUTCOME.finditer(folded):
        if _negated_at(folded, match.start()):
            continue
        # The negation may be part of the matched phrase ("were not inconclusive").
        if _NEGATED_INCONCLUSIVE.search(_clause_before(folded, match.start()) + match.group()):
            continue
        if _background_verdict(folded, spans, starts, match):
            # "Prior studies reported mixed results" or "the article explains why
            # mixed results are common" describes the literature or the topic; it
            # is not the current source's verdict.
            continue
        if _RETRACTED_VERDICT.search(folded[match.end() :]):
            continue
        sentence_index = _sentence_at(spans, starts, match.start())
        if sentence_index is None:
            return True
        if _unrelated_null_difference(folded, spans, sentence_index, match):
            continue
        if _unrelated_generic_result(folded, spans, starts, match):
            continue
        if wins_by_sentence is None:
            wins_by_sentence = [
                _usable_wins_in_sentence(folded, spans, index, markers)
                for index in range(len(spans))
            ]
            decisive_indexes = _decisive_sentence_indexes(folded, spans, wins_by_sentence)
        if _has_later_decisive_win(
            folded,
            spans,
            wins_by_sentence,
            decisive_indexes,
            sentence_index,
            match.end(),
            _source_at(markers, match.start()),
        ):
            continue
        return True
    return False


def has_multiple_sources(metadata: Mapping[str, object]) -> bool:
    """Whether more than one source actually contributed evidence.

    ``source_count`` includes sources whose extraction failed. A single available
    source must still receive the no-winner hint and repair even when the Item was
    composed from several requested sources.
    """
    successful = metadata.get("successful_source_count")
    if type(successful) is int:
        return successful > 1
    count = metadata.get("source_count")
    if type(count) is not int:
        return False
    failures = metadata.get("source_failures")
    if isinstance(failures, list):
        return count - len(failures) > 1
    return count > 1


def title_states_inconclusive(title: str) -> bool:
    """Whether a title already carries the verdict; the suffix is then a no-op."""
    if states_inconclusive_comparison(title):
        return True
    folded = _fold(title)
    return any(
        _fold(suffix.removeprefix("; ")) in folded for suffix in _INCONCLUSIVE_SUFFIX.values()
    )


def _language_hint(text: str) -> str | None:
    """Recognize the languages the repair can write without importing a detector."""
    if _UKRAINIAN.search(text):
        return "uk"
    if _CYRILLIC.search(text):
        return "ru"
    # Two markers avoid treating an English noun such as "die casting" as German.
    if len(_GERMAN.findall(text)) >= 2:
        return "de"
    if len(_SPANISH.findall(text)) >= 2:
        return "es"
    if len(_FRENCH.findall(text)) >= 2:
        return "fr"
    if _ENGLISH.search(text):
        return "en"
    return None


def inconclusive_title_suffix(
    title: str, response_language: str | None = None, evidence: str = ""
) -> str:
    """Pick the suffix for the actual response language.

    The title script and evidence wording are only a fallback for callers without
    profile context; the title wins so a Russian answer keeps a Russian suffix even
    beside English evidence. When no response language or recognizable translation
    is available, return an empty suffix rather than inserting English into another
    language's title.
    """
    language = (response_language or "").split("-")[0].casefold()
    if language in _INCONCLUSIVE_SUFFIX:
        return _INCONCLUSIVE_SUFFIX[language]
    if language:
        return ""
    hint = _language_hint(title)
    if hint == "ru" and _UKRAINIAN.search(evidence):
        # A Ukrainian title can look Russian until its text provides a marker;
        # the evidence disambiguates the shared Cyrillic script.
        hint = "uk"
    if hint is None:
        hint = _language_hint(evidence)
    return _INCONCLUSIVE_SUFFIX.get(hint, "")
