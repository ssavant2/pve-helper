from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import UTC, datetime, time
from xml.sax.saxutils import escape as xml_escape

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from core.models import ProxmoxCluster

from .common import (
    AUDIT_PAGE_SIZE,
    AuditEvent,
    HttpResponse,
    Q,
    StreamingHttpResponse,
    _decorate_audit_events,
    _safe_next_url,
    app_login_required,
    audit_retention_schedule_state,
    content_disposition_header,
    messages,
    navigation_context,
    record_audit_event,
    redirect,
    render,
    require_POST,
    update_audit_retention_schedule,
)

AUDIT_MODULE_FILTERS = [
    {"key": "all", "label": "All"},
    {"key": "auth", "label": "Auth"},
    {"key": "clusters", "label": "Clusters"},
    {"key": "vms", "label": "VMs"},
    {"key": "storage", "label": "Storage"},
    {"key": "network", "label": "Network"},
    {"key": "system", "label": "System"},
]
AUDIT_VALID_MODULES = {item["key"] for item in AUDIT_MODULE_FILTERS}
AUDIT_EXPORT_COLUMNS = ["Time", "Cluster", "Module", "User", "Source IP", "Action", "Object", "Details", "Outcome"]
# "Raw Details" is the whole payload; "Details" above is the readable one-liner.
AUDIT_EXPORT_TECH_COLUMNS = ["Raw Action", "Object Type", "Object ID", "Raw Details"]
# XLSX must be assembled as a ZIP archive in the web worker. CSV/JSON are
# streamed instead, so keep the only in-memory format deliberately bounded.
AUDIT_XLSX_MAX_ROWS = 5_000


@app_login_required
def audit_log(request):
    try:
        audit_page = int(request.GET.get("page", "0"))
    except ValueError:
        audit_page = 0
    audit_page = max(0, audit_page)

    module_filter = _audit_module_filter(request.GET.get("filter", "all"))
    query = _audit_query(request.GET.get("q", ""))
    cluster_filter = _audit_cluster_filter(request.GET.get("cluster", ""))
    events_qs = _audit_events_queryset(
        module_filter=module_filter,
        query=query,
        cluster_key=cluster_filter,
    )

    event_total = events_qs.count()
    max_page = (event_total - 1) // AUDIT_PAGE_SIZE if event_total else 0
    audit_page = min(audit_page, max_page)
    event_offset = audit_page * AUDIT_PAGE_SIZE
    events = list(events_qs.order_by("-timestamp")[event_offset : event_offset + AUDIT_PAGE_SIZE])
    _decorate_audit_events(events)
    context = {
        **navigation_context("audit"),
        "events": events,
        "audit_page": audit_page,
        "audit_has_prev": audit_page > 0,
        "audit_has_next": event_offset + len(events) < event_total,
        "audit_start": event_offset + 1 if event_total else 0,
        "audit_end": event_offset + len(events),
        "audit_total": event_total,
        "audit_filter": module_filter,
        "audit_query": query,
        "audit_cluster": cluster_filter,
        "audit_clusters": ProxmoxCluster.objects.order_by("display_name", "key"),
        "audit_retention_schedule": audit_retention_schedule_state(),
        "audit_filters": AUDIT_MODULE_FILTERS,
        "audit_xlsx_max_rows": AUDIT_XLSX_MAX_ROWS,
    }
    return render(request, "core/audit_log.html", context)


@app_login_required
def audit_export(request):
    module_filter = _audit_module_filter(request.GET.get("filter", "all"))
    query = _audit_query(request.GET.get("q", ""))
    cluster_filter = _audit_cluster_filter(request.GET.get("cluster", ""))
    scope = request.GET.get("scope", "matching")
    export_format = request.GET.get("format", "xlsx").lower()
    include_technical = request.GET.get("include_technical") == "on"
    started_at = _audit_export_datetime(request.GET.get("start", ""), is_end=False)
    ended_at = _audit_export_datetime(request.GET.get("end", ""), is_end=True)

    if export_format not in {"csv", "json", "xlsx"}:
        export_format = "xlsx"

    events_qs = _audit_events_queryset(
        module_filter=module_filter,
        query=query,
        cluster_key=cluster_filter,
        started_at=started_at,
        ended_at=ended_at,
    )
    if scope == "page":
        try:
            audit_page = max(0, int(request.GET.get("page", "0")))
        except ValueError:
            audit_page = 0
        events_qs = events_qs[audit_page * AUDIT_PAGE_SIZE : audit_page * AUDIT_PAGE_SIZE + AUDIT_PAGE_SIZE]

    events_qs = events_qs.order_by("-timestamp")
    columns = AUDIT_EXPORT_COLUMNS + (AUDIT_EXPORT_TECH_COLUMNS if include_technical else [])
    filename = f"pve-helper-audit-{timezone.now().strftime('%Y%m%d-%H%M%S')}.{export_format}"

    if export_format == "csv":
        return _audit_csv_response(columns, events_qs, include_technical, filename)
    if export_format == "json":
        return _audit_json_response(columns, events_qs, include_technical, filename)

    event_count = events_qs.count()
    if event_count > AUDIT_XLSX_MAX_ROWS:
        messages.error(
            request,
            f"Excel export is limited to {AUDIT_XLSX_MAX_ROWS:,} events; narrow the filters or export CSV/JSON instead.",
        )
        return redirect("core:audit_log")
    events = list(events_qs)
    _decorate_audit_events(events)
    rows = [_audit_export_row(event, include_technical=include_technical) for event in events]
    return _audit_xlsx_response(columns, rows, filename)


@require_POST
@app_login_required
def update_audit_retention_schedule_view(request):
    redirect_to = _safe_next_url(request)
    enabled = request.POST.get("enabled") == "on"
    try:
        retention_days = int(request.POST.get("retention_days", "90"))
        state = update_audit_retention_schedule(enabled=enabled, retention_days=retention_days)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_to)

    record_audit_event(
        request,
        action="audit.retention.schedule.updated",
        object_type="audit_retention_schedule",
        object_id="automatic-audit-retention",
        details={
            "enabled": state.enabled,
            "retention_days": state.retention_days,
            "next_run": state.next_run.isoformat() if state.next_run else "",
        },
    )

    return redirect(redirect_to)


def _audit_module_filter(value: str) -> str:
    return value if value in AUDIT_VALID_MODULES else "all"


def _audit_query(value: str) -> str:
    return str(value or "").strip()[:200]


def _audit_cluster_filter(value: str) -> str:
    key = str(value or "").strip().lower()
    return key if key and ProxmoxCluster.objects.filter(key=key).exists() else ""


def _audit_events_queryset(*, module_filter: str, query: str, cluster_key: str = "", started_at=None, ended_at=None):
    events_qs = AuditEvent.objects.select_related("cluster")
    if module_filter != "all":
        events_qs = events_qs.filter(module=module_filter)
    if query:
        events_qs = events_qs.filter(
            Q(username__icontains=query)
            | Q(action__icontains=query)
            | Q(object_id__icontains=query)
            | Q(object_type__icontains=query)
            | Q(source_ip__icontains=query)
            | Q(path__icontains=query)
        )
    if cluster_key:
        events_qs = events_qs.filter(cluster_key_snapshot=cluster_key)
    if started_at:
        events_qs = events_qs.filter(timestamp__gte=started_at)
    if ended_at:
        events_qs = events_qs.filter(timestamp__lte=ended_at)
    return events_qs


def _audit_export_datetime(value: str, *, is_end: bool):
    value = str(value or "").strip()
    if not value:
        return None
    normalized = value.replace(" ", "T", 1) if " " in value and "T" not in value else value
    parsed = parse_datetime(normalized)
    if not parsed:
        parsed_date = parse_date(value)
        if parsed_date:
            parsed = datetime.combine(parsed_date, time.max if is_end else time.min)
    if not parsed:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _audit_export_row(event: AuditEvent, *, include_technical: bool) -> dict[str, str]:
    row = {
        "Time": timezone.localtime(event.timestamp).strftime("%Y-%m-%d %H:%M:%S"),
        "Cluster": event.cluster.display_name if event.cluster_id else event.cluster_key_snapshot or "-",
        "Module": event.display_module,
        "User": event.username or "-",
        "Source IP": str(event.source_ip or "-"),
        "Action": event.display_action,
        "Object": event.guest_identity.full_label if event.guest_identity else event.display_object,
        "Details": event.display_detail or "-",
        "Outcome": event.outcome or "-",
    }
    if include_technical:
        row.update(
            {
                "Raw Action": event.action or "",
                "Object Type": event.object_type or "",
                "Object ID": event.object_id or "",
                "Raw Details": json.dumps(event.details or {}, sort_keys=True, ensure_ascii=False),
            }
        )
    return row


def _audit_export_rows(events_qs, *, include_technical: bool):
    for event in events_qs.iterator(chunk_size=AUDIT_PAGE_SIZE):
        _decorate_audit_events([event])
        yield _audit_export_row(event, include_technical=include_technical)


def _audit_csv_response(columns: list[str], events_qs, include_technical: bool, filename: str) -> StreamingHttpResponse:
    def stream():
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
        for row in _audit_export_rows(events_qs, include_technical=include_technical):
            writer.writerow(row)
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    response = StreamingHttpResponse(stream(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=filename)
    return response


def _audit_json_response(
    columns: list[str], events_qs, include_technical: bool, filename: str
) -> StreamingHttpResponse:
    def stream():
        yield '{"columns":'
        yield json.dumps(columns, ensure_ascii=False)
        yield ',"rows":['
        first = True
        for row in _audit_export_rows(events_qs, include_technical=include_technical):
            if not first:
                yield ","
            yield json.dumps(row, ensure_ascii=False)
            first = False
        yield "]}"

    response = StreamingHttpResponse(stream(), content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=filename)
    return response


def _audit_xlsx_response(columns: list[str], rows: list[dict[str, str]], filename: str) -> HttpResponse:
    workbook = _xlsx_workbook_bytes(
        "Audit Log", [[column for column in columns], *[[row.get(column, "") for column in columns] for row in rows]]
    )
    response = HttpResponse(workbook, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=filename)
    return response


def _xlsx_workbook_bytes(sheet_name: str, rows: list[list[object]]) -> bytes:
    shared_strings: list[str] = []
    shared_string_ids: dict[str, int] = {}
    shared_rows: list[list[int]] = []
    widths: list[int] = []

    for row in rows:
        shared_row = []
        for column_index, value in enumerate(row, start=1):
            text = _xlsx_clean_text(value)
            if len(widths) < column_index:
                widths.append(0)
            widths[column_index - 1] = min(80, max(widths[column_index - 1], len(text) + 2))
            if text not in shared_string_ids:
                shared_string_ids[text] = len(shared_strings)
                shared_strings.append(text)
            shared_row.append(shared_string_ids[text])
        shared_rows.append(shared_row)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xlsx_content_types())
        archive.writestr("_rels/.rels", _xlsx_root_relationships())
        archive.writestr("docProps/app.xml", _xlsx_app_properties(sheet_name))
        archive.writestr("docProps/core.xml", _xlsx_core_properties())
        archive.writestr("xl/workbook.xml", _xlsx_workbook_xml(sheet_name))
        archive.writestr("xl/_rels/workbook.xml.rels", _xlsx_workbook_relationships())
        archive.writestr("xl/styles.xml", _xlsx_styles())
        archive.writestr(
            "xl/sharedStrings.xml", _xlsx_shared_strings(shared_strings, sum(len(row) for row in shared_rows))
        )
        archive.writestr("xl/worksheets/sheet1.xml", _xlsx_worksheet(shared_rows, widths))
    return buffer.getvalue()


def _xlsx_content_types() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""


def _xlsx_root_relationships() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""


def _xlsx_workbook_relationships() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>"""


def _xlsx_app_properties(sheet_name: str) -> str:
    sheet_name = _xlsx_clean_text(sheet_name)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>pve-helper</Application>
  <DocSecurity>0</DocSecurity>
  <ScaleCrop>false</ScaleCrop>
  <HeadingPairs><vt:vector size="2" baseType="variant"><vt:variant><vt:lpstr>Worksheets</vt:lpstr></vt:variant><vt:variant><vt:i4>1</vt:i4></vt:variant></vt:vector></HeadingPairs>
  <TitlesOfParts><vt:vector size="1" baseType="lpstr"><vt:lpstr>{xml_escape(sheet_name)}</vt:lpstr></vt:vector></TitlesOfParts>
  <Company></Company>
  <LinksUpToDate>false</LinksUpToDate>
  <SharedDoc>false</SharedDoc>
  <HyperlinksChanged>false</HyperlinksChanged>
  <AppVersion>16.0000</AppVersion>
</Properties>"""


def _xlsx_core_properties() -> str:
    created = timezone.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>pve-helper</dc:creator>
  <cp:lastModifiedBy>pve-helper</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{xml_escape(created)}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{xml_escape(created)}</dcterms:modified>
</cp:coreProperties>"""


def _xlsx_workbook_xml(sheet_name: str) -> str:
    sheet_name = _xlsx_clean_text(sheet_name)[:31] or "Sheet1"
    escaped_sheet_name = xml_escape(sheet_name, {'"': "&quot;"})
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <workbookPr/>
  <bookViews><workbookView xWindow="0" yWindow="0" windowWidth="24000" windowHeight="12000"/></bookViews>
  <sheets><sheet name="{escaped_sheet_name}" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""


def _xlsx_styles() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/></cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _xlsx_shared_strings(shared_strings: list[str], total_count: int) -> str:
    items = "".join(f"<si><t>{xml_escape(text)}</t></si>" for text in shared_strings)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{total_count}" uniqueCount="{len(shared_strings)}">
  {items}
</sst>"""


def _xlsx_worksheet(rows: list[list[int]], widths: list[int]) -> str:
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, shared_string_id in enumerate(row, start=1):
            style = ' s="1"' if row_index == 1 else ""
            cells.append(f'<c r="{_xlsx_cell_ref(row_index, column_index)}" t="s"{style}><v>{shared_string_id}</v></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    cols = "".join(
        f'<col min="{idx}" max="{idx}" width="{max(10, width)}" customWidth="1"/>'
        for idx, width in enumerate(widths, start=1)
    )
    dimension = f"A1:{_xlsx_cell_ref(max(1, len(rows)), max(1, len(widths)))}"
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="{dimension}"/>
  <sheetViews><sheetView workbookViewId="0"/></sheetViews>
  <sheetFormatPr defaultRowHeight="15"/>
  <cols>{cols}</cols>
  <sheetData>{"".join(sheet_rows)}</sheetData>
</worksheet>"""


def _xlsx_clean_text(value: object) -> str:
    text = str(value if value is not None else "")
    return "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 0x20)


def _xlsx_cell_ref(row: int, column: int) -> str:
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row}"
