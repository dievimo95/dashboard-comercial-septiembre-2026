from pathlib import Path

path = Path("app.py")
text = path.read_text(encoding="utf-8")

metrics_start = 'st.markdown(f\'<div class="hero"><h1>Control comercial</h1><p>{source_label}. Vea qué pasa y qué hacer.</p></div>\', unsafe_allow_html=True)\n\nc1, c2, c3, c4 = st.columns(4)\n'
metrics_end = '\nif sold_forecast / forecast_total > elapsed:\n'
start = text.find(metrics_start)
if start < 0:
    raise SystemExit("No se encontró el bloque de métricas del resumen")
end = text.find(metrics_end, start)
if end < 0:
    raise SystemExit("No se encontró el final del bloque de métricas")

new_metrics = '''st.markdown(f'<div class="hero"><h1>Control comercial</h1><p>{source_label}. Vea qué pasa y qué hacer.</p></div>', unsafe_allow_html=True)

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
c2.metric("Facturado", f"{sold_forecast:,.0f} un.")
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
)'''

text = text[:start] + new_metrics + text[end:]

tab1_start = "\nwith tab1:\n"
tab2_start = "\nwith tab2:\n"
start = text.find(tab1_start)
if start < 0:
    raise SystemExit("No se encontró with tab1")
end = text.find(tab2_start, start + len(tab1_start))
if end < 0:
    raise SystemExit("No se encontró with tab2")

new_tab1 = '''with tab1:
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
                st.markdown(f"**{recommendation}**")'''

replacement = "\n" + new_tab1.rstrip() + "\n"
text = text[:start] + replacement + text[end:]
path.write_text(text, encoding="utf-8")
