"""Prompts and tool schemas for live missions (docs/LIVE.md)."""

from __future__ import annotations

import json
from typing import Any

from ..core.models import (
    AgentReport,
    ApprovalReason,
    ClaimKind,
    Confidence,
    Priority,
    Task,
)
from ..publish.spec import SUBMIT_DOCUMENTS_TOOL

LANGUAGE_RULE = (
    "Language: write every free-text field in the same language as the mission objective "
    "(if the objective is in Spanish, answer in Spanish). Enum values and ids stay as defined."
)

# ---------------------------------------------------------------------------
# Shared protocol
# ---------------------------------------------------------------------------

PROTOCOL = f"""# ATLAS protocol
You are a member of ATLAS, a team of AI agents coordinated by the orchestrator ATLAS. You work on ONE task,
inside ONE mission, inside ONE node (an isolated operating context). Other agents work on other tasks in parallel.

Tools:
- consult(agent_id, question): ask another agent of this mission one focused question. Use it sparingly, only when
  their specialty is needed. The answer comes back as the tool result.
- request_approval(reason, title, detail, proposed_action?): pause and ask the human. REQUIRED before any external
  action (sending, publishing, contacting anyone), any financial commitment, any irreversible step, and when the
  information is ambiguous, conflicting or insufficient to continue responsibly. The human's decision and note come
  back as the tool result; a note may also answer a question you asked. If rejected, do not perform the action.
- list_files(path?, pattern?, recursive?), search_files(query, under?), read_file(path, offset?, max_chars?,
  sheet?): read-only access to the user's files, limited to the folders this node allows plus the mission's
  attachments. list_files with no path shows those folders. read_file extracts text (txt/md/csv/json, PDF, Excel
  with one block per sheet, Word, PowerPoint); long files come in pages: continue with the offset it tells you.
  Images (png/jpg/webp/gif/bmp/tif: screenshots, photos, scans) come back as a picture you can see, and so do the
  first pages of a PDF without a text layer: read them.
  Meeting transcripts (transcripts folder) come with a '<name>.minuta.md' (summary, to-dos with owner/date,
  agreements, issues, each with its minute and character offset): read the minuta first, and open the transcript
  with read_file(offset=…) only around the points you need to verify, instead of reading all of it.
  You can never modify, move or delete the user's files.
- write_deliverable(filename, format, content?, sheets?, document?): create a file for the human (md, txt, csv,
  json, xlsx with sheets {{name: rows[][]}}, docx from markdown-style content, and branded pdf / pptx from a
  `document` spec: title, summary, sections of blocks for the PDF; slides for the deck, in the company's brand
  kit; photos/renders from the files you can read via cover_image, image slides and image blocks). It goes to the mission's outputs folder and never overwrites anything. It is the ONLY way to produce a
  file. Deliverables of this node's earlier missions are readable under outputs/<node>/<mission_id>/: read them
  in place, no copy needed.
- collect_site_images(url): renders and photos from a page of any public website, saved as imagenes/<name> for
  your documents. The company's own sites are its material; images of other sites (a project's microsite, a
  partner, a brand) are usable too: put them in the document with the source credited in the caption
  ("Fuente: <domain>"), never replace them with a text box. imagenes/fuentes.csv records where each came from.
- suite_read / suite_add_todos / suite_open_week / suite_close_week (when PAGA Suite is configured): the Level 10
  module, live. The write tools ask the human inside the tool and write only what was approved; never claim a
  Suite change the tool didn't confirm.
- submit_report(...): deliver your result. It ends your work on the task. Always finish by calling it.
- Role tools (e.g. web_search) when available.

Evidence: the system records every tool call you make (files listed, read and written, web searches, consultations,
approvals) and attaches that log to your report. Never claim an action you didn't take through a tool: do not say
you read, opened, exported, saved or sent a file unless a tool call did it. Claims without a record are flagged as
unverified and lower your report's confidence.

Rules:
- Never present an assumption as a fact. Tag every finding: FACT (verified, with its source), ASSUMPTION (believed
  but not verified), SCENARIO (a what-if projection), RECOMMENDATION (what should be done).
- Say what is unresolved and what limits your confidence.
- Never take or promise external actions without request_approval. You cannot act in the outside world: you
  prepare, analyze and recommend.
- Never reference information from outside the provided context, the task inputs, the answers you receive and
  your tool results. Do not invent sources, figures or names. If data is missing, say so.
- Be concise and concrete: other agents and the human will read your report.
- {LANGUAGE_RULE}"""

CONSULT_PROTOCOL = f"""# ATLAS protocol (consultation)
You are a member of ATLAS. Another agent of the same mission is consulting you with one question about your
specialty. Answer it directly in one message (no tools, no follow-up questions), in at most ~300 words.
Separate facts from assumptions explicitly, never invent data, and say what you cannot know from the information given.
Only use the context provided here and the question. {LANGUAGE_RULE}"""

ORCHESTRATOR = f"""# ATLAS orchestrator protocol
You are ATLAS, the orchestrator of a team of specialist AI agents. You never do the specialists' work yourself:
you decompose the objective, delegate, review and consolidate.

Stages (each request tells you which one you are in):
1. PLANNING: call create_plan with 1–8 tasks. Each task goes to exactly ONE agent from the roster (use their ids),
   has a clear deliverable, and lists depends_on refs only when it truly needs another task's output (tasks without
   dependencies run in parallel). Set requires_approval=true (with approval_reason) for tasks that involve external
   communication, financial commitments, irreversible actions, or consequential decisions. Only use agents from the
   roster. Every task costs a full agent run (its own context, tools and report), so plan the FEWEST tasks that do
   the job:
   - If one agent has the tools to do the whole objective (read, analyze and write/register), plan ONE task.
   - Never give the same agent two tasks where one depends on the other: merge them into one task.
   - Add a second agent only for a genuinely different specialty or for independent work that can run in parallel.
   - Do not add separate "review", "verify" or "summarize" tasks: AUDITOR and your review already do that.
2. REVIEW: read every report against the objective and call request_followups. Open follow-up tasks (at most 3)
   only to fill a real gap or resolve a conflict between reports; otherwise send an empty list.
3. CONSOLIDATION: call submit_mission_report with an executive summary a busy decision-maker can act on. Keep the
   FACT / ASSUMPTION / SCENARIO / RECOMMENDATION tags of the findings you carry over, surface conflicts, failures and
   anything that needs human attention. Never upgrade an assumption to a fact.

Never reference information from outside the objective, the node context and the agents' reports.
{LANGUAGE_RULE}"""


def context_block(context: str) -> str:
    return (
        "# Node context (private material for this node only)\n" + context
        if context
        else "# Node context\n(no local context was provided for this node)"
    )


def cache_control(ttl: str | None = None) -> dict[str, str]:
    """Prompt-cache breakpoint; `ttl` "1h" keeps it across long waits (default 5 minutes)."""
    return {"type": "ephemeral", "ttl": ttl} if ttl in ("5m", "1h") else {"type": "ephemeral"}


def system_blocks(*texts: str, ttl: str | None = None) -> list[dict[str, Any]]:
    """System prompt as text blocks; the last one carries the prompt-cache breakpoint."""
    blocks: list[dict[str, Any]] = [{"type": "text", "text": t} for t in texts if t]
    if blocks:
        blocks[-1]["cache_control"] = cache_control(ttl)
    return blocks


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_KINDS = [k.value for k in ClaimKind]
_CONF = [c.value for c in Confidence]
_PRIO = [p.value for p in Priority]
_REASONS = [r.value for r in ApprovalReason]
_STR_LIST = {"type": "array", "items": {"type": "string"}}

CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": _KINDS},
        "statement": {"type": "string"},
        "sources": {**_STR_LIST, "description": "where it comes from (context file, report, URL...)"},
        "confidence": {"type": "string", "enum": _CONF},
    },
    "required": ["kind", "statement", "confidence"],
}


def _task_schema(agent_ids: list[str], *, ref_help: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "short unique id for this task, e.g. 'research'"},
            "title": {"type": "string", "description": "short imperative title"},
            "description": {"type": "string", "description": "what to do and the expected deliverable"},
            "assigned_to": {"type": "string", "enum": agent_ids},
            "priority": {"type": "string", "enum": _PRIO},
            "depends_on": {**_STR_LIST, "description": ref_help},
            "requires_approval": {"type": "boolean"},
            "approval_reason": {"type": "string", "enum": _REASONS},
        },
        "required": ["ref", "title", "description", "assigned_to"],
    }


def create_plan_tool(agent_ids: list[str]) -> dict[str, Any]:
    return {
        "name": "create_plan",
        "description": "Decompose the objective into 1-8 tasks for the roster agents (a DAG). Fewest tasks that do the job.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rationale": {"type": "string", "description": "one or two sentences on the approach"},
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": _task_schema(agent_ids, ref_help="refs of tasks in this plan it needs"),
                },
            },
            "required": ["tasks"],
        },
    }


def followups_tool(agent_ids: list[str]) -> dict[str, Any]:
    return {
        "name": "request_followups",
        "description": "After reviewing the reports: open at most 3 follow-up tasks, or none (empty list).",
        "input_schema": {
            "type": "object",
            "properties": {
                "assessment": {"type": "string", "description": "short verdict on the reports"},
                "tasks": {
                    "type": "array",
                    "maxItems": 3,
                    "items": _task_schema(
                        agent_ids, ref_help="refs of existing tasks or of other follow-ups it needs"
                    ),
                },
            },
            "required": ["tasks"],
        },
    }


def consult_tool(agent_ids: list[str]) -> dict[str, Any]:
    return {
        "name": "consult",
        "description": "Ask another agent of this mission one focused question; returns their answer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "enum": agent_ids},
                "question": {"type": "string"},
            },
            "required": ["agent_id", "question"],
        },
    }


REQUEST_APPROVAL_TOOL: dict[str, Any] = {
    "name": "request_approval",
    "description": (
        "Pause and ask the human to approve or reject (external actions, money, irreversible steps, "
        "ambiguous/conflicting or missing information). Returns the decision and the human's note."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "enum": _REASONS},
            "title": {"type": "string", "description": "short title shown to the human"},
            "detail": {"type": "string", "description": "what you need decided and why"},
            "proposed_action": {"type": "string", "description": "exactly what would be done if approved"},
        },
        "required": ["reason", "title", "detail"],
    },
}

SUBMIT_REPORT_TOOL: dict[str, Any] = {
    "name": "submit_report",
    "description": "Deliver your report for the task. Ends your work on it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "asked_to": {"type": "string", "description": "one line: what you were asked to do"},
            "actions_taken": {**_STR_LIST, "description": "what you did"},
            "inputs_used": {**_STR_LIST, "description": "inputs, reports and sources you used"},
            "findings": {"type": "array", "items": CLAIM_SCHEMA},
            "unresolved": {**_STR_LIST, "description": "open questions and gaps"},
            "needs_agents": {**_STR_LIST, "description": "agent ids whose help is still needed"},
            "confidence": {"type": "string", "enum": _CONF},
            "limitations": _STR_LIST,
        },
        "required": ["actions_taken", "findings", "confidence"],
    },
}

SUBMIT_MISSION_REPORT_TOOL: dict[str, Any] = {
    "name": "submit_mission_report",
    "description": "Deliver the consolidated executive mission report.",
    "input_schema": {
        "type": "object",
        "properties": {
            "executive_summary": {"type": "string"},
            "objective_status": {"type": "string", "enum": ["ACHIEVED", "PARTIAL", "NOT_ACHIEVED"]},
            "key_findings": {"type": "array", "items": CLAIM_SCHEMA},
            "conflicts": _STR_LIST,
            "assumptions": _STR_LIST,
            "needs_human_attention": _STR_LIST,
            "next_actions": _STR_LIST,
            "references": _STR_LIST,
            "topics": {"type": "array", "items": {"type": "string"},
                       "description": "2-8 short names of the projects, areas and people this mission is about "
                                      "(e.g. 'Balcones 800', 'Torre Acqua', 'crédito puente') — the memory index"},
        },
        "required": ["executive_summary", "objective_status", "key_findings"],
    },
}


LIST_FILES_TOOL: dict[str, Any] = {
    "name": "list_files",
    "description": (
        "List a folder you are allowed to read (read-only): name, size, modified date; at most 500 entries. "
        "Without a path, shows the readable folders, the mission attachments and your deliverables folder."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "folder path (absolute, or relative to a readable folder)"},
            "pattern": {"type": "string", "description": "optional name filter, e.g. '*.xlsx'"},
            "recursive": {"type": "boolean", "description": "include subfolders (limited depth)"},
        },
    },
}

SEARCH_FILES_TOOL: dict[str, Any] = {
    "name": "search_files",
    "description": "Find files by name under the readable folders (at most 100 matches).",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "words in the file name, or a glob like '*rocks*.xlsx'"},
            "under": {"type": "string", "description": "optional folder to search in"},
        },
        "required": ["query"],
    },
}

READ_FILE_TOOL: dict[str, Any] = {
    "name": "read_file",
    "description": (
        "Read a file (read-only): the text of txt/md/csv/json/code, PDF, Excel (xlsx/xlsm: one block per sheet), "
        "Word (docx: paragraphs and tables), PowerPoint (pptx); long files are paged: use `offset`. Images "
        "(png/jpg/webp/gif/bmp/tif) and scanned PDFs come back as pictures for you to look at."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0, "description": "character offset to start from"},
            "max_chars": {"type": "integer", "minimum": 1, "description": "characters to return (capped)"},
            "sheet": {"type": "string", "description": "Excel only: read just this sheet"},
        },
        "required": ["path"],
    },
}

# pdf / pptx deliverables: the document spec SCRIBE uses (publish/spec.py), nothing required but the title
_DOCUMENT_SCHEMA: dict[str, Any] = {
    **{k: v for k, v in SUBMIT_DOCUMENTS_TOOL["input_schema"].items() if k != "required"},
    "description": "pdf / pptx only: {title, subtitle?, summary[], highlights?, sections[{title, blocks}], "
                   "slides[], sources?, style?}",
    "required": ["title"],
}

WRITE_DELIVERABLE_TOOL: dict[str, Any] = {
    "name": "write_deliverable",
    "description": (
        "Create a deliverable file for the human in the mission outputs folder (never overwrites; returns its "
        "download link). md/txt/csv/json take `content`; xlsx takes `sheets` {sheet name: rows[][]}; docx takes "
        "markdown-style `content` (# headings, - bullets, 1. lists, | tables |, **bold**); pdf (institutional "
        "report) and pptx (presentation) take `document` and are laid out in the company's brand kit: for pptx give "
        "every slide in `slides` (cover and closing are added), for pdf give `summary` and `sections`. Photos and "
        "renders: `cover_image`, image slides {type: image, image, title, items?, caption, layout: side|full} and "
        "image blocks {type: image, image, caption}, where `image` is the path of a png/jpg/webp you can read "
        "(e.g. onedrive:/…/Renders/fachada.jpg, or imagenes/<name> from collect_site_images). Credit images that "
        "aren't the company's own in the caption (\"Fuente: <domain>\")."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "file name, e.g. 'resumen_q3'"},
            "format": {"type": "string", "enum": ["md", "txt", "csv", "json", "xlsx", "docx", "pdf", "pptx"]},
            "content": {"type": "string"},
            "sheets": {
                "type": "object",
                "description": "xlsx: {sheet name: rows}, each row an array of cell values",
                "additionalProperties": {"type": "array", "items": {"type": "array"}},
            },
            "document": _DOCUMENT_SCHEMA,
        },
        "required": ["filename", "format"],
    },
}

COLLECT_SITE_IMAGES_TOOL: dict[str, Any] = {
    "name": "collect_site_images",
    "description": (
        "Collect the large images (renders, photos) of a page of any public website: the page opens in a real "
        "browser, lazy galleries load, and every image of at least min_px is saved to the mission outputs. The "
        "company's own sites (see the task's Files section) save as imagenes/<name> (company material); other "
        "sites as imagenes/<domain>/<name>: use those too, crediting the source in the caption ('Fuente: "
        "<domain>'). Use them in write_deliverable (`image`: 'imagenes/…'). Also lists the site's other pages "
        "(open each project's page to get its renders). Look at the sizes and names to pick; read nothing else "
        "into them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "a web page, e.g. https://pagadesarrollos.com/ or a project's microsite"},
            "max_images": {"type": "integer", "minimum": 1, "maximum": 24, "description": "default 24"},
            "min_px": {"type": "integer", "minimum": 200, "description": "smallest longest side kept (default 600)"},
        },
        "required": ["url"],
    },
}

FILE_TOOLS: list[dict[str, Any]] = [LIST_FILES_TOOL, SEARCH_FILES_TOOL, READ_FILE_TOOL, WRITE_DELIVERABLE_TOOL,
                                    COLLECT_SITE_IMAGES_TOOL]


def _btool(name: str, description: str, props: dict[str, Any] | None = None,
           required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "input_schema": schema}


_REF = {"type": "string", "description": "element ref from the last browser_snapshot, e.g. e12 or f1e3"}
_CONFIRM = {"type": "boolean", "description": "true only after request_approval was approved for this click"}

# Only for agents with a `browser:` block (docs/BROWSER.md). The agent's dedicated, already signed-in profile.
BROWSER_TOOLS: list[dict[str, Any]] = [
    _btool("browser_open", "Open a page of the allowed sites in your dedicated browser (no url = the start page).",
           {"url": {"type": "string"}}),
    _btool("browser_snapshot",
           "Read the current page: URL, title, visible text (paged with offset) and the interactive elements "
           "with refs (e12) to click / type / select. Call it after every change of page.",
           {"offset": {"type": "integer", "minimum": 0}, "max_chars": {"type": "integer", "minimum": 500},
            "filter": {"type": "string", "description": "only elements whose text contains this"},
            "max_elements": {"type": "integer", "minimum": 10},
            "elements_only": {"type": "boolean", "description": "skip the page text"}}),
    _btool("browser_click", "Click an element (button, link, tab, menu item, checkbox, option).",
           {"ref": _REF, "confirm": _CONFIRM}, ["ref"]),
    _btool("browser_type", "Type into a field (search box, filter). Never for passwords.",
           {"ref": _REF, "text": {"type": "string"},
            "submit": {"type": "boolean", "description": "press Enter afterwards"},
            "clear": {"type": "boolean", "description": "replace the current value (default true)"}},
           ["ref", "text"]),
    _btool("browser_select", "Choose an option of a <select> dropdown (visible text or value). Custom "
           "dropdowns: click them, snapshot, click the option.",
           {"ref": _REF, "option": {"type": "string"}}, ["ref", "option"]),
    _btool("browser_press", "Press a key on the page (Enter, Escape, Tab, PageDown, ArrowDown ...).",
           {"key": {"type": "string"}}, ["key"]),
    _btool("browser_scroll", "Scroll the page (loads lazy lists) or bring an element into view.",
           {"direction": {"type": "string", "enum": ["down", "up"]}, "ref": _REF}),
    _btool("browser_wait", "Wait for the page (reports that take time to build), optionally until a text appears.",
           {"seconds": {"type": "number", "minimum": 0.5, "maximum": 60}, "text": {"type": "string"}}),
    _btool("browser_back", "Go back to the previous page."),
    _btool("browser_tables",
           "Extract the visible tables / grids of the page (preview). With save_as, save them to the mission "
           "outputs as xlsx (all, one sheet each) or csv (one: give index).",
           {"index": {"type": "integer", "minimum": 1}, "save_as": {"type": "string", "description": "file name"},
            "format": {"type": "string", "enum": ["xlsx", "csv"]}}),
    _btool("browser_download",
           "Click an export / download button and save the file it produces to the mission outputs (then read it "
           "with read_file). Waits up to wait_seconds (default 90).",
           {"ref": _REF, "filename": {"type": "string", "description": "optional name to save it as"},
            "wait_seconds": {"type": "number", "minimum": 5, "maximum": 300}, "confirm": _CONFIRM},
           ["ref"]),
    _btool("browser_charts",
           "Read the numbers behind the charts on the page (charts are drawn as images: their values are not in "
           "the page text). Lists each chart with its title and a table of labels × series. With save_as, saves "
           "them all to the mission outputs as xlsx (one sheet per chart).",
           {"save_as": {"type": "string", "description": "file name"}}),
    _btool("browser_data",
           "The data the pages downloaded from the site's own API while loading (JSON): usually the exact numbers "
           "behind the charts and KPI cards. No index = the list (newest first, with each response's shape); "
           "index = that response, its records as a table and the JSON. save_as saves it as json (+ xlsx of the "
           "records). Captured while pages load: open the view or change a filter first.",
           {"index": {"type": "integer", "minimum": 1}, "filter": {"type": "string", "description": "only "
            "responses whose URL or content contains this"}, "save_as": {"type": "string"}}),
    _btool("browser_screenshot",
           "Take a screenshot and LOOK at it: the page's visible area, the full page, or one element (a chart's "
           "ref). Use it when browser_charts finds no data, or to check what the screen shows. Saved to the "
           "mission outputs under capturas/.",
           {"ref": _REF, "full_page": {"type": "boolean"},
            "name": {"type": "string", "description": "file name, e.g. absorcion_fiori"}}),
]


def web_search_tool(tool_type: str, max_uses: int = 5) -> dict[str, Any]:
    return {"type": tool_type, "name": "web_search", "max_uses": max_uses}


# ---------------------------------------------------------------------------
# User messages
# ---------------------------------------------------------------------------


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


def render_report(report: AgentReport, *, title: str, agent_name: str, ref: str | None = None) -> str:
    lines = [f"## Report · {title}" + (f" [ref: {ref}]" if ref else "") + f" · by {agent_name}",
             f"Confidence: {report.confidence}"]
    if report.actions_taken:
        lines.append("Actions: " + "; ".join(report.actions_taken))
    if report.inputs_used:
        lines.append("Inputs: " + "; ".join(report.inputs_used))
    lines.append("Findings:")
    for c in report.findings:
        src = f" (sources: {', '.join(c.sources)})" if c.sources else ""
        lines.append(f"- [{c.kind} · {c.confidence}] {c.statement}{src}")
    if not report.findings:
        lines.append("- (none)")
    if report.unresolved:
        lines.append("Unresolved: " + "; ".join(report.unresolved))
    if report.limitations:
        lines.append("Limitations: " + "; ".join(report.limitations))
    return "\n".join(lines)


def roster_text(roster: list[dict[str, Any]]) -> str:
    return _j(roster)


def planning_message(objective: str, node: str, roster: list[dict[str, Any]], tools_note: str = "") -> str:
    return (
        "STAGE: PLANNING\n"
        f"Node: {node}\n"
        f"Mission objective:\n{objective}\n\n"
        f"Roster (the ONLY agents you may assign):\n{roster_text(roster)}\n\n"
        + (f"Tools every agent has:\n{tools_note}\n\n" if tools_note else "")
        + "Call create_plan now."
    )


def plan_errors_message(errors: list[str]) -> str:
    return "The plan is invalid:\n- " + "\n- ".join(errors) + "\nFix these problems and call create_plan again."


def task_message(objective: str, node: str, task: Task, dep_reports: list[str], *,
                 consultable: list[str], approval_required: bool, files_note: str | None = None) -> str:
    parts = [
        f"Mission objective ({node} node):\n{objective}",
        f"Your task: {task.title}\n{task.description}",
        f"Priority: {task.priority}",
    ]
    if approval_required:
        parts.append(
            f"This task REQUIRES human approval (reason: {task.approval_reason or 'CONSEQUENTIAL_DECISION'}): "
            "call request_approval before you submit your report."
        )
    if dep_reports:
        parts.append("Inputs from the tasks you depend on:\n\n" + "\n\n".join(dep_reports))
    else:
        parts.append("You have no inputs from other tasks.")
    if files_note:
        parts.append(files_note)
    parts.append(
        "Agents you may consult: " + (", ".join(consultable) if consultable else "none")
        + "\nWork on the task, then call submit_report."
    )
    return "\n\n".join(parts)


STAGE_SPLIT = "\n\nSTAGE: "


def dossier(objective: str, tasks_text: str, reports: list[str], failures: list[str]) -> str:
    """What REVIEW and CONSOLIDATION both read, byte-identical, so the second call reads it from the prompt cache."""
    parts = [
        f"Mission objective:\n{objective}",
        f"Tasks (ref · title · agent · status):\n{tasks_text}",
        "Reports:\n\n" + ("\n\n".join(reports) if reports else "(no reports)"),
    ]
    if failures:
        parts.append("Failures:\n- " + "\n- ".join(failures))
    return "\n\n".join(parts)


def split_stage(prompt: str) -> tuple[str, str]:
    """(cacheable dossier, stage instructions) of a REVIEW / CONSOLIDATION prompt; ("", prompt) otherwise."""
    head, sep, tail = prompt.partition(STAGE_SPLIT)
    return (head, "STAGE: " + tail) if sep and head else ("", prompt)


def review_message(objective: str, tasks_text: str, reports: list[str], failures: list[str],
                   roster: list[dict[str, Any]]) -> str:
    parts = [
        dossier(objective, tasks_text, reports, failures),
        "STAGE: REVIEW",
        f"Roster:\n{roster_text(roster)}",
        "Call request_followups (empty tasks list if no follow-up is needed).",
    ]
    return "\n\n".join(parts)


def consolidation_message(objective: str, tasks_text: str, reports: list[str], failures: list[str]) -> str:
    parts = [dossier(objective, tasks_text, reports, failures), "STAGE: CONSOLIDATION"]
    if failures:
        parts.append("List every failure above under needs_human_attention.")
    parts.append("Call submit_mission_report now.")
    return "\n\n".join(parts)


def consult_message(objective: str, from_name: str, question: str) -> str:
    return f"Mission objective:\n{objective}\n\n{from_name} asks you:\n{question}"


# ---------------------------------------------------------------------------
# Mission thread (docs/PHASE3.md B) — builder P
# ---------------------------------------------------------------------------


def guidance_block(notes: list[str]) -> str:
    """Notes the human wrote in the mission thread, for every task / ATLAS step that starts afterwards."""
    if not notes:
        return ""
    return (
        "# Human guidance (from the mission thread, oldest first)\n"
        "The human who owns this mission added these notes. Follow them when they apply to your work; they take "
        "precedence over earlier instructions where they conflict.\n- " + "\n- ".join(notes)
    )


def attachments_block(names: list[str]) -> str:
    if not names:
        return ""
    return "Files the human attached to this mission (mission attachments folder): " + ", ".join(names)


def with_mission_notes(prompt: str, notes: list[str], attachments: list[str] | None = None) -> str:
    extra = [b for b in (attachments_block(attachments or []), guidance_block(notes)) if b]
    return "\n\n".join([prompt, *extra]) if extra else prompt


ACKNOWLEDGE_TOOL: dict[str, Any] = {
    "name": "acknowledge",
    "description": "Acknowledge the human's note in one or two short sentences.",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "short acknowledgement (max ~40 words)"}},
        "required": ["text"],
    },
}


def acknowledge_message(objective: str, note: str, phase: str) -> str:
    return (
        "STAGE: ACKNOWLEDGE\n"
        f"Mission objective:\n{objective}\n\nCurrent phase: {phase}\n\n"
        f"The human just wrote in the mission thread:\n{note}\n\n"
        "It will be passed to every task that starts from now on and to your review and consolidation. "
        "Call acknowledge with a short reply saying how it will be applied. Do not start any work."
    )


def respond_to_followup_tool(agent_ids: list[str]) -> dict[str, Any]:
    return {
        "name": "respond_to_followup",
        "description": (
            "Reply to the human's follow-up on a finished mission: answer directly from what the mission already "
            "produced, and/or open a new round of 1-6 tasks for the roster agents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "description": "your reply to the human (always say what happens next)"},
                "tasks": {
                    "type": "array",
                    "maxItems": 6,
                    "items": _task_schema(
                        agent_ids, ref_help="refs of earlier tasks (T1, T2...) or of other new tasks it needs"
                    ),
                },
            },
        },
    }


FOLLOWUP_RULES = """Decide:
- If the existing reports already answer it, reply with `answer` only (no tasks).
- If new work is needed, open a new round with `tasks` (1-6, one agent each, roster ids only). Tasks may depend on
  earlier tasks by their ref (T1, T2...) to receive those reports as inputs, or on each other. Also give a short
  `answer` telling the human what you are doing.
Call respond_to_followup now."""


def followup_message(objective: str, node: str, *, request: str, round_no: int, interrupted: bool,
                     latest_report: str, tasks_text: str, reports: list[str], thread: list[str],
                     attachments: list[str], roster: list[dict[str, Any]]) -> str:
    parts = [
        "STAGE: FOLLOW-UP",
        f"Node: {node}",
        f"Mission objective:\n{objective}",
    ]
    if interrupted:
        parts.append("NOTE: this mission was interrupted (the server stopped) before it finished; its open tasks "
                     "were cancelled. The human may want it resumed.")
    parts += [
        f"Latest mission report:\n{latest_report or '(none yet)'}",
        f"Tasks so far (ref · title · agent · status · round):\n{tasks_text or '(none)'}",
        "Agent reports:\n\n" + ("\n\n".join(reports) if reports else "(no reports)"),
        "Mission thread (oldest first):\n" + ("\n".join(thread) if thread else "(empty)"),
    ]
    if attachments:
        parts.append(attachments_block(attachments))
    parts += [
        f"Roster (the ONLY agents you may assign):\n{roster_text(roster)}",
        f"The human's new message (round {round_no + 1} if you open tasks):\n{request}",
        FOLLOWUP_RULES,
    ]
    return "\n\n".join(parts)


def followup_consolidation_note(request: str, round_no: int) -> str:
    return (f"This is follow-up round {round_no}, opened for the human's request:\n{request}\n"
            "Write the mission report as the updated, complete version: integrate the new reports with the earlier "
            "findings and answer the request.")


def render_mission_report(report: Any) -> str:
    lines = [f"v{report.version} · {report.objective_status}", report.executive_summary]
    for c in report.key_findings:
        lines.append(f"- [{c.kind}] {c.statement}")
    if report.next_actions:
        lines.append("Next actions: " + "; ".join(report.next_actions))
    if report.needs_human_attention:
        lines.append("Needs attention: " + "; ".join(report.needs_human_attention))
    return "\n".join(lines)


def files_note(readable: list[str], attachments_dir: str, outputs: str) -> str:
    """What the agent may read and where its deliverables go (part of the task message)."""
    lines = ["Files (read-only, through list_files / search_files / read_file):"]
    lines += [f"- {r}" for r in readable] or ["- (no folders are configured for this node)"]
    if any(r.startswith(("onedrive:", "sharepoint:")) for r in readable):
        lines.append("  (onedrive:/sharepoint: folders are read through Microsoft 365: use those labels as paths, e.g. "
                     "onedrive:/Folder/file.xlsx; search_files there also matches file contents)")
    lines.append(f"- the mission attachments folder: {attachments_dir} (a bare attachment name also works)")
    lines.append(f"Deliverables you create with write_deliverable go to: {outputs}")
    from .siteimages import own_sites

    if own_sites():
        lines.append("Company websites (own material; start there with collect_site_images, then any other site "
                     "that shows the project, crediting it): " + ", ".join(own_sites()))
    return "\n".join(lines)
