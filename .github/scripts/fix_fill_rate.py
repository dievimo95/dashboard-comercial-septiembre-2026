from pathlib import Path

path = Path("app.py")
text = path.read_text(encoding="utf-8")

old = '''def canonical_order(value):
    """Comparable order key while preserving alphanumeric customer orders."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
'''
new = '''def canonical_order(value):
    """Comparable order key while preserving alphanumeric customer orders.

    Invoice PDFs sometimes leave extra text after ``OC:``. Prefer the first
    plausible numeric order number (allowing spaces/hyphens) so an invoice such
    as ``OC: 100 6243 97153`` matches the order parser's ``100624397153``.
    """
    raw = str(value or "").upper().strip()
    numeric = re.search(r"(?<!\\d)((?:\\d[ -]*){6,20})(?!\\d)", raw)
    if numeric:
        digits = re.sub(r"\\D", "", numeric.group(1))
        if len(digits) >= 6:
            return digits
    return re.sub(r"[^A-Z0-9]", "", raw)
'''
if old not in text:
    raise SystemExit("No se encontró canonical_order esperado")
text = text.replace(old, new, 1)

old = '''    purchase_order_re = re.compile(r"\\bOC\\s*:\\s*([^\\n]+)", re.I)
'''
new = '''    # Capture only the order token after OC instead of the whole remainder
    # of the line. The previous expression could turn trailing labels/text into
    # part of the order key and make every Fill Rate match fail silently.
    purchase_order_re = re.compile(
        r"\\bOC\\s*:\\s*((?:\\d[ -]*){6,20}|[A-Z0-9][A-Z0-9._/-]{4,30})",
        re.I,
    )
'''
if old not in text:
    raise SystemExit("No se encontró purchase_order_re esperado")
text = text.replace(old, new, 1)

old = '''        invoices_for_fill_rate = fill_rate_invoice_df if not fill_rate_invoice_df.empty else invoice_df
        order_summary, fill_detail = reconcile_fill_rate(orders_df, invoices_for_fill_rate, pd.Timestamp.today())
        expired = order_summary[order_summary["Estado_plazo"] == "Vencida"]
'''
new = '''        invoices_for_fill_rate = fill_rate_invoice_df if not fill_rate_invoice_df.empty else invoice_df
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
'''
if old not in text:
    raise SystemExit("No se encontró bloque principal de conciliación")
text = text.replace(old, new, 1)

old = '''        invoice_orders = set(invoice_df.get("purchase_order", pd.Series(dtype=str)).map(canonical_order)) - {""}
'''
new = '''        invoice_orders = set(invoices_for_fill_rate.get("purchase_order", pd.Series(dtype=str)).map(canonical_order)) - {""}
'''
if old not in text:
    raise SystemExit("No se encontró diagnóstico de invoice_orders")
text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
