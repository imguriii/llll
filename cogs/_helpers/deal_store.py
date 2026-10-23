"""Per-account manual deal ledger, using exact decimal amounts and IST reports."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import re

from cogs._helpers.payment_store import account_key

ROOT = Path(__file__).resolve().parents[2]
DEALS_FILE = ROOT / 'deal_data.json'
IST = timezone(timedelta(hours=5, minutes=30), name='IST')
CENT = Decimal('0.01')
RATE_DEFAULT = object()
PERIODS = {'daily':'daily','day':'daily','today':'daily', 'weekly':'weekly','week':'weekly',
           'month':'month','monthly':'month', 'alltime':'alltime','all':'alltime','lifetime':'alltime'}


class DealError(ValueError):
    pass


def parse_money(text, currency='USD', allow_zero=False):
    text = text.strip()
    if currency == 'INR':
        text = re.sub(r'\s*(?:per\s*(?:\$|usd)|/\s*\$)\s*$', '', text, flags=re.I)
    unit = r'\$|USD' if currency == 'USD' else r'₹|INR|Rs\.?'
    places = 2 if currency == 'USD' else 4
    number = r'((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,'+str(places)+r'})?)'
    match = re.fullmatch(r'\s*(?:(?:'+unit+r')\s*)?'+number+r'\s*(?:'+unit+r')?\s*', text, re.I)
    if not match:
        example = '10$ or 10.50' if currency == 'USD' else '3₹ or 3.50'
        raise DealError(f'Use {example}. Amounts must be positive, with at most {places} decimal places.')
    value = Decimal(match.group(1).replace(',', ''))
    maximum = Decimal('1000000000000') if currency == 'USD' else Decimal('1000000')
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero) or value > maximum:
        raise DealError('Enter a positive deal amount within the supported range.' if currency == 'USD'
                        else 'Profit per dollar must be from ₹0 to ₹1,000,000.')
    return value


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp must include a timezone.')
    return parsed


def report_window(period, now=None):
    key = PERIODS.get(period.strip().lower().replace(' ', ''))
    if key is None:
        raise DealError('Choose daily, weekly, month or alltime.')
    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = (midnight if key == 'daily' else midnight-timedelta(days=now.weekday())
             if key == 'weekly' else midnight.replace(day=1) if key == 'month' else None)
    return key, start, now


def inventory_entries(entries, now):
    stock = Decimal(0)
    calculated = []
    for original in sorted(entries, key=lambda row:(timestamp(row['created_at']), row['id'])):
        if original.get('voided_at') or timestamp(original['created_at']) > now:
            continue
        entry = dict(original)
        value = Decimal(entry['usd'])
        if entry['direction'] == 'MM':
            entry['profit_inr'] = None
            entry['matched_usd'] = '0.00'
            entry['unmatched_usd'] = '0.00'
        elif entry['direction'] == 'C2I':
            stock += value
            entry['profit_inr'] = '0.00'
            entry['matched_usd'] = '0.00'
            entry['unmatched_usd'] = '0.00'
        else:
            matched = min(stock,value)
            stock -= matched
            entry['matched_usd'] = f'{matched:.2f}'
            entry['unmatched_usd'] = f'{value-matched:.2f}'
            entry['profit_inr'] = (f'{(matched*Decimal(entry["rate"])).quantize(CENT,rounding=ROUND_HALF_UP):.2f}'
                                   if entry.get('rate') is not None else None)
        calculated.append(entry)
    return calculated, stock


class DealStore:
    def __init__(self, bot, path=None):
        self.key = account_key(bot)
        self.path = Path(path) if path is not None else DEALS_FILE

    def load(self):
        if not self.path.exists():
            return {'version':1, 'accounts':{}}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('accounts'), dict):
                raise ValueError
            profile = data['accounts'].get(self.key, {})
            if not isinstance(profile, dict) or not isinstance(profile.get('deals', []), list):
                raise ValueError
            rate = profile.get('profit_rate')
            if rate is not None:
                parse_money(rate, 'INR', allow_zero=True)
            drafts = profile.get('pending',[])
            if not isinstance(drafts,list):
                raise ValueError
            draft_ids = set()
            for draft in drafts:
                if (not isinstance(draft,dict) or type(draft.get('id')) is not int
                        or draft['id']<=0 or draft['id'] in draft_ids
                        or draft.get('direction') not in ('C2I','I2C')
                        or draft.get('status') not in ('pending','completed','cancelled')):
                    raise ValueError
                draft_ids.add(draft['id'])
                parse_money(draft['usd'])
                timestamp(draft['created_at'])
                if draft.get('margin') is not None:
                    parse_money(draft['margin'],'INR',allow_zero=True)
            ids = set()
            for deal in profile.get('deals', []):
                if (not isinstance(deal, dict) or type(deal.get('id')) is not int
                        or deal['id'] <= 0 or deal['id'] in ids or deal.get('direction') not in ('C2I','I2C','MM')):
                    raise ValueError
                ids.add(deal['id'])
                timestamp(deal['created_at'])
                if deal.get('voided_at') is not None:
                    timestamp(deal['voided_at'])
                parse_money(deal['usd'])
                if deal.get('rate') is not None:
                    parse_money(deal['rate'], 'INR', allow_zero=True)
                    profit = Decimal(deal['profit_inr'])
                    if not profit.is_finite() or profit < 0:
                        raise ValueError
                elif deal.get('profit_inr') is not None:
                    raise ValueError
            return data
        except (OSError, ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as exc:
            raise DealError('Cannot read deal_data.json. Repair the saved data before making changes; nothing was overwritten.') from exc

    def profile(self, data):
        profile = data['accounts'].setdefault(self.key, {})
        profile.setdefault('profit_rate', None)
        profile.setdefault('deals', [])
        profile.setdefault('pending', [])
        return profile

    def save(self, data):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
        temporary.replace(self.path)

    def set_profit(self, text):
        rate = parse_money(text, 'INR', allow_zero=True)
        data = self.load()
        self.profile(data)['profit_rate'] = str(rate)
        self.save(data)
        return rate

    def add(self, direction, text, source_id=None, now=None, margin=RATE_DEFAULT, allow_unmatched=False, metadata=None):
        if direction not in ('C2I','I2C','MM'):
            raise DealError('Choose C2I or I2C.')
        usd = parse_money(text)
        data = self.load()
        profile = self.profile(data)
        if source_id is not None:
            for old in profile['deals']:
                if old.get('source_id') == str(source_id):
                    return old, False
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        _, available = inventory_entries(profile['deals'],moment)
        if direction == 'I2C' and usd > available and not allow_unmatched:
            raise DealError(f'Only $ {available:,.2f} of recorded crypto inventory is available. Record your C2I purchase first.')
        saved_rate=profile['profit_rate'] if margin is RATE_DEFAULT else margin
        rate = None if direction=='MM' else Decimal(0) if direction=='C2I' else parse_money(str(saved_rate),'INR',allow_zero=True) if saved_rate is not None else None
        profit = (usd*rate).quantize(CENT, rounding=ROUND_HALF_UP) if rate is not None else None
        deal = {'id':max((deal['id'] for deal in profile['deals']), default=0)+1,
                'direction':direction, 'usd':f'{usd:.2f}',
                'rate':str(rate) if rate is not None else None,
                'profit_inr':f'{profit:.2f}' if profit is not None else None,
                'created_at':moment.isoformat(),
                'source_id':str(source_id) if source_id is not None else None,
                'voided_at':None}
        if metadata:
            for key in ('origin','ticket_id','source_message_id'):
                if key in metadata:
                    deal[key]=str(metadata[key])[:200]
        profile['deals'].append(deal)
        self.save(data)
        calculated, _ = inventory_entries(profile['deals'],moment)
        return next(entry for entry in calculated if entry['id']==deal['id']), True

    def draft(self,direction,text,channel_id=None,source_id=None,fx_rate=None,inr=None):
        if direction not in ('C2I','I2C'):
            raise DealError('Choose C2I or I2C.')
        usd=parse_money(text)
        data=self.load()
        profile=self.profile(data)
        if source_id is not None:
            old=next((row for row in profile['pending'] if row.get('source_id')==str(source_id)),None)
            if old is not None:
                return old
        if sum(row['status']=='pending' for row in profile['pending'])>=100:
            raise DealError('You have 100 pending deals. Complete or cancel some first.')
        quote={'id':max((row['id'] for row in profile['pending']),default=0)+1,
               'direction':direction,'usd':f'{usd:.2f}',
               'margin':profile['profit_rate'],
               'fx_rate':str(fx_rate) if fx_rate is not None else None,
               'inr':str(inr) if inr is not None else None,
               'channel_id':str(channel_id) if channel_id is not None else None,
               'source_id':str(source_id) if source_id is not None else None,
               'created_at':datetime.now(timezone.utc).isoformat(),'status':'pending'}
        profile['pending'].append(quote)
        self.save(data)
        return quote

    def pending(self,channel_id=None):
        data=self.load()
        return [row for row in self.profile(data)['pending'] if row['status']=='pending'
                and (channel_id is None or row.get('channel_id')==str(channel_id))]

    def find_draft(self,profile,quote_id=None,channel_id=None):
        if quote_id is None:
            matches=[row for row in profile['pending'] if row['status']=='pending'
                     and (channel_id is None or row.get('channel_id')==str(channel_id))]
            if not matches:
                raise DealError('No pending deal in this chat. Completed Swappy DM transcripts are imported automatically.')
            if len(matches)>1:
                ids=', '.join('P'+f'{row["id"]:04d}' for row in matches[-5:])
                raise DealError('More than one deal is pending. Choose its ID: '+ids+'.')
            return matches[0]
        value=str(quote_id).strip().upper().removeprefix('P')
        if not value.isdigit():
            raise DealError('Use a pending deal ID such as P0001.')
        row=next((row for row in profile['pending'] if row['id']==int(value)),None)
        if row is None:
            raise DealError('Pending deal not found.')
        return row

    def complete(self,quote_id=None,channel_id=None):
        data=self.load()
        profile=self.profile(data)
        quote=self.find_draft(profile,quote_id,channel_id)
        if quote['status']=='cancelled':
            raise DealError('This pending deal was cancelled.')
        if quote['status']=='completed':
            raise DealError('This deal is already completed; it has not been counted again.')
        # The stable source ID makes retries safe if saving completion is interrupted.
        margin=quote.get('margin') if quote.get('margin') is not None else RATE_DEFAULT
        entry,created=self.add(quote['direction'],quote['usd'],source_id='pending:'+str(quote['id']),margin=margin)
        data=self.load()
        quote=self.find_draft(self.profile(data),quote['id'])
        quote['status']='completed'
        quote['completed_deal_id']=entry['id']
        quote['completed_at']=entry['created_at']
        self.save(data)
        return entry,created

    def cancel_draft(self,quote_id=None,channel_id=None):
        data=self.load()
        quote=self.find_draft(self.profile(data),quote_id,channel_id)
        if quote['status']!='pending':
            raise DealError('Only a pending deal can be cancelled.')
        quote['status']='cancelled'
        self.save(data)
        return quote

    def undo(self, deal_id=None):
        data = self.load()
        entries = self.profile(data)['deals']
        if deal_id is None:
            entry = next((entry for entry in reversed(entries) if not entry.get('voided_at')), None)
        else:
            entry = next((entry for entry in entries if entry['id'] == deal_id), None)
        if entry is None:
            raise DealError('No matching recorded deal found.')
        if entry.get('voided_at'):
            raise DealError('That deal has already been undone.')
        entry['voided_at'] = datetime.now(timezone.utc).isoformat()
        self.save(data)
        return entry

    def summary(self, period='alltime', now=None, selected_date=None):
        key, start, now = report_window(period, now)
        data = self.load()
        profile = self.profile(data)
        calculated, stock = inventory_entries(profile['deals'],now)
        end=None
        if selected_date is not None:
            try:
                day=date.fromisoformat(selected_date)
            except (ValueError,TypeError):
                raise DealError('Choose a valid date in YYYY-MM-DD format.') from None
            start=datetime.combine(day,datetime.min.time(),tzinfo=IST)
            end=start+timedelta(days=1)
            key='custom'
        entries = [entry for entry in calculated if (start is None or timestamp(entry['created_at']) >= start) and (end is None or timestamp(entry['created_at']) < end)]
        totals = {}
        for direction in ('C2I','I2C','MM'):
            selected = [entry for entry in entries if entry['direction'] == direction]
            totals[direction] = {'count':len(selected),
                'usd':sum((Decimal(entry['usd']) for entry in selected), Decimal(0)),
                'profit':sum((Decimal(entry['profit_inr']) for entry in selected if entry['profit_inr'] is not None), Decimal(0))}
        return {'period':key, 'start':start, 'now':now, 'entries':entries,
                'totals':totals, 'stock':stock, 'current_rate':profile.get('profit_rate'),
                'unmatched':sum(Decimal(entry.get('unmatched_usd','0')) for entry in entries),
                'unrated':sum(entry['direction']=='I2C' and entry['profit_inr'] is None for entry in entries)}


def rate_text(rate):
    return f'{Decimal(rate):,.4f}'.rstrip('0').rstrip('.')


def entry_message(deal, created, prefix):
    if not created:
        return f'This message is already recorded as deal **#{deal["id"]:04d}**.'
    profit = f'₹ {Decimal(deal["profit_inr"]):,.2f}' if deal['profit_inr'] is not None else 'Rate not set'
    lines = ['### {{emoji:verified}} **Deal recorded**  `'+deal['direction']+'`',
             f'-# ENTRY #{deal["id"]:04d}', '',
             '{{emoji:money}} **Volume**  ·  `$ '+f'{Decimal(deal["usd"]):,.2f}'+'`',
             '{{emoji:chart}} **Estimated profit**  ·  `'+profit+'`']
    if deal['direction']=='C2I':
        lines.append('-# Purchase added to inventory · profit is counted only when sold.')
    elif deal['rate'] is not None:
        lines.append('-# Saved rate · ₹ '+rate_text(deal['rate'])+' per $1')
    else:
        lines.append(f'-# No rate was set for this entry. Use `{prefix}setprofit 3₹` for future deals.')
    lines.append(f'-# `{prefix}mydata` · Summary  |  `{prefix}dealundo {deal["id"]}` · Undo')
    return '\n'.join(lines)


def summary_message(summary, prefix):
    titles = {'daily':'Today','weekly':'This week','month':'This month','alltime':'All time'}
    start, now = summary['start'], summary['now']
    window = f'{start:%d %b} – {now:%d %b %Y}' if start else f'Through {now:%d %b %Y}'
    totals = summary['totals']
    count = sum(item['count'] for item in totals.values())
    volume = sum(item['usd'] for item in totals.values())
    profit = sum(item['profit'] for item in totals.values())
    lines = ['### {{emoji:chart}} My data  `'+titles[summary['period']]+'`',
             f'-# {window} · IST',
             '> **Est. profit** `₹ '+f'{profit:,.2f}'+'`  ·  **Deals** `'+str(count)+'`',
             '> **Volume** `$ '+f'{volume:,.2f}'+'`  ·  **Stock** `$ '+f'{summary["stock"]:,.2f}'+'`',
             '',
             '  ·  '.join('**'+direction+'** `$ '+f'{totals[direction]["usd"]:,.2f}'+'`' for direction in ('C2I','I2C','MM'))]
    if not count:
        lines.append('-# No deals in this period.')
    if summary['unrated']:
        lines.append(f'-# {summary["unrated"]} sale(s) excluded: no saved margin.')
    if summary.get('unmatched'):
        lines.append(f'-# $ {summary["unmatched"]:,.2f} sold without recorded stock; profit excluded.')
    rate = summary['current_rate']
    lines += ['-# Margin '+('₹ '+rate_text(rate)+'/$' if rate is not None else 'not set')+' · Stock is all time',
              f'-# `{prefix}mydata daily` · `weekly` · `month` · `alltime`']
    return '\n'.join(lines)


def dashboard_summary(bot, selected_date=None, ledger_data=None):
    """Bounded, JSON-safe reports shared by Discord and the dashboard."""
    store = DealStore(bot)
    if ledger_data is not None:
        store.load=lambda:ledger_data
    reports = {}
    for period in ('daily','weekly','month','alltime')+(('custom',) if selected_date else ()):
        data = store.summary('daily' if period=='custom' else period,selected_date=selected_date if period=='custom' else None)
        entries = sorted(data['entries'],key=lambda row:(timestamp(row['created_at']),row['id']),reverse=True)
        reports[period] = {
            'totals':{direction:{'count':value['count'],'usd':str(value['usd']),'profit':str(value['profit'])}
                      for direction,value in data['totals'].items()},
            'profit':str(sum((value['profit'] for value in data['totals'].values()),Decimal(0))),
            'count':len(entries), 'stock':str(data['stock']), 'rate':data['current_rate'],
            'unrated':data['unrated'],'unmatched':str(data['unmatched']),
            'start':data['start'].isoformat() if data['start'] else None,
            'through':data['now'].isoformat(),
            'entries':[{key:entry.get(key) for key in ('id','direction','usd','profit_inr','rate','created_at','matched_usd','origin','ticket_id')}
                       for entry in entries[:20]],
        }
        reports[period]['entries_by_type']={direction:[{key:entry.get(key) for key in ('id','direction','usd','profit_inr','rate','created_at','matched_usd','origin','ticket_id')} for entry in entries if entry['direction']==direction][:20] for direction in ('C2I','I2C','MM')}
    return reports
