"""
core/selector.py - Модуль СЕЛЕКТОР (Like / Pass / VIP).
Отвечает за:
1. Выдачу ранжированных хостов (1-5★) из Коллектора.
2. Быстрый отбор гостем: Like (в очередь), VIP (приоритет), Pass (отклонить).
3. Перевод состояния хоста: discovered -> selected / rejected (с синхронизацией sending_queue).
4. Пакетные правила (например: отобрать всех 4-5★).
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.database import get_connection, log_audit
from core.models import HostCard, HostState


class SelectorEngine:
    """Движок отбора хостов для поездки."""

    def __init__(self, trip_id: str, db_file: Optional[Path] = None):
        self.trip_id = trip_id
        self.db_file = db_file

    def list_candidates(
        self,
        min_stars: int = 1,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Возвращает ранжированных кандидатов в статусе 'discovered'."""
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT host_id, username, name, location, gender, age, occupation,
                       about, interests_json, languages_json, countries_json,
                       profile_url, avatar_url, score_stars, score_reasons_json, state,
                       is_verified, verification_status, recently_active_status,
                       last_active_raw, last_login_days, last_active_at, priority,
                       references_count, birth_country, residence_country, is_hosting,
                       household, special_notes, countries_lived_json,
                       (SELECT raw_data_json FROM core_host_snapshots s
                        WHERE s.trip_id=core_hosts.trip_id AND s.host_id=core_hosts.host_id
                        ORDER BY s.captured_at DESC LIMIT 1) AS profile_snapshot_json
                FROM core_hosts
                WHERE trip_id = ? AND state = 'discovered' AND score_stars >= ?
                ORDER BY CASE WHEN recently_active_status='ACTIVE' THEN 0
                              WHEN recently_active_status='RECENTLY_ACTIVE'
                               AND activity_checked_at >= datetime('now','-30 days') THEN 1 ELSE 2 END,
                         CASE WHEN last_login_days IS NULL THEN 2147483647 ELSE last_login_days END,
                         priority DESC, score_stars DESC, created_at DESC
                LIMIT ? OFFSET ?;
                """,
                (self.trip_id, min_stars, limit, offset),
            )
            rows = cur.fetchall()

            results = []
            for r in rows:
                r_keys = r.keys()
                try:
                    profile_snapshot = json.loads(r["profile_snapshot_json"] or "{}")
                    if not isinstance(profile_snapshot, dict):
                        profile_snapshot = {}
                except (TypeError, ValueError):
                    profile_snapshot = {}
                results.append({
                    "host_id": r["host_id"],
                    "id": r["host_id"],
                    "username": r["username"],
                    "name": r["name"],
                    "location": r["location"],
                    "gender": r["gender"],
                    "age": r["age"],
                    "occupation": r["occupation"],
                    "about": r["about"],
                    "interests": json.loads(r["interests_json"] or "[]"),
                    "languages": json.loads(r["languages_json"] or "[]"),
                    "countries": json.loads(r["countries_json"] or "[]"),
                    "countries_lived": json.loads(r["countries_lived_json"] or "[]") if "countries_lived_json" in r_keys else [],
                    "profile_url": r["profile_url"],
                    "avatar_url": r["avatar_url"],
                    "is_verified": bool(r["is_verified"]) if "is_verified" in r_keys else False,
                    "verification_status": r["verification_status"] if "verification_status" in r_keys else "UNKNOWN",
                    "recently_active_status": r["recently_active_status"] if "recently_active_status" in r_keys else "UNKNOWN",
                    "last_active_raw": r["last_active_raw"] if "last_active_raw" in r_keys else None,
                    "last_login_days": r["last_login_days"] if "last_login_days" in r_keys else None,
                    "last_active_at": r["last_active_at"] if "last_active_at" in r_keys else None,
                    "priority": r["priority"] if "priority" in r_keys else 10,
                    "references_count": r["references_count"] if "references_count" in r_keys else 0,
                    "profile_snapshot": profile_snapshot,
                    "birth_country": r["birth_country"] if "birth_country" in r_keys else None,
                    "residence_country": r["residence_country"] if "residence_country" in r_keys else None,
                    "is_hosting": r["is_hosting"] if "is_hosting" in r_keys else "unknown",
                    "household": r["household"] if "household" in r_keys else None,
                    "special_notes": r["special_notes"] if "special_notes" in r_keys else None,
                    "score_stars": r["score_stars"],
                    "score_reasons": json.loads(r["score_reasons_json"] or "[]"),
                    "state": r["state"],
                })
            return results

    def decide(self, host_id: str, decision: str) -> bool:
        """
        Устанавливает решение по хосту:
        - 'like': перевод в selected
        - 'vip': перевод в selected с пометкой VIP
        - 'pass': перевод в rejected (скрывается, но не удаляется)
        Инвариант: никогда не перезаписывает хоста со статусом 'sent'.
        """
        decision_clean = decision.strip().lower()
        if decision_clean in ("like", "vip"):
            new_state = HostState.SELECTED
            q_status = "Queued"
        elif decision_clean == "pass":
            new_state = HostState.REJECTED
            q_status = "Rejected"
        else:
            raise ValueError(f"Unknown decision '{decision}'. Use 'like', 'vip', or 'pass'.")

        now_iso = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE core_hosts
                SET state = ?, updated_at = ?
                WHERE trip_id = ? AND (host_id = ? OR username = ?) AND state != 'sent';
                """,
                (new_state.value, now_iso, self.trip_id, host_id, host_id),
            )
            affected = cur.rowcount > 0

        if affected:
            log_audit(
                trip_id=self.trip_id,
                module="SELECTOR",
                event=f"DECISION_{decision_clean.upper()}",
                host_id=host_id,
                details={"new_state": new_state.value},
                db_file=self.db_file,
            )
        return affected

    def batch_decide(self, host_ids: List[str], decision: str) -> int:
        """Пакетная установка решения для списка хостов."""
        count = 0
        for hid in host_ids:
            if self.decide(hid, decision):
                count += 1
        return count

    def auto_select_high_match(self, min_stars: int = 4) -> int:
        """Автоматически переводит всех кандидатов с высоким рейтингом (4-5★) в selected."""
        candidates = self.list_candidates(min_stars=min_stars, limit=1000)
        hids = [c["host_id"] for c in candidates]
        return self.batch_decide(hids, "like")
