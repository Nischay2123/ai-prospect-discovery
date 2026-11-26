"""URL normalisation, profile-URL recognition and title parsing. Pure functions, no I/O."""

import ipaddress
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit

from app.schemas import PlatformName

# ------------------------------------------------------------------ URLs ----

_HOST_ALIASES = {
    "twitter.com": "x.com",
    "threads.com": "threads.net",
    "youtu.be": "youtube.com",
    "fb.com": "facebook.com",
}
_STRIP_PREFIXES = ("www.", "m.", "mobile.", "web.", "business.")
_CASE_INSENSITIVE_HOSTS = {"x.com", "instagram.com", "threads.net", "facebook.com", "linkedin.com"}


def normalize_host(host: str) -> str:
    host = host.lower().rstrip(".")
    for prefix in _STRIP_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix):]
    if host.endswith(".linkedin.com"):  # uk.linkedin.com, in.linkedin.com → linkedin.com
        host = "linkedin.com"
    return _HOST_ALIASES.get(host, host)


def normalize_url(url: str) -> str | None:
    """Canonical https URL without tracking params, fragment or trailing slash; None if not an http(s) URL.

    Keeps `?id=` for facebook.com/profile.php, the only profile form that lives in the query string.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = normalize_host(parts.hostname)
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    # Handles are case-insensitive on these platforms (x.com/GCAMotera == x.com/gcamotera). YouTube channel IDs
    # (/channel/UC...) are case-sensitive, so only its @handles are lowercased.
    if host in _CASE_INSENSITIVE_HOSTS or (host == "youtube.com" and path.startswith("/@")):
        path = path.lower()
    query = ""
    if host == "facebook.com" and path == "/profile.php":
        profile_id = parse_qs(parts.query).get("id", [""])[0]
        query = f"?id={profile_id}" if profile_id.isdigit() else ""
    return f"https://{host}{path}{query}"


# ------------------------------------------------------- profile URLs ----

_RESERVED = {
    "X": {"home", "explore", "search", "i", "hashtag", "intent", "share", "login", "signup", "settings", "messages",
          "notifications", "tos", "privacy", "compose", "about"},
    "Instagram": {"p", "reel", "reels", "explore", "stories", "accounts", "about", "developer", "direct", "tv",
                  "legal", "web", "popular"},
    "Facebook": {"groups", "events", "watch", "marketplace", "posts", "photo", "photos", "share", "sharer", "login",
                 "help", "hashtag", "pages", "public", "search", "reel", "story.php", "permalink.php", "video",
                 "videos", "gaming", "policies", "privacy", "business", "media"},
}
_HANDLE = {
    "X": re.compile(r"^[A-Za-z0-9_]{1,15}$"),
    "Instagram": re.compile(r"^[A-Za-z0-9._]{1,30}$"),
    "Facebook": re.compile(r"^[A-Za-z0-9.\-]{3,50}$"),
}
PLATFORM_HOSTS: dict[str, PlatformName] = {
    "linkedin.com": "LinkedIn",
    "x.com": "X",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "threads.net": "Threads",
    "youtube.com": "YouTube",
}


def profile_of(url: str) -> tuple[PlatformName, str] | None:
    """(platform, handle) when the URL is a profile page on a known social platform, else None.

    Posts, reels, groups, search pages etc. are rejected: the brief wants profiles, not content URLs.
    """
    normalized = normalize_url(url)
    if normalized is None:
        return None
    parts = urlsplit(normalized)
    platform = PLATFORM_HOSTS.get(parts.hostname or "")
    segments = [s for s in parts.path.split("/") if s]
    if platform is None or not segments:
        return None

    if platform == "LinkedIn":
        # /in/<person> for people, /company/<slug> for businesses
        return (platform, segments[1]) if len(segments) == 2 and segments[0] in ("in", "company") else None
    if platform == "Threads":
        return (platform, segments[0][1:]) if len(segments) == 1 and segments[0].startswith("@") else None
    if platform == "YouTube":
        if len(segments) == 1 and segments[0].startswith("@"):
            return platform, segments[0][1:]
        if len(segments) == 2 and segments[0] in ("channel", "c", "user"):
            return platform, segments[1]
        return None
    if platform == "Facebook":
        if segments == ["profile.php"]:
            return (platform, parts.query.removeprefix("id=")) if parts.query.startswith("id=") else None
        if len(segments) == 3 and segments[0] == "people":  # facebook.com/people/Jane-Doe/1000123
            return platform, segments[2]
    # X, Instagram, Facebook vanity URLs: exactly one segment that looks like a handle
    handle = segments[0]
    if len(segments) == 1 and handle.lower() not in _RESERVED[platform] and _HANDLE[platform].match(handle):
        return platform, handle
    return None


def is_social_host(url: str) -> bool:
    normalized = normalize_url(url)
    return bool(normalized) and (urlsplit(normalized).hostname or "") in PLATFORM_HOSTS


# Big sites whose pages are never one prospect's own website (used for Google general hits).
_NOT_A_PROSPECT_SITE = (
    "google.", "wikipedia.org", "yelp.", "tripadvisor.", "reddit.com", "quora.com", "medium.com", "eventbrite.",
    "meetup.com", "indeed.", "glassdoor.", "crunchbase.com", "bloomberg.com", "forbes.com", "nytimes.com",
    "bbc.", "cnn.com", "amazon.", "apple.com", "tiktok.com", "pinterest.", "linktr.ee", "clutch.co",
    "upwork.com", "fiverr.com", "zomato.com", "timeout.com", "yellowpages", "justdial.com", "tracxn.com",
)


_TWO_LEVEL_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "com.au", "co.in", "co.ae", "com.sg", "co.nz", "com.br",
                       "co.za", "com.sa", "com.pk", "net.in", "org.in"}
_GENERIC_TOKENS = {"the", "and", "of", "in", "at", "for", "by", "official", "page", "home", "best", "top", "agency",
                   "agencies", "company", "co", "inc", "llc", "ltd", "group", "services", "service", "studio", "team",
                   "club", "fans", "fan", "lovers", "lover", "hub", "media", "news", "online", "digital", "marketing",
                   "restaurant", "restaurants", "cafe", "shop", "store", "academy", "community", "network", "global"}


def site_label(url: str) -> str | None:
    """The brand part of a website's domain: https://hunterdigital.com -> "hunterdigital",
    https://www.acme.co.uk -> "acme". Used as the website's "handle" for identity matching."""
    normalized = normalize_url(url)
    host = urlsplit(normalized).hostname if normalized else None
    if not host:
        return None
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _TWO_LEVEL_SUFFIXES:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else None


def name_tokens(name: str | None) -> set[str]:
    """Distinctive words of a name (folded, 3+ letters, generic business words removed)."""
    return {t for t in re.findall(r"[a-z0-9]+", fold(name or "")) if len(t) >= 3 and t not in _GENERIC_TOKENS}


def squash(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(text or ""))


def handle_belongs(handle: str | None, label: str | None, name: str | None) -> bool:
    """Does a social handle plausibly belong to this site/brand? e.g. @hunterdigitalnyc for hunterdigital.com, or
    @copifycom for "Copify Digital Marketing Agency". Used to keep a website's links to its OWN profiles only."""
    h = squash(handle)
    if len(h) < 4:
        return False
    lab = squash(label)
    if len(lab) >= 4 and (lab in h or h in lab):
        return True
    return any(len(t) >= 4 and t in h for t in name_tokens(name))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def is_prospect_website(url: str) -> bool:
    """A Google general hit counts as a prospect's own site only if it's a home/about/contact-style page
    on a domain that isn't a directory, news site, marketplace or social platform."""
    normalized = normalize_url(url)
    if not normalized or is_social_host(normalized):
        return False
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    if any(marker in host for marker in _NOT_A_PROSPECT_SITE) or _is_ip_literal(host) or "." not in host:
        return False
    segments = [s for s in parts.path.split("/") if s]
    return len(segments) == 0 or (len(segments) == 1 and segments[0].lower() in {"about", "about-us", "contact",
                                                                                   "contact-us", "team", "home"})


# --------------------------------------------------------------- names ----

_TITLE_SUFFIXES = re.compile(
    r"\s*(?:[|·•\-–—]\s*)?(?:LinkedIn|Facebook|Instagram(?: photos and videos)?|Threads(?:, Say more)?|X|Twitter|"
    r"YouTube)\s*$",
    re.IGNORECASE,
)
_HANDLE_IN_TITLE = re.compile(r"\s*\(@[^)]+\).*$")
_GENERIC_NAMES = {"log in", "login", "sign up", "linkedin", "facebook", "instagram", "threads", "x", "twitter",
                  "youtube", "page not found", "home", "profile", "error", "untitled", "welcome", "contact",
                  "contact us", "about", "about us", "our team", "team", "homepage", "home page"}
_PAGE_WORDS = re.compile(r"^(?:contact(?:\s+us)?|about(?:\s+us)?|welcome\s+to|home)\b[\s:,-]*", re.IGNORECASE)
_ROLE_HEADLINE = re.compile(r"^(?:owner|co-?owner|founder|co-?founder|ceo|cto|director|manager|head|president|"
                            r"partner|chef|employee|student|intern)\b.*\b(?:of|at|@)\b", re.IGNORECASE)
_PLACEHOLDER_WORDS = re.compile(r"\b(?:undefined|null|none)\b", re.IGNORECASE)


def name_from_title(platform: PlatformName, title: str) -> str | None:
    """The display name a platform puts in its page title, e.g.
    LinkedIn  "Jane Doe - Founder at Acme | LinkedIn"            -> "Jane Doe"
    X         "Jane Doe (@jdoe) / X"                             -> "Jane Doe"
    Instagram "Jane Doe (@jdoe) • Instagram photos and videos"   -> "Jane Doe"
    Facebook  "Jane Doe | Facebook"                              -> "Jane Doe"
    Google    "Acme Marketing | Digital Agency in New York"      -> "Acme Marketing"
    Never invents: returns None when the title doesn't hold a usable name.
    """
    # Drop invisible format characters (e.g. U+200F right-to-left mark) that platforms leave in titles.
    text = " ".join("".join(ch for ch in title if unicodedata.category(ch) != "Cf").split())
    if not text:
        return None
    text = _HANDLE_IN_TITLE.sub("", text)
    text = _TITLE_SUFFIXES.sub("", text)
    # Website page titles often lead with the page, not the business: "Contact us | Abo Ali Restaurant".
    segments = [s.strip() for s in re.split(r"\s+[|·•\-–—]\s+", text) if s.strip()]
    segments = [_PAGE_WORDS.sub("", s).strip() for s in segments]
    segments = [s for s in segments if s and s.lower() not in _GENERIC_NAMES]
    if not segments:
        return None
    text = segments[0]
    if text.endswith(("...", "…")):  # truncated by the search engine: not a reliable name
        return None
    text = _PLACEHOLDER_WORDS.sub("", text)  # Google sometimes renders a missing surname as "undefined"
    if _ROLE_HEADLINE.match(text):  # "Owner of Shamla Mandi Restaurant" is a headline, the person's name is missing
        return None
    text = " ".join(text.split()).strip(" .,:;|-–—'\"")
    if not (2 <= len(text) <= 80) or not re.search(r"[^\W\d_]", text) or text.lower() in _GENERIC_NAMES:
        return None
    return text


def fold(text: str) -> str:
    """Lowercase, accents removed, whitespace collapsed: for comparing names and finding quotes."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.lower().split())


def normalize_query(query: str) -> str:
    return fold(query).strip(" .?!")


# ------------------------------------------------------------- countries ----

# ponytail: small alias table for the countries users are likely to type; unknown countries still work,
# phone parsing then just needs numbers written with a +country code.
COUNTRY_CODES = {
    "usa": "US", "us": "US", "united states": "US", "united states of america": "US", "america": "US",
    "uk": "GB", "united kingdom": "GB", "england": "GB", "great britain": "GB", "britain": "GB", "scotland": "GB",
    "india": "IN", "uae": "AE", "united arab emirates": "AE", "canada": "CA", "australia": "AU",
    "germany": "DE", "france": "FR", "spain": "ES", "italy": "IT", "netherlands": "NL", "ireland": "IE",
    "singapore": "SG", "pakistan": "PK", "bangladesh": "BD", "sri lanka": "LK", "nepal": "NP",
    "saudi arabia": "SA", "qatar": "QA", "kuwait": "KW", "oman": "OM", "bahrain": "BH", "egypt": "EG",
    "nigeria": "NG", "kenya": "KE", "south africa": "ZA", "brazil": "BR", "mexico": "MX", "argentina": "AR",
    "japan": "JP", "china": "CN", "south korea": "KR", "indonesia": "ID", "malaysia": "MY", "philippines": "PH",
    "new zealand": "NZ", "sweden": "SE", "norway": "NO", "denmark": "DK", "switzerland": "CH", "portugal": "PT",
    "poland": "PL", "turkey": "TR", "israel": "IL", "vietnam": "VN", "thailand": "TH",
}


def country_code(country: str | None) -> str | None:
    if not country:
        return None
    key = fold(country).replace(".", "")
    if len(key) == 2 and key.upper() in COUNTRY_CODES.values():
        return key.upper()
    return COUNTRY_CODES.get(key)
