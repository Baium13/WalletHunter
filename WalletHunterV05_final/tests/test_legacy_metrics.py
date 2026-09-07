import json
import math
import unittest
from core.legacy_metrics import normalise_legacy_metrics


class LegacyMetricTests(unittest.TestCase):
    def state(self,value):
        return {'profiles':{'1':{'account':{'private_key':'unchanged-ciphertext'},
            'copy_enabled':True,'runtime':{'managed':['BTC|']},
            'leader_models':{'wallet':{'test_pf':value}}}}}

    def test_only_positive_legacy_analytical_infinity_is_encoded_as_string(self):
        original=self.state(math.inf)
        updated,changes=normalise_legacy_metrics(original)
        self.assertEqual(updated['profiles']['1']['leader_models']['wallet']['test_pf'],'Infinity')
        self.assertEqual(updated['profiles']['1']['account'],original['profiles']['1']['account'])
        self.assertEqual(updated['profiles']['1']['runtime'],original['profiles']['1']['runtime'])
        self.assertTrue(updated['profiles']['1']['copy_enabled'])
        self.assertEqual(len(changes),1)
        json.dumps(updated,allow_nan=False)
        self.assertEqual(original['profiles']['1']['leader_models']['wallet']['test_pf'],math.inf)

    def test_finite_document_is_unchanged_and_migration_is_idempotent(self):
        finite=self.state(2.0)
        self.assertEqual(normalise_legacy_metrics(finite),(finite,[]))
        encoded,_=normalise_legacy_metrics(self.state(math.inf))
        self.assertEqual(normalise_legacy_metrics(encoded),(encoded,[]))

    def test_invalid_ratios_and_trading_values_are_never_guessed(self):
        for invalid in (math.nan,-math.inf):
            with self.assertRaises(ValueError):normalise_legacy_metrics(self.state(invalid))
        for field in ('balance','size','margin','price'):
            state=self.state(2)
            state['profiles']['1']['runtime'][field]=math.inf
            with self.assertRaises(ValueError):normalise_legacy_metrics(state)


if __name__=='__main__':unittest.main()
