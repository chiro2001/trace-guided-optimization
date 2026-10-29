"""Screening ranks candidates; independent confirmation authorizes publication."""
import math


def complete(validation):
    return (validation.get("equivalent") is True and validation.get("requested_pairs", 0) > 0
            and validation.get("completed_pairs") == validation["requested_pairs"])


def screening_score(validation, minimum_gain):
    score = validation.get("median_reduction_percent", float("nan"))
    return score if complete(validation) and math.isfinite(score) and score > minimum_gain else None


def confirmed(validation, minimum_gain):
    gain = validation.get("min_reduction_percent", float("nan"))
    return complete(validation) and math.isfinite(gain) and gain > minimum_gain
