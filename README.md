# AX RAG 스타터 (문서 Q&A)

- 포털이 넣어 주는 전용 PostgreSQL(pgvector) `DATABASE_URL`에 문서를 저장합니다.
- 임베딩은 `ax-embed`(bge-m3, 1024차원), 답변은 `ax-local`을 씁니다.
- 로컬 개발: `docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16` 후
  `.env` 작성 → `pip install -r requirements.txt && uvicorn app:app --reload`
- 공용 데이터셋 검색: 관리자가 프로젝트에 공용 데이터셋을 연결하면 `SHARED_DATABASE_URL`(읽기 전용)이 들어온다.
  AI 챌린지 데이터는 `ai_challenge.syllabus_chunk`(같은 ax-embed 1024차원)에 강의계획서 조각이 미리 들어 있으니,
  `ask()`의 검색 질의를 `psycopg.connect(os.environ["SHARED_DATABASE_URL"])` + `SELECT curi_nm, chunk FROM ai_challenge.syllabus_chunk ORDER BY embedding <=> %s::vector LIMIT 4`로 바꾸면 된다.
  표 설명은 저장소 `portal/shared-data/ai-challenge/README.md`.
