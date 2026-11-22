"""Torrent search through the piyann gateway.

``/search <key>`` lists the gateway's torrent providers; tapping one runs the
search and paints the results as a paged rich message. Every result carries a
button that sends its magnet and a link button to the provider's page -- the
gateway gives those two, and a few providers have only the page (limetorrents
returns ``magnet: null``), so a row shows the buttons it can honour.

State lives in ``_SEARCHES``, keyed by user id: callback data is capped at 64
bytes, so the query and the result list can only stay here. A new search for the
same user replaces the old one, and the process owns no other copy.
"""

from dataclasses import dataclass, field
from html import escape
from typing import TYPE_CHECKING

from httpx import AsyncClient
from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardMarkup

from .. import LOGGER
from ..helper.net.gateway import gateway_headers, gateway_url
from ..helper.telegram.button_build import ButtonMaker
from ..helper.telegram.message_utils import edit_message, message_text, send_message
from ..helper.util.bot_utils import new_task

if TYPE_CHECKING:
    from pyrogram.types import CallbackQuery, Message

PROVIDERS: dict[str, str] | None = None
PAGE_SIZE = 5
REQUEST_TIMEOUT = 60
ALL_PROVIDERS = "all"


@dataclass
class _SearchState:
    key: str
    provider: str
    results: list[dict[str, object]]
    failed: list[str]
    page: int = field(default=1)


_SEARCHES: dict[int, _SearchState] = {}


async def initiate_search_tools() -> None:
    """Fetch the provider list the search menu and the renderer name from."""
    global PROVIDERS
    try:
        async with AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(
                gateway_url("/api/v1/scrape/torrent/providers"),
                headers=gateway_headers(),
            )
            response.raise_for_status()
            data = response.json()
        PROVIDERS = {str(p["id"]): str(p["name"]) for p in data["providers"]}
        PROVIDERS[ALL_PROVIDERS] = "All"
    except Exception as e:
        LOGGER.error(f"Can't fetch torrent providers from the gateway: {e}")
        PROVIDERS = None


def _provider_name(provider: str) -> str:
    return (PROVIDERS or {}).get(provider, provider.capitalize())


async def _search(
    key: str, provider: str
) -> tuple[list[dict[str, object]], list[str]]:
    """The gateway's items and the providers that failed on the way."""
    async with AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(
            gateway_url("/api/v1/scrape/torrent/search"),
            params={"q": key, "provider": provider},
            headers=gateway_headers(),
        )
        response.raise_for_status()
        data = response.json()
    if not data.get("success"):
        raise RuntimeError(data.get("error") or "gateway search failed")
    results = data.get("results") or []
    failed = [str(e.get("provider", "?")) for e in data.get("errors") or []]
    return results, failed


def _cb(user_id: int, action: str, arg: str | int = "", token: int = 0) -> str:
    """Callback data for *action*; *token* pins it to the page that drew it.

    Zero keeps the old short form, which the provider menu and the page counter
    use. A paging button carries the page it was drawn on, so a tap on a stale
    keyboard (an earlier render, or a double tap) is refused instead of moving
    from the page the user never saw.
    """
    suffix = f":{arg}:{token}" if token else (f":{arg}" if arg != "" else "")
    return f"torser:{user_id}:{action}{suffix}"


def _provider_buttons(user_id: int) -> InlineKeyboardMarkup:
    buttons = ButtonMaker()
    for provider, name in (PROVIDERS or {}).items():
        buttons.data_button(
            name,
            _cb(user_id, "s", provider),
            style=ButtonStyle.PRIMARY
            if provider == ALL_PROVIDERS
            else ButtonStyle.SUCCESS,
        )
    buttons.data_button("Cancel", _cb(user_id, "c"), style=ButtonStyle.DANGER)
    return buttons.build_menu(2)


def render_page(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """The message and keyboard for *page*, clamped into the result count."""
    state = _SEARCHES[user_id]
    total = len(state.results)
    pages = max(1, -(-total // PAGE_SIZE))
    state.page = min(max(page, 1), pages)
    start = (state.page - 1) * PAGE_SIZE
    lines = [
        f"<b>Torrent Search</b> — <i>{escape(state.key)}</i>",
        f"<b>Provider:</b> {escape(_provider_name(state.provider))} | "
        f"<b>Results:</b> {total} | <b>Page:</b> {state.page}/{pages}",
    ]
    if state.failed:
        lines.append(
            f"<b>Failed providers:</b> <i>{escape(', '.join(state.failed))}</i>"
        )
    buttons = ButtonMaker()
    for index in range(start, min(start + PAGE_SIZE, total)):
        item = state.results[index]
        lines.append("")
        lines.append(f"<b>{index + 1}. {escape(str(item.get('name', '')))}</b>")
        lines.append(
            f"{escape(str(item.get('size', '')))} | "
            f"S:{item.get('seeder', 0)} | L:{item.get('leecher', 0)} | "
            f"{escape(_provider_name(str(item.get('provider', ''))))}"
        )
        if item.get("magnet"):
            buttons.data_button(
                "Magnet",
                _cb(user_id, "m", index),
                style=ButtonStyle.SUCCESS,
            )
        if item.get("url"):
            buttons.url_button(
                "Torrent", str(item["url"]), style=ButtonStyle.PRIMARY
            )
    # Two buttons per result row, then the navigation and Close rows: footer
    # buttons each become their own row at ``f_cols=1`` (and land last).
    if state.page > 1:
        buttons.data_button(
            "Prev",
            _cb(user_id, "p", state.page - 1, state.page),
            position="footer",
            style=ButtonStyle.PRIMARY,
        )
    buttons.data_button(
        f"{state.page}/{pages}",
        _cb(user_id, "n"),
        position="footer",
        style=ButtonStyle.DEFAULT,
    )
    if state.page < pages:
        buttons.data_button(
            "Next",
            _cb(user_id, "p", state.page + 1, state.page),
            position="footer",
            style=ButtonStyle.PRIMARY,
        )
    buttons.data_button(
        "Close",
        _cb(user_id, "c"),
        position="footer",
        style=ButtonStyle.DANGER,
    )
    return "\n".join(lines), buttons.build_menu(2, f_cols=1)


def _reply_key(message: Message) -> str | None:
    """The search key, read back from the command the menu replies to."""
    replied = message.reply_to_message
    if replied is None:
        return None
    parts = message_text(replied).split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 and parts[1].strip() else None


async def _start_search(
    query: CallbackQuery, message: Message, user_id: int, provider: str
) -> None:
    key = _reply_key(message)
    if key is None:
        await query.answer("Search key is gone, send the command again.", show_alert=True)
        return
    await query.answer()
    await edit_message(
        message,
        f"<b>Searching</b> <i>{escape(key)}</i> on "
        f"<b>{escape(_provider_name(provider))}</b>...",
    )
    try:
        results, failed = await _search(key, provider)
    except Exception as e:
        await edit_message(message, f"<b>Search failed:</b> {escape(str(e))}")
        return
    if not results:
        await edit_message(
            message,
            f"<b>No result found for <i>{escape(key)}</i></b>\n"
            f"<b>Provider:</b> <i>{escape(_provider_name(provider))}</i>",
        )
        return
    _SEARCHES[user_id] = _SearchState(key, provider, results, failed)
    text, buttons = render_page(user_id, 1)
    await edit_message(message, text, buttons)


async def _show_page(
    query: CallbackQuery, message: Message, user_id: int, page: int, token: int
) -> bool:
    """Paint *page*; False when the press was refused, so it is not re-answered."""
    state = _SEARCHES.get(user_id)
    if state is None:
        await query.answer(
            "That search expired, send the command again.", show_alert=True
        )
        return False
    if token and token != state.page:
        # Drawn for a page the state has left: the tap came from a stale render
        # (an earlier edit, or a double tap). Nothing to redraw.
        return False
    text, buttons = render_page(user_id, page)
    await edit_message(message, text, buttons)
    return True


async def _send_magnet(
    query: CallbackQuery, message: Message, user_id: int, index: int
) -> None:
    state = _SEARCHES.get(user_id)
    if state is None or not 0 <= index < len(state.results):
        await query.answer("That search expired, send the command again.", show_alert=True)
        return
    magnet = state.results[index].get("magnet")
    if not magnet:
        await query.answer("This result has no magnet, use its Torrent button.", show_alert=True)
        return
    await query.answer("Magnet sent")
    await send_message(message, f"<code>{escape(str(magnet))}</code>")


@new_task
async def torrent_search(_, message: Message) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    parts = message_text(message).split(maxsplit=1)
    key = parts[1].strip() if len(parts) > 1 else ""
    if not key:
        await send_message(
            message,
            "Send a search key along with the command, e.g. "
            "<code>/search ubuntu</code>",
        )
        return
    if not PROVIDERS:
        await send_message(
            message,
            "Torrent search is unavailable: the provider list could not be fetched.",
        )
        return
    await send_message(
        message,
        f"<b>Searching for</b> <i>{escape(key)}</i>\n<b>Choose a provider:</b>",
        _provider_buttons(user_id),
    )


@new_task
async def torrent_search_update(_, query: CallbackQuery) -> None:
    if not query.data or query.message is None or query.from_user is None:
        return
    data = query.data.decode() if isinstance(query.data, bytes) else query.data
    parts = data.split(":")
    if len(parts) < 3:
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        return
    if query.from_user.id != user_id:
        await query.answer("Not Yours!", show_alert=True)
        return
    action = parts[2]
    arg = parts[3] if len(parts) > 3 else ""
    message = query.message
    if action == "c":
        await query.answer()
        await edit_message(message, "Search has been canceled!")
    elif action == "s":
        await _start_search(query, message, user_id, arg or ALL_PROVIDERS)
    elif action == "p":
        try:
            page = int(arg)
            token = int(parts[4]) if len(parts) > 4 else 0
        except ValueError:
            await query.answer()
            return
        if await _show_page(query, message, user_id, page, token):
            await query.answer()
    elif action == "m":
        try:
            index = int(arg)
        except ValueError:
            await query.answer()
            return
        await _send_magnet(query, message, user_id, index)
    else:
        # "n" is the page counter, which exists to show the position only.
        await query.answer()
