"""
Tier gate — resolves sender phone to permission tier.

V1_SPEC §18.1. Wired into start.sh AFTER HMAC verify and BEFORE Hermes forward.
Sets X-Save-Tier: stranger|user|admin. Hermes loads MEMORY.md only when admin.

Lookup uses Inkbox-verified sender phone (never user-controlled payload fields).
Unknown phones auto-insert as stranger. DB failure fails CLOSED to stranger
(least privilege).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Optional, Union

VALID_TIERS = ("stranger", "user", "admin")
DEFAULT_TIER = "stranger"


def _drew_phone() -> str:
    return os.environ.get("DREW_PHONE", "").strip()


def _connect():
    """
    Lazy import of psycopg so unit tests can run without the driver installed.
    Raises RuntimeError if DATABASE_URL is missing or driver is unavailable.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set — tier_gate cannot resolve tiers")
    try:
        import psycopg  # type: ignore
    except ImportError as e:
        raise RuntimeError(f"psycopg not installed: {e}")
    return psycopg.connect(url)


def _looks_like_e164(s) -> bool:
    if not isinstance(s, str):
        return False
    s = s.strip()
    return s.startswith("+") and len(s) >= 8 and s[1:].isdigit()


def extract_sender_phone(payload: Union[bytes, bytearray, str, dict, None]) -> Optional[str]:
    """
    Pull an E.164 sender phone from an Inkbox webhook payload.

    Inkbox real wire shapes (inkbox/webhooks.py):
      Text webhook (envelope):
        {event_type, timestamp,
         data: {text_message: {direction, remote_phone_number, ...}, ...}}
      Inbound call (flat):
        {direction, remote_phone_number, local_phone_number, ...}

    Only returns a phone when direction == "inbound" — outbound delivery
    callbacks have no sender to gate. Legacy/test field names checked last
    so non-Inkbox payloads (e.g. internal dev probes) still resolve.

    Returns None when the payload is unparseable, outbound, or lacks a phone.
    """
    if payload is None:
        return None
    if isinstance(payload, dict):
        data = payload
    else:
        if isinstance(payload, (bytes, bytearray)):
            try:
                text = bytes(payload).decode("utf-8", errors="replace")
            except Exception:
                return None
        else:
            text = payload
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None

    # 1. Inkbox text webhook (enveloped). data.text_message.remote_phone_number
    envelope_data = data.get("data")
    if isinstance(envelope_data, dict):
        tm = envelope_data.get("text_message")
        if isinstance(tm, dict) and tm.get("direction") == "inbound":
            rpn = tm.get("remote_phone_number")
            if _looks_like_e164(rpn):
                return rpn.strip()
        # Inkbox message.received shape used by the current webhook:
        # {event_type:"message.received", data:{from:{value_e164:"+1..."}, text:"..."}}
        if data.get("event_type") in {"message.received", "text.received"}:
            sender = envelope_data.get("from")
            if isinstance(sender, dict):
                for key in ("value_e164", "e164", "phone"):
                    v = sender.get(key)
                    if _looks_like_e164(v):
                        return v.strip()

    # 2. Inkbox inbound call (flat). direction == "inbound", remote_phone_number
    if data.get("direction") == "inbound":
        rpn = data.get("remote_phone_number")
        if _looks_like_e164(rpn):
            return rpn.strip()

    # 3. Legacy / test / non-Inkbox shapes (back-compat fallback).
    candidates = []
    for key in ("sender_phone", "from_phone", "from"):
        v = data.get(key)
        if isinstance(v, str):
            candidates.append(v)

    sender = data.get("sender")
    if isinstance(sender, dict):
        for k in ("phone", "e164"):
            v = sender.get(k)
            if isinstance(v, str):
                candidates.append(v)

    message = data.get("message")
    if isinstance(message, dict):
        for k in ("from", "from_phone"):
            v = message.get(k)
            if isinstance(v, str):
                candidates.append(v)

    for c in candidates:
        if _looks_like_e164(c):
            return c.strip()
    return None


def lookup_tier(phone: Optional[str], conn=None) -> str:
    """
    Resolve sender_id → tier via platform_identity table.

    Hard-pin env vars (DREW_PHONE, DREW_TELEGRAM_USER_ID) checked FIRST so
    Drew is never locked out even on a DB outage.

    `conn` is accepted but ignored — platform_identity helper manages its
    own connections. Kept for back-compat with existing test signatures.
    """
    if not phone:
        return DEFAULT_TIER

    # Hard-pin Drew env var (DB-independent, fail-open for the owner)
    drew_phone = _drew_phone()
    if drew_phone and phone == drew_phone:
        return "admin"

    # platform_identity lookup (the new canonical path)
    try:
        from app.proxy.identity import lookup_identity, autoadd_unknown
    except Exception:
        return DEFAULT_TIER

    ident = lookup_identity(phone)
    tier = ident.get("tier") or DEFAULT_TIER
    if not ident.get("user_id"):
        # Unknown sender — best-effort add so we remember next time
        autoadd_unknown(phone, platform="unknown")
    return tier if tier in VALID_TIERS else DEFAULT_TIER


def resolve(payload: Union[bytes, bytearray, str, dict, None]) -> tuple[Optional[str], str]:
    """
    Top-level entry: payload → (phone, tier). On any failure returns
    (phone_or_None, 'stranger'). Fails CLOSED — never escalates on error.
    """
    phone = extract_sender_phone(payload)
    if not phone:
        return None, DEFAULT_TIER
    try:
        return phone, lookup_tier(phone)
    except Exception as e:
        sys.stderr.write(f"[tier_gate] lookup failed for {phone}: {e}\n")
        return phone, DEFAULT_TIER


def header_for(tier: Optional[str]) -> dict:
    """Sanitized header dict for the forwarded request."""
    safe = tier if tier in VALID_TIERS else DEFAULT_TIER
    return {"X-Save-Tier": safe}
