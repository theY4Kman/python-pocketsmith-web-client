import asyncio
import datetime
import logging
import re
from ast import literal_eval
from contextlib import asynccontextmanager
from datetime import date
from typing import (
    Any,
    AsyncContextManager,
    AsyncGenerator,
    Dict,
    Optional,
    Tuple,
    Union,
)

import httpx
from bs4 import BeautifulSoup
from httpx import Response
from pyotp import TOTP

from pocketsmith_web.pusher import PocketsmithPusher
from pocketsmith_web.types import MfaFormInfo, Transaction

logger = logging.getLogger(__name__)


class PocketsmithWebClient:
    class URLs:
        ROOT = 'https://my.pocketsmith.com/login'
        LOGIN = 'https://my.pocketsmith.com/user_session'
        DASHBOARD = 'https://my.pocketsmith.com/dashboard'
        ACCOUNT_SUMMARY = 'https://my.pocketsmith.com/account_summary'
        CONFIRM_TFA = 'https://my.pocketsmith.com/confirm_two_factor'
        VALIDATE_TFA = 'https://my.pocketsmith.com/validate_two_factor_code'

        REFRESH_INSTITUTION = 'https://my.pocketsmith.com/feeds/user_institutions/{uys_id}/refresh_info'
        REFRESH_ITEMS = 'https://my.pocketsmith.com/feeds/user_institutions/{uys_id}/refresh'

        SEARCH_FEEDS = 'https://my.pocketsmith.com/account_summary/search_live_feed_sites'

        # Used to generate auth details to send to Pusher,
        # after initiating websocket and receiving socket ID.
        PUSHER_AUTH = 'https://my.pocketsmith.com/pusher/auth'

        # Used to retrieve more information about MFA details being requested
        GET_MFA_FORM = 'https://my.pocketsmith.com/feeds/user/show_mfa_form'

        # Used to provide a requested MFA item. Request is initiated over Pusher.
        PROVIDE_FEED_MFA = 'https://my.pocketsmith.com/feeds/user_institutions/{uys_id}/post_mfa_input'

        # Powers the transactions search/filtering in-app
        SEARCH_TRANSACTIONS = 'https://my.pocketsmith.com/transactions/query.json'

    loop: asyncio.AbstractEventLoop
    client: httpx.AsyncClient
    authed_client: httpx.AsyncClient

    pusher_client_id: Optional[str]
    pusher_channel: Optional[str]

    csrf_token: Optional[str]
    is_logged_in: bool

    def __init__(self,
                 username: str, password: str, totp_key: str = None, *,
                 loop: asyncio.AbstractEventLoop = None):
        self.username = username
        self.password = password
        self.totp_key = totp_key

        self.loop = loop or asyncio.get_event_loop()

        self.pusher_client_id = None
        self.pusher_channel = None

        self.client = httpx.AsyncClient()
        self.authed_client = self._wrap_client_authed(self.client)
        self.csrf_token = None
        self.is_logged_in = False

    def _wrap_client_authed(self, client: httpx.AsyncClient) -> httpx.AsyncClient:
        pwc = self

        class EnsureAuthClient(client.__class__):
            async def request(self, *args, data=None, **kwargs) -> Response:
                response = await client.request(*args, data=data, **kwargs)

                # If we were redirected to the login page, attempt to re-login once.
                if response.url == pwc.URLs.ROOT:
                    pwc.is_logged_in = False
                    await pwc.login()

                    # Use new CSRF token, if passed
                    if isinstance(data, dict) and 'authenticity_token' in data:
                        data['authenticity_token'] = pwc.csrf_token

                    response = await client.request(*args, data=data, **kwargs)

                return response

        authed_client = EnsureAuthClient()
        authed_client.__dict__.update(client.__dict__)

        return authed_client

    async def login(self):
        ###
        # First, retrieve the CSRF (authenticity) token
        #
        logger.debug(f'Attempting to retrieve Pocketsmith CSRF token')
        res = await self.client.get(self.URLs.ROOT)
        res.raise_for_status()

        # If we're already authenticated, Pocketsmith will redirect us to the dashboard
        if res.url == self.URLs.DASHBOARD:
            self.is_logged_in = True
            logger.info(f'Already authenticated to Pocketsmith')
            return

        match = re.search(
            r'<input name="authenticity_token" type="hidden" value="(?P<csrf_token>[^"]+)" />',
            res.text,
        )
        if not match:
            raise ValueError('Unable to find CSRF token')

        self.csrf_token = match.group('csrf_token')
        logger.debug(f'Retrieved Pocketsmith CSRF token: {self.csrf_token}')

        ###
        # Attempting to log in
        #
        logger.debug(f'Attempting to provide username/password to Pocketsmith')
        res = await self.client.post(
            url=self.URLs.LOGIN,
            data={
                'authenticity_token': self.csrf_token,
                'user_session[login]': self.username,
                'user_session[password]': self.password,
                'commit': 'Sign in',
            },
            allow_redirects=True,
        )
        res.raise_for_status()
        logger.info(f'Pocketsmith username/password accepted')

        ###
        # Enter TFA code, if required
        #
        if res.url == self.URLs.CONFIRM_TFA:
            logger.debug(f'2FA required for successful login.')

            if self.totp_key is None:
                raise ValueError(
                    '2FA is required to login to Pocketsmith, '
                    'but no 2FA TOTP key was provided '
                    '(this is usually a 16-char alphanumeric string)')

            totp = TOTP(self.totp_key)
            tfa_code = totp.now()

            logger.debug(f'Attempting to provide 2FA details ({tfa_code}) to Pocketsmith')
            res = await self.client.post(
                url=self.URLs.VALIDATE_TFA,
                data={
                    'authenticity_token': self.csrf_token,
                    'user_session[validation_code]': tfa_code,
                    'commit': 'VALIDATE',
                },
                allow_redirects=True,
            )
            res.raise_for_status()

            logger.info(f'2FA details accepted by Pocketsmith')

        ###
        # Parse out the Pusher client ID and user channel, which is needed to
        # use the realtime Pusher client
        #
        try:
            self.pusher_client_id, self.pusher_channel = self._extract_pusher_params(res.text)
        except ValueError:
            logger.debug(
                'Unable to find Pusher client ID and/or channel in authenticated response.')

        self.is_logged_in = True
        logger.info(f'Successfully logged in to Pocketsmith!')

    async def _require_login(self):
        if not self.is_logged_in:
            await self.login()

    async def _require_pusher_params(self):
        if self.pusher_client_id is None or self.pusher_channel is None:
            await self._retrieve_pusher_params()

    async def sync_institution(self, uys_id: int, *item_ids):
        await self._require_login()

        if item_ids:
            url = self.URLs.REFRESH_ITEMS.format(uys_id=uys_id)
            res = await self.authed_client.post(
                url=url,
                params={
                    'item_ids[]': item_ids,
                },
                data={
                    'authenticity_token': self.csrf_token,
                },
            )

        else:
            url = self.URLs.REFRESH_INSTITUTION.format(uys_id=uys_id)
            res = await self.authed_client.post(
                url=url,
                data={
                    'authenticity_token': self.csrf_token,
                },
            )

        res.raise_for_status()
        return res

    async def get_mfa_form_info(self) -> Optional[MfaFormInfo]:
        """When a bank requires MFA info, return info about what's being requested

        This appears to be the only way to determine whether, e.g., the last
        four digits of a phone number is expected, or a texted MFA code.

        :return:
            Info about the requested MFA details, if any are being requested.
            Otherwise, None is returned.
        """
        await self._require_login()

        res = await self.authed_client.get(self.URLs.GET_MFA_FORM)

        if res.status_code == 500:
            # Pocketsmith seems to return a 500 if no MFA details are being requested
            return

        res.raise_for_status()

        mfa_form_info = self._parse_mfa_form(res.text)
        return mfa_form_info

    @staticmethod
    def _parse_mfa_form(js: str) -> Dict[str, Any]:
        match = re.match(r"\$\('body'\)\.append\((.+)\);[\s\n]*", js)
        if not match:
            raise ValueError(f'Unable to parse MFA form HTML from JS:\n{js}')

        escaped_html_string = match.group(1)
        escaped_html = literal_eval(escaped_html_string)
        html = escaped_html.replace('\\n', '\n').replace('\\/', '/')

        doc = BeautifulSoup(html, features='html5lib')

        form = doc.select_one('form')
        action = form['action']

        match = re.search(r'/feeds/user_institutions/(?P<uys_id>[^/]+)/post_mfa_input', action)
        uys_id = match.group(1) if match else None

        item_id_input = form.select_one('#item_id')
        item_id = item_id_input['value']

        label_el = form.select_one('label')
        label = label_el.text

        countdown_span = doc.select_one('span[id^="mfa_countdown"]')
        countdown_text = countdown_span.text
        try:
            seconds_remaining = int(countdown_text)
        except (ValueError, TypeError):
            seconds_remaining = None

        return {
            'action': form['action'],
            'uys_id': uys_id,
            'item_id': item_id,
            'label': label,
            'seconds_remaining': seconds_remaining,
        }

    async def provide_feed_mfa(self, uys_id: Union[int, str], item_id: Union[int, str], token: str):
        """Provide requested MFA info for a particular bank feed
        """
        url = self.URLs.PROVIDE_FEED_MFA.format(uys_id=uys_id)
        res = await self.authed_client.post(
            url=url,
            data={
                'authenticity_token': self.csrf_token,
                'credentials[token]': token,
                'item_id': item_id,
            }
        )
        res.raise_for_status()
        return res

    async def get_feed_sites(self) -> Dict[int, str]:
        """Return the user yodlee site IDs for each bank feed

        These user yodlee site IDs can be used to trigger syncing of bank info.
        """
        await self._require_login()

        res = await self.client.get(self.URLs.ACCOUNT_SUMMARY)
        res.raise_for_status()

        feed_site_ids = self._parse_feed_sites(res.text)
        return feed_site_ids

    async def search_transactions(
        self,
        keywords: str = '',
        after_date: Union[date, str] = None,
        before_date: Union[date, str] = None,
        sort_col: str = 'date',
        sort_desc: bool = True,
        include_totals: bool = True,
        per_page: int = 200,
    ) -> AsyncGenerator[Transaction, None]:
        """Search transactions matching particular filters
        """
        await self._require_login()

        params = {
            'saved_search[by_keywords]': keywords,
            'sort[col]': sort_col,
            'sort[dir]': 'desc' if sort_desc else 'asc',
            'include_totals': 1 if include_totals else 0,
            'per_page': per_page,
            'authenticity_token': self.csrf_token,
        }

        after_date = after_date.strftime('%b %d, %Y') if isinstance(after_date, date) else after_date
        before_date = before_date.strftime('%b %d, %Y') if isinstance(before_date, date) else before_date

        if before_date and after_date:
            params['saved_search[by_date_range]'] = f'{before_date} - {after_date}'

        elif after_date:
            params['saved_search[by_date_after]'] = after_date
            params['saved_search[by_date_quantifier]'] = 'after'

        elif before_date:
            params['saved_search[by_date_before]'] = before_date
            params['saved_search[by_date_quantifier]'] = 'before'

        num_returned = 0

        page = 1
        while True:
            res = await self.client.post(
                self.URLs.SEARCH_TRANSACTIONS,
                data={
                    **params,
                    'page': page,
                },
            )
            res.raise_for_status()

            ret = res.json()
            total_count = ret['total_display_records']

            for result in ret['results']:
                date_str = result['date']
                result['date'] = datetime.datetime.strptime(date_str, '%Y-%m-%d')

                yield result
                num_returned += 1

            if num_returned >= total_count:
                break

    @staticmethod
    def _parse_feed_sites(html: str) -> Dict[int, str]:
        doc = BeautifulSoup(html, features='html5lib')
        edit_bank_feed_items = doc.select('''
            div.toolbar-edit-bank-feeds > nav > div.toolbar-item > a.preload_modal
        ''')

        rgx_uys_id = re.compile(r'/feed_sites/(?P<uys_id>\d+)/options')
        feed_site_ids = {}
        for feed in edit_bank_feed_items:
            options_url = feed['href']
            match = rgx_uys_id.match(options_url)
            if not match:
                continue

            uys_id = int(match.group('uys_id'))

            title_div = feed.select_one('.toolbar-item-detail-title')
            title = title_div.text.strip()

            feed_site_ids[uys_id] = title

        return feed_site_ids

    async def pusher_auth(self, socket_id: Union[int, str], channel: str) -> str:
        """Authenticate a Pusher websocket connection

        :param socket_id:
            The socket_id returned after initiating a websocket connection
            to Pusher.

        :param channel:
            The channel name to receive authorization for.

        """
        await self._require_login()

        logger.debug(f'Attempting to authorize Pusher socket {socket_id} for channel {channel}')
        for i in range(2):
            res = await self.authed_client.post(
                url=self.URLs.PUSHER_AUTH,
                data={
                    'authenticity_token': self.csrf_token,
                    'socket_id': str(socket_id),
                    'channel_name': channel,
                },
            )

            if res.status_code == 403:
                self.is_logged_in = False
                await self.login()
                continue

            res.raise_for_status()
            break

        ret = res.json()
        auth = ret['auth']

        logger.info(f'Authorized Pusher socket {socket_id} for channel {channel}: {auth}')
        return auth

    def _extract_pusher_params(self, body: str) -> Tuple[str, str]:
        match = re.search(r'new Pusher\("(?P<client_id>[^"]+)"', body)
        if not match:
            raise ValueError('Unable to find Pusher client ID in body')
        client_id = match.group('client_id')

        match = re.search(r"pusher\.subscribe\('(?P<channel>[^']+)'\)", body)
        if not match:
            raise ValueError('Unable to find Pusher channel name in body')
        channel = match.group('channel')

        return client_id, channel

    async def _retrieve_pusher_params(self):
        """Find and save the client ID and channel name to use with the Pusher client
        """
        res = await self.authed_client.get(self.URLs.DASHBOARD)
        res.raise_for_status()

        self.pusher_client_id, self.pusher_channel = self._extract_pusher_params(res.text)

    @asynccontextmanager
    async def pusher(self) -> AsyncContextManager['PocketsmithPusher']:
        await self._require_login()
        await self._require_pusher_params()

        async with PocketsmithPusher(pwc=self, client_id=self.pusher_client_id) as pusher:
            await pusher.subscribe(self.pusher_channel)
            yield pusher

    async def pusher_loop(self):
        async with self.pusher() as pusher:
            await pusher.recv_forever()


def pusher_loop(*args, **kwargs):
    pwc = PocketsmithWebClient(*args, **kwargs)

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(pwc.pusher_loop())
    finally:
        loop.close()


if __name__ == '__main__':
    pusher_loop()
