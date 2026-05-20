"""Tests for the player-name normalization helper."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dynasty.names import normalize


def test_suffix_stripping():
    assert normalize("Odell Beckham Jr.") == "odell beckham"
    assert normalize("Odell Beckham, Jr.") == "odell beckham"
    assert normalize("Odell Beckham JR") == "odell beckham"
    assert normalize("Kenneth Walker III") == "kenneth walker"
    assert normalize("Marvin Harrison Jr") == "marvin harrison"
    assert normalize("Brian Robinson Jr.") == "brian robinson"
    assert normalize("Travis Etienne Jr.") == "travis etienne"


def test_diacritics():
    assert normalize("Andre Patterson") == "andre patterson"
    assert normalize("Andre\u0301 Patterson") == "andre patterson"  # combining accent
    assert normalize("Bj\u00f6rn Andersson") == "bjorn andersson"


def test_apostrophes_kept():
    # D'Andre Swift -> kept apostrophe so we don't merge with "Dandre"
    assert normalize("D'Andre Swift") == "d'andre swift"


def test_no_false_positive_chops():
    # These names contain substrings that look like suffixes but aren't
    # at word boundary positions.
    assert normalize("Iverson") == "iverson"
    assert normalize("Vince Williams") == "vince williams"
    assert normalize("Justin Jefferson") == "justin jefferson"


def test_idempotent():
    n1 = normalize("Odell Beckham, Jr.")
    n2 = normalize(n1)
    assert n1 == n2 == "odell beckham"


def test_empty_inputs():
    assert normalize(None) is None
    assert normalize("") is None
    assert normalize("   ") is None


def main():
    test_suffix_stripping();         print("1. suffix stripping: \u2713")
    test_diacritics();               print("2. diacritics folded: \u2713")
    test_apostrophes_kept();         print("3. apostrophes preserved: \u2713")
    test_no_false_positive_chops();  print("4. no false-positive suffix chops: \u2713")
    test_idempotent();               print("5. idempotent: \u2713")
    test_empty_inputs();             print("6. empty/None handled: \u2713")
    print("\nAll name normalization tests passed.")


if __name__ == "__main__":
    main()
