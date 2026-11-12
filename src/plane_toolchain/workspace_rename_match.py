"""Workspace slug validation and link matching for the rename snippet.

Pure and stdlib-only: the source is shipped verbatim inside a Django snippet run on the owner
host, so import nothing from plane_toolchain. Never print or log record content.
"""

import re
from urllib.parse import urlsplit

SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]+\Z")
MAX_SLUG = 48
# OLD must end at a path, quote, query or fragment boundary or at the end of the value, so a
# workspace whose slug only shares a prefix (`acme-two` for `acme`) never matches.
_END = r"""(?=[/"'?#]|\Z)"""


def validate_slug(new, restricted, taken):
    """Raise ValueError unless `new` is a usable workspace slug.

    `taken` holds every existing slug, soft-deleted `<slug>__<epoch>` forms included; each
    occupies only its own exact string.
    """
    if not isinstance(new, str) or not SLUG_RE.match(new):
        raise ValueError("slug must match [a-zA-Z0-9_-]+")
    if len(new) > MAX_SLUG:
        raise ValueError(f"slug longer than {MAX_SLUG} characters")
    if new in restricted:
        raise ValueError("slug is restricted")
    if new in taken:
        raise ValueError("slug is taken")


def hosts_from_url(url):
    """Return the lowercase host[:port] of a Plane `WEB_URL`, as a one-item list."""
    netloc = urlsplit(url).netloc.rpartition("@")[2].lower()
    return [netloc] if netloc else []


def _patterns(old, hosts):
    o = re.escape(old)
    pats = {
        "href": re.compile(rf"""(?<=href=")/{o}{_END}|(?<=href=')/{o}{_END}"""),
        "embed": re.compile(rf"""(?<=workspace_identifier="){o}(?=")"""),
    }
    names = [re.escape(h) for h in hosts if h]
    if names:
        pats["url"] = re.compile(rf"""(?i:https?://(?:{"|".join(names)}))/{o}{_END}""")
    return pats


def _sub(pats, text, old, new):
    total = 0
    if "url" in pats:
        text, n = pats["url"].subn(lambda x: x.group(0)[: -len(old)] + new, text)
        total += n
    text, n = pats["href"].subn("/" + new, text)
    total += n
    text, n = pats["embed"].subn(new, text)
    return text, total + n


def rewrite_html(text, old, new, hosts):
    """Return (text, count) with OLD links rewritten to NEW.

    Rewrites own-host absolute URLs, root-relative `href`s and the `workspace_identifier`
    embed attribute. Other hosts, bare words and other workspaces are left alone.
    """
    return _sub(_patterns(old, hosts), text, old, new)


def scan_text(text, old, hosts):
    """Count matches per form without rewriting: {"url", "href", "embed"}."""
    pats = _patterns(old, hosts)
    return {k: len(pats[k].findall(text)) if k in pats else 0 for k in ("url", "href", "embed")}


def rewrite_json_slug(obj, old, new):
    """Return (new object, count) with `workspace_slug` values equal to OLD set to NEW.

    Walks dicts and lists; the input is not mutated.
    """
    if isinstance(obj, dict):
        out, count = {}, 0
        for key, value in obj.items():
            if key == "workspace_slug" and value == old:
                out[key], count = new, count + 1
            else:
                out[key], n = rewrite_json_slug(value, old, new)
                count += n
        return out, count
    if isinstance(obj, list):
        out, count = [], 0
        for item in obj:
            value, n = rewrite_json_slug(item, old, new)
            out.append(value)
            count += n
        return out, count
    return obj, 0
