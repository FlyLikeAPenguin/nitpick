from pathlib import Path

from src.cache import SETTINGS_DEFAULTS, ErrorCache


def test_auto_fixes_are_disabled_by_default() -> None:
    assert SETTINGS_DEFAULTS["fixes_enabled"] == "false"


def test_new_cache_uses_safe_fix_default(tmp_path: Path) -> None:
    cache = ErrorCache(tmp_path / "cache.db")

    assert cache.get_setting("fixes_enabled") == "false"

