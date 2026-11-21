"""강의 질의응답 챗봇: 사용자별로 강의자료를 올리면 pgvector에 저장하고, 질문하면 자기 자료에서 찾아 답한다."""
import os
import re
from pathlib import Path

import psycopg
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from openai import OpenAI
from pydantic import BaseModel, Field

from extractors import ExtractError, check_format, chunk_text, extract

try:
    for line in open(".env", encoding="utf-8"):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v)
except FileNotFoundError:
    pass

client = OpenAI()
CHAT = os.getenv("AX_CHAT_MODEL", "ax-local")
EMBED = os.getenv("AX_EMBED_MODEL", "ax-embed")
DIM = int(os.getenv("EMBED_DIM", "1024"))      # bge-m3 = 1024
DB = os.environ["DATABASE_URL"]
# 기본 "생각(thinking)" 모드는 느리다. 일반 답변은 끄고, 필요하면 AX_THINKING=true
EXTRA = {"chat_template_kwargs": {"enable_thinking": os.getenv("AX_THINKING", "false").lower() == "true"}}
MAX_BYTES = int(os.getenv("MAX_UPLOAD_MB", "30")) * 1024 * 1024
TOP_K = int(os.getenv("TOP_K", "5"))
DEV_USER = os.getenv("DEV_USER_EMAIL", "")      # 로컬 개발 때만 설정. 배포 환경에는 넣지 말 것.
STATIC = Path(__file__).parent / "static"

app = FastAPI()


def get_user(request: Request) -> str:
    """AX 플랫폼이 붙여 주는 로그인 사용자 이메일. 플랫폼이 덮어쓰므로 신뢰할 수 있다."""
    email = request.headers.get("x-auth-request-email", "").strip().lower() or DEV_USER
    if not email:
        raise HTTPException(401, "로그인이 필요합니다. 프로젝트 공개 범위를 '교내 로그인 사용자 누구나'로 설정하세요.")
    return email


def vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def embed(texts: list[str]) -> list[list[float]]:
    r = client.embeddings.create(model=EMBED, input=texts)
    return [d.embedding for d in r.data]


@app.on_event("startup")
def init():
    with psycopg.connect(DB, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        c.execute("""CREATE TABLE IF NOT EXISTS lc_documents(
            id bigserial PRIMARY KEY,
            owner_email text NOT NULL,
            filename text NOT NULL,
            status text NOT NULL DEFAULT 'processing',   -- processing | ready | failed
            error text,
            n_chunks int NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now())""")
        c.execute(f"""CREATE TABLE IF NOT EXISTS lc_chunks(
            id bigserial PRIMARY KEY,
            doc_id bigint NOT NULL REFERENCES lc_documents(id) ON DELETE CASCADE,
            owner_email text NOT NULL,
            location text,
            content text NOT NULL,
            embedding vector({DIM}) NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS lc_documents_owner ON lc_documents(owner_email)")
        c.execute("CREATE INDEX IF NOT EXISTS lc_chunks_owner ON lc_chunks(owner_email, doc_id)")
        # 재시작으로 끊긴 처리 작업은 실패로 표시한다.
        c.execute("UPDATE lc_documents SET status='failed', error='처리 중 서버가 재시작되었습니다. 다시 올려 주세요.' "
                  "WHERE status='processing'")


def index_document(doc_id: int, owner: str, filename: str, data: bytes) -> None:
    """업로드 응답 뒤 백그라운드에서 추출 → 청킹 → 임베딩 → 저장."""
    try:
        rows = [(loc, piece) for text, loc in extract(filename, data) for piece in chunk_text(text)]
        with psycopg.connect(DB) as c:
            for i in range(0, len(rows), 32):
                batch = rows[i:i + 32]
                # 파일명과 위치를 붙여 임베딩하면 "3주차 자료에서..." 같은 질문 검색이 나아진다.
                vecs = embed([f"{filename} {loc or ''}\n{piece}" for loc, piece in batch])
                with c.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO lc_chunks(doc_id, owner_email, location, content, embedding) "
                        "VALUES (%s, %s, %s, %s, %s::vector)",
                        [(doc_id, owner, loc, piece, vec(e)) for (loc, piece), e in zip(batch, vecs)])
            c.execute("UPDATE lc_documents SET status='ready', n_chunks=%s WHERE id=%s", (len(rows), doc_id))
    except Exception as e:
        msg = str(e) if isinstance(e, ExtractError) else f"처리 중 오류가 났습니다: {type(e).__name__}: {e}"[:500]
        with psycopg.connect(DB) as c:
            c.execute("DELETE FROM lc_chunks WHERE doc_id=%s", (doc_id,))
            c.execute("UPDATE lc_documents SET status='failed', error=%s WHERE id=%s", (msg, doc_id))


@app.get("/api/me")
def me(user: str = Depends(get_user)):
    return {"email": user}


@app.post("/api/upload")
async def upload(file: UploadFile, background: BackgroundTasks, user: str = Depends(get_user)):
    filename = Path(file.filename or "noname").name
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"파일이 너무 큽니다. {MAX_BYTES // 1024 // 1024}MB 이하만 올릴 수 있습니다.")
    if not data:
        raise HTTPException(400, "빈 파일입니다.")
    try:  # 형식만 먼저 확인해서 지원하지 않는 파일은 바로 거절한다.
        check_format(filename)
    except ExtractError as e:
        raise HTTPException(400, str(e))
    with psycopg.connect(DB) as c:
        doc_id = c.execute("INSERT INTO lc_documents(owner_email, filename) VALUES (%s, %s) RETURNING id",
                           (user, filename)).fetchone()[0]
    background.add_task(index_document, doc_id, user, filename, data)
    return {"id": doc_id, "filename": filename, "status": "processing"}


@app.get("/api/documents")
def documents(user: str = Depends(get_user)):
    with psycopg.connect(DB) as c:
        rows = c.execute("SELECT id, filename, status, error, n_chunks, created_at FROM lc_documents "
                         "WHERE owner_email=%s ORDER BY created_at DESC", (user,)).fetchall()
    return [{"id": i, "filename": f, "status": s, "error": e, "chunks": n, "created_at": t.isoformat()}
            for i, f, s, e, n, t in rows]


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int, user: str = Depends(get_user)):
    with psycopg.connect(DB) as c:
        n = c.execute("DELETE FROM lc_documents WHERE id=%s AND owner_email=%s", (doc_id, user)).rowcount
    if not n:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    return {"deleted": doc_id}


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=4000)


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default_factory=list)
    doc_ids: list[int] | None = None


SYSTEM = """당신은 대학 강의의 질의응답 조교입니다.
- 반드시 아래 [자료]에 있는 내용만 근거로 한국어로 답하세요.
- 자료에 답이 없으면 지어내지 말고 "올리신 강의자료에서는 찾을 수 없습니다"라고 답하세요.
- 근거로 쓴 자료 번호를 문장 끝에 [1], [2]처럼 표시하세요.
- 학생이 이해하기 쉽게 핵심부터 설명하고, 필요하면 예시나 단계로 정리하세요."""


@app.post("/api/ask")
def ask(a: Ask, user: str = Depends(get_user)):
    history = a.history[-8:]  # 최근 4턴(질문+답변)
    # "그거 더 설명해 줘" 같은 후속 질문도 찾을 수 있게 직전 질문을 함께 임베딩한다.
    prev = next((t.content for t in reversed(history) if t.role == "user"), "")
    q = embed([f"{prev}\n{a.question}" if prev else a.question])[0]

    sql = ("SELECT d.filename, c.location, c.content, 1 - (c.embedding <=> %(q)s::vector) AS score "
           "FROM lc_chunks c JOIN lc_documents d ON d.id = c.doc_id "
           "WHERE c.owner_email = %(u)s AND d.status = 'ready' ")
    params = {"q": vec(q), "u": user, "k": TOP_K}
    if a.doc_ids:
        sql += "AND c.doc_id = ANY(%(ids)s) "
        params["ids"] = a.doc_ids
    sql += "ORDER BY c.embedding <=> %(q)s::vector LIMIT %(k)s"
    with psycopg.connect(DB) as c:
        rows = c.execute(sql, params).fetchall()

    if not rows:
        return {"answer": "검색할 강의자료가 없습니다. 왼쪽에서 강의자료를 먼저 올려 주세요.", "sources": []}

    sources = [{"n": i, "filename": f, "location": loc, "score": round(float(s), 3), "snippet": ch[:300]}
               for i, (f, loc, ch, s) in enumerate(rows, 1)]
    context = "\n\n".join(f"[{i}] ({f}{' ' + loc if loc else ''})\n{ch}"
                          for i, (f, loc, ch, _) in enumerate(rows, 1))
    msgs = [{"role": "system", "content": SYSTEM},
            *[{"role": t.role, "content": t.content} for t in history],
            {"role": "user", "content": f"[자료]\n{context}\n\n[질문]\n{a.question}"}]
    r = client.chat.completions.create(model=CHAT, messages=msgs, temperature=0.2, extra_body=EXTRA)
    answer = r.choices[0].message.content or ""
    # 답변에 실제로 인용된 자료만 출처로 보여 준다. "찾을 수 없습니다" 답변에는 출처가 붙지 않는다.
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    return {"answer": answer, "sources": [s for s in sources if s["n"] in cited]}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
