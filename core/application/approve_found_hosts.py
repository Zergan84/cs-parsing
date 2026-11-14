"""ApproveFoundHosts application use case."""

from typing import Any, Callable, Dict, List, Optional

from core.identity import verify_host_name
from core.messaging import build_personalized_message
from core.models import HostState
from core.repositories.host_queue_repository import HostQueueRepository
from core.host_policy import evaluate_host_eligibility


class TripNotFoundError(ValueError):
    pass


class ApproveFoundHosts:
    """Generate, validate, and approve all eligible found hosts in one trip."""

    def __init__(self, repository: Optional[HostQueueRepository] = None):
        self.repository = repository or HostQueueRepository()

    def execute(self, trip_id: Optional[str] = None) -> Dict[str, Any]:
        resolved_trip_id = self.repository.resolve_trip(trip_id)
        if not resolved_trip_id:
            raise ValueError("Select a trip before approving discovered hosts")
        trip, hosts = self.repository.get_trip_and_found_hosts(resolved_trip_id)
        if not trip:
            raise TripNotFoundError(f"Trip not found: {resolved_trip_id}")

        prepared: List[Dict[str, str]] = []
        failures: List[Dict[str, str]] = []
        neutral_count = 0
        already_approved_count = 0
        from core.approval_service import ApprovalService
        approval_service = ApprovalService(self.repository.db_file)
        for host in hosts:
            username = host.get("username") or host.get("host_id") or ""
            try:
                source_state = HostState(host["state"])
            except (ValueError, KeyError):
                failures.append({"username": username, "reason": "Unknown host state; manual review required."})
                continue
            if source_state == HostState.APPROVED:
                existing_draft = host.get("message_draft") or ""
                existing = approval_service.validate_approval(
                    trip_id=resolved_trip_id,
                    host_id=host["host_id"],
                    username=username,
                    message_text=existing_draft,
                    message_version=host.get("message_version"),
                ) if existing_draft else None
                if existing and existing.valid:
                    already_approved_count += 1
                    continue
            elif not source_state.can_transition_to(HostState.APPROVED):
                failures.append({"username": username, "reason": f"Cannot approve from state '{source_state.value}'."})
                continue
            eligibility = evaluate_host_eligibility(
                host,
                arrival_date=trip.get("arrival_date"),
                departure_date=trip.get("departure_date"),
                guest_gender=host.get("guest_gender") or "male",
                target_city=trip.get("city"),
            )
            if not eligibility["eligible"]:
                self.repository.reject_ineligible(trip_id, host["host_id"], eligibility["reason"], eligibility)
                failures.append({"username": username, "reason": eligibility["reason"]})
                continue

            _, trusted_name = verify_host_name(host)
            if not trusted_name:
                host["_neutral_greeting"] = True
            message, qc_passed, qc_notes = build_personalized_message(host, trip)
            if not qc_passed:
                failures.append({"username": username, "reason": qc_notes})
                continue

            prepared.append({
                "host_id": host["host_id"],
                "message": message,
                "audit_marker": "BULK_APPROVED" if trusted_name else "BULK_APPROVED_NEUTRAL_GREETING",
            })
            neutral_count += int(not trusted_name)

        approved_count = self.repository.approve_prepared(
            trip_id=resolved_trip_id,
            prepared=prepared,
            considered_count=len(hosts),
            failed_count=len(failures),
            neutral_greeting_count=neutral_count,
        )
        return {
            "approved_count": approved_count,
            "considered_count": len(hosts) - already_approved_count,
            "already_approved_count": already_approved_count,
            "neutral_greeting_count": neutral_count,
            "failed_count": len(failures) + len(prepared) - approved_count,
            "failures": failures[:100],
            "trip_id": resolved_trip_id,
        }
