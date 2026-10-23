from .docx import build_docx
from .epub import build_epub
from .pdf import build_pdf
from .web import build_manuscript_html, build_markdown, build_serial_pack, build_website

EXPORTERS = {
    "epub": ("EPUB ebook", "For Kindle (KDP), Kobo, Apple Books, Google Play, Draft2Digital.", build_epub),
    "pdf": ("Print PDF", "Paperback interior at the trim size in Book details, for KDP or IngramSpark, or to read.", build_pdf),
    "html": ("Print-ready HTML", "One page at the trim size in Book details. Open it in a browser and use Save as PDF.", build_manuscript_html),
    "site": ("Website", "Static site with a contents page and chapter pages (zip).", build_website),
    "serial": ("Serial pack", "Per-chapter HTML + Markdown for Royal Road, Wattpad, AO3, Substack (zip).", build_serial_pack),
    "docx": ("Word manuscript", "Standard manuscript format (.docx) for agents, editors and publishers.", build_docx),
    "markdown": ("Markdown manuscript", "Whole book in one .md file, for pandoc or any other tool.", build_markdown),
}

__all__ = ["EXPORTERS"]
