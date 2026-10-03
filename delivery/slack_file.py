"""
Upload an HTML report to Slack as a file using the Files API v2.

Flow (Slack's recommended three-step upload):
  1. files.getUploadURLExternal  → pre-signed upload URL + file_id
  2. PUT the content to that URL
  3. files.completeUploadExternal → publish to channel
"""

import logging
from datetime import datetime
from typing import List, Optional, Tuple

import requests

from config import IMGBB_API_KEY, SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, SLACK_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

_BASE = "https://slack.com/api"
_SECTION_TEXT_LIMIT = 3000
_MESSAGE_BLOCK_LIMIT = 50
_FALLBACK_TEXT_LIMIT = 40_000
_TRUNCATION_NOTICE = "\n… Report truncated to fit Slack message limits."


class SlackFileUploadError(Exception):
    """Raised when the HTML file upload fails."""


def upload_html_report(
    html: str,
    channel_id: Optional[str] = None,
    bot_token: Optional[str] = None,
    title: Optional[str] = None,
) -> None:
    """
    Upload *html* as an HTML file to a Slack channel.

    Args:
        html:       Complete HTML document string.
        channel_id: Slack channel ID (e.g. C03RLCAP1AN).
        bot_token:  Bot OAuth token (xoxb-...).
        title:      Display title shown in Slack.

    Raises:
        SlackFileUploadError: on any failure.
    """
    token   = bot_token   or SLACK_BOT_TOKEN
    channel = channel_id  or SLACK_CHANNEL_ID

    if not token:
        raise SlackFileUploadError("SLACK_BOT_TOKEN is not configured")
    if not channel:
        raise SlackFileUploadError("SLACK_CHANNEL_ID is not configured")

    body     = html.encode("utf-8")
    filename = f"weather_report_{datetime.now().strftime('%Y%m%d')}.html"
    display  = title or f"Night-Hour Weather Report — {datetime.now().strftime('%Y-%m-%d')}"
    headers  = {"Authorization": f"Bearer {token}"}

    # Step 1 — request a pre-signed upload URL
    r1 = requests.post(
        f"{_BASE}/files.getUploadURLExternal",
        headers=headers,
        data={"filename": filename, "length": len(body)},
        timeout=SLACK_TIMEOUT_SECONDS,
    )
    r1.raise_for_status()
    d1 = r1.json()
    if not d1.get("ok"):
        raise SlackFileUploadError(f"getUploadURLExternal failed: {d1.get('error')}")

    upload_url = d1["upload_url"]
    file_id    = d1["file_id"]
    logger.debug("Got upload URL for file_id %s", file_id)

    # Step 2 — PUT the raw HTML to the pre-signed URL
    r2 = requests.put(
        upload_url,
        data=body,
        headers={"Content-Type": "text/html; charset=utf-8"},
        timeout=60,
    )
    r2.raise_for_status()
    logger.debug("HTML content uploaded (%d bytes)", len(body))

    # Step 3 — complete the upload and share to channel
    r3 = requests.post(
        f"{_BASE}/files.completeUploadExternal",
        headers=headers,
        json={
            "files":      [{"id": file_id, "title": display}],
            "channel_id": channel,
        },
        timeout=SLACK_TIMEOUT_SECONDS,
    )
    r3.raise_for_status()
    d3 = r3.json()
    if not d3.get("ok"):
        raise SlackFileUploadError(f"completeUploadExternal failed: {d3.get('error')}")

    # Extract the file permalink to link from the notification message
    permalink = ""
    files = d3.get("files") or []
    if files:
        permalink = files[0].get("permalink", "")

    logger.info("HTML report uploaded to Slack channel %s (file_id %s)", channel, file_id)

    # Post a visible message so the report is prominent in the feed
    msg = f":crescent_moon: *{display}*\nOpen the interactive chart below to explore the forecast."
    if permalink:
        msg += f"\n{permalink}"

    r4 = requests.post(
        f"{_BASE}/chat.postMessage",
        headers=headers,
        json={"channel": channel, "text": msg, "unfurl_links": False},
        timeout=SLACK_TIMEOUT_SECONDS,
    )
    r4.raise_for_status()
    d4 = r4.json()
    if not d4.get("ok"):
        logger.warning("chat.postMessage failed: %s", d4.get("error"))
    else:
        logger.info("Notification message posted to channel %s", channel)


def _upload_to_temp_host(png: bytes, filename: str) -> Optional[str]:
    """
    Upload *png* to a public host and return a direct image URL for Slack.
    Tries imgbb (API key from env) first, then catbox.moe anonymously.
    Returns None if all attempts fail — caller posts without an image block.
    """
    # Attempt 1: imgbb — upload as multipart/form-data binary (not base64 URL-encoded)
    if IMGBB_API_KEY:
        try:
            r = requests.post(
                "https://api.imgbb.com/1/upload",
                params={"key": IMGBB_API_KEY},
                files={"image": (filename, png, "image/png")},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            url = (data.get("data") or {}).get("url")
            if url:
                logger.info("Image hosted at %s (imgbb)", url)
                return url
            logger.warning("imgbb unexpected response: %s", str(data)[:200])
        except Exception as exc:
            logger.warning("imgbb upload failed (%s), trying fallback", exc)
    else:
        logger.warning("IMGBB_API_KEY not set — skipping imgbb, trying fallback")

    # Attempt 2: catbox.moe — anonymous, no account needed
    try:
        r = requests.post(
            "https://catbox.moe/user/api.php",
            headers={"User-Agent": "Mozilla/5.0"},
            data={"reqtype": "fileupload"},
            files={"fileToUpload": (filename, png, "image/png")},
            timeout=30,
        )
        r.raise_for_status()
        url = r.text.strip()
        if url.startswith("http"):
            logger.debug("Image hosted at %s (catbox.moe)", url)
            return url
        logger.warning("catbox.moe unexpected response: %s", url[:120])
    except Exception as exc:
        logger.warning("catbox.moe upload failed (%s); posting without image", exc)

    return None


def _mrkdwn_sections(text: str, max_sections: int) -> list[dict]:
    """Split report text into bounded Slack sections, preferring line boundaries.

    Parameters
    ----------
    text : str
        Recommendation or link text to display.
    max_sections : int
        Remaining Slack block budget after reserving other content.

    Returns
    -------
    list of dict
        Slack sections with at most 3000 characters each. Excess content
        receives a visible truncation notice in the final section.
    """
    if not text or max_sections <= 0:
        return []
    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(current) + len(line) > _SECTION_TEXT_LIMIT:
            if current:
                chunks.append(current)
                current = ""
            else:
                chunks.append(line[:_SECTION_TEXT_LIMIT])
                line = line[_SECTION_TEXT_LIMIT:]
        current += line
    if current:
        chunks.append(current)
    if len(chunks) > max_sections:
        chunks = chunks[:max_sections]
        chunks[-1] = (
            chunks[-1][: _SECTION_TEXT_LIMIT - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE
        )
    return [{"type": "section", "text": {"type": "mrkdwn", "text": chunk}} for chunk in chunks]


def upload_png_report(
    png: Optional[bytes],
    channel_id: Optional[str] = None,
    bot_token: Optional[str] = None,
    title: Optional[str] = None,
    message_text: Optional[str] = None,
    windy_links: Optional[List[Tuple[str, str]]] = None,
) -> None:
    """Post bounded recommendation sections, hosted image, and Windy links.

    Parameters
    ----------
    png : bytes or None
        Report image uploaded to the existing public image host.
    channel_id : str or None, optional
        Slack destination; defaults to configured channel.
    bot_token : str or None, optional
        Slack bot credential; defaults to configured credential.
    title : str or None, optional
        Report title used when no recommendation is available.
    message_text : str or None, optional
        Complete recommendation split into bounded Slack sections.
    windy_links : list of tuple or None, optional
        Existing link titles and Windy URLs.

    Raises
    ------
    SlackFileUploadError
        If credentials are missing or Slack rejects the message.
    """
    token = bot_token or SLACK_BOT_TOKEN
    channel = channel_id or SLACK_CHANNEL_ID

    if not token:
        raise SlackFileUploadError("SLACK_BOT_TOKEN is not configured")
    if not channel:
        raise SlackFileUploadError("SLACK_CHANNEL_ID is not configured")

    filename = f"weather_report_{datetime.now().strftime('%Y%m%d')}.png"
    display = title or f"Night-Hour Weather Report — {datetime.now().strftime('%Y-%m-%d')}"
    headers = {"Authorization": f"Bearer {token}"}

    # Step 1 — upload PNG to imgbb to get a public URL for the image block
    image_url = _upload_to_temp_host(png, filename) if png else None

    # Step 2 — post recommendation text + image block in one message
    windy_blocks: list = []
    if windy_links:
        lines = [":satellite: *Cloud cover on Windy:*"]
        for link_title, url in windy_links:
            lines.append(f"  <{url}|{link_title}>")
        link_budget = _MESSAGE_BLOCK_LIMIT - int(bool(image_url)) - int(bool(message_text))
        windy_blocks = _mrkdwn_sections("\n".join(lines), link_budget)

    section_budget = _MESSAGE_BLOCK_LIMIT - int(bool(image_url)) - len(windy_blocks)
    blocks = _mrkdwn_sections(message_text or "", section_budget)
    if image_url:
        blocks.append({"type": "image", "image_url": image_url, "alt_text": display})
    blocks.extend(windy_blocks)

    fallback_text = message_text or display
    if len(fallback_text) > _FALLBACK_TEXT_LIMIT:
        fallback_text = (
            fallback_text[: _FALLBACK_TEXT_LIMIT - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE
        )
    r1 = requests.post(
        f"{_BASE}/chat.postMessage",
        headers=headers,
        json={"channel": channel, "text": fallback_text, "blocks": blocks}
        if blocks
        else {"channel": channel, "text": fallback_text},
        timeout=SLACK_TIMEOUT_SECONDS,
    )
    r1.raise_for_status()
    d1 = r1.json()
    if not d1.get("ok"):
        raise SlackFileUploadError(f"chat.postMessage failed: {d1.get('error')}")

    if image_url:
        logger.info("Message with inline image posted to Slack channel %s", channel)
    else:
        logger.warning("Image upload failed — posted text-only to Slack channel %s", channel)
