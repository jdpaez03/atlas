"""ARGOS private files: `<ATLAS_LOCAL_DIR>/argos/` (docs/ARGOS.md).

    argos_dir()          <ATLAS_LOCAL_DIR>/argos
    watch_path()         argos/watch.yaml  dashboard rules + L10 thresholds (a commented example is written if missing)
    load_watch()         the parsed watch.yaml as a dict; never raises (bad YAML -> {} and a warning)
    load_watch_with_error()  (config, error-or-None), so the engine can put a bad file in the mission report
    RunState             argos/state.json, key "runs" (last watch / brief, per-check status). Other keys in the file
                         (e.g. processed message ids kept by the dashboards check) are preserved on save.

Every top-level section the checks read (`dashboards`, `l10`, `rocks`) is guaranteed to be a dict.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..core import paths

log = logging.getLogger("atlas.argos")

SECTIONS = ("dashboards", "l10", "rocks")

WATCH_EXAMPLE = """\
# ARGOS watch rules (docs/ARGOS.md). Private: lives in your ATLAS_LOCAL_DIR, never committed.
# Edit freely; ARGOS re-reads this file on every run. A section with `enabled: false` turns that check off.

dashboards:
  enabled: true
  # How far back (days) to look for emails that carry the weekly dashboards
  lookback_days: 10
  # An email attachment is a dashboard when its subject or file name matches one of these
  # (each one is a case-insensitive regular expression; plain words work as-is)
  match:
    - semana
    - reporte semanal
    - dashboard
    - 'S\\d{2}'
  # File types that count as dashboards
  extensions: [.pdf, .xlsx, .xlsm]
  # Projects that must send a report every week (otherwise: any project seen in the previous 3 weeks)
  # expected: [Amāra, Nativa, Altavista]
  # Project keywords. By default ARGOS uses the same projects as inbox/digest_rules.yaml; entries here override them.
  # projects:
  #   Amāra: [amara, amāra]
  #   Nativa: [nativa, "torre n"]

l10:
  enabled: true
  # An open issue older than this many days (or with nobody to decide) is stale
  stale_issue_days: 14

rocks:
  enabled: true
  # The Rocks themselves live in argos/rocks.yaml
"""


def argos_dir() -> Path:
    return paths.local_dir() / "argos"


def watch_path() -> Path:
    return argos_dir() / "watch.yaml"


def state_path() -> Path:
    return argos_dir() / "state.json"


def ensure_watch_example(path: Path | None = None) -> Path:
    path = path or watch_path()
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(WATCH_EXAMPLE, encoding="utf-8")
            log.info("ARGOS: wrote an example %s", path)
        except OSError as exc:  # pragma: no cover - read-only disk
            log.warning("ARGOS: could not write %s: %s", path, exc)
    return path


def _normalize(raw: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    for key in SECTIONS:
        if not isinstance(cfg.get(key), dict):
            cfg[key] = {}
    return cfg


def load_watch_with_error(path: Path | None = None) -> tuple[dict[str, Any], str | None]:
    path = ensure_watch_example(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _normalize({}), None
    except OSError as exc:
        return _normalize({}), f"could not read {path.name}: {exc}"
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        where = getattr(exc, "problem_mark", None)
        at = f" (line {where.line + 1})" if where is not None else ""
        msg = f"{path.name} is not valid YAML{at}; using the defaults"
        log.warning("ARGOS: %s: %s", msg, exc)
        return _normalize({}), msg
    if raw is not None and not isinstance(raw, dict):
        return _normalize({}), f"{path.name} must be a mapping of sections; using the defaults"
    return _normalize(raw), None


def load_watch(path: Path | None = None) -> dict[str, Any]:
    return load_watch_with_error(path)[0]


def check_enabled(config: dict[str, Any], name: str) -> bool:
    section = config.get(name)
    return not (isinstance(section, dict) and section.get("enabled") is False)


# ---------------------------------------------------------------------------
# state.json ("runs")
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def _parse(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class CheckRun:
    last_run: datetime | None = None
    last_ok: bool | None = None
    state: str = "never run"  # ok | failed | not configured | never run
    note: str | None = None
    hint: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"last_run": _iso(self.last_run), "last_ok": self.last_ok, "state": self.state, "note": self.note,
                "hint": self.hint}

    @classmethod
    def from_json(cls, raw: Any) -> CheckRun:
        if not isinstance(raw, dict):
            return cls()
        return cls(last_run=_parse(raw.get("last_run")), last_ok=raw.get("last_ok"),
                   state=str(raw.get("state") or "never run"), note=raw.get("note"), hint=raw.get("hint"))


@dataclass
class RunState:
    path: Path | None = None
    last_watch: datetime | None = None
    last_brief: datetime | None = None
    checks: dict[str, CheckRun] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> RunState:
        path = path or state_path()
        st = cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return st
        except (OSError, ValueError) as exc:
            log.warning("ARGOS: ignoring unreadable %s: %s", path, exc)
            return st
        runs = data.get("runs") if isinstance(data, dict) else None
        if isinstance(runs, dict):
            st.last_watch = _parse(runs.get("last_watch"))
            st.last_brief = _parse(runs.get("last_brief"))
            checks = runs.get("checks")
            if isinstance(checks, dict):
                st.checks = {str(k): CheckRun.from_json(v) for k, v in checks.items()}
        return st

    def check(self, name: str) -> CheckRun:
        return self.checks.setdefault(name, CheckRun())

    def save(self) -> None:
        if self.path is None:
            return
        data: dict[str, Any] = {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            pass
        data["runs"] = {
            "last_watch": _iso(self.last_watch),
            "last_brief": _iso(self.last_brief),
            "checks": {k: v.to_json() for k, v in sorted(self.checks.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
