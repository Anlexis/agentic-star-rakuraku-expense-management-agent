"""Input screening helpers for CMN-C2-280 (outer backbone pre_process).

Pure, stateless domain helpers (NOT framework gate methods). Three
responsibilities, in this order:

1. ``screen_for_injection`` — a fail-closed scan for prompt-injection carriers.
   It runs on the RAW text *and* again on the sanitized text, because the
   markup strip in ``sanitize_query`` changes what is detectable in both
   directions: it deletes chat-template control tokens outright (``<|im_start|>``
   looks like a tag) and it re-joins directives that were split by markup
   (``ig<b>nore`` becomes ``ignore``). Screening only one of the two forms
   silently converts a detectable attack into an undetectable one.
2. ``sanitize_query`` — strips markup and caps length.
3. ``validate_report_reference`` / ``validate_field_map`` — bind caller-supplied
   structured data to explicit, inert shapes before it can reach state or the
   rendered output.

The screens name the offending CLASS, never the offending value: an error that
echoes the rejected text would put caller-controlled content straight into the
logs the screen exists to protect. The class is a code from the declared
``REJECTION_CODES`` frozenset, and ``InputRejected`` carries it as a typed
attribute alongside an optional module-authored location, so a node can rebuild
the refusal notice from those two rather than interpolating the caught
exception - which is how text from outside a module reaches ``error_log``.
"""

from __future__ import annotations

import re
import unicodedata

_HTML_TAG_RE = re.compile(r"<[^>]+>")

DEFAULT_MAX_LENGTH = 4000

# Chat-template control tokens. These are a CLASS, not a list of phrases: an
# attack that carries no directive wording at all still steers a model when it
# can open a system turn. Matched before and after markup stripping.
_CONTROL_TOKEN_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("chat_control_token", re.compile(r"<\|[^|>]{0,64}\|>")),
    ("instruction_token", re.compile(r"\[/?INST\]", re.IGNORECASE)),
    ("system_token", re.compile(r"<</?SYS>>", re.IGNORECASE)),
    ("turn_token", re.compile(r"<\|?(?:im_start|im_end|endoftext|system)\|?>", re.IGNORECASE)),
)

# Directive phrases. Anchored to a verb + object pair so ordinary expense prose
# ("please disregard the duplicate line item") does not trip the screen; the
# screen must never block a real expense request.
_DIRECTIVE_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}?"
            r"\b(?:previous|prior|earlier|above|all|any)\b[^.\n]{0,20}?"
            r"\b(?:instruction|instructions|rule|rules|prompt|prompts|direction|directions)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_reassignment",
        re.compile(
            r"\byou\s+are\s+now\b[^.\n]{0,40}\b(?:unrestricted|jailbroken|developer\s+mode|dan)\b"
            r"|\bact\s+as\s+(?:an?\s+)?(?:unrestricted|jailbroken|different)\b"
            r"|\bpretend\s+(?:to\s+be|you\s+are)\b[^.\n]{0,30}\b(?:unrestricted|admin|root)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_probe",
        re.compile(
            r"\b(?:reveal|print|show|output|repeat|disclose)\b[^.\n]{0,30}?"
            r"\b(?:system\s+prompt|initial\s+instructions|your\s+instructions|hidden\s+rules)\b",
            re.IGNORECASE,
        ),
    ),
)

# The inert alphabet a Rakus report reference may render in. Deliberately
# narrow: this value reaches record_ref, the assembled request body and the
# confirmation line, so free text here would be caller-controlled output.
_REPORT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")

# Caller-supplied expense field names are rendered into the request body, so
# they are held to the same inert shape.
_FIELD_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")

MAX_FIELDS = 20
MAX_FIELD_VALUE_LENGTH = 200


# Every refusal class this module may raise, declared as a frozenset so the
# rejection notice is a closed set BY CONSTRUCTION rather than by accident.
# Every raise site below happens to pass a literal today; nothing but this
# check stops the next one passing a caller-supplied value into a notice that
# is written to error_log.
REJECTION_CODES = frozenset(
    {
        # screen_for_injection / screen_structure - the pattern-table labels
        "chat_control_token",
        "instruction_token",
        "system_token",
        "turn_token",
        "instruction_override",
        "role_reassignment",
        "system_prompt_probe",
        # structural screens
        "context_type",
        "structure_too_deep",
        # report reference
        "report_reference_type",
        "report_reference_format",
        # caller expense fields
        "fields_type",
        "fields_shape",
        "fields_count",
        "field_name_format",
        "field_value_type",
        "field_value_length",
        "non_finite_number",
    }
)


def rejection_notice(code: str, where: str = "") -> str:
    """The refusal notice for a screen failure: a closed-set code plus a location.

    The single place the notice text is built, so the node that logs it can
    rebuild it from an exception's typed attributes instead of interpolating
    the exception itself.
    """
    return f"input rejected: {code}" + (f" at {where}" if where else "")


class InputRejected(Exception):
    """Raised when caller-supplied data fails a screen. Carries a CLASS, not the value.

    Two attributes, BOTH chosen by this module: ``code`` is the offending
    class, constrained to :data:`REJECTION_CODES`, and ``where`` is an optional
    location label this module writes ("position 3", a contract field name).
    The rejected VALUE is never part of either.

    They exist so a node can rebuild the refusal notice from typed attributes
    rather than interpolating the caught exception. ``error_log`` is the
    internal audit channel the state reducer accumulates, and an interpolated
    exception is precisely how text from outside a module reaches it.
    """

    def __init__(self, code: str, where: str = "") -> None:
        if code not in REJECTION_CODES:
            raise ValueError(f"undeclared rejection code: {code!r}")
        self.code = code
        self.where = where
        super().__init__(self.reason)

    @property
    def reason(self) -> str:
        """The refusal notice, rebuilt from the closed-set code and the location."""
        return rejection_notice(self.code, self.where)


def _normalize(text: str) -> str:
    """Fold compatibility forms and strip zero-width characters before screening.

    Without this a full-width or zero-width-joined payload reads as ordinary
    text to the pattern set while still reaching a model intact.
    """
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


def _scan(text: str) -> str | None:
    """Return the offending class name, or None when the text is clean."""
    candidate = _normalize(text)
    for label, pattern in _CONTROL_TOKEN_RES:
        if pattern.search(candidate):
            return label
    for label, pattern in _DIRECTIVE_RES:
        if pattern.search(candidate):
            return label
    return None


def screen_for_injection(raw: str, sanitized: str | None = None) -> None:
    """Fail closed on prompt-injection carriers in caller text.

    Screens the raw text and, when supplied, the post-sanitize text as well.
    Both passes are required: the markup strip removes control tokens (hiding a
    token attack from a post-only screen) and re-assembles split directives
    (hiding a spliced attack from a raw-only screen).
    """
    for candidate in (raw, sanitized):
        if not candidate:
            continue
        found = _scan(candidate)
        if found:
            raise InputRejected(found)


def screen_structure(value: object, *, _depth: int = 0) -> None:
    """Depth-first screen of a parsed caller structure, KEYS included.

    Scanning the parsed object rather than the raw request body is what makes
    escape sequences irrelevant: by this point ``\\u0069gnore`` is already the
    character ``i``. Keys are screened because a hostile field NAME is echoed
    by any code that reports on unknown fields.
    """
    if _depth > 6:
        raise InputRejected("structure_too_deep")
    if isinstance(value, str):
        found = _scan(value)
        if found:
            raise InputRejected(found)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found = _scan(key)
                if found:
                    raise InputRejected(found)
            screen_structure(item, _depth=_depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            screen_structure(item, _depth=_depth + 1)


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip markup tags and cap length."""
    cleaned = _HTML_TAG_RE.sub("", query)
    return cleaned[:max_length]


def validate_report_reference(value: object) -> str:
    """Bind a caller-supplied report reference to the inert identifier alphabet.

    An absent reference is not an error - the request text may still carry the
    number, and an unresolved number is left empty rather than invented. A
    PRESENT but malformed reference IS an error: silently dropping it would let
    a request act on a different report than the caller named.
    """
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InputRejected("report_reference_type")
    text = str(value).strip()
    if not text:
        return ""
    if not _REPORT_REF_RE.match(text):
        raise InputRejected("report_reference_format")
    return text


def validate_field_map(value: object) -> "list[tuple[str, str]]":
    """Bind caller-supplied expense fields to bounded, inert (name, value) pairs.

    Names must match the inert identifier alphabet; an unrecognised name is
    reported by POSITION, never echoed, so a hostile field name cannot ride
    into a log line. Values are length-capped and screened as free text.
    """
    if value is None:
        return []
    if not isinstance(value, dict):
        raise InputRejected("fields_type")
    if len(value) > MAX_FIELDS:
        raise InputRejected("fields_count")
    pairs: list[tuple[str, str]] = []
    for position, (name, raw_value) in enumerate(value.items()):
        if not isinstance(name, str) or not _FIELD_NAME_RE.match(name):
            raise InputRejected("field_name_format", f"position {position}")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int, float)):
            raise InputRejected("field_value_type", f"position {position}")
        if isinstance(raw_value, float):
            check_finite(raw_value, f"position {position}")
        text = str(raw_value).strip()
        if len(text) > MAX_FIELD_VALUE_LENGTH:
            raise InputRejected("field_value_length", f"position {position}")
        screen_for_injection(text)
        pairs.append((name, text))
    return pairs


def check_finite(value: float, field_label: str) -> float:
    """Reject non-finite numerics, which compare False and so fail OPEN silently.

    ``float("nan")`` parses cleanly and arrives through raw JSON, and every
    comparison against it is False - so an unchecked non-finite value quietly
    suppresses whatever decision it feeds. Fail closed instead.
    """
    if value != value or value in (float("inf"), float("-inf")):
        raise InputRejected("non_finite_number", field_label)
    return value
