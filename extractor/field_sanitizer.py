"""
field_sanitizer.py
──────────────────
Post-processing layer. Cleans and validates every field that comes out of
the AI so corrupt/garbled data never reaches the database.
"""

import re
import json
from typing import Any, Optional
from datetime import datetime

# ──────────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────────

GSTIN_PATTERN = re.compile(
    r'^[0-3][0-9][A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'
)

IFSC_PATTERN = re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$')

MONTH_MAP = {
    'jan': 'Jan', 'feb': 'Feb', 'mar': 'Mar', 'apr': 'Apr',
    'may': 'May', 'jun': 'Jun', 'jul': 'Jul', 'aug': 'Aug',
    'sep': 'Sep', 'oct': 'Oct', 'nov': 'Nov', 'dec': 'Dec',
    '01': 'Jan', '02': 'Feb', '03': 'Mar', '04': 'Apr',
    '05': 'May', '06': 'Jun', '07': 'Jul', '08': 'Aug',
    '09': 'Sep', '10': 'Oct', '11': 'Nov', '12': 'Dec',
}

# Noise patterns that pollute vendor/buyer names
LABEL_NOISE = re.compile(
    r'^(invoice\s*(details?)?|bill\s*to|from|issued\s*by|vendor|buyer|'
    r'supplier|client|party|consignee|shipper|to\s*:?)\s*[\n\r:,]?\s*',
    re.IGNORECASE
)

HTML_TAG = re.compile(r'<[^>]+>')
CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]')


# ──────────────────────────────────────────────
#  Individual field cleaners
# ──────────────────────────────────────────────

def clean_amount(raw: Any) -> Optional[float]:
    """Convert any amount representation to a clean float or None."""
    if raw is None:
        return None
    s = str(raw)
    s = HTML_TAG.sub('', s)                    # strip HTML tags
    s = re.sub(r'[₹$€£]|Rs\.?|USD|INR|EUR', '', s, flags=re.IGNORECASE)
    s = s.replace(',', '').replace('/-', '').strip()
    try:
        val = float(s)
        return round(val, 2) if val != 0.0 else None
    except ValueError:
        return None


def clean_date(raw: Any) -> Optional[str]:
    """Normalise any date string to 'DD MMM YYYY'."""
    if not raw:
        return None
    s = str(raw).strip()

    formats_to_try = [
        '%d %B %Y',     # 15 April 2026
        '%d %b %Y',     # 15 Apr 2026
        '%d/%m/%Y',     # 15/04/2026
        '%d-%m-%Y',     # 15-04-2026
        '%Y-%m-%d',     # 2026-04-15
        '%d.%m.%Y',     # 15.04.2026
        '%B %d, %Y',    # April 15, 2026
        '%b %d, %Y',    # Apr 15, 2026
        '%d/%m/%y',     # 15/04/26
        '%d-%m-%y',     # 15-04-26
    ]
    for fmt in formats_to_try:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime('%-d %b %Y')   # e.g. "15 Apr 2026"
        except ValueError:
            continue

    # Last resort: regex extraction
    m = re.search(r'(\d{1,2})[\s./\-](\w+)[\s./\-](\d{2,4})', s)
    if m:
        day, month_raw, year = m.group(1), m.group(2).lower()[:3], m.group(3)
        month = MONTH_MAP.get(month_raw)
        if month and len(year) == 2:
            year = '20' + year
        if month:
            return f"{int(day)} {month} {year}"

    return s  # Return as-is if all parsing fails


def clean_gstin(raw: Any) -> Optional[str]:
    """Validate and return GSTIN or None if invalid."""
    if not raw:
        return None
    s = str(raw).strip().upper().replace(' ', '')
    if GSTIN_PATTERN.match(s):
        return s
    return None


def clean_ifsc(raw: Any) -> Optional[str]:
    """Validate and return IFSC code or None."""
    if not raw:
        return None
    s = str(raw).strip().upper().replace(' ', '')
    if IFSC_PATTERN.match(s):
        return s
    return None


def clean_name(raw: Any) -> Optional[str]:
    """Strip label noise and control chars from company names."""
    if not raw:
        return None
    s = str(raw)
    s = HTML_TAG.sub('', s)
    s = LABEL_NOISE.sub('', s)
    # Take only the first meaningful line if multi-line garbage present
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    if not lines:
        return None
    # If first line is a noise label, skip it
    s = lines[0] if not LABEL_NOISE.match(lines[0]) else (lines[1] if len(lines) > 1 else lines[0])
    s = CONTROL_CHARS.sub(' ', s).strip(' ,:;-')
    return s if len(s) > 2 else None


def clean_invoice_number(raw: Any) -> Optional[str]:
    """Clean invoice number — remove obvious noise."""
    if not raw:
        return None
    s = str(raw).strip()
    s = HTML_TAG.sub('', s)
    s = CONTROL_CHARS.sub('', s).strip()
    # Must have at least 4 chars and contain a digit
    if len(s) < 4 or not re.search(r'\d', s):
        return None
    return s


def clean_string(raw: Any) -> Optional[str]:
    """Generic string cleaner."""
    if raw is None:
        return None
    s = HTML_TAG.sub('', str(raw))
    s = CONTROL_CHARS.sub(' ', s).strip()
    return s if s else None


# ──────────────────────────────────────────────
#  Line item cleaner
# ──────────────────────────────────────────────

# Known metadata labels that should never be line items
METADATA_LABELS = re.compile(
    r'^(invoice\s*no|b/?l\s*no|bl\s*no|bill\s*of\s*lading|vessel|voyage|'
    r'port\s*of\s*(loading|discharge|origin|destination)|container\s*no|'
    r'seal\s*no|container\s*type|hs\s*code|gross\s*weight|net\s*weight|'
    r'cbm|country\s*of\s*(origin|destination)|etd|eta|date\s*of\s*shipment|'
    r'bank\s*name|account\s*(name|no|number)|ifsc|branch|swift|micr|'
    r'flight\s*no|awb|reference\s*no|po\s*no|booking\s*no|'
    r'description\s*of\s*goods|shipper|consignee\s*ref)\s*[:\-]?$',
    re.IGNORECASE
)

BANK_DETAIL_LABEL = re.compile(
    r'(bank|a/?c|account|ifsc|swift|micr|branch|hdfc|sbi|icici|axis|'
    r'kotak|yes\s*bank|baroda|union|canara)',
    re.IGNORECASE
)


def is_metadata_item(item: dict) -> bool:
    """Return True if this line item is actually metadata, not a charge."""
    desc = item.get('description', '') or ''
    # Pure label with no amount = metadata
    if METADATA_LABELS.match(desc.strip()):
        return True
    # Bank-related text = metadata
    if BANK_DETAIL_LABEL.search(desc) and item.get('amount') is None:
        return True
    # Contains newlines suggesting it's a header block dump
    if '\n' in desc and item.get('amount') is None:
        return True
    # Very long description with no amount = likely a metadata dump
    if len(desc) > 200 and item.get('amount') is None:
        return True
    return False


def clean_line_item(raw: dict) -> Optional[dict]:
    """Clean a single line item. Return None if it's actually metadata."""
    if not isinstance(raw, dict):
        return None

    desc = clean_string(raw.get('description'))
    amount = clean_amount(raw.get('amount'))
    unit_price = clean_amount(raw.get('unit_price'))
    quantity = raw.get('quantity')
    hsn = clean_string(raw.get('hsn'))

    # Clean garbled HSN (must be 4–8 digits only)
    if hsn:
        hsn_digits = re.sub(r'[^0-9]', '', hsn)
        hsn = hsn_digits if 4 <= len(hsn_digits) <= 8 else None

    # Clean garbled quantity (sanity check: > 1,000,000 is almost certainly wrong
    # for freight invoices — flag as None)
    if quantity is not None:
        try:
            q = float(str(quantity).replace(',', ''))
            quantity = q if q <= 999999 else None
        except (ValueError, TypeError):
            quantity = None

    # Need at least a description to be a valid line item
    if not desc:
        # No description but has amount → orphan amount row, skip it
        return None

    item = {
        'description': desc,
        'hsn': hsn,
        'quantity': quantity,
        'unit': clean_string(raw.get('unit')),
        'unit_price': unit_price,
        'amount': amount,
    }

    if is_metadata_item(item):
        return None

    # If no amount at all, still keep it (partial extraction) but mark clearly
    return item


# ──────────────────────────────────────────────
#  Main sanitizer
# ──────────────────────────────────────────────

def sanitize(raw: dict) -> dict:
    """
    Take the raw AI output dict and return a fully sanitized, validated dict.
    This is the single function called by the pipeline after every AI extraction.
    """
    # Core fields
    vendor_name   = clean_name(raw.get('vendor_name'))
    buyer_name    = clean_name(raw.get('buyer_name'))
    vendor_gstin  = clean_gstin(raw.get('vendor_gstin'))
    buyer_gstin   = clean_gstin(raw.get('buyer_gstin'))
    invoice_num   = clean_invoice_number(raw.get('invoice_number'))
    invoice_date  = clean_date(raw.get('invoice_date'))
    due_date      = clean_date(raw.get('due_date'))
    currency      = clean_string(raw.get('currency')) or 'INR'
    bank_ifsc     = clean_ifsc(raw.get('bank_ifsc'))
    bank_account  = clean_string(raw.get('bank_account_number'))
    bank_name     = clean_name(raw.get('bank_name'))

    subtotal      = clean_amount(raw.get('subtotal'))
    tax_amount    = clean_amount(raw.get('tax_amount'))
    total_amount  = clean_amount(raw.get('total_amount'))

    # Line items — filter metadata, clean each item
    raw_items = raw.get('line_items') or []
    line_items = []
    for item in raw_items:
        cleaned = clean_line_item(item)
        if cleaned is not None:
            line_items.append(cleaned)

    # ── Amount reconciliation ──────────────────
    # If total missing but subtotal + tax present, compute it
    if total_amount is None and subtotal and tax_amount:
        total_amount = round(subtotal + tax_amount, 2)

    # If total missing but line items present, sum them as subtotal
    if subtotal is None and line_items:
        computed = sum(
            item['amount'] for item in line_items
            if item.get('amount') is not None
        )
        if computed > 0:
            subtotal = round(computed, 2)

    # If still no total, use subtotal as total (no tax found)
    if total_amount is None and subtotal:
        total_amount = subtotal

    # ── Confidence re-scoring ──────────────────
    critical_fields = {
        'vendor_name': vendor_name,
        'invoice_number': invoice_num,
        'invoice_date': invoice_date,
        'total_amount': total_amount,
    }
    secondary_fields = {
        'buyer_name': buyer_name,
        'due_date': due_date,
        'vendor_gstin': vendor_gstin,
    }

    missing_critical = sum(1 for v in critical_fields.values() if v is None)
    missing_secondary = sum(1 for v in secondary_fields.values() if v is None)

    ai_confidence = float(raw.get('confidence', 0.5) or 0.5)

    # Penalise for missing critical fields
    penalty = missing_critical * 0.10 + missing_secondary * 0.04
    adjusted_confidence = max(0.05, round(ai_confidence - penalty, 2))

    return {
        'vendor_name':        vendor_name,
        'vendor_gstin':       vendor_gstin,
        'buyer_name':         buyer_name,
        'buyer_gstin':        buyer_gstin,
        'invoice_number':     invoice_num,
        'invoice_date':       invoice_date,
        'due_date':           due_date,
        'currency':           currency,
        'subtotal':           subtotal,
        'tax_amount':         tax_amount,
        'total_amount':       total_amount,
        'bank_ifsc':          bank_ifsc,
        'bank_account_number': bank_account,
        'bank_name':          bank_name,
        'line_items':         line_items,
        'ai_confidence':      adjusted_confidence,
        'confidence_reason':  clean_string(raw.get('confidence_reason', '')),
    }


def merge_best(primary: dict, fallback: dict) -> dict:
    """
    Merge two sanitized results. For each field, prefer whichever extraction
    actually has a value (primary wins ties).
    """
    merged = dict(primary)
    for key, val in fallback.items():
        if key == 'line_items':
            # Use whichever has more clean line items
            if len(fallback['line_items']) > len(primary.get('line_items', [])):
                merged['line_items'] = fallback['line_items']
        elif key == 'ai_confidence':
            # Keep the higher confidence
            merged['ai_confidence'] = max(
                primary.get('ai_confidence', 0),
                fallback.get('ai_confidence', 0)
            )
        elif merged.get(key) is None and val is not None:
            merged[key] = val
    return merged
