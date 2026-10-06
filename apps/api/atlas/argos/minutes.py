"""Meeting minutes next to each Teams transcript (docs/ARGOS.md · transcripts).

A one-hour junta is ~150k characters of transcript; every mission that needs it used to read all of it (5+ tool
turns, ~40k tokens each time). When ARGOS saves a transcript it now also writes `<transcript>.minuta.md` once:
summary, agreements, to-dos (who / what / when / project / tower), issues and decisions, each with the [mm:ss]
of the conversation and the character offset in the transcript, so an agent reads ~5k characters and opens the
transcript only around the offsets it needs (read_file with offset).

Long transcripts are read in chunks of CHUNK_CHARS cut at speaker turns (map), and the chunks' items are
concatenated in order (reduce). Items whose [mm:ss] is not in the transcript are dropped: the minuta only points
at things that were said.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .checks import CheckContext

log = logging.getLogger("atlas.argos")

SUFFIX = ".minuta.md"
CHUNK_CHARS = 90_000
MAX_PER_RUN = 3  # backfill at most this many transcripts per ARGOS run

_STAMP = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]", re.MULTILINE)

MINUTA_NOTE = """# Meeting minutes (ARGOS)
You turn one part of a meeting transcript into structured minutes for the team's agents. Each paragraph of the
transcript starts with [mm:ss] (or [h:mm:ss]) and the speaker's name.
- Capture EVERY commitment someone takes ("yo lo mando", "te lo paso el viernes", "queda Melanie de…"): who,
  what, by when (a date only if one was said, as YYYY-MM-DD using the meeting date for relative days), project and
  tower when they are mentioned.
- Issues are problems raised that are not solved in the meeting; decisions are what was settled.
- Every item carries `ts`: the [mm:ss] of the paragraph where it was said, copied exactly. Never invent names,
  dates or items; if unsure who owns something, leave responsable empty.
- Write in the transcript's language, short and concrete."""

_ITEM = {
    "type": "object",
    "properties": {"texto": {"type": "string"}, "ts": {"type": "string", "description": "[mm:ss] it was said"}},
    "required": ["texto", "ts"],
}
WRITE_MINUTA_TOOL: dict[str, Any] = {
    "name": "write_minuta",
    "description": "Structured minutes of this part of the meeting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "resumen": {"type": "array", "items": {"type": "string"}, "description": "3-8 lines"},
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "titulo": {"type": "string"},
                        "responsable": {"type": "string"},
                        "fecha_compromiso": {"type": "string", "description": "YYYY-MM-DD, only if said"},
                        "proyecto": {"type": "string"},
                        "torre": {"type": "string"},
                        "ts": {"type": "string"},
                        "cita": {"type": "string", "description": "the words that support it (short quote)"},
                    },
                    "required": ["titulo", "ts"],
                },
            },
            "acuerdos": {"type": "array", "items": _ITEM},
            "issues": {"type": "array", "items": _ITEM},
            "decisiones": {"type": "array", "items": _ITEM},
        },
        "required": ["resumen", "todos"],
    },
}
LISTS = ("todos", "acuerdos", "issues", "decisiones")


def minuta_path(transcript: Path) -> Path:
    return transcript.with_name(transcript.stem + SUFFIX)


def offsets(text: str) -> dict[str, int]:
    """[mm:ss] → character offset of its first paragraph."""
    out: dict[str, int] = {}
    for m in _STAMP.finditer(text):
        out.setdefault(m.group(1), m.start())
    return out


def _clean_ts(v: Any) -> str:
    return str(v or "").strip().strip("[]").strip()


def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split at paragraph (speaker turn) boundaries into pieces of at most ~size characters."""
    if len(text) <= size:
        return [text]
    out, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def merge(parts: list[dict[str, Any]], text: str) -> dict[str, Any]:
    """Concatenate the chunks' items in order, keep only those whose ts is in the transcript, add offsets."""
    where = offsets(text)
    merged: dict[str, Any] = {"resumen": [], **{k: [] for k in LISTS}}
    for p in parts:
        merged["resumen"] += [str(x).strip() for x in p.get("resumen") or [] if str(x).strip()]
        for k in LISTS:
            for item in p.get(k) or []:
                if not isinstance(item, dict):
                    continue
                ts = _clean_ts(item.get("ts"))
                if ts not in where:
                    continue
                merged[k].append({**item, "ts": ts, "offset": where[ts]})
    return merged


def render(subject: str, transcript: Path, m: dict[str, Any], chars: int) -> str:
    def at(item: dict[str, Any]) -> str:
        return f"[{item['ts']}] (carácter {item['offset']:,})"

    how = (f"Transcripción completa: `{transcript.name}` ({chars:,} caracteres). Cada punto trae el minuto y el "
           "carácter donde se dijo: para ver el contexto abre la transcripción con read_file desde ese carácter "
           "(offset) en lugar de leerla toda.")
    caveat = "Escrita por ARGOS a partir de la transcripción; verifica contra el original antes de registrar algo."
    out = [f"# Minuta · {subject}", "", how, "", caveat, "", "## Resumen", ""]
    out += [f"- {x}" for x in m["resumen"]] or ["- (sin resumen)"]
    out += ["", f"## To-dos ({len(m['todos'])})", ""]
    if m["todos"]:
        out += ["| # | To-do | Responsable | Fecha | Proyecto | Torre | Dónde |", "|---|---|---|---|---|---|---|"]
        for i, t in enumerate(m["todos"], 1):
            cell = [str(t.get(k) or "").replace("|", "/").strip() for k in
                    ("titulo", "responsable", "fecha_compromiso", "proyecto", "torre")]
            out.append(f"| {i} | " + " | ".join(c or "—" for c in cell) + f" | {at(t)} |")
        quotes = [(i, t) for i, t in enumerate(m["todos"], 1) if str(t.get("cita") or "").strip()]
        if quotes:
            out += ["", "Citas:", ""] + [f"- {i}. «{str(t['cita']).strip()}» {at(t)}" for i, t in quotes]
    else:
        out.append("- (no se detectaron compromisos)")
    for key, title in (("acuerdos", "Acuerdos"), ("decisiones", "Decisiones"), ("issues", "Issues abiertos")):
        out += ["", f"## {title}", ""]
        out += [f"- {x['texto'].strip()} {at(x)}" for x in m[key] if str(x.get("texto") or "").strip()] or ["- —"]
    return "\n".join(out) + "\n"


async def write_minuta(ctx: CheckContext, transcript: Path, executor: Any = None) -> Path | None:
    """Write the minuta of one saved transcript. None when no LLM step is available or nothing came back."""
    scope = ctx.scope
    ex = executor or getattr(ctx, "executor", None)
    if scope is None or ex is None:
        return None
    raw = transcript.read_text(encoding="utf-8")
    lines = raw.split("\n", 3)
    titled = lines[0].startswith("# ")
    subject = lines[0][2:].strip() if titled else transcript.stem
    meta = "\n".join(lines[1:3]) if titled else ""
    text = raw
    agent = scope.agents.get("argos") or scope.orchestrator
    pieces = chunks(text)
    parts: list[dict[str, Any]] = []
    for i, piece in enumerate(pieces, 1):
        prompt = (f"Meeting: {subject}\n{meta}\nPart {i} of {len(pieces)} of the transcript.\n\n{piece}\n\n"
                  "Call write_minuta with what was said in THIS part.")
        try:
            data = await ex.structured(
                scope, model=agent.model, system=[agent.role_prompt, MINUTA_NOTE], prompt=prompt,
                tool=WRITE_MINUTA_TOOL, max_tokens=min(scope.config.max_tokens, 8000),
            )
        except Exception as exc:  # noqa: BLE001 — the transcript is saved either way
            log.warning("minuta of %s failed: %s", transcript.name, exc)
            return None
        if isinstance(data, dict):
            parts.append(data)
    if not parts:
        return None
    path = minuta_path(transcript)
    path.write_text(render(subject, transcript, merge(parts, raw), len(raw)), encoding="utf-8")
    return path


def missing(folder: Path) -> list[Path]:
    """Transcripts in the folder without a minuta, newest first."""
    if not folder.is_dir():
        return []
    files = [p for p in folder.glob("*.txt") if not minuta_path(p).exists()]
    return sorted(files, key=lambda p: p.name, reverse=True)
