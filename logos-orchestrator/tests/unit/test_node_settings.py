from logos.node_settings import error_items, settings_errors


def test_a_complete_valid_document_has_no_problems():
    assert (
        settings_errors(
            {
                "global_extra_args": ["--safetensors-load-strategy=prefetch", "--max-num-seqs", "8"],
                "gpu_devices": "0,1",
                "auto_reboot_on_stuck_gpu": False,
                "gpu_poll_interval": 5,
            }
        )
        == []
    )
    assert settings_errors({}) == []
    assert settings_errors({"gpu_devices": "all"}) == []
    assert settings_errors({"gpu_devices": "none"}) == []


def test_every_refused_value_names_its_key():
    errors = settings_errors(
        {
            "global_extra_args": "--x",
            "gpu_devices": "0, a",
            "auto_reboot_on_stuck_gpu": "yes",
            "gpu_poll_interval": True,
            "colour": 1,
        }
    )

    assert sorted(e.split(":")[0] for e in errors) == [
        "auto_reboot_on_stuck_gpu",
        "colour",
        "global_extra_args",
        "gpu_devices",
        "gpu_poll_interval",
    ]


def test_arguments_must_be_flags_or_the_value_of_the_flag_before():
    assert settings_errors({"global_extra_args": ["stray"]})
    assert settings_errors({"global_extra_args": ["--a=1", "value"]})
    assert settings_errors({"global_extra_args": [""]})
    assert settings_errors({"global_extra_args": [1]})
    assert settings_errors({"global_extra_args": ["--a", "value"]}) == []
    assert settings_errors({"global_extra_args": ["--a"] * 201})


def test_the_poll_interval_is_whole_seconds_within_limits():
    assert settings_errors({"gpu_poll_interval": 300}) == []
    assert settings_errors({"gpu_poll_interval": 301})
    assert settings_errors({"gpu_poll_interval": 0})
    assert settings_errors({"gpu_poll_interval": 2.5})


def test_errors_become_items_by_key():
    assert error_items(["gpu_devices: use all"]) == [{"key": "gpu_devices", "message": "use all"}]
    assert error_items(["odd"]) == [{"key": "odd", "message": "odd"}]
