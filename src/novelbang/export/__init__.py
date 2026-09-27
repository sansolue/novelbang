from .epub import build_epub
from .web import build_manuscript_html, build_markdown, build_serial_pack, build_website

EXPORTERS = {
    "epub": ("EPUB ebook", "For Kindle (KDP), Kobo, Apple Books, Google Play, Draft2Digital.", build_epub),
    "html": ("Print-ready HTML", "One page styled at 6×9in. Open it and use Save as PDF.", build_manuscript_html),
    "site": ("Website", "Static site with a contents page and chapter pages (zip).", build_website),
    "serial": ("Serial pack", "Per-chapter HTML + Markdown for Royal Road, Wattpad, AO3, Substack (zip).", build_serial_pack),
    "markdown": ("Markdown manuscript", "Whole book in one .md file, e.g. for pandoc → .docx.", build_markdown),
}

__all__ = ["EXPORTERS"]
