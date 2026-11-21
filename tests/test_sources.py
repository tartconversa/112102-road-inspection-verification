import csv
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "sources"
HEADERS = {'trial_assignment': ['segment', 'segment_version', 'block', 'method', 'start_day', 'prior_segment', 'inclusion_probability'], 'survey_reports': ['report', 'segment', 'day', 'candidate', 'severity'], 'verification_draws': ['draw', 'segment', 'planned_day', 'selection_type', 'probability', 'crew'], 'field_outcomes': ['result', 'draw', 'segment', 'day', 'defect', 'physical_state', 'known_day'], 'works': ['job', 'segment', 'defect', 'day', 'state'], 'dispatch_updates': ['event', 'draw', 'from_crew', 'to_segment', 'day', 'decision'], 'crew_capacity': ['crew', 'day', 'places', 'urgent_reserved']}
REFERENCES = [['trial_assignment', 'prior_segment', 'trial_assignment', 'segment'], ['survey_reports', 'segment', 'trial_assignment', 'segment'], ['field_outcomes', 'draw', 'verification_draws', 'draw'], ['dispatch_updates', 'to_segment', 'trial_assignment', 'segment']]
COUNTS = [['verification_draws', 'selection_type', 'random', 5], ['field_outcomes', 'physical_state', 'repaired', 2], ['dispatch_updates', 'decision', 'substitution', 1]]


class SourceContractTests(unittest.TestCase):
    def setUp(self):
        self.records = {}
        for name, header in HEADERS.items():
            with (ROOT / f"{name}.csv").open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, header)
                self.records[name] = list(reader)

    def test_record_shape(self):
        for name, records in self.records.items():
            with self.subTest(source=name):
                self.assertTrue(records)
                for row in records:
                    self.assertEqual(set(row), set(HEADERS[name]))
                    self.assertNotIn(None, row.values())
                    self.assertTrue(row[HEADERS[name][0]])
                self.assertEqual(len(records), len({tuple(row.values()) for row in records}))

    def test_cross_source_references(self):
        for child, fk, parent, pk in REFERENCES:
            with self.subTest(source=child, field=fk):
                keys = {row[pk] for row in self.records[parent]}
                values = {row[fk] for row in self.records[child] if row[fk]}
                self.assertTrue(values)
                self.assertFalse(values - keys)

    def test_source_has_relevant_observations(self):
        for source, field, value, minimum in COUNTS:
            with self.subTest(source=source, field=field, value=value):
                self.assertGreaterEqual(sum(row[field] == value for row in self.records[source]), minimum)
