# 강의 Q&A 챗봇 (AX Playground, RAG)

강의자료를 올리면 사용자별로 pgvector에 저장하고, 질문하면 **내가 올린 자료에서만** 찾아 출처와 함께 답합니다.
AX RAG 스타터(FastAPI + pgvector)를 확장했습니다.

## 기능
- 업로드 형식: **PDF, PPTX(발표자 노트 포함), DOCX, HWPX, MD, TXT** (최대 30MB). HWP/PPT/DOC 구버전과 스캔 PDF(OCR)는 지원하지 않습니다.
- 사용자별 분리: AX가 붙여 주는 `X-Auth-Request-Email` 헤더로 문서, 검색, 삭제를 나눕니다.
- 출처 표시: 페이지(PDF), 슬라이드(PPTX), 소제목(DOCX/MD) 단위로 표시하며, 답변에 실제로 인용된 자료만 보여 줍니다.
- 후속 질문: 최근 4턴의 대화를 함께 보냅니다.

## 구성
| 파일 | 역할 |
|---|---|
| `app.py` | API (`/api/upload`, `/api/documents`, `DELETE /api/documents/{id}`, `/api/ask`, `/api/me`), DB 스키마 |
| `extractors.py` | 형식별 텍스트 추출, 청킹(800자, 150자 겹침) |
| `static/index.html` | 화면 (왼쪽: 자료 업로드와 목록, 오른쪽: 채팅) |

DB 테이블: `lc_documents`(문서, 처리 상태), `lc_chunks`(조각, 위치, 임베딩 1024차원). 문서를 지우면 조각도 함께 지워집니다.

## 배포 (AX Playground)
- `main`에 push하면 자동으로 배포됩니다.
- **프로젝트 공개 범위를 "교내 로그인 사용자 누구나"로 설정**해야 합니다. "누구나"로 두면 로그인 헤더가 오지 않아 401이 납니다.
- 배포 환경에는 `DEV_USER_EMAIL`을 넣지 마세요.

## 로컬 개발
```bash
docker run -d -p 127.0.0.1:5432:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16
cp .env.example .env   # AX 프로젝트 화면의 값으로 채우기
python -m venv .venv && .venv/bin/pip install -r requirements.txt
DEV_USER_EMAIL=me@test .venv/bin/uvicorn app:app --reload
```
로컬에서는 로그인 헤더가 없으므로 `DEV_USER_EMAIL`을 사용자로 씁니다. 다른 사용자로 시험하려면 요청에 `X-Auth-Request-Email` 헤더를 직접 붙이세요.

## 선택 환경변수
`MAX_UPLOAD_MB`(기본 30), `TOP_K`(검색 조각 수, 기본 5), `AX_THINKING`(true면 생각 모드, 느림)
