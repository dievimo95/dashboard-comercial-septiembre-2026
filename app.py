import io
import json
import re
import base64
import uuid
import zlib
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber
import plotly.express as px
import streamlit as st
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
    records, invoice, date, customer = [], None, None, ""
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
            for raw in text.splitlines():
                match = spanish.match(raw.strip()) or english.match(raw.strip())
                if not match or not invoice or not date:
                    continue
                qty, price, net = map(number_local, match.groups()[1:])
                records.append({"invoice": invoice, "date": date, "week": 1 if date.day <= 7 else 2 if date.day <= 14 else 3 if date.day <= 21 else 4 if date.day <= 28 else 5, "customer": customer, "code": match.group(1), "quantity": qty, "unit_price": price, "net_sales": net, "pdf_page": page_no})
    return pd.DataFrame(records)


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
        "invoices": pd.DataFrame(columns=["invoice", "date", "week", "customer", "code", "quantity", "unit_price", "net_sales", "pdf_page"]),
        "cutoff": pd.NaT,
    }


def pack_bundle(bundle):
    data = {
        "version": 1,
        "tables": {name: bundle[name].to_json(orient="records", date_format="iso") for name in ["forecast", "stock", "lots", "invoices"]},
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
    if data.get("version") != 1:
        raise ValueError("Versión de copia no compatible")
    bundle = blank_bundle()
    for name in ["forecast", "stock", "lots", "invoices"]:
        bundle[name] = pd.read_json(io.StringIO(data["tables"][name]), orient="records", dtype={"code": str}, convert_dates=False)
    for name, column in [("stock", "expiry"), ("lots", "expiry"), ("invoices", "date")]:
        if column in bundle[name]:
            bundle[name][column] = pd.to_datetime(bundle[name][column], errors="coerce")
    bundle["cutoff"] = pd.to_datetime(data["cutoff"]) if data.get("cutoff") else pd.NaT
    if bundle["forecast"].empty or bundle["invoices"].empty:
        raise ValueError("La copia no contiene forecast y facturas")
    return bundle


def build_analysis(forecast, stock, invoices, cutoff):
    sold = invoices.groupby("code", as_index=False).agg(sold=("quantity", "sum"), net_sales=("net_sales", "sum"))
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
                            rows.append({"Cliente": "TIA", "Orden": order_id, "Producto en pedido": str(line[6] or "").replace("\n", " "), "Referencia cliente": client_code, "Cajas": order_number(line[1]) if line[1] else None, "Unidades por caja": None, "Unidades pedidas": units, "Código SKU": client_to_sku.get(client_code, ""), "Archivo": filename})
                            found += 1
                elif "CORPORACION EL ROSADO" in text:
                    for table in page.extract_tables():
                        if len(table) < 6 or not any("NUMERO DE ORDEN" in str(cell) for line in table[:5] for cell in line):
                            continue
                        order_id = next((str(line[2]) for line in table if str(line[0]).startswith("NUMERO DE ORDEN")), f"página {page_no}")
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
                            rows.append({"Cliente": "El Rosado", "Orden": order_id, "Producto en pedido": str(line[2] or "").replace("\n", " "), "Referencia cliente": reference, "Cajas": packs, "Unidades por caja": uxc, "Unidades pedidas": packs * uxc, "Código SKU": sku, "Archivo": filename})
                            found += 1
                elif "CORPORACION FAVORITA" in text and "Pedida" in text:
                    blocks = re.split(r"(?=Tda/Alm/CDI:)", text)
                    for block_no, block in enumerate(blocks, 1):
                        if "IT D e s c r i p c i o n" not in block:
                            continue
                        order_match = re.search(r"ORDEN COMPRA[^\n]*?\s(\d{5})\s*\n", block)
                        order_id = order_match.group(1) if order_match else f"página {page_no}, bloque {block_no}"
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
                            rows.append({"Cliente": "Supermaxi", "Orden": order_id, "Producto en pedido": description, "Referencia cliente": barcode, "Cajas": packs, "Unidades por caja": uxc, "Unidades pedidas": packs * uxc, "Código SKU": resolve_barcode(barcode, uxc), "Archivo": filename})
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
    sold["sold"] = sold[["normal", "export", "other"]].sum(axis=1)
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
            combined_invoices = pd.concat([active["invoices"], new_invoices], ignore_index=True)
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
                st.session_state.validated_bundle = {"forecast": candidate_forecast, "stock": candidate_stock, "lots": candidate_lots, "invoices": combined_invoices, "cutoff": cutoff_candidate}
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

c1, c2, c3, c4 = st.columns(4)
c1.metric("Forecast del mes", f"{forecast_total:,.0f}")
c2.metric("Vendido", f"{sold_forecast:,.0f}")
c3.metric("Avance", f"{sold_forecast / forecast_total:.1%}", f"{sold_forecast / forecast_total - elapsed:+.1%} frente al tiempo")
c4.metric("Stock disponible", f"{analysis['available'].sum():,.0f}")

if sold_forecast / forecast_total > elapsed:
    st.success(f"En general vamos adelantados: se vendió {sold_forecast / forecast_total:.1%} del forecast y ha pasado {elapsed:.1%} del mes.")
else:
    st.warning(f"En general vamos atrasados: se vendió {sold_forecast / forecast_total:.1%} del forecast y ha pasado {elapsed:.1%} del mes.")

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["Resumen", "Qué hacer", "Todos los productos", "Facturas", "Ventas por cliente", "Pedido semanal"])

with tab1:
    left, right = st.columns([1, 1])
    with left:
        progress = pd.DataFrame({"Indicador": ["Mes transcurrido", "Forecast vendido"], "Porcentaje": [elapsed, sold_forecast / forecast_total]})
        fig = px.bar(progress, x="Indicador", y="Porcentaje", text_auto=".0%", color="Indicador", color_discrete_sequence=["#9fbad0", "#1f6d8c"])
        fig.update_layout(title="¿Vamos al ritmo correcto?", showlegend=False, yaxis_tickformat=".0%", yaxis_range=[0, max(.5, progress["Porcentaje"].max() * 1.25)], height=360)
        st.plotly_chart(fig, width="stretch")
    with right:
        counts = analysis["status"].value_counts().rename_axis("Estado").reset_index(name="Productos")
        fig = px.bar(counts, x="Productos", y="Estado", orientation="h", color="Estado", color_discrete_map=COLORS, text_auto=True)
        fig.update_layout(title="Productos por estado", showlegend=False, height=360, yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, width="stretch")

    st.subheader("Atención inmediata")
    urgent = analysis[analysis["status"].isin(["No hay stock", "Falta stock"])].head(10)
    for _, row in urgent.iterrows():
        with st.container(border=True):
            st.markdown(f"**{row['status']} · {row['code']}** — {row['product']}")
            st.caption(f"Forecast {row.forecast:,.0f} · Vendido {row.sold:,.0f} · Stock {row.available:,.0f}")
            st.markdown(f"**{row.action}**")

with tab2:
    chosen = st.multiselect("Mostrar estados", list(COLORS), default=["No hay stock", "Falta stock", "Va lento", "Hay que venderlo"])
    actions = analysis[analysis["status"].isin(chosen)].copy()
    show = actions[["status", "code", "product", "forecast", "sold", "available", "remaining", "missing_stock", "action"]].rename(columns={"status":"Estado", "code":"Código", "product":"Producto", "forecast":"Forecast", "sold":"Vendido", "available":"Stock", "remaining":"Falta vender", "missing_stock":"Stock faltante", "action":"Qué hacer"})
    st.dataframe(show, width="stretch", hide_index=True, column_config={"Código": st.column_config.TextColumn(), "Forecast": st.column_config.NumberColumn(format="%,.0f"), "Vendido": st.column_config.NumberColumn(format="%,.0f"), "Stock": st.column_config.NumberColumn(format="%,.0f"), "Falta vender": st.column_config.NumberColumn(format="%,.0f"), "Stock faltante": st.column_config.NumberColumn(format="%,.0f")})

with tab3:
    search = st.text_input("Buscar código o producto")
    full = analysis.copy()
    if search:
        full = full[full["code"].str.contains(search, case=False, na=False) | full["product"].str.contains(search, case=False, na=False)]
    full["Avance"] = full["advance"]
    show = full[["code", "product", "forecast", "sold", "Avance", "available", "remaining", "projection", "status", "action"]].rename(columns={"code":"Código", "product":"Producto", "forecast":"Forecast", "sold":"Vendido", "available":"Stock", "remaining":"Falta vender", "projection":"Proyección", "status":"Estado", "action":"Qué hacer"})
    st.dataframe(show, width="stretch", hide_index=True, column_config={"Código": st.column_config.TextColumn(), "Avance": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.1%%"), "Forecast": st.column_config.NumberColumn(format="%,.0f"), "Vendido": st.column_config.NumberColumn(format="%,.0f"), "Stock": st.column_config.NumberColumn(format="%,.0f"), "Falta vender": st.column_config.NumberColumn(format="%,.0f"), "Proyección": st.column_config.NumberColumn(format="%,.0f")})

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
    })
    st.dataframe(
        customer_table[["Cliente", "Venta neta", "% de la venta", "Facturas", "Ticket promedio", "Unidades", "Última compra"]],
        width="stretch",
        hide_index=True,
        column_config={
            "Venta neta": st.column_config.NumberColumn(format="$%,.2f"),
            "% de la venta": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.1%%"),
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
    st.caption("Forecast pendiente = forecast del mes − unidades ya facturadas. Se cuentan tanto facturas normales (001-100) como de exportación (001-901). Todas las cantidades son unidades individuales.")
    entry_method = st.radio("Cómo ingresar el pedido", ["Subir PDF de clientes", "Subir Excel o CSV", "Escribir pedido"], horizontal=True)
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
        forecast_table = order_analysis[["code", "product", "signal", "ordered", "forecast_remaining", "result_label"]].rename(columns={
            "code": "Código", "product": "Producto", "signal": "Semáforo", "ordered": "Pedido", "forecast_remaining": "Forecast libre", "result_label": "Resultado",
        })
        color_by_signal = {
            "🔴 Sin forecast": "background-color: #ffe1e1; color: #8b1010; font-weight: 700",
            "🟡 Queda poco": "background-color: #fff1c7; color: #795200; font-weight: 700",
            "🟢 Hay margen": "background-color: #dcf3e4; color: #176238; font-weight: 700",
        }
        if forecast_table.empty:
            st.info("No hay productos en esta categoría para el pedido cargado.")
        else:
            styled_forecast = forecast_table.style.format({"Pedido": show_units, "Forecast libre": show_units}).apply(
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
                    "code": "Código", "product": "Producto", "forecast": "Forecast del mes", "normal": "Facturado normal", "export": "Facturado exportación", "other": "Otras facturas", "sold": "Total facturado", "forecast_remaining": "Forecast libre", "ordered": "Pedido", "outside_forecast": "Faltan", "forecast_after_order": "Quedan", "available": "Stock informado",
                })
                numeric_columns = detail_table.select_dtypes(include="number").columns
                st.dataframe(detail_table.style.format({column: show_units for column in numeric_columns}), width="stretch", hide_index=True, column_config={"Código": st.column_config.TextColumn()})
        st.caption("Que el pedido quepa en el forecast no garantiza entrega inmediata: el stock puede ser parcial porque hay producción bajo pedido. El pedido no se descuenta ni se factura automáticamente. Si ya aparece en las facturas cargadas, no lo ingrese otra vez.")
    else:
        st.info("Cargue los pedidos y confirme sus códigos y cantidades para comparar el pedido con el forecast pendiente.")

st.caption("Regla: el stock usa Cantidad disponible. Va lento si el avance está más de 10 puntos por debajo del tiempo transcurrido; va más rápido si está más de 10 puntos por encima.")
