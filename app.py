import io
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber
import plotly.express as px
import streamlit as st


st.set_page_config(page_title="Control comercial", page_icon="📊", layout="wide")

BASE = Path(__file__).resolve().parent
SAMPLE = BASE / "sample_data.json"

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
                auth = auth_date_re.search(text)
                if auth:
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


if "active_bundle" not in st.session_state:
    st.session_state.active_bundle = blank_bundle()
if "upload_generation" not in st.session_state:
    st.session_state.upload_generation = 0

with st.sidebar:
    st.header("Actualizar información")
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
            }
            if not errors:
                st.session_state.validated_bundle = {"forecast": candidate_forecast, "stock": candidate_stock, "lots": candidate_lots, "invoices": combined_invoices, "cutoff": cutoff_candidate}
        except Exception as exc:
            st.session_state.validated_signature = signature
            st.session_state.validation_summary = {"errors": [f"No pude leer los archivos: {exc}"], "warnings": [], "new_lines": 0, "new_invoices": 0, "duplicates": 0, "date_min": None, "date_max": None}
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
            if summary["duplicates"]:
                st.info(f"Se encontraron {summary['duplicates']} líneas ya cargadas. No se duplicarán.")
            for message in summary["warnings"]:
                st.warning(message)

    can_update = "validated_bundle" in st.session_state and not (summary or {}).get("errors")
    if st.button("Cargar y actualizar dashboard", disabled=not can_update, use_container_width=True):
        st.session_state.active_bundle = st.session_state.validated_bundle
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

tab1, tab2, tab3, tab4 = st.tabs(["Resumen", "Qué hacer", "Todos los productos", "Facturas"])

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

st.caption("Regla: el stock usa Cantidad disponible. Va lento si el avance está más de 10 puntos por debajo del tiempo transcurrido; va más rápido si está más de 10 puntos por encima.")
