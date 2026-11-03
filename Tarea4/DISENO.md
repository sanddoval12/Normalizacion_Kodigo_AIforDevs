# Diseño del sistema de normalización de transacciones

Este documento cubre el modelo de datos, las reglas de transformación y validación, la decisión de interfaz y el ejemplo de entrada y salida. El uso diario (ejecutar, probar, agregar fuentes) está en `.claude/skills/normalizacion-transacciones/SKILL.md`.

## 1. Dependencias y por qué

| Dependencia | Dónde | Justificación frente a la biblioteca estándar |
|---|---|---|
| **Pydantic 2** | `normalizacion.py` | Expresa el esquema con tipos y restricciones declarativas, hace cumplir invariantes con validadores (UTC obligatorio, código ISO, prohibición de `float`, coherencia de validez), serializa `Decimal` como texto exacto y genera JSON Schema. Con `dataclasses` habría que escribir a mano la validación y la serialización. |
| **pandas** | no se usa en el núcleo | Con montos `Decimal` las columnas quedan con tipo `object` y se pierde la vectorización; a esta escala no aporta claridad ni velocidad. Solo llega como dependencia transitiva de Streamlit. |
| **Streamlit** | `app.py` | Interfaz interactiva (ver sección 5). |
| **tzdata** | solo Windows | Windows no trae la base de zonas IANA que usa `zoneinfo`. |

Para las fechas se usa la biblioteca estándar (`datetime.fromisoformat`, `email.utils`, `strptime`, `zoneinfo`) y para el JSON, `json` con `parse_float=Decimal`. De esa forma los montos nunca pasan por `float` binario.

## 2. Modelo de datos normalizado

`TransaccionNormalizada` es un modelo Pydantic inmutable (`frozen=True`). El JSON Schema completo se obtiene con:

```bash
python -c "import json, normalizacion as n; print(json.dumps(n.TransaccionNormalizada.model_json_schema(), indent=2, ensure_ascii=False))"
```

| Campo | Tipo y restricción | Por qué existe |
|---|---|---|
| `id` | `str`, no vacío; `"<fuente>:<id_origen>"` o `"<fuente>:#<índice>"` si falta el id | Dos fuentes pueden repetir el mismo id; el prefijo da unicidad global y estable. |
| `id_origen` | `str \| None` | Permite conciliar con el sistema de origen. |
| `fuente` | `str`; `"no_reconocida"` si ningún adaptador coincide | Trazabilidad y filtro por fuente. |
| `indice_origen` | `int ≥ 0` | Ubica el registro en el archivo aunque no tenga id. |
| `fecha_utc` | `datetime` con zona, convertido a UTC; se serializa en ISO 8601 con `Z` | Una sola referencia temporal hace comparables las fuentes. |
| `zona_horaria_asumida` | `str \| None` (nombre IANA) | Deja constancia de que la hora local se interpretó con una zona supuesta. |
| `monto` | `Decimal` finito, nunca `float`; se serializa como texto | Exactitud contable: `0.1 + 0.2` debe dar `0.3`. |
| `moneda` | `str` con el patrón `^[A-Z]{3}$` y presente en `MONEDAS_ISO4217` | Código ISO 4217; el catálogo también guarda los decimales de cada moneda. |
| `estado` | `EstadoNormalizado`: `completada`, `pendiente`, `fallida`, `cancelada`, `reembolsada` o `desconocido` | Vocabulario cerrado para contar y filtrar. |
| `estado_origen` | `str \| None` | Evita perder el valor original cuando el mapeo falla. |
| `incidencias` | `tuple[Incidencia, ...]`, cada una con `codigo`, `severidad`, `campo` y `mensaje` | Motivos de rechazo y advertencias, legibles por máquinas y personas. |
| `es_valida` | `bool` calculado: no hay ninguna incidencia de severidad `error` | No puede contradecir a `incidencias` porque se deriva de ellas. |
| `registro_original` | `Any` | Trazabilidad completa del dato de entrada. |

**Invariante del modelo:** una transacción válida siempre tiene `fecha_utc`, `monto`, `moneda` y un `estado` distinto de `desconocido`. Si el núcleo omitiera alguna regla, el modelo rechazaría el registro en lugar de producir un dato válido incompleto.

## 3. Reglas de transformación

Cada adaptador declara las convenciones de su fuente: separador decimal, si el monto viene en unidades menores, zona horaria, formatos de fecha, unidad del epoch, alias de moneda y estados propios. El núcleo aplica las reglas con esa configuración, así que el adaptador solo dice dónde está cada dato.

**Montos (`parsear_monto`)**
- Acepta `int`, `Decimal`, `float` (convertido con `repr` para no arrastrar error binario) y cadenas de texto.
- En las cadenas admite signo `-` o `+`, paréntesis contables `(1,000.00)`, un símbolo de moneda al inicio o al final, y como separador de miles `,`, `.`, espacio, espacio duro o apóstrofo. Los grupos de miles deben tener tres dígitos.
- El separador decimal lo declara la fuente. Si no lo declara, se infiere: con ambos separadores, el último es el decimal; si un mismo separador se repite, es de miles. **El caso ambiguo se rechaza**: en `"1,234"` la coma puede ser de miles o decimal.
- Unidades menores: el valor debe ser entero y se divide entre 10^d, donde d son los decimales de la moneda según ISO 4217 (USD 2, JPY 0, KWD 3). Por eso la moneda se resuelve antes que el monto.

**Moneda (`normalizar_moneda`)**
- El orden de búsqueda es: alias del adaptador, luego el código ISO directo, luego `ALIAS_MONEDA`. La comparación ignora mayúsculas, acentos y espacios.
- Alias aceptados (resumen): `$`, `US$`, `dólar(es)` → USD · `€`, `euro(s)` → EUR · `£` → GBP · `R$`, `real(es)` → BRL · `quetzal(es)` → GTQ · `lempira(s)` → HNL · `córdoba(s)` → NIO · `balboa(s)` → PAB · `sol(es)`, `S/` → PEN · `peso mexicano` → MXN, y el resto de la lista en el código.
- `$` → USD es una **convención del proyecto**, porque opera en una economía dolarizada. Una fuente que use `$` para otra moneda lo sobrescribe en su `alias_moneda`.
- `¥`, `C$` y `₡` se excluyen a propósito porque cada uno corresponde a más de una moneda.

**Estados (`mapear_estado`)**
- Hay una tabla global explícita (`TABLA_ESTADOS`) en español e inglés, por ejemplo `completed`, `approved` y `aprobada` → `completada`, o `declined` y `rechazada` → `fallida`. Cada adaptador puede añadir entradas, como `REVERSED` → `reembolsada` en la app móvil.
- Un estado sin entrada en las tablas queda como `desconocido` con la incidencia `estado_desconocido`. No se intenta adivinar por similitud.

**Fechas (`parsear_fecha`)**
- Los formatos se prueban en este orden: ISO 8601 (incluido el sufijo `Z`), RFC 2822, los formatos `strptime` de la fuente y por último un epoch Unix (en segundos o milisegundos, según declare la fuente).
- La lista por defecto usa día antes que mes, la convención latinoamericana. Una fuente con fechas mes/día declara sus propios `formatos_fecha`.
- Si la fecha no trae zona, se usa la del adaptador, se registra en `zona_horaria_asumida` y se añade una advertencia. Un epoch es UTC por definición y no genera advertencia.

## 4. Validación y tratamiento de inválidos

Ningún registro se **descarta**. Descartar rompe la trazabilidad y hace imposible auditar por qué no cuadran los totales. Solo hay dos acciones posibles:

- **Separar**: el registro va a `rechazadas`, queda fuera de los totales y conserva sus motivos.
- **Marcar**: el registro sigue siendo válido pero lleva una advertencia.

| Código | Acción | Justificación |
|---|---|---|
| `registro_no_es_objeto` | separar | No hay campos que mapear; se conserva el valor crudo. |
| `fuente_no_reconocida` | separar | Mapear sin conocer la estructura sería adivinar. Se listan las claves recibidas para crear el adaptador que falta. |
| `fuente_ambigua` | separar | Si dos adaptadores reconocen la misma estructura, elegir uno sería arbitrario. |
| `error_adaptador` | separar (y se registra en el log) | Un fallo en el código de un adaptador no debe detener el lote. |
| `campo_obligatorio_faltante` | separar | Sin id, monto, moneda, fecha o estado no se puede contabilizar ni conciliar. |
| `monto_invalido` | separar | Un monto no numérico, ambiguo o no finito haría falsos los totales. |
| `moneda_no_reconocida` | separar | No se puede sumar sin saber en qué moneda está el monto. |
| `fecha_invalida` | separar | Sin fecha no se puede ubicar la transacción en un período. |
| `estado_desconocido` | separar | Contarla como completada o fallida cambiaría el resultado. |
| `id_duplicado` | separar (se conserva la primera aparición) | Evita contar dos veces el mismo dinero. |
| `zona_horaria_asumida` | marcar | El dato es usable, pero la hora depende de la zona supuesta. |
| `monto_cero` | marcar | Es legítimo (por ejemplo, un pago fallido), pero conviene revisarlo. |
| `monto_negativo` | marcar | Es legítimo en un reembolso o contracargo, pero conviene revisarlo. |

Un registro acumula **todos** sus motivos, no solo el primero. Así puede corregirse en una sola pasada.

**Errores del archivo completo** (no existe, no está en UTF-8, JSON mal formado, la raíz no contiene una lista de transacciones) se devuelven en `errores_carga` y el proceso no se detiene. La raíz puede ser una lista o un objeto con la lista en `transacciones`, `transactions`, `data` o `registros`. Los literales `NaN` e `Infinity` se rechazan registro por registro, no invalidan el archivo entero.

**Métricas.** Los conteos por estado, fuente y motivo se calculan sobre **todas** las transacciones procesadas, de modo que los rechazos también se ven. En cambio, `totales_por_moneda` suma solo las válidas y lleva un total independiente por moneda. Como ese total incluye pagos fallidos o cancelados, `totales_por_moneda_y_estado` lo desglosa por estado para saber cuánto dinero se movió realmente.

## 5. Decisión de interfaz: Streamlit

**Se elige Streamlit porque es la opción más fácil de implementar y mantener para este alcance.**

- Los filtros pedidos (multiselección por fuente, estado y moneda, rango de fechas con calendario, validez) son componentes ya hechos. En una CLI con `input()` habría que escribir y validar cada menú a mano, y con Textual habría que mantener eventos, foco y diseño de terminal.
- La tabla tiene selección de fila, la vista de detalle muestra el registro original en formato JSON y las métricas incluyen tarjetas y gráfico. Todo vive en unas 200 líneas de código.
- La interfaz no contiene lógica de datos: solo llama a `procesar_texto`, `filtrar` y `calcular_metricas`. Las métricas se recalculan sobre el subconjunto filtrado.
- Corre en local con `streamlit run app.py`, sin servidor ni despliegue. Se prueba sin navegador con `streamlit.testing`, y eso está incluido en las pruebas.

**Evaluación de Stitch.** Stitch genera maquetas y las exporta como HTML con Tailwind o como archivo de Figma. Es un generador de prototipos, no un framework de frontend, y su código exportado no incluye enlace con datos ni backend ([edana.ch](https://edana.ch/en/2026/03/29/google-stitch-ai-driven-ui-design-revolution-or-just-a-prototype-accelerator-full-analysis-and-real-world-applications/)).

Conectar ese HTML estático al módulo **no exigiría reescribir la lógica de datos**, porque el módulo ya devuelve JSON serializable con `ResultadoProcesamiento.model_dump_json()`. Pero sí exigiría añadir un servidor HTTP (por ejemplo, FastAPI) y JavaScript para los filtros y el detalle. Eso es una capa nueva de backend y frontend que queda fuera del alcance pedido.

Por eso la interfaz se construye directamente en Python. El diseño de Stitch se aprovecha como referencia visual: sus colores y tipografía se copian en `.streamlit/config.toml`. Si más adelante se quiere usar el HTML de Stitch tal cual, el punto de integración es `model_dump_json()` y no hay que tocar `normalizacion.py`.

## 6. Ejemplo de entrada y salida

- Entrada: `ejemplos/entrada_ejemplo.json`, con 12 registros: 4 de la pasarela de pagos, 3 del banco local, 3 de la app móvil, uno con estructura desconocida y una cadena suelta.
- Salida completa: `ejemplos/salida_esperada.json`, generada con `python normalizacion.py ejemplos/entrada_ejemplo.json --salida ejemplos/salida_esperada.json`. La prueba `TestEjemploDeReferencia` verifica que la salida documentada coincide con la real.

Las tres fuentes del ejemplo:

```jsonc
// pasarela_pagos: plano, snake_case en inglés, ISO 8601, punto decimal
{"transaction_id": "TX-1001", "amount": "1,250.50", "currency": "USD",
 "status": "completed", "created_at": "2026-10-01T14:30:00Z"}
// banco_local: español, importe anidado, coma decimal, hora local sin zona
{"referencia": "BL-0077", "fecha": "01/10/2026 08:15",
 "importe": {"valor": "2.500,75", "divisa": "dólares"}, "estado": "Aprobada"}
// app_movil: camelCase, centavos, epoch en milisegundos, estados propios
{"txnId": "m-501", "amountCents": 125050, "currencyCode": "usd",
 "state": "SUCCESS", "timestampMs": 1790955900000}
```

Resumen de la salida:

```
Procesadas: 12  Válidas: 7  Inválidas: 5  Con advertencias: 3
Por estado: completada=5, desconocido=3, fallida=1, pendiente=1, reembolsada=2
Totales por moneda (válidas): EUR 89.9, MXN 49.99, USD 4851.75
  rechazada pasarela_pagos:TX-1003 (índice 2): monto no numérico: 'abc'
  rechazada pasarela_pagos:TX-1001 (índice 3): id repetido; primera aparición en el índice 0
  rechazada banco_local:BL-0079 (índice 6): moneda no reconocida: 'pesos de oro'; fecha con formato no reconocido: '31/02/2026 10:00'; estado de origen sin mapeo: 'en revisión'
  rechazada no_reconocida:#10 (índice 10): ninguna fuente reconoce la estructura; claves: id, tipo, valor
  rechazada no_reconocida:#11 (índice 11): se esperaba un objeto JSON, se recibió str
```

USD 4851.75 sale de 1250.50 + 2500.75 − 150.00 + 1250.50 + 0.00. El duplicado de TX-1001 y TX-1003 quedan fuera. En el desglose por estado, lo completado en USD suma 5001.75.

Una transacción válida con advertencia (las 08:15 en El Salvador, UTC−6, son las 14:15 UTC):

```json
{
  "id": "banco_local:BL-0077", "id_origen": "BL-0077", "fuente": "banco_local", "indice_origen": 4,
  "fecha_utc": "2026-10-01T14:15:00Z", "zona_horaria_asumida": "America/El_Salvador",
  "monto": "2500.75", "moneda": "USD", "estado": "completada", "estado_origen": "Aprobada",
  "incidencias": [{"codigo": "zona_horaria_asumida", "severidad": "advertencia", "campo": "fecha_utc",
                   "mensaje": "la fuente no indicó zona horaria; se asumió America/El_Salvador"}],
  "es_valida": true
}
```

Una transacción rechazada que acumula tres motivos:

```json
{
  "id": "banco_local:BL-0079", "fuente": "banco_local", "indice_origen": 6,
  "fecha_utc": null, "monto": "300.00", "moneda": null,
  "estado": "desconocido", "estado_origen": "en revisión",
  "incidencias": [
    {"codigo": "moneda_no_reconocida", "severidad": "error", "campo": "moneda", "mensaje": "moneda no reconocida: 'pesos de oro'"},
    {"codigo": "fecha_invalida", "severidad": "error", "campo": "fecha_utc", "mensaje": "fecha con formato no reconocido: '31/02/2026 10:00'"},
    {"codigo": "estado_desconocido", "severidad": "error", "campo": "estado", "mensaje": "estado de origen sin mapeo: 'en revisión'"}
  ],
  "es_valida": false
}
```

## 7. Límites conocidos

- El archivo se carga completo en memoria. Es adecuado para miles o decenas de miles de registros; para volúmenes mayores habría que leerlo en streaming.
- La agrupación de miles al estilo indio (`1,23,456`) se rechaza como formato inválido.
- No hay conversión entre monedas: los totales se informan por moneda, como se pidió.
- No se usa base de datos, autenticación ni despliegue, porque quedaron fuera del alcance.
