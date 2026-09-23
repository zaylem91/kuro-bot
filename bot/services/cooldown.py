"""Simple per-user cooldown to prevent Drive spam."""
import time

_cooldowns: dict[str, dict[str, float]] = {}
DEFAULT_COOLDOWN = 5.0

def _prune_expired(now: float | None = None) -> None:
    """Remove expired cooldown entries to prevent unbounded growth (called lazily)."""
    if now is None:
        now = time.time()
    # Keep entries only if still within the largest cooldown window (max 30s buffer)
    max_age = 3600  # 1 hour max per entry
    for cmd, users in list(_cooldowns.items()):
        for uid, ts in list(users.items()):
            if now - ts > max_age:
                users.pop(uid, None)
        if not users:
            _cooldowns.pop(cmd, None)

def check_cooldown(user_id: str | int, command: str, seconds: float = DEFAULT_COOLDOWN) -> tuple[bool, float]:
    """Return (is_on_cooldown, remaining_seconds). Updates timestamp if not on cooldown."""
    now = time.time()
    # Lazy prune every ~100 calls or when dict grows large
    if sum(len(v) for v in _cooldowns.values()) > 500 or int(now) % 300 == 0:
        _prune_expired(now)
    uid = str(user_id)
    last = _cooldowns.get(command, {}).get(uid, 0.0)
    elapsed = now - last
    if elapsed < seconds:
        return True, round(seconds - elapsed, 1)
    _cooldowns.setdefault(command, {})[uid] = now
    return False, 0.0

def reset_cooldown(user_id: str | int, command: str) -> None:
    uid = str(user_id)
    if command in _cooldowns:
        _cooldowns[command].pop(uid, None)
