"""Строка утреннего отчёта обязана называть то, что посчитал её запрос (пункт ПД-3).

🔴 Проверка идёт через НАСТОЯЩИЙ путь: собирается весь текст отчёта и разбирается сам
запрос, а не читается исходник. Поведение `reporting_service.py` до этого не проверял
никто (единственное упоминание файла — список разрешённых отправок в
`test_manager_alert_service.py`).

🔴 ГРАНИЦЫ СТОРОЖА, чтобы на него не полагались шире, чем он держит. Со стороны ЗАПРОСА
он стережёт свойство: считаются люди (`distinct`), мерятся выданные серверы, а не трафик
и не активность, и фильтра по статусу/сроку нет — то есть «за всю историю» правда. Со
стороны НАДПИСИ он держит слабее: ловит слово «серверов», слово «истори» и чёрный список
корней прежнего вранья. Синоним вроде «не вышли в сеть» он не поймает — от такой замены
защищает не он, а комментарий у самого запроса.

🔴 Обе проверки со стороны запроса переписаны 06.09.2026 после того, как скептик обошёл их
мутациями: `'distinct' in sql` проходило на `.label('distinct_users_count')` при убранном
DISTINCT, а чёрный список `('status', 'end_date')` не видел сужения по `created_at`.
"""

import re
from typing import Self

import pytest
from sqlalchemy.dialects import postgresql

from app.services import reporting_service as module
from app.services.reporting_service import ReportingService, ReportPeriod


# Слова, которыми строка обещала бы поведение человека вместо поломки выдачи.
_PROMISES_BEHAVIOUR = ('подключ', 'заход', 'воспольз')


class _FakeResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar(self) -> int:
        return self._value


class _RecordingSession:
    """Сессия, которая запоминает переданные ей запросы и ничего не исполняет."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _FakeResult(len(self.statements))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


def _line_with(report: str, needle: str) -> str:
    matches = [line for line in report.split('\n') if needle in line]
    assert len(matches) == 1, f'ожидали ровно одну строку с {needle!r}, нашли {len(matches)}'
    return matches[0]


@pytest.mark.asyncio
async def test_usage_line_names_servers_and_promises_nothing_about_connections(monkeypatch) -> None:
    service = ReportingService()

    async def _totals(_session):
        return {'active_trials': 1, 'active_paid': 2, 'open_tickets': 3}

    async def _stats(_session, _start, _end):
        return {
            'new_users': 1,
            'new_trials': 2,
            'trial_to_paid_conversions': 1,
            'new_paid_subscriptions': 1,
            'deposits_amount': 10000,
            'deposits_count': 1,
            'subscription_payments_count': 1,
            'subscription_payments_amount': 20000,
            'new_tickets': 0,
        }

    async def _referrers(_session, _start, _end, limit=5):
        return []

    async def _usage(_session):
        # 4321 — заведомо не совпадает ни с одним другим числом отчёта, иначе строку
        # можно было бы найти по чужому значению.
        return {'active_paid_users': 7, 'users_without_servers': 4321}

    monkeypatch.setattr(service, '_collect_current_totals', _totals)
    monkeypatch.setattr(service, '_collect_period_stats', _stats)
    monkeypatch.setattr(service, '_get_top_referrers', _referrers)
    monkeypatch.setattr(service, '_get_user_usage_stats', _usage)
    monkeypatch.setattr(module, 'AsyncSessionLocal', _RecordingSession)

    report = await service._build_report(ReportPeriod.DAILY, None)

    line = _line_with(report, '4321')
    assert 'серверов' in line, f'строка не называет серверы: {line!r}'
    assert 'истори' in line, f'строка не говорит, что число копится за всю историю: {line!r}'
    for promise in _PROMISES_BEHAVIOUR:
        assert promise not in line.lower(), (
            f'строка обещает поведение человека ({promise!r}), а запрос считает поломку выдачи: {line!r}'
        )


@pytest.mark.asyncio
async def test_usage_query_measures_servers_and_not_connections() -> None:
    session = _RecordingSession()

    usage = await ReportingService()._get_user_usage_stats(session)

    assert set(usage) == {'active_paid_users', 'users_without_servers'}
    assert len(session.statements) == 2, 'ожидали ровно два запроса: платные и без серверов'

    sql = str(session.statements[1].compile(dialect=postgresql.dialect())).lower()
    assert 'connected_squads' in sql, 'запрос перестал смотреть на выданные серверы'
    # Надпись говорит «Пользователей» — значит считать надо людей, а не строки подписок.
    # Проверяем сам вызов, а не слово: скептик обошёл `'distinct' in sql` тем, что убрал
    # DISTINCT и назвал колонку `distinct_users_count`.
    assert 'count(distinct' in sql, 'запрос считает подписки, а строка обещает пользователей'
    # Надпись говорит «накопительно за всю историю» — значит в отборе не должно быть НИЧЕГО,
    # кроме выданных серверов. Проверяем СОСТАВОМ колонок, а не чёрным списком имён: список
    # скептик обошёл сужением по `created_at`, которого в нём не было.
    # Переносы строк в скомпилированном SQL сначала схлопываем: без этого `where` стоит
    # после `\n` и не находится, а тест краснеет на исправном коде.
    where = ' '.join(sql.split()).split(' where ', 1)
    assert len(where) == 2, f'у запроса пропал отбор целиком: {sql}'
    narrowed_by = set(re.findall(r'subscriptions\.(\w+)', where[1]))
    assert narrowed_by == {'connected_squads'}, (
        f'запрос отбирает ещё и по {sorted(narrowed_by - {"connected_squads"})} — '
        '«накопительно за всю историю» в строке отчёта стало ложью'
    )
    # Подключения живут в панели, а не в этих колонках: пока их тут нет, строка не имеет
    # права обещать «не подключился».
    for column in ('traffic_used', 'last_activity', 'lifetime_used_traffic'):
        assert column not in sql, (
            f'запрос начал смотреть на {column} — значит он мерит уже другое, '
            'и название строки в отчёте надо пересматривать вместе с ним'
        )
