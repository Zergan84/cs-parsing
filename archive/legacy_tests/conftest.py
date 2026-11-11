"""
conftest.py - Глобальная конфигурация pytest для полной изоляции тестов (Фаза 0).

Выполняется ДО импорта любых тестовых модулей:
1. Активирует изолированные пути CS_OUTPUT_DIR и CS_DB_PATH во временной директории.
2. Снимает эталонные SHA-256 всех production-файлов (SQLite и JSON).
3. Устанавливает runtime-перехватчики на SQLite, файловую систему, сеть, Playwright и AutoSender.
4. Для каждого теста выделяет чистую изолированную директорию tmp_path и проверяет неизменность production после теста.
"""

from pathlib import Path
from typing import Dict, Any, Generator

try:
    import pytest
except ImportError:
    pytest = None  # type: ignore[assignment]

from test_isolation import (
    activate_test_isolation,
    assert_not_production_paths,
    rebind_all_modules,
    snapshot_production_checksums,
    verify_production_checksums,
)

# 1. Активируем тестовую изоляцию ДО импорта любых модулей приложения
_BOOT_OUT_DIR, _BOOT_DB_PATH = activate_test_isolation()
_PROD_CHECKSUMS_BEFORE: Dict[str, Dict[str, Any]] = snapshot_production_checksums()

if pytest is not None:
    def pytest_configure(config: Any) -> None:
        """Проверяет изоляцию путей на старте конфигурации pytest."""
        assert_not_production_paths()

    @pytest.fixture(autouse=True)
    def isolated_test_env(tmp_path: Path) -> Generator[Dict[str, Path], None, None]:
        """
        Автоматическая фикстура для каждого теста:
        перенаправляет CS_OUTPUT_DIR и CS_DB_PATH в уникальный tmp_path теста
        и проверяет контрольные суммы production-файлов до и после выполнения теста.
        """
        test_output_dir = (tmp_path / "output").resolve()
        test_db_path = (test_output_dir / "couchsurfing_platform.db").resolve()

        rebind_all_modules(output_dir=test_output_dir, db_path=test_db_path)

        import database
        database.init_db()

        yield {
            "output_dir": test_output_dir,
            "db_path": test_db_path,
        }

        verify_production_checksums(_PROD_CHECKSUMS_BEFORE)

    def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
        """Финальная сверка SHA-256 всех production-файлов по завершении всей сессии pytest."""
        verify_production_checksums(_PROD_CHECKSUMS_BEFORE)
