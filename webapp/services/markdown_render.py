from __future__ import annotations

import re

import bleach
import markdown as md


_ALLOWED_TAGS = [
    "p",
    "br",
    "hr",
    "blockquote",
    "pre",
    "code",
    "strong",
    "em",
    "b",
    "i",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "a",
]

_ALLOWED_ATTRS = {
    "a": ["href", "title", "rel", "target"],
    "th": ["colspan", "rowspan"],
    "td": ["colspan", "rowspan"],
    "code": ["class"],
    "pre": ["class"],
    "table": ["class"],
}

_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def _postprocess_tables(html: str) -> str:
    # Add Bootstrap table classes to plain <table> tags.
    html = re.sub(r"<table(?![^>]*class=)", '<table class="table table-sm align-middle">', html)
    return html


def render_markdown_safe(markdown_text: str) -> str:
    raw_html = md.markdown(
        markdown_text or "",
        extensions=[
            "fenced_code",
            "tables",
            "sane_lists",
        ],
        output_format="html5",
    )

    raw_html = _postprocess_tables(raw_html)

    cleaned = bleach.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )

    # Make links safer
    cleaned = bleach.linkify(
        cleaned,
        callbacks=[bleach.callbacks.nofollow, bleach.callbacks.target_blank],
        skip_tags=["pre", "code"],
    )

    return cleaned

