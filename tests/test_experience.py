import copy
from datetime import datetime, timezone
import unittest
from listening.experience import score_rows, reaction_active, album_identity, editorial_selection


class ExperienceTests(unittest.TestCase):
    def test_original_experiment_scores_are_lossless_and_not_mutated(self):
        original = [{'criterion': '공간감', 'a': 3, 'b': 4, 'impression': 'B가 나음', 'detail': '효과음의 위치가 다름', 'extraEvidence': 'preserve'}]
        before = copy.deepcopy(original)
        self.assertEqual(score_rows(original, ['공간감']), original)
        self.assertEqual(original, before)

    def test_early_migration_scores_remain_readable(self):
        rows = score_rows({'공간감': 'A 선호', '질감': '미평가'}, ['공간감', '질감'])
        self.assertEqual(rows[0]['impression'], 'A가 나음')
        self.assertIsNone(rows[1]['impression'])
        self.assertIsNone(rows[0]['a'])

    def test_skips_expire_and_cancelled_reactions_are_not_used(self):
        clock = datetime(2026, 10, 27, tzinfo=timezone.utc)
        self.assertTrue(reaction_active({'action': 'skip', 'updatedAt': '2026-10-26T12:00:00Z'}, clock))
        self.assertFalse(reaction_active({'action': 'skip', 'updatedAt': '2026-10-01T12:00:00Z'}, clock))
        self.assertFalse(reaction_active({'action': 'clear'}, clock))
        self.assertTrue(reaction_active({'action': 'known'}, clock))
        self.assertEqual(album_identity('itunes-us-123456'), album_identity('123456'))

    def test_catalogue_words_are_combined_and_comma_separates_alternatives(self):
        a = {'work': 'Goldberg', 'artists': 'Víkingur Ólafsson', 'album': 'Bach', 'keywords': ['올라프손'], 'tags': ['reflect']}
        b = {'work': 'Beethoven 4', 'artists': 'Other', 'album': 'Symphony', 'keywords': [], 'tags': ['energy']}
        result = editorial_selection([a, b], 'Bach Olafsson, Beethoven')
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(editorial_selection([a, b], 'Bach Other')['items'], [])
        fallback = editorial_selection([a], '', ['energy'])
        self.assertTrue(fallback['fallback'])
        self.assertEqual(fallback['unmatchedGoals'], ['energy'])
