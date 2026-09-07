"""Isolated compatibility calls for pre-canonical test/migration fixtures.

Production composition always supplies canonical context before this module can
be reached. It is intentionally not imported by gateway code.
"""
def manual_close(client, coin, dex): return client.market_close(coin, dex)
def ai_order(signer, payload, expires_ms):
    return signer.submit_user_ioc(payload['coin'], payload['direction']=='LONG', payload['size'], payload['limit_price'], payload['leverage'], payload['cloid'], expires_ms=expires_ms)
def ai_position(signer, payload, expires_ms):
    return signer.submit_position_ioc(payload['coin'], payload['is_buy'], payload['size'], payload['limit_price'], payload['reduce_only'], payload['cloid'], payload['dex'], expires_ms=expires_ms, expected_position=payload.get('expected_position'))
