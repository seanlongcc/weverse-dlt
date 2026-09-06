from scripts.weverse_output import write_title_file


def test_write_title_file_creates_plain_title_file(tmp_path):
    title_path = write_title_file(tmp_path, "  STAYC LIVE replay  ")

    assert title_path == tmp_path / "title.txt"
    assert title_path.read_text(encoding="utf-8") == "STAYC LIVE replay\n"


def test_write_title_file_preserves_unicode(tmp_path):
    write_title_file(tmp_path, "장재이 라이브")

    assert (tmp_path / "title.txt").read_text(encoding="utf-8") == "장재이 라이브\n"
