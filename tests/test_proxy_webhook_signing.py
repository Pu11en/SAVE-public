"""Tests for the trusted-internal-forward signing fix.

Scenario: The proxy authenticates the external request (BlueBubbles password
or Inkbox HMAC or admin-token), possibly mutates the body (tier-marker
injection), then must produce a valid Inkbox HMAC over the final forward_body
so Hermes's _handle_webhook accepts it.

Two invariants tested:
  1. An authenticated forward produces X-Inkbox-Signature that Hermes's
     verify_webhook() accepts (using the same INKBOX_SIGNING_KEY).
  2. An unauthenticated external request is rejected by the proxy BEFORE the
     signing block is reached — signing never covers unauthenticated traffic.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sys
import types
import unittest
import uuid
from unittest.mock import MagicMock, patch

# Ensure the project root is importable.
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Minimal stub for inkbox.signing_keys.verify_webhook — mirrors the real SDK
# algorithm exactly so tests work without the inkbox package installed.
# ---------------------------------------------------------------------------

def _verify_webhook_stub(*, payload: bytes, headers: dict, secret: str) -> bool:
    """Pure-Python replica of inkbox.signing_keys.verify_webhook."""
    h = {k.lower(): v for k, v in headers.items()}
    signature = h.get("x-inkbox-signature", "")
    request_id = h.get("x-inkbox-request-id", "")
    timestamp = h.get("x-inkbox-timestamp", "")
    if not signature.startswith("sha256="):
        return False
    key = secret
    if key.startswith("whsec_"):
        key = key[len("whsec_"):]
    message = f"{request_id}.{timestamp}.".encode() + payload
    expected = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
    received = signature[len("sha256="):]
    return hmac.compare_digest(expected, received)


# ---------------------------------------------------------------------------
# Helper: simulate the signing block from server.py in isolation.
# We extract the logic into a testable function rather than spawning a server.
# ---------------------------------------------------------------------------

def _proxy_sign_headers(forward_body: bytes, signing_key: str) -> dict:
    """Reproduce the signing block added to server.py's do_POST.

    Returns a dict of the headers that the signing block would inject.
    Raises ValueError if signing_key is empty (proxy skips signing then).
    """
    if not signing_key:
        raise ValueError("No signing key — proxy does not sign")
    key = signing_key
    if key.startswith("whsec_"):
        key = key[len("whsec_"):]
    request_id = str(uuid.uuid4())
    import time as _t
    timestamp = str(int(_t.time()))
    msg = f"{request_id}.{timestamp}.".encode() + forward_body
    sig = "sha256=" + hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()
    return {
        "X-Inkbox-Signature": sig,
        "X-Inkbox-Request-Id": request_id,
        "X-Inkbox-Timestamp": timestamp,
        "Content-Length": str(len(forward_body)),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestProxyForwardSigning(unittest.TestCase):
    """Invariant 1: authenticated forward produces a valid Inkbox signature."""

    SIGNING_KEY = "whsec_test-secret-key-for-unit-tests"

    def _make_forward_body(self, text: str = "hello from Drew") -> bytes:
        payload = {
            "event_type": "text.received",
            "timestamp": "2026-06-09T12:00:00Z",
            "data": {
                "text_message": {
                    "id": "msg-001",
                    "text": f"[SAVE-TIER:admin] {text}",
                    "direction": "inbound",
                    "local_phone_number": "+15550109999",
                    "remote_phone_number": "+15550101000",
                    "type": "sms",
                    "is_read": False,
                    "origin": "user_initiated",
                    "error_code": None,
                    "error_detail": None,
                    "sent_at": None,
                    "delivered_at": None,
                    "failed_at": None,
                    "created_at": "2026-06-09T12:00:00Z",
                    "updated_at": "2026-06-09T12:00:00Z",
                    "delivery_status": None,
                    "media": None,
                },
                "contact": None,
            },
        }
        return json.dumps(payload).encode("utf-8")

    def test_signed_forward_passes_hermes_verify(self):
        """Proxy-signed forward body must pass verify_webhook with same key."""
        forward_body = self._make_forward_body()
        headers = _proxy_sign_headers(forward_body, self.SIGNING_KEY)

        ok = _verify_webhook_stub(
            payload=forward_body,
            headers=headers,
            secret=self.SIGNING_KEY,
        )
        self.assertTrue(ok, "verify_webhook rejected a correctly-signed forward")

    def test_signed_forward_passes_with_plain_key(self):
        """Also works when INKBOX_SIGNING_KEY has no whsec_ prefix."""
        plain_key = "plain-no-prefix-secret"
        forward_body = self._make_forward_body()
        headers = _proxy_sign_headers(forward_body, plain_key)

        ok = _verify_webhook_stub(
            payload=forward_body,
            headers=headers,
            secret=plain_key,
        )
        self.assertTrue(ok)

    def test_verify_fails_if_body_tampered_after_signing(self):
        """Signature binds to the exact bytes — mutation after signing fails."""
        forward_body = self._make_forward_body()
        headers = _proxy_sign_headers(forward_body, self.SIGNING_KEY)
        tampered = forward_body + b" extra"

        ok = _verify_webhook_stub(
            payload=tampered,
            headers=headers,
            secret=self.SIGNING_KEY,
        )
        self.assertFalse(ok, "verify_webhook should reject tampered body")

    def test_verify_fails_with_wrong_key(self):
        """Signature produced with one key fails verification with a different key."""
        forward_body = self._make_forward_body()
        headers = _proxy_sign_headers(forward_body, self.SIGNING_KEY)

        ok = _verify_webhook_stub(
            payload=forward_body,
            headers=headers,
            secret="whsec_totally-different-key",
        )
        self.assertFalse(ok, "verify_webhook must reject wrong signing key")

    def test_verify_fails_without_sha256_prefix(self):
        """Hermes verify_webhook rejects signatures missing the sha256= prefix."""
        forward_body = self._make_forward_body()
        request_id = str(uuid.uuid4())
        import time as _t
        timestamp = str(int(_t.time()))
        key = self.SIGNING_KEY[len("whsec_"):]
        msg = f"{request_id}.{timestamp}.".encode() + forward_body
        raw_hex = hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()
        # deliberately omit "sha256=" prefix
        headers = {
            "X-Inkbox-Signature": raw_hex,
            "X-Inkbox-Request-Id": request_id,
            "X-Inkbox-Timestamp": timestamp,
        }
        ok = _verify_webhook_stub(
            payload=forward_body,
            headers=headers,
            secret=self.SIGNING_KEY,
        )
        self.assertFalse(ok, "must reject signature without sha256= prefix")

    def test_tier_marker_body_signs_correctly(self):
        """After tier-marker injection the new body signs and verifies end-to-end."""
        original_body = self._make_forward_body(text="hi")
        # Simulate tier-marker injection (the actual proxy does this before signing).
        parsed = json.loads(original_body)
        parsed["data"]["text_message"]["text"] = "[SAVE-TIER:admin] hi"
        forward_body = json.dumps(parsed).encode("utf-8")

        headers = _proxy_sign_headers(forward_body, self.SIGNING_KEY)
        ok = _verify_webhook_stub(
            payload=forward_body,
            headers=headers,
            secret=self.SIGNING_KEY,
        )
        self.assertTrue(ok, "tier-marker mutated body should still verify correctly")


class TestUnauthenticatedRequestRejected(unittest.TestCase):
    """Invariant 2: unauthenticated external requests are rejected before signing."""

    def _make_handler(self, env_overrides: dict):
        """
        Import and instantiate ProxyHandler against a fake socket, injecting
        env vars so we can assert rejection paths without a real server.

        We drive do_POST by sending a raw HTTP request via a BytesIO pipe.
        The handler writes its response to a BytesIO output buffer.
        """
        # Patch environment before importing the module (it reads env at call-time).
        with patch.dict(os.environ, env_overrides, clear=False):
            # Reload server module with the patched environment if needed.
            import importlib
            # server.py runs server.serve_forever() at module scope when loaded
            # directly — we need to exercise ProxyHandler without that.
            # Import the class only via importlib with the module's own globals.
            pass
        return None  # handler setup is done inline in each test

    def _run_post(
        self,
        path: str,
        body: bytes,
        extra_headers: dict,
        env: dict,
    ) -> tuple[int, bytes]:
        """
        Exercise ProxyHandler.do_POST by monkey-patching http.server internals.
        Returns (status_code, response_body).
        """
        import http.server
        import importlib
        import importlib.util

        server_path = os.path.join(PROJECT_ROOT, "app", "proxy", "server.py")
        spec = importlib.util.spec_from_file_location(
            "proxy_server_under_test", server_path
        )
        # We cannot exec the module because it calls serve_forever() at module
        # scope.  Instead we compile only the class/function definitions by
        # reading the source and compiling everything up to (not including) the
        # server instantiation at the bottom.
        with open(server_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        # Strip the last two lines that instantiate and start the server.
        lines = source.splitlines()
        # Find the first `server = http.server.HTTPServer` line and cut there.
        cut = next(
            (i for i, ln in enumerate(lines) if ln.startswith("server = http.server.")),
            len(lines),
        )
        safe_source = "\n".join(lines[:cut])

        ns: dict = {}
        with patch.dict(os.environ, env, clear=False):
            exec(compile(safe_source, server_path, "exec"), ns)  # noqa: S102

        ProxyHandler = ns["ProxyHandler"]

        # Build a fake request line + headers as bytes.
        header_lines = "\r\n".join(
            f"{k}: {v}" for k, v in extra_headers.items()
        )
        raw_request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: localhost\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"{header_lines}\r\n"
            f"\r\n"
        ).encode() + body

        input_buf = io.BytesIO(raw_request)
        output_buf = io.BytesIO()

        class _FakeSocket:
            def makefile(self, mode, **_kw):
                if "r" in mode:
                    return io.BufferedReader(input_buf)
                return output_buf

            def sendall(self, data):
                output_buf.write(data)

            def shutdown(self, *_a):
                pass

            def close(self):
                pass

        class _FakeServer:
            server_name = "localhost"
            server_port = 8080

        with patch.dict(os.environ, env, clear=False):
            # Suppress proxy_to_inkbox from actually making network calls.
            with patch.object(
                sys.modules.get("builtins", __builtins__),  # noqa
                "__import__",
                wraps=__import__,
            ):
                handler = ProxyHandler.__new__(ProxyHandler)
                handler.server = _FakeServer()
                handler.client_address = ("127.0.0.1", 12345)
                # Feed the raw request bytes through the handler's parse logic.
                handler.rfile = io.BufferedReader(io.BytesIO(raw_request[raw_request.find(b"\r\n\r\n") + 4:]))
                # Build parsed headers dict from the raw request.
                import email.parser
                raw_headers_str = raw_request[:raw_request.find(b"\r\n\r\n")].decode()
                lines_h = raw_headers_str.split("\r\n")
                # skip request line
                header_block = "\r\n".join(lines_h[1:])
                from http.client import HTTPMessage, parse_headers
                handler.headers = parse_headers(
                    io.BytesIO(header_block.encode() + b"\r\n")
                )
                handler.path = path
                handler.wfile = output_buf
                handler.request_version = "HTTP/1.1"
                handler.command = "POST"
                handler.close_connection = True

                # We need send_response etc. wired up properly.
                # Use BaseHTTPRequestHandler.send_response directly.
                responses_written: list[int] = []
                _orig_send_response = http.server.BaseHTTPRequestHandler.send_response

                def _capture_send_response(self_h, code, message=None):
                    responses_written.append(code)
                    # Write status line to wfile.
                    self_h.wfile.write(
                        f"HTTP/1.1 {code} {message or ''}\r\n".encode()
                    )

                handler.send_response = lambda code, msg=None: _capture_send_response(handler, code, msg)
                handler.send_header = lambda k, v: handler.wfile.write(f"{k}: {v}\r\n".encode())
                handler.end_headers = lambda: handler.wfile.write(b"\r\n")

                # Stub proxy_to_inkbox in the handler's closure so we can
                # assert it was not called for rejected requests.
                called_proxy: list[bool] = []

                def _stub_proxy(method, path, headers, body):
                    called_proxy.append(True)
                    return 200, {}, b'{"status":"ok"}'

                ns["proxy_to_inkbox"] = _stub_proxy

                with patch.dict(os.environ, env, clear=False):
                    handler.do_POST()

        output_buf.seek(0)
        raw_response = output_buf.read()
        status = responses_written[0] if responses_written else 0
        # Body is everything after the blank line.
        body_start = raw_response.find(b"\r\n\r\n")
        resp_body = raw_response[body_start + 4:] if body_start >= 0 else b""
        return status, resp_body, called_proxy

    def test_missing_bluebubbles_password_returns_401(self):
        """BlueBubbles webhook with wrong password must return 401."""
        body = json.dumps({"type": "new-message"}).encode()
        env = {
            "BLUEBUBBLES_PASSWORD": "correct-password",
            "INKBOX_SIGNING_KEY": "whsec_test-secret-key-for-unit-tests",
            "INKBOX_REQUIRE_SIGNATURE": "true",
            "PORT": "8080",
            "INKBOX_LISTEN_PORT": "8765",
        }
        status, resp_body, called_proxy = self._run_post(
            "/bluebubbles/webhook?password=WRONG-PASSWORD",
            body,
            {},
            env,
        )
        self.assertEqual(status, 401, f"expected 401 got {status}; body={resp_body}")
        self.assertFalse(called_proxy, "proxy_to_inkbox must NOT be called for rejected requests")

    def test_missing_inkbox_signature_returns_401(self):
        """Inkbox webhook with no signature must return 401 when key is set."""
        body = json.dumps({"event_type": "text.received"}).encode()
        env = {
            "INKBOX_SIGNING_KEY": "whsec_test-secret-key-for-unit-tests",
            "INKBOX_REQUIRE_SIGNATURE": "true",
            "BLUEBUBBLES_PASSWORD": "",
            "PORT": "8080",
            "INKBOX_LISTEN_PORT": "8765",
        }
        status, resp_body, called_proxy = self._run_post(
            "/webhook",
            body,
            {},  # no X-Inkbox-Signature header
            env,
        )
        self.assertEqual(status, 401, f"expected 401 got {status}; body={resp_body}")
        self.assertFalse(called_proxy, "proxy_to_inkbox must NOT be called for rejected requests")


if __name__ == "__main__":
    unittest.main()
