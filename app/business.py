"""Business & finance tools. Everything here is computed locally (no third-party calls, no quota, instant):
VAT, pricing and margins, discounts, break-even, invoices, loans, compound growth, ROI, SaaS metrics, stock
reorder points / EOQ, volumetric shipping weight, GTIN/EAN, IBAN, EU VAT-ID format and card-number checks, UTM links
and business-day maths. Money uses Decimal and is rounded half-up to 2 places. Routes live under /v1/business."""
import html as _html
import math
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/business", tags=["business"])
D = Decimal
BIG = 1e12  # sanity cap so a typo cannot make the server build a monstrous number


def money(x) -> float:
    return float(D(str(x)).quantize(D("0.01"), rounding=ROUND_HALF_UP))


def pct(x) -> float:
    return float(D(str(x)).quantize(D("0.01"), rounding=ROUND_HALF_UP))


# Standard VAT rates in percent. Rates change, so always confirm with the tax authority before filing.
VAT_RATES = {"AT": 20, "BE": 21, "BG": 20, "HR": 25, "CY": 19, "CZ": 21, "DK": 25, "EE": 24, "FI": 25.5, "FR": 20,
             "DE": 19, "GR": 24, "HU": 27, "IE": 23, "IT": 22, "LV": 21, "LT": 21, "LU": 17, "MT": 18, "NL": 21,
             "PL": 23, "PT": 23, "RO": 21, "SK": 23, "SI": 22, "ES": 21, "SE": 25, "GB": 20, "NO": 25, "CH": 8.1}


@router.get("/vat")
def vat(amount: float = Query(gt=-BIG, lt=BIG), rate: float | None = Query(None, ge=0, le=100),
        country: str | None = Query(None, min_length=2, max_length=2), mode: Literal["add", "remove"] = "add"):
    """Add VAT to a net price (`mode=add`) or split VAT out of a gross price (`mode=remove`).
    Give `rate` in percent, or a 2-letter `country` code to use its standard rate."""
    if rate is None:
        if not country:
            raise HTTPException(400, "Give rate or country")
        rate = VAT_RATES.get(country.upper())
        if rate is None:
            raise HTTPException(404, f"No standard rate stored for {country.upper()}; pass rate=")
    a, r = D(str(amount)), D(str(rate)) / 100
    net, gross = (a, a * (1 + r)) if mode == "add" else (a / (1 + r), a)
    return {"mode": mode, "rate": rate, "country": country.upper() if country else None,
            "net": money(net), "vat": money(gross - net), "gross": money(gross),
            "note": "Standard rate only; reduced rates and reverse charge are not applied."}


@router.get("/vat-rates")
def vat_rates():
    """Standard VAT rates by country (EU, UK, NO, CH). Verify before filing: rates change."""
    return {"rates": VAT_RATES}


@router.get("/pricing")
def pricing(cost: float | None = Query(None, ge=0, lt=BIG), price: float | None = Query(None, gt=0, lt=BIG),
            margin: float | None = Query(None, ge=0, lt=100, description="gross margin %"),
            markup: float | None = Query(None, ge=0, le=100000, description="markup % on cost")):
    """Solve the price triangle. Give `cost` plus either `margin` or `markup` to get a price,
    or give `cost` and `price` to see the margin and markup you are making."""
    if cost is not None and margin is not None:
        price = float(D(str(cost)) / (1 - D(str(margin)) / 100))
    elif cost is not None and markup is not None:
        price = float(D(str(cost)) * (1 + D(str(markup)) / 100))
    elif cost is None or price is None:
        raise HTTPException(400, "Give cost+margin, cost+markup, or cost+price")
    profit = D(str(price)) - D(str(cost))
    return {"cost": money(cost), "price": money(price), "profit": money(profit),
            "margin_pct": pct(profit / D(str(price)) * 100), "markup_pct": pct(profit / D(str(cost)) * 100) if cost else None}


@router.get("/discount")
def discount(price: float = Query(gt=0, lt=BIG), percent: str = Query("10", description="one or several, comma separated, applied in order"),
             quantity: int = Query(1, ge=1, le=1_000_000)):
    """Apply one or more percentage discounts in sequence (`percent=20,10` is 28% off, not 30%)."""
    try:
        steps = [D(x) for x in percent.split(",") if x.strip()]
    except Exception:
        raise HTTPException(400, "percent must be numbers like 10 or 20,10")
    if not steps or any(s < 0 or s > 100 for s in steps):
        raise HTTPException(400, "each percent must be 0-100")
    p = D(str(price))
    final = p
    for s in steps:
        final = final * (1 - s / 100)
    off = p - final
    return {"original": money(p), "final": money(final), "saved": money(off), "effective_percent_off": pct(off / p * 100),
            "quantity": quantity, "total": money(final * quantity), "total_saved": money(off * quantity)}


@router.get("/break-even")
def break_even(fixed_costs: float = Query(ge=0, lt=BIG), price: float = Query(gt=0, lt=BIG),
               variable_cost: float = Query(ge=0, lt=BIG), target_profit: float = Query(0, ge=0, lt=BIG)):
    """Units and revenue needed to cover fixed costs (and optionally a profit target)."""
    unit = D(str(price)) - D(str(variable_cost))
    if unit <= 0:
        raise HTTPException(400, "price must be higher than variable_cost or you lose money on every sale")
    units = math.ceil((D(str(fixed_costs)) + D(str(target_profit))) / unit)
    return {"contribution_per_unit": money(unit), "contribution_margin_pct": pct(unit / D(str(price)) * 100),
            "units_needed": units, "revenue_needed": money(units * D(str(price)))}


@router.get("/roi")
def roi(invested: float = Query(gt=0, lt=BIG), returned: float = Query(ge=0, lt=BIG),
        years: float | None = Query(None, gt=0, le=200)):
    """Return on investment, plus the annualised rate if you give `years`."""
    gain = D(str(returned)) - D(str(invested))
    out = {"gain": money(gain), "roi_pct": pct(gain / D(str(invested)) * 100)}
    if years:
        out["annualised_pct"] = pct(((returned / invested) ** (1 / years) - 1) * 100)
    return out


@router.get("/loan")
def loan(principal: float = Query(gt=0, lt=BIG), annual_rate: float = Query(ge=0, le=100, description="% per year"),
         months: int = Query(ge=1, le=1200), schedule: bool = False):
    """Fixed monthly payment (annuity), total interest and optionally the full amortisation table."""
    r = annual_rate / 100 / 12
    pay = principal / months if r == 0 else principal * r / (1 - (1 + r) ** -months)
    out = {"monthly_payment": money(pay), "total_paid": money(pay * months), "total_interest": money(pay * months - principal)}
    if schedule:
        bal, rows = principal, []
        for m in range(1, months + 1):
            interest = bal * r
            princ = pay - interest
            bal = max(0.0, bal - princ)
            rows.append({"month": m, "payment": money(pay), "interest": money(interest), "principal": money(princ), "balance": money(bal)})
        out["schedule"] = rows
    return out


@router.get("/compound")
def compound(principal: float = Query(ge=0, lt=BIG), annual_rate: float = Query(ge=-100, le=1000), years: float = Query(gt=0, le=100),
             monthly_contribution: float = Query(0, ge=0, lt=BIG), compounds_per_year: int = Query(12, ge=1, le=365)):
    """Future value with optional monthly deposits."""
    n, r = compounds_per_year, annual_rate / 100
    fv = principal * (1 + r / n) ** (n * years)
    if monthly_contribution:
        i = (1 + r / n) ** (n / 12) - 1  # effective monthly rate
        months = years * 12
        fv += monthly_contribution * months if i == 0 else monthly_contribution * (((1 + i) ** months - 1) / i)
    paid = principal + monthly_contribution * years * 12
    return {"future_value": money(fv), "total_contributed": money(paid), "growth": money(fv - paid)}


@router.get("/saas-metrics")
def saas_metrics(customers: int = Query(gt=0, le=10**9), mrr: float = Query(ge=0, lt=BIG), churned: int = Query(0, ge=0, le=10**9),
                 new_customers: int = Query(0, ge=0, le=10**9), marketing_spend: float = Query(0, ge=0, lt=BIG),
                 gross_margin_pct: float = Query(80, ge=0, le=100)):
    """MRR, ARR, ARPU, churn, LTV, CAC, LTV:CAC and payback months for one period (usually a month)."""
    arpu = mrr / customers
    churn = churned / customers
    cac = marketing_spend / new_customers if new_customers else None
    ltv = arpu * (gross_margin_pct / 100) / churn if churn else None
    return {"mrr": money(mrr), "arr": money(mrr * 12), "arpu": money(arpu), "churn_rate_pct": pct(churn * 100),
            "ltv": money(ltv) if ltv is not None else None, "cac": money(cac) if cac is not None else None,
            "ltv_to_cac": round(ltv / cac, 2) if ltv and cac else None,
            "cac_payback_months": round(cac / (arpu * gross_margin_pct / 100), 1) if cac and arpu else None}


@router.get("/reorder")
def reorder(daily_demand: float = Query(gt=0, lt=BIG), lead_time_days: float = Query(ge=0, le=365),
            safety_days: float = Query(0, ge=0, le=365), order_cost: float | None = Query(None, ge=0, lt=BIG),
            holding_cost_per_unit_year: float | None = Query(None, gt=0, lt=BIG)):
    """When to reorder stock: reorder point = demand x (lead time + safety days). Add `order_cost` and
    `holding_cost_per_unit_year` to also get the economic order quantity (EOQ)."""
    out = {"reorder_point": math.ceil(daily_demand * (lead_time_days + safety_days)),
           "safety_stock": math.ceil(daily_demand * safety_days)}
    if order_cost is not None and holding_cost_per_unit_year:
        eoq = math.sqrt(2 * daily_demand * 365 * order_cost / holding_cost_per_unit_year)
        out["eoq"] = math.ceil(eoq)
        out["orders_per_year"] = round(daily_demand * 365 / eoq, 1) if eoq else None
    return out


@router.get("/volumetric")
def volumetric(length_cm: float = Query(gt=0, le=10000), width_cm: float = Query(gt=0, le=10000), height_cm: float = Query(gt=0, le=10000),
               weight_kg: float = Query(0, ge=0, le=100000), divisor: int = Query(5000, ge=1000, le=10000, description="5000 is the usual courier divisor")):
    """Volumetric vs actual weight; couriers bill the larger (chargeable) one."""
    vol = length_cm * width_cm * height_cm / divisor
    return {"volumetric_kg": round(vol, 2), "actual_kg": weight_kg, "chargeable_kg": round(max(vol, weight_kg), 2),
            "billed_by": "volume" if vol > weight_kg else "weight"}


# ---------- validators ----------
def gtin_check(digits: str) -> int:
    body = digits[::-1]
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(body))
    return (10 - total % 10) % 10


@router.get("/gtin")
def gtin(code: str = Query(min_length=7, max_length=14, pattern=r"^\d+$"), generate: bool = False):
    """Validate a barcode (EAN-8, UPC-A, EAN-13, GTIN-14) by its check digit. With `generate=true` the code is
    treated as missing its last digit (7, 11, 12 or 13 digits) and the check digit is returned."""
    if generate:
        if len(code) not in (7, 11, 12, 13):
            raise HTTPException(400, "generate needs 7, 11, 12 or 13 digits")
        cd = gtin_check(code)
        return {"body": code, "check_digit": cd, "full_code": code + str(cd)}
    if len(code) not in (8, 12, 13, 14):
        raise HTTPException(400, "Length must be 8, 12, 13 or 14")
    kind = {8: "EAN-8", 12: "UPC-A", 13: "EAN-13", 14: "GTIN-14"}[len(code)]
    expected = gtin_check(code[:-1])
    return {"code": code, "type": kind, "valid": expected == int(code[-1]), "expected_check_digit": expected}


@router.get("/iban")
def iban(iban: str = Query(min_length=15, max_length=42)):
    """IBAN structure + mod-97 checksum. Does not confirm the account exists."""
    s = re.sub(r"\s+", "", iban).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return {"iban": s, "valid": False, "reason": "bad format"}
    num = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return {"iban": s, "valid": int(num) % 97 == 1, "country": s[:2], "formatted": " ".join(s[i:i + 4] for i in range(0, len(s), 4))}


VAT_ID = {"AT": r"ATU\d{8}", "BE": r"BE[01]\d{9}", "BG": r"BG\d{9,10}", "HR": r"HR\d{11}", "CY": r"CY\d{8}[A-Z]", "CZ": r"CZ\d{8,10}",
          "DK": r"DK\d{8}", "EE": r"EE\d{9}", "FI": r"FI\d{8}", "FR": r"FR[A-Z0-9]{2}\d{9}", "DE": r"DE\d{9}", "GR": r"EL\d{9}",
          "HU": r"HU\d{8}", "IE": r"IE\d[A-Z0-9+*]\d{5}[A-Z]{1,2}", "IT": r"IT\d{11}", "LV": r"LV\d{11}", "LT": r"LT(\d{9}|\d{12})",
          "LU": r"LU\d{8}", "MT": r"MT\d{8}", "NL": r"NL\d{9}B\d{2}", "PL": r"PL\d{10}", "PT": r"PT\d{9}", "RO": r"RO\d{2,10}",
          "SK": r"SK\d{10}", "SI": r"SI\d{8}", "ES": r"ES[A-Z0-9]\d{7}[A-Z0-9]", "SE": r"SE\d{12}", "GB": r"GB(\d{9}|\d{12}|GD\d{3}|HA\d{3})"}


@router.get("/vat-id")
def vat_id(number: str = Query(min_length=4, max_length=20)):
    """Check that an EU/UK VAT number has the right shape for its country. Format only: use VIES to confirm it is registered."""
    s = re.sub(r"[\s.\-]", "", number).upper()
    for cc, pat in VAT_ID.items():
        if s.startswith(pat[:2] if cc != "GR" else "EL"):
            return {"vat_id": s, "country": cc, "valid_format": bool(re.fullmatch(pat, s)),
                    "verify_at": "https://ec.europa.eu/taxation_customs/vies/"}
    return {"vat_id": s, "country": None, "valid_format": False, "reason": "unknown country prefix"}


@router.get("/card")
def card(number: str = Query(min_length=8, max_length=25)):
    """Luhn check and card brand from the number. Never stores anything; do not send real customer card numbers."""
    s = re.sub(r"[\s\-]", "", number)
    if not s.isdigit():
        return {"valid": False, "reason": "digits only"}
    total = 0
    for i, c in enumerate(reversed(s)):
        d = int(c) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    brand = ("visa" if s[0] == "4" else "mastercard" if re.match(r"5[1-5]|2(2[2-9]|[3-6]|7[01]|720)", s) else
             "amex" if re.match(r"3[47]", s) else "discover" if re.match(r"6011|65|64[4-9]", s) else
             "jcb" if s.startswith("35") else "diners" if re.match(r"3(0[0-5]|[68])", s) else "unknown")
    return {"valid": total % 10 == 0, "brand": brand, "length": len(s), "masked": "*" * (len(s) - 4) + s[-4:]}


# ---------- links and dates ----------
@router.get("/utm")
def utm(url: str = Query(max_length=2000), source: str = Query(max_length=100), medium: str = Query(max_length=100),
        campaign: str = Query(max_length=100), term: str | None = Query(None, max_length=100), content: str | None = Query(None, max_length=100)):
    """Add tidy UTM parameters (lowercased, spaces to dashes) to a link, keeping any existing query string."""
    u = urlsplit(url if "://" in url else "https://" + url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise HTTPException(400, "url must be http(s)")
    fix = lambda v: re.sub(r"\s+", "-", v.strip().lower())
    extra = {k: fix(v) for k, v in (("utm_source", source), ("utm_medium", medium), ("utm_campaign", campaign),
                                    ("utm_term", term), ("utm_content", content)) if v}
    q = [(k, v) for k, v in parse_qsl(u.query) if k not in extra] + list(extra.items())
    return {"url": urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))}


@router.get("/business-days")
def business_days(start: date, days: int | None = Query(None, ge=-3650, le=3650), end: date | None = None,
                  holidays: str | None = Query(None, max_length=2000, description="comma separated YYYY-MM-DD to skip")):
    """Add N business days to a date (`days`) or count them between two dates (`end`). Skips weekends and any `holidays` you list."""
    try:
        off = {date.fromisoformat(h.strip()) for h in holidays.split(",") if h.strip()} if holidays else set()
    except ValueError:
        raise HTTPException(400, "holidays must be YYYY-MM-DD")
    work = lambda d: d.weekday() < 5 and d not in off
    if days is not None:
        d, left, step = start, abs(days), 1 if days >= 0 else -1
        while left:
            d += timedelta(days=step)
            if work(d):
                left -= 1
        return {"start": start.isoformat(), "business_days": days, "result": d.isoformat(), "weekday": d.strftime("%A")}
    if end is None:
        raise HTTPException(400, "Give days or end")
    a, b = sorted((start, end))
    if (b - a).days > 36600:
        raise HTTPException(400, "range too long")
    n = sum(1 for i in range(1, (b - a).days + 1) if work(a + timedelta(days=i)))
    return {"start": start.isoformat(), "end": end.isoformat(), "business_days": n if end >= start else -n}


# ---------- invoices ----------
class Party(BaseModel):
    name: str = Field(max_length=200)
    address: str = Field("", max_length=500)
    tax_id: str = Field("", max_length=50)
    email: str = Field("", max_length=200)


class Item(BaseModel):
    description: str = Field(max_length=300)
    quantity: float = Field(1, gt=0, lt=1e9)
    unit_price: float = Field(ge=0, lt=BIG)
    tax_rate: float | None = Field(None, ge=0, le=100, description="overrides the invoice rate for this line")


class InvoiceReq(BaseModel):
    number: str = Field(max_length=40)
    seller: Party
    buyer: Party
    items: list[Item] = Field(min_length=1, max_length=200)
    currency: str = Field("USD", min_length=3, max_length=3)
    tax_rate: float = Field(0, ge=0, le=100)
    prices_include_tax: bool = False
    discount_pct: float = Field(0, ge=0, le=100)
    issue_date: date | None = None
    due_days: int = Field(14, ge=0, le=365)
    notes: str = Field("", max_length=1000)


def _invoice(r: InvoiceReq) -> dict:
    rows, tax_by_rate, net_total = [], {}, D(0)
    disc = D(str(r.discount_pct)) / 100
    for it in r.items:
        rate = D(str(r.tax_rate if it.tax_rate is None else it.tax_rate))
        line = D(str(it.unit_price)) * D(str(it.quantity)) * (1 - disc)
        net = line / (1 + rate / 100) if r.prices_include_tax else line
        tax = net * rate / 100
        net_total += net
        tax_by_rate[rate] = tax_by_rate.get(rate, D(0)) + tax
        rows.append({"description": it.description, "quantity": it.quantity, "unit_price": money(it.unit_price),
                     "tax_rate": float(rate), "net": money(net), "tax": money(tax), "gross": money(net + tax)})
    tax_total = sum(tax_by_rate.values(), D(0))
    issued = r.issue_date or date.today()
    return {"number": r.number, "currency": r.currency.upper(), "issue_date": issued.isoformat(),
            "due_date": (issued + timedelta(days=r.due_days)).isoformat(), "seller": r.seller.model_dump(),
            "buyer": r.buyer.model_dump(), "items": rows, "discount_pct": r.discount_pct,
            "tax_breakdown": [{"rate": float(k), "tax": money(v)} for k, v in sorted(tax_by_rate.items())],
            "subtotal": money(net_total), "tax_total": money(tax_total), "total": money(net_total + tax_total), "notes": r.notes}


def _invoice_html(inv: dict) -> str:
    e = lambda s: _html.escape(str(s))
    cur = e(inv["currency"])
    rows = "".join(f"<tr><td>{e(i['description'])}</td><td class=r>{i['quantity']:g}</td><td class=r>{i['unit_price']:.2f}</td>"
                   f"<td class=r>{i['tax_rate']:g}%</td><td class=r>{i['gross']:.2f}</td></tr>" for i in inv["items"])
    taxes = "".join(f"<tr><td colspan=4 class=r>VAT {t['rate']:g}%</td><td class=r>{t['tax']:.2f}</td></tr>" for t in inv["tax_breakdown"])
    party = lambda p: f"<b>{e(p['name'])}</b><br>{e(p['address']).replace(chr(10), '<br>')}<br>{e(p['tax_id'])}<br>{e(p['email'])}"
    return f"""<!doctype html><meta charset=utf-8><title>Invoice {e(inv['number'])}</title>
<style>body{{font:14px system-ui,sans-serif;max-width:780px;margin:32px auto;color:#222}}h1{{margin:0}}table{{width:100%;border-collapse:collapse;margin-top:24px}}
th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}.r{{text-align:right}}.cols{{display:flex;gap:40px;margin-top:24px}}.cols>div{{flex:1}}
.total td{{font-weight:700;font-size:16px;border-top:2px solid #222}}</style>
<h1>Invoice {e(inv['number'])}</h1><p>Issued {e(inv['issue_date'])} &middot; Due {e(inv['due_date'])}</p>
<div class=cols><div>From<br>{party(inv['seller'])}</div><div>Bill to<br>{party(inv['buyer'])}</div></div>
<table><tr><th>Description</th><th class=r>Qty</th><th class=r>Unit</th><th class=r>Tax</th><th class=r>Amount ({cur})</th></tr>{rows}
<tr><td colspan=4 class=r>Subtotal</td><td class=r>{inv['subtotal']:.2f}</td></tr>{taxes}
<tr class=total><td colspan=4 class=r>Total {cur}</td><td class=r>{inv['total']:.2f}</td></tr></table><p>{e(inv['notes'])}</p>"""


@router.post("/invoice")
def invoice(r: InvoiceReq, format: Literal["json", "html"] = "json"):
    """Build an invoice: line totals, per-rate tax breakdown, discount, due date. `format=html` returns a printable page
    (open it and use Print > Save as PDF). `json` also includes the same numbers plus an `html` field."""
    inv = _invoice(r)
    if format == "html":
        return HTMLResponse(_invoice_html(inv))
    inv["html"] = _invoice_html(inv)
    return inv
