import ast
import unittest
from pathlib import Path

class DirectWriterGuard(unittest.TestCase):
    def test_product_modules_have_no_direct_submission_attributes(self):
        root=Path(__file__).parents[1]/'core'
        forbidden={'market_open','market_reduce','market_close','set_leverage','submit_user_ioc','submit_position_ioc'}
        for name in ('manual_positions.py','ai_user_orders.py','ai_position_actions.py'):
            tree=ast.parse((root/name).read_text(encoding='utf-8'))
            self.assertFalse([n for n in ast.walk(tree) if isinstance(n,ast.Attribute) and n.attr in forbidden],name)
