import re
import unittest
from pathlib import Path


class WorkflowScheduleTests(unittest.TestCase):
    def test_independent_recovery_schedules_and_test_step(self):
        workflow = Path(".github/workflows/auto-charge.yml").read_text(encoding="utf-8")
        crons = re.findall(r"cron:\s*'([^']+)'", workflow)
        self.assertEqual(len(crons), 8)
        self.assertEqual(crons[0], "47 18 * * *")
        self.assertEqual(crons[-1], "7 22 * * *")
        self.assertIn("python -m unittest discover", workflow)
