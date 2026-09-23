import os
import sys
sys.path.insert(0, ".")
os.environ.setdefault("DISCORD_TOKEN", "dummy_token_for_ci")
os.environ.setdefault("TEAM_LEADER_ROLE_ID", "100")
os.environ.setdefault("ADMIN_ROLE_ID", "200")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("GUILD_ID", "123456789012345678")
from bot.extensions.admin import extract_user_ids

def test_mention():
    assert extract_user_ids("<@123456789012345678>") == ["123456789012345678"]
    assert extract_user_ids("<@!123456789012345678>") == ["123456789012345678"]

def test_multiple():
    assert set(extract_user_ids("<@111111111111111111> <@222222222222222222>")) == {"111111111111111111", "222222222222222222"}

def test_long_id():
    assert extract_user_ids("123456789012345678") == ["123456789012345678"]

def test_ignores_short():
    ids = extract_user_ids("123456789012345678 999")
    assert "123456789012345678" in ids
    assert "999" not in ids

def test_empty():
    assert extract_user_ids("") == []
    assert extract_user_ids(None) == []

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("All extract tests passed")
