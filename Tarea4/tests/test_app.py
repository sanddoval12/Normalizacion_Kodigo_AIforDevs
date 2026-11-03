"""Prueba de humo de la interfaz Streamlit (se omite si Streamlit no está instalado)."""

from __future__ import annotations

import importlib.util
import unittest
import warnings
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app.py"


@unittest.skipUnless(importlib.util.find_spec("streamlit"), "Streamlit no está instalado")
class TestApp(unittest.TestCase):
    def setUp(self) -> None:
        from streamlit.testing.v1 import AppTest

        # Avisos internos de Streamlit sobre APIs de pandas (no del proyecto).
        warnings.filterwarnings("ignore", module=r"streamlit\..*")

        self.at = AppTest.from_file(str(APP), default_timeout=60).run()

    def test_carga_ejemplo_y_muestra_metricas(self) -> None:
        self.assertFalse(self.at.exception)
        metricas = {m.label: m.value for m in self.at.metric}
        self.assertEqual(metricas["Procesadas"], "12")
        self.assertEqual(metricas["Válidas"], "7")

    def test_filtros_recalculan_vistas(self) -> None:
        self.at.sidebar.radio[0].set_value("invalidas").run()
        self.assertEqual({m.label: m.value for m in self.at.metric}["Procesadas"], "5")
        self.at.sidebar.multiselect[0].set_value(["app_movil"]).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(self.at.info), 3)  # sin filas: aviso en métricas, tabla y detalle

    def test_detalle_de_registro_no_objeto(self) -> None:
        self.at.sidebar.radio[0].set_value("invalidas").run()
        self.at.selectbox[0].set_value(4).run()  # "registro corrupto"
        self.assertFalse(self.at.exception)
        self.assertTrue(any("registro_no_es_objeto" in e.value for e in self.at.error))

    def test_rango_de_fechas(self) -> None:
        self.at.sidebar.checkbox[0].check().run()
        self.assertFalse(self.at.exception)
        self.assertEqual({m.label: m.value for m in self.at.metric}["Procesadas"], "9")  # 9 registros con fecha válida


if __name__ == "__main__":
    unittest.main()
