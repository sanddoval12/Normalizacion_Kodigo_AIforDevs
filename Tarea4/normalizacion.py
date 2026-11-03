"""Normalización de transacciones financieras provenientes de múltiples fuentes.

Flujo:

    JSON de entrada ──► cargar_registros() ──► detección de fuente (adaptadores)
        ──► transformación (monto, moneda, estado, fecha) ──► validación
        ──► ResultadoProcesamiento(validas, rechazadas, metricas, errores_carga)

Principios de diseño:

* **Nada se adivina.** Un estado, una moneda o un separador decimal ambiguo se
  marca como incidencia; nunca se rellena con un valor "probable".
* **Nada se pierde.** Ningún registro se descarta en silencio: los que fallan
  van al conjunto de rechazados con el registro original y los motivos.
* **Nada detiene el lote.** Un registro corrupto, un adaptador con un error o
  un JSON mal formado se reportan; el resto del archivo se sigue procesando.
* **Agregar una fuente no toca el núcleo.** Cada fuente es una subclase de
  :class:`AdaptadorFuente` registrada con :func:`registrar_fuente`.

Uso por línea de comandos (procesamiento por lotes, no interactivo)::

    python normalizacion.py ejemplos/entrada_ejemplo.json --salida salida.json

La interfaz interactiva está en ``app.py`` (Streamlit).

Compatibilidad: Python 3.12.8. Dependencias: Pydantic 2 (y ``tzdata`` en
Windows, que no trae base de datos de zonas horarias).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import unicodedata
from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

__all__ = [
    "ADAPTADORES_REGISTRADOS",
    "AdaptadorFuente",
    "CamposExtraidos",
    "CodigoIncidencia",
    "ErrorCargaArchivo",
    "ErrorNormalizacion",
    "EstadoNormalizado",
    "FechaInvalidaError",
    "FiltroTransacciones",
    "Incidencia",
    "Metricas",
    "MonedaNoReconocidaError",
    "MontoInvalidoError",
    "ResultadoProcesamiento",
    "Severidad",
    "TransaccionNormalizada",
    "calcular_metricas",
    "cargar_registros",
    "filtrar",
    "normalizar_moneda",
    "normalizar_registro",
    "obtener_ruta",
    "parsear_fecha",
    "parsear_monto",
    "procesar_archivo",
    "procesar_registros",
    "procesar_texto",
    "registrar_fuente",
]

logger = logging.getLogger(__name__)

FUENTE_NO_RECONOCIDA = "no_reconocida"
"""Valor de ``fuente`` para registros que no coinciden con ningún adaptador."""


# ═══════════════════════════════════════════════════════════════════════════
# Excepciones
# ═══════════════════════════════════════════════════════════════════════════


class ErrorNormalizacion(Exception):
    """Base de los errores de transformación de un campo individual.

    Las funciones ``parsear_*`` y ``normalizar_*`` lanzan subclases de esta
    excepción; el núcleo las captura y las convierte en :class:`Incidencia`.
    """


class MontoInvalidoError(ErrorNormalizacion):
    """El monto no es numérico, es ambiguo o no puede convertirse a Decimal."""


class MonedaNoReconocidaError(ErrorNormalizacion):
    """La moneda no corresponde a ningún código ISO 4217 ni alias conocido."""


class FechaInvalidaError(ErrorNormalizacion):
    """La fecha no coincide con ningún formato aceptado."""


class ErrorCargaArchivo(Exception):
    """El archivo completo no pudo leerse o no tiene la forma esperada."""


# ═══════════════════════════════════════════════════════════════════════════
# Catálogos: estados, monedas
# ═══════════════════════════════════════════════════════════════════════════


class EstadoNormalizado(StrEnum):
    """Estados del esquema común. ``DESCONOCIDO`` nunca se asigna por intuición:
    significa que el estado de origen falta o no está en ninguna tabla."""

    COMPLETADA = "completada"
    PENDIENTE = "pendiente"
    FALLIDA = "fallida"
    CANCELADA = "cancelada"
    REEMBOLSADA = "reembolsada"
    DESCONOCIDO = "desconocido"


def _clave_texto(valor: str) -> str:
    """Clave de búsqueda canónica: sin acentos, minúsculas, espacios simples."""
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFKD", valor) if not unicodedata.combining(c)
    )
    return " ".join(sin_acentos.casefold().replace("-", " ").replace("_", " ").split())


def _tabla(
    grupos: Mapping[Any, Iterable[str]],
) -> dict[str, Any]:
    """Invierte ``{destino: [alias, ...]}`` a ``{clave_canonica: destino}``."""
    tabla: dict[str, Any] = {}
    for destino, alias in grupos.items():
        for a in alias:
            clave = _clave_texto(a)
            if clave in tabla and tabla[clave] != destino:
                raise ValueError(f"Alias duplicado con destinos distintos: {a!r}")
            tabla[clave] = destino
    return tabla


TABLA_ESTADOS: dict[str, EstadoNormalizado] = _tabla(
    {
        EstadoNormalizado.COMPLETADA: [
            "completed", "complete", "completado", "completada", "success",
            "succeeded", "successful", "exitoso", "exitosa", "approved",
            "aprobado", "aprobada", "paid", "pagado", "pagada", "settled",
            "liquidado", "liquidada",
        ],
        EstadoNormalizado.PENDIENTE: [
            "pending", "pendiente", "processing", "procesando", "en proceso",
            "in progress", "authorized", "autorizado", "autorizada",
        ],
        EstadoNormalizado.FALLIDA: [
            "failed", "failure", "fallido", "fallida", "error", "declined",
            "rechazado", "rechazada", "denied", "denegado", "denegada",
        ],
        EstadoNormalizado.CANCELADA: [
            "cancelled", "canceled", "cancelado", "cancelada", "void",
            "voided", "anulado", "anulada",
        ],
        EstadoNormalizado.REEMBOLSADA: [
            "refunded", "reembolsado", "reembolsada", "devuelto", "devuelta",
        ],
    }
)
"""Mapeo global estado de origen → estado normalizado (claves canónicas).
Los adaptadores pueden añadir entradas propias que tienen prioridad."""


MONEDAS_ISO4217: dict[str, int] = {
    # código: número de decimales de la unidad menor (ISO 4217)
    "USD": 2, "EUR": 2, "GBP": 2, "CHF": 2, "CAD": 2, "CNY": 2, "JPY": 0,
    "MXN": 2, "GTQ": 2, "HNL": 2, "NIO": 2, "CRC": 2, "PAB": 2, "DOP": 2,
    "COP": 2, "PEN": 2, "CLP": 0, "ARS": 2, "BRL": 2, "KWD": 3,
}
"""Monedas soportadas y sus decimales. El número de decimales se usa para
convertir montos expresados en unidades menores (centavos)."""


ALIAS_MONEDA: dict[str, str] = _tabla(
    {
        # "$" se asigna a USD por convención del proyecto (operación en una
        # economía dolarizada). Una fuente que use "$" para otra moneda debe
        # declararlo en su adaptador (``alias_moneda``), que tiene prioridad.
        "USD": ["$", "us$", "u$s", "dolar", "dolares", "dollar", "dollars",
                "us dollar", "dolar estadounidense", "dolares estadounidenses"],
        "EUR": ["€", "euro", "euros"],
        "GBP": ["£", "libra esterlina", "libras esterlinas", "pound sterling"],
        "MXN": ["mx$", "peso mexicano", "pesos mexicanos"],
        "GTQ": ["quetzal", "quetzales"],
        "HNL": ["lempira", "lempiras"],
        "NIO": ["cordoba", "cordobas"],
        "CRC": ["colon costarricense", "colones costarricenses"],
        "PAB": ["balboa", "balboas"],
        "COP": ["peso colombiano", "pesos colombianos"],
        "PEN": ["sol", "soles", "s/"],
        "CLP": ["peso chileno", "pesos chilenos"],
        "ARS": ["peso argentino", "pesos argentinos"],
        "BRL": ["r$", "real", "reales", "reais"],
        "JPY": ["yen", "yenes"],
        "CNY": ["yuan", "renminbi"],
        "CHF": ["franco suizo", "francos suizos"],
        "CAD": ["dolar canadiense", "dolares canadienses"],
    }
)
"""Alias aceptados (claves canónicas) → código ISO 4217. Símbolos ambiguos como
"¥", "C$" o "₡" no se incluyen a propósito."""


# ═══════════════════════════════════════════════════════════════════════════
# Modelo de datos (Pydantic)
# ═══════════════════════════════════════════════════════════════════════════


class Severidad(StrEnum):
    """ERROR separa el registro (inválido); ADVERTENCIA solo lo marca."""

    ERROR = "error"
    ADVERTENCIA = "advertencia"


class CodigoIncidencia(StrEnum):
    """Catálogo cerrado de motivos de rechazo o advertencia."""

    REGISTRO_NO_ES_OBJETO = "registro_no_es_objeto"
    FUENTE_NO_RECONOCIDA = "fuente_no_reconocida"
    FUENTE_AMBIGUA = "fuente_ambigua"
    ERROR_ADAPTADOR = "error_adaptador"
    CAMPO_OBLIGATORIO_FALTANTE = "campo_obligatorio_faltante"
    MONTO_INVALIDO = "monto_invalido"
    MONEDA_NO_RECONOCIDA = "moneda_no_reconocida"
    FECHA_INVALIDA = "fecha_invalida"
    ESTADO_DESCONOCIDO = "estado_desconocido"
    ID_DUPLICADO = "id_duplicado"
    # Advertencias
    ZONA_HORARIA_ASUMIDA = "zona_horaria_asumida"
    MONTO_CERO = "monto_cero"
    MONTO_NEGATIVO = "monto_negativo"


SEVERIDAD_POR_CODIGO: dict[CodigoIncidencia, Severidad] = {
    c: Severidad.ERROR for c in CodigoIncidencia
} | {
    CodigoIncidencia.ZONA_HORARIA_ASUMIDA: Severidad.ADVERTENCIA,
    CodigoIncidencia.MONTO_CERO: Severidad.ADVERTENCIA,
    CodigoIncidencia.MONTO_NEGATIVO: Severidad.ADVERTENCIA,
}


class Incidencia(BaseModel):
    """Motivo de rechazo o advertencia asociado a una transacción."""

    model_config = ConfigDict(frozen=True)

    codigo: CodigoIncidencia
    severidad: Severidad
    campo: str | None = Field(default=None, description="Campo normalizado afectado")
    mensaje: str = Field(min_length=1)

    @classmethod
    def crear(cls, codigo: CodigoIncidencia, mensaje: str, campo: str | None = None) -> Incidencia:
        """Crea la incidencia con la severidad definida en el catálogo."""
        return cls(codigo=codigo, severidad=SEVERIDAD_POR_CODIGO[codigo], campo=campo, mensaje=mensaje)


class TransaccionNormalizada(BaseModel):
    """Transacción en el esquema común.

    Los campos de negocio son opcionales porque un registro rechazado puede no
    tenerlos; el validador de modelo garantiza que una transacción **válida**
    siempre los tiene todos.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, description="Identificador único global: '<fuente>:<id_origen>'")
    id_origen: str | None = Field(default=None, description="Identificador tal como lo envió la fuente")
    fuente: str = Field(min_length=1, description="Adaptador que reconoció el registro")
    indice_origen: int = Field(ge=0, description="Posición del registro en el archivo de entrada")
    fecha_utc: datetime | None = Field(default=None, description="Fecha y hora ISO 8601 en UTC")
    zona_horaria_asumida: str | None = Field(
        default=None, description="Zona IANA aplicada cuando la fuente no indicó zona"
    )
    monto: Decimal | None = Field(default=None, description="Monto exacto en unidades mayores")
    moneda: str | None = Field(default=None, pattern=r"^[A-Z]{3}$", description="Código ISO 4217")
    estado: EstadoNormalizado = EstadoNormalizado.DESCONOCIDO
    estado_origen: str | None = Field(default=None, description="Estado tal como lo envió la fuente")
    incidencias: tuple[Incidencia, ...] = ()
    registro_original: Any = Field(default=None, description="Copia del registro de entrada")

    @field_validator("fecha_utc")
    @classmethod
    def _fecha_en_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("fecha_utc debe tener zona horaria")
        return v.astimezone(UTC)

    @field_validator("monto", mode="before")
    @classmethod
    def _monto_sin_float(cls, v: Any) -> Any:
        if isinstance(v, float):
            raise ValueError("monto no acepta float; use Decimal o str")
        return v

    @field_validator("monto")
    @classmethod
    def _monto_finito(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and not v.is_finite():
            raise ValueError("monto debe ser finito")
        return v

    @field_validator("moneda")
    @classmethod
    def _moneda_soportada(cls, v: str | None) -> str | None:
        if v is not None and v not in MONEDAS_ISO4217:
            raise ValueError(f"moneda {v!r} no está en el catálogo ISO 4217 soportado")
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def es_valida(self) -> bool:
        """``True`` si no hay ninguna incidencia de severidad ERROR."""
        return not any(i.severidad is Severidad.ERROR for i in self.incidencias)

    @model_validator(mode="after")
    def _invariante_validez(self) -> TransaccionNormalizada:
        if self.es_valida:
            faltantes = [
                n for n, v in (("fecha_utc", self.fecha_utc), ("monto", self.monto), ("moneda", self.moneda))
                if v is None
            ]
            if faltantes or self.estado is EstadoNormalizado.DESCONOCIDO:
                raise ValueError(
                    f"transacción marcada válida con campos incompletos: {faltantes or ['estado']}"
                )
        return self

    def con_incidencia(self, incidencia: Incidencia) -> TransaccionNormalizada:
        """Devuelve una copia con una incidencia adicional (el modelo es inmutable)."""
        return self.model_copy(update={"incidencias": (*self.incidencias, incidencia)})

    @property
    def motivos(self) -> list[str]:
        """Mensajes de incidencias, útil para vistas resumidas."""
        return [f"[{i.severidad}] {i.codigo}: {i.mensaje}" for i in self.incidencias]


class Metricas(BaseModel):
    """Métricas de un conjunto de transacciones.

    ``totales_por_moneda`` suma solo transacciones válidas y nunca mezcla
    monedas. Incluye todos los estados (también fallidas o canceladas); para
    saber cuánto dinero se movió realmente use ``totales_por_moneda_y_estado``.
    """

    total_procesadas: int = 0
    total_validas: int = 0
    total_invalidas: int = 0
    total_con_advertencias: int = 0
    conteo_por_estado: dict[str, int] = Field(default_factory=dict)
    conteo_por_fuente: dict[str, int] = Field(default_factory=dict)
    conteo_por_motivo: dict[str, int] = Field(default_factory=dict)
    totales_por_moneda: dict[str, Decimal] = Field(default_factory=dict)
    totales_por_moneda_y_estado: dict[str, dict[str, Decimal]] = Field(default_factory=dict)


class ResultadoProcesamiento(BaseModel):
    """Salida completa de un procesamiento."""

    origen: str | None = Field(default=None, description="Archivo o nombre de la entrada")
    errores_carga: list[str] = Field(
        default_factory=list, description="Problemas del archivo completo (no de un registro)"
    )
    validas: list[TransaccionNormalizada] = Field(default_factory=list)
    rechazadas: list[TransaccionNormalizada] = Field(default_factory=list)
    metricas: Metricas = Field(default_factory=Metricas)

    @property
    def todas(self) -> list[TransaccionNormalizada]:
        """Válidas y rechazadas en el orden original del archivo."""
        return sorted([*self.validas, *self.rechazadas], key=lambda t: t.indice_origen)


# ═══════════════════════════════════════════════════════════════════════════
# Transformaciones de campo
# ═══════════════════════════════════════════════════════════════════════════

_SIMBOLOS_EN_MONTO = "$€£¥₡"
_SEPARADORES_MILES_EXTRA = (" ", " ", " ", "'", "’")


def parsear_monto(
    valor: Any,
    *,
    separador_decimal: Literal[".", ","] | None = None,
    en_unidades_menores: bool = False,
    decimales: int = 2,
) -> Decimal:
    """Convierte ``valor`` a :class:`Decimal` exacto.

    Args:
        valor: ``int``, ``Decimal``, ``float`` o ``str``. Las cadenas admiten
            signo (``-``, ``+`` o paréntesis contables), separadores de miles
            (``,``, ``.``, espacio, apóstrofo) y un símbolo de moneda al inicio
            o al final.
        separador_decimal: convención de la fuente. Si es ``None`` se infiere,
            pero un caso ambiguo como ``"1,234"`` (¿mil doscientos o uno coma
            dos?) se rechaza en lugar de adivinarse.
        en_unidades_menores: el valor viene en centavos u otra unidad menor y
            debe ser entero.
        decimales: decimales de la moneda (2 para USD, 0 para JPY, 3 para KWD)
            usados al convertir unidades menores.

    Raises:
        MontoInvalidoError: si el valor no es interpretable sin ambigüedad.
    """
    if valor is None:
        raise MontoInvalidoError("monto ausente (null)")
    if isinstance(valor, bool):
        raise MontoInvalidoError(f"monto booleano no es numérico: {valor!r}")
    if isinstance(valor, Decimal):
        numero = valor
    elif isinstance(valor, int):
        numero = Decimal(valor)
    elif isinstance(valor, float):
        # repr() da la representación decimal más corta que reproduce el float.
        numero = Decimal(repr(valor))
    elif isinstance(valor, str):
        numero = _parsear_monto_texto(valor, separador_decimal)
    else:
        raise MontoInvalidoError(f"tipo de monto no soportado: {type(valor).__name__}")

    if not numero.is_finite():
        raise MontoInvalidoError(f"monto no finito: {valor!r}")
    if en_unidades_menores:
        if numero != numero.to_integral_value():
            raise MontoInvalidoError(f"monto en unidades menores debe ser entero: {valor!r}")
        numero = Decimal(int(numero)).scaleb(-decimales)
    return numero


def _parsear_monto_texto(texto: str, separador_decimal: str | None) -> Decimal:
    s = texto.strip()
    if not s:
        raise MontoInvalidoError("monto vacío")
    negativo = False
    if s.startswith("(") and s.endswith(")"):
        negativo, s = True, s[1:-1].strip()
    s = s.strip(_SIMBOLOS_EN_MONTO).strip()
    if s[:1] in "+-":
        negativo, s = negativo ^ (s[0] == "-"), s[1:].strip()
    s = s.strip(_SIMBOLOS_EN_MONTO).strip()
    for extra in _SEPARADORES_MILES_EXTRA:
        s = s.replace(extra, "_")  # marcador temporal de miles
    if not s or not re.fullmatch(r"[0-9.,_]+", s):
        raise MontoInvalidoError(f"monto no numérico: {texto!r}")

    decimal = separador_decimal or _inferir_separador_decimal(s, texto)
    miles = "," if decimal == "." else "."
    s = s.replace("_", miles)
    patron = (
        rf"(?P<entero>\d{{1,3}}(?:{re.escape(miles)}\d{{3}})+|\d+)"
        rf"(?:{re.escape(decimal)}(?P<frac>\d+))?"
    )
    m = re.fullmatch(patron, s)
    if m is None:
        raise MontoInvalidoError(
            f"formato de monto inválido para separador decimal {decimal!r}: {texto!r}"
        )
    entero = m["entero"].replace(miles, "")
    numero = Decimal(f"{entero}.{m['frac']}" if m["frac"] else entero)
    return -numero if negativo else numero


def _inferir_separador_decimal(s: str, original: str) -> str:
    """Infiere el separador decimal; lanza error si el caso es ambiguo."""
    pos_punto, pos_coma = s.rfind("."), s.rfind(",")
    if pos_punto >= 0 and pos_coma >= 0:
        return "." if pos_punto > pos_coma else ","
    sep = "." if pos_punto >= 0 else "," if pos_coma >= 0 else None
    if sep is None:
        return "."
    if s.count(sep) > 1:  # "1.234.567" → solo puede ser miles
        return "," if sep == "." else "."
    entero, frac = s.split(sep)
    entero = entero.replace("_", "")
    if len(frac) == 3 and entero not in ("", "0"):
        raise MontoInvalidoError(
            f"monto ambiguo {original!r}: {sep!r} puede ser separador de miles o decimal; "
            "declare 'separador_decimal' en el adaptador de la fuente"
        )
    return sep


def normalizar_moneda(valor: Any, alias_extra: Mapping[str, str] | None = None) -> str:
    """Convierte un código, nombre o símbolo de moneda a código ISO 4217.

    Prioridad: ``alias_extra`` (del adaptador) → código ISO directo →
    :data:`ALIAS_MONEDA`. La comparación ignora mayúsculas, acentos y espacios.

    Raises:
        MonedaNoReconocidaError: si no hay correspondencia.
    """
    if not isinstance(valor, str) or not valor.strip():
        raise MonedaNoReconocidaError(f"moneda ausente o no textual: {valor!r}")
    clave = _clave_texto(valor)
    if alias_extra:
        extra = {_clave_texto(k): v for k, v in alias_extra.items()}
        if clave in extra:
            return extra[clave]
    codigo = clave.upper()
    if codigo in MONEDAS_ISO4217:
        return codigo
    if clave in ALIAS_MONEDA:
        return ALIAS_MONEDA[clave]
    raise MonedaNoReconocidaError(f"moneda no reconocida: {valor!r}")


FORMATOS_FECHA_POR_DEFECTO: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y",
)
"""Formatos probados después de ISO 8601 y RFC 2822. Día antes que mes
(convención latinoamericana); una fuente con mes/día declara sus formatos."""


def parsear_fecha(
    valor: Any,
    *,
    zona_por_defecto: str = "UTC",
    formatos: Sequence[str] = FORMATOS_FECHA_POR_DEFECTO,
    unidad_epoch: Literal["s", "ms"] = "s",
) -> tuple[datetime, str | None]:
    """Convierte ``valor`` a ``datetime`` en UTC.

    Acepta ``datetime``, ``date``, epoch Unix (``int``/``Decimal``, en la
    unidad indicada), ISO 8601 (incluido el sufijo ``Z``), RFC 2822 y los
    ``formatos`` de ``strptime`` dados.

    Returns:
        ``(fecha_utc, zona_asumida)``. ``zona_asumida`` es el nombre IANA de
        ``zona_por_defecto`` si la entrada no traía zona; ``None`` si la traía.
        Un epoch Unix es UTC por definición y no implica suposición.

    Raises:
        FechaInvalidaError: si no se puede interpretar o la zona no existe.
    """
    try:
        zona = ZoneInfo(zona_por_defecto)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise FechaInvalidaError(
            f"zona horaria por defecto inválida {zona_por_defecto!r} "
            "(en Windows instale el paquete 'tzdata')"
        ) from exc

    if valor is None:
        raise FechaInvalidaError("fecha ausente (null)")
    if isinstance(valor, bool):
        raise FechaInvalidaError(f"fecha booleana: {valor!r}")
    if isinstance(valor, (int, Decimal)):
        divisor = 1000 if unidad_epoch == "ms" else 1
        try:
            return datetime.fromtimestamp(float(Decimal(valor) / divisor), tz=UTC), None
        except (OverflowError, OSError, ValueError, InvalidOperation) as exc:
            raise FechaInvalidaError(f"epoch fuera de rango: {valor!r}") from exc

    if isinstance(valor, datetime):
        fecha: datetime = valor
    elif isinstance(valor, date):
        fecha = datetime.combine(valor, time())
    elif isinstance(valor, str) and valor.strip():
        fecha = _parsear_fecha_texto(valor.strip(), formatos)
    else:
        raise FechaInvalidaError(f"fecha vacía o de tipo no soportado: {valor!r}")

    if fecha.tzinfo is None or fecha.utcoffset() is None:
        return fecha.replace(tzinfo=zona).astimezone(UTC), zona_por_defecto
    return fecha.astimezone(UTC), None


def _parsear_fecha_texto(texto: str, formatos: Sequence[str]) -> datetime:
    try:
        return datetime.fromisoformat(texto)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(texto)
    except (TypeError, ValueError, IndexError):
        pass
    for formato in formatos:
        try:
            return datetime.strptime(texto, formato)
        except ValueError:
            continue
    raise FechaInvalidaError(f"fecha con formato no reconocido: {texto!r}")


def mapear_estado(
    valor: Any, tabla_extra: Mapping[str, EstadoNormalizado] | None = None
) -> EstadoNormalizado | None:
    """Busca el estado en la tabla del adaptador y luego en la global.

    Returns:
        El estado normalizado, o ``None`` si no está mapeado (nunca se adivina).
    """
    if valor is None or isinstance(valor, bool):
        return None
    clave = _clave_texto(str(valor))
    if tabla_extra:
        extra = {_clave_texto(k): v for k, v in tabla_extra.items()}
        if clave in extra:
            return extra[clave]
    return TABLA_ESTADOS.get(clave)


# ═══════════════════════════════════════════════════════════════════════════
# Adaptadores de fuente
# ═══════════════════════════════════════════════════════════════════════════

_FALTA = object()


def obtener_ruta(registro: Mapping[str, Any], ruta: str, defecto: Any = None) -> Any:
    """Lee un valor anidado con notación de puntos sin lanzar ``KeyError``.

    >>> obtener_ruta({"a": {"b": 1}}, "a.b")
    1
    >>> obtener_ruta({"a": 1}, "a.b") is None
    True
    """
    actual: Any = registro
    for parte in ruta.split("."):
        if not isinstance(actual, Mapping):
            return defecto
        actual = actual.get(parte, _FALTA)
        if actual is _FALTA:
            return defecto
    return actual


@dataclass(frozen=True, slots=True)
class CamposExtraidos:
    """Valores crudos que un adaptador saca de un registro, sin transformar."""

    id_origen: Any = None
    fecha: Any = None
    monto: Any = None
    moneda: Any = None
    estado: Any = None


class AdaptadorFuente(ABC):
    """Contrato de una fuente. Para agregar una fuente nueva:

    1. Subclasear, fijar ``nombre`` y la configuración de clase necesaria.
    2. Implementar :meth:`detectar` (estructura) y :meth:`extraer` (valores).
    3. Decorar con :func:`registrar_fuente`.

    El núcleo aplica las transformaciones usando la configuración declarada,
    por lo que el adaptador no convierte montos, monedas, fechas ni estados.
    """

    nombre: ClassVar[str]
    separador_decimal: ClassVar[Literal[".", ","] | None] = None
    monto_en_unidades_menores: ClassVar[bool] = False
    zona_horaria: ClassVar[str] = "UTC"
    formatos_fecha: ClassVar[tuple[str, ...]] = FORMATOS_FECHA_POR_DEFECTO
    unidad_epoch: ClassVar[Literal["s", "ms"]] = "s"
    alias_moneda: ClassVar[Mapping[str, str]] = {}
    tabla_estados: ClassVar[Mapping[str, EstadoNormalizado]] = {}

    @abstractmethod
    def detectar(self, registro: Mapping[str, Any]) -> bool:
        """``True`` si la estructura del registro corresponde a esta fuente.
        Debe ser barato, no lanzar excepciones y no solaparse con otras fuentes."""

    @abstractmethod
    def extraer(self, registro: Mapping[str, Any]) -> CamposExtraidos:
        """Extrae los valores crudos; ausencias se devuelven como ``None``."""


ADAPTADORES_REGISTRADOS: list[AdaptadorFuente] = []
"""Registro global en orden de alta. :func:`procesar_registros` lo usa por defecto."""


def registrar_fuente[T: type[AdaptadorFuente]](clase: T) -> T:
    """Decorador que instancia y registra un adaptador.

    Raises:
        ValueError: si ya existe una fuente con el mismo ``nombre``.
    """
    nombre = getattr(clase, "nombre", None)
    if not nombre or nombre == FUENTE_NO_RECONOCIDA:
        raise ValueError(f"{clase.__name__} debe definir un 'nombre' válido")
    if any(a.nombre == nombre for a in ADAPTADORES_REGISTRADOS):
        raise ValueError(f"ya existe una fuente registrada con nombre {nombre!r}")
    ADAPTADORES_REGISTRADOS.append(clase())
    return clase


@registrar_fuente
class AdaptadorPasarelaPagos(AdaptadorFuente):
    """Pasarela de pagos: JSON plano, ``snake_case`` en inglés, ISO 8601.

    ``{"transaction_id", "amount", "currency", "status", "created_at"}``
    """

    nombre = "pasarela_pagos"
    separador_decimal = "."
    zona_horaria = "UTC"

    def detectar(self, registro: Mapping[str, Any]) -> bool:
        return "transaction_id" in registro and "created_at" in registro

    def extraer(self, registro: Mapping[str, Any]) -> CamposExtraidos:
        return CamposExtraidos(
            id_origen=registro.get("transaction_id"),
            fecha=registro.get("created_at"),
            monto=registro.get("amount"),
            moneda=registro.get("currency"),
            estado=registro.get("status"),
        )


@registrar_fuente
class AdaptadorBancoLocal(AdaptadorFuente):
    """Banco local: claves en español, importe anidado, coma decimal, fechas
    ``dd/mm/aaaa`` en hora local de El Salvador sin zona explícita.

    ``{"referencia", "fecha", "importe": {"valor", "divisa"}, "estado"}``
    """

    nombre = "banco_local"
    separador_decimal = ","
    zona_horaria = "America/El_Salvador"

    def detectar(self, registro: Mapping[str, Any]) -> bool:
        return "referencia" in registro and isinstance(registro.get("importe"), Mapping)

    def extraer(self, registro: Mapping[str, Any]) -> CamposExtraidos:
        return CamposExtraidos(
            id_origen=registro.get("referencia"),
            fecha=registro.get("fecha"),
            monto=obtener_ruta(registro, "importe.valor"),
            moneda=obtener_ruta(registro, "importe.divisa"),
            estado=registro.get("estado"),
        )


@registrar_fuente
class AdaptadorAppMovil(AdaptadorFuente):
    """App móvil: ``camelCase``, monto en centavos, fecha como epoch en ms y
    estados propios en mayúsculas.

    ``{"txnId", "amountCents", "currencyCode", "state", "timestampMs"}``
    """

    nombre = "app_movil"
    monto_en_unidades_menores = True
    unidad_epoch = "ms"
    tabla_estados = {"REVERSED": EstadoNormalizado.REEMBOLSADA}

    def detectar(self, registro: Mapping[str, Any]) -> bool:
        return "txnId" in registro and "amountCents" in registro

    def extraer(self, registro: Mapping[str, Any]) -> CamposExtraidos:
        return CamposExtraidos(
            id_origen=registro.get("txnId"),
            fecha=registro.get("timestampMs"),
            monto=registro.get("amountCents"),
            moneda=registro.get("currencyCode"),
            estado=registro.get("state"),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Núcleo de normalización
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class _Construccion:
    """Acumulador mutable mientras se normaliza un registro."""

    datos: dict[str, Any] = field(default_factory=dict)
    incidencias: list[Incidencia] = field(default_factory=list)

    def agregar(self, codigo: CodigoIncidencia, mensaje: str, campo: str | None = None) -> None:
        self.incidencias.append(Incidencia.crear(codigo, mensaje, campo))


def _detectar_adaptador(
    registro: Mapping[str, Any], adaptadores: Sequence[AdaptadorFuente]
) -> tuple[AdaptadorFuente | None, list[str]]:
    coincidencias: list[AdaptadorFuente] = []
    for adaptador in adaptadores:
        try:
            if adaptador.detectar(registro):
                coincidencias.append(adaptador)
        except Exception:  # un detector defectuoso no debe tumbar el lote
            logger.exception("detectar() falló en el adaptador %s", adaptador.nombre)
    nombres = [a.nombre for a in coincidencias]
    return (coincidencias[0] if len(coincidencias) == 1 else None), nombres


def normalizar_registro(
    registro: Any,
    indice: int,
    adaptadores: Sequence[AdaptadorFuente] | None = None,
) -> TransaccionNormalizada:
    """Normaliza un registro individual. Nunca lanza excepciones por datos
    defectuosos: los problemas quedan como incidencias en la transacción."""
    adaptadores = ADAPTADORES_REGISTRADOS if adaptadores is None else adaptadores
    c = _Construccion()

    def _rechazo(fuente: str, codigo: CodigoIncidencia, mensaje: str) -> TransaccionNormalizada:
        c.agregar(codigo, mensaje)
        return TransaccionNormalizada(
            id=f"{fuente}:#{indice}", fuente=fuente, indice_origen=indice,
            incidencias=tuple(c.incidencias), registro_original=registro,
        )

    if not isinstance(registro, Mapping):
        return _rechazo(
            FUENTE_NO_RECONOCIDA, CodigoIncidencia.REGISTRO_NO_ES_OBJETO,
            f"se esperaba un objeto JSON, se recibió {type(registro).__name__}",
        )

    adaptador, coincidencias = _detectar_adaptador(registro, adaptadores)
    if adaptador is None:
        if coincidencias:
            return _rechazo(
                FUENTE_NO_RECONOCIDA, CodigoIncidencia.FUENTE_AMBIGUA,
                f"la estructura coincide con varias fuentes: {', '.join(coincidencias)}",
            )
        claves = ", ".join(sorted(map(str, registro.keys()))) or "(sin claves)"
        return _rechazo(
            FUENTE_NO_RECONOCIDA, CodigoIncidencia.FUENTE_NO_RECONOCIDA,
            f"ninguna fuente reconoce la estructura; claves: {claves}",
        )

    try:
        crudo = adaptador.extraer(registro)
    except Exception as exc:
        logger.exception("extraer() falló en el adaptador %s (índice %d)", adaptador.nombre, indice)
        return _rechazo(
            adaptador.nombre, CodigoIncidencia.ERROR_ADAPTADOR,
            f"error interno al extraer campos: {type(exc).__name__}: {exc}",
        )

    _normalizar_campos(crudo, adaptador, c)
    id_origen = c.datos.get("id_origen")
    try:
        return TransaccionNormalizada(
            id=f"{adaptador.nombre}:{id_origen if id_origen else f'#{indice}'}",
            fuente=adaptador.nombre,
            indice_origen=indice,
            incidencias=tuple(c.incidencias),
            registro_original=registro,
            **c.datos,
        )
    except ValueError as exc:  # pydantic.ValidationError hereda de ValueError
        # Defensa en profundidad: si una regla del modelo detecta algo que el
        # núcleo no cubrió, se rechaza el registro en vez de abortar el lote.
        logger.error("Modelo rechazó el registro %d: %s", indice, exc)
        return _rechazo(adaptador.nombre, CodigoIncidencia.ERROR_ADAPTADOR, f"validación del modelo: {exc}")


def _normalizar_campos(crudo: CamposExtraidos, adaptador: AdaptadorFuente, c: _Construccion) -> None:
    """Aplica transformaciones y validaciones de campo, acumulando incidencias."""
    falta = CodigoIncidencia.CAMPO_OBLIGATORIO_FALTANTE

    # Identificador
    if crudo.id_origen is None or (isinstance(crudo.id_origen, str) and not crudo.id_origen.strip()):
        c.agregar(falta, "identificador ausente", "id_origen")
    else:
        c.datos["id_origen"] = str(crudo.id_origen).strip()

    # Moneda (antes que el monto: los decimales dependen de la moneda)
    if crudo.moneda is None:
        c.agregar(falta, "moneda ausente", "moneda")
    else:
        try:
            c.datos["moneda"] = normalizar_moneda(crudo.moneda, adaptador.alias_moneda)
        except MonedaNoReconocidaError as exc:
            c.agregar(CodigoIncidencia.MONEDA_NO_RECONOCIDA, str(exc), "moneda")

    # Monto
    if crudo.monto is None:
        c.agregar(falta, "monto ausente", "monto")
    elif adaptador.monto_en_unidades_menores and "moneda" not in c.datos:
        c.agregar(
            CodigoIncidencia.MONTO_INVALIDO,
            "no se puede convertir de unidades menores sin una moneda reconocida",
            "monto",
        )
    else:
        try:
            monto = parsear_monto(
                crudo.monto,
                separador_decimal=adaptador.separador_decimal,
                en_unidades_menores=adaptador.monto_en_unidades_menores,
                decimales=MONEDAS_ISO4217.get(c.datos.get("moneda", ""), 2),
            )
            c.datos["monto"] = monto
            if monto == 0:
                c.agregar(CodigoIncidencia.MONTO_CERO, "monto igual a cero", "monto")
            elif monto < 0:
                c.agregar(CodigoIncidencia.MONTO_NEGATIVO, f"monto negativo: {monto}", "monto")
        except MontoInvalidoError as exc:
            c.agregar(CodigoIncidencia.MONTO_INVALIDO, str(exc), "monto")

    # Fecha
    if crudo.fecha is None:
        c.agregar(falta, "fecha ausente", "fecha_utc")
    else:
        try:
            fecha, zona = parsear_fecha(
                crudo.fecha,
                zona_por_defecto=adaptador.zona_horaria,
                formatos=adaptador.formatos_fecha,
                unidad_epoch=adaptador.unidad_epoch,
            )
            c.datos["fecha_utc"] = fecha
            if zona is not None:
                c.datos["zona_horaria_asumida"] = zona
                c.agregar(
                    CodigoIncidencia.ZONA_HORARIA_ASUMIDA,
                    f"la fuente no indicó zona horaria; se asumió {zona}",
                    "fecha_utc",
                )
        except FechaInvalidaError as exc:
            c.agregar(CodigoIncidencia.FECHA_INVALIDA, str(exc), "fecha_utc")

    # Estado
    if crudo.estado is None:
        c.agregar(falta, "estado ausente", "estado")
    else:
        c.datos["estado_origen"] = str(crudo.estado)
        estado = mapear_estado(crudo.estado, adaptador.tabla_estados)
        if estado is None:
            c.agregar(
                CodigoIncidencia.ESTADO_DESCONOCIDO,
                f"estado de origen sin mapeo: {crudo.estado!r}",
                "estado",
            )
        else:
            c.datos["estado"] = estado


def procesar_registros(
    registros: Iterable[Any],
    adaptadores: Sequence[AdaptadorFuente] | None = None,
    *,
    origen: str | None = None,
    errores_carga: Sequence[str] = (),
) -> ResultadoProcesamiento:
    """Normaliza, valida, detecta duplicados y calcula métricas.

    Duplicados: la primera aparición de ``(fuente, id_origen)`` se conserva;
    las siguientes se rechazan con ``ID_DUPLICADO`` para no inflar totales.
    """
    validas: list[TransaccionNormalizada] = []
    rechazadas: list[TransaccionNormalizada] = []
    vistos: dict[str, int] = {}
    for indice, registro in enumerate(registros):
        t = normalizar_registro(registro, indice, adaptadores)
        if t.id_origen is not None:
            if t.id in vistos:
                t = t.con_incidencia(Incidencia.crear(
                    CodigoIncidencia.ID_DUPLICADO,
                    f"id repetido; primera aparición en el índice {vistos[t.id]}",
                    "id",
                ))
            else:
                vistos[t.id] = indice
        (validas if t.es_valida else rechazadas).append(t)
    resultado = ResultadoProcesamiento(
        origen=origen, errores_carga=list(errores_carga), validas=validas, rechazadas=rechazadas,
        metricas=calcular_metricas([*validas, *rechazadas]),
    )
    logger.info(
        "Procesadas %d transacciones (%d válidas, %d rechazadas)",
        resultado.metricas.total_procesadas, len(validas), len(rechazadas),
    )
    return resultado


# ═══════════════════════════════════════════════════════════════════════════
# Carga robusta del archivo
# ═══════════════════════════════════════════════════════════════════════════

CLAVES_CONTENEDOR = ("transacciones", "transactions", "data", "registros")
"""Claves aceptadas cuando la raíz del JSON es un objeto en lugar de una lista."""


def _decodificar(texto: str | bytes) -> list[Any]:
    """Parsea JSON con decimales exactos. Lanza :class:`ErrorCargaArchivo`."""
    if isinstance(texto, bytes):
        try:
            texto = texto.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ErrorCargaArchivo(f"el archivo no está en UTF-8: {exc}") from exc
    try:
        # parse_float=Decimal evita pasar por float binario. NaN/Infinity se
        # conservan como texto para que el registro se rechace individualmente.
        datos = json.loads(texto, parse_float=Decimal, parse_constant=lambda s: s)
    except json.JSONDecodeError as exc:
        raise ErrorCargaArchivo(
            f"JSON mal formado (línea {exc.lineno}, columna {exc.colno}): {exc.msg}"
        ) from exc
    if isinstance(datos, list):
        return datos
    if isinstance(datos, Mapping):
        for clave in CLAVES_CONTENEDOR:
            contenido = datos.get(clave)
            if isinstance(contenido, list):
                return contenido
        raise ErrorCargaArchivo(
            "la raíz es un objeto sin lista de transacciones; claves esperadas: "
            + ", ".join(CLAVES_CONTENEDOR)
        )
    raise ErrorCargaArchivo(f"la raíz del JSON debe ser lista u objeto, no {type(datos).__name__}")


def cargar_registros(ruta: str | Path) -> list[Any]:
    """Lee el archivo y devuelve la lista de registros crudos.

    Raises:
        ErrorCargaArchivo: archivo inexistente, ilegible, no UTF-8, JSON mal
            formado o raíz sin lista de transacciones.
    """
    ruta = Path(ruta)
    try:
        contenido = ruta.read_bytes()
    except OSError as exc:
        raise ErrorCargaArchivo(f"no se pudo leer {ruta}: {exc.strerror or exc}") from exc
    return _decodificar(contenido)


def procesar_texto(
    texto: str | bytes,
    adaptadores: Sequence[AdaptadorFuente] | None = None,
    *,
    origen: str | None = None,
) -> ResultadoProcesamiento:
    """Como :func:`procesar_archivo` pero desde contenido en memoria
    (p. ej. un archivo subido en la interfaz). Nunca lanza por datos malos."""
    try:
        registros = _decodificar(texto)
    except ErrorCargaArchivo as exc:
        logger.error("Error de carga en %s: %s", origen or "<memoria>", exc)
        return ResultadoProcesamiento(origen=origen, errores_carga=[str(exc)])
    return procesar_registros(registros, adaptadores, origen=origen)


def procesar_archivo(
    ruta: str | Path, adaptadores: Sequence[AdaptadorFuente] | None = None
) -> ResultadoProcesamiento:
    """Punto de entrada recomendado: carga, normaliza y mide.

    Los problemas del archivo completo se devuelven en ``errores_carga`` en
    lugar de lanzarse, para que la interfaz pueda mostrarlos.
    """
    try:
        registros = cargar_registros(ruta)
    except ErrorCargaArchivo as exc:
        logger.error("Error de carga: %s", exc)
        return ResultadoProcesamiento(origen=str(ruta), errores_carga=[str(exc)])
    return procesar_registros(registros, adaptadores, origen=str(ruta))


# ═══════════════════════════════════════════════════════════════════════════
# Métricas y filtros
# ═══════════════════════════════════════════════════════════════════════════


def calcular_metricas(transacciones: Iterable[TransaccionNormalizada]) -> Metricas:
    """Calcula métricas sobre cualquier subconjunto (p. ej. el filtrado).

    * Conteos por estado, fuente y motivo: sobre todas las transacciones.
    * Totales por moneda: solo válidas, un total independiente por moneda.
    """
    lista = list(transacciones)
    totales: dict[str, Decimal] = defaultdict(Decimal)
    por_estado: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    motivos: Counter[str] = Counter()
    for t in lista:
        motivos.update({i.codigo.value for i in t.incidencias})
        if t.es_valida and t.moneda is not None and t.monto is not None:
            totales[t.moneda] += t.monto
            por_estado[t.moneda][t.estado.value] += t.monto
    validas = sum(t.es_valida for t in lista)
    return Metricas(
        total_procesadas=len(lista),
        total_validas=validas,
        total_invalidas=len(lista) - validas,
        total_con_advertencias=sum(
            any(i.severidad is Severidad.ADVERTENCIA for i in t.incidencias) for t in lista
        ),
        conteo_por_estado=dict(sorted(Counter(t.estado.value for t in lista).items())),
        conteo_por_fuente=dict(sorted(Counter(t.fuente for t in lista).items())),
        conteo_por_motivo=dict(sorted(motivos.items())),
        totales_por_moneda=dict(sorted(totales.items())),
        totales_por_moneda_y_estado={
            m: dict(sorted(e.items())) for m, e in sorted(por_estado.items())
        },
    )


class FiltroTransacciones(BaseModel):
    """Criterios de filtrado; ``None`` o vacío significa "sin restricción".

    ``desde``/``hasta`` son fechas UTC inclusivas (día completo). Si hay rango
    de fechas, las transacciones sin fecha quedan fuera.
    """

    fuentes: frozenset[str] | None = None
    estados: frozenset[EstadoNormalizado] | None = None
    monedas: frozenset[str] | None = None
    desde: date | None = None
    hasta: date | None = None
    validez: Literal["todas", "validas", "invalidas"] = "todas"

    @model_validator(mode="after")
    def _rango_coherente(self) -> FiltroTransacciones:
        if self.desde and self.hasta and self.desde > self.hasta:
            raise ValueError("'desde' no puede ser posterior a 'hasta'")
        return self

    def acepta(self, t: TransaccionNormalizada) -> bool:
        """``True`` si la transacción cumple todos los criterios."""
        if self.validez == "validas" and not t.es_valida:
            return False
        if self.validez == "invalidas" and t.es_valida:
            return False
        if self.fuentes and t.fuente not in self.fuentes:
            return False
        if self.estados and t.estado not in self.estados:
            return False
        if self.monedas and t.moneda not in self.monedas:
            return False
        if self.desde or self.hasta:
            if t.fecha_utc is None:
                return False
            if self.desde and t.fecha_utc < datetime.combine(self.desde, time(), UTC):
                return False
            if self.hasta and t.fecha_utc >= datetime.combine(self.hasta + timedelta(days=1), time(), UTC):
                return False
        return True


def filtrar(
    transacciones: Iterable[TransaccionNormalizada], filtro: FiltroTransacciones
) -> list[TransaccionNormalizada]:
    """Devuelve las transacciones que cumplen ``filtro``, en el orden dado."""
    return [t for t in transacciones if filtro.acepta(t)]


# ═══════════════════════════════════════════════════════════════════════════
# CLI por lotes
# ═══════════════════════════════════════════════════════════════════════════


def _resumen_texto(r: ResultadoProcesamiento) -> str:
    m = r.metricas
    lineas = [f"Origen: {r.origen}"]
    lineas += [f"ERROR DE CARGA: {e}" for e in r.errores_carga]
    lineas += [
        f"Procesadas: {m.total_procesadas}  Válidas: {m.total_validas}  "
        f"Inválidas: {m.total_invalidas}  Con advertencias: {m.total_con_advertencias}",
        "Por estado: " + (", ".join(f"{k}={v}" for k, v in m.conteo_por_estado.items()) or "-"),
        "Totales por moneda (válidas): "
        + (", ".join(f"{k} {v}" for k, v in m.totales_por_moneda.items()) or "-"),
    ]
    for t in r.rechazadas:
        errores = "; ".join(i.mensaje for i in t.incidencias if i.severidad is Severidad.ERROR)
        lineas.append(f"  rechazada {t.id} (índice {t.indice_origen}): {errores}")
    return "\n".join(lineas)


def main(argv: Sequence[str] | None = None, salida_texto: Callable[[str], None] = print) -> int:
    """CLI por lotes. Códigos de salida: 0 procesado, 1 error de carga,
    2 hay rechazadas y se pidió ``--estricto``."""
    parser = argparse.ArgumentParser(description="Normaliza transacciones desde un JSON.")
    parser.add_argument("entrada", type=Path, help="archivo JSON de entrada")
    parser.add_argument("--salida", type=Path, help="escribe el resultado completo en JSON")
    parser.add_argument("--estricto", action="store_true", help="código 2 si hay rechazadas")
    parser.add_argument("-v", "--verbose", action="store_true", help="registro detallado")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    resultado = procesar_archivo(args.entrada)
    if args.salida:
        try:
            args.salida.write_text(resultado.model_dump_json(indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            salida_texto(f"No se pudo escribir {args.salida}: {exc}")
            return 1
    salida_texto(_resumen_texto(resultado))
    if resultado.errores_carga:
        return 1
    if args.estricto and resultado.rechazadas:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
