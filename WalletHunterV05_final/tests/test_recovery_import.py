import copy
import unittest

from scripts.recover_ownership import validate_positions


class RecoveryImportTest(unittest.TestCase):
    def setUp(self):
        self.actual={
            "BTC":{"szi":"-0.00058","entryPx":"79477","leverage":{"value":40,"type":"cross"}},
            "ETH":{"szi":"-0.0188","entryPx":"2444.6","leverage":{"value":20,"type":"cross"}},
        }

    def test_confirmed_snapshot_is_read_only(self):
        before=copy.deepcopy(self.actual)
        self.assertEqual(set(validate_positions(self.actual)),{"BTC","ETH"})
        self.assertEqual(self.actual,before)

    def test_changed_or_unknown_margin_mode_aborts(self):
        for mode in ("isolated",None):
            with self.subTest(mode=mode):
                self.actual["BTC"]["leverage"]["type"]=mode
                with self.assertRaises(RuntimeError):validate_positions(self.actual)

    def test_missing_or_changed_position_aborts(self):
        for field,value in (("szi","-0.00059"),("entryPx","79478"),("szi","NaN"),("entryPx","Infinity")):
            with self.subTest(field=field):
                changed=copy.deepcopy(self.actual)
                changed["BTC"][field]=value
                with self.assertRaises(RuntimeError):validate_positions(changed)
        with self.assertRaises(RuntimeError):validate_positions({"BTC":self.actual["BTC"]})
