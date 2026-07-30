"""Shared utility for generating campaign deep links and web links."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.config import settings


def get_campaign_deep_link(start_parameter: str) -> str:
    """Generate a Telegram deep link for a campaign."""
    bot_username = settings.get_bot_username()
    if bot_username:
        return f'https://t.me/{bot_username}?start={start_parameter}'
    return f'?start={start_parameter}'


def get_campaign_web_link(start_parameter: str) -> str | None:
    """Generate a web app link for a campaign.

    Prefers CABINET_URL (where the auth flow captures ?campaign= param),
    falls back to MINIAPP_CUSTOM_URL for backwards compatibility.
    """

    def _with_campaign(url: str) -> str:
        parsed = urlsplit(url)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.append(('campaign', start_parameter))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))

    cabinet_url = settings._normalized_cabinet_url()
    if cabinet_url:
        return _with_campaign(cabinet_url)

    base_url = (settings.MINIAPP_CUSTOM_URL or '').strip()
    if base_url:
        return _with_campaign(base_url)
    return None
