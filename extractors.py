"""강의자료 파일에서 텍스트를 뽑는다.

모든 추출기는 [(텍스트, 위치)] 목록을 돌려준다. 위치는 "p.3", "슬라이드 5", 소제목 같은
출처 표시용 문자열이며, 알 수 없으면 None이다.
"""
import io
import re
import zipfile
from xml.etree import ElementTree as ET

Unit = tuple[str, str | None]

SUPPORTED = {".pdf", ".pptx", ".docx", ".hwpx", ".md", ".markdown", ".txt"}


class ExtractError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 추출 오류."""


def check_format(filename: str) -> str:
    """확장자를 확인해 돌려준다. 지원하지 않으면 안내 문구와 함께 ExtractError."""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == ".hwp":
        raise ExtractError("HWP(구 한글) 형식은 지원하지 않습니다. 한글에서 HWPX나 PDF로 저장해 올려 주세요.")
    if ext in (".ppt", ".doc"):
        raise ExtractError(f"{ext} 구버전 형식은 지원하지 않습니다. {ext}x 또는 PDF로 저장해 올려 주세요.")
    if ext not in SUPPORTED:
        raise ExtractError(f"지원하지 않는 형식입니다({ext or '확장자 없음'}). PDF, PPTX, DOCX, HWPX, MD, TXT만 올릴 수 있습니다.")
    return ext


def extract(filename: str, data: bytes) -> list[Unit]:
    ext = check_format(filename)
    try:
        if ext == ".pdf":
            units = _pdf(data)
        elif ext == ".pptx":
            units = _pptx(data)
        elif ext == ".docx":
            units = _docx(data)
        elif ext == ".hwpx":
            units = _hwpx(data)
        elif ext in (".md", ".markdown"):
            units = _markdown(_decode(data))
        else:
            units = [(_decode(data), None)]
    except ExtractError:
        raise
    except Exception as e:  # 손상된 파일 등
        raise ExtractError(f"파일을 읽지 못했습니다. 손상되었거나 암호가 걸린 파일인지 확인해 주세요. ({type(e).__name__})") from e

    units = [(_clean(t), loc) for t, loc in units]
    units = [(t, loc) for t, loc in units if t]
    if not units:
        if ext == ".pdf":
            raise ExtractError("PDF에서 텍스트를 찾지 못했습니다. 스캔한 이미지 PDF는 지원하지 않습니다(OCR 미지원).")
        raise ExtractError("파일에서 텍스트를 찾지 못했습니다.")
    return units


def _clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf(data: bytes) -> list[Unit]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise ExtractError("암호가 걸린 PDF는 읽을 수 없습니다.")
    return [(page.extract_text() or "", f"p.{i}") for i, page in enumerate(reader.pages, 1)]


def _pptx(data: bytes) -> list[Unit]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    def shape_text(shape) -> list[str]:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            return [t for s in shape.shapes for t in shape_text(s)]
        out = []
        if shape.has_text_frame:
            out.append(shape.text_frame.text)
        if getattr(shape, "has_table", False) and shape.has_table:
            for row in shape.table.rows:
                out.append(" | ".join(cell.text for cell in row.cells))
        return out

    units = []
    for i, slide in enumerate(Presentation(io.BytesIO(data)).slides, 1):
        parts = [t for shape in slide.shapes for t in shape_text(shape)]
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text if slide.notes_slide.notes_text_frame else ""
            if notes.strip():
                parts.append("[발표자 노트] " + notes)
        units.append(("\n".join(parts), f"슬라이드 {i}"))
    return units


def _docx(data: bytes) -> list[Unit]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    units: list[Unit] = []
    heading, buf = None, []

    def flush():
        if buf:
            units.append(("\n".join(buf), heading))
            buf.clear()

    for block in doc.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(block, doc)
            style = (p.style.name if p.style is not None else "") or ""
            if p.text.strip() and (style.startswith("Heading") or style.startswith("제목") or style == "Title"):
                flush()
                heading = p.text.strip()[:60]
            buf.append(p.text)
        elif tag == "tbl":
            for row in Table(block, doc).rows:
                cells = []
                for cell in row.cells:  # 병합 셀은 같은 셀이 반복되므로 제거
                    if not cells or cells[-1] != cell.text:
                        cells.append(cell.text)
                buf.append(" | ".join(cells))
    flush()
    return units


def _hwpx(data: bytes) -> list[Unit]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        sections = [n for n in z.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)]
        if not sections:
            raise ExtractError("HWPX 본문(Contents/section*.xml)을 찾지 못했습니다.")
        sections.sort(key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[-1]).group(1)))
        many = len(sections) > 1
        units = []
        for idx, name in enumerate(sections, 1):
            root = ET.fromstring(z.read(name))
            lines = []
            # 표 안의 문단도 별도 <hp:p>로 들어 있으므로 모든 문단을 순서대로 돈다.
            # 각 문단에서는 자기 run/t만 읽어 중첩 문단 텍스트가 두 번 들어가지 않게 한다.
            for p in root.iter():
                if not p.tag.endswith("}p"):
                    continue
                text = "".join(
                    "".join(t.itertext())
                    for run in p if run.tag.endswith("}run")
                    for t in run if t.tag.endswith("}t")
                )
                if text.strip():
                    lines.append(text)
            units.append(("\n".join(lines), f"구역 {idx}" if many else None))
        return units


def _markdown(text: str) -> list[Unit]:
    units: list[Unit] = []
    heading, buf = None, []
    in_code = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"#{1,6}\s+(.+)", line)
        if m:
            if "".join(buf).strip():
                units.append(("\n".join(buf), heading))
            buf = []
            heading = m.group(1).strip()[:60]
        buf.append(line)
    if "".join(buf).strip():
        units.append(("\n".join(buf), heading))
    return units


def chunk_text(text: str, size: int = 800, overlap: int = 150) -> list[str]:
    """문단 경계를 우선해 size자 안팎으로 자르고, 앞 조각 끝 overlap자를 다음 조각에 겹친다."""
    pieces: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
            continue
        # 긴 문단은 문장 단위로 쪼갠 뒤, 그래도 긴 문장은 글자 수로 자른다.
        for sent in re.split(r"(?<=[.!?。])\s+|\n", para):
            while len(sent) > size:
                pieces.append(sent[:size])
                sent = sent[size - overlap:]
            if sent.strip():
                pieces.append(sent)

    out: list[str] = []
    cur = ""
    for piece in pieces:
        if cur and len(cur) + len(piece) + 1 > size:
            out.append(cur)
            tail = cur[-overlap:]
            # 겹치는 부분이 단어 중간에서 시작하지 않도록 첫 공백 뒤부터 쓴다.
            cut = tail.find(" ")
            tail = tail[cut + 1:] if 0 <= cut < len(tail) - 1 else tail
            cur = tail + "\n" + piece
        else:
            cur = f"{cur}\n{piece}" if cur else piece
    if cur.strip():
        out.append(cur)
    return out
