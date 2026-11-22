"""Chinese thesis format audit and safe-copy DOCX formatting."""
from __future__ import annotations

from .audit import (
    audit_document,
    cmd_format_audit,
    cmd_format_docx,
    extract_document_spec,
    extract_template_spec,
    format_document,
    load_spec_from_inputs,
    render_markdown_report,
    write_report,
)
from .spec import default_spec, load_spec_file, parse_text_spec

__all__ = [
    "audit_document",
    "cmd_format_audit",
    "cmd_format_docx",
    "default_spec",
    "extract_document_spec",
    "extract_template_spec",
    "format_document",
    "load_spec_file",
    "load_spec_from_inputs",
    "parse_text_spec",
    "render_markdown_report",
    "write_report",
]
