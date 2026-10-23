"""Persist payment details per selfbot account. Does not initiate payments."""
import json
from pathlib import Path
import re
import unicodedata

ROOT = Path(__file__).resolve().parents[2]
PAYMENTS_FILE = ROOT / 'payment_profiles.json'


def account_key(bot):
    user = getattr(bot, 'user', None)
    return f'user:{user.id}' if user else f'account:{getattr(bot, "account_num", 1)}'


class PaymentStore:
    def __init__(self, bot, config, path=None):
        self.bot, self.config = bot, config
        self.path = Path(path) if path is not None else PAYMENTS_FILE
        self.key = account_key(bot)

    def legacy(self, name):
        number = getattr(self.bot, 'account_num', 1)
        value = getattr(self.config, f'{name}_{number}', getattr(self.config, name, None))
        if value is None or str(value).strip().casefold() in ('', 'not set', 'none'):
            return None
        return str(value).strip()

    def load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            raise ValueError('Cannot read payment_profiles.json. Repair the file before saving payment details.') from exc
        if not isinstance(data, dict):
            raise ValueError('payment_profiles.json must contain a JSON object.')
        return data

    def profile(self, data=None):
        data = self.load() if data is None else data
        result = data.get(self.key, {})
        if (not isinstance(result, dict) or not isinstance(result.get('upi', {}), dict)
                or not isinstance(result.get('wallets', {}), dict) or not isinstance(result.get('emoji', {}), dict)):
            raise ValueError('Your saved payment profile is invalid. Repair payment_profiles.json first.')
        return result

    def save(self, data, profile):
        data[self.key] = profile
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
        temporary.replace(self.path)

    @staticmethod
    def slot_number(slot):
        if isinstance(slot, bool) or slot not in range(1, 6):
            raise ValueError('Choose a UPI slot from 1 to 5.')
        return str(slot)

    def upi(self, slot=None):
        profile = self.profile()
        slot = profile.get('active_upi', 1) if slot is None else slot
        key = self.slot_number(slot)
        entries = profile.get('upi', {})
        if key in entries:
            entry = entries[key]
            if entry is not None and (not isinstance(entry, dict) or not isinstance(entry.get('id'), str)):
                raise ValueError('A saved UPI slot is invalid. Repair payment_profiles.json first.')
            return entry
        legacy = self.legacy('UPI') if key == '1' else None
        return {'id': legacy, 'label': 'Primary'} if legacy else None

    def set_upi(self, slot, upi_id, label=''):
        key = self.slot_number(slot)
        upi_id = upi_id.strip()
        if len(upi_id) > 320 or not re.fullmatch(r'[A-Za-z0-9._-]+@[A-Za-z0-9.-]+', upi_id):
            raise ValueError('Use a UPI ID such as name@bank.')
        label = label.strip()
        if len(label) > 32 or any(character in label for character in '\r\n`'):
            raise ValueError('Keep the slot label on one line, within 32 characters, without backticks.')
        data = self.load()
        profile = self.profile(data)
        has_active = self.upi() is not None
        profile.setdefault('upi', {})[key] = {'id':upi_id, 'label':label or f'Slot {slot}'}
        if not has_active:
            profile['active_upi'] = slot
        self.save(data, profile)

    def select_upi(self, slot):
        self.slot_number(slot)
        if self.upi(slot) is None:
            raise ValueError(f'UPI slot {slot} is empty. Set it first.')
        data = self.load()
        profile = self.profile(data)
        profile['active_upi'] = slot
        self.save(data, profile)

    def delete_upi(self, slot):
        key = self.slot_number(slot)
        data = self.load()
        profile = self.profile(data)
        profile.setdefault('upi', {})[key] = None
        # A tombstone prevents a deleted config.UPI slot from silently returning.
        if profile.get('active_upi', 1) == slot:
            for other in range(1, 6):
                if other == slot:
                    continue
                entry = profile['upi'].get(str(other), 'legacy')
                if entry == 'legacy':
                    entry = self.legacy('UPI') if other == 1 else None
                if entry:
                    profile['active_upi'] = other
                    break
        self.save(data, profile)

    @staticmethod
    def coin_name(coin):
        coin = coin.strip().upper()
        if not re.fullmatch(r'[A-Z0-9]{2,12}', coin):
            raise ValueError('Use a coin symbol such as LTC, USDT, SOL, BTC or ETH.')
        return coin

    @staticmethod
    def network_name(network):
        network = network.strip().upper().replace(' ', '-')
        if network and not re.fullmatch(r'[A-Z0-9-]{2,24}', network):
            raise ValueError('Use a short network label such as TRC20, ERC20 or SOLANA.')
        return network

    def set_wallet(self, coin, address, network=''):
        coin, network = self.coin_name(coin), self.network_name(network)
        address = address.strip()
        if not re.fullmatch(r'[A-Za-z0-9:_-]{10,200}', address):
            raise ValueError('Provide a single wallet address, 10–200 characters, without spaces.')
        if coin in ('USDT', 'USDC') and not network:
            raise ValueError(f'Include the {coin} network label, for example TRC20, ERC20 or SOLANA.')
        network = network or {'SOL':'SOLANA','LTC':'LITECOIN','BTC':'BITCOIN','ETH':'ETHEREUM'}.get(coin, '')
        data = self.load()
        profile = self.profile(data)
        profile.setdefault('wallets', {})[coin+'/'+network] = {
            'coin':coin, 'address':address, 'network':network,
        }
        self.save(data, profile)
        return network

    def wallet_entries(self, coin):
        coin = self.coin_name(coin)
        entries = []
        for value in self.profile().get('wallets', {}).values():
            if not isinstance(value, dict) or not all(isinstance(value.get(key), str) for key in ('coin','address','network')):
                raise ValueError('A saved wallet entry is invalid. Repair payment_profiles.json first.')
            if value['coin'] == coin:
                entries.append(value)
        if not entries and self.legacy(coin):
            entries.append({'coin':coin, 'address':self.legacy(coin),
                'network':self.legacy(coin+'_NETWORK') or {'LTC':'LITECOIN','SOL':'SOLANA','BTC':'BITCOIN','ETH':'ETHEREUM'}.get(coin, '')})
        return entries

    def wallet(self, coin, network=''):
        entries = self.wallet_entries(coin)
        if network:
            network = self.network_name(network)
            entries = [entry for entry in entries if entry['network'] == network]
        if len(entries) > 1:
            choices = ', '.join(entry['network'] or 'unspecified' for entry in entries)
            raise ValueError(f'Multiple networks saved. Choose one: {choices}.')
        return entries[0] if entries else None

    def set_emoji(self, asset, emoji):
        asset = self.coin_name(asset)
        emoji = emoji.strip()
        custom = re.fullmatch(r'<a?:[A-Za-z0-9_]{2,32}:\d{15,22}>', emoji)
        unicode_emoji = (0 < len(emoji) <= 12 and all(ord(char) > 127 for char in emoji)
                         and any(unicodedata.category(char) == 'So' for char in emoji))
        if not (custom or unicode_emoji):
            raise ValueError('Provide one emoji, such as 💳, or a Discord custom emoji code.')
        data = self.load()
        profile = self.profile(data)
        profile.setdefault('emoji', {})[asset] = emoji
        self.save(data, profile)


def selected_upi(bot, config):
    entry = PaymentStore(bot, config).upi()
    return entry['id'] if entry else None
