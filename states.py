from aiogram.fsm.state import State, StatesGroup


class AddChannelState(StatesGroup):
    """Kanal qo'shish jarayoni uchun FSM holatlari."""

    waiting_for_channel_info = State()
