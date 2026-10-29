"""Automated tests for QRForge.

Run from the project folder with:  python -m unittest discover -s tests -v
(pytest also works if you have it installed).

These tests check real output: PNG files are opened with Pillow and checked for
dimensions and pixel colors, SVGs are parsed as XML, and Wi-Fi/vCard/mailto
payloads are compared against exact expected strings. If OpenCV happens to be
installed, PNGs are additionally decoded by an independent QR reader.
"""

import base64
import io
import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from app import create_app
from qr_service import PayloadTooLargeError, generate_qr
from validators import (
    RenderOptions, ValidationError, build_email_payload, build_phone_payload, build_sms_payload,
    build_vcard_payload, build_wifi_payload, _vcard_escape, contrast_ratio, normalize_url,
)

try:  # optional independent decoder, never required
    import cv2
    import numpy
except ImportError:
    cv2 = None

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def decode_png_with_opencv(png_bytes):
    image = cv2.imdecode(numpy.frombuffer(png_bytes, numpy.uint8), cv2.IMREAD_COLOR)
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(image)
    return text


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = create_app({})
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def generate(self, content_type="url", data=None, **options):
        body = {"type": content_type, "data": data if data is not None else {"url": "https://example.com"}}
        body.update(options)
        return self.client.post("/api/generate", json=body)

    def image_bytes(self, response):
        return base64.b64decode(response.get_json()["image_base64"])

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        error = response.get_json()["error"]
        self.assertEqual(error["code"], code)
        self.assertTrue(error["message"])
        return error


class NormalGenerationTests(ApiTestCase):
    def test_url_png_is_valid_png(self):
        response = self.generate("url", {"url": "https://example.com/a?b=1#c"}, format="png", size=1024)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["mime_type"], "image/png")
        self.assertEqual(body["filename"], "qrforge-website.png")
        self.assertEqual(body["payload"], "https://example.com/a?b=1#c")
        png = self.image_bytes(response)
        self.assertTrue(png.startswith(PNG_SIGNATURE))
        image = Image.open(io.BytesIO(png))
        image.load()
        self.assertEqual(image.size, (1024, 1024))
        self.assertEqual(body["pixel_size"], 1024)

    def test_text_svg_is_valid_svg(self):
        response = self.generate("text", {"text": "Hello QRForge"}, format="svg")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["mime_type"], "image/svg+xml")
        self.assertEqual(body["filename"], "qrforge-text.svg")
        root = ET.fromstring(self.image_bytes(response))
        self.assertTrue(root.tag.endswith("svg"))
        self.assertIn("viewBox", root.attrib)
        self.assertTrue(any(el.tag.endswith("path") for el in root.iter()), "SVG should contain vector path data")
        self.assertFalse(any(el.tag.endswith("image") for el in root.iter()), "SVG must not embed a raster image")

    def test_unicode_payloads(self):
        text = "नमस्ते दुनिया — héllo — 日本語 — 🙂"
        response = self.generate("text", {"text": text}, format="png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["payload"], text)
        if cv2:
            self.assertEqual(decode_png_with_opencv(self.image_bytes(response)), text)

    def test_wifi_generation_and_payload(self):
        response = self.generate("wifi", {"ssid": "Home;Net", "password": "p4ss:word,1", "security": "WPA", "hidden": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["payload"], "WIFI:T:WPA;S:Home\\;Net;P:p4ss\\:word\\,1;H:true;;")

    def test_contact_generation(self):
        response = self.generate("contact", {"full_name": "Asha Rao", "phone": "+91 98765 43210",
                                             "email": "asha@example.com", "organization": "Rao, Sons; Co",
                                             "website": "example.com"})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["payload"]
        self.assertTrue(payload.startswith("BEGIN:VCARD\r\nVERSION:3.0\r\n"))
        self.assertIn("FN:Asha Rao", payload)
        self.assertIn("N:Rao;Asha;;;", payload)
        self.assertIn("TEL;TYPE=CELL:+919876543210", payload)
        self.assertIn("ORG:Rao\\, Sons\\; Co", payload)
        self.assertIn("URL:https://example.com", payload)
        self.assertTrue(payload.endswith("END:VCARD"))

    def test_email_generation(self):
        response = self.generate("email", {"to": "a@example.com", "subject": "Hi & bye", "body": "Line 1\nLine 2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["payload"], "mailto:a@example.com?subject=Hi%20%26%20bye&body=Line%201%0D%0ALine%202")

    def test_phone_and_sms(self):
        phone = self.generate("phone", {"number": "+91 (98765) 43-210"})
        self.assertEqual(phone.get_json()["payload"], "tel:+919876543210")
        self.assertEqual(phone.get_json()["filename"], "qrforge-phone.png")
        sms = self.generate("sms", {"number": "98765 43210", "message": "Hello there"})
        self.assertEqual(sms.get_json()["payload"], "sms:9876543210?body=Hello%20there")

    def test_url_without_scheme_is_normalized_transparently(self):
        response = self.generate("url", {"url": "example.com/menu"})
        body = response.get_json()
        self.assertEqual(body["payload"], "https://example.com/menu")
        self.assertTrue(any("https://" in w for w in body["warnings"]))

    @unittest.skipUnless(cv2, "OpenCV not installed (optional independent decoder)")
    def test_png_round_trip_with_independent_decoder(self):
        for ec in ("L", "M", "Q", "H"):
            with self.subTest(ec=ec):
                response = self.generate("url", {"url": "https://example.com/round-trip"}, error_correction=ec,
                                         size=512, foreground="#112233", background="#F5F5DC")
                self.assertEqual(decode_png_with_opencv(self.image_bytes(response)), "https://example.com/round-trip")


class ConfigurationTests(ApiTestCase):
    def test_all_error_correction_levels(self):
        versions = {}
        for level in ("L", "M", "Q", "H"):
            with self.subTest(level=level):
                response = self.generate(error_correction=level)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["error_correction"], level)
                versions[level] = response.get_json()["version"]
        # Stronger error correction never needs a smaller symbol for the same data.
        self.assertTrue(versions["L"] <= versions["M"] <= versions["Q"] <= versions["H"])

    def test_error_correction_changes_the_symbol(self):
        text = "x" * 120
        low = generate_qr(text, RenderOptions(error_correction="L"))
        high = generate_qr(text, RenderOptions(error_correction="H"))
        self.assertLess(low.version, high.version)

    def test_colors_are_applied_to_png(self):
        response = self.generate(foreground="#FF0000", background="#00FF00", size=512)
        image = Image.open(io.BytesIO(self.image_bytes(response))).convert("RGB")
        colors = {color for _, color in image.getcolors(maxcolors=10)}
        self.assertEqual(colors, {(255, 0, 0), (0, 255, 0)}, "only the two chosen colors may appear (no blur/anti-aliasing)")

    def test_colors_are_applied_to_svg(self):
        response = self.generate(format="svg", foreground="#112233", background="#FEDCBA")
        svg = self.image_bytes(response).decode("utf-8")
        self.assertIn("#112233", svg)
        self.assertIn("#FEDCBA", svg)

    def test_supported_sizes_give_exact_dimensions(self):
        for size in (512, 1024, 2048):
            with self.subTest(size=size):
                image = Image.open(io.BytesIO(self.image_bytes(self.generate(size=size, format="png"))))
                self.assertEqual(image.size, (size, size))

    def test_png_modules_are_crisp_uniform_squares(self):
        # With 512px and a 25-module code plus a 4-module quiet zone, scale must be a whole number.
        response = self.generate(size=512)
        body = response.get_json()
        image = Image.open(io.BytesIO(self.image_bytes(response))).convert("RGB")
        total = body["modules"] + 2 * body["quiet_zone"]
        scale = 512 // total
        pad = (512 - total * scale) // 2
        # Every pixel inside one module block has the same color.
        for module_x, module_y in ((4, 4), (5, 4), (body["modules"] + 3, 4)):
            x0, y0 = pad + module_x * scale, pad + module_y * scale
            block = {image.getpixel((x0 + dx, y0 + dy)) for dx in range(scale) for dy in range(scale)}
            self.assertEqual(len(block), 1)

    def test_quiet_zone_is_blank_in_png(self):
        response = self.generate(size=512, border=4)
        body = response.get_json()
        image = Image.open(io.BytesIO(self.image_bytes(response))).convert("RGB")
        total = body["modules"] + 2 * body["quiet_zone"]
        scale = 512 // total
        pad = (512 - total * scale) // 2
        margin_px = pad + 4 * scale
        for x in range(512):
            for y in list(range(margin_px)) + list(range(512 - margin_px, 512)):
                self.assertEqual(image.getpixel((x, y)), (255, 255, 255))

    def test_quiet_zone_minimum_enforced(self):
        for border in (0, 1, 3, -2):
            with self.subTest(border=border):
                self.assert_error(self.generate(border=border), 400, "QUIET_ZONE_TOO_SMALL")
        self.assertEqual(self.generate(border=6).get_json()["quiet_zone"], 6)

    def test_svg_viewbox_includes_quiet_zone(self):
        response = self.generate(format="svg", border=4)
        body = response.get_json()
        root = ET.fromstring(self.image_bytes(response))
        side = body["modules"] + 8
        self.assertEqual(root.attrib["viewBox"], f"0 0 {side} {side}")

    def test_low_contrast_and_inverted_warnings(self):
        low = self.generate(foreground="#777777", background="#888888").get_json()
        self.assertTrue(any("contrast" in w.lower() for w in low["warnings"]))
        inverted = self.generate(foreground="#FFFFFF", background="#000000").get_json()
        self.assertTrue(any("inverted" in w.lower() for w in inverted["warnings"]))
        self.assertEqual(self.generate().get_json()["warnings"], [])

    def test_dense_code_warning(self):
        response = self.generate("text", {"text": "dense " * 120}, error_correction="H")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("dense" in w.lower() for w in response.get_json()["warnings"]))

    def test_contrast_ratio_math(self):
        self.assertAlmostEqual(contrast_ratio("#000000", "#FFFFFF"), 21.0, places=1)
        self.assertAlmostEqual(contrast_ratio("#FFFFFF", "#FFFFFF"), 1.0, places=2)


class FailureTests(ApiTestCase):
    def test_malformed_json(self):
        response = self.client.post("/api/generate", data="{not json", content_type="application/json")
        self.assert_error(response, 400, "MALFORMED_REQUEST")

    def test_non_object_json(self):
        self.assert_error(self.client.post("/api/generate", json=["a"]), 400, "MALFORMED_REQUEST")

    def test_missing_data(self):
        self.assert_error(self.client.post("/api/generate", json={"type": "url"}), 400, "MISSING_DATA")
        self.assert_error(self.generate("url", {}), 400, "MISSING_FIELD")
        self.assert_error(self.generate("text", {"text": "   "}), 400, "MISSING_FIELD")

    def test_invalid_content_type(self):
        self.assert_error(self.generate("calendar", {"x": 1}), 400, "INVALID_TYPE")
        self.assert_error(self.client.post("/api/generate", json={"data": {}}), 400, "INVALID_TYPE")

    def test_invalid_format(self):
        for bad in ("gif", "PNG", "", None, 5):
            with self.subTest(bad=bad):
                self.assert_error(self.generate(format=bad), 400, "INVALID_FORMAT")

    def test_invalid_error_correction(self):
        for bad in ("X", "m", "", None, 1):
            with self.subTest(bad=bad):
                self.assert_error(self.generate(error_correction=bad), 400, "INVALID_ERROR_CORRECTION")

    def test_invalid_colors(self):
        for bad in ("red", "#FFF", "#GGGGGG", "000000", "#0000000", "", None, 123, "url(x)"):
            with self.subTest(bad=bad):
                self.assert_error(self.generate(foreground=bad), 400, "INVALID_COLOR")
                self.assert_error(self.generate(background=bad), 400, "INVALID_COLOR")
        self.assert_error(self.generate(foreground="#123456", background="#123456"), 400, "INVALID_COLOR")

    def test_invalid_sizes(self):
        for bad in (0, -5, 100, 100000, "1024", 1024.5, None, True):
            with self.subTest(bad=bad):
                self.assert_error(self.generate(size=bad), 400, "INVALID_SIZE")

    def test_invalid_urls(self):
        for bad in ("hello world", "javascript:alert(1)", "ftp://example.com", "http://", "https://exa mple.com",
                    "http://example.com:99999", "data:text/html,hi", "justaword"):
            with self.subTest(bad=bad):
                self.assert_error(self.generate("url", {"url": bad}), 400, "INVALID_URL")

    def test_wrong_field_types(self):
        self.assert_error(self.generate("text", {"text": 123}), 400, "INVALID_FIELD")
        self.assert_error(self.generate("wifi", {"ssid": ["a"], "password": "x"}), 400, "INVALID_FIELD")

    def test_excessive_payload_returns_capacity_error(self):
        response = self.generate("text", {"text": "A" * 5000})
        error = self.assert_error(response, 413, "PAYLOAD_TOO_LARGE")
        self.assertNotIn("Traceback", error["message"])

    def test_capacity_depends_on_error_correction(self):
        text = "A" * 2500
        self.assertEqual(self.generate("text", {"text": text}, error_correction="L").status_code, 200)
        self.assert_error(self.generate("text", {"text": text}, error_correction="H"), 413, "PAYLOAD_TOO_LARGE")

    def test_oversized_request_is_rejected(self):
        response = self.client.post("/api/generate", json={"type": "text", "data": {"text": "A" * (2 * 1024 * 1024)}})
        self.assert_error(response, 413, "REQUEST_TOO_LARGE")

    def test_invalid_wifi_inputs(self):
        self.assert_error(self.generate("wifi", {"ssid": "", "password": "password1"}), 400, "MISSING_FIELD")
        self.assert_error(self.generate("wifi", {"ssid": "Net", "password": ""}), 400, "MISSING_FIELD")
        self.assert_error(self.generate("wifi", {"ssid": "Net", "password": "short"}), 400, "INVALID_PASSWORD")
        self.assert_error(self.generate("wifi", {"ssid": "Net", "password": "password1", "security": "WPA9"}), 400, "INVALID_SECURITY")
        self.assert_error(self.generate("wifi", {"ssid": "x" * 40, "password": "password1"}), 400, "INVALID_SSID")
        self.assert_error(self.generate("wifi", {"ssid": "Net", "password": "password1", "hidden": "yes"}), 400, "INVALID_FIELD")

    def test_invalid_contact_email_phone(self):
        self.assert_error(self.generate("contact", {"full_name": ""}), 400, "MISSING_FIELD")
        self.assert_error(self.generate("contact", {"full_name": "A B", "email": "nope"}), 400, "INVALID_EMAIL")
        self.assert_error(self.generate("contact", {"full_name": "A B", "phone": "abc"}), 400, "INVALID_PHONE")
        # A line break inside a field would let a user inject extra vCard properties, so it is rejected.
        self.assert_error(self.generate("contact", {"full_name": "A B\nTEL:999"}), 400, "INVALID_FIELD")
        self.assert_error(self.generate("email", {"to": "bad"}), 400, "INVALID_EMAIL")
        self.assert_error(self.generate("email", {"to": "a@example.com", "subject": "a\nb"}), 400, "INVALID_FIELD")
        self.assert_error(self.generate("phone", {"number": "12"}), 400, "INVALID_PHONE")


class ApiBehaviorTests(ApiTestCase):
    def test_response_shape_and_metadata_are_real(self):
        body = self.generate("url", {"url": "https://example.com"}, error_correction="Q").get_json()
        for key in ("filename", "mime_type", "format", "image_base64", "payload", "version", "modules",
                    "error_correction", "quiet_zone", "warnings"):
            self.assertIn(key, body)
        self.assertEqual(body["modules"], 17 + 4 * body["version"])  # QR geometry: modules = 17 + 4*version
        self.assertEqual(body["error_correction"], "Q")

    def test_content_types_and_headers(self):
        self.assertEqual(self.generate().mimetype, "application/json")
        self.assertEqual(self.generate().headers["Cache-Control"], "no-store")
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("default-src 'self'", page.headers["Content-Security-Policy"])

    def test_no_traceback_or_paths_leak(self):
        responses = [self.generate("calendar", {}), self.generate("text", {"text": "A" * 5000}),
                     self.client.post("/api/generate", data="x", content_type="application/json"),
                     self.client.get("/api/generate"), self.client.get("/api/nope")]
        for response in responses:
            text = response.get_data(as_text=True)
            for leak in ("Traceback", "File \"", PROJECT_DIR, ".py"):
                self.assertNotIn(leak, text)
            self.assertEqual(response.mimetype, "application/json")

    def test_wrong_method_is_json(self):
        self.assert_error(self.client.get("/api/generate"), 405, "METHOD_NOT_ALLOWED")

    def test_unexpected_error_is_sanitized(self):
        import app as app_module
        original = app_module.generate_qr
        app_module.generate_qr = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret-" + "password-123 /home/x/file.py"))
        try:
            client = create_app({}).test_client()
            with self.assertLogs("qrforge", level="ERROR") as logs:
                response = client.post("/api/generate", json={"type": "text", "data": {"text": "hi"}})
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.get_json()["error"]["code"], "INTERNAL_ERROR")
            self.assertNotIn("secret-password-123", response.get_data(as_text=True))
            self.assertNotIn("secret-password-123", "\n".join(logs.output), "exception messages must not reach the log")
        finally:
            app_module.generate_qr = original

    def test_nothing_is_written_to_disk(self):
        def snapshot():
            return sorted((root, name) for root, dirs, files in os.walk(PROJECT_DIR)
                          for name in files if "__pycache__" not in root and ".venv" not in root)
        before = snapshot()
        for fmt in ("png", "svg"):
            self.generate("wifi", {"ssid": "N", "password": "password1"}, format=fmt)
        self.assertEqual(before, snapshot())

    def test_repeated_requests_are_independent_and_unlimited(self):
        # No quota: many consecutive generations all succeed.
        for i in range(60):
            response = self.generate("text", {"text": f"code {i}"}, size=512)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["payload"], f"code {i}")

    def test_filenames_never_come_from_payload(self):
        response = self.generate("text", {"text": "../../etc/passwd; rm -rf /"})
        self.assertEqual(response.get_json()["filename"], "qrforge-text.png")


class PayloadBuilderTests(unittest.TestCase):
    def test_wifi_open_network(self):
        self.assertEqual(build_wifi_payload({"ssid": "Cafe Wifi", "security": "nopass"}), "WIFI:T:nopass;S:Cafe Wifi;;")

    def test_wifi_escapes_every_special_character(self):
        payload = build_wifi_payload({"ssid": 'a\\b;c,d:e"f', "password": 'p\\;,:"word', "security": "WPA"})
        self.assertEqual(payload, 'WIFI:T:WPA;S:a\\\\b\\;c\\,d\\:e\\"f;P:p\\\\\\;\\,\\:\\"word;;')

    def test_wifi_hex_only_values_are_quoted(self):
        self.assertEqual(build_wifi_payload({"ssid": "1234", "password": "12345678", "security": "WPA"}),
                         'WIFI:T:WPA;S:"1234";P:"12345678";;')
        self.assertEqual(build_wifi_payload({"ssid": "123", "password": "12345678a", "security": "WPA"}),
                         "WIFI:T:WPA;S:123;P:12345678a;;")  # odd length cannot be hex bytes

    def test_wifi_password_not_stripped(self):
        self.assertIn("P: pass word ;", build_wifi_payload({"ssid": "N", "password": " pass word ", "security": "WEP"}))

    def test_wifi_unicode_ssid(self):
        self.assertIn("S:घर वाईफाई;", build_wifi_payload({"ssid": "घर वाईफाई", "password": "password1"}))

    def test_vcard_escapes_newlines_and_specials(self):
        self.assertEqual(_vcard_escape("A\nB\r\nC"), "A\\nB\\nC")
        card = build_vcard_payload({"full_name": "Dr. Jane; Q, Public", "organization": "A"})
        self.assertIn("FN:Dr. Jane\\; Q\\, Public", card)
        self.assertEqual(card.count("\r\n"), card.count("\n"), "all line breaks must be CRLF")

    def test_vcard_single_name(self):
        self.assertIn("N:;Madonna;;;", build_vcard_payload({"full_name": "Madonna"}))

    def test_email_encoding(self):
        payload = build_email_payload({"to": "x@example.com", "subject": "50% off? yes/no", "body": "é"})
        self.assertEqual(payload, "mailto:x@example.com?subject=50%25%20off%3F%20yes%2Fno&body=%C3%A9")
        self.assertEqual(build_email_payload({"to": "x@example.com"}), "mailto:x@example.com")

    def test_phone_and_sms_formats(self):
        self.assertEqual(build_phone_payload({"number": "(022) 1234-5678"}), "tel:02212345678")
        self.assertEqual(build_sms_payload({"number": "+1 555 0100"}), "sms:+15550100")

    def test_url_normalization(self):
        self.assertEqual(normalize_url("HTTPS://Example.com/x?y=1&z=2#frag")[0], "HTTPS://Example.com/x?y=1&z=2#frag")
        self.assertEqual(normalize_url("localhost:8000/a"), ("https://localhost:8000/a", "No scheme was given, so https:// was added: https://localhost:8000/a"))
        with self.assertRaises(ValidationError):
            normalize_url("mailto:a@b.com")


class QrServiceTests(unittest.TestCase):
    def test_payload_too_large_raises_specific_error(self):
        with self.assertRaises(PayloadTooLargeError):
            generate_qr("A" * 4000, RenderOptions())

    def test_no_truncation(self):
        # A payload that nearly fills a version-40-L symbol must encode in full.
        result = generate_qr("a" * 2900, RenderOptions(error_correction="L", output_format="svg"))
        self.assertEqual(result.version, 40)

    def test_small_png_size_still_has_whole_pixel_modules(self):
        # Large symbol, small requested size: output grows to the next whole-pixel scale instead of blurring.
        result = generate_qr("a" * 2900, RenderOptions(error_correction="L", size=256))
        image = Image.open(io.BytesIO(result.image_bytes))
        self.assertGreaterEqual(image.size[0], 256)
        self.assertEqual(image.size[0], image.size[1])
        self.assertLessEqual(len(image.convert("RGB").getcolors(maxcolors=10)), 2)


if __name__ == "__main__":
    unittest.main()
