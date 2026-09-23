import sys
sys.path.insert(0, ".")
import pytest
from bot.services.pricing import get_price_for_role, calculate_editor_price, format_cents_as_dollars, invalidate_cache

@pytest.fixture(autouse=True)
def clear_cache():
    invalidate_cache()

def test_tl_flat():
    assert get_price_for_role("TL") == 85

def test_pr_flat():
    assert get_price_for_role("PR") == 85

def test_ed_under_30():
    assert calculate_editor_price(10) == 150
    assert calculate_editor_price(29) == 150

def test_ed_30_44():
    assert calculate_editor_price(30) == 200
    assert calculate_editor_price(44) == 200

def test_ed_45_plus():
    assert calculate_editor_price(45) == 300
    assert calculate_editor_price(100) == 300

def test_format():
    assert format_cents_as_dollars(85) == "$0.85"
    assert format_cents_as_dollars(150) == "$1.50"
    assert format_cents_as_dollars(300) == "$3.00"

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("All pricing tests passed")
