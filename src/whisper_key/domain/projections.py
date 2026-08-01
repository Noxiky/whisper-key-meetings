import html
import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import quote

MARKER_LABELS = {
    "question": "Question",
    "not_understood": "Not understood",
    "important": "Important",
    "investigate": "Investigate",
    "quote": "Quote",
    "disagreement": "Disagreement",
    "action": "Action",
}


def format_timestamp(milliseconds: int) -> str:
    total_seconds = max(0, int(milliseconds) // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def render_literal_markdown(session: dict, events: Iterable[dict]) -> str:
    ordered = sorted(events, key=lambda event: event["sequence"])
    title = _escape_markdown_inline(session.get("title") or "Untitled session")
    mode = str(session["mode"]).replace("_", " ").title()
    lines = [f"# {title}", "", f"Mode: {mode}", "", "## Literal transcript", ""]

    transcripts = [event for event in ordered if event["type"] == "transcript_final"]
    transcripts.sort(key=lambda event: (event["payload"]["started_at_ms"], event["sequence"]))
    for event in transcripts:
        payload = event["payload"]
        started = format_timestamp(payload["started_at_ms"])
        ended = format_timestamp(payload["ended_at_ms"])
        language = _escape_markdown_inline(payload.get("language") or "und")
        lines.extend(
            [
                f"[{started}–{ended}] **{payload['source']} · {language}**",
                "",
                _escape_markdown_text(payload["raw_text"].strip()),
                "",
            ]
        )

    lines.extend(["## Markers and media", ""])
    context_events = [
        event for event in ordered if event["type"] in {"marker_created", "spoken_note", "snapshot_created"}
    ]
    context_events.sort(
        key=lambda event: (
            event["payload"].get("at_ms", event["payload"].get("started_at_ms", 0)),
            event["sequence"],
        )
    )
    for event in context_events:
        payload = event["payload"]
        timestamp = format_timestamp(payload.get("at_ms", payload.get("started_at_ms", 0)))
        if event["type"] == "marker_created":
            label = MARKER_LABELS[payload["kind"]]
            note = _escape_markdown_text(payload.get("note") or "No note")
            lines.append(f"- [{timestamp}] **{label}:** {note}")
        elif event["type"] == "spoken_note":
            lines.append(f"- [{timestamp}] **Spoken note:** {_escape_markdown_text(payload['raw_text'].strip())}")
        else:
            label = str(payload["kind"]).replace("_", " ").title()
            path = payload["relative_path"].replace("\\", "/")
            lines.append(f"- [{timestamp}] **{label}:** {_markdown_link(path, path)}")

    return "\n".join(lines).rstrip() + "\n"


def render_clean_markdown(
    session: dict,
    events: Iterable[dict],
    revision: int = 1,
    speaker_revision: dict | None = None,
) -> str:
    title = _escape_markdown_inline(session.get("title") or "Untitled session")
    lines = [f"# {title} · Clean", "", f"Revision: {revision}", "", "## Transcript", ""]
    transcripts = sorted(
        (event for event in events if event["type"] == "transcript_final"),
        key=lambda event: (event["payload"]["started_at_ms"], event["sequence"]),
    )
    for event in transcripts:
        payload = event["payload"]
        text = _escape_markdown_text(_normalize_literal_text(payload["raw_text"]))
        source = _escape_markdown_inline(_speaker_label(payload, speaker_revision))
        lines.extend(
            [
                f"### {format_timestamp(payload['started_at_ms'])} · {source}",
                "",
                text,
                "",
                f"<!-- raw-segment:{payload['segment_id']} -->",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_markers_markdown(session: dict, events: Iterable[dict]) -> str:
    title = _escape_markdown_inline(session.get("title") or "Untitled session")
    ordered = sorted(events, key=lambda event: event["sequence"])
    attachments = [event["payload"] for event in ordered if event["type"] == "snapshot_created"]
    spoken_notes = {
        event["payload"]["marker_id"]: event["payload"] for event in ordered if event["type"] == "spoken_note"
    }
    lines = [f"# {title} · Markers and media", ""]
    for event in ordered:
        if event["type"] != "marker_created":
            continue
        marker = event["payload"]
        lines.extend(
            [
                f"## {format_timestamp(marker['at_ms'])} · {MARKER_LABELS[marker['kind']]}",
                "",
                _escape_markdown_text(marker.get("note") or "No note"),
                "",
            ]
        )
        spoken_note = spoken_notes.get(marker["marker_id"])
        if spoken_note:
            lines.extend([f"**Spoken note:** {_escape_markdown_text(spoken_note['raw_text'].strip())}", ""])
        related = [item for item in attachments if item.get("at_ms") == marker["at_ms"]]
        for item in related:
            path = item["relative_path"].replace("\\", "/")
            lines.append(f"- {_markdown_link(path, item['kind'])}")
        if related:
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_mode_markdown(
    session: dict,
    events: Iterable[dict],
    speaker_revision: dict | None = None,
    clean_markdown: str | None = None,
    markers_markdown: str | None = None,
) -> str:
    mode = session["mode"]
    heading = {
        "meeting": "Meeting record",
        "learning": "Learning record",
        "reading": "Reading record",
        "idea": "Idea record",
        "dictation": "Dictation record",
    }.get(mode, "Session record")
    clean = clean_markdown or render_clean_markdown(session, events, speaker_revision=speaker_revision)
    markers = markers_markdown or render_markers_markdown(session, events)
    return f"# {heading}\n\n{clean.split(chr(10), 1)[1].lstrip()}\n{markers.split(chr(10), 1)[1].lstrip()}"


def render_handoff_markdown(session: dict, manifest: dict | None = None) -> str:
    mode = session["mode"]
    inputs = manifest.get("inputs", []) if manifest else []
    attachments = manifest.get("attachments", []) if manifest else []
    selected = (
        "\n".join(
            f"- `{item['path']}` · {item['role']} · SHA-256 `{item['sha256']}`" for item in [*inputs, *attachments]
        )
        or "- `transcript.raw.md`\n- `transcript.clean.md`"
    )
    return (
        f"# LLM handoff · {_escape_markdown_inline(session.get('title') or 'Untitled session')}\n\n"
        "WhisperKey no sube ni envía automáticamente ningún archivo. Este paquete solo se usa "
        "cuando tú eliges abrirlo con Codex, Claude u otra herramienta.\n\n"
        "## Flujo preferido\n\n"
        "1. Ejecuta `nox-learn-anything` usando `handoff/handoff.json` como mapa de entradas.\n"
        "2. Guarda el aprendizaje procesado en `exports/downstream/learning.md`.\n"
        "3. Opcionalmente ejecuta `nox-html-learning` sobre ese Markdown y guarda "
        "`exports/downstream/learning.html`.\n\n"
        "## Archivos seleccionados y verificados\n\n"
        f"{selected}\n\n"
        "## Prompt listo para copiar\n\n"
        f"Procesa esta sesión `{mode}` sin modificar la evidencia original. Conserva referencias "
        "a IDs `raw-segment`, identifica incertidumbres, preguntas, contradicciones y próximas acciones. "
        "Escribe todo resultado nuevo únicamente dentro de `exports/downstream/`. "
        "No edites `transcript.raw.md`, `handoff/session.snapshot.json` ni "
        "`handoff/timeline.snapshot.jsonl`.\n"
    )


def render_self_contained_html(
    session: dict,
    events: Iterable[dict],
    embedded_images: dict[str, str],
    speaker_revision: dict | None = None,
) -> str:
    title = html.escape(session.get("title") or "Untitled session")
    transcript_blocks = []
    for event in sorted(events, key=lambda item: item["sequence"]):
        if event["type"] != "transcript_final":
            continue
        payload = event["payload"]
        source = _speaker_label(payload, speaker_revision)
        transcript_blocks.append(
            "<article><div class='meta'>"
            f"{html.escape(format_timestamp(payload['started_at_ms']))} · {html.escape(source)}"
            "</div><p>"
            f"{html.escape(_normalize_literal_text(payload['raw_text']))}</p>"
            f"<code>raw:{html.escape(payload['segment_id'])}</code></article>"
        )
    images = "".join(
        f"<figure><img src='{data}' alt='{html.escape(path)}'><figcaption>{html.escape(path)}</figcaption></figure>"
        for path, data in embedded_images.items()
    )
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{font:16px/1.55 system-ui,sans-serif;max-width:920px;margin:auto;padding:32px;color:#17202b;background:#f7f9fb}}
article,figure{{background:white;border:1px solid #dce2e9;border-radius:12px;padding:18px;margin:14px 0}}
.meta,code,figcaption{{color:#607083;font-size:13px}} img{{max-width:100%;height:auto;border-radius:8px}}
</style><body><h1>{title}</h1><p>Mode: {html.escape(session["mode"])}</p>
<h2>Transcript</h2>{"".join(transcript_blocks)}<h2>Visual context</h2>{images}</body></html>"""


def _normalize_literal_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def _escape_markdown_inline(value: object) -> str:
    return _escape_markdown_text(" ".join(str(value).split()))


def _escape_markdown_text(value: object) -> str:
    """Neutralize active Markdown/HTML while keeping ordinary punctuation readable."""
    text = str(value)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([!\[\]])", r"\\\1", text)


def _markdown_link(path: str, label: str) -> str:
    safe_url = quote(path.replace("\\", "/"), safe="/-._~")
    return f"[{_escape_markdown_inline(label)}]({safe_url})"


def _speaker_label(payload: dict, speaker_revision: dict | None) -> str:
    fallback = payload["source"]
    if not speaker_revision:
        return fallback
    assignments = {item["segment_id"]: item["speaker_id"] for item in speaker_revision.get("assignments", [])}
    display_names = {item["speaker_id"]: item["display_name"] for item in speaker_revision.get("speakers", [])}
    speaker_id = assignments.get(payload.get("segment_id"))
    return display_names.get(speaker_id, speaker_id or fallback)
