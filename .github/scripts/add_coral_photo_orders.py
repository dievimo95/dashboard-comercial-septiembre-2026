from pathlib import Path

path = Path('app.py')
text = path.read_text(encoding='utf-8')

# Imports
text = text.replace(
    'from datetime import datetime\nfrom pathlib import Path\n\nimport pandas as pd\n',
    'from datetime import datetime\nfrom pathlib import Path\nfrom difflib import SequenceMatcher\n\nimport pandas as pd\n'
)
text = text.replace(
    'import plotly.express as px\nimport streamlit as st\n',
    'import plotly.express as px\nimport streamlit as st\nfrom PIL import Image, ImageEnhance, ImageFilter, ImageOps\nimport pytesseract\n'
)

anchor = '''def canonical_order(value):
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

helper = r'''

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
'''

if helper.strip() not in text:
    text = text.replace(anchor, anchor + helper)

old_radio = 'entry_method = st.radio("Cómo ingresar el pedido", ["Subir PDF de clientes", "Subir Excel o CSV", "Escribir pedido"], horizontal=True)'
new_radio = 'entry_method = st.radio("Cómo ingresar el pedido", ["Subir PDF de clientes", "Foto pedido Coral", "Subir Excel o CSV", "Escribir pedido"], horizontal=True)'
if old_radio not in text:
    raise SystemExit('No se encontró selector de método de pedido')
text = text.replace(old_radio, new_radio, 1)

old_branch = '''    elif entry_method == "Subir Excel o CSV":
        order_file = st.file_uploader("Pedido semanal (.xlsx o .csv)", type=["xlsx", "csv"], key="weekly_order_file")
'''
new_branch = r'''    elif entry_method == "Foto pedido Coral":
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
'''
if old_branch not in text:
    raise SystemExit('No se encontró rama Excel/CSV para insertar Coral')
text = text.replace(old_branch, new_branch, 1)

path.write_text(text, encoding='utf-8')
