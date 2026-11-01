"""Five-route review API. Decisions append; no graph or financial actions run."""
from datetime import datetime, timezone
from hmac import compare_digest
import os
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from mangum import Mangum
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
import psycopg
from psycopg.rows import dict_row

from ledgerguard.audit import app_connection
from ledgerguard.db import runtime_database_url
from ledgerguard.investigator import Note
from ledgerguard.models import Citation, FindingStatus, FindingType, SourceRef

Money = Annotated[str, Field(pattern=r'^\d+\.\d{2}$')]
UnitPrice = Annotated[str, Field(pattern=r'^\d+\.\d{4}$')]
Decision = Literal['approve', 'reject']


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    decision: Decision
    note: str = Field(strict=True, min_length=1, max_length=1000)


class FindingOutput(BaseModel):
    finding_id: int
    run_id: str
    position: int
    type: FindingType
    invoice_numbers: list[str]
    amount_at_issue: Money
    calculation: str
    evidence: list[SourceRef]
    citation: Citation | None
    no_clause_reason: Literal['no applicable clause identified'] | None
    confidence: float = Field(ge=0, le=1)
    status: FindingStatus
    notes: list[Note]


class FindingCounts(BaseModel):
    pending: int
    approved: int
    rejected: int


class ModelIds(BaseModel):
    extract: str
    policy: str
    investigate: str


class RunOutput(BaseModel):
    run_id: str
    started_at: str
    finished_at: str
    status: Literal['completed', 'needs_review', 'stopped', 'failed']
    stopped_reason: str | None
    skipped_stages: list[str]
    model_ids: ModelIds
    findings: FindingCounts


class RunsOutput(BaseModel):
    runs: list[RunOutput]


class FindingsOutput(BaseModel):
    run_id: str
    findings: list[FindingOutput]


class LineOutput(BaseModel):
    position: int
    description: str
    quantity: str
    unit_price: UnitPrice
    amount: Money
    source: SourceRef


class InvoiceOutput(BaseModel):
    document_id: str
    invoice_number: str
    vendor_name: str
    po_number: str | None
    invoice_date: str
    due_date: str
    currency: str
    subtotal: Money
    tax: Money
    total: Money
    line_items: list[LineOutput]


class DecisionOutput(BaseModel):
    decision_id: int
    decision: Decision
    reviewer: str
    note: str
    time: str


class FindingDetail(BaseModel):
    finding: FindingOutput
    invoices: list[InvoiceOutput]
    decisions: list[DecisionOutput]


class DecisionCreated(DecisionOutput):
    finding_id: int
    status: FindingStatus


def utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def database():
    url = runtime_database_url()
    with app_connection(url) as conn:
        yield conn


FINDINGS_SQL = '''SELECT f.*, review.status, n.finding_index, n.summary,
    n.related_invoices, n.uncertainty FROM findings f
    JOIN finding_review_status review USING (finding_id)
    LEFT JOIN notes n USING (finding_id)'''


def finding_output(row):
    data = {name: row[name] for name in FindingOutput.model_fields if name != 'notes'}
    data['amount_at_issue'] = format(row['amount_at_issue'], '.2f')
    data['notes'] = [] if row['summary'] is None else [
        {name: row[name] for name in Note.model_fields}]
    return data


def create_app() -> FastAPI:
    mode = os.environ.get('LEDGERGUARD_ENV', 'local')
    if mode not in {'local', 'aws'}:
        raise RuntimeError('LEDGERGUARD_ENV must be local or aws')
    if mode == 'aws' and any(name in os.environ for name in ('REVIEW_REVIEWER', 'REVIEW_API_TOKEN')):
        raise RuntimeError('Local review settings are forbidden in AWS mode')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.middleware('http')
    async def authenticate(request, call_next):
        # Authenticate before body parsing/validation, including malformed JSON.
        if request.url.path != '/health':
            if mode == 'aws':
                # Only API Gateway's verified claims, carried by Mangum's event.
                # Client headers/body can never create this ASGI scope field.
                try:
                    claims = request.scope['aws.event']['requestContext']['authorizer']['jwt']['claims']
                    reviewer = claims['email']
                    valid = (isinstance(reviewer, str) and bool(reviewer.strip())
                             and claims.get('email_verified') in (True, 'true')
                             and claims.get('token_use') == 'id')
                except (KeyError, TypeError):
                    valid = False
                if valid:
                    request.state.reviewer = reviewer.strip()
            else:
                expected = os.environ.get('REVIEW_API_TOKEN')
                parts = request.headers.get('authorization', '').split()
                valid = (expected and len(parts) == 2 and parts[0].lower() == 'bearer'
                         and compare_digest(parts[1].encode('utf-8'), expected.encode('utf-8')))
            if not valid:
                return JSONResponse(status_code=401, content={'detail': 'Not authenticated'},
                                    headers={'WWW-Authenticate': 'Bearer'})
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse(status_code=422, content={'detail': [
            {'loc': list(item['loc']), 'msg': item['msg']} for item in error.errors()]})

    async def database_error(request, error):
        return JSONResponse(status_code=503, content={'detail': 'Service unavailable'})

    app.add_exception_handler(psycopg.Error, database_error)
    app.add_exception_handler(OSError, database_error)

    @app.exception_handler(404)
    async def not_found(request, error):
        return JSONResponse(status_code=404, content={'detail': 'Not found'})

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.get('/runs', response_model=RunsOutput)
    def runs(conn=Depends(database)):
        with conn.cursor(row_factory=dict_row) as cursor:
            rows = cursor.execute('''SELECT r.run_id, r.started_at, r.finished_at, r.status,
                r.stopped_reason, r.skipped_stages, r.model_ids,
                count(*) FILTER (WHERE s.status='pending') AS pending,
                count(*) FILTER (WHERE s.status='approved') AS approved,
                count(*) FILTER (WHERE s.status='rejected') AS rejected
                FROM runs r LEFT JOIN findings f USING (run_id)
                LEFT JOIN finding_review_status s USING (finding_id)
                GROUP BY r.run_id ORDER BY r.started_at DESC, r.run_id''').fetchall()
        return {'runs': [{**{key: row[key] for key in RunOutput.model_fields if key != 'findings'},
                          'started_at': utc(row['started_at']), 'finished_at': utc(row['finished_at']),
                          'findings': {status: row[status] for status in FindingCounts.model_fields}}
                         for row in rows]}

    @app.get('/runs/{run_id}/findings', response_model=FindingsOutput)
    def findings(run_id: str, status: FindingStatus | None = None, conn=Depends(database)):
        with conn.cursor(row_factory=dict_row) as cursor:
            if cursor.execute('SELECT 1 FROM runs WHERE run_id=%s', (run_id,)).fetchone() is None:
                raise HTTPException(404)
            query = FINDINGS_SQL + ' WHERE f.run_id=%s'
            params = [run_id]
            if status is not None:
                query += ' AND review.status=%s'
                params.append(status)
            rows = cursor.execute(query + ' ORDER BY f.position', params).fetchall()
        return {'run_id': run_id, 'findings': [finding_output(row) for row in rows]}

    @app.get('/findings/{finding_id}', response_model=FindingDetail)
    def finding(finding_id: int, conn=Depends(database)):
        if not -(2 ** 63) <= finding_id < 2 ** 63:
            raise HTTPException(404)
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
            # All parts of a detail response share one read snapshot.
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            row = cursor.execute(FINDINGS_SQL + ' WHERE f.finding_id=%s', (finding_id,)).fetchone()
            if row is None:
                raise HTTPException(404)
            invoices = cursor.execute('''SELECT * FROM invoices WHERE run_id=%s
                AND invoice_number=ANY(%s) ORDER BY invoice_number''',
                (row['run_id'], row['invoice_numbers'])).fetchall()
            invoice_outputs = []
            for invoice in invoices:
                items = cursor.execute('SELECT * FROM line_items WHERE invoice_id=%s ORDER BY position',
                                       (invoice['invoice_id'],)).fetchall()
                data = {name: invoice[name] for name in InvoiceOutput.model_fields if name != 'line_items'}
                for name in ('subtotal', 'tax', 'total'):
                    data[name] = format(invoice[name], '.2f')
                for name in ('invoice_date', 'due_date'):
                    data[name] = invoice[name].isoformat()
                data['line_items'] = [{
                    'position': item['position'], 'description': item['description'],
                    'quantity': format(item['quantity'], 'f'), 'unit_price': format(item['unit_price'], '.4f'),
                    'amount': format(item['amount'], '.2f'),
                    'source': {'page': item['source_page'], 'text': item['source_text']}} for item in items]
                invoice_outputs.append(data)
            decisions = cursor.execute('''SELECT decision_id, decision, reviewer, note, time
                FROM decisions WHERE finding_id=%s ORDER BY decision_id''', (finding_id,)).fetchall()
        return {'finding': finding_output(row), 'invoices': invoice_outputs,
                'decisions': [{**decision, 'time': utc(decision['time'])} for decision in decisions]}

    @app.post('/findings/{finding_id}/decisions', status_code=201, response_model=DecisionCreated)
    def decide(finding_id: int, body: DecisionInput, request: Request, conn=Depends(database)):
        if not -(2 ** 63) <= finding_id < 2 ** 63:
            raise HTTPException(404)
        reviewer = request.state.reviewer if mode == 'aws' else os.environ.get('REVIEW_REVIEWER', '').strip()
        if not reviewer:
            raise OSError('Reviewer identity is not configured')
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
            # Serialize API decisions for this finding without UPDATE privilege
            # on findings. The append-only decisions table remains the authority.
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('ledgerguard:decision:' || %s::text, 0))",
                           (finding_id,))
            row = cursor.execute('''INSERT INTO decisions (finding_id, reviewer, decision, note)
                SELECT finding_id, %s, %s, %s FROM findings WHERE finding_id=%s
                RETURNING decision_id, finding_id, decision, reviewer, note, time''',
                (reviewer, body.decision, body.note, finding_id)).fetchone()
            if row is None:
                raise HTTPException(404)
            status = cursor.execute('SELECT status FROM finding_review_status WHERE finding_id=%s',
                                    (finding_id,)).fetchone()['status']
        # Commit completes before a successful response is constructed.
        return {**row, 'time': utc(row['time']), 'status': status}

    return app


app = create_app()


handler = Mangum(app, lifespan='off')
