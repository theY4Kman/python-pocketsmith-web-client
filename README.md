# pocketsmith-web-client

A web-based client for Pocketsmith, which adds support for a few things missing from the API:

 - Searching transactions
 - Syncing institutions, including those requiring MFA!
 - Real-time events through [Pusher](https://pusher.com/) (just like the web UI)


# Installation

```bash
pip install pocketsmith-web-client
```


# Usage

```python
import asyncio
from pocketsmith_web import PocketsmithWebClient

pwc = PocketsmithWebClient(
    username='hambob',
    password='Myspace123',
    # If 2fa is enabled on the account — NOTE: this is the KEY, not a one-time code!
    totp_key='81r0dq0815u88qi2',
)

async def main():
    # Check login — NOTE: API methods requiring auth will automatically call this
    await pwc.login()

    # Search for some transactions and print them out
    async for transaction in pwc.search_transactions('Merchant, inc'):
        print(f'[{transaction["id"]:>8}] {transaction["date"]:%Y-%m-%d} ${transaction["amount"]:.2f}')

    # Sync some institutions
    # NOTE: these parameters can be scraped from the Account Summary page, 
    #       in URLs of the format: "/feeds/user_institutions/<uys_id>/refresh?item_ids%5B%5D=<item_id>
    await pwc.sync_institution(162303, 91821548)

asyncio.run(main())
```

If you have an institution requiring MFA info, the Pusher client can be used to provide this info when requested. It's up to you to figure out how to acquire the MFA info, though — whether it's from user input, a generated TOTP, a text message, email, etc.

```python
import asyncio
import json
from pocketsmith_web import PocketsmithWebClient, PusherEvent

pwc = PocketsmithWebClient('hambob', 'Myspace123', totp_key='81r0dq0815u88qi2')


async def sync_my_mfa_bank():
    uys_id = 162303
    item_id = 91821548

    await pwc.sync_institution(uys_id, item_id)

    async with pwc.pusher() as pusher:
        # Wait for an MFA event for our bank
        await pusher.events.expect(
            PusherEvent.MfaChanged(pwc.pusher_channel),
            matches_uys_item(uys_id, item_id),
        )

        # Grab the MFA popup form details
        mfa_req = await pwc.get_mfa_form_info()

        # Ask the user for the MFA deets, please
        print(f'MFA deets required: {mfa_req["label"]}')
        token = input('Token: ')

        # Now shoot the token back to Pocketsmith
        await pwc.provide_feed_mfa(uys_id, item_id, token)


def matches_uys_item(uys_id, item_id):
    uys_id = str(uys_id)
    item_id = str(item_id)

    def does_event_match_uys_item(event: PusherEvent):
        if not isinstance(event.data, dict):
            return False

        event_uys_id = event.data.get('user_yodlee_site_id')

        event_items = event.data.get('new_mfa_items', ())
        if isinstance(event_items, str):
            try:
                event_items = json.loads(event_items)
            except (TypeError, ValueError):
                pass

        return uys_id == event_uys_id and item_id in event_items

    return does_event_match_uys_item


asyncio.run(sync_my_mfa_bank())
```
