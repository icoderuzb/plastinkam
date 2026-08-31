from aiogram.fsm.state import State, StatesGroup


class AddChannelState(StatesGroup):
    """Kanal qo'shish jarayoni uchun FSM holatlari."""

    waiting_for_channel_info = State()


class EditLabelTextState(StatesGroup):
    """Vinyl plastinkasiga yoziladigan matnni (Artist - Title) tahrirlash holati."""

    waiting_for_text = State()


class BatchAudioState(StatesGroup):
    """Bir nechta audio yuborilganda foydalanuvchi qarorini kutish holati."""

    waiting_for_decision = State()
