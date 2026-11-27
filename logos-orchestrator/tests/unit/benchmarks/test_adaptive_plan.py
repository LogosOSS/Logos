from logos.benchmarks.adaptive_plan import options
from logos.benchmarks.configuration import NGRAM_CONFIG, QWEN_MTP_CONFIG


def test_automatic_limits_start_with_one_explicit_parameter_per_test():
    current = {"max_num_seqs": 0}
    limits = {"current": current, "gpu_count": 1}

    candidates = options(current, {}, [], limits)

    assert candidates[:4] == [
        {"max_num_seqs": 4},
        {"max_num_batched_tokens": 2048},
        {"max_num_seqs": 8},
        {"max_num_batched_tokens": 4096},
    ]
    assert all(len(candidate) == 1 for candidate in candidates)


def test_search_tries_an_unobserved_direction_then_refines_towards_incumbent():
    current = {"max_num_seqs": 16, "max_num_batched_tokens": 4096}
    limits = {"current": current, "gpu_count": 1}

    first = options(current, {}, [], limits)
    next_trial = options(current, {}, [{"max_num_seqs": 8}], limits)
    refined = options(current, {}, [{"max_num_seqs": 8}, {"max_num_seqs": 32}], limits)

    assert first[0] == {"max_num_seqs": 8}
    assert next_trial[:2] == [{"max_num_batched_tokens": 2048}, {"max_num_seqs": 32}]
    assert {"max_num_seqs": 12} in refined
    assert {"max_num_seqs": 24} in refined
    assert {"max_num_seqs": 8} not in refined
    assert {"max_num_seqs": 32} not in refined


def test_search_varies_each_axis_from_a_measured_incumbent_and_keeps_other_overrides():
    current = {"max_num_seqs": 4, "max_num_batched_tokens": 4096, "kv_cache_dtype": "auto"}
    choices = {"kv_cache_dtype": ("auto", "fp8")}
    limits = {"current": current, "gpu_count": 1, "nightly_vllm_choices": choices}
    incumbent = {"max_num_seqs": 8, "kv_cache_dtype": "fp8"}
    attempted = [{"max_num_seqs": 8}, incumbent]

    candidates = options(current, incumbent, attempted, limits, choices)

    assert candidates[:2] == [
        {"kv_cache_dtype": "fp8"},
        {"max_num_seqs": 8, "max_num_batched_tokens": 2048, "kv_cache_dtype": "fp8"},
    ]
    assert {"kv_cache_dtype": "fp8"} in candidates
    assert {"max_num_seqs": 8} not in candidates
    for candidate in candidates:
        effective = current | candidate
        center = current | incumbent
        assert sum(effective[key] != center[key] for key in current) == 1


def test_candidates_respect_compatible_worker_limits_and_only_enabled_variations():
    current = {"max_num_seqs": 4, "max_num_batched_tokens": 4, "kv_cache_dtype": "auto"}
    limits = {"current": current, "metal_batching": True}

    candidates = options(current, {}, [], limits, {"kv_cache_dtype": ("auto", "fp8")})

    assert candidates == [{"max_num_seqs": 2}, {"max_num_batched_tokens": 8}]


def test_mtp_off_is_an_explicit_option_and_can_be_combined_with_a_batching_change():
    current = {"max_num_seqs": 4, "speculative_config": QWEN_MTP_CONFIG}
    choices = {"speculative_config": ("", QWEN_MTP_CONFIG)}
    limits = {"current": current, "gpu_count": 1, "nightly_vllm_choices": choices}
    incumbent = {"max_num_seqs": 8}

    candidates = options(current, incumbent, [incumbent], limits, choices)

    assert {"max_num_seqs": 8, "speculative_config": ""} in candidates
    assert all("speculative_config" not in candidate for candidate in options(current, incumbent, [], limits))


def test_fallback_covers_all_four_turing_axes_in_one_round_without_an_improving_incumbent():
    current = {
        "max_num_seqs": 5,
        "max_num_batched_tokens": 2560,
        "enable_prefix_caching": True,
        "speculative_config": "",
    }
    choices = {"enable_prefix_caching": (False, True), "speculative_config": ("", NGRAM_CONFIG)}
    limits = {"current": current, "gpu_count": 1, "nightly_vllm_choices": choices}
    attempted = []
    for _ in range(4):
        attempted.append(options(current, {}, attempted, limits, choices)[0])

    assert attempted == [
        {"max_num_seqs": 2},
        {"max_num_batched_tokens": 1280},
        {"enable_prefix_caching": False},
        {"speculative_config": NGRAM_CONFIG},
    ]
