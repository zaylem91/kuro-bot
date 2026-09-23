import sys
sys.path.insert(0, ".")
from decimal import Decimal
from bot.services.drive import extract_drive_id, extract_chapter_number

def test_raw_id():
    assert extract_drive_id("1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345") == "1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345"

def test_folders_link():
    assert extract_drive_id("https://drive.google.com/drive/folders/1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345") == "1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345"

def test_folders_with_query():
    assert extract_drive_id("https://drive.google.com/drive/folders/1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345?usp=sharing") == "1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345"

def test_u0_folders():
    assert extract_drive_id("https://drive.google.com/drive/u/0/folders/1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345") == "1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345"

def test_open_id():
    assert extract_drive_id("https://drive.google.com/open?id=1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345") == "1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345"

def test_file_d():
    assert extract_drive_id("https://drive.google.com/file/d/1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345/view?usp=sharing") == "1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345"

def test_uc_id():
    assert extract_drive_id("https://drive.google.com/uc?id=1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345&export=download") == "1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345"

def test_with_spaces_and_brackets():
    assert extract_drive_id("  <https://drive.google.com/drive/folders/1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345>  ") == "1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345"

def test_docs_d():
    assert extract_drive_id("https://docs.google.com/document/d/1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345/edit") == "1a2B3cDeFgHiJkLmNoPqRsTuVwXyZ012345"

def test_invalid():
    try:
        extract_drive_id("not a link")
        assert False, "should raise"
    except ValueError:
        pass

def test_short():
    try:
        extract_drive_id("https://drive.google.com/drive/folders/short")
        assert False
    except ValueError:
        pass

def test_extract_chapter_number():
    assert extract_chapter_number("Chapter 10 (v2)") == Decimal("10")
    assert extract_chapter_number("Chapter 10v2") == Decimal("10")
    assert extract_chapter_number("Chapter 10_v2") == Decimal("10")
    assert extract_chapter_number("Chapter 10-v2") == Decimal("10")
    assert extract_chapter_number("Bonus Chapter 4.5v2") == Decimal("4.5")
    assert extract_chapter_number("Bonus Chapter 4.5_v2") == Decimal("4.5")
    assert extract_chapter_number("Ch 10 [1080p]") == Decimal("10")
    assert extract_chapter_number("Ch. 10.5") == Decimal("10.5")
    assert extract_chapter_number("c12") == Decimal("12")
    assert extract_chapter_number("015") == Decimal("15")
    assert extract_chapter_number("10v2") == Decimal("10")
    assert extract_chapter_number("10_v2") == Decimal("10")
    assert extract_chapter_number("4.5v2") == Decimal("4.5")
    assert extract_chapter_number("Solo Leveling - 150") == Decimal("150")
    assert extract_chapter_number("Season 2 Ch 10") == Decimal("10")
    assert extract_chapter_number("Vol. 2 Ch. 15") == Decimal("15")
    assert extract_chapter_number("Vol 1 Chapter 20 (v2)") == Decimal("20")
    assert extract_chapter_number("[Flame] Title - Ch. 99 [v3]") == Decimal("99")
    assert extract_chapter_number("Episode 12") == Decimal("12")
    assert extract_chapter_number("Ep. 15.5v2") == Decimal("15.5")
    assert extract_chapter_number("10.5_v2") == Decimal("10.5")
    assert extract_chapter_number("Chapter 10 (End)") == Decimal("10")
    assert extract_chapter_number("10v03") == Decimal("10")
    assert extract_chapter_number("no_numbers_here") is None

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("All drive extract tests passed")
