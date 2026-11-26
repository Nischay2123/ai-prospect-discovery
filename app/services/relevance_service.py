"""Brief §7 step 9: calculate each candidate's relevance to the query.

1. Deterministic pre-filter (free): candidates whose text mentions neither the interest nor the location are not
   sent to the LLM.
2. One batched LLM call for the rest: per candidate, interest/location/audience verdicts, with SEPARATE exact quotes
   for the interest and for the location.
3. Output guardrails: a quote that isn't really in the candidate's text, or that is only the candidate's own name,
   turns the "yes" it supports into "unknown". The interest quote supports interest only; the location quote
   supports location only (one phrase can't justify everything).

The prospect's displayed location is the verified location quote from its own text, never the query's location.
If the LLM is unavailable, a keyword-only verdict is used and every match is "partial" at most.
"""

import re
from dataclasses import dataclass
from typing import Literal

import logfire

from app.errors import InvalidLLMOutput, LLMUnavailable
from app.schemas import CandidateProfile, QueryIntent, RelevanceResult, Verdict
from app.services.llm_service import LLMGateway, candidates_block, load_prompt
from app.utils.normalizers import fold
from app.utils.validators import quote_in_text

MAX_LLM_CANDIDATES = 40
Match = Literal["confirmed", "partial"]

_LOCATION_ALIASES = {
    "usa": ["united states", "u.s.a", "america"], "uk": ["united kingdom", "england", "britain"],
    "uae": ["united arab emirates", "emirates"], "washington dc": ["washington, dc", "washington d.c.", "dc",
                                                                   "district of columbia"],
    "new york": ["nyc", "new york city", "manhattan", "brooklyn"],
}


@dataclass(frozen=True)
class Assessment:
    verdict: Verdict
    match: Match | None          # None = not relevant enough to return on its own
    by_llm: bool
    location_text: str | None = None   # verified quote of where the profile says it is


def location_terms(intent: QueryIntent) -> list[str]:
    terms = [t for t in (intent.location.city, intent.location.state, intent.location.country) if t]
    for term in list(terms):
        terms += _LOCATION_ALIASES.get(fold(term), [])
    return [fold(t) for t in terms]


def interest_terms(intent: QueryIntent) -> list[str]:
    return [fold(t) for t in [intent.interest, *intent.keywords] if t]


_POSSESSIVE = re.compile(r"(\w)['’]s\b")


def _words(text: str) -> str:
    """Folded words, punctuation ignored. Possessives are folded too ("Dubai's" -> "dubai"), so a stray "s" can
    never make a quote look like more than the name."""
    text = _POSSESSIVE.sub(r"\1", fold(text))
    return " " + " ".join(re.sub(r"[^\w]+", " ", text).split()) + " "


def _found(terms: list[str], text: str) -> str | None:
    """First term present as a whole word/phrase (punctuation ignored); returned so it can serve as evidence."""
    haystack = _words(text)
    for term in terms:
        needle = _words(term)
        if len(needle.strip()) >= 2 and needle in haystack:
            return term
    return None


_FILLER = {"the", "and", "of", "in", "at", "for", "by", "a", "an", "near", "official", "page", "best", "top",
           "agency", "agencies", "company", "services", "group", "club", "team", "fans", "fan", "lovers", "lover",
           "community", "hub", "network", "media", "news", "online", "local", "city", "owner", "owners", "founder",
           "founders", "people", "enthusiasts", "life", "world", "live", "tv", "channel"}


def generic_name(name: str | None, intent: QueryIntent) -> bool:
    """A "name" made only of the query's own words, filler and emoji ("NYC", "Digital Marketing Agency",
    "🏏 Cricket Ahmedabad 🏏"), or an SEO phrase like "Restaurant in Dubai Mall", is a page title, not a name."""
    words = re.findall(r"[a-z0-9]+", fold(name or ""))
    if not words:
        return True
    vocab = set(_FILLER)
    for term in [intent.interest, intent.audience, *intent.keywords, *location_terms(intent)]:
        vocab |= set(re.findall(r"[a-z0-9]+", fold(term)))
        vocab |= {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", fold(term))}
    def all_generic(ws: list[str]) -> bool:
        return all(w in vocab or w.rstrip("s") in vocab for w in ws)

    if all_generic(words):
        return True
    # "Restaurant in Dubai Mall" is an SEO phrase; "Old Tbilisi Georgian Restaurant in Dubai" is a real name.
    joined = " ".join(words)
    for place in (t for t in location_terms(intent) if len(t) >= 3):
        match = re.search(rf"\b(?:in|near)\s+{re.escape(place)}\b", joined)
        if match and all_generic(joined[:match.start()].split()):
            return True
    return False


def prefilter_score(candidate: CandidateProfile, intent: QueryIntent) -> int:
    return int(_found(interest_terms(intent), candidate.text) is not None) + \
        int(_found(location_terms(intent), candidate.text) is not None)


def match_of(v: Verdict) -> Match | None:
    answers = (v.interest, v.location, v.audience)
    if "no" in answers:
        return None
    if v.interest == "yes" and v.location == "yes":
        return "confirmed"
    # Interest is the core of the query: evidence of the place alone ("lives in DC") is not a prospect.
    if v.interest == "yes":
        return "partial"
    return None


def _unknown(cid: str) -> Verdict:
    return Verdict(candidate_id=cid, interest="unknown", location="unknown", audience="unknown", evidence="",
                   location_text="")


def supported(quote: str, candidate: CandidateProfile) -> bool:
    """The quote is really in the text AND says more than the candidate's own name ("Red Devils DC" quoting the
    account called Red Devils DC proves nothing)."""
    if not quote_in_text(quote, candidate.text):
        return False
    name_words = set(_words(candidate.name or "").split()) | set(_words(candidate.handle or "").split())
    return not set(_words(quote).split()) <= name_words


def target_place_terms(intent: QueryIntent) -> list[str]:
    """Terms that name the target place itself: the city and its aliases when a city was asked for (a country or
    state alone is too broad then), otherwise the state/country."""
    loc = intent.location
    if loc.city:
        terms = [loc.city, *_LOCATION_ALIASES.get(fold(loc.city), [])]
        # A city-state or emirate (Dubai, Singapore) shares its name with the region.
        if loc.state and fold(loc.state) == fold(loc.city):
            terms.append(loc.state)
    else:
        terms = [t for t in (loc.state, loc.country) if t]
        for t in list(terms):
            terms += _LOCATION_ALIASES.get(fold(t), [])
    return [fold(t) for t in terms]


def names_target_place(quote: str, intent: QueryIntent) -> bool:
    """The quote names the target place as a location, not just as a possessive ("the best of Dubai's new
    restaurants" talks about Dubai; it doesn't say the person is there)."""
    term = _found(target_place_terms(intent), quote)
    if term is None:
        return False
    pattern = r"\W+".join(map(re.escape, term.split()))
    uses = re.findall(rf"\b{pattern}\b(['’]s\b)?", fold(quote))
    return any(not possessive for possessive in uses)


# Words that mark an organisation, team, venue or media page rather than a person.
_ORGANISATION = re.compile(
    r"\b(?:group|association|club|academy|team|tournament|league|federation|council|society|foundation|"
    r"community|news|media|updates?|official|stadium|arena|radio|magazine|network|fc|cc|inc|llc|ltd)\b",
    re.IGNORECASE)


def looks_like_organisation(name: str | None) -> bool:
    return bool(name and _ORGANISATION.search(name))


def place_quote(quote: str, intent: QueryIntent) -> str:
    """Show the place, not the sentence: "I'm in London" -> "London", "heart of London" -> "London";
    "London, England, United Kingdom" stays as is."""
    quote = quote.strip()
    term = _found(target_place_terms(intent), quote)
    if term:
        original = _original(term, quote)
        start = quote.lower().find(original.lower())
        if start > 0:
            return quote[start:].strip(" ,.;:-")
    return quote


def place_like(quote: str) -> bool:
    """A location quote must read like a place: has a real word, isn't a @handle or a bare postcode."""
    return "@" not in quote and bool(re.search(r"[^\W\d_]{3,}", quote))


def _original(term: str, text: str) -> str:
    """The term as it is written in the text (for showing a keyword match as a quote)."""
    match = re.search(r"\b" + r"\W+".join(map(re.escape, term.split())) + r"\b", text, re.IGNORECASE)
    return match.group() if match else term


class RelevanceService:
    def __init__(self, llm: LLMGateway) -> None:
        self._llm = llm
        self._prompt = load_prompt("relevance")

    async def assess(self, candidates: list[CandidateProfile], intent: QueryIntent,
                     budget: float) -> dict[str, Assessment]:
        with logfire.span("relevance") as span:
            scored = sorted(((prefilter_score(c, intent), c) for c in candidates), key=lambda x: -x[0])
            to_llm = [c for score, c in scored if score > 0][:MAX_LLM_CANDIDATES]
            result = {c.id: Assessment(_unknown(c.id), None, by_llm=False) for c in candidates}
            span.set_attributes({"candidates": len(candidates), "prefiltered_out": len(candidates) - len(to_llm),
                                 "sent_to_llm": len(to_llm)})
            if not to_llm:
                return result

            verdicts, by_llm = await self._llm_verdicts(to_llm, intent, budget)
            downgraded = 0
            for candidate in to_llm:
                verdict = verdicts.get(candidate.id, _unknown(candidate.id))
                update: dict[str, str] = {}
                # Each "yes" needs its own quote that really is in the text. A quote may be the profile's name
                # ("Rags To Riches Restaurant" shows the business type) only if the OTHER answer is backed by text
                # beyond the name, so no match ever rests on a name alone (brief §4 Section 5).
                interest_strict = supported(verdict.evidence, candidate)
                # The quote must name the target place itself (or a known alias): "Cazenovia, New York" for New York
                # City or "Valley Stream, NY" is not the city. Checked by code, not left to the model.
                in_target = names_target_place(verdict.location_text, intent)
                location_strict = supported(verdict.location_text, candidate) and place_like(verdict.location_text) \
                    and in_target
                interest_ok = interest_strict or (quote_in_text(verdict.evidence, candidate.text) and location_strict)
                location_ok = location_strict or (quote_in_text(verdict.location_text, candidate.text)
                                                  and place_like(verdict.location_text) and in_target
                                                  and interest_strict)
                if verdict.interest == "yes" and not interest_ok:
                    update["interest"] = "unknown"
                if verdict.location == "yes" and not location_ok:
                    update["location"] = "unknown"
                if update:
                    downgraded += 1
                    verdict = verdict.model_copy(update=update)
                # The audience asked for people: organisations, teams and media pages are not prospects, whatever
                # the model said (it let "Ahmedabad Cricket Morning Group" through once).
                if intent.target_type == "individual" and looks_like_organisation(candidate.name) \
                        and verdict.audience != "no":
                    verdict = verdict.model_copy(update={"audience": "no"})
                match = match_of(verdict)
                if not by_llm and match == "confirmed":
                    match = "partial"
                where = place_quote(verdict.location_text, intent) if verdict.location == "yes" else None
                result[candidate.id] = Assessment(verdict, match, by_llm, where or None)
            span.set_attributes({"guardrail.downgraded_verdicts": downgraded, "by_llm": by_llm,
                                 "confirmed": sum(a.match == "confirmed" for a in result.values()),
                                 "partial": sum(a.match == "partial" for a in result.values())})
        return result

    async def _llm_verdicts(self, candidates: list[CandidateProfile], intent: QueryIntent,
                            budget: float) -> tuple[dict[str, Verdict], bool]:
        loc = intent.location
        location = ", ".join(dict.fromkeys(p for p in (loc.city, loc.state, loc.country) if p)) or "any"
        kind = "businesses (or their owners)" if intent.target_type == "business" else "individual people"
        user = (f'Target: interest "{intent.interest}", audience "{intent.audience}" ({kind}), '
                f'location "{location}"\n' + candidates_block([(c.id, c.text) for c in candidates]))
        try:
            llm = await self._llm.structured(self._prompt, user, RelevanceResult, budget)
        except (LLMUnavailable, InvalidLLMOutput) as exc:
            logfire.warn("relevance model unavailable, using keyword verdicts: {error}", error=str(exc))
            return {c.id: self._keyword_verdict(c, intent) for c in candidates}, False
        sent = {c.id for c in candidates}
        # Guardrail: only ids we sent; a verdict for anything else is ignored.
        return {v.candidate_id: v for v in llm.value.verdicts if v.candidate_id in sent}, True

    @staticmethod
    def _keyword_verdict(candidate: CandidateProfile, intent: QueryIntent) -> Verdict:
        interest = _found(interest_terms(intent), candidate.text)
        location = _found(location_terms(intent), candidate.text)
        return Verdict(candidate_id=candidate.id, interest="yes" if interest else "unknown",
                       location="yes" if location else "unknown", audience="unknown",
                       evidence=_original(interest, candidate.text) if interest else "",
                       location_text=_original(location, candidate.text) if location else "")
