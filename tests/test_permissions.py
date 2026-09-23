import os
import sys
sys.path.insert(0, ".")
os.environ.setdefault("DISCORD_TOKEN", "dummy_token_for_ci")
os.environ.setdefault("TEAM_LEADER_ROLE_ID", "100")
os.environ.setdefault("ADMIN_ROLE_ID", "200")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("GUILD_ID", "123456789012345678")
from unittest.mock import MagicMock

import bot.config as cfg

from bot.services.permissions import is_admin, is_team_leader

def make_member(role_ids):
    m = MagicMock()
    m.role_ids = role_ids
    import hikari
    m.permissions = hikari.Permissions.NONE
    return m

def test_team_leader():
    assert is_team_leader(make_member([cfg.TEAM_LEADER_ROLE_ID])) is True
    assert is_team_leader(make_member([cfg.ADMIN_ROLE_ID])) is False
    assert is_team_leader(None) is False

def test_admin():
    assert is_admin(make_member([cfg.ADMIN_ROLE_ID])) is True
    assert is_admin(make_member([cfg.TEAM_LEADER_ROLE_ID])) is True  # team leader inherits
    assert is_admin(make_member([999])) is False
    assert is_admin(None) is False

def test_is_admin_inherits():
    assert is_admin(make_member([cfg.TEAM_LEADER_ROLE_ID])) is True

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("All permissions tests passed")
