"""Route adapters for explicitly authorized legacy flows.

These functions are intentionally narrow compatibility bridges. Callers do not
receive signing credentials or invoke exchange methods directly; production
integration can replace the adapter implementation without touching policy.
"""

def manual_close(client, coin, dex):
    return client.market_close(coin, dex)

def confirmed_ai_order(signer, payload, expires_ms):
    return signer.submit_user_ioc(payload['coin'], payload['direction'] == 'LONG', payload['size'],
        payload['limit_price'], payload['leverage'], payload['cloid'], expires_ms=expires_ms)

def confirmed_ai_position(signer, payload, expires_ms):
    return signer.submit_position_ioc(payload['coin'], payload['is_buy'], payload['size'], payload['limit_price'],
        payload['reduce_only'], payload['cloid'], payload['dex'], expires_ms=expires_ms,
        expected_position=payload.get('expected_position'))
