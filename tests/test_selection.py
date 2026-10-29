from copy import deepcopy
from pathlib import Path
import json
import unittest
from tgo.selection import screening_score, confirmed


class SelectionPolicy(unittest.TestCase):
    def test_real_vllm_screening_noise_does_not_bypass_strict_confirmation(self):
        evidence = Path(__file__).resolve().parents[1] / "evidence/2026-10-29"
        screening = json.loads((evidence / "vllm-retired-multi-r1-build.json").read_text())["validation"]
        confirmation = json.loads((evidence / "vllm-retired-multi-r1-confirmation.json").read_text())
        self.assertLess(screening["min_reduction_percent"], 0.5)
        self.assertGreater(screening_score(screening, 0.5), 3)
        self.assertFalse(confirmed(screening, 0.5))
        self.assertTrue(confirmed(confirmation, 0.5))
        regression = deepcopy(confirmation)
        regression["min_reduction_percent"] = -0.1
        self.assertFalse(confirmed(regression, 0.5))
        incomplete = deepcopy(confirmation)
        incomplete["completed_pairs"] -= 1
        self.assertIsNone(screening_score(incomplete, 0.5))
        self.assertFalse(confirmed(incomplete, 0.5))


if __name__ == "__main__":
    unittest.main()
