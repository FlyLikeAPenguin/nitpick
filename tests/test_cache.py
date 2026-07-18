from pathlib import Path

from src.cache import SETTINGS_DEFAULTS, ErrorCache
from src.demo import seed_demo_data


def test_auto_fixes_are_disabled_by_default() -> None:
    assert SETTINGS_DEFAULTS["fixes_enabled"] == "false"


def test_new_cache_uses_safe_fix_default(tmp_path: Path) -> None:
    cache = ErrorCache(tmp_path / "cache.db")

    assert cache.get_setting("fixes_enabled") == "false"


def test_demo_data_is_repeatable_and_has_dashboard_records(tmp_path: Path) -> None:
    db_path = tmp_path / "demo.db"

    seed_demo_data(db_path)
    seed_demo_data(db_path)
    cache = ErrorCache(db_path)

    assert cache.get_stats()["total_errors"] == 3
    assert len(cache.get_investigations()) == 2
    assert len(cache.get_runs()) == 1
