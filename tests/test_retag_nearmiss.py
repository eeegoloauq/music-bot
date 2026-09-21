"""Retag dry-run says *why* an album is "not found" when a hit came close."""

import retagger


def _cand(artist, title, nb, aid="1"):
    return {"id": aid, "artist": {"name": artist}, "title": title, "nb_tracks": nb}


def _words(s):
    return retagger._significant_words(s)


def test_edition_mismatch_is_reported():
    note = retagger._near_miss(_cand("Kedr Livanskiy", "Ariadna", 9),
                               _words("Kedr Livanskiy"), _words("Ariadna"), 16)
    assert note == "track count differs: 9 on Deezer vs 16 here (Kedr Livanskiy — Ariadna)"


def test_other_artist_is_reported():
    note = retagger._near_miss(_cand("Bby Goyard", "LORESEEKER", 10),
                               _words("DJ Smokey"), _words("LORESEEKER"), 10)
    assert note == "found as Bby Goyard — LORESEEKER (10 tracks)"


def test_other_artist_beats_track_count():
    note = retagger._near_miss(_cand("Someone Else", "LORESEEKER", 4),
                               _words("DJ Smokey"), _words("LORESEEKER"), 10)
    assert note.startswith("found as Someone Else")


def test_deluxe_title_still_counts_as_near_miss():
    note = retagger._near_miss(_cand("Kedr Livanskiy", "Ariadna", 9),
                               _words("Kedr Livanskiy"), _words("Ariadna (Deluxe)"), 16)
    assert note and note.startswith("track count differs")


def test_unrelated_title_has_no_note():
    assert retagger._near_miss(_cand("Dream Caster", "Book of Wood", 10),
                               _words("Dream Caster"), _words("woody"), 11) is None
    # a one-word folder name buried in a longer title is a coincidence
    assert retagger._near_miss(_cand("Waterbug", "fnl (feat. Woody)", 1),
                               _words("Dream Caster"), _words("woody"), 11) is None


def test_clean_hit_has_no_note():
    assert retagger._near_miss(_cand("Kedr Livanskiy", "Ariadna", 16),
                               _words("Kedr Livanskiy"), _words("Ariadna"), 16) is None
