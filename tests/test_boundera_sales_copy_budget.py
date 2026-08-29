from pathlib import Path
import runpy


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "boundera-sales"
    / "scripts"
    / "check_copy.py"
)
SCRIPT = runpy.run_path(str(SCRIPT_PATH))


def test_word_count_treats_booking_url_as_one_word() -> None:
    assert SCRIPT["word_count"]("Book here https://cal.com/example/path") == 3


def test_sentence_count_handles_punctuation_and_paragraphs() -> None:
    assert SCRIPT["sentence_count"]("One. Two?\n\nThree!") == 3


def test_sentence_count_ignores_salutation_and_time_abbreviations() -> None:
    copy = """Hi Jordan,

Good speaking today.

I can do Tuesday at 2 p.m. ET or Wednesday at 11 a.m. ET.

If neither works, would Thursday be easier?"""

    assert SCRIPT["sentence_count"](copy) == 3


def test_sentence_count_preserves_unpunctuated_paragraphs() -> None:
    assert SCRIPT["sentence_count"]("Thanks Jordan\n\nWould Tuesday work?") == 2


def test_connection_note_safe_ceiling() -> None:
    assert SCRIPT["evaluate"]("linkedin-connect", "x" * 200)[1] is False
    assert SCRIPT["evaluate"]("linkedin-connect", "x" * 201)[1] is True


def test_cold_email_ceiling_uses_words() -> None:
    assert SCRIPT["evaluate"]("email-cold", "word " * 100)[1] is False
    assert SCRIPT["evaluate"]("email-cold", "word " * 101)[1] is True
