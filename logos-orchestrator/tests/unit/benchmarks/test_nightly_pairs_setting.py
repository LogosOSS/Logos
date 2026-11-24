from logos.benchmarks.nightly import DEFAULT_PAIRS_PER_NIGHT, pairs_per_night


def test_the_default_admits_two_pairs():
    assert DEFAULT_PAIRS_PER_NIGHT == 2
    assert pairs_per_night(None) == 2
    assert pairs_per_night("") == 2


def test_a_deployment_can_admit_more_pairs():
    assert pairs_per_night("3") == 3
    assert pairs_per_night(" 5 ") == 5


def test_a_night_admits_at_least_one_pair():
    assert pairs_per_night("0") == 1
    assert pairs_per_night("-4") == 1


def test_an_unreadable_value_keeps_the_default():
    assert pairs_per_night("three") == 2
