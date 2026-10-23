"""Word manuscript (.docx) in standard manuscript format, for agents, editors and publishers.

Times New Roman 12 pt, double-spaced, 1-inch margins, half-inch paragraph indents. The
first page carries the author's name and an approximate word count, with the title and
byline halfway down. Each chapter starts on a new page a third of the way down, and
every page after the first is headed "Surname / TITLE / page". Italics stay italic,
scene breaks are a centred "#", footnotes are real Word footnotes, pictures are
embedded, and the manuscript closes with "END".

Written with the standard library: a .docx is a zip of XML parts.
"""

from __future__ import annotations

import re
import struct
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from ..project import Project, slugify
from .render import render_chapters

TWIPS_PER_INCH = 1440
EMU_PER_INCH = 914400
TEXT_WIDTH_IN = 6.5  # 8.5in page minus 1in margins
MAX_PICTURE_HEIGHT_IN = 7.5

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NAMESPACES = (
    f'xmlns:w="{W_NS}" xmlns:r="{R_NS}" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)
IMAGE_CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}


def image_size(path: Path) -> tuple[int, int] | None:
    """(width, height) in pixels from a JPEG, PNG or GIF header."""
    with path.open("rb") as f:
        head = f.read(32)
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return struct.unpack(">II", head[16:24])
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", head[6:10])
        if head.startswith(b"\xff\xd8"):
            f.seek(2)
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                if marker[1] in (0xD8, 0x01) or 0xD0 <= marker[1] <= 0xD7:
                    continue
                size = struct.unpack(">H", f.read(2))[0]
                if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", f.read(5)[1:5])
                    return w, h
                f.seek(size - 2, 1)
    return None


def approx_words(n: int) -> str:
    """Manuscript convention: rounded, never exact."""
    if n < 1000:
        return f"about {max(100, round(n, -2)):,} words"
    if n < 10000:
        return f"about {round(n, -2):,} words"
    return f"about {round(n, -3):,} words"


def _t(text: str) -> str:
    return f'<w:t xml:space="preserve">{escape(text)}</w:t>'


@dataclass
class _Doc:
    body: list[str] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)
    media: list[tuple[str, Path]] = field(default_factory=list)  # (rel id, picture)
    _ids: int = 0

    def next_id(self) -> int:
        self._ids += 1
        return self._ids


class _Writer:
    """Turns a chapter's XHTML (from render.py) into WordprocessingML paragraphs."""

    def __init__(self, doc: _Doc, notes: dict[str, ET.Element]):
        self.doc = doc
        self.notes = notes  # footnote id ("fn:1") → its <li>
        self.note_numbers: dict[str, int] = {}

    def runs(self, el: ET.Element, italic: bool = False, bold: bool = False) -> str:
        out = []

        def run(text: str, i: bool, b: bool) -> None:
            text = text.replace("\n", " ")  # a line break in Markdown is just a wrapped line
            if not text:
                return
            props = ("<w:i/>" if i else "") + ("<w:b/>" if b else "")
            out.append(f"<w:r>{f'<w:rPr>{props}</w:rPr>' if props else ''}{_t(text)}</w:r>")

        run(el.text or "", italic, bold)
        for child in el:
            tag = child.tag
            if tag == "sup" and (a := child.find("a")) is not None and a.get("class") == "footnote-ref":
                out.append(self.footnote(a.get("href", "").lstrip("#")))
            elif tag == "br":
                out.append("<w:r><w:br/></w:r>")
            elif tag == "img":
                out.append(self.picture(child))
            elif tag != "a" or child.get("class") != "footnote-backref":
                out.append(self.runs(child, italic or tag in ("em", "i"), bold or tag in ("strong", "b")))
            run(child.tail or "", italic, bold)
        return "".join(out)

    def footnote(self, ref: str) -> str:
        li = self.notes.get(ref)
        if li is None:
            return ""
        num = self.doc.next_id() + 10  # Word reserves the first footnote ids
        paras = [p for p in li if p.tag == "p"] or [li]
        body = "".join(
            '<w:p><w:pPr><w:pStyle w:val="FootnoteText"/></w:pPr>'
            + ('<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteRef/></w:r><w:r>' + _t(" ") + "</w:r>"
               if i == 0 else "")
            + self.runs(p) + "</w:p>"
            for i, p in enumerate(paras)
        )
        self.doc.footnotes.append(f'<w:footnote w:id="{num}">{body}</w:footnote>')
        return f'<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteReference w:id="{num}"/></w:r>'

    def picture(self, img: ET.Element) -> str:
        path = Path(img.get("data-path", ""))
        size = image_size(path) if path.is_file() else None
        if not size:
            return ""
        w_px, h_px = size
        width = min(TEXT_WIDTH_IN, w_px / 150)  # 150 dpi on paper, never wider than the text
        height = width * h_px / w_px
        if height > MAX_PICTURE_HEIGHT_IN:
            width, height = width * MAX_PICTURE_HEIGHT_IN / height, MAX_PICTURE_HEIGHT_IN
        n = self.doc.next_id()
        rid = f"rIdImg{n}"
        self.doc.media.append((rid, path))
        cx, cy = int(width * EMU_PER_INCH), int(height * EMU_PER_INCH)
        alt = escape(img.get("alt", ""), {'"': "&quot;"})
        return (
            f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
            f'<wp:docPr id="{n}" name="Picture {n}" descr="{alt}"/>'
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{n}" name="{escape(path.name)}"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
            "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
        )

    def blocks(self, root: ET.Element) -> list[str]:
        out: list[str] = []
        first = True  # the paragraph after a heading or scene break isn't indented

        def para(content: str, style: str = "", first_line: bool = False) -> None:
            props = f'<w:pStyle w:val="{style}"/>' if style else ""
            if first_line:
                props += '<w:ind w:firstLine="0"/>'
            out.append(f"<w:p>{f'<w:pPr>{props}</w:pPr>' if props else ''}{content}</w:p>")

        for el in root:
            tag, cls = el.tag, el.get("class", "")
            if tag == "div" and "footnote" in cls:
                continue  # placed as Word footnotes where they're referenced
            if tag == "p" and "scene-break" in cls:
                para(f"<w:r>{_t('#')}</w:r>", "SceneBreak")
                first = True
            elif tag == "p":
                para(self.runs(el), first_line=first)
                first = False
            elif tag in ("h2", "h3", "h4", "h5", "h6"):
                para(self.runs(el), "Subheading")
                first = True
            elif tag == "blockquote":
                for p in el.iter("p"):
                    para(self.runs(p), "Quote")
                first = True
            elif tag in ("ul", "ol"):
                for i, li in enumerate(el.findall("li"), 1):
                    bullet = f"{i}. " if tag == "ol" else "• "
                    paras = [p for p in li if p.tag == "p"] or [li]
                    for p in paras:
                        para(f"<w:r>{_t(bullet)}</w:r>" + self.runs(p), "ListPara")
                        bullet = ""
            elif tag == "figure":
                if (img := el.find("img")) is not None:
                    para(self.picture(img), "Picture")
                if (cap := el.find("figcaption")) is not None:
                    para(self.runs(cap, italic=True), "Caption")
                first = True
            else:
                para(self.runs(el))
        return out


def _header(text_right: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:hdr {NAMESPACES}>'
        f'<w:p><w:pPr><w:pStyle w:val="Header"/><w:jc w:val="right"/></w:pPr><w:r>{_t(text_right + " / ")}</w:r>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>2</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "</w:p></w:hdr>"
    )


def _styles(lang: str) -> str:
    spacing = '<w:spacing w:before="0" w:after="0" w:line="480" w:lineRule="auto"/>'
    centred = '<w:jc w:val="center"/><w:ind w:firstLine="0"/>'

    def style(sid: str, name: str, ppr: str = "", rpr: str = "", kind: str = "paragraph", based: str = "Normal") -> str:
        b = f'<w:basedOn w:val="{based}"/>' if based and sid != based else ""
        default = ' w:default="1"' if sid == "Normal" else ""
        return (f'<w:style w:type="{kind}"{default} w:styleId="{sid}"><w:name w:val="{name}"/>{b}<w:qFormat/>'
                f"{f'<w:pPr>{ppr}</w:pPr>' if ppr else ''}{f'<w:rPr>{rpr}</w:rPr>' if rpr else ''}</w:style>")

    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {NAMESPACES}>'
        "<w:docDefaults><w:rPrDefault><w:rPr>"
        '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="Times New Roman" w:cs="Times New Roman"/>'
        f'<w:sz w:val="24"/><w:szCs w:val="24"/><w:lang w:val="{escape(lang)}"/></w:rPr></w:rPrDefault>'
        f"<w:pPrDefault><w:pPr>{spacing}</w:pPr></w:pPrDefault></w:docDefaults>"
        + style("Normal", "Normal", f'{spacing}<w:ind w:firstLine="720"/><w:widowControl/>', based="")
        + style("Contact", "Contact Block", '<w:spacing w:line="240" w:lineRule="auto"/><w:ind w:firstLine="0"/>')
        + style("BookTitle", "Title", f'<w:spacing w:before="4320" w:line="480" w:lineRule="auto"/>{centred}')
        + style("Byline", "Byline", centred)
        # Heading 1, so Word's navigation pane and the importer see each chapter.
        + style("Heading1", "heading 1",
                f'<w:pageBreakBefore/><w:spacing w:before="2880" w:after="480" w:line="480" w:lineRule="auto"/>'
                f'{centred}<w:keepNext/><w:outlineLvl w:val="0"/>', '<w:b w:val="0"/>')
        + style("Subheading", "heading 2", f'{centred}<w:keepNext/><w:outlineLvl w:val="1"/>')
        + style("SceneBreak", "Scene Break", centred)
        + style("Quote", "Quote", '<w:ind w:left="720" w:firstLine="0"/>')
        + style("ListPara", "List Paragraph", '<w:ind w:left="720" w:hanging="360"/>')
        + style("Picture", "Picture", '<w:jc w:val="center"/><w:ind w:firstLine="0"/><w:spacing w:line="240" w:lineRule="auto"/>')
        + style("Caption", "caption", centred)
        + style("End", "End", f'<w:spacing w:before="480"/>{centred}')
        + style("Header", "header", '<w:spacing w:line="240" w:lineRule="auto"/><w:ind w:firstLine="0"/>')
        + style("FootnoteText", "footnote text", '<w:spacing w:line="240" w:lineRule="auto"/><w:ind w:firstLine="0"/>',
                '<w:sz w:val="20"/><w:szCs w:val="20"/>')
        + style("FootnoteReference", "footnote reference", "", '<w:vertAlign w:val="superscript"/>', "character", "")
        + "</w:styles>"
    )


def _xhtml_root(body: str) -> ET.Element:
    return ET.fromstring(f"<div>{body}</div>")


def build_docx(project: Project, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or project.exports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    m = project.meta
    author = m.author.strip() or "Anonymous"
    surname = author.split()[-1]
    title = m.title.strip() or "Untitled"
    words = sum(project.word_counts().values())

    # Pictures are tagged with their file so the writer can embed them.
    chapters = render_chapters(project, image_src=lambda path: str(path))
    doc = _Doc()

    def para(text: str, style: str) -> str:
        return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r>{_t(text)}</w:r></w:p>'

    # First page: contact block and word count, then the title halfway down.
    doc.body.append(
        '<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:tblLook w:val="0000"/></w:tblPr>'
        '<w:tblGrid><w:gridCol w:w="4680"/><w:gridCol w:w="4680"/></w:tblGrid><w:tr>'
        f'<w:tc><w:tcPr><w:tcW w:w="2500" w:type="pct"/></w:tcPr>{para(author, "Contact")}</w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="2500" w:type="pct"/></w:tcPr>'
        f'<w:p><w:pPr><w:pStyle w:val="Contact"/><w:jc w:val="right"/></w:pPr><w:r>{_t(approx_words(words))}</w:r></w:p>'
        "</w:tc></w:tr></w:tbl>"
    )
    doc.body.append(para(title, "BookTitle"))
    if m.subtitle.strip():
        doc.body.append(para(m.subtitle.strip(), "Byline"))
    doc.body.append(para(f"by {author}", "Byline"))

    for rc in chapters:
        root = _xhtml_root(re.sub(r'src="([^"]*)"', lambda mm: f'src="" data-path="{mm[1]}"', rc.body))
        notes = {li.get("id", ""): li for li in root.iter("li") if li.get("id", "").startswith("fn:")}
        label = rc.label if m.number_chapters and rc.number is not None else ""
        heading = f"{label}: {rc.chapter.title}" if label and rc.chapter.title != label else rc.chapter.title
        doc.body.append(para(heading, "Heading1"))
        doc.body.extend(_Writer(doc, notes).blocks(root))
    doc.body.append(para("END", "End"))

    section = (
        '<w:sectPr><w:headerReference w:type="default" r:id="rIdHeader"/>'
        '<w:headerReference w:type="first" r:id="rIdHeaderFirst"/>'
        '<w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>'
        "<w:titlePg/></w:sectPr>"
    )
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {NAMESPACES}><w:body>'
        + "".join(doc.body) + section + "</w:body></w:document>"
    )
    footnotes = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:footnotes {NAMESPACES}>'
        '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
        '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
        + "".join(doc.footnotes) + "</w:footnotes>"
    )
    rels = [
        ("rIdStyles", "styles", "styles.xml"),
        ("rIdSettings", "settings", "settings.xml"),
        ("rIdFootnotes", "footnotes", "footnotes.xml"),
        ("rIdHeader", "header", "header1.xml"),
        ("rIdHeaderFirst", "header", "header2.xml"),
    ]
    media_names = {}
    for rid, path in doc.media:
        name = media_names.setdefault(path, f"media/image{len(media_names) + 1}{path.suffix.lower()}")
        rels.append((rid, "image", name))
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{kind}" '
                  f'Target="{target}"/>' for rid, kind, target in rels)
        + "</Relationships>"
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{escape(title)}</dc:title><dc:creator>{escape(author)}</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>'
    )
    types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(f'<Default Extension="{ext[1:]}" ContentType="{ct}"/>' for ext, ct in IMAGE_CONTENT_TYPES.items())
        + '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
        '<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>'
        '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
        '<Override PartName="/word/header2.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )
    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        "</Relationships>"
    )
    settings = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:settings {NAMESPACES}>'
        '<w:defaultTabStop w:val="720"/><w:footnotePr><w:numFmt w:val="decimal"/></w:footnotePr>'
        '<w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>'
        "</w:settings>"
    )

    out = out_dir / f"{slugify(title, 'book')}-manuscript.docx"
    tmp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", package_rels)
        z.writestr("docProps/core.xml", core)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", _styles(m.language or "en"))
        z.writestr("word/settings.xml", settings)
        z.writestr("word/footnotes.xml", footnotes)
        z.writestr("word/header1.xml", _header(f"{surname} / {title.upper()}"))
        z.writestr("word/header2.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:hdr {NAMESPACES}><w:p/></w:hdr>')
        for path, name in media_names.items():
            z.write(path, f"word/{name}")
    tmp.replace(out)
    return out
