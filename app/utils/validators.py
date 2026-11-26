"""Find and validate publicly published contact details in text. Never guesses or builds a contact."""

import re

import phonenumbers
from email_validator import EmailNotValidError, validate_email

from app.schemas import Contact
from app.utils.normalizers import fold

_EMAIL = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![\w-])")
_FAKE_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "email.com", "test.com",
                       "yourdomain.com", "sentry.io", "wixpress.com", "sentry-next.wixpress.com"}
_NO_REPLY = re.compile(r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster)$", re.IGNORECASE)
_FILE_SUFFIX = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|js)$", re.IGNORECASE)

# Only links that are literally published. A phone number is never turned into a WhatsApp link.
_WHATSAPP = re.compile(
    r"(?:https?://)?(?:wa\.me/|api\.whatsapp\.com/send/?\?(?:[^\s\"'<>]*&)?phone=)\+?(\d{7,15})", re.IGNORECASE
)


def valid_email(candidate: str, check_deliverability: bool = False) -> str | None:
    """Normalised address, or None for bad syntax, placeholders, no-reply boxes and filenames like logo@2x.png."""
    if _FILE_SUFFIX.search(candidate):
        return None
    try:
        info = validate_email(candidate, check_deliverability=check_deliverability)
    except EmailNotValidError:
        return None
    local, domain = info.local_part, info.domain.lower()
    if domain in _FAKE_EMAIL_DOMAINS or _NO_REPLY.match(local):
        return None
    return info.normalized


def find_emails(text: str, source_url: str, check_deliverability: bool = False) -> list[Contact]:
    found = {}
    for match in _EMAIL.findall(text):
        email = valid_email(match, check_deliverability)
        if email:
            found.setdefault(email.lower(), Contact("Email", f"mailto:{email}", source_url))
    return list(found.values())


def find_phones(text: str, source_url: str, region: str | None) -> list[Contact]:
    """Numbers libphonenumber considers valid AND that belong to the searched country.

    Web pages are full of digit runs (IDs, prices, tracking codes) that happen to parse as valid numbers in some
    country; a Dubai restaurant's page once yielded Thai numbers. When the query names a country, only that
    country's numbers count. Without a region, only +country-code numbers can match at all.
    """
    found = {}
    try:
        matches = phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID)
        for match in matches:
            if region and phonenumbers.region_code_for_number(match.number) != region:
                continue
            e164 = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
            found.setdefault(e164, Contact("Phone", f"tel:{e164}", source_url))
    except Exception:  # noqa: S110 - a malformed number in third-party text must not break extraction
        pass
    return list(found.values())


def find_whatsapp(text: str, source_url: str) -> list[Contact]:
    found = {}
    for digits in _WHATSAPP.findall(text):
        try:
            number = phonenumbers.parse(f"+{digits}", None)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(number):
            found.setdefault(digits, Contact("WhatsApp", f"https://wa.me/{digits}", source_url))
    return list(found.values())


def find_contacts(text: str, source_url: str, region: str | None, check_deliverability: bool = False) -> list[Contact]:
    return (find_emails(text, source_url, check_deliverability) + find_phones(text, source_url, region)
            + find_whatsapp(text, source_url))


# ------------------------------------------------------------- guardrails ----
# Deterministic checks: they cost nothing and run before any API spend. The intent LLM is a second line
# (it returns is_valid=false for the same categories), but these never depend on the model behaving.

_URL_OR_EMAIL = re.compile(r"https?://|www\.|[\w.+-]+@[\w-]+\.\w")
_DIGIT_RUN = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_YEARS = re.compile(r"^(?:(?:19|20)\d{2}[\s-]*)+$")


def _has_phone_number(query: str) -> bool:
    """A real phone number has 9+ digits; "2020 - 2024" or "top 100" must not count."""
    for match in _DIGIT_RUN.finditer(query):
        text = match.group().strip()
        if _YEARS.match(text.replace("(", "").replace(")", "")):
            continue
        if len(re.sub(r"\D", "", text)) >= 9:
            return True
    return False


_TARGETED = [
    # "John Smith's phone", "Jane Doe's email address"
    re.compile(r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)+'s\s+(?:phone|number|mobile|email|e-mail|address|whatsapp|contact)",
               re.UNICODE),
    # "phone number of John Smith", "email of Jane Doe"
    re.compile(r"\b(?:phone|number|mobile|email|e-mail|address|whatsapp|contact)\s+(?:\w+\s+){0,2}(?:of|for)\s+"
               r"[A-Z][a-z]+\s[A-Z][a-z]+"),
]
# Special-category data (GDPR art. 9) and minors when they ARE the audience: people defined by a condition, a
# religion, orientation or politics. Professions and businesses that merely mention a word ("dentists treating
# patients", "Christian bookstore owners", "cancer researchers") are left to the intent LLM, which judges context.
_SENSITIVE = re.compile(
    r"\b(?:(?:patients|people|persons|sufferers|survivors)\s+(?:with|suffering|living\s+with|diagnosed)|"
    r"(?:cancer|hiv|aids|diabetes|depression|dementia|autism|adhd)\s+(?:patients|sufferers)|"
    r"diabetics|hiv[- ]positive|disabled\s+people|"
    r"muslims|christians|hindus|jews|sikhs|buddhists|atheists|"
    r"gays?\s+(?:men|people)|lesbians|lgbtq?\+?\s+(?:people|individuals)|transgender\s+(?:people|individuals)|"
    r"bisexuals|democrats|republicans|(?:party|political)\s+supporters|"
    r"minors|under[- ]?18s?|teenagers|teens|schoolchildren|school\s+kids)\b",
    re.IGNORECASE,
)
_INJECTION_HINTS = re.compile(r"ignore (?:all|previous|your)|system prompt|you are now|disregard", re.IGNORECASE)


def check_query(query: str) -> str | None:
    """Return the name of the blocking rule, or None when the query may proceed."""
    if _URL_OR_EMAIL.search(query) or _has_phone_number(query):
        return "contact_in_query"
    if any(p.search(query) for p in _TARGETED):
        return "targeted_individual"
    if _SENSITIVE.search(query):
        return "sensitive_category"
    return None


GUARDRAIL_MESSAGES = {
    "contact_in_query": "The query must describe an audience, not contain links, emails or phone numbers.",
    "targeted_individual": "This API finds audiences, not a specific person's contact details.",
    "sensitive_category": "Queries that target people by health, religion, sexual orientation, ethnicity, "
                          "political views or age (minors) are not supported.",
}


def looks_like_injection(text: str) -> bool:
    """Not blocked (could be legitimate text); logged so it shows up in traces."""
    return bool(_INJECTION_HINTS.search(text))


def quote_in_text(quote: str, text: str) -> bool:
    """Output guardrail: an LLM evidence quote counts only if it really appears in the source text."""
    def words(value: str) -> str:  # punctuation/emoji/case-insensitive, so "Washington, DC" matches "washington dc"
        value = re.sub(r"(\w)['’]s\b", r"\1", fold(value))  # possessives folded: "Dubai's" -> "dubai"
        return " " + " ".join(re.sub(r"[^\w]+", " ", value).split()) + " "

    q = words(quote)
    return len(q.strip()) >= 3 and q in words(text)
