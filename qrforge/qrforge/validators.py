"""Input validation and QR payload construction for QRForge.

Everything here is pure Python with no Flask dependency, so it can be tested
and reused (for example by a future batch feature) without an HTTP request.

User-supplied values are never executed or interpolated into code. Every
payload format (Wi-Fi, vCard, mailto, ...) is built by a dedicated function
that validates and escapes its inputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

# --------------------------------------------------------------------------
# Constants (allowlists)
# --------------------------------------------------------------------------

CONTENT_TYPES = ("url", "text", "wifi", "contact", "email", "phone", "sms")
OUTPUT_FORMATS = ("png", "svg")
ERROR_CORRECTION_LEVELS = ("L", "M", "Q", "H")
WIFI_SECURITY_TYPES = ("WPA", "WEP", "nopass")

MIN_QUIET_ZONE = 4  # QR specification minimum, in modules
MAX_QUIET_ZONE = 20
MIN_IMAGE_SIZE = 256
MAX_IMAGE_SIZE = 4096
DEFAULT_IMAGE_SIZE = 1024

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_EMAIL = re.compile(r"^[^@\s<>,;:\"\\]+@[^@\s<>,;:\"\\]+\.[^@\s<>,;:\"\\]{2,}$")
_BARE_HOST = re.compile(r"^(localhost|([a-z0-9\u00a1-\uffff-]+\.)+[a-z\u00a1-\uffff]{2,})([:/?#].*)?$", re.I)
_HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")
_PHONE_CHARS = re.compile(r"^\+?[0-9\s().\-]+$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class ValidationError(Exception):
    """A user-correctable problem with the request.

    ``code`` is a stable machine-readable identifier, ``message`` is shown to
    the user, and ``field`` optionally names the offending input.
    """

    def __init__(self, code: str, message: str, field: str | None = None, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.status = status


@dataclass(frozen=True)
class RenderOptions:
    """Validated, normalized appearance and output settings."""

    output_format: str = "png"
    size: int = DEFAULT_IMAGE_SIZE
    error_correction: str = "M"
    foreground: str = "#000000"
    background: str = "#FFFFFF"
    border: int = MIN_QUIET_ZONE


@dataclass(frozen=True)
class GenerationRequest:
    """A fully validated request: what to encode and how to render it."""

    content_type: str
    payload: str
    options: RenderOptions
    notes: list[str] = field(default_factory=list)  # transparent adjustments, e.g. added https://


# --------------------------------------------------------------------------
# Small field helpers
# --------------------------------------------------------------------------

def _get_str(data: dict[str, Any], key: str, *, required: bool = False, label: str | None = None,
             max_length: int = 5000, allow_newlines: bool = False) -> str:
    """Read an optional/required string field, rejecting non-strings and control characters."""
    label = label or key.replace("_", " ")
    value = data.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError("INVALID_FIELD", f"{label.capitalize()} must be text.", f"data.{key}")
    value = value.strip()
    if required and not value:
        raise ValidationError("MISSING_FIELD", f"{label.capitalize()} is required.", f"data.{key}")
    if len(value) > max_length:
        raise ValidationError("FIELD_TOO_LONG", f"{label.capitalize()} is too long.", f"data.{key}")
    check = value.replace("\n", "").replace("\r", "").replace("\t", "") if allow_newlines else value
    if _CONTROL_CHARS.search(check):
        raise ValidationError("INVALID_FIELD", f"{label.capitalize()} contains unsupported control characters.", f"data.{key}")
    return value


def _validate_email_address(value: str, key: str, label: str = "email address") -> str:
    if not _EMAIL.match(value):
        raise ValidationError("INVALID_EMAIL", f"Enter a valid {label}, such as name@example.com.", f"data.{key}")
    return value


def _normalize_phone_number(value: str, key: str) -> str:
    """Return the number with only digits and an optional leading '+'."""
    if not _PHONE_CHARS.match(value):
        raise ValidationError("INVALID_PHONE", "Phone numbers may contain digits, spaces, + ( ) - and . only.", f"data.{key}")
    digits = re.sub(r"\D", "", value)
    if not 3 <= len(digits) <= 15:
        raise ValidationError("INVALID_PHONE", "Phone numbers should have between 3 and 15 digits.", f"data.{key}")
    return ("+" if value.startswith("+") else "") + digits


def _percent_encode(value: str) -> str:
    """Percent-encode for URI query components (UTF-8, spaces as %20)."""
    return quote(value, safe="")


# --------------------------------------------------------------------------
# URL
# --------------------------------------------------------------------------

def normalize_url(raw: str, key: str = "url") -> tuple[str, str | None]:
    """Validate an http(s) URL. Returns (url, note).

    A value that is clearly a bare domain ("example.com/page") gets https://
    prepended, and a note says so. Anything else without a scheme is rejected
    rather than guessed at.
    """
    value = raw.strip()
    if not value:
        raise ValidationError("MISSING_FIELD", "Website address is required.", f"data.{key}")
    if re.search(r"\s", value) or _CONTROL_CHARS.search(value):
        raise ValidationError("INVALID_URL", "A URL cannot contain spaces or line breaks. Encode them (for example %20).", f"data.{key}")

    note = None
    if "://" not in value:
        if value.lower().startswith(("javascript:", "data:", "file:", "mailto:", "tel:", "sms:")) or not _BARE_HOST.match(value):
            raise ValidationError(
                "INVALID_URL",
                "Enter a full web address beginning with http:// or https:// (for example https://example.com).",
                f"data.{key}",
            )
        value = "https://" + value
        note = f"No scheme was given, so https:// was added: {value}"

    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for an invalid port
    except ValueError:
        raise ValidationError("INVALID_URL", "This web address could not be parsed. Check the port number and brackets.", f"data.{key}") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise ValidationError("INVALID_URL", "Only http:// and https:// addresses are supported.", f"data.{key}")
    if not parts.hostname:
        raise ValidationError("INVALID_URL", "The web address is missing a domain name.", f"data.{key}")
    return value, note


def build_url_payload(data: dict[str, Any]) -> tuple[str, list[str]]:
    url, note = normalize_url(_get_str(data, "url", required=True, label="website address", max_length=8000), "url")
    return url, [note] if note else []


# --------------------------------------------------------------------------
# Plain text
# --------------------------------------------------------------------------

def build_text_payload(data: dict[str, Any]) -> str:
    """Plain text is encoded exactly as typed (only outer whitespace is trimmed)."""
    return _get_str(data, "text", required=True, label="text", max_length=100_000, allow_newlines=True)


# --------------------------------------------------------------------------
# Wi-Fi
# --------------------------------------------------------------------------

def _wifi_escape(value: str, *, quote_hex: bool = True) -> str:
    """Escape Wi-Fi payload special characters: \\ ; , : and ".

    Values made only of an even number of hex digits could be read as raw hex
    bytes by some readers, so they are wrapped in double quotes (the ZXing
    convention). Odd-length values cannot be hex bytes and are left alone.
    """
    escaped = "".join("\\" + ch if ch in '\\;,:"' else ch for ch in value)
    if quote_hex and len(value) % 2 == 0 and _HEX_ONLY.match(value):
        return f'"{escaped}"'
    return escaped


def build_wifi_payload(data: dict[str, Any]) -> str:
    ssid = _get_str(data, "ssid", required=True, label="network name (SSID)", max_length=64)
    if len(ssid.encode("utf-8")) > 32:
        raise ValidationError("INVALID_SSID", "A network name (SSID) can be at most 32 bytes long.", "data.ssid")

    security = data.get("security", "WPA")
    if security not in WIFI_SECURITY_TYPES:
        raise ValidationError("INVALID_SECURITY", "Choose WPA/WPA2/WPA3, WEP, or No password.", "data.security")

    hidden = data.get("hidden", False)
    if not isinstance(hidden, bool):
        raise ValidationError("INVALID_FIELD", "Hidden network must be true or false.", "data.hidden")

    password = ""
    if security != "nopass":
        raw_password = data.get("password", "")
        if not isinstance(raw_password, str):
            raise ValidationError("INVALID_FIELD", "Password must be text.", "data.password")
        password = raw_password  # deliberately NOT stripped: spaces can be part of a password
        if not password:
            raise ValidationError("MISSING_FIELD", "Enter the Wi-Fi password, or choose 'No password'.", "data.password")
        if len(password) > 64 or _CONTROL_CHARS.search(password):
            raise ValidationError("INVALID_FIELD", "The password is too long or contains unsupported characters.", "data.password")
        is_raw_psk = len(password) == 64 and bool(_HEX_ONLY.match(password))
        if security == "WPA" and not is_raw_psk and not 8 <= len(password) <= 63:
            raise ValidationError("INVALID_PASSWORD", "A WPA password must be 8 to 63 characters long.", "data.password")

    parts = [f"T:{security}", f"S:{_wifi_escape(ssid)}"]
    if security != "nopass":
        is_raw_psk = len(password) == 64 and bool(_HEX_ONLY.match(password))
        parts.append(f"P:{_wifi_escape(password, quote_hex=not is_raw_psk)}")
    if hidden:
        parts.append("H:true")
    return "WIFI:" + ";".join(parts) + ";;"


# --------------------------------------------------------------------------
# Contact card (vCard 3.0)
# --------------------------------------------------------------------------

def _vcard_escape(value: str) -> str:
    """Escape text per RFC 6350/2426: backslash, comma, semicolon, newline."""
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def build_vcard_payload(data: dict[str, Any]) -> str:
    """Build a vCard 3.0 (RFC 2426) with CRLF line endings.

    Lines are not folded at 75 octets; mainstream phone scanners accept long
    lines and folding would only add bytes to the QR code.
    """
    full_name = _get_str(data, "full_name", required=True, label="full name", max_length=200)
    phone = _get_str(data, "phone", label="phone number", max_length=40)
    email = _get_str(data, "email", label="email address", max_length=254)
    organization = _get_str(data, "organization", label="organization", max_length=200)
    website = _get_str(data, "website", label="website", max_length=2000)

    name_parts = full_name.split()
    family = name_parts[-1] if len(name_parts) > 1 else ""
    given = " ".join(name_parts[:-1]) if len(name_parts) > 1 else full_name

    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"N:{_vcard_escape(family)};{_vcard_escape(given)};;;",
        f"FN:{_vcard_escape(full_name)}",
    ]
    if organization:
        lines.append(f"ORG:{_vcard_escape(organization)}")
    if phone:
        lines.append(f"TEL;TYPE=CELL:{_normalize_phone_number(phone, 'phone')}")
    if email:
        lines.append(f"EMAIL;TYPE=INTERNET:{_validate_email_address(email, 'email')}")
    if website:
        site, _ = normalize_url(website, "website")
        lines.append(f"URL:{_vcard_escape(site)}")
    lines.append("END:VCARD")
    return "\r\n".join(lines)


# --------------------------------------------------------------------------
# Email, telephone, SMS
# --------------------------------------------------------------------------

def build_email_payload(data: dict[str, Any]) -> str:
    """Build an RFC 6068 ``mailto:`` URI."""
    to = _validate_email_address(_get_str(data, "to", required=True, label="recipient email", max_length=254), "to", "recipient email address")
    subject = _get_str(data, "subject", label="subject", max_length=500)
    body = _get_str(data, "body", label="message", max_length=5000, allow_newlines=True)
    if "\n" in subject or "\r" in subject:
        raise ValidationError("INVALID_FIELD", "The subject must be a single line.", "data.subject")

    query = []
    if subject:
        query.append("subject=" + _percent_encode(subject))
    if body:
        query.append("body=" + _percent_encode(body.replace("\r\n", "\n").replace("\n", "\r\n")))
    return "mailto:" + quote(to, safe="@+._-") + ("?" + "&".join(query) if query else "")


def build_phone_payload(data: dict[str, Any]) -> str:
    number = _normalize_phone_number(_get_str(data, "number", required=True, label="phone number", max_length=40), "number")
    return f"tel:{number}"


def build_sms_payload(data: dict[str, Any]) -> str:
    """Build an RFC 5724 ``sms:`` URI. Scanners normally ask before sending."""
    number = _normalize_phone_number(_get_str(data, "number", required=True, label="phone number", max_length=40), "number")
    message = _get_str(data, "message", label="message", max_length=1000, allow_newlines=True)
    return f"sms:{number}" + (f"?body={_percent_encode(message)}" if message else "")


# --------------------------------------------------------------------------
# Rendering options
# --------------------------------------------------------------------------

def validate_hex_color(value: Any, key: str) -> str:
    if not isinstance(value, str) or not _HEX_COLOR.match(value):
        raise ValidationError("INVALID_COLOR", "Colors must be six-digit hex values such as #000000.", key)
    return value.upper()


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def relative_luminance(color: str) -> float:
    """WCAG relative luminance of a #RRGGBB color."""
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in _hex_to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(color_a: str, color_b: str) -> float:
    lighter, darker = sorted((relative_luminance(color_a), relative_luminance(color_b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _validate_int(value: Any, key: str, minimum: int, maximum: int, label: str) -> int:
    # bool is a subclass of int, so exclude it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("INVALID_" + key.upper(), f"{label} must be a whole number.", key)
    if not minimum <= value <= maximum:
        raise ValidationError("INVALID_" + key.upper(), f"{label} must be between {minimum} and {maximum}.", key)
    return value


def parse_render_options(body: dict[str, Any]) -> RenderOptions:
    output_format = body.get("format", "png")
    if output_format not in OUTPUT_FORMATS:
        raise ValidationError("INVALID_FORMAT", "Choose PNG or SVG as the output format.", "format")

    error_correction = body.get("error_correction", "M")
    if error_correction not in ERROR_CORRECTION_LEVELS:
        raise ValidationError("INVALID_ERROR_CORRECTION", "Error correction must be L, M, Q or H.", "error_correction")

    size = _validate_int(body.get("size", DEFAULT_IMAGE_SIZE), "size", MIN_IMAGE_SIZE, MAX_IMAGE_SIZE, "Image size")
    border = body.get("border", MIN_QUIET_ZONE)
    if isinstance(border, int) and not isinstance(border, bool) and border < MIN_QUIET_ZONE:
        raise ValidationError(
            "QUIET_ZONE_TOO_SMALL",
            f"The quiet zone must be at least {MIN_QUIET_ZONE} modules; scanners rely on it.",
            "border",
        )
    border = _validate_int(border, "border", MIN_QUIET_ZONE, MAX_QUIET_ZONE, "Quiet zone")

    foreground = validate_hex_color(body.get("foreground", "#000000"), "foreground")
    background = validate_hex_color(body.get("background", "#FFFFFF"), "background")
    if foreground == background:
        raise ValidationError("INVALID_COLOR", "Foreground and background colors must be different.", "foreground")

    return RenderOptions(output_format, size, error_correction, foreground, background, border)


def contrast_warnings(options: RenderOptions) -> list[str]:
    """Advisory messages about color choices. These are estimates, not guarantees."""
    warnings = []
    ratio = contrast_ratio(options.foreground, options.background)
    if relative_luminance(options.foreground) > relative_luminance(options.background):
        warnings.append("Light modules on a dark background (an inverted code) are not read by some scanners. Dark on light is safest.")
    if ratio < 4.0:
        warnings.append(f"The color contrast is low (about {ratio:.1f}:1). This may make the code harder to scan; black on white is the safest choice.")
    return warnings


# --------------------------------------------------------------------------
# Top-level request parsing
# --------------------------------------------------------------------------

def build_payload(content_type: str, data: dict[str, Any]) -> tuple[str, list[str]]:
    """Dispatch to the right payload builder. Returns (payload, notes)."""
    if content_type == "url":
        return build_url_payload(data)
    builders = {
        "text": build_text_payload,
        "wifi": build_wifi_payload,
        "contact": build_vcard_payload,
        "email": build_email_payload,
        "phone": build_phone_payload,
        "sms": build_sms_payload,
    }
    return builders[content_type](data), []


def parse_generation_request(body: Any) -> GenerationRequest:
    """Validate a decoded JSON request body (see README for the schema)."""
    if not isinstance(body, dict):
        raise ValidationError("MALFORMED_REQUEST", "The request must be a JSON object.")
    content_type = body.get("type")
    if content_type not in CONTENT_TYPES:
        raise ValidationError("INVALID_TYPE", "Unsupported content type.", "type")
    data = body.get("data")
    if not isinstance(data, dict):
        raise ValidationError("MISSING_DATA", "The request is missing its 'data' object.", "data")

    options = parse_render_options(body)
    payload, notes = build_payload(content_type, data)
    return GenerationRequest(content_type, payload, options, notes)
