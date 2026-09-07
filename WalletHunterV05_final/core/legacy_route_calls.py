"""Explicit legacy/test fixture calls; never a production execution path.

This module is intentionally kept separate from the canonical adapter.  It is
only suitable for dependency injection by isolated legacy tests that model an
old signer/client.  Normal application construction has no reference to these
functions and fails closed without canonical context.
"""
def manual_close(client, coin, dex): return client.market_close(coin, dex)
def ai_order(signer, payload, expires_ms):
    return signer.submit_user_ioc(payload['coin'], payload['direction']=='LONG', payload['size'], payload['limit_price'], payload['leverage'], payload['cloid'], expires_ms=expires_ms)
def ai_position(signer, payload, expires_ms):
    return signer.submit_position_ioc(payload['coin'], payload['is_buy'], payload['size'], payload['limit_price'], payload['reduce_only'], payload['cloid'], payload['dex'], expires_ms=expires_ms, expected_position=payload.get('expected_position'))
