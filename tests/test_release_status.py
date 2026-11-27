import unittest
from collector.release_status import assess


class ReleaseStatusTests(unittest.TestCase):
    def test_missing_reviews_and_small_versions_do_not_imply_testing(self):
        value=assess(dict(appId='com.game.slots',title='Slots Game',version='0.1',reviews=0,ratings=0,description='Test your luck and win!'))
        self.assertEqual(value['status'],'unknown')

    def test_name_and_version_are_hints_not_verified_tracks(self):
        self.assertEqual(assess(dict(appId='com.example.beta'))['status'],'possible_beta')
        self.assertEqual(assess(dict(title='Game (Beta)'))['status'],'possible_beta')
        self.assertEqual(assess(dict(version='1.0-beta'))['status'],'possible_beta')
        self.assertEqual(assess(dict(title='Game Early Access'))['status'],'possible_testing')

    def test_stale_manual_evidence_does_not_confirm_a_new_build(self):
        review=dict(status='confirmed_beta',note='Tester page inspected',source_url='https://play.google.com/',observed_version='1.0',reviewed='2026-11-27')
        self.assertEqual(assess(dict(version='1.0'),review)['status'],'confirmed_beta')
        self.assertEqual(assess(dict(version='2.0'),review)['method'],'historical_review')
        self.assertFalse(assess(dict(version='2.0'),review)['status'].startswith('confirmed'))

    def test_program_availability_is_distinct_from_beta_build(self):
        review=dict(status='beta_program_available',note='Join the beta displayed',source_url='https://play.google.com/',observed_version='',reviewed='2026-11-27')
        self.assertEqual(assess({},review)['status'],'beta_program_available')
