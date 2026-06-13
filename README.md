# Onion Chop

Onion Chop is a black-and-white arcade webapp with a small escrow server for real Onion entries.

## Run Locally

Demo onions:

```bash
python3 server.py --port 8766
```

Then open:

```text
http://127.0.0.1:8766/
```

Real onions:

```bash
export ONION_EXTERNAL_API_KEY="replace-with-api-key"
export ONION_API_BASE="https://oniondao.dev"
export ONION_CHOP_HOUSE_USERNAME="Caleb Martin"
export ONION_CHOP_ENTRY=5
export ONION_CHOP_ADMIN_PIN="choose-a-settlement-pin"
python3 server.py --port 8766
```

## Flow

1. Player enters their Onion username.
2. Server creates a 5 Onion transfer request from the player to the house vault.
3. Player approves the request in the Onion portal or badge flow.
4. Server confirms the entry, issues a one-run token, and the player gets 20 chops.
5. Score is submitted to the server.
6. When at least three players have scores, admin settles:
   - 60% to first place
   - 20% to second place
   - 10% to third place
   - 10% stays with `Caleb Martin`

## Production Notes

The browser never stores the Onion API key. Keep the key on the server.

For a public event, put this behind HTTPS and set:

```bash
export ONION_CHOP_CALLBACK_URL="https://your-domain.example/api/onion-callback"
export ONION_CHOP_CALLBACK_SECRET="choose-a-random-shared-secret"
```

Callbacks are supported, and the server also polls request status when players are waiting to start.
