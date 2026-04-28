"""Telegram tool for reading posts from public Telegram channels."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.json import JsonObjectType

from .base_tool import BaseTool
from .cache import SQLiteCache
from .const import (
    CONF_TELEGRAM_NUM_POSTS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _to_local_datetime(dt_str: str, local_tz: ZoneInfo) -> str:
    """Convert an ISO datetime string to local timezone."""
    if not dt_str:
        return dt_str
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(local_tz).isoformat()
    except (ValueError, ZoneInfoNotFoundError):
        return dt_str


def extract_channel_name(url_or_name: str) -> str:
    """Extract channel name from a Telegram URL or raw name."""
    url_or_name = url_or_name.strip()

    # If it looks like a URL, extract the channel name
    if "t.me/" in url_or_name or "telegram.me" in url_or_name:
        # Remove protocol
        url_or_name = re.sub(r"^https?://", "", url_or_name)
        # Remove domain part and optional /s/ prefix (static page marker)
        url_or_name = re.sub(r"^(t\.me|telegram\.me)/", "", url_or_name)
        url_or_name = url_or_name.removeprefix("s/")
        # Remove path after channel name
        parts = url_or_name.split("?")[0].split("/")
        parts = [p for p in parts if p]
        url_or_name = parts[0] if parts else ""

    # If it still has @ prefix, remove it
    return url_or_name.removeprefix("@")


def extract_posts(html_content: str) -> list[dict[str, str]]:
    """Extract post text and date from Telegram static page HTML."""
    # Extract post dates from time tags in message meta
    date_pattern = r'<time datetime="([^"]+)"[^>]*class="time"[^>]*>.*?</time>'
    dates = re.findall(date_pattern, html_content)

    # Extract post text content
    text_pattern = (
        r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>'
    )
    text_matches = re.findall(text_pattern, html_content, re.DOTALL)

    # Pair dates with text (same count expected)
    posts = []
    for i, match in enumerate(text_matches):
        text = match
        text = re.sub(r"<br\s*/?>", " ", text)
        text = re.sub(r"<tg-emoji[^>]*>.*?</tg-emoji>", "", text, flags=re.DOTALL)
        text = re.sub(r"<a[^>]*>", "", text)
        text = text.replace("</a>", "")
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\s+", " ", text).strip()

        if text:
            posts.append(
                {
                    "date": dates[i] if i < len(dates) else "",
                    "text": text,
                }
            )

    return posts


class ReadTelegramTool(BaseTool):
    """Tool for reading posts from public Telegram channels."""

    name = "read_telegram"
    description = (
        "Read recent posts from a public Telegram channel. "
        "Provide the channel URL (t.me/channel_name or t.me/s/channel_name)."
    )
    prompt_description = None

    parameters = vol.Schema(
        {
            vol.Required(
                "url",
                description="The Telegram channel URL (e.g. https://t.me/channel_name or t.me/s/channel_name)",
            ): str,
        }
    )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Call the tool."""
        config_data = hass.data[DOMAIN].get("config", {})
        entry = next(iter(hass.config_entries.async_entries(DOMAIN)))
        config_data = {**config_data, **entry.options}

        url = tool_input.tool_args["url"]
        num_posts = int(config_data.get(CONF_TELEGRAM_NUM_POSTS, 5))

        _LOGGER.info("Telegram read requested for: %s", url)

        channel_name = extract_channel_name(url)
        if not channel_name:
            return {"error": f"Could not extract channel name from: {url}"}

        fetch_url = f"https://t.me/s/{channel_name}"

        cache = SQLiteCache()
        cached_response = cache.get(__name__, {"url": fetch_url})
        if cached_response:
            return cached_response

        try:
            session = async_get_clientsession(hass)

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }

            async with session.get(fetch_url, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.error(
                        "Telegram fetch received HTTP %s for channel: %s",
                        resp.status,
                        channel_name,
                    )
                    return {
                        "error": f"Failed to fetch channel. HTTP status: {resp.status}",
                        "channel": channel_name,
                    }

                all_posts = []
                page_url = fetch_url
                seen_urls = set()

                while len(all_posts) < num_posts:
                    if page_url in seen_urls:
                        break
                    seen_urls.add(page_url)

                    async with session.get(page_url, headers=headers) as page_resp:
                        if page_resp.status != 200:
                            _LOGGER.error(
                                "Telegram fetch received HTTP %s for channel: %s",
                                page_resp.status,
                                channel_name,
                            )
                            return {
                                "error": f"Failed to fetch channel. HTTP status: {page_resp.status}",
                                "channel": channel_name,
                            }

                        html_content = await page_resp.text()
                        page_posts = extract_posts(html_content)

                        if not page_posts:
                            break

                        all_posts.extend(page_posts)

                        # Find "show more history" link for pagination
                        more_pattern = r'href="([^"]*before=\d+)"'
                        more_match = re.search(more_pattern, html_content)
                        if more_match:
                            next_path = more_match.group(1)
                            page_url = f"https://t.me{next_path}"
                            await asyncio.sleep(1)
                        else:
                            break

                if not all_posts:
                    return {
                        "result": f"No posts found in channel {channel_name}",
                        "channel": channel_name,
                    }

                # Sort by date (newest first), then limit
                all_posts.sort(key=lambda p: p.get("date", ""), reverse=True)
                posts = all_posts[:num_posts]

                # Convert dates to user's local timezone
                local_tz = ZoneInfo(hass.config.time_zone or "UTC")
                for post in posts:
                    post["date"] = _to_local_datetime(post["date"], local_tz)

                result = {
                    "channel": channel_name,
                    "posts_count": len(posts),
                    "posts": posts,
                }

                cache.set(__name__, {"url": fetch_url}, result)
                return result

        except Exception as e:
            _LOGGER.exception("Telegram fetch error for %s", channel_name)
            return {
                "error": f"Error reading Telegram channel: {e!s}",
                "channel": channel_name,
            }
