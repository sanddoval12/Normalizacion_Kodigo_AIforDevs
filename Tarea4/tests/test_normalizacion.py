"""Pruebas básicas de normalizacion.py: transformación, validación, métricas,
filtros, carga robusta y CLI.

Ejecutar desde la raíz del proyecto:  python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

import normalizacion as n
from normalizacion import CodigoIncidencia as C
from normalizacion import EstadoNormalizado as E

RAIZ = Path(__file__).resolve().parent.parent
EJEMPLO_ENTRADA = RAIZ / "ejemplos" / "entrada_ejemplo.json"
EJEMPLO_SALIDA = RAIZ / "ejemplos" / "salida_esperada.json"


def pasarela(**cambios: Any) -> dict[str, Any]:
    base = {"transaction_id": "TX-1", "amount": "10.00", "currency": "USD",
            "status": "completed", "created_at": "2026-10-01T12:00:00Z"}
    return {**base, **cambios}


def codigos(t: n.TransaccionNormalizada) -> set[C]:
    return {i.codigo for i in t.incidencias}


# ─── Transformaciones ──────────────────────────────────────────────────────


class TestParsearMonto(unittest.TestCase):
    def test_formatos_de_separadores(self) -> None:
        casos = {
            ("1,250.50", None): Decimal("1250.50"),
            ("1.250,50", None): Decimal("1250.50"),
            ("1.250,50", ","): Decimal("1250.50"),
            ("1,234,567", None): Decimal("1234567"),
            ("1 234,5", None): Decimal("1234.5"),
            ("1'234.50", None): Decimal("1234.50"),
            ("0,500", None): Decimal("0.500"),
            ("1,234", "."): Decimal("1234"),
            ("1,234", ","): Decimal("1.234"),
            ("-12.5", None): Decimal("-12.5"),
            ("(1,000.00)", None): Decimal("-1000.00"),
            ("$ 99.99", None): Decimal("99.99"),
            ("1.234,56 €", None): Decimal("1234.56"),
        }
        for (texto, sep), esperado in casos.items():
            with self.subTest(texto=texto, sep=sep):
                self.assertEqual(n.parsear_monto(texto, separador_decimal=sep), esperado)

    def test_tipos_numericos_y_exactitud(self) -> None:
        self.assertEqual(n.parsear_monto(5), Decimal(5))
        self.assertEqual(n.parsear_monto(Decimal("0.10")), Decimal("0.10"))
        self.assertEqual(n.parsear_monto(0.1), Decimal("0.1"))  # no 0.1000000000000000055…

    def test_unidades_menores_segun_moneda(self) -> None:
        self.assertEqual(n.parsear_monto(125050, en_unidades_menores=True), Decimal("1250.50"))
        self.assertEqual(n.parsear_monto("1999", en_unidades_menores=True, decimales=0), Decimal(1999))
        self.assertEqual(n.parsear_monto(1234, en_unidades_menores=True, decimales=3), Decimal("1.234"))
        with self.assertRaises(n.MontoInvalidoError):
            n.parsear_monto("12.5", en_unidades_menores=True)

    def test_rechazos(self) -> None:
        for valor in [None, True, "", "abc", "1,234", "1.2.3,4", "NaN", "Infinity", "12a", [], {}]:
            with self.subTest(valor=valor), self.assertRaises(n.MontoInvalidoError):
                n.parsear_monto(valor)

    def test_ambiguedad_sugiere_declarar_separador(self) -> None:
        with self.assertRaisesRegex(n.MontoInvalidoError, "separador_decimal"):
            n.parsear_monto("1.234")


class TestNormalizarMoneda(unittest.TestCase):
    def test_codigos_alias_y_simbolos(self) -> None:
        casos = {"usd": "USD", " EUR ": "EUR", "€": "EUR", "dólares": "USD",
                 "Dolares": "USD", "US$": "USD", "R$": "BRL", "quetzales": "GTQ", "$": "USD"}
        for entrada, esperado in casos.items():
            with self.subTest(entrada=entrada):
                self.assertEqual(n.normalizar_moneda(entrada), esperado)

    def test_alias_del_adaptador_tiene_prioridad(self) -> None:
        self.assertEqual(n.normalizar_moneda("$", {"$": "MXN"}), "MXN")

    def test_no_reconocida(self) -> None:
        for valor in ["pesos de oro", "XYZ", "", None, 840, "¥"]:
            with self.subTest(valor=valor), self.assertRaises(n.MonedaNoReconocidaError):
                n.normalizar_moneda(valor)


class TestMapearEstado(unittest.TestCase):
    def test_tabla_global_tolerante_a_formato(self) -> None:
        self.assertIs(n.mapear_estado("COMPLETED"), E.COMPLETADA)
        self.assertIs(n.mapear_estado("Aprobada"), E.COMPLETADA)
        self.assertIs(n.mapear_estado("en_proceso"), E.PENDIENTE)
        self.assertIs(n.mapear_estado("Reembolsado"), E.REEMBOLSADA)

    def test_tabla_del_adaptador(self) -> None:
        self.assertIs(n.mapear_estado("REVERSED", {"REVERSED": E.REEMBOLSADA}), E.REEMBOLSADA)

    def test_desconocido_no_se_adivina(self) -> None:
        for valor in ["en revisión", "completed-ish", None, ""]:
            with self.subTest(valor=valor):
                self.assertIsNone(n.mapear_estado(valor))


class TestParsearFecha(unittest.TestCase):
    def test_con_zona_no_asume(self) -> None:
        fecha, zona = n.parsear_fecha("2026-10-01T09:00:00-06:00")
        self.assertEqual(fecha, datetime(2026, 10, 1, 15, 0, tzinfo=UTC))
        self.assertIsNone(zona)
        self.assertEqual(n.parsear_fecha("2026-10-01T15:00:00Z")[0], fecha)
        self.assertEqual(n.parsear_fecha("Thu, 01 Oct 2026 09:00:00 -0600")[0], fecha)

    def test_sin_zona_registra_la_asumida(self) -> None:
        fecha, zona = n.parsear_fecha("01/10/2026 08:15", zona_por_defecto="America/El_Salvador")
        self.assertEqual(fecha, datetime(2026, 10, 1, 14, 15, tzinfo=UTC))
        self.assertEqual(zona, "America/El_Salvador")

    def test_epoch(self) -> None:
        self.assertEqual(n.parsear_fecha(1790955900000, unidad_epoch="ms"),
                         (datetime(2026, 10, 2, 15, 45, tzinfo=UTC), None))
        self.assertEqual(n.parsear_fecha(1790955900)[0], datetime(2026, 10, 2, 15, 45, tzinfo=UTC))

    def test_formatos_personalizados(self) -> None:
        fecha, _ = n.parsear_fecha("10-01-2026", formatos=("%m-%d-%Y",))
        self.assertEqual(fecha.date(), date(2026, 10, 1))

    def test_rechazos(self) -> None:
        for valor in ["31/02/2026", "ayer", "", None, True, 10**20]:
            with self.subTest(valor=valor), self.assertRaises(n.FechaInvalidaError):
                n.parsear_fecha(valor)
        with self.assertRaisesRegex(n.FechaInvalidaError, "zona horaria"):
            n.parsear_fecha("2026-10-01", zona_por_defecto="Marte/Olympus")


# ─── Detección de fuente y normalización de registros ─────────────────────


class TestDeteccionYNormalizacion(unittest.TestCase):
    def test_cada_fuente_de_ejemplo(self) -> None:
        a = n.normalizar_registro(pasarela(amount="1,250.50"), 0)
        self.assertEqual((a.fuente, a.monto, a.moneda, a.estado),
                         ("pasarela_pagos", Decimal("1250.50"), "USD", E.COMPLETADA))
        self.assertTrue(a.es_valida)

        b = n.normalizar_registro({"referencia": "BL-1", "fecha": "01/10/2026 08:15",
                                   "importe": {"valor": "2.500,75", "divisa": "dólares"}, "estado": "Aprobada"}, 1)
        self.assertEqual((b.fuente, b.monto, b.moneda), ("banco_local", Decimal("2500.75"), "USD"))
        self.assertEqual(b.zona_horaria_asumida, "America/El_Salvador")
        self.assertEqual(codigos(b), {C.ZONA_HORARIA_ASUMIDA})
        self.assertTrue(b.es_valida)  # una advertencia no invalida

        c = n.normalizar_registro({"txnId": "m-1", "amountCents": 4999, "currencyCode": "usd",
                                   "state": "REVERSED", "timestampMs": 1790955900000}, 2)
        self.assertEqual((c.fuente, c.monto, c.estado), ("app_movil", Decimal("49.99"), E.REEMBOLSADA))
        self.assertEqual(c.id, "app_movil:m-1")

    def test_centavos_respetan_decimales_de_la_moneda(self) -> None:
        t = n.normalizar_registro({"txnId": "m-2", "amountCents": 1500, "currencyCode": "JPY",
                                   "state": "SUCCESS", "timestampMs": 1790955900000}, 0)
        self.assertEqual(t.monto, Decimal(1500))

    def test_fuente_no_reconocida_no_falla(self) -> None:
        t = n.normalizar_registro({"foo": 1}, 7)
        self.assertEqual((t.fuente, t.id, t.es_valida), (n.FUENTE_NO_RECONOCIDA, "no_reconocida:#7", False))
        self.assertEqual(codigos(t), {C.FUENTE_NO_RECONOCIDA})
        self.assertEqual(t.registro_original, {"foo": 1})

    def test_registro_que_no_es_objeto(self) -> None:
        for valor in ["texto", 42, None, [1, 2]]:
            with self.subTest(valor=valor):
                self.assertEqual(codigos(n.normalizar_registro(valor, 0)), {C.REGISTRO_NO_ES_OBJETO})

    def test_fuente_ambigua_y_adaptador_defectuoso(self) -> None:
        class Todo(n.AdaptadorFuente):
            nombre = "todo"
            def detectar(self, r: Mapping[str, Any]) -> bool: return True
            def extraer(self, r: Mapping[str, Any]) -> n.CamposExtraidos: raise KeyError("x")

        class DetectorRoto(n.AdaptadorFuente):
            nombre = "roto"
            def detectar(self, r: Mapping[str, Any]) -> bool: raise RuntimeError("bug")
            def extraer(self, r: Mapping[str, Any]) -> n.CamposExtraidos: return n.CamposExtraidos()

        with self.assertLogs("normalizacion", "ERROR"):
            t = n.normalizar_registro(pasarela(), 0, [Todo(), DetectorRoto()])
        self.assertEqual(codigos(t), {C.ERROR_ADAPTADOR})
        t = n.normalizar_registro(pasarela(), 0, [Todo(), *n.ADAPTADORES_REGISTRADOS])
        self.assertEqual(codigos(t), {C.FUENTE_AMBIGUA})

    def test_fuente_nueva_sin_tocar_el_nucleo(self) -> None:
        class Tarjeta(n.AdaptadorFuente):
            nombre = "tarjeta"
            alias_moneda = {"$": "MXN"}
            tabla_estados = {"00": E.COMPLETADA}
            def detectar(self, r: Mapping[str, Any]) -> bool: return "auth_code" in r
            def extraer(self, r: Mapping[str, Any]) -> n.CamposExtraidos:
                return n.CamposExtraidos(r["auth_code"], r["ts"], r["amt"], r["cur"], r["rc"])

        r = n.procesar_registros([{"auth_code": "A1", "ts": "2026-10-01T00:00:00Z",
                                   "amt": "5.00", "cur": "$", "rc": "00"}], [Tarjeta()])
        self.assertEqual(r.metricas.totales_por_moneda, {"MXN": Decimal("5.00")})

    def test_registrar_fuente_rechaza_nombre_repetido(self) -> None:
        with self.assertRaises(ValueError):
            @n.registrar_fuente
            class Otra(n.AdaptadorPasarelaPagos):
                pass


# ─── Validación ────────────────────────────────────────────────────────────


class TestValidacion(unittest.TestCase):
    def test_campos_obligatorios(self) -> None:
        for campo in ["transaction_id", "amount", "currency", "status", "created_at"]:
            with self.subTest(campo=campo):
                registro = pasarela(**{campo: None})
                t = n.normalizar_registro(registro, 0)
                self.assertFalse(t.es_valida)
                self.assertIn(C.CAMPO_OBLIGATORIO_FALTANTE, codigos(t))

    def test_motivos_acumulados_y_trazables(self) -> None:
        t = n.normalizar_registro(pasarela(amount="abc", currency="XYZ", status="??",
                                           created_at="mañana"), 3)
        self.assertEqual(codigos(t), {C.MONTO_INVALIDO, C.MONEDA_NO_RECONOCIDA,
                                      C.ESTADO_DESCONOCIDO, C.FECHA_INVALIDA})
        self.assertEqual(t.estado, E.DESCONOCIDO)
        self.assertEqual(t.estado_origen, "??")
        self.assertEqual(t.indice_origen, 3)
        self.assertTrue(all(i.severidad is n.Severidad.ERROR for i in t.incidencias))

    def test_centavos_sin_moneda_no_se_convierten(self) -> None:
        t = n.normalizar_registro({"txnId": "m", "amountCents": 100, "currencyCode": "zzz",
                                   "state": "SUCCESS", "timestampMs": 0}, 0)
        self.assertIsNone(t.monto)
        self.assertIn(C.MONTO_INVALIDO, codigos(t))

    def test_advertencias_no_invalidan(self) -> None:
        cero = n.normalizar_registro(pasarela(amount="0"), 0)
        negativo = n.normalizar_registro(pasarela(amount="-5"), 0)
        self.assertEqual((cero.es_valida, codigos(cero)), (True, {C.MONTO_CERO}))
        self.assertEqual((negativo.es_valida, codigos(negativo)), (True, {C.MONTO_NEGATIVO}))

    def test_duplicados_se_separan(self) -> None:
        r = n.procesar_registros([pasarela(), pasarela(), pasarela(transaction_id="TX-2")])
        self.assertEqual(len(r.validas), 2)
        self.assertEqual(codigos(r.rechazadas[0]), {C.ID_DUPLICADO})
        self.assertEqual(r.metricas.totales_por_moneda, {"USD": Decimal("20.00")})

    def test_invariantes_del_modelo(self) -> None:
        base = {"id": "x:1", "fuente": "x", "indice_origen": 0}
        with self.assertRaises(ValidationError):  # válida sin campos de negocio
            n.TransaccionNormalizada(**base)
        completa = {**base, "fecha_utc": datetime(2026, 1, 1, tzinfo=UTC), "monto": Decimal(1),
                    "moneda": "USD", "estado": E.COMPLETADA}
        n.TransaccionNormalizada(**completa)
        for cambio in [{"monto": 1.5}, {"moneda": "usd"}, {"moneda": "XYZ"},
                       {"fecha_utc": datetime(2026, 1, 1)}, {"monto": Decimal("NaN")}]:
            with self.subTest(cambio=cambio), self.assertRaises(ValidationError):
                n.TransaccionNormalizada(**(completa | cambio))

    def test_serializacion_exacta(self) -> None:
        t = n.normalizar_registro(pasarela(amount="0.10"), 0)
        datos = json.loads(t.model_dump_json())
        self.assertEqual(datos["monto"], "0.10")
        self.assertEqual(datos["fecha_utc"], "2026-10-01T12:00:00Z")
        self.assertTrue(datos["es_valida"])


# ─── Métricas y filtros ────────────────────────────────────────────────────


class TestMetricas(unittest.TestCase):
    def setUp(self) -> None:
        self.r = n.procesar_registros([
            pasarela(transaction_id="1", amount="10.10"),
            pasarela(transaction_id="2", amount="5.05", status="failed"),
            pasarela(transaction_id="3", amount="7", currency="EUR"),
            pasarela(transaction_id="4", amount="abc"),           # inválida
            pasarela(transaction_id="5", currency="XYZ"),         # inválida
        ])

    def test_conteos(self) -> None:
        m = self.r.metricas
        self.assertEqual((m.total_procesadas, m.total_validas, m.total_invalidas), (5, 3, 2))
        self.assertEqual(m.conteo_por_estado, {"completada": 4, "fallida": 1})
        self.assertEqual(m.conteo_por_motivo, {"moneda_no_reconocida": 1, "monto_invalido": 1})

    def test_totales_por_moneda_solo_validas_sin_mezclar(self) -> None:
        m = self.r.metricas
        self.assertEqual(m.totales_por_moneda, {"EUR": Decimal("7"), "USD": Decimal("15.15")})
        self.assertEqual(m.totales_por_moneda_y_estado["USD"],
                         {"completada": Decimal("10.10"), "fallida": Decimal("5.05")})

    def test_vacio(self) -> None:
        self.assertEqual(n.calcular_metricas([]), n.Metricas())


class TestFiltros(unittest.TestCase):
    def setUp(self) -> None:
        self.todas = n.procesar_texto(EJEMPLO_ENTRADA.read_bytes()).todas

    def ids(self, **criterios: Any) -> set[str]:
        return {t.id for t in n.filtrar(self.todas, n.FiltroTransacciones(**criterios))}

    def test_por_criterio(self) -> None:
        self.assertEqual(self.ids(fuentes={"app_movil"}), {"app_movil:m-501", "app_movil:m-502", "app_movil:m-503"})
        self.assertEqual(self.ids(estados={"pendiente"}), {"pasarela_pagos:TX-1002"})
        self.assertEqual(self.ids(monedas={"MXN"}), {"app_movil:m-502"})
        self.assertEqual(len(self.ids(validez="invalidas")), 5)
        self.assertEqual(self.ids(validez="validas", estados={"reembolsada"}, monedas={"USD"}),
                         {"banco_local:BL-0078"})

    def test_rango_de_fechas_inclusivo_en_utc(self) -> None:
        self.assertEqual(self.ids(desde=date(2026, 10, 5), hasta=date(2026, 10, 5)), {"app_movil:m-503"})
        self.assertNotIn("no_reconocida:#10", self.ids(desde=date(2000, 1, 1)))

    def test_rango_incoherente(self) -> None:
        with self.assertRaises(ValidationError):
            n.FiltroTransacciones(desde=date(2026, 2, 1), hasta=date(2026, 1, 1))


# ─── Carga robusta, ejemplo de referencia y CLI ───────────────────────────


class TestCarga(unittest.TestCase):
    def test_errores_de_archivo_se_reportan_sin_lanzar(self) -> None:
        casos = {
            b"{no es json": "JSON mal formado",
            b'{"otra_cosa": 1}': "sin lista de transacciones",
            b'"hola"': "lista u objeto",
            "año".encode("latin-1"): "UTF-8",
        }
        for contenido, fragmento in casos.items():
            with self.subTest(contenido=contenido):
                with self.assertLogs("normalizacion", "ERROR"):
                    r = n.procesar_texto(contenido)
                self.assertEqual(r.metricas.total_procesadas, 0)
                self.assertIn(fragmento, r.errores_carga[0])

    def test_archivo_inexistente(self) -> None:
        with self.assertLogs("normalizacion", "ERROR"):
            r = n.procesar_archivo(RAIZ / "no_existe.json")
        self.assertIn("no se pudo leer", r.errores_carga[0])
        with self.assertRaises(n.ErrorCargaArchivo):
            n.cargar_registros(RAIZ / "no_existe.json")

    def test_raiz_lista_bom_y_constantes_no_finitas(self) -> None:
        con_nan = ('{"transaction_id": "N", "created_at": "2026-10-01", "amount": NaN, '
                   '"currency": "USD", "status": "paid"}')
        contenido = "\ufeff" + json.dumps([pasarela()])[:-1] + ", " + con_nan + "]"
        r = n.procesar_texto(contenido.encode())
        self.assertEqual((len(r.validas), len(r.rechazadas)), (1, 1))
        self.assertIn(C.MONTO_INVALIDO, codigos(r.rechazadas[0]))

    def test_decimales_exactos_desde_json(self) -> None:
        r = n.procesar_texto(json.dumps([pasarela(amount=None)]).replace("null", "0.1").encode())
        self.assertEqual(r.validas[0].monto, Decimal("0.1"))


class TestEjemploDeReferencia(unittest.TestCase):
    """La salida documentada en ejemplos/ debe coincidir con la real."""

    def test_salida_esperada(self) -> None:
        real = json.loads(n.procesar_archivo(EJEMPLO_ENTRADA).model_dump_json())
        esperada = json.loads(EJEMPLO_SALIDA.read_text(encoding="utf-8"))
        real.pop("origen"), esperada.pop("origen")
        self.assertEqual(real, esperada)


class TestCLI(unittest.TestCase):
    def test_codigos_de_salida_y_archivo(self) -> None:
        mensajes: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            salida = Path(tmp) / "out.json"
            self.assertEqual(n.main([str(EJEMPLO_ENTRADA), "--salida", str(salida)], mensajes.append), 0)
            self.assertEqual(json.loads(salida.read_text(encoding="utf-8"))["metricas"]["total_validas"], 7)
            self.assertEqual(n.main([str(EJEMPLO_ENTRADA), "--estricto"], mensajes.append), 2)
            with self.assertLogs("normalizacion", "ERROR"):
                self.assertEqual(n.main([str(Path(tmp) / "nada.json")], mensajes.append), 1)
        self.assertIn("Válidas: 7", mensajes[0])


if __name__ == "__main__":
    unittest.main()
