"""Meeting minutes next to each transcript (atlas/argos/minutes.py)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from atlas.argos import minutes as M

TRANSCRIPT = """# Junta Semanal
Fecha: 2026-10-05 09:00 (CST)
Fuente: transcripción de Teams (Microsoft Graph), id t1

[00:01] Juan Diego Paez: Arrancamos con los pendientes.

[05:10] Melanie Ruiz: Yo mando la lista de precios de Fiori el viernes.

[12:40] Josué Pérez: El B600 sigue atrasado en escrituración, lo vemos en issues.
"""


@dataclass
class FakeAgent:
    model: str = "m"
    role_prompt: str = "ARGOS"


@dataclass
class FakeScope:
    agents: dict[str, Any] = field(default_factory=lambda: {"argos": FakeAgent()})
    orchestrator: Any = field(default_factory=FakeAgent)
    config: Any = field(default_factory=lambda: type("C", (), {"max_tokens": 8000})())


class FakeExecutor:
    def __init__(self, answers: list[dict[str, Any]]):
        self.answers, self.prompts = answers, []

    async def structured(self, scope, *, prompt, tool, **kw):
        assert tool["name"] == "write_minuta" and "MINUTA" not in prompt
        self.prompts.append(prompt)
        return self.answers[len(self.prompts) - 1]


@dataclass
class Ctx:
    scope: Any
    executor: Any
    node: str = "corporate"


def test_minuta_points_to_what_was_said(tmp_path):
    path = tmp_path / "2026-10-05 Junta Semanal.txt"
    path.write_text(TRANSCRIPT, encoding="utf-8")
    ex = FakeExecutor([{
        "resumen": ["Pendientes de la semana"],
        "todos": [{"titulo": "Enviar lista de precios Fiori", "responsable": "Melanie Ruiz",
                   "fecha_compromiso": "2026-10-09", "proyecto": "SONOMA", "torre": "FIORI", "ts": "[05:10]",
                   "cita": "Yo mando la lista de precios de Fiori el viernes"},
                  {"titulo": "Inventado", "ts": "99:99"}],  # not in the transcript: dropped
        "issues": [{"texto": "B600 atrasado en escrituración", "ts": "12:40"}],
    }])
    out = asyncio.run(M.write_minuta(Ctx(FakeScope(), ex), path))
    assert out == tmp_path / "2026-10-05 Junta Semanal.minuta.md"
    text = out.read_text(encoding="utf-8")
    assert "# Minuta · Junta Semanal" in text and "## To-dos (1)" in text and "Inventado" not in text
    off = TRANSCRIPT.index("[05:10]")
    assert f"| Enviar lista de precios Fiori | Melanie Ruiz | 2026-10-09 | SONOMA | FIORI | [05:10] (carácter {off:,})" in text
    assert "B600 atrasado" in text and "Fecha: 2026-10-05" in ex.prompts[0]
    assert M.missing(tmp_path) == []


def test_long_transcripts_are_read_in_chunks_at_turns():
    paras = [f"[{i // 60:02d}:{i % 60:02d}] P: " + "x" * 1000 for i in range(250)]
    parts = M.chunks("\n\n".join(paras), size=90_000)
    assert len(parts) == 3 and all(len(p) <= 90_000 for p in parts)
    assert all(p.startswith("[") for p in parts) and sum(p.count("[") for p in parts) == 250


def test_no_llm_no_minuta(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text(TRANSCRIPT, encoding="utf-8")
    assert asyncio.run(M.write_minuta(Ctx(None, None), path)) is None
    assert M.missing(tmp_path) == [path]
