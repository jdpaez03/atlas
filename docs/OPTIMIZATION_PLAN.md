# ATLAS — Plan de optimización de corridas (2026-10)

## Línea base medida (20 misiones, 30-sep a 5-oct-2026)

- **Costo:** US$ 124 en total.
  - 8 misiones de trabajo: US$ 123, entre US$ 3 y 67 cada una.
  - 12 rutinas (inbox scan, ARGOS watch, L10 brief): US$ 1.6 juntas, entre US$ 0.03 y 0.54 cada una.
- **Tiempo:** 104 h de reloj, pero solo unas 7–8 h activas.
  - 72 h son CLOSED entre rondas.
  - 25 h son de espera de aprobación (una sola de 25 h retuvo una tarea de ALFRED).
- **Por agente:**

| Agente | Costo | % | Nota |
|---|---|---|---|
| ATLAS (orquestador) | US$ 39.6 | 32 % | Solo 55 % de caché. En REDI: 52 llamadas × ~53k tokens de entrada sin caché = US$ 26.6. |
| ALFRED | US$ 23.7 | 19 % | |
| ARGOS | US$ 19.0 | 15 % | |
| SCRIBE | US$ 13.4 | 11 % | 45 % de caché. |
| MERCATO | US$ 12.9 | 10 % | 97 % de caché, pero 8 de 16 reportes auto-wrapped (se quedó sin turnos). |
| AUDITOR | US$ 5.0 | 4 % | |

- **Auditoría:** 88 veredictos: 33 PASS, 31 ISSUES y **24 FAIL (27 %)**. Cada FAIL rehace la tarea, por eso VALIDATION tarda 18–22 min en las misiones de junta.
- **Misiones de "junta → to-dos":** US$ 8–12 y 40–48 min cada una, con 99–165 llamadas.
  - Reparten la tarea entre 4–5 agentes (ARGOS, ALFRED, EOS-traction, EOS-issues…).
  - Es una tarea de un solo agente, que debería costar alrededor de US$ 1 y tardar unos 5 min.

### Prioridades según los datos

1. **Orquestador (32 %).**
   - Cachear system y contexto en las llamadas de ATLAS.
   - No mandar contexto del nodo a revisión ni a consolidación.
   - Revisión con sonnet.
   - Meta: −US$ 25 por cada US$ 124.
2. **Planner que no reparta de más.**
   - Ruta rápida de un solo agente para misiones simples.
   - Plantilla fija "junta → to-dos" (ALFRED con suite_read y suite_add_todos).
   - Meta: de US$ 10 a ~US$ 1 por junta.
3. **FAIL del 27 %.** `ATLAS_AUDIT_REVISIONS=0` en misiones internas, y revisión incremental en vez de rehacer la tarea.
4. **SCRIBE (11 %).** Apagado por defecto; solo cuando se pide documento.
5. **MERCATO auto-wrap 50 %.** Recortar los resultados viejos del navegador y usar `browser_data` primero (playbook REDI).
6. **Aprobaciones.** Liberar el slot mientras se espera, y avisar con push cuando una aprobación lleve más de 1 h pendiente.

Las rutinas (inbox, ARGOS watch, L10 brief) ya son baratas. No se tocan.

## Cómo corre hoy una misión

1. **Memoria**: recupera lecciones y misiones previas.
2. **Planeación** (ATLAS, opus, 1–2 llamadas): arma el DAG de tareas.
3. **Ejecución**: tareas en paralelo (máx. `ATLAS_MAX_CONCURRENCY=4`). Cada agente hace un loop de herramientas hasta `max_turns` (8 API / 12 suscripción; 40–80 con navegador). Puede consultar a otros agentes (hasta 2 consultas).
4. **Revisión** (opus): lee todos los reportes y puede abrir hasta 3 tareas de seguimiento.
5. **AUDITOR**: vuelve a leer todos los reportes. Un FAIL rehace la tarea completa.
6. **Consolidación** (opus): escribe el reporte final.
7. **SCRIBE** (siempre activo): vuelve a mandar todos los reportes y genera docx, pdf o pptx (hasta 16k tokens de salida).

## Dónde se va el tiempo y el dinero (de mayor a menor)

| # | Problema | Efecto |
|---|---|---|
| 1 | El contexto del nodo (hasta 150k caracteres, ~40k tokens) va en **cada** tarea, consulta (aun con haiku), paso del orquestador y acuse. | Es la mayor parte de los tokens de entrada. |
| 2 | En el backend API el historial de mensajes **no usa prompt caching**: en cada turno se vuelven a cobrar todos los resultados de herramientas anteriores. | Una corrida de navegador de 80 turnos crece de forma ~cuadrática (MERCATO). |
| 3 | Revisión y AUDITOR son secuenciales y los dos leen todos los reportes. | Dos pasadas grandes en serie al final. |
| 4 | SCRIBE corre siempre, aunque la misión sea interna. | Una llamada grande más, de varios minutos. |
| 5 | Una revisión del AUDITOR o un reintento transitorio rehace la tarea completa. | Se duplica el costo de la tarea. |
| 6 | El bloque de memoria va duplicado: en el system prompt y en cada mensaje de tarea. | Tokens repetidos. |
| 7 | Un slot global sigue ocupado mientras se espera una aprobación o durante el sleep de un reintento. | Otras tareas esperan sin razón. |
| 8 | El backend por suscripción lanza un CLI por cada paso, tarea y consulta, más un turno extra de cierre. | Latencia de arranque en cada llamada. |
| 9 | El esquema de `write_deliverable` (~9k caracteres) se manda a todos los agentes en cada turno. | Tokens fijos por turno. |
| 10 | `ATLAS_AUDIT_REVISIONS` solo revisa si es >0; no respeta el número que se le pone. | Bug menor. |

## Gameplan

### Fase 0: medir (hoy)

En el servidor:

```bash
bash ~/atlas/deploy/linux/update.sh
python3 ~/atlas/deploy/linux/run_stats.py --last 20
```

El script solo lee la base. Muestra tiempo por fase, tareas por agente (reintentos, revisiones, fallas, reportes auto-wrapped), espera de aprobaciones, veredictos de auditoría y costo por agente. Es la línea base para comparar.

### Fase 1: solo configuración (sin código, reversible)

| Variable | Hoy | Propuesto | Por qué |
|---|---|---|---|
| `ATLAS_PUBLISH` | on | off por defecto. Activarlo (o el switch de la misión) solo cuando se necesita un documento | SCRIBE es el paso más largo al final. |
| `ATLAS_CONTEXT_MAX_CHARS` | 150000 | 40000, y limpiar `atlas-local/context/<nodo>/` | Ataca directo el problema #1. |
| `ATLAS_AUDIT_REVISIONS` | 1 | 0 en misiones internas: AUDITOR marca, no rehace | Evita tareas duplicadas. |
| `ATLAS_MAX_CONSULTS` | 2 | 1 | Cada consulta carga todo el contexto. |
| `max_turns` de agentes sin navegador | 8/12 | Dejar igual | Sí terminan. |

### Fase 2: código, alto impacto y bajo riesgo (orden sugerido)

1. **Prompt caching del historial (API)**: `cache_control` en el último bloque de cada turno. Las corridas largas de navegador cuestan mucho menos.
2. **Recortar resultados viejos de herramientas**: los últimos N completos y los anteriores reducidos a un resumen de una línea, sobre todo snapshots del navegador.
3. **Contexto del nodo por agente**: frontmatter `agents: [sofia, oracle]` en cada archivo de contexto. Las consultas y los acuses no reciben contexto.
4. **Herramientas por agente**: `write_deliverable` solo para quien entrega archivos (ALFRED, MERCATO, SCRIBE).
5. **Memoria una sola vez**: quitarla del mensaje de tarea.
6. **Liberar el slot** durante esperas de aprobación y sleeps de reintento.
7. Corregir el bug de `ATLAS_AUDIT_REVISIONS`.

### Fase 3: estructura

1. **Auditar por tarea al terminarla**: corre en paralelo con las demás y el haiku o sonnet barato basta. La revisión final se queda solo con lo cross-tarea. Opcional: fusionar revisión y auditoría en una sola pasada.
2. **Revisión incremental**: en lugar de rehacer la tarea, el agente recibe solo los issues y corrige sobre su reporte.
3. **Revisión con sonnet** en vez de opus. Opus queda para planeación y consolidación.
4. **Backend por suscripción**: una sesión por tarea, sin turno extra de cierre.

## Funciones que sobran (candidatas a apagar o volver opcionales)

- **SCRIBE siempre activo** → opcional por misión.
- **Seguimientos de revisión** hasta 3 → 1. Casi siempre se pide 0 o 1.
- **Doble lectura de reportes** (revisión y AUDITOR) → una sola, o auditoría por tarea.
- **Acuses del orquestador con contexto completo** → sin contexto.
- **Consultas con contexto completo** → solo con la pregunta y el reporte relevante.

## Metas medibles (con `run_stats.py`, antes y después)

- Costo promedio por misión: −40 a −60 %.
- Tiempo promedio por misión: −30 %. Lo que más se mueve: SCRIBE, auditoría en serie y revisiones.
- Reportes auto-wrapped: 0 en misiones sin navegador.
- Cache hit en agentes de navegador: más de 60 %.
