"""d1js -- selective JetStream D1 adapter around the historical D1HybridGateway (BK11 candidate v0.1).

LOCAL_SYNTHETIC_IMPLEMENTATION_ONLY. Default off. The broker carries pointers only; the canonical owner keeps the
material truth; a transit ACK is sent only after an independently readable disposition. Nothing here is a scheduler,
owner engine, truth store, production config or a change to the Signalvev Core NATS client.
"""
__version__ = "0.1.0"
