"""Smart sprinkler relays: manual toggle, master switch and automatic activation (port of SprinklerService.cs)."""
from django.utils import timezone

from . import dto, events
from .models import SmartSprinkler, SprinklerMode


def _apply(s: SmartSprinkler, is_active: bool | None, mode: str | None, actor: str, reason: str | None) -> bool:
    status_changed = is_active is not None and is_active != s.is_active
    mode_changed = mode is not None and mode != s.mode
    if not status_changed and not mode_changed:
        return False
    if mode_changed:
        s.mode = mode
    if status_changed:
        s.is_active = is_active
        if s.is_active:
            s.last_triggered_at = timezone.now()
    s.updated_at = timezone.now()
    s.save()

    events.publish("sprinkler", dto.sprinkler(s))
    why = f" — {reason}" if reason else ""
    if status_changed:
        events.activity("warning" if s.is_active else "info",
                        f"Sprinkler {s.id} {'ACTIVATED' if s.is_active else 'deactivated'} by {actor}{why}", s.zone)
    else:
        events.activity("info", f"Sprinkler {s.id} set to {s.mode} mode by {actor}", s.zone)
    return True


def toggle(s: SmartSprinkler, is_active: bool | None, mode: str | None, actor: str) -> None:
    if is_active is None and mode is None:
        is_active = not s.is_active  # empty body = flip
    _apply(s, is_active, mode, actor, None)


def set_all(is_active: bool, zone: str | None, actor: str, reason: str) -> list[str]:
    query = SmartSprinkler.objects.all()
    if zone and zone.strip():
        query = query.filter(zone=zone)
    changed = [s.id for s in query if _apply(s, is_active, None, actor, reason)]
    where = zone if zone and zone.strip() else "all zones"
    events.activity("critical" if is_active else "info",
                    f"Master sprinkler switch → {'ON' if is_active else 'OFF'} ({where}, {len(changed)} relay(s)) by {actor}", zone)
    return changed


def auto_activate_zone(zone: str, reason: str) -> list[str]:
    targets = SmartSprinkler.objects.filter(zone=zone, mode=SprinklerMode.AUTO, is_active=False)
    return [s.id for s in targets if _apply(s, True, None, "auto-engine", reason)]
