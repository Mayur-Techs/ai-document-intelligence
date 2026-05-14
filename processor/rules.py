"""
processor/rules.py — Production-grade rule-based extraction. Target: 95%+.

ALL 4 BUGS FIXED:
  1. _parse_amount: Indian comma format 2,29,422 → 229422.0 (not 2.0)
  2. _PO: requires explicit "P.O. No:" or "Purchase Order:" label + digit in value
  3. vendor_name: label-anchored (Supplier/From/Seller), heuristic only as fallback
  4. Line items: summary/tax/total rows filtered by keyword blacklist
  5. buyer_name ≠ vendor_name: cross-check before storing
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("docai.processor.rules")

RULES_CONFIDENCE_THRESHOLD = 0.70

_SUMMARY_KEYWORDS = {
    "subtotal","sub total","sub-total","taxable value","taxable amount",
    "cgst","sgst","igst","gst","cess","tds","tax",
    "grand total","total amount","net payable","amount payable",
    "total payable","net amount","invoice total","balance due",
    "exchange rate","forex","conversion","usd rate","inr rate",
    "round off","rounding","discount","service charge","handling",
}

_COMPANY_SUFFIX = (
    r'(?:Pvt\.?\s*Ltd\.?|Private\s+Limited|Limited|LLP|LLC|Inc\.?|Corp\.?|'
    r'Corporation|Logistics|Freight|Shipping|Transport(?:ation)?|Express|'
    r'International|Exports?|Imports?|Industries|Enterprises|Solutions|'
    r'Services|Group|Associates|Traders?|Agency|Agencies|Forwarders?|'
    r'Carriers?|Movers?|Packers?|Couriers?|Worldwide|Global|National|'
    r'Terminal|Clearance|Customs|Cargo|Supply\s*Chain)'
)
_COMPANY_RE = re.compile(
    rf'((?:[A-Z][A-Za-z0-9\s&\.\-\'\/]{{2,60}})\s*{_COMPANY_SUFFIX})',
    re.IGNORECASE,
)

_GSTIN   = re.compile(r'\b(\d{2}[A-Z]{5}\d{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1})\b')
_IFSC    = re.compile(r'\b([A-Z]{4}0[A-Z0-9]{6})\b')

_INV_NO  = re.compile(
    r'(?:invoice\s*(?:no|number|#|num|ref)\.?\s*[:–\-]\s*)'
    r'([A-Z0-9][A-Z0-9/\-]{3,30})',
    re.IGNORECASE,
)

_DATE    = re.compile(
    r'\b('
    r'\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}'
    r'|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}'
    r'|\d{4}[\/\-]\d{2}[\/\-]\d{2}'
    r'|\d{1,2}\s+(?:January|February|March|April|May|June|July|August|'
    r'September|October|November|December)\s+\d{4}'
    r')\b',
    re.IGNORECASE,
)

_DUE_DATE = re.compile(
    r'(?:due\s*(?:date|on)?|payment\s*(?:due|date)|due\s*by|pay\s*by)\s*[:–\-]?\s*'
    r'(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}'
    r'|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})',
    re.IGNORECASE,
)

# Total: label-anchored ONLY — never freetext scan
_TOTAL   = re.compile(
    r'(?:grand\s*total|total\s*amount\s*due?|total\s*payable|net\s*(?:payable|amount|total)|'
    r'amount\s*(?:due|payable)|invoice\s*(?:total|value)|balance\s*(?:due|payable))\s*'
    r'[:–\-]?\s*(?:₹|Rs\.?|INR|USD|\$|EUR|GBP|AED)?\s*([\d,]+(?:\.\d{1,2})?)',
    re.IGNORECASE,
)

_SUBTOTAL = re.compile(
    r'(?:sub\s*[-\s]?total|subtotal|taxable\s*(?:amount|value)|base\s*amount)\s*'
    r'[:–\-]?\s*(?:₹|Rs\.?|INR|USD|\$|EUR|GBP)?\s*([\d,]+(?:\.\d{1,2})?)',
    re.IGNORECASE,
)

# GST: captures the RUPEE AMOUNT after the %, not the rate number
_GST_AMOUNT = re.compile(
    r'(?:CGST|SGST|IGST)\s*@?\s*[\d\.]+\s*%\s*[:–\-]?\s*(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d{1,2})?)',
    re.IGNORECASE,
)
_GST_TOTAL = re.compile(
    r'(?:total\s*gst|gst\s*(?:amount|total|payable))\s*[:–\-]?\s*(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d{1,2})?)',
    re.IGNORECASE,
)

_CURRENCY = re.compile(r'\b(INR|USD|EUR|GBP|AED|SGD|JPY|CNY)\b')

# PO: requires explicit label WITH separator, value must contain a digit
_PO = re.compile(
    r'(?:P\.?O\.?\s*(?:No|Number|#)\.?\s*[:–\-]\s*|'
    r'Purchase\s*Order\s*(?:No|Number|#)\.?\s*[:–\-]\s*)'
    r'([A-Z0-9][A-Z0-9/\-]{3,25})',
    re.IGNORECASE,
)

_HSN = re.compile(
    r'(?:HSN|SAC|HSN/SAC)\s*(?:code|no\.?)?\s*[:–\-]?\s*(\d{4,8})',
    re.IGNORECASE,
)

_VENDOR_LABELS = re.compile(
    r'(?:^|\n)\s*(?:From|Supplier|Seller|Vendor|Issued\s*by|'
    r'Billed?\s*from|Consignor|Shipper|Exporter)\s*[:–\-]',
    re.IGNORECASE | re.MULTILINE,
)
_BUYER_LABELS = re.compile(
    r'(?:^|\n)\s*(?:To|Bill\s*To|Ship\s*To|Consignee|Buyer|'
    r'Importer|Customer|Client|Recipient|Billed?\s*to)\s*[:–\-]',
    re.IGNORECASE | re.MULTILINE,
)

_SECTION_HEADERS = {
    "invoice details","shipment details","billing details","payment details",
    "bank details","terms and conditions","tax summary","charge details",
    "freight charges","road transport","air transport","sea freight",
    "invoice details\nmumbai port freight",
}


@dataclass
class RulesResult:
    data: dict[str, Any]
    confidence: float
    missing_fields: list[str] = field(default_factory=list)
    method: str = "rules"
    tables_found: int = 0


def _parse_amount(raw: str) -> float:
    """
    Parse Indian number format correctly.
      2,29,422.00  → 229422.0
      ₹1,83,195    → 183195.0
      Rs. 27,140   → 27140.0
      23,240.00    → 23240.0

    KEY FIX: strip currency symbols first, THEN remove commas,
    keep the decimal point, parse as float.
    Never read sequential digits list numbers like "1." or "2." as amounts.
    """
    if not raw:
        return 0.0
    s = str(raw).strip()
    # Remove currency symbols
    s = re.sub(r'[₹$£€¥]', '', s)
    s = re.sub(r'\bRs\.?\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\b(?:INR|USD|EUR|GBP|AED)\b', '', s, flags=re.IGNORECASE)
    # Remove commas (Indian separators), keep decimal
    s = s.replace(',', '').strip()
    # Extract first valid decimal number — must be >= 2 digits to avoid "1." "2."
    m = re.search(r'\d{2,}(?:\.\d{1,4})?', s)
    if not m:
        return 0.0
    try:
        return float(m.group())
    except ValueError:
        return 0.0


def _is_summary_row(desc: str, row: list) -> bool:
    """Return True if this is a tax/summary/total row — NOT a line item."""
    if desc:
        desc_lower = desc.lower().strip()
        if any(kw in desc_lower for kw in _SUMMARY_KEYWORDS):
            return True
    else:
        row_text = " ".join(str(c).lower() for c in row if c)
        if any(kw in row_text for kw in _SUMMARY_KEYWORDS):
            return True
    return False


def _find_col(header: list[str], keywords: list[str]) -> int | None:
    for i, cell in enumerate(header):
        if any(kw in cell for kw in keywords):
            return i
    return None


def _is_section_header(name: str) -> bool:
    return name.lower().strip() in _SECTION_HEADERS or len(name.split()) > 8


def _extract_vendor_label(text: str) -> str | None:
    """Label-anchored vendor: looks for Supplier:/From:/Seller: then company name."""
    m = _VENDOR_LABELS.search(text)
    if not m:
        return None
    after = text[m.end():m.end() + 400]
    cm = _COMPANY_RE.search(after)
    if cm:
        name = cm.group(1).strip()
        if len(name) > 5 and not _is_section_header(name):
            return name
    return None


def _extract_buyer_label(text: str) -> str | None:
    """Label-anchored buyer: looks for Bill To:/Consignee:/Buyer: then company name."""
    m = _BUYER_LABELS.search(text)
    if not m:
        return None
    after = text[m.end():m.end() + 400]
    cm = _COMPANY_RE.search(after)
    if cm:
        name = cm.group(1).strip()
        if len(name) > 5 and not _is_section_header(name):
            return name
    return None


def _extract_vendor_heuristic(text: str) -> str | None:
    """
    Fallback: first company-shaped name in document header (first 600 chars).
    Only used when no label found. Rejects section headers.
    """
    top = text[:600]
    matches = _COMPANY_RE.findall(top)
    candidates = [m for m in matches if len(m) > 6 and not _is_section_header(m)]
    if not candidates:
        return None
    return max(candidates, key=len).strip()


def extract_from_text(text: str) -> RulesResult:
    data: dict[str, Any] = {}

    # Invoice number
    m = _INV_NO.search(text)
    if m:
        val = m.group(1).strip()
        if re.search(r'[\d/\-]', val):
            data["invoice_number"] = val

    # Dates
    due_m = _DUE_DATE.search(text)
    if due_m:
        data["due_date"] = due_m.group(1).strip()

    all_dates = _DATE.findall(text)
    if all_dates:
        data["invoice_date"] = all_dates[0].strip()
        if len(all_dates) > 1 and "due_date" not in data:
            data["due_date"] = all_dates[-1].strip()

    # GSTINs
    gstins = list(dict.fromkeys(_GSTIN.findall(text)))
    if gstins:
        data["vendor_gstin"] = gstins[0]
        if len(gstins) > 1:
            data["buyer_gstin"] = gstins[1]

    # Amounts — label-anchored only
    total_m = _TOTAL.search(text)
    if total_m:
        v = _parse_amount(total_m.group(1))
        if v > 0:
            data["total_amount"] = v

    sub_m = _SUBTOTAL.search(text)
    if sub_m:
        v = _parse_amount(sub_m.group(1))
        if v > 0:
            data["subtotal"] = v

    gst_total_m = _GST_TOTAL.search(text)
    if gst_total_m:
        v = _parse_amount(gst_total_m.group(1))
        if v > 0:
            data["tax_amount"] = v
    else:
        individual = [_parse_amount(x) for x in _GST_AMOUNT.findall(text)]
        valid = [v for v in individual if v > 0]
        if valid:
            data["tax_amount"] = round(sum(valid), 2)

    # Currency
    curr_m = _CURRENCY.search(text)
    data["currency"] = curr_m.group(1) if curr_m else "INR"

    # PO — strict label + digit required
    po_m = _PO.search(text)
    if po_m:
        val = po_m.group(1).strip()
        if re.search(r'\d', val):
            data["po_number"] = val

    # HSN
    hsn_m = _HSN.search(text)
    if hsn_m:
        data["hsn_code"] = hsn_m.group(1)

    # Bank IFSC
    ifsc_m = _IFSC.search(text)
    if ifsc_m:
        data["bank_ifsc"] = ifsc_m.group(1)

    # Vendor name — label first, heuristic fallback
    vendor = _extract_vendor_label(text) or _extract_vendor_heuristic(text)
    if vendor:
        data["vendor_name"] = vendor

    # Buyer name — label only, must differ from vendor
    buyer = _extract_buyer_label(text)
    if buyer and buyer != data.get("vendor_name"):
        data["buyer_name"] = buyer

    # Confidence
    key_fields   = ["invoice_number", "invoice_date", "vendor_name", "total_amount", "tax_amount"]
    bonus_fields = ["vendor_gstin", "buyer_name", "subtotal", "due_date", "currency", "buyer_gstin"]
    key_score    = sum(1 for f in key_fields   if f in data) / len(key_fields)
    bonus_score  = sum(1 for f in bonus_fields if f in data) / len(bonus_fields)
    confidence   = round(key_score * 0.75 + bonus_score * 0.25, 3)

    all_target = ["invoice_number","invoice_date","due_date","vendor_name",
                  "buyer_name","total_amount","subtotal","tax_amount",
                  "currency","vendor_gstin","buyer_gstin","line_items"]
    missing = [f for f in all_target if f not in data]

    logger.info("Rules: confidence=%.0f%% fields=%d missing=%s",
                confidence*100, len(data), missing)
    return RulesResult(data=data, confidence=confidence, missing_fields=missing)


def extract_from_tables(tables: list[list[list[str]]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    line_items: list[dict[str, Any]] = []

    for table in tables:
        if not table or len(table) < 2:
            continue

        header = [str(c).lower().strip() if c else "" for c in table[0]]
        desc_col = _find_col(header, ["description","particulars","item","product",
                                       "service","goods","details","narration"])
        qty_col  = _find_col(header, ["qty","quantity","units","nos","pcs","count"])
        rate_col = _find_col(header, ["rate","unit price","unit rate","price"])
        amt_col  = _find_col(header, ["amount","total","value","amt","net amount"])
        hsn_col  = _find_col(header, ["hsn","sac","hsn/sac","code"])

        for row in table[1:]:
            if not row or all(not c for c in row):
                continue

            desc = ""
            if desc_col is not None and desc_col < len(row) and row[desc_col]:
                desc = str(row[desc_col]).strip()

            if _is_summary_row(desc, row):
                # Capture grand total from summary row
                desc_lower = desc.lower()
                if any(k in desc_lower for k in
                       ["grand total","total amount","net payable","total payable"]):
                    if amt_col is not None and amt_col < len(row):
                        v = _parse_amount(str(row[amt_col] or ""))
                        if v > 0 and not data.get("total_amount"):
                            data["total_amount"] = v
                continue

            item: dict[str, Any] = {}
            if desc:
                item["description"] = desc[:200]
            if qty_col is not None and qty_col < len(row):
                v = _parse_amount(str(row[qty_col] or ""))
                if v > 0:
                    item["quantity"] = v
            if rate_col is not None and rate_col < len(row):
                v = _parse_amount(str(row[rate_col] or ""))
                if v > 0:
                    item["unit_price"] = v
            if amt_col is not None and amt_col < len(row):
                v = _parse_amount(str(row[amt_col] or ""))
                if v > 0:
                    item["amount"] = v
            if hsn_col is not None and hsn_col < len(row) and row[hsn_col]:
                item["hsn"] = str(row[hsn_col]).strip()

            if item.get("description") or item.get("amount", 0) > 0:
                line_items.append(item)

    if line_items:
        data["line_items"] = line_items
        logger.info("Table extraction: %d line items", len(line_items))

    return data