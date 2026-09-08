import ast
import unittest
from pathlib import Path

from core.confirmed_execution_adapter import (
    CanonicalContextRequired, confirmed_ai_order, confirmed_ai_position, manual_close,
)

class DirectWriterGuard(unittest.TestCase):
    def test_product_modules_have_no_direct_submission_attributes(self):
        root=Path(__file__).parents[1]
        forbidden={'market_open','market_reduce','market_close','set_leverage','submit_user_ioc','submit_position_ioc',
                   'submit_copy_ioc','place_stop_loss','cancel_order','cancel_open_orders','bulk_orders','bulk_cancel'}
        allowed={'core/foundation/copy_execution.py','core/legacy_route_calls.py'}
        for folder in ('core','desktop','webapp'):
            for path in (root/folder).rglob('*.py'):
                name=path.relative_to(root).as_posix()
                if name in allowed:continue
                tree=ast.parse(path.read_text(encoding='utf-8-sig'))
                self.assertFalse([n for n in ast.walk(tree) if isinstance(n,ast.Call)
                    and isinstance(n.func,ast.Attribute) and n.func.attr in forbidden],name)

    def test_normal_route_modules_do_not_import_legacy_route_calls(self):
        root=Path(__file__).parents[1]/'core'
        for name in ('confirmed_execution_adapter.py','manual_positions.py',
                     'ai_user_orders.py','ai_position_actions.py'):
            tree=ast.parse((root/name).read_text(encoding='utf-8'))
            imports=[]
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imports.append(node.module or '')
                elif isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
            self.assertNotIn('core.legacy_route_calls', imports, name)

    def test_adapter_without_context_fails_closed(self):
        with self.assertRaises(CanonicalContextRequired):
            manual_close(object(), 'BTC', '')
        with self.assertRaises(CanonicalContextRequired):
            confirmed_ai_order(object(), {}, 0)
        with self.assertRaises(CanonicalContextRequired):
            confirmed_ai_position(object(), {}, 0)
