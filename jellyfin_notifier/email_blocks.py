"""Visual "stackable blocks" editor for mails (poster/CMS-like, Mailchimp
style): compiles a list of blocks (JSON) into an email-safe HTML/Jinja2
template (tables, inline styles only - no flexbox/grid, no free
positioning, to stay compatible with mail clients).

The compiled result is SAVED AS-IS as the raw template (email.html /
email_upcoming.html, via the same `_save_raw_template` function used by
the advanced HTML editor) - so email_sender.py doesn't need to know
anything about blocks, and the live preview / actual sending work with no
changes. The blocks' JSON is kept separately in Settings so the visual
editor can be reopened and edited further."""

from __future__ import annotations

import html
import json
import uuid

# ---------------------------------------------------------------------------
# Available block types
# ---------------------------------------------------------------------------

BLOCK_TYPES: dict[str, str] = {
    "content_card": "Content card",
    "text_title": "Heading",
    "button": "Button",
    "spacer": "Divider",
    "text_date": "Date",
}

DEFAULT_PROPS: dict[str, dict] = {
    "content_card": {
        "show_synopsis": True,
        "show_genres": True,
        "show_rating": True,
        "show_duration": True,
        "button_text": "Watch",
    },
    "text_title": {"heading": "", "body": "", "align": "left"},
    "button": {"text": "Open Jellyfin", "url": "", "align": "left"},
    "spacer": {"height": 16, "show_line": False},
    "text_date": {"text": "", "date": "", "use_send_date": True, "align": "left"},
}


def new_block(block_type: str) -> dict:
    return {"id": uuid.uuid4().hex[:8], "type": block_type, "props": dict(DEFAULT_PROPS.get(block_type, {}))}


def default_blocks() -> list[dict]:
    """The block list matching the built-in default raw templates (a single
    content card with its default options) - used only as a display fallback
    so the Visual Block Builder isn't shown empty while the Live Preview
    (which always reflects the currently saved raw template) shows a fully
    rendered card. Nothing is written to disk/Settings until the admin
    actually clicks "Save changes"."""
    return [new_block("content_card")]


def resolve_blocks_json(raw: str | None) -> str:
    """Settings.email_blocks / upcoming_email_blocks defaults to "[]" (never
    customized via the visual builder yet). In that case, fall back to
    default_blocks() instead of showing an empty builder - see its
    docstring. Any real saved block list (including one the admin emptied
    out on purpose) is returned as-is."""
    if not raw or raw == "[]":
        return json.dumps(default_blocks())
    return raw


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _align(props: dict) -> str:
    a = props.get("align") or "left"
    return a if a in ("left", "center", "right") else "left"


# ---------------------------------------------------------------------------
# Rendering of each block type -> HTML/Jinja2 table fragment (one or more
# <tr>) - values entered by the admin are escaped (literal text, not
# executable Jinja); dynamic variables (item.*, color_*, date) remain real
# Jinja expressions.
# ---------------------------------------------------------------------------

def _render_content_card(props: dict) -> str:
    show_synopsis = props.get("show_synopsis", True)
    show_genres = props.get("show_genres", True)
    show_rating = props.get("show_rating", True)
    show_duration = props.get("show_duration", True)
    button_text = _esc(props.get("button_text") or "Watch")

    if show_duration and show_rating:
        meta = (
            '{% if item.duration or item.rating %}'
            '<div style="color:{{ color_muted }};font-size:12px;margin-bottom:6px;">'
            '{% if item.duration %}{{ item.duration }}{% endif %}'
            '{% if item.duration and item.rating %} · {% endif %}'
            '{% if item.rating %}⭐ {{ item.rating }}/10{% endif %}'
            '</div>{% endif %}'
        )
    elif show_duration:
        meta = (
            '{% if item.duration %}<div style="color:{{ color_muted }};font-size:12px;margin-bottom:6px;">'
            '{{ item.duration }}</div>{% endif %}'
        )
    elif show_rating:
        meta = (
            '{% if item.rating %}<div style="color:{{ color_muted }};font-size:12px;margin-bottom:6px;">'
            '⭐ {{ item.rating }}/10</div>{% endif %}'
        )
    else:
        meta = ""

    genres_html = ""
    if show_genres:
        genres_html = (
            '{% if item.genres %}<div style="margin-bottom:8px;">'
            '<span style="display:inline-block;background-color:#2a2a2e;color:#c9c9ce;'
            'font-size:11px;padding:3px 9px;border-radius:12px;">{{ item.genres }}</span>'
            '</div>{% endif %}'
        )

    synopsis_html = ""
    if show_synopsis:
        synopsis_html = (
            '<div style="color:#b5b5ba;font-size:13px;line-height:1.5;margin-bottom:12px;">'
            '{{ item.overview or "No synopsis available." }}</div>'
        )

    button_html = (
        '{% if item.deep_link %}<a href="{{ item.deep_link }}" '
        'style="background-color:{{ color_button }};color:#ffffff;text-decoration:none;'
        f'padding:8px 18px;border-radius:6px;font-size:13px;font-weight:bold;display:inline-block;">'
        f'▶ {button_text}</a>{{% endif %}}'
    )

    card = f"""
{{% for item in items %}}
<tr>
  <td style="padding:16px 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="width:110px;vertical-align:top;">
          {{% if item.image_cid %}}
          <img src="cid:{{{{ item.image_cid }}}}" width="100" style="border-radius:8px;display:block;border:1px solid #2a2a2e;" />
          {{% elif item.image_url %}}
          <img src="{{{{ item.image_url }}}}" width="100" style="border-radius:8px;display:block;border:1px solid #2a2a2e;" />
          {{% else %}}
          <div style="width:100px;height:150px;background:#2a2a2e;border-radius:8px;display:flex;align-items:center;justify-content:center;color:#666;font-size:11px;text-align:center;">No poster</div>
          {{% endif %}}
        </td>
        <td style="vertical-align:top;padding-left:18px;">
          <div style="color:{{{{ color_accent }}}};font-size:11px;font-weight:bold;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">{{{{ item.type_label }}}}</div>
          <div style="color:{{{{ color_text }}}};font-size:17px;font-weight:bold;line-height:1.3;margin-bottom:4px;">
            {{{{ item.name }}}}{{% if item.year %}} <span style="color:{{{{ color_muted }}}};font-weight:normal;">({{{{ item.year }}}})</span>{{% endif %}}
          </div>
          {meta}
          {genres_html}
          {synopsis_html}
          {button_html}
        </td>
      </tr>
    </table>
  </td>
</tr>
{{% if not loop.last %}}
<tr><td style="padding:0 24px;"><div style="border-top:1px solid #2a2a2e;"></div></td></tr>
{{% endif %}}
{{% endfor %}}
"""
    return card


def _render_text_title(props: dict) -> str:
    heading = _esc(props.get("heading"))
    body = _esc(props.get("body"))
    align = _align(props)
    parts = []
    if heading:
        parts.append(
            f'<div style="color:{{{{ color_text }}}};font-size:18px;font-weight:bold;'
            f'line-height:1.3;margin-bottom:6px;text-align:{align};">{heading}</div>'
        )
    if body:
        parts.append(
            f'<div style="color:{{{{ color_muted }}}};font-size:13.5px;line-height:1.55;'
            f'text-align:{align};">{body}</div>'
        )
    if not parts:
        return ""
    return f'<tr><td style="padding:16px 24px 4px 24px;">{"".join(parts)}</td></tr>'


def _render_button(props: dict) -> str:
    text = _esc(props.get("text") or "Open Jellyfin")
    url = html.escape(str(props.get("url") or "#"), quote=True)
    align = _align(props)
    return f"""
<tr>
  <td style="padding:18px 24px;text-align:{align};">
    <a href="{url}" style="background-color:{{{{ color_button }}}};color:#ffffff;text-decoration:none;
       padding:10px 22px;border-radius:6px;font-size:13.5px;font-weight:bold;display:inline-block;">
      {text}
    </a>
  </td>
</tr>
"""


def _render_spacer(props: dict) -> str:
    try:
        height = max(0, min(120, int(props.get("height", 16))))
    except (TypeError, ValueError):
        height = 16
    if props.get("show_line"):
        return (
            f'<tr><td style="padding:{height // 2}px 24px;">'
            '<div style="border-top:1px solid #2a2a2e;"></div></td></tr>'
        )
    return f'<tr><td style="height:{height}px;line-height:{height}px;font-size:0;">&nbsp;</td></tr>'


def _render_text_date(props: dict) -> str:
    text = _esc(props.get("text"))
    align = _align(props)
    if props.get("use_send_date", True):
        date_expr = "{{ date }}"
    else:
        date_expr = _esc(props.get("date"))
    if not text and not date_expr:
        return ""
    parts = []
    if date_expr:
        parts.append(
            f'<div style="color:{{{{ color_accent }}}};font-size:11px;font-weight:bold;'
            f'text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;text-align:{align};">{date_expr}</div>'
        )
    if text:
        parts.append(
            f'<div style="color:{{{{ color_text }}}};font-size:14px;line-height:1.5;text-align:{align};">{text}</div>'
        )
    return f'<tr><td style="padding:14px 24px;">{"".join(parts)}</td></tr>'


_RENDERERS = {
    "content_card": _render_content_card,
    "text_title": _render_text_title,
    "button": _render_button,
    "spacer": _render_spacer,
    "text_date": _render_text_date,
}


def compile_blocks_to_html(blocks: list[dict]) -> str:
    """Assembles the blocks into the same skeleton (logo+date header,
    introduction, body, footer) as the hand-written email.html/
    email_upcoming.html templates, for a consistent render regardless of
    which editing mode is used. The intro text (like the header/footer)
    stays defined on the "Texts & colors" tab, shared by all 3 editing
    modes - it isn't a block in itself."""
    body_rows = []
    for block in blocks:
        renderer = _RENDERERS.get(block.get("type"))
        if not renderer:
            continue
        fragment = renderer(block.get("props") or {})
        if fragment:
            body_rows.append(fragment)

    if not body_rows:
        # No blocks: at least a visual reminder so a mail isn't sent with
        # an almost-empty card (a safety net, not a real use case).
        body_rows.append(
            '<tr><td style="padding:24px;color:{{ color_muted }};font-size:13px;text-align:center;">'
            "(No blocks configured yet)</td></tr>"
        )

    body_html = "\n".join(body_rows)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Jellyfin</title>
</head>
<body style="margin:0;padding:0;background-color:{{{{ color_bg }}}};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{{{{ color_bg }}}};padding:32px 12px;">
  <tr>
    <td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="max-width:560px;background-color:{{{{ color_card }}}};border-radius:14px;overflow:hidden;border:1px solid #2a2a2e;font-family:Helvetica,Arial,sans-serif;">

        <!-- Header -->
        <tr>
          <td style="background-color:{{{{ color_header }}}};padding:20px 24px;border-bottom:1px solid #2a2a2e;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td>
                  {{% if logo_src %}}
                  <img src="{{{{ logo_src }}}}" width="126" height="36" alt="Jellyfin" style="display:block;" />
                  {{% else %}}
                  <span style="color:{{{{ color_text }}}};font-size:20px;font-weight:bold;letter-spacing:0.5px;">JELLYFIN</span>
                  {{% endif %}}
                </td>
                <td align="right" style="color:{{{{ color_muted }}}};font-size:12px;">
                  {{{{ date }}}}
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Introduction (text defined on the "Texts & colors" tab - fixed,
             like the header/footer, so it stays visible and editable
             regardless of which editing mode is used) -->
        <tr>
          <td style="padding:20px 24px 4px 24px;">
            <div style="color:{{{{ color_text }}}};font-size:16px;font-weight:bold;">
              {{{{ intro_text }}}}
            </div>
          </td>
        </tr>

        <!-- Blocks -->
        {body_html}

        <!-- Footer -->
        <tr>
          <td style="padding:20px 24px;background-color:{{{{ color_header }}}};">
            <div style="color:#666;font-size:11px;text-align:center;">
              {{{{ footer_text }}}}
            </div>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>
"""
