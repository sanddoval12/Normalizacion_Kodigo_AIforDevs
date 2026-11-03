"""Interfaz interactiva (Streamlit) para explorar transacciones normalizadas.

Ejecutar:  streamlit run app.py

Esta capa solo presenta datos: toda la lógica (normalización, validación,
métricas y filtros) vive en ``normalizacion.py``. Aquí no se transforma
ningún valor; los montos se muestran como texto para conservar el Decimal
exacto.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import streamlit as st

from normalizacion import (
    EstadoNormalizado,
    FiltroTransacciones,
    Metricas,
    ResultadoProcesamiento,
    TransaccionNormalizada,
    calcular_metricas,
    filtrar,
    procesar_texto,
)

EJEMPLO = Path(__file__).parent / "ejemplos" / "entrada_ejemplo.json"

st.set_page_config(page_title="Normalización de transacciones", page_icon="💱", layout="wide")


@st.cache_data(show_spinner="Normalizando transacciones…")
def _procesar(contenido: bytes, origen: str) -> ResultadoProcesamiento:
    return procesar_texto(contenido, origen=origen)


def _cargar_entrada() -> tuple[bytes | None, str]:
    st.sidebar.header("Entrada")
    subido = st.sidebar.file_uploader("Archivo JSON de transacciones", type=["json"])
    if subido is not None:
        return subido.getvalue(), subido.name
    if EJEMPLO.exists():
        st.sidebar.caption(f"Sin archivo subido: se usa `{EJEMPLO.name}`.")
        return EJEMPLO.read_bytes(), EJEMPLO.name
    return None, ""


def _filtros(todas: list[TransaccionNormalizada]) -> FiltroTransacciones:
    st.sidebar.header("Filtros")
    fuentes = sorted({t.fuente for t in todas})
    monedas = sorted({t.moneda for t in todas if t.moneda})
    fechas = [t.fecha_utc.date() for t in todas if t.fecha_utc]

    sel_fuentes = st.sidebar.multiselect("Fuente", fuentes)
    sel_estados = st.sidebar.multiselect("Estado", [e.value for e in EstadoNormalizado])
    sel_monedas = st.sidebar.multiselect("Moneda", monedas)
    validez = st.sidebar.radio(
        "Validez", ["todas", "validas", "invalidas"], horizontal=True,
        format_func={"todas": "Todas", "validas": "Válidas", "invalidas": "Inválidas"}.get,
    )
    desde = hasta = None
    if fechas and st.sidebar.checkbox("Filtrar por rango de fechas (UTC)"):
        rango = st.sidebar.date_input(
            "Rango", value=(min(fechas), max(fechas)), format="DD/MM/YYYY"
        )
        if isinstance(rango, date):
            desde = hasta = rango
        elif len(rango) == 2:
            desde, hasta = rango
        elif len(rango) == 1:  # el usuario aún no elige el fin del rango
            desde = rango[0]
    return FiltroTransacciones(
        fuentes=frozenset(sel_fuentes) or None,
        estados=frozenset(EstadoNormalizado(e) for e in sel_estados) or None,
        monedas=frozenset(sel_monedas) or None,
        desde=desde,
        hasta=hasta,
        validez=validez,
    )


def _fila(t: TransaccionNormalizada) -> dict[str, object]:
    return {
        "id": t.id,
        "fuente": t.fuente,
        "fecha_utc": t.fecha_utc.strftime("%Y-%m-%d %H:%M:%SZ") if t.fecha_utc else "—",
        "monto": str(t.monto) if t.monto is not None else "—",
        "moneda": t.moneda or "—",
        "estado": t.estado.value,
        "válida": "✅" if t.es_valida else "❌",
        "incidencias": "; ".join(i.codigo.value for i in t.incidencias),
    }


def _vista_metricas(m: Metricas) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Procesadas", m.total_procesadas)
    c2.metric("Válidas", m.total_validas)
    c3.metric("Inválidas", m.total_invalidas)
    c4.metric("Con advertencias", m.total_con_advertencias)

    izq, der = st.columns(2)
    with izq:
        st.subheader("Conteo por estado")
        if m.conteo_por_estado:
            st.bar_chart(m.conteo_por_estado, horizontal=True, y_label="", x_label="transacciones")
        st.subheader("Conteo por fuente")
        st.table({"fuente": list(m.conteo_por_fuente), "cantidad": list(m.conteo_por_fuente.values())})
    with der:
        st.subheader("Totales por moneda (solo válidas)")
        if m.totales_por_moneda:
            st.table({
                "moneda": list(m.totales_por_moneda),
                "total": [str(v) for v in m.totales_por_moneda.values()],
            })
            st.caption("Cada moneda se suma por separado; nunca se mezclan.")
            with st.expander("Desglose por estado"):
                filas = [
                    {"moneda": mon, "estado": est, "total": str(total)}
                    for mon, por_estado in m.totales_por_moneda_y_estado.items()
                    for est, total in por_estado.items()
                ]
                st.table(filas)
        else:
            st.info("No hay transacciones válidas en la selección.")
        st.subheader("Motivos de rechazo y advertencias")
        if m.conteo_por_motivo:
            st.table({"motivo": list(m.conteo_por_motivo), "cantidad": list(m.conteo_por_motivo.values())})
        else:
            st.caption("Sin incidencias.")


def _vista_detalle(t: TransaccionNormalizada) -> None:
    st.markdown(f"### `{t.id}` {'✅ válida' if t.es_valida else '❌ rechazada'}")
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**Fuente:** {t.fuente}  \n**Id de origen:** {t.id_origen or '—'}  \n"
                f"**Índice en el archivo:** {t.indice_origen}")
    c2.markdown(f"**Monto:** {t.monto if t.monto is not None else '—'} {t.moneda or ''}  \n"
                f"**Estado:** {t.estado.value} (origen: `{t.estado_origen or '—'}`)")
    c3.markdown(f"**Fecha UTC:** {t.fecha_utc.isoformat() if t.fecha_utc else '—'}  \n"
                f"**Zona asumida:** {t.zona_horaria_asumida or 'ninguna'}")
    st.markdown("**Incidencias**")
    if t.incidencias:
        for i in t.incidencias:
            texto = f"`{i.codigo.value}`{f' en *{i.campo}*' if i.campo else ''}: {i.mensaje}"
            (st.error if i.severidad == "error" else st.warning)(texto)
    else:
        st.success("Sin incidencias.")
    st.markdown("**Registro original**")
    original = json.loads(t.model_dump_json(include={"registro_original"}))["registro_original"]
    if isinstance(original, (dict, list)):
        st.json(original)
    else:  # p. ej. un registro que no era un objeto JSON
        st.code(json.dumps(original, ensure_ascii=False), language="json")


def main() -> None:
    st.title("Normalización de transacciones")
    contenido, origen = _cargar_entrada()
    if contenido is None:
        st.info("Suba un archivo JSON en la barra lateral para comenzar.")
        return

    resultado = _procesar(contenido, origen)
    for error in resultado.errores_carga:
        st.error(f"No se pudo procesar **{origen}**: {error}")
    if resultado.errores_carga:
        return

    todas = resultado.todas
    try:
        filtro = _filtros(todas)
    except ValueError as exc:
        st.sidebar.error(f"Filtro inválido: {exc}")
        filtro = FiltroTransacciones()
    vista = filtrar(todas, filtro)
    st.caption(f"**{origen}** · {len(vista)} de {len(todas)} transacciones según los filtros")

    pestana_metricas, pestana_tabla, pestana_detalle = st.tabs(["📊 Métricas", "📋 Transacciones", "🔎 Detalle"])
    with pestana_metricas:
        _vista_metricas(calcular_metricas(vista))
    with pestana_tabla:
        if not vista:
            st.info("Ningún registro cumple los filtros.")
        evento = st.dataframe(
            [_fila(t) for t in vista], hide_index=True, width="stretch",
            on_select="rerun", selection_mode="single-row", key="tabla",
        )
        st.caption("Seleccione una fila para verla en la pestaña Detalle.")
        st.download_button(
            "Descargar resultado completo (JSON)",
            resultado.model_dump_json(indent=2), file_name="resultado_normalizado.json",
            mime="application/json",
        )
    with pestana_detalle:
        if not vista:
            st.info("Ningún registro cumple los filtros.")
            return
        filas = evento.selection.rows if evento is not None else []
        ids = [t.id for t in vista]
        indice = filas[0] if filas and filas[0] < len(vista) else 0
        elegido = st.selectbox("Transacción", range(len(vista)), index=indice, format_func=ids.__getitem__)
        _vista_detalle(vista[elegido])


main()
