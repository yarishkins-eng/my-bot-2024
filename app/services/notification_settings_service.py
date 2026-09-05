import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class NotificationSettingsService:
    # 🔴 Последний день, на который сообщение реально уйдёт. Выборка «кто остался без
    # подписки» смотрит назад ровно на 30 дней, а отправка требует, чтобы прошло от N
    # до N+1 дня: при 30 остаётся точка «ровно 30,000 суток», в которую часовой обход
    # не попадает. Зажимаем ЗДЕСЬ, а не только на экране: в файл настроек можно попасть
    # мимо раздела (чат-админка потолка не имеет), и тогда сообщение молча не уходило бы
    # никогда. Лучше отправить на последний рабочий день, чем не отправить вовсе.
    MAX_TRIGGER_DAYS = 29

    # Через сколько часов после начала пробного писать тому, кто не подключился.
    # 🔴 Потолок держит АРИФМЕТИКА, а не вкус. Отбор требует, чтобы до конца пробного
    # оставалось не меньше TRIAL_NOT_CONNECTED_MIN_HOURS_LEFT (12 ч), поэтому окно
    # кандидатности = длина пробного − 12 − N. При трёхдневном пробном (72 ч) на N=60
    # окно схлопывается в точку и сообщение не уходит НИКОМУ, а экран показывает
    # «работает»; на 58–59 окно уже часового шага обхода и часть людей пропускается
    # молча. 24 оставляет 36 часов запаса. Минимум 1, а не 2: окно здесь одностороннее
    # (человек остаётся кандидатом до конца пробного), слепой полосы снизу нет. Ноль
    # запрещён — письмо ушло бы человеку, который прямо сейчас ставит приложение.
    MIN_NOT_CONNECTED_HOURS = 1
    MAX_NOT_CONNECTED_HOURS = 24
    """Runtime-editable notification settings stored on disk."""

    _storage_path: Path = Path('data/notification_settings.json')
    _data: dict[str, dict[str, Any]] = {}
    _loaded: bool = False

    _DEFAULTS: dict[str, dict[str, Any]] = {
        'trial_channel_unsubscribed': {'enabled': True},
        'expired_1d': {'enabled': True},
        'expired_second_wave': {
            'enabled': True,
            'discount_percent': 10,
            'valid_hours': 24,
        },
        'expired_third_wave': {
            'enabled': True,
            'discount_percent': 20,
            'valid_hours': 24,
            'trigger_days': 5,
        },
        # Скидка-крючок для тех, у кого закончился ТРИАЛ (платной не было).
        # По умолчанию ВЫКЛЮЧЕНО — включается вручную в админке (создаёт реальные офферы).
        'trial_expired_discount': {
            'enabled': False,
            'discount_percent': 10,
            'valid_hours': 24,
            'trigger_days': 1,
        },
        # --- АС-2: выключатели остальным сообщениям ---
        # Умолчание True у всех: появление ключа не должно ничего менять в поведении.
        # 🔴 Три ключа гасят ПО ДВА сообщения сразу — так устроен код бота, и на экране
        # это написано прямо. Разводить их по отдельным выключателям — отдельная работа
        # (решение владельца 01.09.2026).
        'trial_2h': {'enabled': True, 'warn_hours': 2},
        # Письмо тем, у кого идёт пробный, а первого подключения не было.
        # По умолчанию ВЫКЛЮЧЕНО: появление ключа не должно начать рассылку само,
        # включается вручную в кабинете. Тот же порядок, что у 'trial_expired_discount'.
        'trial_not_connected': {'enabled': False, 'not_connected_after_hours': 3},
        'subscription_expired': {'enabled': True},  # «пробный истёк» И «подписка истекла»
        'subscription_expiring': {'enabled': True},  # «истекает через 3 дня» И «истекает завтра»
        'traffic_warning': {'enabled': True},
        'low_balance': {'enabled': True},
        'autopay_success': {'enabled': True},
        'autopay_failed': {'enabled': True},  # «не прошёл» И «последнее напоминание»
        'autopay_legacy': {'enabled': True},
        'daily_charge': {'enabled': True},
        'daily_paused': {'enabled': True},
        'traffic_reset': {'enabled': True},
        'channel_restored': {'enabled': True},
    }

    @classmethod
    def _ensure_dir(cls) -> None:
        try:
            cls._storage_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - filesystem guard
            logger.error('Failed to create notification settings dir', exc=exc)

    @classmethod
    def _load(cls) -> None:
        if cls._loaded:
            return

        cls._ensure_dir()
        try:
            if cls._storage_path.exists():
                raw = cls._storage_path.read_text(encoding='utf-8')
                cls._data = json.loads(raw) if raw.strip() else {}
            else:
                cls._data = {}
        except Exception as exc:
            logger.error('Failed to load notification settings', exc=exc)
            cls._data = {}

        changed = cls._apply_defaults()
        if changed:
            cls._save()
        cls._loaded = True

    @classmethod
    def _apply_defaults(cls) -> bool:
        changed = False
        for key, defaults in cls._DEFAULTS.items():
            current = cls._data.get(key)
            if not isinstance(current, dict):
                cls._data[key] = deepcopy(defaults)
                changed = True
                continue

            for def_key, def_value in defaults.items():
                if def_key not in current:
                    current[def_key] = def_value
                    changed = True
        return changed

    @classmethod
    def _save(cls) -> bool:
        cls._ensure_dir()
        try:
            cls._storage_path.write_text(
                json.dumps(cls._data, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            return True
        except Exception as exc:
            logger.error('Failed to save notification settings', exc=exc)
            return False

    @classmethod
    def _get(cls, key: str) -> dict[str, Any]:
        cls._load()
        value = cls._data.get(key)
        if not isinstance(value, dict):
            value = deepcopy(cls._DEFAULTS.get(key, {}))
            cls._data[key] = value
        return value

    @classmethod
    def get_config(cls) -> dict[str, dict[str, Any]]:
        cls._load()
        return deepcopy(cls._data)

    @classmethod
    def _set_field(cls, key: str, field: str, value: Any) -> bool:
        cls._load()
        section = cls._get(key)
        section[field] = value
        cls._data[key] = section
        return cls._save()

    @classmethod
    def set_enabled(cls, key: str, enabled: bool) -> bool:
        return cls._set_field(key, 'enabled', bool(enabled))

    @classmethod
    def is_enabled(cls, key: str) -> bool:
        return bool(cls._get(key).get('enabled', True))

    @classmethod
    def is_trial_channel_unsubscribed_enabled(cls) -> bool:
        return cls.is_enabled('trial_channel_unsubscribed')

    @classmethod
    def set_trial_channel_unsubscribed_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('trial_channel_unsubscribed', enabled)

    # Expired subscription notifications
    @classmethod
    def is_expired_1d_enabled(cls) -> bool:
        return cls.is_enabled('expired_1d')

    @classmethod
    def set_expired_1d_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('expired_1d', enabled)

    @classmethod
    def is_second_wave_enabled(cls) -> bool:
        return cls.is_enabled('expired_second_wave')

    @classmethod
    def set_second_wave_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('expired_second_wave', enabled)

    @classmethod
    def get_second_wave_discount_percent(cls) -> int:
        value = cls._get('expired_second_wave').get('discount_percent', 10)
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 10

    @classmethod
    def set_second_wave_discount_percent(cls, percent: int) -> bool:
        try:
            percent_int = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_second_wave', 'discount_percent', percent_int)

    @classmethod
    def get_second_wave_valid_hours(cls) -> int:
        value = cls._get('expired_second_wave').get('valid_hours', 24)
        try:
            return max(1, min(168, int(value)))
        except (TypeError, ValueError):
            return 24

    @classmethod
    def set_second_wave_valid_hours(cls, hours: int) -> bool:
        try:
            hours_int = max(1, min(168, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_second_wave', 'valid_hours', hours_int)

    @classmethod
    def is_third_wave_enabled(cls) -> bool:
        return cls.is_enabled('expired_third_wave')

    @classmethod
    def set_third_wave_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('expired_third_wave', enabled)

    @classmethod
    def get_third_wave_discount_percent(cls) -> int:
        value = cls._get('expired_third_wave').get('discount_percent', 20)
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 20

    @classmethod
    def set_third_wave_discount_percent(cls, percent: int) -> bool:
        try:
            percent_int = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_third_wave', 'discount_percent', percent_int)

    @classmethod
    def get_third_wave_valid_hours(cls) -> int:
        value = cls._get('expired_third_wave').get('valid_hours', 24)
        try:
            return max(1, min(168, int(value)))
        except (TypeError, ValueError):
            return 24

    @classmethod
    def set_third_wave_valid_hours(cls, hours: int) -> bool:
        try:
            hours_int = max(1, min(168, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_third_wave', 'valid_hours', hours_int)

    @classmethod
    def get_third_wave_trigger_days(cls) -> int:
        value = cls._get('expired_third_wave').get('trigger_days', 5)
        try:
            return max(2, min(cls.MAX_TRIGGER_DAYS, int(value)))
        except (TypeError, ValueError):
            return 5

    @classmethod
    def set_third_wave_trigger_days(cls, days: int) -> bool:
        try:
            days_int = max(2, min(cls.MAX_TRIGGER_DAYS, int(days)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('expired_third_wave', 'trigger_days', days_int)

    # Скидка после окончания триала
    @classmethod
    def is_trial_expired_discount_enabled(cls) -> bool:
        return bool(cls._get('trial_expired_discount').get('enabled', False))

    @classmethod
    def set_trial_expired_discount_enabled(cls, enabled: bool) -> bool:
        return cls.set_enabled('trial_expired_discount', enabled)

    @classmethod
    def get_trial_expired_discount_percent(cls) -> int:
        value = cls._get('trial_expired_discount').get('discount_percent', 10)
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 10

    @classmethod
    def set_trial_expired_discount_percent(cls, percent: int) -> bool:
        try:
            percent_int = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('trial_expired_discount', 'discount_percent', percent_int)

    @classmethod
    def get_trial_expired_discount_valid_hours(cls) -> int:
        value = cls._get('trial_expired_discount').get('valid_hours', 24)
        try:
            return max(1, min(168, int(value)))
        except (TypeError, ValueError):
            return 24

    @classmethod
    def set_trial_expired_discount_valid_hours(cls, hours: int) -> bool:
        try:
            hours_int = max(1, min(168, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('trial_expired_discount', 'valid_hours', hours_int)

    @classmethod
    def get_trial_expired_discount_trigger_days(cls) -> int:
        value = cls._get('trial_expired_discount').get('trigger_days', 1)
        try:
            return max(1, min(cls.MAX_TRIGGER_DAYS, int(value)))
        except (TypeError, ValueError):
            return 1

    @classmethod
    def set_trial_expired_discount_trigger_days(cls, days: int) -> bool:
        try:
            days_int = max(1, min(cls.MAX_TRIGGER_DAYS, int(days)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('trial_expired_discount', 'trigger_days', days_int)

    @classmethod
    def get_trial_warn_hours(cls) -> int:
        """За сколько часов до конца пробного предупреждать.

        🔴 Нижняя граница — ДВА часа, и это не вкусовщина. Служба мониторинга спит час
        ПОСЛЕ обхода, значит шаг между обходами — час плюс длительность самого обхода.
        Условие отправки — «до конца осталось не больше N», то есть окно шириной ровно N.
        Окно в один час уже шага, и в каждом обороте остаётся слепая полоса: кто попал в
        неё, не получит ничего, причём молча. Два часа перекрывают шаг с запасом.
        """
        try:
            return max(2, min(48, int(cls._get('trial_2h').get('warn_hours', 2))))
        except (TypeError, ValueError):
            return 2

    @classmethod
    def get_trial_not_connected_after_hours(cls) -> int:
        """Через сколько часов после начала пробного писать неподключившемуся.

        Зажимаем ЗДЕСЬ, а не только на экране: в файл настроек можно попасть мимо
        раздела, и значение выше потолка молча убило бы сообщение навсегда.
        """
        try:
            stored = int(cls._get('trial_not_connected').get('not_connected_after_hours', 3))
            return max(cls.MIN_NOT_CONNECTED_HOURS, min(cls.MAX_NOT_CONNECTED_HOURS, stored))
        except (TypeError, ValueError):
            return 3

    @classmethod
    def set_trial_not_connected_after_hours(cls, hours: int) -> bool:
        try:
            value = max(cls.MIN_NOT_CONNECTED_HOURS, min(cls.MAX_NOT_CONNECTED_HOURS, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('trial_not_connected', 'not_connected_after_hours', value)

    @classmethod
    def set_trial_warn_hours(cls, hours: int) -> bool:
        try:
            value = max(2, min(48, int(hours)))
        except (TypeError, ValueError):
            return False
        return cls._set_field('trial_2h', 'warn_hours', value)

    @classmethod
    def are_notifications_globally_enabled(cls) -> bool:
        return bool(getattr(settings, 'ENABLE_NOTIFICATIONS', True))
