"""Generate a bounded set of next configurations from attempted Nightly runs."""

from collections.abc import Iterable, Mapping
from itertools import zip_longest

from logos.benchmarks.configuration import ServingOverrides
from logos.benchmarks.worker_limits import validate_worker_overrides

_SEQ_LIMIT = "max_num_seqs"
_TOKEN_LIMIT = "max_num_batched_tokens"
_AUTO_TRIALS = {_SEQ_LIMIT: (4, 8, 2, 16, 1, 32), _TOKEN_LIMIT: (2048, 4096, 1024, 8192, 512, 16384)}
_MAX_OPTIONS = 12
TrialValue = int | str | bool


def options(
    current: dict,
    incumbent: Mapping[str, TrialValue],
    attempted: Iterable[Mapping[str, TrialValue]],
    limits: dict,
    extra_choices: Mapping[str, tuple[TrialValue, ...]] | None = None,
) -> list[dict[str, TrialValue]]:
    """Return safe single-axis neighbours of the incumbent, best fallback first.

    Input and output overrides are relative to the original serving configuration.
    Include failed configurations in `attempted` to avoid retrying them in the search.
    """
    extra_choices = extra_choices or {}
    baseline = {
        key: current.get(key) if type(current.get(key)) is int and current[key] > 0 else None for key in _AUTO_TRIALS
    }
    baseline.update({key: current.get(key) for key in extra_choices})

    def effective(overrides):
        return {key: overrides.get(key, value) for key, value in baseline.items()}

    center = effective(incumbent)
    seen = [baseline, center, *(effective(trial) for trial in attempted)]
    dimensions = []
    for key in baseline:
        observed = {
            trial[key]
            for trial in seen
            if trial[key] != center[key] and all(trial[field] == center[field] for field in baseline if field != key)
        }
        if key in _AUTO_TRIALS:
            values = _numeric_neighbours(key, center, baseline, observed)
        else:
            values = extra_choices[key]
        candidates = []
        for value in (*values, baseline[key]):
            trial = {**center, key: value}
            if trial in seen:
                continue
            seen.append(trial)
            overrides = {field: setting for field, setting in trial.items() if setting != baseline[field]}
            try:
                validate_worker_overrides(ServingOverrides(**overrides), limits, nightly=True)
            except ValueError:
                continue
            candidates.append(overrides)
        if candidates:
            dimensions.append((len(observed), candidates))

    dimensions.sort(key=lambda entry: entry[0])
    # Give each dimension a candidate before returning additional values on one axis.
    rows = zip_longest(*(candidates for _, candidates in dimensions))
    return [candidate for row in rows for candidate in row if candidate is not None][:_MAX_OPTIONS]


def _numeric_neighbours(key, center, baseline, observed):
    if key == _SEQ_LIMIT:
        lower, upper = 1, min(center[_TOKEN_LIMIT] or 65536, 65536)
    else:
        lower = center[_SEQ_LIMIT] or 1
        upper = max(32768, 2 * (baseline[key] or 0), lower)
    value = center[key]
    if value is None:
        return [max(lower, min(seed, upper)) for seed in _AUTO_TRIALS[key]]

    below = max((trial for trial in observed if type(trial) is int and trial < value), default=None)
    above = min((trial for trial in observed if type(trial) is int and trial > value), default=None)
    next_lower = (below + value) // 2 if below is not None else value // 2
    next_upper = (above + value + 1) // 2 if above is not None else 2 * value
    # Try an untouched direction before spending another run refining an observed interval.
    choices = (next_upper, next_lower) if above is None and below is not None else (next_lower, next_upper)
    return [max(lower, min(trial, upper)) for trial in choices]
