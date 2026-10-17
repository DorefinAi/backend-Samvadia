from datetime import datetime, timezone

from app.services.filenames import extension_for, generated_name, kind_of, safe_name, unique_name


def test_safe_name_keeps_hindi_and_extension():
    assert safe_name("जयपुर बारिश  फोटो.jpg") == "जयपुर बारिश फोटो.jpg"


def test_safe_name_strips_folders_and_bad_characters():
    assert safe_name("C:\\Users\\x\\press:note?.pdf") == "pressnote.pdf"
    assert safe_name("../../etc/passwd") == "passwd"


def test_safe_name_shortens_but_keeps_extension():
    name = safe_name("a" * 300 + ".mp4")
    assert len(name) <= 120 and name.endswith(".mp4")


def test_generated_name_uses_india_time():
    at = datetime(2026, 10, 17, 5, 12, tzinfo=timezone.utc)  # 10:42 IST
    assert generated_name(at, 1, "video/mp4") == "20261017-1042_01.mp4"
    assert generated_name(at, 2, "image/jpeg") == "20261017-1042_02.jpg"


def test_unique_name():
    assert unique_name("IMG_0001.jpg", set()) == "IMG_0001.jpg"
    assert unique_name("IMG_0001.jpg", {"img_0001.JPG"}) == "IMG_0001_2.jpg"
    assert unique_name("README", {"README", "README_2"}) == "README_3"


def test_kind_and_extension():
    assert kind_of("audio/ogg; codecs=opus") == "audio"
    assert kind_of("application/pdf") == "document"
    assert extension_for("audio/ogg; codecs=opus") == ".ogg"
