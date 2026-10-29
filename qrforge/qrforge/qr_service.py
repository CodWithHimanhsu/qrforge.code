"""QR code generation for QRForge.

This module knows nothing about Flask or HTTP. It takes a finished payload
string plus validated options and returns image bytes with metadata.

Design notes
------------
* The ``qrcode`` library builds the symbol (version selection, Reed-Solomon
  error correction, masking). We always use its matrix and quiet-zone logic.
* PNGs are rendered from that matrix with Pillow using whole-number pixel
  scaling and nearest-neighbour resizing, so module edges stay perfectly sharp.
* SVGs use the library's own ``SvgPathImage`` (a single vector <path>).
* Nothing is written to disk.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import qrcode
from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q
from qrcode.exceptions import DataOverflowError
from qrcode.image.svg import SvgPathImage
from PIL import Image

from validators import RenderOptions

_EC_LEVELS = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q, "H": ERROR_CORRECT_H}

MIME_TYPES = {"png": "image/png", "svg": "image/svg+xml"}

# Versions at or above this produce noticeably dense symbols (57x57+ modules).
DENSE_VERSION_THRESHOLD = 10


class PayloadTooLargeError(Exception):
    """The content does not fit in any QR version at the selected error-correction level."""


@dataclass(frozen=True)
class QRResult:
    image_bytes: bytes
    mime_type: str
    output_format: str
    version: int          # QR version 1-40
    modules: int          # modules per side, excluding the quiet zone
    error_correction: str
    pixel_size: int | None  # actual PNG width/height; None for SVG
    warnings: list[str]


def _build_qr(payload: str, error_correction: str, border: int) -> qrcode.QRCode:
    """Create and populate a QRCode, choosing the smallest version that fits.

    ``border`` is the quiet zone in modules; the library adds it to both the
    matrix (PNG) and the SVG viewBox.
    """
    qr = qrcode.QRCode(
        version=None,  # auto-select smallest version
        error_correction=_EC_LEVELS[error_correction],
        box_size=10,  # SVG units: viewBox is measured in modules regardless
        border=border,
    )
    qr.add_data(payload)
    try:
        qr.make(fit=True)
    except (DataOverflowError, ValueError) as exc:
        # qrcode raises DataOverflowError (or ValueError for version 40 overflow)
        # when the data cannot fit. Re-raise as our own, message-free, error.
        raise PayloadTooLargeError from exc
    return qr


def _render_png(qr: qrcode.QRCode, options: RenderOptions) -> tuple[bytes, int]:
    """Render with integer module scaling, centred on a square canvas of the requested size.

    Using an exact integer pixels-per-module guarantees uniform, crisp modules.
    If the requested size is not an exact multiple, extra background-coloured
    padding is added around the code (which only enlarges the quiet zone).
    """
    matrix = qr.get_matrix()  # includes the quiet zone
    total = len(matrix)

    small = Image.new("RGB", (total, total), options.background)
    pixels = small.load()
    foreground = _rgb(options.foreground)
    for y, row in enumerate(matrix):
        for x, is_dark in enumerate(row):
            if is_dark:
                pixels[x, y] = foreground

    scale = max(1, options.size // total)
    scaled = small.resize((total * scale, total * scale), Image.NEAREST)

    canvas_size = max(options.size, total * scale)
    canvas = Image.new("RGB", (canvas_size, canvas_size), options.background)
    offset = (canvas_size - total * scale) // 2
    canvas.paste(scaled, (offset, offset))

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), canvas_size


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _render_svg(qr: qrcode.QRCode, options: RenderOptions) -> bytes:
    """Render a single-path vector SVG whose viewBox is measured in modules."""
    size = options.size
    foreground, background = options.foreground, options.background

    class _ConfiguredSvg(SvgPathImage):
        QR_PATH_STYLE = {**SvgPathImage.QR_PATH_STYLE, "fill": foreground}
        background = None  # set below; class attribute used by SvgImage._svg

        def _svg(self, *args, **kwargs):
            svg = super()._svg(*args, **kwargs)
            # Pixel width/height (the library defaults to millimetres); the
            # viewBox keeps the artwork scalable to any size.
            svg.set("width", str(size))
            svg.set("height", str(size))
            svg.set("shape-rendering", "crispEdges")
            return svg

    _ConfiguredSvg.background = background

    image = qr.make_image(image_factory=_ConfiguredSvg)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue()


def generate_qr(payload: str, options: RenderOptions) -> QRResult:
    """Generate a QR code image for ``payload`` in memory.

    Raises:
        PayloadTooLargeError: the payload exceeds QR capacity for these settings.
    """
    qr = _build_qr(payload, options.error_correction, options.border)
    modules = qr.modules_count

    if options.output_format == "svg":
        image_bytes, pixel_size = _render_svg(qr, options), None
    else:
        image_bytes, pixel_size = _render_png(qr, options)

    warnings = []
    if qr.version >= DENSE_VERSION_THRESHOLD:
        warnings.append(
            "Your content creates a dense QR code. Use SVG or download a larger PNG, "
            "and test the printed size before distributing it. A shorter URL scans more easily."
        )

    return QRResult(
        image_bytes=image_bytes,
        mime_type=MIME_TYPES[options.output_format],
        output_format=options.output_format,
        version=qr.version,
        modules=modules,
        error_correction=options.error_correction,
        pixel_size=pixel_size,
        warnings=warnings,
    )
