"""РС-14г (мина GZ): сервер отбивает повтор той же кампании на ту же аудиторию.

Ключа идемпотентности у рассылок нет, и завести его нельзя — это миграция. Забор по окну
не делает отправку идемпотентной, он лишь не даёт УЙТИ второму письму после потерянного
ответа и перезагрузки страницы. Проверяем ровно это: 409 до создания записи и до воркера.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_broadcasts as broadcast_routes
from app.cabinet.schemas.broadcasts import BroadcastCreateRequest
from app.database.models import BroadcastHistory, UserStatus


class _Rows:
    def __init__(self, values):
        self._values = values

    def all(self):
        return [(v,) for v in self._values]

    def scalars(self):
        return SimpleNamespace(first=lambda: self._values[0] if self._values else None)


class _RecordingSession:
    """Фейк, который ИСПОЛНЯЕТ запрос: он компилирует его и отдаёт заранее заданный ответ."""

    def __init__(self, duplicate_id: int | None):
        self._duplicate_id = duplicate_id
        self.duplicate_sql: str | None = None
        self.added = 0
        self.commits = 0

    async def execute(self, statement):
        sql = str(statement)
        if 'broadcast_history' in sql:
            self.duplicate_sql = sql
            return _Rows([self._duplicate_id] if self._duplicate_id is not None else [])
        return _Rows([])

    async def get(self, _model, pk):
        # Путь `self` сверяет получателя с личностью админа (забор HD) — фейку нужен и `get`.
        return SimpleNamespace(
            id=pk,
            telegram_id=555000,
            username='owner',
            status=UserStatus.ACTIVE.value,
            language='ru',
        )

    def add(self, obj) -> None:
        self.added += 1
        self._added_obj = obj

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj) -> None:
        # В боевой базе оба поля проставляет сервер; фейк обязан их изобразить,
        # иначе схема ответа падает раньше, чем тест успеет что-либо проверить.
        obj.id = 777
        obj.created_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _request(text: str = 'Скидка 20 % до пятницы') -> BroadcastCreateRequest:
    return BroadcastCreateRequest(target='all', message_text=text, selected_buttons=[], custom_buttons=[])


@pytest.fixture
def no_worker(monkeypatch):
    started = []

    async def _record(*args, **kwargs):
        started.append(args)

    monkeypatch.setattr(broadcast_routes.broadcast_service, 'start_broadcast', _record)
    return started


@pytest.mark.asyncio
async def test_duplicate_is_rejected_before_record_and_worker(no_worker) -> None:
    session = _RecordingSession(duplicate_id=41)
    admin = SimpleNamespace(id=9, username='manager')

    with pytest.raises(HTTPException) as exc:
        await broadcast_routes.create_broadcast(_request(), admin, session)

    assert exc.value.status_code == 409
    assert '#41' in exc.value.detail, 'отказ обязан назвать номер уже созданной кампании'
    assert session.added == 0, 'вторая запись в историю не создаётся'
    assert session.commits == 0
    assert no_worker == [], 'воркер не запускается — второе письмо людям не уходит'


@pytest.mark.asyncio
async def test_first_send_is_not_blocked(no_worker) -> None:
    """Забор обязан молчать, когда такой кампании ещё не было."""
    session = _RecordingSession(duplicate_id=None)
    admin = SimpleNamespace(id=9, username='manager')

    await broadcast_routes.create_broadcast(_request(), admin, session)

    assert session.added == 1
    assert len(no_worker) == 1


@pytest.mark.asyncio
async def test_guard_filters_by_admin_target_text_and_time(no_worker) -> None:
    """Сторож формы запроса: без любого из четырёх условий забор ловил бы чужое или всё подряд."""
    session = _RecordingSession(duplicate_id=None)
    await broadcast_routes.create_broadcast(_request(), SimpleNamespace(id=9, username='manager'), session)

    sql = session.duplicate_sql
    assert sql is not None, 'запрос к истории рассылок не выполнялся вовсе'
    for column in ('admin_id', 'target_type', 'message_text', 'created_at'):
        assert f'broadcast_history.{column}' in sql, f'забор не смотрит на {column}'


@pytest.mark.asyncio
async def test_window_is_short_enough_to_allow_a_deliberate_repeat() -> None:
    """Окно — защита от промаха, а не запрет повторять: намеренный повтор ждёт минуты, не часы."""
    assert 1 <= broadcast_routes.DUPLICATE_BROADCAST_WINDOW_MINUTES <= 15


@pytest.mark.asyncio
async def test_self_canary_repeat_is_never_blocked(no_worker) -> None:
    """Канарейку «только мне» повторяют СПЕЦИАЛЬНО — это проверка перед широкой отправкой.

    Ревью показало сценарий: послал себе, увидел в Телеграме, что картинка не прицепилась,
    поправил и жмёшь снова — забор отвечал «измените текст», а текст-то верный.
    """
    session = _RecordingSession(duplicate_id=41)
    request = BroadcastCreateRequest(target='self', message_text='Проверка', selected_buttons=[], custom_buttons=[])
    await broadcast_routes.create_broadcast(request, SimpleNamespace(id=9, username='owner'), session)

    assert session.added == 1, 'повтор канарейки заблокирован — способ проверить кампанию сломан'
    assert session.duplicate_sql is None, 'для self история вообще не должна опрашиваться'


@pytest.mark.asyncio
async def test_guard_key_includes_category_media_and_subject(no_worker) -> None:
    """Кампания, отличающаяся вложением, категорией или темой письма, — НЕ повтор.

    Пока в ключе был только текст, у почтовой кампании он вырождался в `message_text IS NULL`,
    и два РАЗНЫХ письма подряд на одну аудиторию считались одним и тем же.
    """
    session = _RecordingSession(duplicate_id=None)
    await broadcast_routes.create_broadcast(_request(), SimpleNamespace(id=9, username='m'), session)

    sql = session.duplicate_sql
    assert sql is not None
    for column in ('category', 'media_file_id', 'email_subject'):
        assert f'broadcast_history.{column}' in sql, f'забор не смотрит на {column} — разные кампании слипнутся'


@pytest.mark.asyncio
async def test_different_text_is_not_a_duplicate(no_worker) -> None:
    """Сравнение идёт по тексту: другое письмо той же аудитории проходит без задержки."""
    session = _RecordingSession(duplicate_id=None)
    await broadcast_routes.create_broadcast(
        _request('Совсем другое письмо'), SimpleNamespace(id=9, username='manager'), session
    )
    assert session.added == 1, 'другое письмо той же аудитории — не повтор, забор молчать обязан'
    assert BroadcastHistory.__tablename__ == 'broadcast_history'
