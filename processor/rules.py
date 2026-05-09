"""
processor/rules.py — Rule-based extraction. Zero AI. Zero cost.

Extraction order (each layer fills gaps left by the previous):
  Layer 1: pdfplumber table parsing → structured rows directly
  Layer 2: Regex patterns on raw text → known field formats
  Layer 3: Heuristic scoring → vendor name, line items from text blocks

Returns RulesResult with a confidence score 0.0–1.0.
If confidence >= RULES_CONFIDENCE_THRESHOLD, pipeline skips AI entirely.
If confidence < threshold, pipeline passes partial result to AI with
only the MISSING fields in the prompt — not the full document again.

WHY this order matters:
  An Indian GST invoice has a GSTIN field that is ALWAYS 15 chars, 
  alphanumeric, specific pattern. Regex gets it 100% of the time.
  Sending that to Claude costs tokens for something a regex handles 
  in 0.001 seconds.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("docai.processor.rules")

# If rule-based extraction achieves this confidence, skip AI entirely
RULES_CONFIDENCE_THRESHOLD = 0.70


@dataclass
class RulesResult:
    """Output of rule-based extraction pass."""
    data: dict[str, Any]        # extracted fields (populated ones only)
    confidence: float           # 0.0–1.0
    missing_fields: list[str]   # fields we couldn't extract — passed to AI
    method: str = "rules"
    tables_found: int = 0       # number of tables extracted from PDF


# ── Compiled patterns — compiled once at module load ────────────────────────────

# GSTIN: 2-digit state code + 10-char PAN + 1 entity + 1 checksum + Z
_GSTIN = re.compile(r'\b(\d{2}[A-Z]{5}\d{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1})\b')

# Invoice number — covers most Indian invoice formats
_INV_NO = re.compile(
    r'(?:invoice\s*(?:no|number|#|num)?\.?\s*[:–\-]?\s*)([A-Z0-9][A-Z0-9/\-]{2,25})',
    re.IGNORECASE,
)

# Date — covers DD/MM/YYYY, DD-MM-YYYY, DD Mon YYYY, YYYY-MM-DD
_DATE = re.compile(
    r'\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}|\d{4}[\/\-]\d{2}[\/\-]\d{2})\b',
    re.IGNORECASE,
)

# Due date — same format but preceded by "due" or "payment"
_DUE_DATE = re.compile(
    r'(?:due\s*(?:date|on)?|payment\s*due|pay\s*by)\s*[:–\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})',
    re.IGNORECASE,
)

# Currency amounts — ₹, Rs., INR, USD, EUR prefix or suffix
_AMOUNT = re.compile(
    r'(?:₹|Rs\.?|INR|USD|\$|EUR|€)\s*([\d,]+(?:\.\d{1,2})?)|'
    r'([\d,]+(?:\.\d{1,2})?)\s*(?:₹|Rs\.?|INR)',
    re.IGNORECASE,
)

# Total — labelled total lines
_TOTAL = re.compile(
    r'(?:grand\s*total|total\s*amount|total\s*payable|net\s*payable|amount\s*due|total\s*invoice)\s*[:–\-]?\s*(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d{1,2})?)',
    re.IGNORECASE,
)

# Subtotal
_SUBTOTAL = re.compile(
    r'(?:sub\s*total|subtotal|taxable\s*amount|taxable\s*value)\s*[:–\-]?\s*(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d{1,2})?)',
    re.IGNORECASE,
)

# GST — CGST + SGST or IGST
_GST = re.compile(
    r'(?:CGST|SGST|IGST|GST|tax)\s*[@\d\.%]*\s*[:–\-]?\s*(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d{1,2})?)',
    re.IGNORECASE,
)

# Currency code
_CURRENCY = re.compile(r'\b(INR|USD|EUR|GBP|AED|SGD)\b')

# PO / Purchase Order number
_PO = re.compile(
    r'(?:P\.?O\.?\s*(?:no|number|#)?\.?\s*[:–\-]?\s*)([A-Z0-9][A-Z0-9/\-]{2,20})',
    re.IGNORECASE,
)

# Bank account details
_BANK_ACC = re.compile(r'\b(\d{9,18})\b')
_IFSC = re.compile(r'\b([A-Z]{4}0[A-Z0-9]{6})\b')

# HSN / SAC code
_HSN = re.compile(r'(?:HSN|SAC|HSN/SAC)\s*(?:code|no\.?)?\s*[:–\-]?\s*(\d{4,8})', re.IGNORECASE)


def extract_from_text(text: str) -> RulesResult:
    """
    Run all rule-based extractors on raw text.
    Returns RulesResult with confidence score.
    """
    data: dict[str, Any] = {}

    # ── Invoice number ──────────────────────────────────────────────────────────
    m = _INV_NO.search(text)
    if m:
        data["invoice_number"] = m.group(1).strip()

    # ── Dates ───────────────────────────────────────────────────────────────────
    due_m = _DUE_DATE.search(text)
    if due_m:
        data["due_date"] = due_m.group(1).strip()

    all_dates = _DATE.findall(text)
    if all_dates:
        # First date found = invoice date (usually at top of document)
        data["invoice_date"] = all_dates[0].strip()
        if len(all_dates) > 1 and "due_date" not in data:
            data["due_date"] = all_dates[-1].strip()

    # ── GSTIN ───────────────────────────────────────────────────────────────────
    gstins = _GSTIN.findall(text)
    if gstins:
        data["vendor_gstin"] = gstins[0]
        if len(gstins) > 1:
            data["buyer_gstin"] = gstins[1]

    # ── Amounts ─────────────────────────────────────────────────────────────────
    total_m = _TOTAL.search(text)
    if total_m:
        data["total_amount"] = _parse_amount(total_m.group(1))

    sub_m = _SUBTOTAL.search(text)
    if sub_m:
        data["subtotal"] = _parse_amount(sub_m.group(1))

    # GST amounts — sum all GST lines
    gst_amounts = [_parse_amount(m) for m in _GST.findall(text) if _parse_amount(m) > 0]
    if gst_amounts:
        data["tax_amount"] = round(sum(gst_amounts), 2)

    # ── Currency ────────────────────────────────────────────────────────────────
    curr_m = _CURRENCY.search(text)
    data["currency"] = curr_m.group(1) if curr_m else "INR"

    # ── Purchase Order ──────────────────────────────────────────────────────────
    po_m = _PO.search(text)
    if po_m:
        data["po_number"] = po_m.group(1).strip()

    # ── HSN/SAC ─────────────────────────────────────────────────────────────────
    hsn_m = _HSN.search(text)
    if hsn_m:
        data["hsn_code"] = hsn_m.group(1)

    # ── Bank details ────────────────────────────────────────────────────────────
    ifsc_m = _IFSC.search(text)
    if ifsc_m:
        data["bank_ifsc"] = ifsc_m.group(1)

    # ── Vendor name heuristic ───────────────────────────────────────────────────
    vendor = _extract_vendor_name(text)
    if vendor:
        data["vendor_name"] = vendor

    # ── Buyer name heuristic ────────────────────────────────────────────────────
    buyer = _extract_buyer_name(text)
    if buyer:
        data["buyer_name"] = buyer

    # ── Confidence score ────────────────────────────────────────────────────────
    key_fields = ["invoice_number", "invoice_date", "vendor_name", "total_amount", "tax_amount"]
    bonus_fields = ["vendor_gstin", "buyer_name", "subtotal", "due_date", "currency"]

    key_score = sum(1 for f in key_fields if f in data) / len(key_fields)
    bonus_score = sum(1 for f in bonus_fields if f in data) / len(bonus_fields)
    confidence = round(key_score * 0.75 + bonus_score * 0.25, 3)

    # Missing critical fields
    all_fields = ["invoice_number", "invoice_date", "due_date", "vendor_name",
                  "buyer_name", "total_amount", "subtotal", "tax_amount",
                  "currency", "vendor_gstin", "buyer_gstin", "line_items"]
    missing = [f for f in all_fields if f not in data]

    logger.info(
        "Rules extraction: %d fields found, confidence=%.0f%%, missing=%s",
        len(data), confidence * 100, missing,
    )
    return RulesResult(data=data, confidence=confidence, missing_fields=missing)


def extract_from_tables(tables: list[list[list[str]]]) -> dict[str, Any]:
    """
    Extract line items and amounts from pdfplumber table output.
    
    pdfplumber returns tables as list of rows, each row is list of cells.
    This is called separately from text extraction — tables often contain
    the most structured data in an invoice.
    """
    data: dict[str, Any] = {}
    line_items = []

    for table in tables:
        if not table or len(table) < 2:
            continue

        # Detect header row
        header = [str(c).lower().strip() if c else "" for c in table[0]]

        desc_col = _find_col(header, ["description", "particulars", "item", "product", "service", "goods"])
        qty_col  = _find_col(header, ["qty", "quantity", "units", "nos", "pcs"])
        rate_col = _find_col(header, ["rate", "unit price", "price", "unit rate"])
        amt_col  = _find_col(header, ["amount", "total", "value", "amt"])
        hsn_col  = _find_col(header, ["hsn", "sac", "hsn/sac"])
        tax_col  = _find_col(header, ["gst", "tax", "igst", "cgst", "sgst"])

        for row in table[1:]:
            if not row or all(not c for c in row):
                continue

            item: dict[str, Any] = {}

            if desc_col is not None and desc_col < len(row) and row[desc_col]:
                item["description"] = str(row[desc_col]).strip()
            if qty_col is not None and qty_col < len(row):
                item["quantity"] = _parse_amount(str(row[qty_col] or ""))
            if rate_col is not None and rate_col < len(row):
                item["unit_price"] = _parse_amount(str(row[rate_col] or ""))
            if amt_col is not None and amt_col < len(row):
                item["amount"] = _parse_amount(str(row[amt_col] or ""))
            if hsn_col is not None and hsn_col < len(row) and row[hsn_col]:
                item["hsn"] = str(row[hsn_col]).strip()

            # Only add if it has at least a description or amount
            if item.get("description") or item.get("amount"):
                # Skip total rows
                desc = item.get("description", "").lower()
                if any(t in desc for t in ["total", "subtotal", "grand", "tax", "gst", "cgst", "sgst"]):
                    # Extract total from this row instead
                    if item.get("amount") and not data.get("total_amount"):
                        if any(t in desc for t in ["grand total", "total amount", "net payable"]):
                            data["total_amount"] = item["amount"]
                    continue
                line_items.append(item)

    if line_items:
        data["line_items"] = line_items
        logger.info("Extracted %d line items from tables", len(line_items))

    return data


# ── Private helpers ────────────────────────────────────────────────────────────

def _parse_amount(raw: str) -> float:
    """Convert '1,97,355.00' or '197355' to float."""
    if not raw:
        return 0.0
    cleaned = re.sub(r"[₹Rs.,\s]", "", str(raw).strip(), flags=re.IGNORECASE)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _find_col(header: list[str], keywords: list[str]) -> int | None:
    """Find column index by matching any keyword in header."""
    for i, cell in enumerate(header):
        if any(kw in cell for kw in keywords):
            return i
    return None


def _extract_vendor_name(text: str) -> str | None:
    """
    Heuristic: vendor name is usually in the first 5 lines of an invoice.
    Look for lines that end with Pvt Ltd, Limited, LLP, Inc, Corp, etc.
    """
    company_suffixes = re.compile(
        r'((?:[A-Z][A-Za-z\s&\.\-]{2,50})'
        r'(?:Pvt\.?\s*Ltd\.?|Private\s*Limited|Limited|LLP|Inc\.?|Corp\.?|'
        r'Co\.?|Corporation|Logistics|Freight|Shipping|Transport|Express|'
        r'International|Exports?|Imports?|Industries|Enterprises|Solutions|'
        r'Services|Group))',
        re.IGNORECASE,
    )
    # Check first 500 characters — vendor name is always at the top
    top = text[:500]
    matches = company_suffixes.findall(top)
    if matches:
        # Return longest match (most complete company name)
        return max(matches, key=len).strip()
    return None


def _extract_buyer_name(text: str) -> str | None:
    """
    Buyer name usually follows 'Bill To:', 'To:', 'Consignee:', 'Buyer:'.
    """
    bill_to = re.compile(
        r'(?:bill\s*to|ship\s*to|consignee|buyer|to)\s*[:–\-]\s*\n?\s*'
        r'((?:[A-Z][A-Za-z\s&\.\-]{2,50})'
        r'(?:Pvt\.?\s*Ltd\.?|Private\s*Limited|Limited|LLP|Inc\.?|Corp\.?|'
        r'Co\.?|Corporation|Logistics|Industries|Enterprises|Solutions|Services|Group))',
        re.IGNORECASE,
    )
    m = bill_to.search(text)
    return m.group(1).strip() if m else None
