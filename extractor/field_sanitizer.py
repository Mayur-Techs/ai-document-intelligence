"""
extractor/field_sanitizer.py
─────────────────────────────
Post-processing layer. Cleans and validates every field that comes out of
Gemini so corrupt/garbled data never reaches the database.

No external imports — pure Python stdlib only.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# ─────────────────────────────────────────────────────────────
#  Compiled patterns (built once at import time)
# ─────────────────────────────────────────────────────────────

GSTIN_RE = re.compile(r"^[0-3][0-9][A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
HTML_TAG = re.compile(r"<[^>]+>")
CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

LABEL_NOISE = re.compile(
    r"^(invoice[\s\-]*(details?|no\.?)?|bill[\s\-]*to|from|issued[\s\-]*by|"
    r"vendor|buyer|supplier|client|party|consignee|shipper|to)\s*[\n\r:,]?\s*",
    re.IGNORECASE,
)

METADATA_LABELS = re.compile(
    r"^(invoice\s*no|b/?l\s*no|bill\s*of\s*lading|vessel|voyage|"
    r"port\s*of\s*(loading|discharge|origin|destination)|container\s*no|"
    r"seal\s*no|hs\s*code|gross\s*weight|net\s*weight|cbm|"
    r"country\s*of\s*(origin|destination)|etd|eta|date\s*of\s*shipment|"
    r"bank\s*name|account\s*(name|no|number)|ifsc|branch|swift|micr|"
    r"flight\s*no|awb|reference\s*no|po\s*no|booking\s*no)\s*[:\-]?$",
    re.IGNORECASE,
)

BANK_IN_DESC = re.compile(
    r"(bank|a/?c\s*no|account\s*no|ifsc|swift|micr|branch)",
    re.IGNORECASE,
)

MONTH_NUM = {
    "01": "Jan",
    "02": "Feb",
    "03": "Mar",
    "04": "Apr",
    "05": "May",
    "06": "Jun",
    "07": "Jul",
    "08": "Aug",
    "09": "Sep",
    "10": "Oct",
    "11": "Nov",
    "12": "Dec",
}


# ─────────────────────────────────────────────────────────────
#  Individual field cleaners
# ─────────────────────────────────────────────────────────────


def clean_amount(raw: Any) -> float | None:
    if raw is None:
        return None
    s = HTML_TAG.sub("", str(raw))
    s = re.sub(r"[₹$€£]|Rs\.?|USD|INR|EUR", "", s, flags=re.IGNORECASE)
    s = s.replace(",", "").replace("/-", "").strip()
    try:
        val = float(s)
        return round(val, 2) if val != 0.0 else None
    except ValueError:
        return None


def clean_date(raw: Any) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    for fmt in (
        "%d %B %Y",
        "%d %b %Y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d/%m/%y",
        "%d-%m-%y",
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%-d %b %Y")
        except ValueError:
            continue
    # Regex fallback
    m = re.search(r"(\d{1,2})[\s./\-](\w+)[\s./\-](\d{2,4})", s)
    if m:
        day, mon_raw, year = m.group(1), m.group(2).lower()[:3], m.group(3)
        month = {
            "jan": "Jan",
            "feb": "Feb",
            "mar": "Mar",
            "apr": "Apr",
            "may": "May",
            "jun": "Jun",
            "jul": "Jul",
            "aug": "Aug",
            "sep": "Sep",
            "oct": "Oct",
            "nov": "Nov",
            "dec": "Dec",
        }.get(mon_raw)
        if month:
            year = "20" + year if len(year) == 2 else year
            return f"{int(day)} {month} {year}"
    return s


def clean_gstin(raw: Any) -> str | None:
    if not raw:
        return None
    s = str(raw).strip().upper().replace(" ", "")
    return s if GSTIN_RE.match(s) else None


def clean_ifsc(raw: Any) -> str | None:
    if not raw:
        return None
    s = str(raw).strip().upper().replace(" ", "")
    return s if IFSC_RE.match(s) else None


def clean_name(raw: Any) -> str | None:
    if not raw:
        return None
    s = HTML_TAG.sub("", str(raw))
    s = LABEL_NOISE.sub("", s)
    lines = [line.strip() for line in s.splitlines() if line.strip()]
    if not lines:
        return None
    s = lines[0] if not LABEL_NOISE.match(lines[0]) else (lines[1] if len(lines) > 1 else lines[0])
    s = CTRL_CHARS.sub(" ", s).strip(" ,:;-")
    return s if len(s) > 2 else None


def clean_invoice_number(raw: Any) -> str | None:
    if not raw:
        return None
    s = HTML_TAG.sub("", str(raw))
    s = CTRL_CHARS.sub("", s).strip()
    return s if len(s) >= 4 and re.search(r"\d", s) else None


def clean_string(raw: Any) -> str | None:
    if raw is None:
        return None
    s = HTML_TAG.sub("", str(raw))
    return CTRL_CHARS.sub(" ", s).strip() or None


# ─────────────────────────────────────────────────────────────
#  Line item cleaner
# ─────────────────────────────────────────────────────────────


def _is_metadata(item: dict) -> bool:
    desc = item.get("description") or ""
    if METADATA_LABELS.match(desc.strip()):
        return True
    if BANK_IN_DESC.search(desc) and item.get("amount") is None:
        return True
    if "\n" in desc and item.get("amount") is None:
        return True
    return bool(len(desc) > 200 and item.get("amount") is None)


def clean_line_item(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None

    desc = clean_string(raw.get("description"))
    amount = clean_amount(raw.get("amount"))

    # No description AND no amount → skip
    if not desc and amount is None:
        return None

    # Has description but it's metadata → skip
    if desc and _is_metadata({"description": desc, "amount": amount}):
        return None

    # No description but has amount → orphan row → skip
    if not desc:
        return None

    # Validate HSN (must be 4-8 digits)
    hsn = clean_string(raw.get("hsn"))
    if hsn:
        digits = re.sub(r"[^0-9]", "", hsn)
        hsn = digits if 4 <= len(digits) <= 8 else None

    # Sanity-check quantity (> 999999 is almost certainly OCR corruption)
    qty = raw.get("quantity")
    if qty is not None:
        try:
            q = float(str(qty).replace(",", ""))
            qty = q if q <= 999_999 else None
        except (ValueError, TypeError):
            qty = None

    return {
        "description": desc,
        "hsn": hsn,
        "quantity": qty,
        "unit": clean_string(raw.get("unit")),
        "unit_price": clean_amount(raw.get("unit_price")),
        "amount": amount,
    }


# ─────────────────────────────────────────────────────────────
#  Main sanitizer — called after every AI extraction
# ─────────────────────────────────────────────────────────────


def sanitize(raw: dict) -> dict:
    """Clean and validate every field in the raw AI output dict."""

    vendor_name = clean_name(raw.get("vendor_name"))
    buyer_name = clean_name(raw.get("buyer_name"))
    vendor_gstin = clean_gstin(raw.get("vendor_gstin"))
    buyer_gstin = clean_gstin(raw.get("buyer_gstin"))
    invoice_num = clean_invoice_number(raw.get("invoice_number"))
    invoice_date = clean_date(raw.get("invoice_date"))
    due_date = clean_date(raw.get("due_date"))
    currency = clean_string(raw.get("currency")) or "INR"
    bank_ifsc = clean_ifsc(raw.get("bank_ifsc"))
    bank_acct = clean_string(raw.get("bank_account_number"))
    bank_name = clean_name(raw.get("bank_name"))

    subtotal = clean_amount(raw.get("subtotal"))
    tax_amount = clean_amount(raw.get("tax_amount"))
    total_amount = clean_amount(raw.get("total_amount"))

    # Clean line items — drops metadata rows automatically
    line_items = [
        item
        for item in (clean_line_item(i) for i in (raw.get("line_items") or []))
        if item is not None
    ]

    # Amount reconciliation
    if total_amount is None and subtotal and tax_amount:
        total_amount = round(subtotal + tax_amount, 2)

    if subtotal is None and line_items:
        s = sum(i["amount"] for i in line_items if i.get("amount"))
        if s > 0:
            subtotal = round(s, 2)

    if total_amount is None and subtotal:
        total_amount = subtotal

    # Confidence re-scoring with penalty for missing critical fields
    critical = {
        "vendor_name": vendor_name,
        "invoice_number": invoice_num,
        "invoice_date": invoice_date,
        "total_amount": total_amount,
    }
    secondary = {
        "buyer_name": buyer_name,
        "due_date": due_date,
        "vendor_gstin": vendor_gstin,
    }
    ai_conf = float(raw.get("confidence") or 0.5)
    penalty = (
        sum(1 for v in critical.values() if v is None) * 0.10
        + sum(1 for v in secondary.values() if v is None) * 0.04
    )
    adjusted_conf = max(0.05, round(ai_conf - penalty, 2))

    return {
        "vendor_name": vendor_name,
        "vendor_gstin": vendor_gstin,
        "buyer_name": buyer_name,
        "buyer_gstin": buyer_gstin,
        "invoice_number": invoice_num,
        "invoice_date": invoice_date,
        "due_date": due_date,
        "currency": currency,
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
        "bank_ifsc": bank_ifsc,
        "bank_account_number": bank_acct,
        "bank_name": bank_name,
        "line_items": line_items,
        "ai_confidence": adjusted_conf,
        "confidence_reason": clean_string(raw.get("confidence_reason", "")),
    }


def merge_best(primary: dict, fallback: dict) -> dict:
    """
    Merge two sanitized results.
    Primary wins on ties. Fallback fills any null fields.
    Line items: whichever has more clean rows wins.
    """
    merged = dict(primary)
    for key, val in fallback.items():
        if key == "line_items":
            if len(fallback["line_items"]) > len(primary.get("line_items", [])):
                merged["line_items"] = fallback["line_items"]
        elif key == "ai_confidence":
            merged["ai_confidence"] = max(
                primary.get("ai_confidence", 0),
                fallback.get("ai_confidence", 0),
            )
        elif merged.get(key) is None and val is not None:
            merged[key] = val
    return merged
