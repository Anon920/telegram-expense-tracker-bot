"""Read-only Telegram connection check; never prints credentials or error URLs."""
from bot import Telegram, configured_token


def main():
    try:
        api = Telegram(configured_token())
        identity = api.call('getMe')
        webhook = api.call('getWebhookInfo')
        print('Connected: @' + identity['username'])
        print('Webhook configured:', bool(webhook.get('url')))
    except Exception as error:
        print('Connection failed:', type(error).__name__, getattr(error, 'code', ''))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
