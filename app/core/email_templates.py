"""Shared HTML layouts for branded transactional emails."""

from html import escape
from re import split

BRAND_RED = "#E0242A"


def text_to_html(text: str) -> str:
    paragraphs = []
    for paragraph in split(r"\n\s*\n", text.strip()):
        if paragraph:
            content = escape(paragraph).replace("\n", "<br>\n")
            paragraphs.append(f'<p style="margin:0 0 16px;color:#344054;font-size:15px;line-height:1.6">{content}</p>')
    return "\n".join(paragraphs)


def render_branded_email(
    brand_name: str,
    subject: str,
    content_html: str,
    header_highlights: tuple[tuple[str, str], ...] = (),
) -> str:
    brand = escape(brand_name)
    title = escape(subject)
    highlights_html = ""
    if header_highlights:
        rows = "\n".join(
            f"""\
            <tr>
              <td style="padding:8px 14px;color:#667085;font-size:12px;white-space:nowrap">{escape(label)}</td>
              <td style="padding:8px 14px;color:#182230;font-size:15px;font-weight:700">{escape(value)}</td>
            </tr>"""
            for label, value in header_highlights
        )
        highlights_html = f"""\
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                       style="margin:0 0 24px;background:#fff8f8;border:1px solid #ffd4d6;border-radius:8px">
                  {rows}
                </table>
"""
    return f"""\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="x-apple-disable-message-reformatting">
    <title>{title}</title>
  </head>
  <body style="margin:0;padding:24px 12px;background:#f4f6f8;font-family:Arial,Helvetica,sans-serif">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                 style="max-width:600px;background:#fff;border:1px solid #e6e9ed;border-radius:12px">
            <tr>
              <td align="center" style="padding:28px 24px 20px;border-bottom:1px solid #edf0f2">
                <img src="cid:trustus-health-logo" width="264" alt="{brand}"
                     style="display:block;width:264px;max-width:100%;height:auto;border:0">
              </td>
            </tr>
            <tr>
              <td style="padding:28px 32px 32px">
                <div style="width:44px;height:4px;background:{BRAND_RED};border-radius:4px;margin:0 0 18px"></div>
                {highlights_html}
                {content_html}
              </td>
            </tr>
            <tr>
              <td style="padding:18px 24px;background:#fafbfc;border-top:1px solid #edf0f2;
                         color:#667085;font-size:12px;line-height:1.5;text-align:center">
                Automated message from {brand}. Please do not share sensitive information by replying to this email.
              </td>
            </tr>
          </table>
          <p style="margin:16px 0 0;color:#98a2b3;font-size:11px">{brand}</p>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def otp_email_body(text: str, code: str) -> str:
    safe_code = escape(code)
    safe_text = escape(text).replace("\n", "<br>\n")
    highlighted_code = (
        f'<span style="display:inline-block;padding:8px 12px;background:#fff5f5;border:1px solid #ffd4d6;'
        f'border-radius:8px;color:{BRAND_RED};font-size:24px;font-weight:700;letter-spacing:6px">'
        f"{safe_code}</span>"
    )
    safe_text = safe_text.replace(safe_code, highlighted_code)
    return f"""\
<p style="margin:0;color:#344054;font-size:15px;line-height:2">{safe_text}</p>
"""
