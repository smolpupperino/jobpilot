"""Job URL helpers: canonicalize for dedupe, detect which ATS hosts the posting."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that are tracking noise, not part of the job's identity.
_TRACKING_PREFIXES = ("utm_", "gh_src", "src", "source", "ref", "trk", "refid", "trackingid",
                      "__jv", "tm_", "bid")  # __jv*/tm_*: Jobvite/Talemetry source tracking

# Query params that DO identify the job on some sites and must be kept.
_KEEP_PARAMS = {"gh_jid", "jobid", "job_id", "jk", "currentjobid", "id", "req", "requisitionid"}

_ATS_HOST_PATTERNS: list[tuple[str, str]] = [
    ("myworkdayjobs.com", "workday"),
    ("myworkday.com", "workday"),
    ("greenhouse.io", "greenhouse"),
    ("lever.co", "lever"),
    ("ashbyhq.com", "ashby"),
    ("icims.com", "icims"),
    ("taleo.net", "taleo"),
    ("successfactors.com", "successfactors"),
    ("successfactors.eu", "successfactors"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("jobvite.com", "jobvite"),
    ("ttcportals.com", "jobvite"),  # Talemetry career portals, now part of Jobvite
    ("ultipro.com", "ukg"),
    ("ukg.net", "ukg"),
    ("paylocity.com", "paylocity"),
    ("adp.com", "adp"),
    ("oraclecloud.com", "oracle"),
    ("bamboohr.com", "bamboohr"),
    ("linkedin.com", "linkedin"),
    ("indeed.com", "indeed"),
]


class InvalidJobURL(ValueError):
    pass


def canonicalize_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidJobURL("URL must start with http:// or https://")
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    path = parts.path.rstrip("/") or "/"
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() in _KEEP_PARAMS
        or not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ]
    kept.sort()
    return urlunsplit(("https", host + port, path, urlencode(kept), ""))


def detect_ats(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for pattern, name in _ATS_HOST_PATTERNS:
        if host == pattern or host.endswith("." + pattern):
            return name
    return "unknown"
