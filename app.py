import io
import json
import re
import base64
import uuid
import zlib
import unicodedata
from datetime import datetime
from pathlib import Path
from difflib import SequenceMatcher

import pandas as pd
import pdfplumber
import plotly.express as px
import streamlit as st
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract
import streamlit.components.v1 as components


st.set_page_config(page_title="Control comercial", page_icon="📊", layout="wide")

BASE = Path(__file__).resolve().parent
SAMPLE = BASE / "sample_data.json"
APP_DATA_VERSION = "nuevo-mes-en-blanco-v1"
browser_store = components.declare_component("control_comercial_browser_store", path=str(BASE / "browser_store"))

COLORS = {
    "No hay stock": "#d73027",
    "Falta stock": "#ef6548",
    "Va lento": "#f4a261",
    "Hay que venderlo": "#e9c46a",
    "Más rápido": "#277da1",
    "Va bien": "#43aa8b",
    "Meta cumplida": "#2a9d8f",
    "Revisar dato": "#7f8c8d",
    "Sin movimiento": "#adb5bd",
}

st.markdown(
    """
    <style>
    .block-container {padding-top: 1.5rem; padding-bottom: 2rem;}
    [data-testid="stMetric"] {background:#f5f8fb; border:1px solid #dce6ef; padding:16px; border-radius:12px;}
    .hero {background:linear-gradient(120deg,#16324f,#24557c);color:white;padding:20px 24px;border-radius:16px;margin-bottom:16px;}
    .hero h1 {margin:0;font-size:2rem;}
    .hero p {margin:.4rem 0 0;color:#dceaf5;}
    .action-card {padding:14px 16px;border-radius:12px;background:#fff;border:1px solid #e5e7eb;margin-bottom:9px;}
    .small {color:#667085;font-size:.88rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


def norm_code(value):
    if pd.isna(value):
        return None
    match = re.search(r"\[(\d{8})\]", str(value))
    if match:
        return match.group(1)
    try:
        if float(value).is_integer():
            value = str(int(float(value)))
    except (ValueError, TypeError):
        pass
    digits = re.sub(r"\D", "", str(value))
    return digits.zfill(8) if digits else None


def number_local(value):
    value = str(value).strip()
    if "," in value:
        return float(value.replace(".", "").replace(",", "."))
    return float(value.replace(",", ""))


def canonical_order(value):
    """Comparable order key while preserving alphanumeric customer orders.

    Invoice PDFs sometimes leave extra text after ``OC:``. Prefer the first
    plausible numeric order number (allowing spaces/hyphens) so an invoice such
    as ``OC: 100 6243 97153`` matches the order parser's ``100624397153``.
    """
    raw = str(value or "").upper().strip()
    numeric = re.search(r"(?<!\d)((?:\d[ -]*){6,20})(?!\d)", raw)
    if numeric:
        digits = re.sub(r"\D", "", numeric.group(1))
        if len(digits) >= 6:
            return digits
    return re.sub(r"[^A-Z0-9]", "", raw)


def _normalize_product_text(value):
    value = str(value or "").upper()
    value = re.sub(r"[^A-Z0-9ÁÉÍÓÚÑ ]+", " ", value)
    value = re.sub(r"\bREF\b.*$", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _product_catalog(forecast_df, stock_df):
    frames = []
    for source in (forecast_df, stock_df):
        if source is not None and not source.empty and {"code", "product"}.issubset(source.columns):
            frames.append(source[["code", "product"]].copy())
    if not frames:
        return pd.DataFrame(columns=["code", "product", "product_norm"])
    catalog = pd.concat(frames, ignore_index=True).dropna(subset=["code", "product"])
    catalog["code"] = catalog["code"].map(norm_code)
    catalog = catalog[catalog["code"].notna()].drop_duplicates("code")
    catalog["product_norm"] = catalog["product"].map(_normalize_product_text)
    return catalog.reset_index(drop=True)


def _best_product_match(name, catalog):
    target = _normalize_product_text(name)
    if not target or catalog.empty:
        return "", "", 0.0

    best_code, best_name, best_score = "", "", 0.0
    target_tokens = set(target.split())
    for row in catalog.itertuples(index=False):
        candidate = row.product_norm
        seq = SequenceMatcher(None, target, candidate).ratio()
        cand_tokens = set(candidate.split())
        overlap = len(target_tokens & cand_tokens) / max(len(target_tokens | cand_tokens), 1)
        contains = 1.0 if target in candidate or candidate in target else 0.0
        score = max(seq, (seq * 0.65 + overlap * 0.35), contains)
        if score > best_score:
            best_code, best_name, best_score = row.code, row.product, score
    return best_code, best_name, best_score


def read_coral_order_image(file, forecast_df, stock_df):
    """Read a simple Coral product/quantity table from a photo.

    The source image does not contain a customer SKU, so product descriptions
    are OCR'd and then matched against the forecast/stock product catalog.
    Every result remains editable before it is accepted as a weekly order.
    """
    image = Image.open(file).convert("L")
    image = ImageOps.autocontrast(image)
    image = image.resize((image.width * 2, image.height * 2))
    image = ImageEnhance.Contrast(image).enhance(1.7)
    image = image.filter(ImageFilter.SHARPEN)

    try:
        ocr = pytesseract.image_to_string(image, config="--psm 6")
    except Exception as exc:
        raise RuntimeError(f"No pude ejecutar OCR sobre la imagen: {exc}") from exc

    rows = []
    for raw in ocr.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or "PRODUCTO" in line.upper() or "CANTIDAD" in line.upper():
            continue
        match = re.match(r"^(.*?)[\s|]+(\d{1,5})\s*$", line)
        if not match:
            continue
        product = match.group(1).strip(" |:-")
        qty = int(match.group(2))
        if not product or qty <= 0:
            continue
        rows.append((product, qty))

    if not rows:
        raise ValueError("No pude identificar filas PRODUCTO / CANTIDAD en la imagen. Pruebe con una foto más recta y nítida.")

    catalog = _product_catalog(forecast_df, stock_df)
    parsed = []
    for product, qty in rows:
        code, matched_name, score = _best_product_match(product, catalog)
        parsed.append({
            "Cliente": "Coral",
            "Producto leído": product,
            "Producto identificado": matched_name,
            "Cantidad": qty,
            "Código SKU": code if score >= 0.58 else "",
            "Coincidencia": score,
            "Revisión": "SKU identificado" if score >= 0.58 else "Revisar SKU",
        })

    result = pd.DataFrame(parsed)
    result = result.drop_duplicates(["Producto leído", "Cantidad"], keep="first")
    return result, ocr


def read_forecast(file):
    df = pd.read_excel(file, sheet_name=0, header=3)
    df = df.iloc[:, :4]
    df.columns = ["code", "product", "handling_unit", "forecast"]
    df["code"] = df["code"].map(norm_code)
    df = df[df["code"].notna()].copy()
    df["forecast"] = pd.to_numeric(df["forecast"], errors="coerce").fillna(0)
    df["handling_unit"] = pd.to_numeric(df["handling_unit"], errors="coerce")
    return df


def read_stock(file):
    raw = pd.read_excel(file, sheet_name=0)
    raw.columns = ["product", "location", "lot", "available", "quantity", "expiry", "uom", "company"]
    raw["code"] = raw["product"].map(norm_code)
    raw = raw[raw["code"].notna()].copy()
    summary = raw[raw["location"].isna()].copy()
    detail = raw[raw["location"].notna()].copy()
    summary["available"] = pd.to_numeric(summary["available"], errors="coerce").fillna(0)
    summary["quantity"] = pd.to_numeric(summary["quantity"], errors="coerce").fillna(0)
    detail["expiry"] = pd.to_datetime(detail["expiry"], errors="coerce")
    expiry = detail.groupby("code", as_index=False)["expiry"].min()
    summary = summary[["code", "product", "available", "quantity"]].merge(expiry, on="code", how="left")
    return summary, detail


def read_pdf(file):
    invoice_re = re.compile(r"No\.:\s*(\d{3}-\d{3}-\d{9})")
    issue_date_re = re.compile(r"(?:Fecha de emisi[oó]n:|Issue Date:)\s*(\d{2}/\d{2}/\d{4})", re.I)
    auth_date_re = re.compile(r"(?:N[uú]mero de autorizaci[oó]n:|Authorization No\.:)\s*\n?(\d{8})", re.I)
    spanish = re.compile(r"^(\d{8})\s+.*?\s([\d.]+,\d{4})\s+([\d.]+,\d{5})\s+.*?\$\s*([\d.]+,\d{2})$")
    english = re.compile(r"^(\d{8})\s+.*?\s([\d,]+\.\d{4})\s+([\d,]+\.\d{5})\s+.*?\$\s*([\d,]+\.\d{2})$")
    # Capture only the order token after OC instead of the whole remainder
    # of the line. The previous expression could turn trailing labels/text into
    # part of the order key and make every Fill Rate match fail silently.
    purchase_order_re = re.compile(
        r"\bOC\s*:\s*((?:\d[ -]*){6,20}|[A-Z0-9][A-Z0-9._/-]{4,30})",
        re.I,
    )
    records, invoice, date, customer, order_by_invoice = [], None, None, "", {}
    source = io.BytesIO(file.getvalue()) if hasattr(file, "getvalue") else file
    with pdfplumber.open(source) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            inv = invoice_re.search(text)
            if inv:
                invoice = inv.group(1)
                issued = issue_date_re.search(text)
                auth = auth_date_re.search(text)
                if issued:
                    date = datetime.strptime(issued.group(1), "%d/%m/%Y")
                elif auth:
                    date = datetime.strptime(auth.group(1), "%d%m%Y")
                lines = text.splitlines()
                customer = ""
                for idx, line in enumerate(lines):
                    if ("Nombres y apellidos:" in line or "Partner:" in line) and idx + 1 < len(lines):
                        customer = re.split(r"\s+(?:Fecha de vencimiento:|Due Date:)", lines[idx + 1])[0].strip()
                        break
            if invoice:
                order_match = purchase_order_re.search(text)
                if order_match:
                    order_by_invoice[invoice] = canonical_order(order_match.group(1))
            for raw in text.splitlines():
                match = spanish.match(raw.strip()) or english.match(raw.strip())
                if not match or not invoice or not date:
                    continue
                qty, price, net = map(number_local, match.groups()[1:])
                records.append({"invoice": invoice, "date": date, "week": 1 if date.day <= 7 else 2 if date.day <= 14 else 3 if date.day <= 21 else 4 if date.day <= 28 else 5, "customer": customer, "code": match.group(1), "quantity": qty, "unit_price": price, "net_sales": net, "pdf_page": page_no})
    result = pd.DataFrame(records)
    if not result.empty:
        result["purchase_order"] = result["invoice"].map(order_by_invoice).fillna("")
    return result


def profit_sku(value):
    """Normalize profitability SKUs without turning EXP codes into national SKUs."""
    if pd.isna(value):
        return None
    raw = str(value).strip().upper()
    bracketed = re.search(r"\[([^\]]+)\]", raw)
    if bracketed:
        raw = bracketed.group(1)
    compact = re.sub(r"[^A-Z0-9]", "", raw)
    if not compact:
        return None
    return compact.zfill(8) if compact.isdigit() and len(compact) <= 8 else compact


def _plain_header(value):
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def read_profitability_costs(file):
    raw = pd.read_excel(file, dtype=str)
    columns = {_plain_header(column): column for column in raw.columns}

    def find(*terms):
        return next((original for normalized, original in columns.items() if all(term in normalized for term in terms)), None)

    code_col = find("referencia", "interna") or find("sku") or find("codigo")
    name_col = find("nombre") or find("producto")
    cost_col = find("costo") or find("coste")
    uom_col = find("unidad", "medida") or find("udm")
    missing = [label for label, value in [("Referencia Interna", code_col), ("Nombre", name_col), ("Costo", cost_col), ("Unidad de Medida", uom_col)] if value is None]
    if missing:
        raise ValueError("Faltan columnas en costos: " + ", ".join(missing))
    result = raw[[code_col, name_col, cost_col, uom_col]].copy()
    result.columns = ["code", "cost_product", "unit_cost", "uom"]
    result["code"] = result["code"].map(profit_sku)
    result["unit_cost"] = result["unit_cost"].map(lambda value: number_local(value) if pd.notna(value) and str(value).strip() else pd.NA)
    return result[result["code"].notna()].reset_index(drop=True)


def read_profitability_pdfs(files):
    invoice_re = re.compile(r"No\.:\s*(\d{3}-\d{3}-\d{9})")
    issue_date_re = re.compile(r"(?:Fecha de emisi[oó]n:|Issue Date:)\s*(\d{2}/\d{2}/\d{4})", re.I)
    auth_date_re = re.compile(r"(?:N[uú]mero de autorizaci[oó]n:|Authorization No\.:)\s*\n?(\d{8})", re.I)
    spanish = re.compile(r"^(\d{8}|EXP[A-Z0-9._/-]*)\s+(.*?)\s+([\d.]+,\d{4})\s+([\d.]+,\d{5})\s+.*?\$\s*([\d.]+,\d{2})$")
    english = re.compile(r"^(\d{8}|EXP[A-Z0-9._/-]*)\s+(.*?)\s+([\d,]+\.\d{4})\s+([\d,]+\.\d{5})\s+.*?\$\s*([\d,]+\.\d{2})$", re.I)
    rows = []
    for file in files or []:
        source = io.BytesIO(file.getvalue()) if hasattr(file, "getvalue") else file
        with pdfplumber.open(source) as pdf:
            invoice, date, customer, credit = None, None, "", False
            for page_no, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                found = invoice_re.search(text)
                if found:
                    invoice = found.group(1)
                    credit = bool(re.search(r"NOTA\s+DE\s+CR[EÉ]DITO|CREDIT\s+NOTE", text, re.I))
                    issued, auth = issue_date_re.search(text), auth_date_re.search(text)
                    date = datetime.strptime(issued.group(1), "%d/%m/%Y") if issued else datetime.strptime(auth.group(1), "%d%m%Y") if auth else None
                    lines = text.splitlines()
                    for idx, line in enumerate(lines):
                        if ("Nombres y apellidos:" in line or "Partner:" in line) and idx + 1 < len(lines):
                            customer = re.split(r"\s+(?:Fecha de vencimiento:|Due Date:)", lines[idx + 1])[0].strip()
                            break
                for line_no, raw_line in enumerate(text.splitlines(), 1):
                    match = spanish.match(raw_line.strip()) or english.match(raw_line.strip())
                    if not match or not invoice or not date:
                        continue
                    code, product, qty_raw, price_raw, net_raw = match.groups()
                    qty, price, net = number_local(qty_raw), number_local(price_raw), number_local(net_raw)
                    sign = -1 if credit else 1
                    gross = abs(qty * price)
                    discount = max(0.0, (1 - abs(net) / gross) * 100) if gross else 0.0
                    rows.append({
                        "date": pd.Timestamp(date), "invoice": invoice, "customer": customer or "Cliente sin identificar",
                        "code": profit_sku(code), "product": product.strip(), "quantity": sign * abs(qty),
                        "unit_price": price, "discount_pct": discount, "net_sales": sign * abs(net),
                        "document_type": "Nota de crédito" if credit else "Factura", "pdf_page": page_no,
                        "line_no": line_no, "invoice_type": "Exportación" if invoice.startswith("001-901-") else "Nacional" if invoice.startswith("001-100-") else "Otra",
                        "source_file": getattr(file, "name", "archivo.pdf"),
                    })
    return pd.DataFrame(rows)


def blank_profit_store():
    return {"months": {}}


def pack_profit_store(store):
    months = {}
    for key, value in store.get("months", {}).items():
        months[key] = {
            "invoices": value.get("invoices", pd.DataFrame()).to_json(orient="records", date_format="iso"),
            "costs": value.get("costs", pd.DataFrame()).to_json(orient="records", date_format="iso"),
        }
    raw = json.dumps({"version": 1, "months": months}).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, level=9)).decode("ascii")


def unpack_profit_store(encoded):
    if not isinstance(encoded, str) or len(encoded) > 20_000_000:
        raise ValueError("Copia de rentabilidad inválida")
    raw = zlib.decompress(base64.b64decode(encoded, validate=True))
    if len(raw) > 30_000_000:
        raise ValueError("Copia de rentabilidad demasiado grande")
    payload = json.loads(raw)
    if payload.get("version") != 1:
        raise ValueError("Versión de rentabilidad no compatible")
    store = blank_profit_store()
    for key, value in payload.get("months", {}).items():
        invoices = pd.read_json(io.StringIO(value.get("invoices", "[]")), orient="records", dtype={"code": str}, convert_dates=False)
        costs = pd.read_json(io.StringIO(value.get("costs", "[]")), orient="records", dtype={"code": str}, convert_dates=False)
        if "date" in invoices:
            invoices["date"] = pd.to_datetime(invoices["date"], errors="coerce")
        store["months"][key] = {"invoices": invoices, "costs": costs}
    return store


def profitability_lines(invoices, costs):
    clean_costs = costs.drop_duplicates("code", keep=False).copy()
    result = invoices.merge(clean_costs[["code", "cost_product", "unit_cost", "uom"]], on="code", how="left")
    result["cost_of_sales"] = result["quantity"] * result["unit_cost"]
    result["gross_profit"] = result["net_sales"] - result["cost_of_sales"]
    result["gross_margin"] = result["gross_profit"] / result["net_sales"].replace(0, pd.NA)
    result["real_avg_price"] = result["net_sales"] / result["quantity"].replace(0, pd.NA)
    result["gross_before_discount"] = result["quantity"] * result["unit_price"]
    result["discount_amount"] = result["gross_before_discount"] - result["net_sales"]
    return result


def sample_frames():
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    forecast = pd.DataFrame(data["forecast"])[["code", "product", "handling_unit", "forecast_units"]].rename(columns={"forecast_units": "forecast"})
    stock_summary = pd.DataFrame(data["stock_summary"])
    lots = pd.DataFrame(data["stock_lots"])
    stock_summary["expiry"] = stock_summary["code"].map(pd.DataFrame(data["stock_lots"]).groupby("code")["expiry"].min())
    invoices = pd.DataFrame(data["invoice_lines"]).rename(columns={"net_amount": "net_sales", "page": "pdf_page"})
    invoices["date"] = pd.to_datetime(invoices["date"])
    return forecast, stock_summary, lots, invoices, pd.Timestamp(data["as_of"])


def blank_bundle():
    return {
        "forecast": pd.DataFrame(columns=["code", "product", "handling_unit", "forecast"]),
        "stock": pd.DataFrame(columns=["code", "product", "available", "quantity", "expiry"]),
        "lots": pd.DataFrame(columns=["code", "product", "location", "lot", "available", "quantity", "expiry", "uom", "company"]),
        "invoices": pd.DataFrame(columns=["invoice", "date", "week", "customer", "code", "quantity", "unit_price", "net_sales", "pdf_page", "purchase_order"]),
        "fill_rate_invoices": pd.DataFrame(columns=["invoice", "date", "week", "customer", "code", "quantity", "unit_price", "net_sales", "pdf_page", "purchase_order"]),
        "orders": pd.DataFrame(columns=["Cliente", "Orden", "Fecha pedido", "Fecha inicio", "Fecha límite", "Producto en pedido", "Referencia cliente", "Cajas", "Unidades por caja", "Unidades pedidas", "Código SKU", "Archivo", "Revisión"]),
        "weekly_order": pd.DataFrame(columns=["code", "ordered"]),
        "cutoff": pd.NaT,
    }


def pack_bundle(bundle):
    data = {
        "version": 3,
        "tables": {name: bundle.get(name, blank_bundle()[name]).to_json(orient="records", date_format="iso") for name in ["forecast", "stock", "lots", "invoices", "fill_rate_invoices", "orders", "weekly_order"]},
        "cutoff": None if pd.isna(bundle["cutoff"]) else pd.Timestamp(bundle["cutoff"]).isoformat(),
    }
    return base64.b64encode(zlib.compress(json.dumps(data).encode("utf-8"), level=9)).decode("ascii")


def unpack_bundle(encoded):
    if not isinstance(encoded, str) or len(encoded) > 20_000_000:
        raise ValueError("Copia guardada inválida o demasiado grande")
    inflater = zlib.decompressobj()
    raw = inflater.decompress(base64.b64decode(encoded, validate=True), 30_000_001)
    if len(raw) > 30_000_000 or inflater.unconsumed_tail or not inflater.eof:
        raise ValueError("Copia guardada demasiado grande o dañada")
    data = json.loads(raw)
    if data.get("version") not in [1, 2, 3]:
        raise ValueError("Versión de copia no compatible")
    bundle = blank_bundle()
    for name in ["forecast", "stock", "lots", "invoices", "fill_rate_invoices", "orders", "weekly_order"]:
        if name in data["tables"]:
            bundle[name] = pd.read_json(io.StringIO(data["tables"][name]), orient="records", dtype={"code": str}, convert_dates=False)
    for name, column in [("stock", "expiry"), ("lots", "expiry"), ("invoices", "date"), ("fill_rate_invoices", "date"), ("orders", "Fecha pedido"), ("orders", "Fecha inicio"), ("orders", "Fecha límite")]:
        if column in bundle[name]:
            bundle[name][column] = pd.to_datetime(bundle[name][column], errors="coerce")
    bundle["cutoff"] = pd.to_datetime(data["cutoff"]) if data.get("cutoff") else pd.NaT
    if bundle["forecast"].empty or bundle["invoices"].empty:
        raise ValueError("La copia no contiene forecast y facturas")
    return bundle


def build_analysis(forecast, stock, invoices, cutoff):
    # Las exportaciones 001-901 se informan por separado, pero pertenecen a
    # otro plan comercial y no consumen el forecast nacional del mes.
    forecast_invoices = invoices[
        ~invoices["invoice"].astype(str).str.startswith("001-901-")
    ].copy()
    sold = forecast_invoices.groupby("code", as_index=False).agg(
        sold=("quantity", "sum"), net_sales=("net_sales", "sum")
    )
    master = forecast.merge(stock[["code", "available", "quantity", "expiry"]], on="code", how="outer")
    master = master.merge(sold, on="code", how="outer")
    names = pd.concat([
        forecast[["code", "product"]],
        stock[["code", "product"]]
    ]).dropna().drop_duplicates("code")
    master = master.drop(columns=["product"], errors="ignore").merge(names, on="code", how="left")
    for col in ["available", "quantity", "sold", "net_sales"]:
        master[col] = pd.to_numeric(master[col], errors="coerce").fillna(0)
    month_days = cutoff.days_in_month
    elapsed = cutoff.day / month_days
    master["advance"] = master["sold"] / master["forecast"].replace(0, pd.NA)
    master["remaining"] = (master["forecast"].fillna(0) - master["sold"]).clip(lower=0)
    master["missing_stock"] = (master["remaining"] - master["available"]).clip(lower=0)
    master["needed_per_day"] = master["remaining"] / max(month_days - cutoff.day, 1)
    master["projection"] = master["sold"] / max(cutoff.day, 1) * month_days

    def status(row):
        if pd.isna(row["forecast"]):
            return "Hay que venderlo" if row["available"] > 0 else "Revisar dato"
        if row["forecast"] == 0:
            return "Hay que venderlo" if row["available"] > 0 else "Sin movimiento"
        if row["available"] == 0 and row["remaining"] > 0:
            return "No hay stock"
        if row["missing_stock"] > 0:
            return "Falta stock"
        if row["sold"] >= row["forecast"]:
            return "Meta cumplida"
        if row["advance"] > elapsed + .10:
            return "Más rápido"
        if row["advance"] < elapsed - .10:
            return "Va lento"
        return "Va bien"

    def action(row):
        if row["status"] == "No hay stock":
            return f"Reponer {row['remaining']:,.0f} unidades."
        if row["status"] == "Falta stock":
            return f"Reponer {row['missing_stock']:,.0f} unidades para cumplir."
        if row["status"] == "Hay que venderlo":
            return f"Vender el saldo: {row['available']:,.0f} unidades."
        if row["status"] == "Va lento":
            return f"Impulsar ventas: faltan {row['remaining']:,.0f} unidades."
        if row["status"] == "Más rápido":
            return "No promocionar. Revisar si el stock alcanzará."
        if row["status"] == "Va bien":
            return f"Mantener un ritmo de {row['needed_per_day']:,.0f} unidades por día."
        if row["status"] == "Meta cumplida":
            return "Meta cumplida. No promocionar."
        return "Revisar código o dato."

    master["status"] = master.apply(status, axis=1)
    master["action"] = master.apply(action, axis=1)
    order = {name: idx for idx, name in enumerate(["No hay stock", "Falta stock", "Va lento", "Hay que venderlo", "Más rápido", "Va bien", "Meta cumplida", "Revisar dato", "Sin movimiento"])}
    master["priority"] = master["status"].map(order).fillna(99)
    return master.sort_values(["priority", "missing_stock"], ascending=[True, False]).reset_index(drop=True), elapsed


def file_signature(file):
    return None if file is None else (file.name, len(file.getvalue()))


def prepare_weekly_order(raw, code_column, quantity_column):
    order = raw[[code_column, quantity_column]].copy()
    order.columns = ["code", "ordered"]
    order["code"] = order["code"].map(norm_code)
    order["ordered"] = pd.to_numeric(order["ordered"], errors="coerce")
    order = order.dropna(subset=["code", "ordered"])
    order = order[order["ordered"] > 0]
    return order.groupby("code", as_index=False)["ordered"].sum()


def order_number(value):
    return float(str(value).strip().replace(".", "").replace(",", ".")) if "," in str(value) else float(str(value).strip())


def tia_mappings(pdf):
    client_to_sku, barcode_to_sku = {}, {}
    for page in pdf.pages:
        for table in page.extract_tables():
            for row in table:
                if len(row) < 10:
                    continue
                client = re.sub(r"\D", "", str(row[0] or ""))
                barcode = re.sub(r"\D", "", str(row[5] or ""))
                sku = re.sub(r"\D", "", str(row[7] or ""))
                if len(client) == 9 and len(sku) == 8 and sku != client:
                    client_to_sku[client] = sku
                    if len(barcode) == 13:
                        barcode_to_sku[barcode] = sku
    return client_to_sku, barcode_to_sku


def parse_order_pdfs(files, forecast, stock):
    documents = []
    known_skus = set(forecast["code"].dropna().astype(str)) | set(stock["code"].dropna().astype(str))
    client_to_sku, barcode_to_sku = {}, {
        "7862123515891": "01020019", "7862123513842": "03020007",
        "7862123515495": "01020014", "7862123510391": "01020002",
        "7862123516430": "13020001", "7862123516447": "13020002",
    }
    barcode_candidates = {}
    for catalog in [forecast, stock]:
        for _, item in catalog.iterrows():
            match = re.search(r"\b\d{13}\b", str(item["product"]))
            if match and pd.notna(item["code"]):
                code = str(item["code"])
                candidate = barcode_candidates.setdefault(match.group(), {}).setdefault(code, set())
                if "handling_unit" in catalog.columns and pd.notna(item.get("handling_unit")):
                    candidate.add(float(item["handling_unit"]))
    barcode_to_sku.update({barcode: next(iter(codes)) for barcode, codes in barcode_candidates.items() if len(codes) == 1})
    ambiguous_barcodes = {barcode for barcode, codes in barcode_candidates.items() if len(codes) > 1}

    def resolve_barcode(barcode, units_per_case):
        candidates = barcode_candidates.get(barcode, {})
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1 and units_per_case is not None:
            matches = [code for code, handling_units in candidates.items() if float(units_per_case) in handling_units]
            if len(matches) == 1:
                return matches[0]
            return ""
        return barcode_to_sku.get(barcode, "")
    for file in files:
        pdf = pdfplumber.open(io.BytesIO(file.getvalue()))
        documents.append((file.name, pdf))
        tia, barcodes = tia_mappings(pdf)
        client_to_sku.update(tia)
        barcode_to_sku.update(barcodes)
        for page in pdf.pages:
            for table in page.extract_tables():
                for line in table:
                    if len(line) < 8:
                        continue
                    client = re.sub(r"\D", "", str(line[0] or ""))
                    barcode = re.sub(r"\D", "", str(line[5] or ""))
                    if len(client) == 9 and barcode in barcode_to_sku:
                        client_to_sku[client] = barcode_to_sku[barcode]

    rows, errors = [], []
    try:
        for filename, pdf in documents:
            found = 0
            for page_no, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                if "Resultado De La Homologación" in text or "TIENDAS INDUSTRIALES ASOCIADAS" in text:
                    order_match = re.search(r"ORDEN DE COMPRA N[º°]\s*(\d+)", text)
                    order_id = order_match.group(1) if order_match else f"página {page_no}"
                    window = re.search(r"Desde El:\s*Hasta El:\s*(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})", text)
                    loaded = re.search(r"Fecha Carga:(\d{4}-\d{2}-\d{2})", text)
                    order_date = pd.to_datetime(loaded.group(1)) if loaded else pd.NaT
                    start_date = pd.to_datetime(window.group(1)) if window else pd.NaT
                    end_date = pd.to_datetime(window.group(2)) if window else pd.NaT
                    for table in page.extract_tables():
                        for line in table:
                            if len(line) < 11:
                                continue
                            client_code = re.sub(r"\D", "", str(line[10] or ""))
                            try:
                                units = order_number(line[0])
                            except (ValueError, TypeError):
                                continue
                            if len(client_code) != 9 or units <= 0:
                                continue
                            rows.append({"Cliente": "TIA", "Orden": canonical_order(order_id), "Fecha pedido": order_date, "Fecha inicio": start_date, "Fecha límite": end_date, "Producto en pedido": str(line[6] or "").replace("\n", " "), "Referencia cliente": client_code, "Cajas": order_number(line[1]) if line[1] else None, "Unidades por caja": None, "Unidades pedidas": units, "Código SKU": client_to_sku.get(client_code, ""), "Archivo": filename})
                            found += 1
                elif "CORPORACION EL ROSADO" in text:
                    for table in page.extract_tables():
                        if len(table) < 6 or not any("NUMERO DE ORDEN" in str(cell) for line in table[:5] for cell in line):
                            continue
                        order_id = next((str(line[2]) for line in table if str(line[0]).startswith("NUMERO DE ORDEN")), f"página {page_no}")
                        dates = re.search(r"FECHA DEL\s*(\d{4}\.\d{2}\.\d{2})\s+FECHA DE\s*(\d{4}\.\d{2}\.\d{2})", text)
                        order_date = pd.to_datetime(dates.group(1).replace(".", "-")) if dates else pd.NaT
                        end_date = pd.to_datetime(dates.group(2).replace(".", "-")) if dates else pd.NaT
                        for line in table:
                            if len(line) < 9 or not str(line[0] or "").isdigit() or not str(line[1] or "").isdigit():
                                continue
                            reference = re.sub(r"\D", "", str(line[5] or ""))
                            try:
                                packs, uxc = order_number(line[8]), order_number(line[7])
                            except (ValueError, TypeError):
                                continue
                            if packs <= 0 or uxc <= 0:
                                continue
                            if len(reference) == 8:
                                sku = reference
                            elif len(reference) == 7 and reference.zfill(8) in known_skus:
                                sku = reference.zfill(8)
                            else:
                                sku = resolve_barcode(reference, uxc)
                            rows.append({"Cliente": "El Rosado", "Orden": canonical_order(order_id), "Fecha pedido": order_date, "Fecha inicio": order_date, "Fecha límite": end_date, "Producto en pedido": str(line[2] or "").replace("\n", " "), "Referencia cliente": reference, "Cajas": packs, "Unidades por caja": uxc, "Unidades pedidas": packs * uxc, "Código SKU": sku, "Archivo": filename})
                            found += 1
                elif "CORPORACION FAVORITA" in text and "Pedida" in text:
                    blocks = re.split(r"(?=Tda/Alm/CDI:)", text)
                    for block_no, block in enumerate(blocks, 1):
                        if "IT D e s c r i p c i o n" not in block:
                            continue
                        order_match = re.search(r"ORDEN COMPRA[^\n]*?50\s*:\s*(\d+)\s+(\d+)\s+(\d+)", block)
                        order_id = "".join(order_match.groups()) if order_match else f"página {page_no}, bloque {block_no}"
                        elaborated = re.search(r"Fecha Elabora:\s*(\d{2}/[A-Z]{3}/\d{4})", block)
                        valid = re.search(r"Fecha Vigencia:\s*(\d{2}/[A-Z]{3}/\d{4})", block)
                        cancelled = re.search(r"Fecha Cancela:\s*(\d{2}/[A-Z]{3}/\d{4})", block)
                        month_map = {"ENE":"JAN", "ABR":"APR", "AGO":"AUG", "DIC":"DEC"}
                        def super_date(match):
                            if not match:
                                return pd.NaT
                            value = match.group(1)
                            for es, en in month_map.items():
                                value = value.replace(es, en)
                            return pd.to_datetime(value, format="%d/%b/%Y", errors="coerce")
                        for line in block.splitlines():
                            if not re.match(r"^\d{2}[A-Z]", line):
                                continue
                            match = re.search(r"\*?\s*(\d{13})\s+(\d+)\s+\d+\.\d+.*?\s+(\d+(?:\.\d+)?)$", line)
                            if not match:
                                errors.append(f"{filename}: no pude leer una línea de Supermaxi: {line[:85]}")
                                continue
                            barcode, uxc, packs = match.groups()
                            packs, uxc = float(packs), float(uxc)
                            description = line[2:match.start()].strip()
                            rows.append({"Cliente": "Supermaxi", "Orden": canonical_order(order_id), "Fecha pedido": super_date(elaborated), "Fecha inicio": super_date(valid), "Fecha límite": super_date(cancelled), "Producto en pedido": description, "Referencia cliente": barcode, "Cajas": packs, "Unidades por caja": uxc, "Unidades pedidas": packs * uxc, "Código SKU": resolve_barcode(barcode, uxc), "Archivo": filename})
                            found += 1
            if not found:
                errors.append(f"{filename}: no reconocí líneas de pedido. Revise el formato del PDF.")
    finally:
        for _, pdf in documents:
            pdf.close()
    result = pd.DataFrame(rows)
    if not result.empty:
        result["Revisión"] = result.apply(
            lambda line: "Código de barras ambiguo: revise SKU" if not line["Código SKU"] and line["Referencia cliente"] in ambiguous_barcodes
            else "SKU no identificado: complete código" if not line["Código SKU"]
            else "Código completado con cero inicial: confirme producto" if len(line["Referencia cliente"]) == 7 and line["Código SKU"] == line["Referencia cliente"].zfill(8)
            else "Empatado por unidades por caja: confirme SKU" if line["Referencia cliente"] in ambiguous_barcodes
            else "Confirme código y unidades", axis=1,
        )
    return result, errors


def analyze_weekly_order(order, forecast, stock, invoices):
    billed = invoices[["code", "invoice", "quantity"]].copy()
    billed["type"] = billed["invoice"].astype(str).map(
        lambda number: "export" if number.startswith("001-901-") else "normal" if number.startswith("001-100-") else "other"
    )
    sold = billed.pivot_table(index="code", columns="type", values="quantity", aggfunc="sum", fill_value=0).reset_index()
    for invoice_type in ["normal", "export", "other"]:
        if invoice_type not in sold:
            sold[invoice_type] = 0
    # La exportación sigue visible en el detalle, pero no reduce el forecast.
    sold["sold"] = sold[["normal", "other"]].sum(axis=1)
    names = pd.concat([forecast[["code", "product"]], stock[["code", "product"]]]).drop_duplicates("code")
    result = order.merge(forecast[["code", "forecast"]], on="code", how="left")
    result = result.merge(stock[["code", "available"]], on="code", how="left")
    result = result.merge(sold, on="code", how="left")
    result = result.merge(names, on="code", how="left")
    result["available"] = pd.to_numeric(result["available"], errors="coerce").fillna(0).clip(lower=0)
    for column in ["normal", "export", "other", "sold"]:
        result[column] = pd.to_numeric(result[column], errors="coerce").fillna(0)
    result["forecast_remaining"] = (result["forecast"] - result["sold"]).clip(lower=0)
    result["fits_forecast"] = result[["ordered", "forecast_remaining"]].fillna(0).min(axis=1)
    result["outside_forecast"] = result["ordered"] - result["fits_forecast"]
    result["forecast_after_order"] = (result["forecast_remaining"] - result["ordered"]).clip(lower=0)
    result["status"] = result.apply(
        lambda row: "Sin forecast" if pd.isna(row["forecast"])
        else "Supera el forecast" if row["outside_forecast"] > 0
        else "Cabe en el forecast", axis=1,
    )
    result["product"] = result["product"].fillna("Producto no identificado")
    return result.sort_values(["outside_forecast", "ordered"], ascending=False).reset_index(drop=True)


def reconcile_fill_rate(orders, invoices, as_of):
    if orders.empty:
        return pd.DataFrame(), pd.DataFrame()
    order_lines = orders.copy()
    order_lines["Orden"] = order_lines["Orden"].map(canonical_order)
    order_lines["Código SKU"] = order_lines["Código SKU"].map(norm_code)
    order_lines["Unidades pedidas"] = pd.to_numeric(order_lines["Unidades pedidas"], errors="coerce").fillna(0)
    order_lines = order_lines[order_lines["Código SKU"].notna() & (order_lines["Unidades pedidas"] > 0)]
    keys = ["Cliente", "Orden", "Código SKU"]
    line_summary = order_lines.groupby(keys, as_index=False).agg(
        Fecha_pedido=("Fecha pedido", "min"), Fecha_inicio=("Fecha inicio", "min"), Fecha_limite=("Fecha límite", "max"),
        Producto=("Producto en pedido", "first"), Unidades_pedidas=("Unidades pedidas", "sum"),
    )
    billed = invoices.copy()
    if "purchase_order" not in billed:
        billed["purchase_order"] = ""
    billed["Orden"] = billed["purchase_order"].map(canonical_order)
    billed["Código SKU"] = billed["code"].map(norm_code)
    billed = billed[billed["Orden"].ne("")]
    billed_summary = billed.groupby(["Orden", "Código SKU"], as_index=False).agg(
        Unidades_facturadas=("quantity", "sum"), Facturas=("invoice", "nunique"), Ultima_factura=("date", "max")
    )
    detail = line_summary.merge(billed_summary, on=["Orden", "Código SKU"], how="left")
    detail["Unidades_facturadas"] = pd.to_numeric(detail["Unidades_facturadas"], errors="coerce").fillna(0)
    detail["Facturas"] = pd.to_numeric(detail["Facturas"], errors="coerce").fillna(0)
    detail["Unidades_cumplidas"] = detail[["Unidades_pedidas", "Unidades_facturadas"]].min(axis=1)
    detail["Pendiente"] = (detail["Unidades_pedidas"] - detail["Unidades_cumplidas"]).clip(lower=0)
    detail["Fill_rate"] = detail["Unidades_cumplidas"] / detail["Unidades_pedidas"].replace(0, pd.NA)
    today = pd.Timestamp(as_of).normalize()
    detail["Estado_plazo"] = detail["Fecha_limite"].map(
        lambda date: "Fecha sin identificar" if pd.isna(date) else "En plazo" if pd.Timestamp(date).normalize() >= today else "Vencida"
    )
    detail["Resultado"] = detail.apply(
        lambda row: "Completa" if row["Pendiente"] <= 0 else "En plazo" if row["Estado_plazo"] == "En plazo" else "Incumplida" if row["Estado_plazo"] == "Vencida" else "Revisar", axis=1
    )
    summary = detail.groupby(["Cliente", "Orden"], as_index=False).agg(
        Fecha_pedido=("Fecha_pedido", "min"), Fecha_inicio=("Fecha_inicio", "min"), Fecha_limite=("Fecha_limite", "max"),
        Pedidas=("Unidades_pedidas", "sum"), Facturadas=("Unidades_cumplidas", "sum"), Pendientes=("Pendiente", "sum"),
        Productos=("Código SKU", "nunique"), Productos_completos=("Pendiente", lambda values: int((values <= 0).sum())),
        Facturas=("Facturas", "sum"), Estado_plazo=("Estado_plazo", "first"),
    )
    summary["Fill_rate"] = summary["Facturadas"] / summary["Pedidas"].replace(0, pd.NA)
    summary["Resultado"] = summary.apply(
        lambda row: "Completa" if row["Pendientes"] <= 0 else "En plazo" if row["Estado_plazo"] == "En plazo" else "Incumplida" if row["Estado_plazo"] == "Vencida" else "Revisar", axis=1
    )
    return summary.sort_values(["Fecha_limite", "Cliente"], ascending=[False, True]), detail


def forecast_signal(row):
    if pd.isna(row["forecast"]) or row["outside_forecast"] > 0 or row["forecast_after_order"] <= 0:
        return "🔴 Sin forecast"
    if row["forecast_after_order"] <= row["forecast"] * 0.20:
        return "🟡 Queda poco"
    return "🟢 Hay margen"


def validate_bundle(forecast_data, stock_data, invoice_data, new_invoice_data=None):
    errors, warnings = [], []
    if forecast_data.empty:
        errors.append("El forecast no contiene productos.")
    if forecast_data["code"].isna().any() or forecast_data["code"].duplicated().any():
        errors.append("El forecast tiene códigos vacíos o duplicados.")
    if stock_data.empty:
        errors.append("El stock no contiene productos.")
    if stock_data["code"].isna().any() or stock_data["code"].duplicated().any():
        errors.append("El stock tiene códigos vacíos o duplicados.")
    if invoice_data.empty:
        errors.append("No se pudieron leer líneas de producto en las facturas.")
    required_invoice = {"invoice", "date", "code", "quantity", "net_sales"}
    if not required_invoice.issubset(invoice_data.columns):
        errors.append("Las facturas no contienen todos los campos necesarios.")
    elif invoice_data[list(required_invoice)].isna().any().any():
        errors.append("Existen líneas de factura incompletas.")
    if new_invoice_data is not None and not new_invoice_data.empty:
        master_codes = set(forecast_data["code"]) | set(stock_data["code"])
        outside = sorted(set(new_invoice_data["code"]) - master_codes)
        if outside:
            warnings.append(f"{len(outside)} códigos facturados no están en forecast ni stock: {', '.join(outside[:8])}")
    return errors, warnings


if st.session_state.get("app_data_version") != APP_DATA_VERSION:
    st.session_state.active_bundle = blank_bundle()
    st.session_state.app_data_version = APP_DATA_VERSION
    for stale_key in ["validated_bundle", "validation_summary", "validated_signature", "reset_pending"]:
        st.session_state.pop(stale_key, None)
if "upload_generation" not in st.session_state:
    st.session_state.upload_generation = 0
if "current_weekly_order" not in st.session_state:
    st.session_state.current_weekly_order = st.session_state.active_bundle.get("weekly_order", pd.DataFrame(columns=["code", "ordered"])).copy()
if "storage_command" not in st.session_state:
    st.session_state.storage_command = {"action": "load", "revision": "initial", "payload": ""}
command = st.session_state.storage_command
storage_result = browser_store(**command, key="saved_commercial_cut")
if isinstance(storage_result, dict):
    if storage_result.get("action") == "loaded" and not st.session_state.get("storage_loaded"):
        st.session_state.storage_loaded = True
        if storage_result.get("payload"):
            try:
                st.session_state.active_bundle = unpack_bundle(storage_result["payload"])
                st.session_state.current_weekly_order = st.session_state.active_bundle.get("weekly_order", pd.DataFrame(columns=["code", "ordered"])).copy()
                st.session_state.storage_notice = "Se recuperó el último corte guardado en este navegador."
            except Exception as exc:
                st.session_state.storage_notice = f"No pude recuperar la copia del navegador: {exc}"
        elif not st.session_state.active_bundle["forecast"].empty:
            st.session_state.storage_command = {"action": "save", "revision": uuid.uuid4().hex, "payload": pack_bundle(st.session_state.active_bundle)}
        st.rerun()
    elif storage_result.get("action") in ["saved", "cleared"] and storage_result.get("revision") == command["revision"]:
        st.session_state.storage_command = {"action": "load", "revision": command["revision"], "payload": ""}
        st.session_state.storage_notice = "Corte guardado en este navegador." if storage_result["action"] == "saved" else "Copia guardada eliminada."
    elif storage_result.get("action") == "error":
        st.session_state.storage_notice = f"No se pudo guardar en este navegador: {storage_result.get('message', 'error desconocido')}"

# Rentabilidad uses a separate browser record and never reads or mutates the
# forecast/stock/fill-rate bundle above.
if "profit_store" not in st.session_state:
    st.session_state.profit_store = blank_profit_store()
if "profit_storage_command" not in st.session_state:
    st.session_state.profit_storage_command = {"action": "load", "revision": "initial-profit", "payload": ""}
profit_command = st.session_state.profit_storage_command
profit_storage_result = browser_store(**profit_command, key="saved_profitability_cuts")
if isinstance(profit_storage_result, dict):
    if profit_storage_result.get("action") == "loaded" and not st.session_state.get("profit_storage_loaded"):
        st.session_state.profit_storage_loaded = True
        if profit_storage_result.get("payload"):
            try:
                st.session_state.profit_store = unpack_profit_store(profit_storage_result["payload"])
                st.session_state.profit_notice = "Se recuperó el histórico de Rentabilidad guardado en este navegador."
            except Exception as exc:
                st.session_state.profit_notice = f"No pude recuperar Rentabilidad: {exc}"
        st.rerun()
    elif profit_storage_result.get("action") == "saved" and profit_storage_result.get("revision") == profit_command["revision"]:
        st.session_state.profit_storage_command = {"action": "load", "revision": profit_command["revision"], "payload": ""}
        st.session_state.profit_notice = "Rentabilidad guardada en este navegador."
    elif profit_storage_result.get("action") == "error":
        st.session_state.profit_notice = f"No se pudo guardar Rentabilidad: {profit_storage_result.get('message', 'error desconocido')}"

with st.sidebar:
    st.header("Actualizar información")
    st.caption("El corte validado se guarda en este navegador y vuelve al refrescar. No se comparte con otros usuarios. Conserve también sus archivos originales.")
    if st.session_state.get("storage_notice"):
        st.info(st.session_state.pop("storage_notice"))
    has_current_data = not st.session_state.active_bundle["forecast"].empty
    if has_current_data:
        st.caption("Puede subir solo las facturas nuevas. El forecast y el stock actuales se conservan si no carga otros archivos.")
    else:
        st.info("Nuevo mes: cargue forecast, stock y facturas para comenzar.")

    if st.button("🗓️ Iniciar nuevo mes", use_container_width=True):
        st.session_state.reset_pending = True
    if st.session_state.get("reset_pending"):
        st.warning("Esto quitará de la pantalla todos los datos actuales.")
        confirm_col, cancel_col = st.columns(2)
        if confirm_col.button("Sí, dejar en blanco", type="primary", use_container_width=True):
            st.session_state.active_bundle = blank_bundle()
            st.session_state.current_weekly_order = pd.DataFrame(columns=["code", "ordered"])
            st.session_state.pop("current_weekly_order_signature", None)
            st.session_state.storage_loaded = True
            st.session_state.storage_command = {"action": "clear", "revision": uuid.uuid4().hex, "payload": ""}
            st.session_state.upload_generation += 1
            for key in ["validated_bundle", "validation_summary", "validated_signature", "reset_pending"]:
                st.session_state.pop(key, None)
            st.rerun()
        if cancel_col.button("Cancelar", use_container_width=True):
            st.session_state.pop("reset_pending", None)
            st.rerun()

    generation = st.session_state.upload_generation
    forecast_file = st.file_uploader("Forecast (.xlsx)", type="xlsx", key=f"forecast_{generation}")
    stock_file = st.file_uploader("Stock (.xlsx)", type="xlsx", key=f"stock_{generation}")
    pdf_file = st.file_uploader("Facturas nuevas (.pdf)", type="pdf", key=f"pdf_{generation}")
    signature = (file_signature(forecast_file), file_signature(stock_file), file_signature(pdf_file))
    if st.session_state.get("validated_signature") != signature:
        st.session_state.pop("validated_bundle", None)
        st.session_state.pop("validation_summary", None)

    if st.button("Prevalidar información", type="primary", use_container_width=True):
        try:
            active = st.session_state.active_bundle
            candidate_forecast = read_forecast(forecast_file) if forecast_file else active["forecast"].copy()
            if stock_file:
                candidate_stock, candidate_lots = read_stock(stock_file)
            else:
                candidate_stock, candidate_lots = active["stock"].copy(), active["lots"].copy()

            new_invoices = read_pdf(pdf_file) if pdf_file else pd.DataFrame(columns=active["invoices"].columns)
            # Put the newly parsed copy first so a repeated invoice can enrich an
            # older saved line with its purchase-order number instead of losing it.
            combined_invoices = pd.concat([new_invoices, active["invoices"]], ignore_index=True)
            duplicate_key = ["invoice", "date", "code", "quantity", "net_sales"]
            duplicated = int(combined_invoices.duplicated(duplicate_key).sum())
            combined_invoices = combined_invoices.drop_duplicates(duplicate_key, keep="first")
            cutoff_candidate = pd.to_datetime(combined_invoices["date"]).max() if not combined_invoices.empty else pd.NaT
            errors, warnings = validate_bundle(candidate_forecast, candidate_stock, combined_invoices, new_invoices)

            st.session_state.validated_signature = signature
            st.session_state.validation_summary = {
                "errors": errors,
                "warnings": warnings,
                "new_lines": len(new_invoices),
                "new_invoices": int(new_invoices["invoice"].nunique()) if not new_invoices.empty else 0,
                "duplicates": duplicated,
                "date_min": new_invoices["date"].min() if not new_invoices.empty else None,
                "date_max": new_invoices["date"].max() if not new_invoices.empty else None,
                "cutoff": cutoff_candidate,
            }
            if not errors:
                st.session_state.validated_bundle = {"forecast": candidate_forecast, "stock": candidate_stock, "lots": candidate_lots, "invoices": combined_invoices, "fill_rate_invoices": active.get("fill_rate_invoices", blank_bundle()["fill_rate_invoices"]).copy(), "orders": active.get("orders", blank_bundle()["orders"]).copy(), "weekly_order": active.get("weekly_order", pd.DataFrame(columns=["code", "ordered"])).copy(), "cutoff": cutoff_candidate}
        except Exception as exc:
            st.session_state.validated_signature = signature
            st.session_state.validation_summary = {"errors": [f"No pude leer los archivos: {exc}"], "warnings": [], "new_lines": 0, "new_invoices": 0, "duplicates": 0, "date_min": None, "date_max": None, "cutoff": None}
            st.session_state.pop("validated_bundle", None)

    summary = st.session_state.get("validation_summary")
    if summary:
        if summary["errors"]:
            st.error("Prevalidación no aprobada")
            for message in summary["errors"]:
                st.write(f"• {message}")
        else:
            st.success("Información conforme")
            if summary["new_lines"]:
                st.write(f"**{summary['new_invoices']}** facturas y **{summary['new_lines']}** líneas leídas.")
                st.write(f"Fechas: **{pd.Timestamp(summary['date_min']).strftime('%d-%b-%Y')}** a **{pd.Timestamp(summary['date_max']).strftime('%d-%b-%Y')}**.")
            else:
                st.write("No se cargaron facturas nuevas.")
            if summary.get("cutoff") is not None and not pd.isna(summary["cutoff"]):
                st.write(f"**Fecha de corte que usará el dashboard: {pd.Timestamp(summary['cutoff']).strftime('%d/%m/%Y')}**")
            if summary["duplicates"]:
                st.info(f"Se encontraron {summary['duplicates']} líneas ya cargadas. No se duplicarán.")
            for message in summary["warnings"]:
                st.warning(message)

    can_update = "validated_bundle" in st.session_state and not (summary or {}).get("errors")
    if st.button("Cargar y actualizar dashboard", disabled=not can_update, use_container_width=True):
        st.session_state.active_bundle = st.session_state.validated_bundle
        st.session_state.storage_loaded = True
        st.session_state.storage_command = {"action": "save", "revision": uuid.uuid4().hex, "payload": pack_bundle(st.session_state.active_bundle)}
        st.session_state.pop("validated_bundle", None)
        st.session_state.pop("validation_summary", None)
        st.success("Dashboard actualizado.")
        st.rerun()

active = st.session_state.active_bundle
forecast_df = active["forecast"]
stock_df = active["stock"]
lots_df = active["lots"]
invoice_df = active["invoices"]
fill_rate_invoice_df = active.get("fill_rate_invoices", blank_bundle()["fill_rate_invoices"])
orders_df = active.get("orders", blank_bundle()["orders"])

if forecast_df.empty or stock_df.empty or invoice_df.empty:
    st.markdown('<div class="hero"><h1>Control comercial</h1><p>Comience un nuevo mes cargando la información.</p></div>', unsafe_allow_html=True)
    st.title("La aplicación está en blanco")
    st.write("Para crear el análisis del mes, cargue en el panel izquierdo:")
    st.write("1. **Forecast del mes** en Excel")
    st.write("2. **Stock disponible** en Excel")
    st.write("3. **Facturas del mes** en PDF")
    st.info("Después pulse **Prevalidar información**. Si los datos están conformes, se habilitará **Cargar y actualizar dashboard**.")
    st.stop()

cutoff = pd.Timestamp(active["cutoff"])
month_names = {1:"enero", 2:"febrero", 3:"marzo", 4:"abril", 5:"mayo", 6:"junio", 7:"julio", 8:"agosto", 9:"septiembre", 10:"octubre", 11:"noviembre", 12:"diciembre"}
source_label = f"Corte actual: {cutoff.day} de {month_names[cutoff.month]} de {cutoff.year}"

analysis, elapsed = build_analysis(forecast_df, stock_df, invoice_df, cutoff)
sold_forecast = analysis.loc[analysis["forecast"].notna(), "sold"].sum()
forecast_total = analysis["forecast"].fillna(0).sum()

st.markdown(f'<div class="hero"><h1>Control comercial</h1><p>{source_label}. Vea qué pasa y qué hacer.</p></div>', unsafe_allow_html=True)

# KPIs ejecutivos del resumen: venta, forecast asegurado y riesgo de inventario.
summary_analysis = analysis.copy()
summary_weekly_order = st.session_state.get(
    "current_weekly_order",
    pd.DataFrame(columns=["code", "ordered"]),
)
if not summary_weekly_order.empty:
    summary_analysis = summary_analysis.merge(
        summary_weekly_order.rename(columns={"ordered": "upcoming_order"}),
        on="code",
        how="left",
    )
else:
    summary_analysis["upcoming_order"] = 0

summary_analysis["upcoming_order"] = pd.to_numeric(
    summary_analysis["upcoming_order"], errors="coerce"
).fillna(0)
summary_analysis["available"] = pd.to_numeric(
    summary_analysis["available"], errors="coerce"
).fillna(0)
summary_analysis["sold"] = pd.to_numeric(
    summary_analysis["sold"], errors="coerce"
).fillna(0)
summary_analysis["forecast"] = pd.to_numeric(
    summary_analysis["forecast"], errors="coerce"
)

forecast_rows = summary_analysis[summary_analysis["forecast"].notna()].copy()
forecast_rows["secured_units"] = (
    forecast_rows["sold"] + forecast_rows["upcoming_order"]
).clip(lower=0)
forecast_rows["secured_units"] = forecast_rows[
    ["secured_units", "forecast"]
].min(axis=1)
forecast_secured_rate = (
    forecast_rows["secured_units"].sum() / forecast_total
    if forecast_total else 0
)

summary_analysis["confirmed_uncovered"] = (
    summary_analysis["upcoming_order"] - summary_analysis["available"]
).clip(lower=0)
summary_analysis["summary_critical"] = (
    summary_analysis["confirmed_uncovered"].gt(0)
    | (
        summary_analysis["available"].le(0)
        & summary_analysis["remaining"].gt(0)
    )
)
summary_critical_skus = int(summary_analysis["summary_critical"].sum())
summary_missing_units = float(summary_analysis["missing_stock"].sum())

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Forecast del mes", f"{forecast_total:,.0f} un.")
c2.metric(
    "Facturado contra forecast",
    f"{sold_forecast:,.0f} un.",
    help="No incluye facturas de exportación 001-901.",
)
c3.metric(
    "Avance",
    f"{sold_forecast / forecast_total:.1%}" if forecast_total else "0,0%",
    f"{sold_forecast / forecast_total - elapsed:+.1%} vs. tiempo" if forecast_total else None,
)
c4.metric(
    "Forecast asegurado",
    f"{forecast_secured_rate:.1%}",
    help="Porcentaje del forecast ya cubierto por facturación + pedidos confirmados, limitado al forecast de cada SKU.",
)
c5.metric(
    "🔴 SKU críticos",
    f"{summary_critical_skus:,}",
    help="SKU con pedido confirmado sin cobertura suficiente o sin stock mientras aún falta forecast por vender.",
)
c6.metric(
    "⚠️ Faltante de stock",
    f"{summary_missing_units:,.0f} un.",
    help="Unidades adicionales requeridas para tener capacidad de cumplir el forecast pendiente.",
)
if sold_forecast / forecast_total > elapsed:
    st.success(f"En general vamos adelantados: se vendió {sold_forecast / forecast_total:.1%} del forecast y ha pasado {elapsed:.1%} del mes.")
else:
    st.warning(f"En general vamos atrasados: se vendió {sold_forecast / forecast_total:.1%} del forecast y ha pasado {elapsed:.1%} del mes.")

tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs(["Resumen", "Qué hacer", "Todos los productos", "Facturas", "Ventas por cliente", "Pedido semanal", "Fill Rate", "Rentabilidad"])

with tab1:
    left, right = st.columns([1, 1])
    with left:
        progress = pd.DataFrame({
            "Indicador": ["Mes transcurrido", "Forecast facturado", "Forecast asegurado"],
            "Porcentaje": [
                elapsed,
                sold_forecast / forecast_total if forecast_total else 0,
                forecast_secured_rate,
            ],
        })
        fig = px.bar(
            progress,
            x="Indicador",
            y="Porcentaje",
            text_auto=".0%",
            color="Indicador",
            color_discrete_sequence=["#9fbad0", "#1f6d8c", "#43aa8b"],
        )
        fig.update_layout(
            title="¿Vamos al ritmo correcto y cuánto ya está asegurado?",
            showlegend=False,
            yaxis_tickformat=".0%",
            yaxis_range=[0, max(.5, progress["Porcentaje"].max() * 1.25)],
            height=360,
        )
        st.plotly_chart(fig, width="stretch")

    with right:
        counts = analysis["status"].value_counts().rename_axis("Estado").reset_index(name="Productos")
        fig = px.bar(
            counts,
            x="Productos",
            y="Estado",
            orientation="h",
            color="Estado",
            color_discrete_map=COLORS,
            text_auto=True,
        )
        fig.update_layout(
            title="Estado del portafolio",
            showlegend=False,
            height=360,
            yaxis={"categoryorder": "total ascending"},
        )
        st.plotly_chart(fig, width="stretch")

    st.subheader("Atención inmediata · Top 5")
    st.caption("Los productos que requieren acción primero por pedidos sin cobertura, falta de stock o riesgo de incumplir el forecast.")

    urgent = summary_analysis.copy()
    urgent["attention_rank"] = 3
    urgent.loc[urgent["missing_stock"].gt(0), "attention_rank"] = 2
    urgent.loc[
        urgent["available"].le(0) & urgent["remaining"].gt(0),
        "attention_rank",
    ] = 1
    urgent.loc[urgent["confirmed_uncovered"].gt(0), "attention_rank"] = 0
    urgent["attention_units"] = urgent[
        ["confirmed_uncovered", "missing_stock", "remaining"]
    ].max(axis=1)
    urgent = urgent[
        urgent["attention_rank"].lt(3)
    ].sort_values(
        ["attention_rank", "attention_units"],
        ascending=[True, False],
    ).head(5)

    if urgent.empty:
        st.success("No hay alertas críticas de abastecimiento en este corte.")
    else:
        for _, row in urgent.iterrows():
            if row["confirmed_uncovered"] > 0:
                headline = "🔴 Pedido sin cobertura"
                recommendation = (
                    f"Cubrir {row['confirmed_uncovered']:,.0f} unidades de pedidos confirmados."
                )
            elif row["available"] <= 0 and row["remaining"] > 0:
                headline = "🔴 Sin stock"
                recommendation = f"Reponer {row['remaining']:,.0f} unidades."
            else:
                headline = "🟠 Falta stock"
                recommendation = (
                    f"Reponer {row['missing_stock']:,.0f} unidades para poder cumplir el forecast."
                )

            with st.container(border=True):
                st.markdown(f"**{headline} · {row['code']}** — {row['product']}")
                st.caption(
                    f"Forecast {row.forecast:,.0f} · Facturado {row.sold:,.0f} · "
                    f"Pedido por venir {row.upcoming_order:,.0f} · Stock {row.available:,.0f}"
                )
                st.markdown(f"**{recommendation}**")

with tab2:
    st.subheader("Centro de alertas comerciales")
    st.caption("Prioriza abastecimiento, pedidos confirmados, ritmo de venta y stock para decidir qué atender primero esta semana.")

    alerts = analysis.copy()
    upcoming_order = st.session_state.get(
        "current_weekly_order",
        pd.DataFrame(columns=["code", "ordered"]),
    )

    if not upcoming_order.empty:
        alerts = alerts.merge(
            upcoming_order.rename(columns={"ordered": "upcoming_order"}),
            on="code",
            how="left",
        )
    else:
        alerts["upcoming_order"] = 0

    for column in ["upcoming_order", "available", "sold", "remaining", "missing_stock"]:
        alerts[column] = pd.to_numeric(alerts[column], errors="coerce").fillna(0)
    alerts["forecast"] = pd.to_numeric(alerts["forecast"], errors="coerce")

    alerts["forecast_after_upcoming"] = (
        alerts["forecast"].fillna(0)
        - alerts["sold"]
        - alerts["upcoming_order"]
    )
    alerts["forecast_unsecured"] = alerts["forecast_after_upcoming"].clip(lower=0)
    alerts["confirmed_uncovered"] = (
        alerts["upcoming_order"] - alerts["available"]
    ).clip(lower=0)

    alerts["is_slow"] = (
        alerts["forecast"].fillna(0).gt(0)
        & alerts["advance"].notna()
        & alerts["advance"].lt(elapsed - 0.10)
        & alerts["sold"].lt(alerts["forecast"].fillna(0))
    )

    def commercial_priority(row):
        if row["confirmed_uncovered"] > 0:
            return "CRÍTICO"
        if row["available"] <= 0 and row["remaining"] > 0:
            return "CRÍTICO"
        if row["missing_stock"] > 0:
            return "ALTO"
        if row["is_slow"] and row["forecast_unsecured"] > 0:
            return "ALTO"
        if row["status"] in ["Hay que venderlo", "Más rápido", "Revisar dato"]:
            return "MEDIO"
        return "BAJO"

    alerts["commercial_priority"] = alerts.apply(commercial_priority, axis=1)
    priority_order = {"CRÍTICO": 0, "ALTO": 1, "MEDIO": 2, "BAJO": 3}
    alerts["priority_rank"] = alerts["commercial_priority"].map(priority_order).fillna(99)
    alerts["impact_units"] = alerts[
        ["confirmed_uncovered", "missing_stock", "forecast_unsecured", "available"]
    ].max(axis=1)

    def priority_action(row):
        if row["confirmed_uncovered"] > 0:
            return (
                f"URGENTE: cubrir {row['confirmed_uncovered']:,.0f} unidades "
                "de pedidos confirmados sin stock suficiente."
            )
        if row["available"] <= 0 and row["remaining"] > 0:
            return f"Sin stock. Reponer {row['remaining']:,.0f} unidades."
        if row["missing_stock"] > 0:
            return (
                f"Reponer {row['missing_stock']:,.0f} unidades "
                "para poder cumplir el forecast."
            )
        if row["is_slow"] and row["forecast_unsecured"] > 0:
            return (
                f"Impulsar venta. Quedan {row['forecast_unsecured']:,.0f} unidades "
                "del forecast sin asegurar."
            )
        if row["status"] == "Hay que venderlo":
            return (
                f"Activar venta/promoción. Hay {row['available']:,.0f} unidades "
                "en inventario."
            )
        if row["status"] == "Más rápido":
            return (
                "Venta por encima del ritmo. Monitorear inventario y evitar "
                "promoción agresiva."
            )
        if row["status"] == "Meta cumplida":
            return "Meta cumplida. No requiere impulso comercial."
        return row["action"]

    alerts["priority_action"] = alerts.apply(priority_action, axis=1)

    critical_skus = int((alerts["commercial_priority"] == "CRÍTICO").sum())
    uncovered_units = float(alerts["confirmed_uncovered"].sum())
    missing_units = float(alerts["missing_stock"].sum())
    slow_skus = int(alerts["is_slow"].sum())
    push_skus = int((alerts["status"] == "Hay que venderlo").sum())

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("🔴 SKU críticos", f"{critical_skus:,}")
    k2.metric("📦 Pedidos sin cobertura", f"{uncovered_units:,.0f} un.")
    k3.metric("⚠️ Faltante para forecast", f"{missing_units:,.0f} un.")
    k4.metric("📉 SKU atrasados", f"{slow_skus:,}")
    k5.metric("📣 Hay que vender", f"{push_skus:,}")

    st.divider()
    st.subheader("Prioridades de esta semana")

    top_alerts = alerts[
        alerts["commercial_priority"].isin(["CRÍTICO", "ALTO"])
    ].copy()
    top_alerts = top_alerts.sort_values(
        ["priority_rank", "impact_units"],
        ascending=[True, False],
    ).head(10)

    priority_labels = {
        "CRÍTICO": "🔴 CRÍTICO",
        "ALTO": "🟠 ALTO",
        "MEDIO": "🟡 MEDIO",
        "BAJO": "🟢 BAJO",
    }

    if top_alerts.empty:
        st.success("No hay alertas críticas o altas en este corte.")
    else:
        top_alerts["Prioridad"] = top_alerts["commercial_priority"].map(priority_labels)
        top_show = top_alerts[
            [
                "Prioridad", "code", "product", "status", "forecast", "sold",
                "upcoming_order", "available", "confirmed_uncovered", "missing_stock",
                "priority_action",
            ]
        ].rename(
            columns={
                "code": "Código",
                "product": "Producto",
                "status": "Estado",
                "forecast": "Forecast",
                "sold": "Facturado",
                "upcoming_order": "Pedido por venir",
                "available": "Stock",
                "confirmed_uncovered": "Pedido sin cobertura",
                "missing_stock": "Stock faltante",
                "priority_action": "Acción inmediata",
            }
        )
        st.dataframe(
            top_show,
            width="stretch",
            hide_index=True,
            column_config={
                "Código": st.column_config.TextColumn(),
                "Producto": st.column_config.TextColumn(width="large"),
                "Forecast": st.column_config.NumberColumn(format="%,.0f"),
                "Facturado": st.column_config.NumberColumn(format="%,.0f"),
                "Pedido por venir": st.column_config.NumberColumn(format="%,.0f"),
                "Stock": st.column_config.NumberColumn(format="%,.0f"),
                "Pedido sin cobertura": st.column_config.NumberColumn(format="%,.0f"),
                "Stock faltante": st.column_config.NumberColumn(format="%,.0f"),
                "Acción inmediata": st.column_config.TextColumn(width="large"),
            },
        )

    st.divider()
    st.subheader("Detalle de alertas")

    f1, f2 = st.columns(2)
    with f1:
        selected_priorities = st.multiselect(
            "Prioridad",
            ["CRÍTICO", "ALTO", "MEDIO", "BAJO"],
            default=["CRÍTICO", "ALTO", "MEDIO"],
            key="action_priority_filter",
        )
    with f2:
        selected_status = st.multiselect(
            "Estado",
            list(COLORS),
            default=[
                "No hay stock", "Falta stock", "Va lento",
                "Hay que venderlo", "Más rápido",
            ],
            key="action_status_filter",
        )

    search_action = st.text_input(
        "Buscar código o producto",
        key="action_search",
    )

    detail = alerts[
        alerts["commercial_priority"].isin(selected_priorities)
        & alerts["status"].isin(selected_status)
    ].copy()

    if search_action:
        detail = detail[
            detail["code"].str.contains(search_action, case=False, na=False)
            | detail["product"].str.contains(search_action, case=False, na=False)
        ]

    detail = detail.sort_values(
        ["priority_rank", "impact_units"],
        ascending=[True, False],
    )
    detail["Prioridad"] = detail["commercial_priority"].map(priority_labels)
    detail_show = detail[
        [
            "Prioridad", "status", "code", "product", "forecast", "sold",
            "upcoming_order", "forecast_unsecured", "available",
            "confirmed_uncovered", "missing_stock", "priority_action",
        ]
    ].rename(
        columns={
            "status": "Estado",
            "code": "Código",
            "product": "Producto",
            "forecast": "Forecast",
            "sold": "Facturado",
            "upcoming_order": "Pedido por venir",
            "forecast_unsecured": "Forecast sin asegurar",
            "available": "Stock",
            "confirmed_uncovered": "Pedido sin cobertura",
            "missing_stock": "Stock faltante",
            "priority_action": "Qué hacer",
        }
    )

    st.dataframe(
        detail_show,
        width="stretch",
        hide_index=True,
        column_config={
            "Código": st.column_config.TextColumn(),
            "Producto": st.column_config.TextColumn(width="large"),
            "Forecast": st.column_config.NumberColumn(format="%,.0f"),
            "Facturado": st.column_config.NumberColumn(format="%,.0f"),
            "Pedido por venir": st.column_config.NumberColumn(format="%,.0f"),
            "Forecast sin asegurar": st.column_config.NumberColumn(format="%,.0f"),
            "Stock": st.column_config.NumberColumn(format="%,.0f"),
            "Pedido sin cobertura": st.column_config.NumberColumn(format="%,.0f"),
            "Stock faltante": st.column_config.NumberColumn(format="%,.0f"),
            "Qué hacer": st.column_config.TextColumn(width="large"),
        },
    )

with tab3:
    search = st.text_input("Buscar código o producto")
    full = analysis.copy()
    upcoming_order = st.session_state.current_weekly_order
    if not upcoming_order.empty:
        full = full.merge(upcoming_order.rename(columns={"ordered": "upcoming_order"}), on="code", how="left")
    else:
        full["upcoming_order"] = 0
    full["upcoming_order"] = pd.to_numeric(full["upcoming_order"], errors="coerce").fillna(0)
    full["forecast_after_upcoming"] = full["forecast"].fillna(0) - full["sold"] - full["upcoming_order"]
    if search:
        full = full[full["code"].str.contains(search, case=False, na=False) | full["product"].str.contains(search, case=False, na=False)]
    full["Avance"] = full["advance"].map(
        lambda value: "—" if pd.isna(value) else f"{value:.1%}".replace(".", ",").replace("%", " %")
    )
    show = full[["code", "product", "forecast", "sold", "upcoming_order", "forecast_after_upcoming", "Avance", "available", "status", "action"]].rename(columns={"code":"Código", "product":"Producto", "forecast":"Forecast", "sold":"Facturado", "upcoming_order":"Pedido por venir", "forecast_after_upcoming":"Saldo tras pedido", "available":"Stock", "status":"Estado", "action":"Qué hacer"})
    styled_show = show.style.apply(
        lambda column: ["background-color: #e8f3ff; color: #174f7a; font-weight: 700" if show["Pedido por venir"].iloc[position] > 0 else "" for position in range(len(column))],
        subset=["Pedido por venir", "Saldo tras pedido"],
    )
    st.caption("Pedido por venir = pedidos cargados y confirmados en la pestaña Pedido semanal. Saldo tras pedido = forecast − facturado − pedido.")
    st.dataframe(styled_show, width="stretch", hide_index=True, column_config={"Código": st.column_config.TextColumn(), "Producto": st.column_config.TextColumn(width="large"), "Avance": st.column_config.TextColumn(width="small"), "Forecast": st.column_config.NumberColumn(format="%,.0f"), "Facturado": st.column_config.NumberColumn(format="%,.0f"), "Pedido por venir": st.column_config.NumberColumn(format="%,.0f"), "Saldo tras pedido": st.column_config.NumberColumn(format="%,.0f"), "Stock": st.column_config.NumberColumn(format="%,.0f")})

with tab4:
    weekly = invoice_df.groupby("week", as_index=False).agg(Unidades=("quantity", "sum"), Venta_neta=("net_sales", "sum"))
    fig = px.bar(weekly, x="week", y="Unidades", text_auto=",.0f", color_discrete_sequence=["#1f6d8c"])
    fig.update_layout(title="Unidades facturadas por semana", xaxis_title="Semana", yaxis_title="Unidades", height=330)
    st.plotly_chart(fig, width="stretch")
    st.dataframe(invoice_df.sort_values("date", ascending=False), width="stretch", hide_index=True)

with tab5:
    customer_sales = invoice_df.copy()
    customer_sales["customer"] = customer_sales["customer"].fillna("").str.strip().replace("", "Cliente sin identificar")
    invoice_totals = customer_sales.groupby(["customer", "invoice", "date"], as_index=False).agg(
        Unidades=("quantity", "sum"),
        Venta_neta=("net_sales", "sum"),
    )
    customers = invoice_totals.groupby("customer", as_index=False).agg(
        Facturas=("invoice", "nunique"),
        Unidades=("Unidades", "sum"),
        Venta_neta=("Venta_neta", "sum"),
        Ultima_compra=("date", "max"),
    )
    customers["Ticket_promedio"] = customers["Venta_neta"] / customers["Facturas"].replace(0, pd.NA)
    total_customer_sales = customers["Venta_neta"].sum()
    customers["Participacion"] = customers["Venta_neta"] / total_customer_sales if total_customer_sales else 0
    customers = customers.sort_values("Venta_neta", ascending=False).reset_index(drop=True)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Venta neta", f"${total_customer_sales:,.2f}")
    m2.metric("Clientes", f"{customers['customer'].nunique():,.0f}")
    m3.metric("Facturas", f"{invoice_totals['invoice'].nunique():,.0f}")
    average_ticket = total_customer_sales / max(invoice_totals["invoice"].nunique(), 1)
    m4.metric("Ticket promedio", f"${average_ticket:,.2f}")

    st.subheader("¿Quiénes nos están comprando más?")
    top_customers = customers.head(15).sort_values("Venta_neta")
    fig = px.bar(
        top_customers,
        x="Venta_neta",
        y="customer",
        orientation="h",
        text_auto="$.2s",
        color="Venta_neta",
        color_continuous_scale=["#9fbad0", "#1f6d8c"],
    )
    fig.update_layout(height=max(380, len(top_customers) * 34), xaxis_title="Venta neta en dólares", yaxis_title="Cliente", coloraxis_showscale=False)
    st.plotly_chart(fig, width="stretch")

    st.subheader("Resumen por cliente")
    customer_table = customers.rename(columns={
        "customer": "Cliente",
        "Venta_neta": "Venta neta",
        "Ticket_promedio": "Ticket promedio",
        "Participacion": "% de la venta",
        "Ultima_compra": "Última compra",
    }).copy()
    customer_table["% de la venta"] = customer_table["% de la venta"].map(lambda value: f"{value:.1%}")
    st.dataframe(
        customer_table[["Cliente", "Venta neta", "% de la venta", "Facturas", "Ticket promedio", "Unidades", "Última compra"]],
        width="stretch",
        hide_index=True,
        column_config={
            "Venta neta": st.column_config.NumberColumn(format="$%,.2f"),
            "% de la venta": st.column_config.TextColumn(width="small", help="Porcentaje de la venta neta total del mes"),
            "Ticket promedio": st.column_config.NumberColumn(format="$%,.2f"),
            "Unidades": st.column_config.NumberColumn(format="%,.0f"),
            "Última compra": st.column_config.DateColumn(format="DD/MM/YYYY"),
        },
    )

    selected_customer = st.selectbox("Ver las facturas de un cliente", customers["customer"].tolist())
    selected_invoices = invoice_totals[invoice_totals["customer"] == selected_customer].sort_values("date", ascending=False)
    st.dataframe(
        selected_invoices.rename(columns={"invoice": "Factura", "date": "Fecha", "Venta_neta": "Venta neta"})[["Factura", "Fecha", "Unidades", "Venta neta"]],
        width="stretch",
        hide_index=True,
        column_config={"Fecha": st.column_config.DateColumn(format="DD/MM/YYYY"), "Unidades": st.column_config.NumberColumn(format="%,.0f"), "Venta neta": st.column_config.NumberColumn(format="$%,.2f")},
    )

with tab6:
    st.subheader("¿Cabe el pedido de esta semana en el forecast?")
    st.caption("Forecast pendiente = forecast del mes − facturación nacional. Las facturas de exportación (001-901) se muestran aparte y no consumen forecast. Todas las cantidades son unidades individuales.")
    entry_method = st.radio("Cómo ingresar el pedido", ["Subir PDF de clientes", "Foto pedido Coral", "Subir Excel o CSV", "Escribir pedido"], horizontal=True)
    weekly_order = pd.DataFrame(columns=["code", "ordered"])
    if entry_method == "Subir PDF de clientes":
        order_pdfs = st.file_uploader("Pedidos de TIA, El Rosado o Supermaxi (.pdf)", type="pdf", accept_multiple_files=True, key="weekly_order_pdfs")
        st.caption("Puede cargar varios pedidos a la vez. La aplicación convierte cajas × unidades por caja y muestra los SKU para revisión antes de analizar.")
        if order_pdfs:
            try:
                pdf_lines, pdf_errors = parse_order_pdfs(order_pdfs, forecast_df, stock_df)
                for message in pdf_errors:
                    st.error(message)
                if not pdf_lines.empty:
                    st.write(f"Se leyeron **{len(pdf_lines)} líneas** de **{len(order_pdfs)} archivos**; total provisional: **{pdf_lines['Unidades pedidas'].sum():,.0f} unidades**.")
                    st.caption("Rojo = producto cuyo SKU debe corregirse. Verde = SKU identificado. Puede editar 'Código SKU' y 'Unidades pedidas' en la tabla.")
                    review_edits = st.session_state.get("pdf_order_review", {}).get("edited_rows", {})
                    current_codes = pdf_lines["Código SKU"].fillna("").astype(str).str.strip().copy()
                    for row_index, changes in review_edits.items():
                        if "Código SKU" in changes:
                            current_codes.iloc[int(row_index)] = str(changes["Código SKU"] or "").strip()
                    needs_review = current_codes.map(lambda code: not re.fullmatch(r"\d{8}", code))
                    colored_lines = pdf_lines.style.apply(
                        lambda column: [
                            "background-color: #ffe2e2; color: #8b1010; font-weight: 700" if needs_review.iloc[position]
                            else "background-color: #e0f3e7; color: #176238"
                            for position in range(len(column))
                        ],
                        subset=["Producto en pedido", "Revisión"],
                    )
                    edited_lines = st.data_editor(
                        colored_lines, width="stretch", hide_index=True, num_rows="fixed",
                        key="pdf_order_review",
                        disabled=["Cliente", "Orden", "Producto en pedido", "Referencia cliente", "Cajas", "Unidades por caja", "Archivo", "Revisión"],
                        column_config={"Código SKU": st.column_config.TextColumn(help="Código interno de 8 dígitos"), "Unidades pedidas": st.column_config.NumberColumn(min_value=0, step=1)},
                    )
                    codes = edited_lines["Código SKU"].fillna("").astype(str).str.strip()
                    invalid = codes.map(lambda code: not re.fullmatch(r"\d{8}", code))
                    invalid_quantities = pd.to_numeric(edited_lines["Unidades pedidas"], errors="coerce").isna() | (pd.to_numeric(edited_lines["Unidades pedidas"], errors="coerce") <= 0)
                    if invalid.any():
                        st.warning(f"Faltan o son inválidos {int(invalid.sum())} códigos SKU. Corríjalos en la tabla antes de obtener el resultado completo.")
                        st.write("**Productos que necesitan un código:**")
                        for _, pending in edited_lines.loc[invalid, ["Cliente", "Producto en pedido", "Referencia cliente"]].iterrows():
                            st.markdown(f":red[🔴 {pending['Producto en pedido']}] · {pending['Cliente']} · referencia {pending['Referencia cliente']}")
                    if invalid_quantities.any():
                        st.error(f"Revise {int(invalid_quantities.sum())} cantidades vacías o no positivas.")
                    if not invalid.any() and not invalid_quantities.any() and not pdf_errors:
                        weekly_order = prepare_weekly_order(edited_lines, "Código SKU", "Unidades pedidas")
            except Exception as exc:
                st.error(f"No pude leer los PDF de pedidos: {exc}")
    elif entry_method == "Foto pedido Coral":
        coral_image = st.file_uploader(
            "Foto del pedido de Coral (.jpg, .jpeg o .png)",
            type=["jpg", "jpeg", "png"],
            key="coral_order_image",
        )
        st.caption("La app leerá PRODUCTO + CANTIDAD, buscará el SKU más parecido en su forecast/stock y le permitirá corregirlo antes de usar el pedido.")
        if coral_image is not None:
            try:
                coral_lines, coral_ocr = read_coral_order_image(coral_image, forecast_df, stock_df)
                st.write(f"Se identificaron **{len(coral_lines)} líneas**; total provisional: **{coral_lines['Cantidad'].sum():,.0f} unidades**.")
                st.caption("Verde = SKU identificado automáticamente. Rojo = revise el SKU. Las cantidades también son editables.")

                needs_review = coral_lines["Código SKU"].fillna("").astype(str).str.fullmatch(r"\d{8}").eq(False)
                styled_coral = coral_lines.style.apply(
                    lambda column: [
                        "background-color: #ffe2e2; color: #8b1010; font-weight: 700" if needs_review.iloc[pos]
                        else "background-color: #e0f3e7; color: #176238"
                        for pos in range(len(column))
                    ],
                    subset=["Producto leído", "Revisión"],
                )
                edited_coral = st.data_editor(
                    styled_coral,
                    width="stretch",
                    hide_index=True,
                    num_rows="fixed",
                    key="coral_order_review",
                    disabled=["Cliente", "Producto leído", "Producto identificado", "Coincidencia", "Revisión"],
                    column_config={
                        "Cantidad": st.column_config.NumberColumn(min_value=1, step=1, format="%,.0f"),
                        "Código SKU": st.column_config.TextColumn(help="Código interno de 8 dígitos"),
                        "Coincidencia": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="%.0%%"),
                    },
                )

                codes = edited_coral["Código SKU"].fillna("").astype(str).str.strip()
                quantities = pd.to_numeric(edited_coral["Cantidad"], errors="coerce")
                invalid_codes = ~codes.str.fullmatch(r"\d{8}")
                invalid_qty = quantities.isna() | (quantities <= 0)
                if invalid_codes.any():
                    st.warning(f"Revise {int(invalid_codes.sum())} SKU antes de usar el pedido de Coral.")
                if invalid_qty.any():
                    st.error(f"Revise {int(invalid_qty.sum())} cantidades.")
                if not invalid_codes.any() and not invalid_qty.any():
                    weekly_order = prepare_weekly_order(edited_coral, "Código SKU", "Cantidad")
            except Exception as exc:
                st.error(f"No pude leer la foto del pedido de Coral: {exc}")

    elif entry_method == "Subir Excel o CSV":
        order_file = st.file_uploader("Pedido semanal (.xlsx o .csv)", type=["xlsx", "csv"], key="weekly_order_file")
        st.caption("El archivo debe incluir una columna de código de producto y otra de cantidad solicitada. Puede elegirlas abajo.")
        if order_file is not None:
            try:
                raw_order = pd.read_csv(order_file, sep=None, engine="python", dtype=str) if order_file.name.lower().endswith(".csv") else pd.read_excel(order_file, dtype=str)
                if len(raw_order.columns) < 2:
                    st.error("El pedido necesita al menos dos columnas: código y cantidad.")
                else:
                    cols = list(raw_order.columns)
                    code_guess = next((i for i, col in enumerate(cols) if any(term in str(col).lower() for term in ["cód", "cod", "sku"])), 0)
                    quantity_guess = next((i for i, col in enumerate(cols) if any(term in str(col).lower() for term in ["cant", "unid", "pedido"])), min(1, len(cols) - 1))
                    left, right = st.columns(2)
                    code_col = left.selectbox("Columna de código", cols, index=code_guess)
                    quantity_col = right.selectbox("Columna de cantidad", cols, index=quantity_guess)
                    if code_col == quantity_col:
                        st.error("Seleccione columnas distintas para código y cantidad.")
                    else:
                        weekly_order = prepare_weekly_order(raw_order, code_col, quantity_col)
            except Exception as exc:
                st.error(f"No pude leer el pedido: {exc}")
    else:
        typed_order = st.data_editor(
            pd.DataFrame({"Código": [""], "Cantidad": [None]}),
            num_rows="dynamic", hide_index=True, width="stretch", key="weekly_order_editor",
            column_config={"Código": st.column_config.TextColumn(help="Código/SKU de 8 dígitos"), "Cantidad": st.column_config.NumberColumn(min_value=1, step=1)},
        )
        weekly_order = prepare_weekly_order(typed_order, "Código", "Cantidad")

    if not weekly_order.empty:
        order_signature = tuple(map(tuple, weekly_order[["code", "ordered"]].sort_values("code").to_numpy()))
        if st.session_state.get("current_weekly_order_signature") != order_signature:
            st.session_state.current_weekly_order = weekly_order[["code", "ordered"]].copy()
            st.session_state.current_weekly_order_signature = order_signature
            st.session_state.active_bundle["weekly_order"] = st.session_state.current_weekly_order.copy()
            st.session_state.storage_command = {"action": "save", "revision": uuid.uuid4().hex, "payload": pack_bundle(st.session_state.active_bundle)}
            st.rerun()
        order_analysis = analyze_weekly_order(weekly_order, forecast_df, stock_df, invoice_df)
        total_ordered = order_analysis["ordered"].sum()
        total_fitting = order_analysis["fits_forecast"].sum()
        total_outside = order_analysis["outside_forecast"].sum()
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Pedido semanal", f"{total_ordered:,.0f} unidades")
        p2.metric("Cabe en el forecast", f"{total_fitting:,.0f} unidades")
        p3.metric("Sobre el forecast", f"{total_outside:,.0f} unidades")
        p4.metric("Cobertura del forecast", f"{total_fitting / total_ordered:.1%}")
        if total_outside:
            st.warning(f"El pedido supera el forecast pendiente en {total_outside:,.0f} unidades de {int((order_analysis['outside_forecast'] > 0).sum())} productos. Revise si debe ampliar el forecast o programar producción adicional.")
        else:
            st.success("Todo el pedido semanal cabe dentro del forecast aún no facturado.")
        missing_forecast = order_analysis["forecast"].isna().sum()
        if missing_forecast:
            st.warning(f"{missing_forecast} códigos del pedido no aparecen en el forecast; revise si deben incluirse.")
        st.caption("Semáforo: 🔴 sin forecast o pedido que lo supera · 🟡 queda hasta el 20% del forecast mensual · 🟢 queda más del 20%.")
        order_analysis["signal"] = order_analysis.apply(forecast_signal, axis=1)
        signal_options = ["Todos", "🔴 Sin forecast", "🟡 Queda poco", "🟢 Hay margen"]
        signal_filter = st.selectbox("Mostrar productos según forecast tras el pedido", signal_options, key="weekly_forecast_filter")
        if signal_filter != "Todos":
            order_analysis = order_analysis.loc[order_analysis["signal"] == signal_filter].copy()
        st.caption(f"Mostrando {len(order_analysis)} productos de {len(weekly_order)} códigos del pedido.")
        def show_units(value):
            if pd.isna(value) or value == 0:
                return "—"
            formatted = f"{value:,.3f}".rstrip("0").rstrip(".")
            return formatted.replace(",", "_").replace(".", ",").replace("_", ".")

        def order_result(row):
            if row["outside_forecast"] > 0:
                return f"Faltan {show_units(row['outside_forecast'])}"
            if row["forecast_after_order"] <= 0:
                return "Se agota"
            return f"Quedan {show_units(row['forecast_after_order'])}"

        order_analysis["result_label"] = order_analysis.apply(order_result, axis=1)
        forecast_table = order_analysis[["code", "product", "signal", "forecast", "sold", "forecast_remaining", "ordered", "result_label"]].rename(columns={
            "code": "Código", "product": "Producto", "signal": "Semáforo", "forecast": "Forecast mes", "sold": "Facturado", "forecast_remaining": "Forecast libre", "ordered": "Pedido", "result_label": "Resultado",
        })
        color_by_signal = {
            "🔴 Sin forecast": "background-color: #ffe1e1; color: #8b1010; font-weight: 700",
            "🟡 Queda poco": "background-color: #fff1c7; color: #795200; font-weight: 700",
            "🟢 Hay margen": "background-color: #dcf3e4; color: #176238; font-weight: 700",
        }
        if forecast_table.empty:
            st.info("No hay productos en esta categoría para el pedido cargado.")
        else:
            styled_forecast = forecast_table.style.format({column: show_units for column in ["Forecast mes", "Facturado", "Forecast libre", "Pedido"]}).apply(
                lambda column: [color_by_signal[forecast_table["Semáforo"].iloc[position]] for position in range(len(column))],
                subset=["Producto", "Semáforo", "Resultado"],
            )
            st.dataframe(
                styled_forecast,
                width="stretch", hide_index=True,
                column_config={"Código": st.column_config.TextColumn(width="small"), "Producto": st.column_config.TextColumn(width="large"), "Resultado": st.column_config.TextColumn(width="medium")},
            )
            with st.expander("Ver detalle de forecast, facturas y stock"):
                detail_table = order_analysis[["code", "product", "forecast", "normal", "export", "other", "sold", "forecast_remaining", "ordered", "outside_forecast", "forecast_after_order", "available"]].rename(columns={
                    "code": "Código", "product": "Producto", "forecast": "Forecast del mes", "normal": "Facturado nacional", "export": "Exportación (no resta)", "other": "Otras facturas", "sold": "Aplicado al forecast", "forecast_remaining": "Forecast libre", "ordered": "Pedido", "outside_forecast": "Faltan", "forecast_after_order": "Quedan", "available": "Stock informado",
                })
                numeric_columns = detail_table.select_dtypes(include="number").columns
                st.dataframe(detail_table.style.format({column: show_units for column in numeric_columns}), width="stretch", hide_index=True, column_config={"Código": st.column_config.TextColumn()})
        st.caption("Que el pedido quepa en el forecast no garantiza entrega inmediata: el stock puede ser parcial porque hay producción bajo pedido. El pedido no se descuenta ni se factura automáticamente. Si ya aparece en las facturas cargadas, no lo ingrese otra vez.")
    else:
        st.info("Cargue los pedidos y confirme sus códigos y cantidades para comparar el pedido con el forecast pendiente.")

with tab7:
    st.subheader("Fill Rate: ¿cuánto entregamos de cada orden?")
    st.caption("La factura se concilia por número de orden de compra + SKU. Las órdenes que todavía no vencen se muestran 'En plazo' y no cuentan como incumplimiento.")

    st.markdown("#### 1. Facturas para conciliar")
    st.caption("Estas facturas se guardan únicamente para el Fill Rate. No cambian las ventas, el forecast ni el stock de las otras pestañas.")
    fill_rate_pdf = st.file_uploader("Cargar facturas del mes (.pdf)", type="pdf", key="fill_rate_invoice_pdf")
    if fill_rate_pdf is not None:
        try:
            parsed_fill_invoices = read_pdf(fill_rate_pdf)
            if parsed_fill_invoices.empty:
                st.error("No pude leer líneas de producto en este PDF de facturas.")
            else:
                orders_read = int(parsed_fill_invoices.loc[parsed_fill_invoices["purchase_order"].ne(""), "invoice"].nunique())
                st.write(f"Se leyeron **{parsed_fill_invoices['invoice'].nunique()} facturas**, **{len(parsed_fill_invoices)} líneas** y **{orders_read} facturas con orden de compra**.")
                if st.button("Guardar facturas y conciliar", type="primary", key="save_fill_rate_invoices"):
                    combined_fill_invoices = pd.concat([parsed_fill_invoices, fill_rate_invoice_df], ignore_index=True)
                    fill_key = ["invoice", "date", "code", "quantity", "net_sales"]
                    duplicate_fill_lines = int(combined_fill_invoices.duplicated(fill_key).sum())
                    combined_fill_invoices = combined_fill_invoices.drop_duplicates(fill_key, keep="first")
                    st.session_state.active_bundle["fill_rate_invoices"] = combined_fill_invoices
                    st.session_state.storage_command = {"action": "save", "revision": uuid.uuid4().hex, "payload": pack_bundle(st.session_state.active_bundle)}
                    st.session_state.storage_notice = f"Facturas guardadas para Fill Rate. Se ignoraron {duplicate_fill_lines} líneas repetidas."
                    st.rerun()
        except Exception as exc:
            st.error(f"No pude leer las facturas para Fill Rate: {exc}")

    if not fill_rate_invoice_df.empty:
        saved_invoice_count = fill_rate_invoice_df["invoice"].nunique()
        saved_order_count = fill_rate_invoice_df.loc[fill_rate_invoice_df.get("purchase_order", "").ne(""), "invoice"].nunique() if "purchase_order" in fill_rate_invoice_df else 0
        st.success(f"Hay {saved_invoice_count} facturas guardadas para conciliar; {saved_order_count} incluyen número de orden de compra.")
    else:
        st.info("Todavía no hay facturas guardadas específicamente para el Fill Rate.")

    st.markdown("#### 2. Órdenes de compra")
    historical_order_files = st.file_uploader(
        "Agregar órdenes de compra (.pdf)", type="pdf", accept_multiple_files=True, key="fill_rate_order_pdfs"
    )
    st.caption("En este segundo cargador coloque solamente pedidos. Si aparece Facturas (41).pdf aquí, quítelo con la X.")
    if historical_order_files:
        try:
            imported_orders, import_errors = parse_order_pdfs(historical_order_files, forecast_df, stock_df)
            if import_errors:
                for message in import_errors:
                    st.warning(message)
            if not imported_orders.empty:
                imported_orders["Orden"] = imported_orders["Orden"].map(canonical_order)
                existing_ids = set(zip(orders_df.get("Cliente", []), orders_df.get("Orden", []).map(canonical_order) if not orders_df.empty else []))
                imported_orders["Ya guardada"] = imported_orders.apply(lambda row: (row["Cliente"], row["Orden"]) in existing_ids, axis=1)
                imported_orders["Excluir"] = False
                repeated_files = int(imported_orders["Ya guardada"].sum())
                # A Styler keeps showing the values parsed from the PDF unless we
                # reapply the data-editor changes before validating and coloring.
                review_edits = st.session_state.get("fill_rate_order_review", {}).get("edited_rows", {})
                for row_index, changes in review_edits.items():
                    position = int(row_index)
                    if position >= len(imported_orders):
                        continue
                    for editable_column in ["Código SKU", "Unidades pedidas", "Excluir"]:
                        if editable_column in changes:
                            imported_orders.at[imported_orders.index[position], editable_column] = changes[editable_column]
                visible_codes = imported_orders["Código SKU"].fillna("").astype(str).str.strip()
                visible_quantities = pd.to_numeric(imported_orders["Unidades pedidas"], errors="coerce")
                visible_invalid = ~visible_codes.str.fullmatch(r"\d{8}") | visible_quantities.isna() | (visible_quantities <= 0)
                imported_orders.loc[~visible_invalid & imported_orders["Revisión"].str.contains("no identificado", case=False, na=False), "Revisión"] = "SKU corregido manualmente"

                def color_order_row(row):
                    if bool(row["Excluir"]):
                        return ["background-color:#eef0f3;color:#667085;text-decoration:line-through"] * len(row)
                    if visible_invalid.iloc[row.name]:
                        return ["background-color:#ffe1e1;color:#9b1c1c;font-weight:700"] * len(row)
                    if row["Ya guardada"]:
                        return ["background-color:#f2f4f7;color:#667085"] * len(row)
                    return ["background-color:#e0f3e7;color:#176238"] * len(row)

                review = st.data_editor(
                    imported_orders.style.apply(color_order_row, axis=1),
                    width="stretch", hide_index=True, num_rows="fixed", key="fill_rate_order_review",
                    disabled=["Cliente", "Orden", "Fecha pedido", "Fecha inicio", "Fecha límite", "Producto en pedido", "Referencia cliente", "Cajas", "Unidades por caja", "Archivo", "Revisión", "Ya guardada"],
                    column_config={
                        "Fecha pedido": st.column_config.DateColumn(format="DD/MM/YYYY"), "Fecha inicio": st.column_config.DateColumn(format="DD/MM/YYYY"),
                        "Fecha límite": st.column_config.DateColumn(format="DD/MM/YYYY"), "Unidades pedidas": st.column_config.NumberColumn(format="%,.0f"),
                        "Código SKU": st.column_config.TextColumn(help="Código interno de 8 dígitos"),
                        "Excluir": st.column_config.CheckboxColumn(help="Marque esta casilla si el producto todavía no está creado y no debe entrar al Fill Rate"),
                    },
                )
                # Use the edited cells explicitly as the source of truth. This
                # avoids a one-refresh delay when Streamlit receives the edit.
                for row_index, changes in st.session_state.get("fill_rate_order_review", {}).get("edited_rows", {}).items():
                    position = int(row_index)
                    if position >= len(review):
                        continue
                    for editable_column in ["Código SKU", "Unidades pedidas", "Excluir"]:
                        if editable_column in changes:
                            review.at[review.index[position], editable_column] = changes[editable_column]
                excluded_count = int(review["Excluir"].fillna(False).astype(bool).sum())
                candidate = review.loc[~review["Ya guardada"] & ~review["Excluir"].fillna(False).astype(bool)].drop(columns=["Ya guardada", "Excluir"]).copy()
                candidate["Código SKU"] = candidate["Código SKU"].map(norm_code)
                invalid = candidate["Código SKU"].isna() | (pd.to_numeric(candidate["Unidades pedidas"], errors="coerce") <= 0)
                invalid_count = int(invalid.sum())
                valid_candidate = candidate.loc[~invalid].copy()
                if repeated_files:
                    st.info(f"{repeated_files} líneas pertenecen a órdenes ya guardadas y se ignorarán.")
                if excluded_count:
                    st.info(f"{excluded_count} líneas fueron excluidas y no entrarán al Fill Rate.")
                if invalid_count:
                    st.warning(f"{invalid_count} líneas todavía no tienen un SKU válido. Puede corregirlas o guardar ahora: se excluirán automáticamente del Fill Rate.")
                if valid_candidate.empty:
                    st.success("No hay líneas nuevas para guardar: ya estaban guardadas o fueron excluidas.")
                elif st.button("Guardar pedidos válidos y conciliar", type="primary", key="save_fill_rate_orders"):
                    line_key = ["Cliente", "Orden", "Código SKU", "Referencia cliente", "Unidades pedidas"]
                    valid_candidate = valid_candidate.drop_duplicates(line_key, keep="first")
                    combined_orders = pd.concat([orders_df, valid_candidate], ignore_index=True)
                    combined_orders = combined_orders.drop_duplicates(line_key, keep="first")
                    st.session_state.active_bundle["orders"] = combined_orders
                    st.session_state.storage_command = {"action": "save", "revision": uuid.uuid4().hex, "payload": pack_bundle(st.session_state.active_bundle)}
                    st.session_state.storage_notice = f"Se guardaron {valid_candidate['Orden'].nunique()} órdenes nuevas y se conciliaron con las facturas disponibles."
                    st.rerun()
        except Exception as exc:
            st.error(f"No pude leer las órdenes de compra: {exc}")

    if orders_df.empty:
        st.info("Cargue las órdenes de compra para comenzar la conciliación.")
    else:
        invoices_for_fill_rate = fill_rate_invoice_df if not fill_rate_invoice_df.empty else invoice_df
        order_summary, fill_detail = reconcile_fill_rate(orders_df, invoices_for_fill_rate, pd.Timestamp.today())

        # Visible diagnostics: distinguish "data loaded" from "data matched".
        order_keys = set(orders_df["Orden"].map(canonical_order)) - {""}
        invoice_order_keys = set(
            invoices_for_fill_rate.get("purchase_order", pd.Series(dtype=str)).map(canonical_order)
        ) - {""}
        matched_order_keys = order_keys & invoice_order_keys
        matched_lines = int((pd.to_numeric(fill_detail.get("Facturas", pd.Series(dtype=float)), errors="coerce").fillna(0) > 0).sum()) if not fill_detail.empty else 0

        d1, d2, d3, d4 = st.columns(4)
        d1.metric("OC cargadas", f"{len(order_keys):,}")
        d2.metric("OC detectadas en facturas", f"{len(invoice_order_keys):,}")
        d3.metric("OC conciliadas", f"{len(matched_order_keys):,}")
        d4.metric("Líneas OC + SKU conciliadas", f"{matched_lines:,}")

        if order_keys and invoice_order_keys and not matched_order_keys:
            st.error(
                "Las órdenes y las facturas están cargadas, pero sus números de OC no están coincidiendo. "
                "La app ahora normaliza espacios y guiones; vuelva a guardar las facturas del Fill Rate "
                "para que se relean los números de OC con la corrección."
            )
        elif matched_order_keys and matched_lines == 0:
            st.warning(
                "Los números de OC sí coinciden, pero todavía no coincide ningún SKU dentro de esas órdenes. "
                "Revise los códigos SKU identificados en las órdenes de compra."
            )
        elif matched_lines > 0:
            st.success(
                f"Conciliación activa: {len(matched_order_keys)} OC y {matched_lines} líneas OC + SKU tienen facturación asociada."
            )

        expired = order_summary[order_summary["Estado_plazo"] == "Vencida"]
        active_orders = order_summary[order_summary["Estado_plazo"] == "En plazo"]
        final_rate = expired["Facturadas"].sum() / expired["Pedidas"].sum() if expired["Pedidas"].sum() else 0
        provisional_rate = active_orders["Facturadas"].sum() / active_orders["Pedidas"].sum() if active_orders["Pedidas"].sum() else 0
        f1, f2, f3, f4 = st.columns(4)
        f1.metric("Fill Rate vencido", f"{final_rate:.1%}")
        f2.metric("Fill Rate en plazo", f"{provisional_rate:.1%}", help="Es provisional: estas órdenes todavía pueden entregarse")
        f3.metric("Órdenes guardadas", f"{order_summary['Orden'].nunique():,.0f}")
        f4.metric("Unidades pendientes", f"{order_summary['Pendientes'].sum():,.0f}")

        status_filter = st.multiselect("Mostrar", ["En plazo", "Completa", "Incumplida", "Revisar"], default=["En plazo", "Incumplida"])
        client_filter = st.multiselect("Cliente", sorted(order_summary["Cliente"].dropna().unique()), default=[])
        shown_orders = order_summary[order_summary["Resultado"].isin(status_filter)].copy()
        if client_filter:
            shown_orders = shown_orders[shown_orders["Cliente"].isin(client_filter)]
        shown_orders["Fill Rate"] = shown_orders["Fill_rate"].map(lambda value: f"{value:.1%}" if pd.notna(value) else "—")
        st.dataframe(
            shown_orders.rename(columns={"Fecha_limite":"Fecha límite", "Estado_plazo":"Plazo"})[["Cliente", "Orden", "Fecha límite", "Resultado", "Pedidas", "Facturadas", "Pendientes", "Fill Rate", "Productos"]],
            width="stretch", hide_index=True,
            column_config={"Fecha límite": st.column_config.DateColumn(format="DD/MM/YYYY"), "Pedidas": st.column_config.NumberColumn(format="%,.0f"), "Facturadas": st.column_config.NumberColumn(format="%,.0f"), "Pendientes": st.column_config.NumberColumn(format="%,.0f")},
        )

        if not shown_orders.empty:
            selected_order = st.selectbox("Ver conciliación producto por producto", shown_orders["Orden"].drop_duplicates().tolist())
            detail = fill_detail[fill_detail["Orden"] == selected_order].copy()
            detail["Fill Rate"] = detail["Fill_rate"].map(lambda value: f"{value:.1%}" if pd.notna(value) else "—")
            st.dataframe(
                detail.rename(columns={"Código SKU":"Código", "Unidades_pedidas":"Pedido", "Unidades_facturadas":"Facturado", "Ultima_factura":"Última factura"})[["Código", "Producto", "Pedido", "Facturado", "Pendiente", "Fill Rate", "Resultado", "Última factura"]],
                width="stretch", hide_index=True,
                column_config={"Código": st.column_config.TextColumn(), "Pedido": st.column_config.NumberColumn(format="%,.0f"), "Facturado": st.column_config.NumberColumn(format="%,.0f"), "Pendiente": st.column_config.NumberColumn(format="%,.0f"), "Última factura": st.column_config.DateColumn(format="DD/MM/YYYY")},
            )

        invoice_orders = set(invoices_for_fill_rate.get("purchase_order", pd.Series(dtype=str)).map(canonical_order)) - {""}
        saved_orders = set(orders_df["Orden"].map(canonical_order))
        unmatched = invoice_orders - saved_orders
        if unmatched:
            st.caption(f"Hay {len(unmatched)} órdenes mencionadas en facturas que todavía no están cargadas en esta pestaña.")

with tab8:
    st.subheader("Rentabilidad · Margen bruto")
    st.caption("Módulo independiente. Sus facturas y costos no cambian Forecast, Stock, Pedido semanal ni Fill Rate.")
    if st.session_state.get("profit_notice"):
        st.info(st.session_state.pop("profit_notice"))

    month_labels = {1:"Enero", 2:"Febrero", 3:"Marzo", 4:"Abril", 5:"Mayo", 6:"Junio", 7:"Julio", 8:"Agosto", 9:"Septiembre", 10:"Octubre", 11:"Noviembre", 12:"Diciembre"}
    now = pd.Timestamp.today()
    saved_months = sorted(st.session_state.profit_store.get("months", {}).keys(), reverse=True)
    default_month = f"{now.year:04d}-{now.month:02d}"
    month_options = sorted(set(saved_months + [default_month]), reverse=True)
    selected_month = st.selectbox(
        "Mes de rentabilidad",
        month_options,
        format_func=lambda key: f"{month_labels[int(key[5:7])]} {key[:4]}",
        key="profit_month",
    )
    month_name = f"{month_labels[int(selected_month[5:7])]} {selected_month[:4]}"
    saved_cut = st.session_state.profit_store.get("months", {}).get(selected_month, {"invoices": pd.DataFrame(), "costs": pd.DataFrame()})
    saved_profit_invoices = saved_cut.get("invoices", pd.DataFrame()).copy()
    saved_profit_costs = saved_cut.get("costs", pd.DataFrame()).copy()
    st.markdown(f"**Costos activos: {month_name}**")

    up1, up2 = st.columns(2)
    with up1:
        profit_pdfs = st.file_uploader("Facturas para Rentabilidad (.pdf)", type="pdf", accept_multiple_files=True, key=f"profit_pdfs_{selected_month}")
        st.caption("Puede agregar facturas nuevas del mismo mes. Las ya procesadas no se duplicarán.")
    with up2:
        profit_cost_file = st.file_uploader("Costos del mes desde Odoo (.xlsx)", type="xlsx", key=f"profit_costs_{selected_month}")
        st.caption("Referencia Interna · Nombre · Costo · Unidad de Medida")

    profit_signature = (selected_month, tuple(file_signature(file) for file in profit_pdfs or []), file_signature(profit_cost_file))
    if st.session_state.get("profit_validated_signature") != profit_signature:
        st.session_state.pop("profit_candidate", None)
        st.session_state.pop("profit_validation", None)

    if st.button("Prevalidar Rentabilidad", type="primary", key="validate_profit"):
        try:
            parsed = read_profitability_pdfs(profit_pdfs)
            existing_invoice_ids = set(saved_profit_invoices.get("invoice", pd.Series(dtype=str)).astype(str))
            repeated_invoice_ids = sorted(set(parsed.get("invoice", pd.Series(dtype=str)).astype(str)) & existing_invoice_ids)
            new_invoices = parsed[~parsed["invoice"].astype(str).isin(existing_invoice_ids)].copy() if not parsed.empty else parsed
            upload_duplicate_ids = (
                new_invoices.groupby("invoice")["source_file"].nunique().loc[lambda values: values > 1].index.tolist()
                if not new_invoices.empty and "source_file" in new_invoices else []
            )
            invoice_candidate = pd.concat([saved_profit_invoices, new_invoices], ignore_index=True)
            invoice_candidate = invoice_candidate.drop_duplicates(["invoice", "pdf_page", "line_no", "code"], keep="first") if not invoice_candidate.empty else invoice_candidate
            cost_candidate = read_profitability_costs(profit_cost_file) if profit_cost_file else saved_profit_costs.copy()
            month_period = pd.Period(selected_month, freq="M")
            outside_month = int((invoice_candidate["date"].dt.to_period("M") != month_period).sum()) if not invoice_candidate.empty else 0
            invoiced_codes = set(invoice_candidate.get("code", pd.Series(dtype=str)).dropna())
            all_duplicate_cost_codes = set(cost_candidate.loc[cost_candidate.duplicated("code", keep=False), "code"].dropna().unique()) if not cost_candidate.empty else set()
            all_zero_cost_codes = set(cost_candidate.loc[pd.to_numeric(cost_candidate.get("unit_cost"), errors="coerce").fillna(0).le(0), "code"].dropna().unique()) if not cost_candidate.empty else set()
            duplicate_cost_codes = sorted(all_duplicate_cost_codes & invoiced_codes)
            zero_cost_codes = sorted(all_zero_cost_codes & invoiced_codes)
            exp_codes = sorted(code for code in invoiced_codes if str(code).startswith("EXP"))
            valid_cost_codes = set(cost_candidate.loc[~cost_candidate["code"].isin(all_duplicate_cost_codes | all_zero_cost_codes), "code"]) if not cost_candidate.empty else set()
            missing_cost_codes = sorted(invoiced_codes - valid_cost_codes)
            errors = []
            if invoice_candidate.empty:
                errors.append("No existen facturas de Rentabilidad para este mes.")
            if cost_candidate.empty:
                errors.append("No existe un archivo de costos válido para este mes.")
            warnings = []
            if outside_month:
                warnings.append(f"{outside_month} líneas tienen fecha fuera de {month_name}; quedarán fuera de los resultados de este mes.")
            if repeated_invoice_ids:
                warnings.append(f"Se ignoraron {len(repeated_invoice_ids)} facturas que ya estaban guardadas.")
            if upload_duplicate_ids:
                warnings.append(f"El lote contiene {len(upload_duplicate_ids)} números de factura repetidos; se conservarán sus líneas una sola vez.")
            if duplicate_cost_codes:
                warnings.append(f"Hay {len(duplicate_cost_codes)} SKU facturados que están duplicados en costos; no se calculará su margen hasta corregirlos.")
            if zero_cost_codes:
                warnings.append(f"Hay {len(zero_cost_codes)} SKU facturados con costo cero o inválido.")
            if exp_codes:
                warnings.append(f"Hay {len(exp_codes)} códigos EXP facturados. Se mantendrán separados del SKU nacional.")
            negative_lines = int(((invoice_candidate.get("quantity", 0) < 0) | (invoice_candidate.get("net_sales", 0) < 0)).sum()) if not invoice_candidate.empty else 0
            if negative_lines:
                warnings.append(f"Se detectaron {negative_lines} líneas negativas; se tratarán como devoluciones/notas de crédito.")
            st.session_state.profit_validated_signature = profit_signature
            st.session_state.profit_validation = {"errors": errors, "warnings": warnings, "missing": missing_cost_codes, "duplicates": duplicate_cost_codes, "zero": zero_cost_codes, "new_invoices": int(new_invoices['invoice'].nunique()) if not new_invoices.empty else 0}
            if not errors:
                st.session_state.profit_candidate = {"month": selected_month, "invoices": invoice_candidate, "costs": cost_candidate}
        except Exception as exc:
            st.session_state.profit_validation = {"errors": [f"No pude prevalidar Rentabilidad: {exc}"], "warnings": [], "missing": [], "duplicates": [], "zero": [], "new_invoices": 0}
            st.session_state.pop("profit_candidate", None)

    profit_validation = st.session_state.get("profit_validation")
    if profit_validation:
        if profit_validation["errors"]:
            for message in profit_validation["errors"]:
                st.error(message)
        else:
            st.success(f"Prevalidación completa. Facturas nuevas: {profit_validation['new_invoices']:,}.")
        for message in profit_validation["warnings"]:
            st.warning(message)
        if profit_validation.get("missing"):
            st.error(f"{len(profit_validation['missing'])} SKU facturados no tienen un costo válido. No se asumirá costo cero.")

    if st.button("Guardar / actualizar Rentabilidad", disabled="profit_candidate" not in st.session_state, key="save_profit"):
        candidate = st.session_state.profit_candidate
        st.session_state.profit_store.setdefault("months", {})[candidate["month"]] = {"invoices": candidate["invoices"], "costs": candidate["costs"]}
        st.session_state.profit_storage_loaded = True
        st.session_state.profit_storage_command = {"action": "save", "revision": uuid.uuid4().hex, "payload": pack_profit_store(st.session_state.profit_store)}
        st.session_state.pop("profit_candidate", None)
        st.session_state.pop("profit_validation", None)
        st.rerun()

    saved_cut = st.session_state.profit_store.get("months", {}).get(selected_month)
    if not saved_cut or saved_cut.get("invoices", pd.DataFrame()).empty or saved_cut.get("costs", pd.DataFrame()).empty:
        st.info("Seleccione el mes, cargue facturas y costos, prevalide y guarde para ver la rentabilidad.")
    else:
        month_invoices = saved_cut["invoices"].copy()
        month_invoices = month_invoices[month_invoices["date"].dt.to_period("M") == pd.Period(selected_month, freq="M")]
        month_costs = saved_cut["costs"].copy()
        calculated = profitability_lines(month_invoices, month_costs)
        all_codes = calculated["code"].nunique()
        found_codes = calculated.loc[calculated["unit_cost"].notna() & calculated["unit_cost"].gt(0), "code"].nunique()
        missing_table = calculated[calculated["unit_cost"].isna() | calculated["unit_cost"].le(0)].groupby(["code", "product"], as_index=False).agg(Unidades=("quantity", "sum"), Venta_afectada=("net_sales", "sum"))
        v1, v2, v3, v4 = st.columns(4)
        v1.metric("SKU facturados", f"{all_codes:,}")
        v2.metric("SKU con costo", f"{found_codes:,}")
        v3.metric("SKU sin costo", f"{all_codes - found_codes:,}")
        v4.metric("Cobertura de costos", f"{found_codes / all_codes:.1%}" if all_codes else "0,0%")
        if not missing_table.empty:
            st.error("Existen ventas sin costo válido. Estas líneas no entran en utilidad ni margen hasta que se cargue el costo correcto.")
            st.dataframe(missing_table.rename(columns={"code":"SKU sin costo", "product":"Producto", "Venta_afectada":"Venta afectada"}), hide_index=True, width="stretch", column_config={"Venta afectada": st.column_config.NumberColumn(format="$%,.2f"), "Unidades": st.column_config.NumberColumn(format="%,.2f")})

        valid = calculated[calculated["unit_cost"].notna() & calculated["unit_cost"].gt(0)].copy()
        if not valid.empty:
            st.caption("Los KPI de margen incluyen únicamente líneas con costo válido. El indicador de cobertura muestra qué parte del catálogo facturado pudo calcularse.")
            clients = sorted(valid["customer"].dropna().unique())
            fc1, fc2, fc3 = st.columns(3)
            selected_clients = fc1.multiselect("Cliente", clients, key="profit_client_filter")
            sku_search = fc2.text_input("Buscar SKU o producto", key="profit_search")
            invoice_types = fc3.multiselect("Tipo de factura", ["Nacional", "Exportación", "Otra"], default=["Nacional", "Exportación", "Otra"], key="profit_invoice_type")
            min_date, max_date = valid["date"].min().date(), valid["date"].max().date()
            date_range = st.date_input("Fecha", value=(min_date, max_date), min_value=min_date, max_value=max_date, key="profit_dates")
            margin_low = st.number_input("Límite crítico (%)", value=15.0, step=1.0, key="margin_critical")
            margin_good = st.number_input("Límite saludable (%)", value=25.0, step=1.0, key="margin_good")
            margin_range = st.slider("Rango de margen (%)", min_value=-100, max_value=100, value=(-100, 100), key="profit_margin_range")
            filtered = valid[valid["invoice_type"].isin(invoice_types)].copy()
            if selected_clients:
                filtered = filtered[filtered["customer"].isin(selected_clients)]
            if sku_search:
                filtered = filtered[filtered["code"].str.contains(sku_search, case=False, na=False) | filtered["product"].str.contains(sku_search, case=False, na=False)]
            if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
                filtered = filtered[filtered["date"].dt.date.between(date_range[0], date_range[1])]
            filtered_margin = filtered["gross_margin"] * 100
            filtered = filtered[filtered_margin.between(margin_range[0], margin_range[1])]

            net_total, cost_total, profit_total = filtered["net_sales"].sum(), filtered["cost_of_sales"].sum(), filtered["gross_profit"].sum()
            margin_total = profit_total / net_total if net_total else pd.NA
            sku_base = filtered.groupby(["code", "product", "invoice_type"], as_index=False).agg(Unidades=("quantity", "sum"), Venta=("net_sales", "sum"), Venta_lista=("gross_before_discount", "sum"), Descuento_valor=("discount_amount", "sum"), Costo_unitario=("unit_cost", "first"), Costo_vendido=("cost_of_sales", "sum"), Utilidad=("gross_profit", "sum"))
            sku_base["Precio_promedio"] = sku_base["Venta"] / sku_base["Unidades"].replace(0, pd.NA)
            sku_base["Descuento_promedio"] = sku_base["Descuento_valor"] / sku_base["Venta_lista"].replace(0, pd.NA)
            sku_base["Margen"] = sku_base["Utilidad"] / sku_base["Venta"].replace(0, pd.NA)
            sku_base["Semáforo"] = sku_base["Margen"].map(
                lambda value: "🔴 Pérdida" if value < 0 else "🟠 Crítico" if value <= margin_low / 100 else "🟡 Revisar" if value <= margin_good / 100 else "🟢 Saludable"
            )
            k1, k2, k3, k4, k5, k6 = st.columns(6)
            k1.metric("Venta neta", f"${net_total:,.2f}")
            k2.metric("Costo de venta", f"${cost_total:,.2f}")
            k3.metric("Utilidad bruta", f"${profit_total:,.2f}")
            k4.metric("Margen bruto", f"{margin_total:.1%}" if pd.notna(margin_total) else "—")
            k5.metric("SKU margen crítico", f"{int(((sku_base['Margen'] >= 0) & (sku_base['Margen'] <= margin_low / 100)).sum()):,}")
            k6.metric("SKU con pérdida", f"{int((sku_base['Margen'] < 0).sum()):,}")

            view1, view2, view3, view4 = st.tabs(["Por SKU", "Por cliente", "Cliente × SKU", "Top y Bottom"])
            with view1:
                sort_label = st.selectbox("Ordenar por", ["Venta", "Utilidad", "Margen", "Unidades"], key="profit_sort")
                st.caption("Cada SKU aparece separado por tipo de venta. Las filas de exportación se resaltan porque pueden tener un descuento comercial adicional.")
                sku_show = sku_base.sort_values(sort_label, ascending=False).rename(columns={"code":"Código", "product":"Producto", "invoice_type":"Tipo de venta", "Precio_promedio":"Precio promedio real", "Descuento_promedio":"Descuento promedio %", "Costo_unitario":"Costo unitario", "Costo_vendido":"Costo vendido $", "Utilidad":"Utilidad bruta $", "Margen":"Margen bruto %", "Venta":"Venta neta $", "Unidades":"Unidades vendidas"})
                sku_show = sku_show[["Código", "Producto", "Tipo de venta", "Semáforo", "Unidades vendidas", "Venta neta $", "Precio promedio real", "Descuento promedio %", "Costo unitario", "Costo vendido $", "Utilidad bruta $", "Margen bruto %"]]
                styled_sku = sku_show.style.apply(
                    lambda row: ["background-color: #fff3cd; color: #7a4b00; font-weight: 600"] * len(row) if row["Tipo de venta"] == "Exportación" else [""] * len(row),
                    axis=1,
                )
                st.dataframe(styled_sku, hide_index=True, width="stretch", column_config={"Venta neta $":st.column_config.NumberColumn(format="$%,.2f"), "Precio promedio real":st.column_config.NumberColumn(format="$%,.4f"), "Descuento promedio %":st.column_config.NumberColumn(format="%.1%%"), "Costo unitario":st.column_config.NumberColumn(format="$%,.5f"), "Costo vendido $":st.column_config.NumberColumn(format="$%,.2f"), "Utilidad bruta $":st.column_config.NumberColumn(format="$%,.2f"), "Margen bruto %":st.column_config.NumberColumn(format="%.1%%")})
            client_base = filtered.groupby("customer", as_index=False).agg(Venta=("net_sales", "sum"), Costo=("cost_of_sales", "sum"), Utilidad=("gross_profit", "sum"), Unidades=("quantity", "sum"))
            client_base["Margen"] = client_base["Utilidad"] / client_base["Venta"].replace(0, pd.NA)
            with view2:
                st.dataframe(client_base.rename(columns={"customer":"Cliente", "Venta":"Venta neta", "Costo":"Costo vendido", "Utilidad":"Utilidad bruta", "Margen":"Margen bruto %"}).sort_values("Utilidad bruta", ascending=False), hide_index=True, width="stretch", column_config={"Venta neta":st.column_config.NumberColumn(format="$%,.2f"), "Costo vendido":st.column_config.NumberColumn(format="$%,.2f"), "Utilidad bruta":st.column_config.NumberColumn(format="$%,.2f"), "Margen bruto %":st.column_config.NumberColumn(format="%.1%%")})
                chart_data = client_base.melt(id_vars="customer", value_vars=["Venta", "Utilidad"], var_name="Indicador", value_name="Dólares")
                st.plotly_chart(px.bar(chart_data, x="customer", y="Dólares", color="Indicador", barmode="group", title="Venta neta vs. utilidad bruta por cliente"), width="stretch")
            with view3:
                mode = st.radio("Comparar", ["SKU dentro de un cliente", "Un SKU entre clientes"], horizontal=True, key="profit_cross_mode")
                if mode == "SKU dentro de un cliente":
                    chosen = st.selectbox("Cliente", sorted(filtered["customer"].unique()), key="profit_cross_client")
                    cross = filtered[filtered["customer"] == chosen].groupby(["code", "product", "invoice_type"], as_index=False).agg(Unidades=("quantity", "sum"), Venta=("net_sales", "sum"), Costo=("cost_of_sales", "sum"), Utilidad=("gross_profit", "sum"))
                else:
                    sku_choices = sku_base.assign(label=lambda frame: frame["code"] + " · " + frame["product"]).drop_duplicates("label")
                    chosen_label = st.selectbox("SKU", sku_choices["label"].tolist(), key="profit_cross_sku")
                    chosen_code = chosen_label.split(" · ", 1)[0]
                    cross = filtered[filtered["code"] == chosen_code].groupby(["customer", "invoice_type"], as_index=False).agg(Unidades=("quantity", "sum"), Venta=("net_sales", "sum"), Costo=("cost_of_sales", "sum"), Utilidad=("gross_profit", "sum"))
                cross["Margen"] = cross["Utilidad"] / cross["Venta"].replace(0, pd.NA)
                st.dataframe(cross, hide_index=True, width="stretch", column_config={"Venta":st.column_config.NumberColumn(format="$%,.2f"), "Costo":st.column_config.NumberColumn(format="$%,.2f"), "Utilidad":st.column_config.NumberColumn(format="$%,.2f"), "Margen":st.column_config.NumberColumn(format="%.1%%")})
            with view4:
                top_left, top_right = st.columns(2)
                top_left.markdown("**Top 10 SKU por utilidad bruta**")
                top_left.dataframe(sku_base.nlargest(10, "Utilidad")[["code", "product", "Utilidad"]], hide_index=True, width="stretch")
                top_right.markdown("**Bottom 10 SKU por margen bruto**")
                top_right.dataframe(sku_base.nsmallest(10, "Margen")[["code", "product", "Margen"]], hide_index=True, width="stretch")
                ctop, cbottom = st.columns(2)
                ctop.markdown("**Top clientes por utilidad**")
                ctop.dataframe(client_base.nlargest(10, "Utilidad")[["customer", "Utilidad"]], hide_index=True, width="stretch")
                cbottom.markdown("**Clientes con menor margen**")
                cbottom.dataframe(client_base.nsmallest(10, "Margen")[["customer", "Margen"]], hide_index=True, width="stretch")

st.caption("Regla: el stock usa Cantidad disponible. Va lento si el avance está más de 10 puntos por debajo del tiempo transcurrido; va más rápido si está más de 10 puntos por encima.")
