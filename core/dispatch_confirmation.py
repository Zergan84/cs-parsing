"""
core/dispatch_confirmation.py — Строгая валидация подтверждения отправки Stay Request.

Определяет явные уровни исхода диспетчеризации и компонент валидации подтверждения,
который различает:
* Создание Stay Request.
* Отправку сообщения.
* Создание диалога.
* Навигацию в чат.
* Навигацию на профиль.
* Общие ответы API.

Правило: HTTP 200-299 НЕ является достаточным подтверждением.
Граф-QUERY успех НЕ является достаточным подтверждением.
Появление страницы чата НЕ является достаточным подтверждением.
Общий текст успеха НЕ является достаточным подтверждением.

Если точное поведение Couchsurfing при успешном создании Stay Request не может
быть установлено, механизм возвращает UNKNOWN_OUTCOME (fail-closed).

Модель能力 (capability):
Подтверждение выпускается только ConfirmationValidator в виде неподделываемого
ConfirmationCapability токена. mark_as_sent() принимает этот токен и проверяет его
подлинность перед переходом в состояние SENT.
"""

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("DispatchConfirmation")


# ---------------------------------------------------------------------------
# Уровни исхода диспетчеризации
# ---------------------------------------------------------------------------

class DispatchOutcome(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    CONFIRMED_SENT = "CONFIRMED_SENT"
    CONFIRMED_REJECTED = "CONFIRMED_REJECTED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    REQUIRES_RECONCILIATION = "REQUIRES_RECONCILIATION"


# ---------------------------------------------------------------------------
# Типы подтверждения (evidence)
# ---------------------------------------------------------------------------

class EvidenceType(str, Enum):
    NONE = "none"
    NAVIGATION = "navigation"
    FORM_SUBMISSION = "form_submission"
    CONFIRMED_ACCEPTANCE = "confirmed_acceptance"
    VIEW_STAY_REQUEST_BUTTON = "view_stay_request_button"
    LEGACY = "legacy"
    OUTSIDE_STAY_REQUEST = "outside_stay_request"
    EXISTING_CONVERSATION = "existing_conversation"
    MANUAL_RECONCILIATION = "manual_reconciliation"


# ---------------------------------------------------------------------------
# Модель подтверждения (неподделываемый capability токен)
# ---------------------------------------------------------------------------

@dataclass
class ConfirmationCapability:
    """
    Неподделываемый токен подтверждения выпускаемый только ConfirmationValidator.

    mark_as_sent() принимает этот токен и проверяет:
    1. cap_token совпадает с hash, вычисленным из attempt_id + canonical_host_key + nonce.
    2. nonce хранится в БД и соответствует attempt_id.
    3. Токен не истек (ttl_seconds).
    4. Попытка ещё не переведена в состояние 'sent' (проверяется в mark_as_sent).

    Атрибуты:
        attempt_id: Идентификатор попытки отправки.
        canonical_host_key: Канонический ключ хоста.
        nonce: Случайный строка, связанная с attempt_id в БД.
        issued_at: Время выпуска токена (ISO 8601).
        ttl_seconds: Время жизни токена в секундах.
        cap_token: SHA256-хеш, связывающий все поля вместе.
        outcome: Исход диспетчеризации (должен быть CONFIRMED_SENT).
    evidence_type: Тип подтверждения, выданного валидатором.
    """
    attempt_id: str
    canonical_host_key: str
    nonce: str
    issued_at: str
    ttl_seconds: int
    cap_token: str
    outcome: DispatchOutcome = DispatchOutcome.CONFIRMED_SENT
    evidence_type: EvidenceType = EvidenceType.CONFIRMED_ACCEPTANCE

    def is_valid_format(self) -> bool:
        """Проверяет базовый формат токена (не подлинность, только структуру)."""
        return bool(self.attempt_id and self.canonical_host_key and self.nonce and self.cap_token)

    def compute_expected_token(self) -> str:
        """Вычисляет ожидаемый cap_token из полей токена."""
        material = f"{self.attempt_id}|{self.canonical_host_key}|{self.nonce}|{self.issued_at}|{self.ttl_seconds}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def is_expired(self) -> bool:
        """Проверяет, истек ли срок действия токена."""
        try:
            issued = datetime.fromisoformat(self.issued_at)
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=timezone.utc)
            expiry = issued + timedelta(seconds=self.ttl_seconds)
            return datetime.now(timezone.utc) > expiry
        except Exception:
            return True


@dataclass
class ConfirmationResult:
    """
    Результат валидации подтверждения.

    Атрибуты:
        outcome: Итоговый исход диспетчеризации.
        capability: ConfirmationCapability (только если outcome == CONFIRMED_SENT).
        evidence_type: Тип подтверждения.
        reason: Человекочитаемое описание причины.
        attributable_to_stay_request: Можно ли atribутировать подтверждение
            конкретной операции создания Stay Request.
        http_status: HTTP-код ответа, если применимо.
        response_url: URL ответа, если применимо.
        page_url: URL страницы в момент подтверждения, если применимо.
    """
    outcome: DispatchOutcome
    capability: Optional[ConfirmationCapability] = None
    evidence_type: EvidenceType = EvidenceType.NONE
    reason: str = ""
    attributable_to_stay_request: bool = False
    http_status: Optional[int] = None
    response_url: Optional[str] = None
    page_url: Optional[str] = None

    def is_confirmed(self) -> bool:
        return self.outcome == DispatchOutcome.CONFIRMED_SENT

    def is_rejected(self) -> bool:
        return self.outcome == DispatchOutcome.CONFIRMED_REJECTED

    def is_unknown(self) -> bool:
        return self.outcome in (
            DispatchOutcome.UNKNOWN_OUTCOME,
            DispatchOutcome.REQUIRES_RECONCILIATION,
        )


# ---------------------------------------------------------------------------
# Валидатор подтверждения
# ---------------------------------------------------------------------------

class ConfirmationValidator:
    """
    Строгий валидатор подтверждения отправки Stay Request Couchsurfing.

    Различает ответы, которые могут быть atribутированы операции создания
    Stay Request, от общих ответов API, навигации и других побочных явлений.

    Токен ConfirmationCapability выпускается ТОЛЬКО этим валидатором и только
    когда outcome == CONFIRMED_SENT. Каждый токен привязан к конкретному
    attempt_id и canonical_host_key.
    """

    DEFAULT_TTL_SECONDS = 300  # 5 минут на подтверждение после клика Send

    def __init__(self, db_file: Optional[str] = None):
        self.logger = logging.getLogger("DispatchConfirmation.Validator")
        self.db_file = db_file

    # ------------------------------------------------------------------
    # Точка входа: валидация подтверждения
    # ------------------------------------------------------------------

    def validate(
        self,
        api_responses: List[Dict[str, Any]],
        page_url: Optional[str] = None,
        page_text: Optional[str] = None,
        dom_selectors: Optional[List[str]] = None,
        send_button_clicked: bool = False,
        attempt_id: Optional[str] = None,
        canonical_host_key: Optional[str] = None,
    ) -> ConfirmationResult:
        """
        Валидирует, было ли подтверждено создание Stay Request.

        Args:
            api_responses: Список перехваченных сетевых ответов (из Playwright).
            page_url: URL страницы в момент проверки.
            page_text: Текст страницы (innerText).
            dom_selectors: Найденные DOM-селекторы.
            send_button_clicked: Был ли выполнен клик по кнопке отправки.
            attempt_id: Идентификатор попытки (требуется для выпуска capability).
            canonical_host_key: Канонический ключ хоста (требуется для выпуска capability).

        Возвращает ConfirmationResult.
        """
        self.logger.debug(
            "Validating confirmation: %d API responses, page_url=%s, send_clicked=%s",
            len(api_responses), page_url, send_button_clicked,
        )

        # Если Send не был нажат, отправка не началась.
        if not send_button_clicked:
            return ConfirmationResult(
                outcome=DispatchOutcome.NOT_STARTED,
                evidence_type=EvidenceType.NONE,
                reason="Send button was not clicked; no dispatch attempted",
                attributable_to_stay_request=False,
            )

        # Couchsurfing replaces the send form with a visible "View Stay Request"
        # control after accepting the request. This signal is only collected from
        # a visible button/link after the current attempt clicked Send Request.
        # It is stronger and more specific than generic success text or HTTP 2xx.
        if self._has_view_stay_request_evidence(dom_selectors or []):
            capability = None
            if attempt_id and canonical_host_key:
                try:
                    capability = self.issue_capability(
                        attempt_id=attempt_id,
                        canonical_host_key=canonical_host_key,
                        evidence_type=EvidenceType.VIEW_STAY_REQUEST_BUTTON,
                    )
                except Exception:
                    self.logger.exception(
                        "Could not issue capability for visible View Stay Request evidence"
                    )
            if capability is not None:
                return ConfirmationResult(
                    outcome=DispatchOutcome.CONFIRMED_SENT,
                    capability=capability,
                    evidence_type=EvidenceType.VIEW_STAY_REQUEST_BUTTON,
                    reason="Visible View Stay Request button appeared after Send Request was clicked",
                    attributable_to_stay_request=True,
                    page_url=page_url,
                )
            return ConfirmationResult(
                outcome=DispatchOutcome.UNKNOWN_OUTCOME,
                evidence_type=EvidenceType.VIEW_STAY_REQUEST_BUTTON,
                reason=(
                    "View Stay Request appeared after click, but the attempt could not "
                    "be cryptographically tied to a dispatch reservation"
                ),
                attributable_to_stay_request=False,
                page_url=page_url,
            )

        # URL-ключевое слово, HTTP 2xx и общий текст сами по себе не подтверждают
        # отправку. Явное UI-доказательство View Stay Request обработано выше.

        # 1. Ищем ответы, связанные с созданием Stay Request. Одного URL
        # совпадения или HTTP 2xx недостаточно без подтверждения из интерфейса.
        stay_request_evidence = self._find_stay_request_response(api_responses)
        if stay_request_evidence:
            # F2: Даже если найден ответ с ключевыми словами Stay Request,
            # HTTP 2xx недостаточно. Возвращаем UNKNOWN_OUTCOME.
            self.logger.warning(
                "Stay Request URL keyword matched but HTTP 2xx is insufficient evidence; "
                "outcome set to UNKNOWN_OUTCOME"
            )
            return ConfirmationResult(
                outcome=DispatchOutcome.UNKNOWN_OUTCOME,
                evidence_type=EvidenceType.OUTSIDE_STAY_REQUEST,
                reason=(
                    "HTTP 2xx response with Stay Request URL keyword is not sufficient "
                    "evidence of acceptance; outcome unknown"
                ),
                attributable_to_stay_request=False,
                http_status=stay_request_evidence.get("http_status"),
                response_url=stay_request_evidence.get("response_url"),
                page_url=page_url,
            )

        # 2. Проверяем, есть ли только не-Stay-Request ответы.
        non_sr_responses = self._find_non_stay_request_responses(api_responses)
        if non_sr_responses:
            self.logger.warning(
                "Found non-Stay-Request responses only; cannot confirm Stay Request acceptance"
            )
            return ConfirmationResult(
                outcome=DispatchOutcome.UNKNOWN_OUTCOME,
                evidence_type=EvidenceType.OUTSIDE_STAY_REQUEST,
                reason=(
                    f"Received {len(non_sr_responses)} response(s) unrelated to Stay Request "
                    "creation; acceptance cannot be established"
                ),
                attributable_to_stay_request=False,
                http_status=non_sr_responses[0].get("http_status"),
                response_url=non_sr_responses[0].get("response_url"),
                page_url=page_url,
            )

        # 3. Навигация в чат — не является подтверждением.
        if page_url and self._is_chat_navigation(page_url):
            self.logger.warning(
                "Chat navigation detected but no Stay Request confirmation evidence"
            )
            return ConfirmationResult(
                outcome=DispatchOutcome.UNKNOWN_OUTCOME,
                evidence_type=EvidenceType.NAVIGATION,
                reason=(
                    f"Navigation to chat page ({page_url}) detected, but no "
                    "attributable Stay Request acceptance evidence"
                ),
                attributable_to_stay_request=False,
                page_url=page_url,
            )

        # 4. DOM-индикаторы успеха — не являются подтверждением без ответного доказательства.
        dom_success = self._check_dom_success(dom_selectors or [], page_text or "")
        if dom_success:
            self.logger.warning(
                "DOM success indicator found but no attributable Stay Request response"
            )
            return ConfirmationResult(
                outcome=DispatchOutcome.UNKNOWN_OUTCOME,
                evidence_type=EvidenceType.FORM_SUBMISSION,
                reason=(
                    f"DOM success indicator detected ({dom_success}) but no "
                    "attributable Stay Request API response; outcome unknown"
                ),
                attributable_to_stay_request=False,
                page_url=page_url,
            )

        # 5. Нет никаких подтверждающих доказательств — UNKNOWN_OUTCOME (fail-closed).
        return ConfirmationResult(
            outcome=DispatchOutcome.UNKNOWN_OUTCOME,
            evidence_type=EvidenceType.NONE,
            reason=(
                "No attributable evidence of Stay Request acceptance; "
                "outcome is unknown"
            ),
            attributable_to_stay_request=False,
            page_url=page_url,
        )

    # ------------------------------------------------------------------
    # Выпуск capability токена (только для CONFIRMED_SENT)
    # ------------------------------------------------------------------

    def issue_capability(
        self,
        attempt_id: str,
        canonical_host_key: str,
        ttl_seconds: Optional[int] = None,
        evidence_type: EvidenceType = EvidenceType.CONFIRMED_ACCEPTANCE,
    ) -> Optional[ConfirmationCapability]:
        """
        Выпускает неподделываемый ConfirmationCapability токен.

        Вызывается только когда outcome == CONFIRMED_SENT и есть верифицированное
        доказательство принятия Stay Request.

        Args:
            attempt_id: Идентификатор попытки отправки.
            canonical_host_key: Канонический ключ хоста.
            ttl_seconds: Время жизни токена (по умолчанию DEFAULT_TTL_SECONDS).

        Возвращает ConfirmationCapability или None если параметры невалидны.
        """
        if not attempt_id or not canonical_host_key:
            return None

        if ttl_seconds is None:
            ttl_seconds = self.DEFAULT_TTL_SECONDS

        nonce = secrets.token_hex(16)
        issued_at = datetime.now(timezone.utc).isoformat()

        material = f"{attempt_id}|{canonical_host_key}|{nonce}|{issued_at}|{ttl_seconds}"
        cap_token = hashlib.sha256(material.encode("utf-8")).hexdigest()

        # Сохраняем capability в БД для последующей проверки в mark_as_sent()
        self._store_capability(
            attempt_id=attempt_id,
            canonical_host_key=canonical_host_key,
            nonce=nonce,
            cap_token=cap_token,
            issued_at=issued_at,
            ttl_seconds=ttl_seconds,
        )

        return ConfirmationCapability(
            attempt_id=attempt_id,
            canonical_host_key=canonical_host_key,
            nonce=nonce,
            issued_at=issued_at,
            ttl_seconds=ttl_seconds,
            cap_token=cap_token,
            outcome=DispatchOutcome.CONFIRMED_SENT,
            evidence_type=evidence_type,
        )

    @staticmethod
    def _has_view_stay_request_evidence(dom_selectors: List[str]) -> bool:
        """True only for the explicit marker emitted for a visible UI control."""
        return "visible_view_stay_request_button" in dom_selectors

    # ------------------------------------------------------------------
    # Проверка capability токена (используется mark_as_sent())
    # ------------------------------------------------------------------

    def verify_capability(
        self,
        capability: ConfirmationCapability,
    ) -> bool:
        """
        Проверяет подлинность ConfirmationCapability токена.

        Проверяет:
        1. cap_token совпадает с вычисленным из полей токена.
        2. Токен не истек.
        3. attempt_id и canonical_host_key соответствуют записи в БД.
        4. nonce совпадает с сохранённым в БД.

        Возвращает True если токен валиден.
        """
        if not capability or not capability.is_valid_format():
            self.logger.warning("Invalid ConfirmationCapability format")
            return False

        # Проверка формата токена
        expected_token = capability.compute_expected_token()
        if capability.cap_token != expected_token:
            self.logger.warning("cap_token mismatch; possible forgery attempt")
            return False

        # Проверка срока действия
        if capability.is_expired():
            self.logger.warning("ConfirmationCapability expired")
            return False

        # Проверка в БД
        return self._verify_capability_in_db(
            attempt_id=capability.attempt_id,
            canonical_host_key=capability.canonical_host_key,
            nonce=capability.nonce,
            cap_token=capability.cap_token,
        )

    def invalidate_capability(self, attempt_id: str) -> None:
        """Помечает capability как использованный (предотвращает повторное использование)."""
        self._mark_capability_used(attempt_id)

    # ------------------------------------------------------------------
    # Внутренние методы валидации (F2 — удалено URL-based inference)
    # ------------------------------------------------------------------

    def _find_stay_request_response(
        self, api_responses: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """
        Ищет ответы, связанные с Stay Request.

        URL ключевые слова и HTTP 2xx сами по себе не являются достаточными
        доказательствами. Метод возвращает найденный ответ для диагностики.

        Возвращает словарь с деталями или None.
        """
        for resp in api_responses:
            method = resp.get("method", "").upper()
            if method not in ("POST", "PUT"):
                continue

            url = resp.get("url", "") or ""
            url_lower = url.lower()
            status = resp.get("status", 0)
            body = resp.get("body", "") or ""

            # F2: Не используем HTTP 2xx или URL ключевые слова как условие успеха.
            # Просто собираем ответы для диагностики.
            has_sr_keyword = any(k in url_lower for k in (
                "stay_request", "stay-request", "stayrequest",
                "host_request", "host-request", "hostrequest",
            ))
            if not has_sr_keyword:
                continue

            return {
                "http_status": status,
                "response_url": url,
                "method": method,
                "response_body_fragment": body[:500] if body else None,
            }

        return None

    def _find_non_stay_request_responses(
        self, api_responses: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Находит ответы, не связанные с созданием Stay Request."""
        non_sr = []
        for resp in api_responses:
            method = resp.get("method", "").upper()
            if method not in ("POST", "PUT"):
                continue

            url = resp.get("url", "") or ""
            url_lower = url.lower()
            status = resp.get("status", 0)

            if not (200 <= status <= 299):
                continue

            has_sr_keyword = any(k in url_lower for k in (
                "stay_request", "stay-request", "stayrequest",
                "host_request", "host-request", "hostrequest",
            ))
            if has_sr_keyword:
                continue

            has_non_sr_keyword = any(k in url_lower for k in (
                "/messages/", "/conversations/", "/c/chat/",
                "/c/messages/", "/messages/thread/",
                "graphql", "/api/", "profile", "dashboard", "search",
            ))
            if not has_non_sr_keyword:
                continue

            non_sr.append({
                "http_status": status,
                "response_url": url,
                "method": method,
                "response_body_fragment": str(resp.get("body", "") or "")[:500],
            })

        return non_sr

    def _is_chat_navigation(self, page_url: str) -> bool:
        """Проверяет, является ли URL навигацией в чат/диалог."""
        url_lower = page_url.lower()
        return any(k in url_lower for k in ("/c/chat/", "/c/messages/", "/messages/thread/"))

    def _check_dom_success(
        self, dom_selectors: List[str], page_text: str
    ) -> Optional[str]:
        """Проверяет наличие DOM-индикаторов успеха (не является доказательством)."""
        for selector in dom_selectors:
            if selector and ("stay-request-sent" in selector or "request-sent" in selector):
                return f"DOM selector: {selector}"

        text_lower = page_text.lower()
        for marker in ("request sent", "your request was sent", "запрос отправлен"):
            if marker in text_lower:
                return f"Text marker: {marker}"

        return None

    # ------------------------------------------------------------------
    # Capability storage (SQLite)
    # ------------------------------------------------------------------

    def _store_capability(
        self,
        attempt_id: str,
        canonical_host_key: str,
        nonce: str,
        cap_token: str,
        issued_at: str,
        ttl_seconds: int,
    ) -> None:
        """Сохраняет capability в БД для последующей проверки."""
        from core.database import get_connection

        expires_at = (
            datetime.fromisoformat(issued_at)
            .replace(tzinfo=timezone.utc)
            + __import__("datetime").timedelta(seconds=ttl_seconds)
        ).isoformat()

        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS confirmation_capabilities (
                    attempt_id TEXT PRIMARY KEY,
                    canonical_host_key TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    cap_token TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                """
            )
            cur.execute(
                """
                INSERT OR REPLACE INTO confirmation_capabilities
                (attempt_id, canonical_host_key, nonce, cap_token, issued_at,
                 expires_at, used, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?);
                """,
                (
                    attempt_id,
                    canonical_host_key,
                    nonce,
                    cap_token,
                    issued_at,
                    expires_at,
                    issued_at,
                ),
            )
            conn.commit()

    def _verify_capability_in_db(
        self,
        attempt_id: str,
        canonical_host_key: str,
        nonce: str,
        cap_token: str,
    ) -> bool:
        """Проверяет capability в БД."""
        from core.database import get_connection

        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT canonical_host_key, nonce, cap_token, used, expires_at
                FROM confirmation_capabilities
                WHERE attempt_id = ?;
                """,
                (attempt_id,),
            )
            row = cur.fetchone()
            if not row:
                self.logger.warning(f"No capability record for attempt_id={attempt_id}")
                return False

            stored_key = str(row["canonical_host_key"])
            stored_nonce = str(row["nonce"])
            stored_token = str(row["cap_token"])
            used = int(row["used"] or 0)
            expires_at = str(row["expires_at"])

            if used:
                self.logger.warning(f"Capability for attempt_id={attempt_id} already used")
                return False

            if stored_key != canonical_host_key:
                self.logger.warning("canonical_host_key mismatch")
                return False

            if stored_nonce != nonce:
                self.logger.warning("nonce mismatch")
                return False

            if stored_token != cap_token:
                self.logger.warning("cap_token mismatch in DB")
                return False

            # Проверка срока действия
            try:
                expiry = datetime.fromisoformat(expires_at)
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expiry:
                    self.logger.warning("Capability expired in DB")
                    return False
            except Exception:
                return False

            return True

    def _mark_capability_used(self, attempt_id: str) -> None:
        """Помечает capability как использованный."""
        from core.database import get_connection

        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE confirmation_capabilities
                SET used = 1
                WHERE attempt_id = ?;
                """,
                (attempt_id,),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Фасад-функции
# ---------------------------------------------------------------------------

_validator_instance: Optional[ConfirmationValidator] = None


def get_confirmation_validator(
    db_file: Optional[str] = None,
) -> ConfirmationValidator:
    """Возвращает экземпляр ConfirmationValidator."""
    global _validator_instance
    if _validator_instance is None or (db_file is not None and _validator_instance.db_file != db_file):
        _validator_instance = ConfirmationValidator(db_file=db_file)
    return _validator_instance


def validate_dispatch_confirmation(
    api_responses: List[Dict[str, Any]],
    page_url: Optional[str] = None,
    page_text: Optional[str] = None,
    dom_selectors: Optional[List[str]] = None,
    send_button_clicked: bool = False,
    attempt_id: Optional[str] = None,
    canonical_host_key: Optional[str] = None,
    db_file: Optional[str] = None,
) -> ConfirmationResult:
    """
    Удобная функция для валидации подтверждения отправки Stay Request.
    """
    validator = get_confirmation_validator(db_file=db_file)
    return validator.validate(
        api_responses=api_responses,
        page_url=page_url,
        page_text=page_text,
        dom_selectors=dom_selectors,
        send_button_clicked=send_button_clicked,
        attempt_id=attempt_id,
        canonical_host_key=canonical_host_key,
    )
