from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

import config
from sub_service import check_subscriptions
from texts import fmt_emoji


class ForceSubMiddleware(BaseMiddleware):
    """Majburiy kanallarga a'zo bo'lishni tekshiruvchi middleware (Force Sub)."""

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        # Agar callback_data == "check_sub" bo'lsa — middleware o'tkazib yuboradi
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        bot = data["bot"]
        session: AsyncSession = data.get("session")
        user_id = event.from_user.id if event.from_user else None

        if not user_id or not session:
            return await handler(event, data)

        # Developer/Admin uchun majburiy obuna tekshiruvini aylanib o'tish (ixtiyoriy)
        if config.DEVELOPER_ID and user_id == config.DEVELOPER_ID:
            return await handler(event, data)

        unsubscribed = await check_subscriptions(bot=bot, user_id=user_id, session=session)

        if unsubscribed:
            keyboard_buttons = []
            for ch in unsubscribed:
                keyboard_buttons.append([
                    InlineKeyboardButton(text=f"📢 {ch.name}", url=ch.url)
                ])

            icon_check = config.EMOJI_SUCCESS or None
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="A'zo bo'ldim" if icon_check else "✅ A'zo bo'ldim",
                    callback_data="check_sub",
                    style="success",
                    icon_custom_emoji_id=icon_check,
                )
            ])

            reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            icon_warn = fmt_emoji("⚠️", config.EMOJI_WARNING)
            text = (
                f"{icon_warn} <b>Botdan foydalanish uchun quyidagi kanallarga a'zo bo'lishingiz shart:</b>\n\n"
                "Kanallarga a'zo bo'lgach, <b>'✅ A'zo bo'ldim'</b> tugmasini bosing."
            )

            if isinstance(event, Message):
                await event.answer(text, reply_markup=reply_markup, parse_mode="HTML")
            elif isinstance(event, CallbackQuery):
                await event.answer("⚠️ Avval kanallarga a'zo bo'ling!", show_alert=True)
                if event.message:
                    await event.message.answer(text, reply_markup=reply_markup, parse_mode="HTML")

            # Handler bajarilishini to'xtatadi
            return

        return await handler(event, data)
