"""Vercel-hosted dashboard. Only this server sees SFTP credentials."""
import functools
import hmac
import json
import logging
import os
import re
import time
import sys
import copy
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import paramiko
from flask import Flask, jsonify, request, send_from_directory, g
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

app = Flask(__name__, static_folder=str(Path(__file__).resolve().parent / 'public'), static_url_path='/assets')
app.config['MAX_CONTENT_LENGTH'] = 32768
LOG = logging.getLogger(__name__)
BOT_ROOT=Path(__file__).resolve().parent.parent
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0,str(BOT_ROOT))
from cogs._helpers.dashboard_auth import normalized_username, verify_password

LOGIN_ATTEMPTS = OrderedDict()
LOGIN_LOCK = threading.Lock()
CUSTOMER_ACTIONS = {
    'deal_complete','deal_cancel','deal_add','deal_margin','deal_undo',
    'upi_save','upi_delete','wallet_save','emoji_save','prefix_save',
    'presence_save','presence_clear','rotator_start','rotator_stop',
    'reminder_add','reminder_cancel'}


def sessions():
    secret = os.environ.get('DASHBOARD_PASSWORD','')
    if len(secret) < 8:
        raise ValueError('Configure the dashboard server password first.')
    return URLSafeTimedSerializer(secret, salt='customer-dashboard-v1')


def logins(sftp):
    try:
        return read_remote(sftp,'/.dashboard/logins.json')
    except FileNotFoundError:
        return {}


def scoped_result(result):
    if g.account is not None and result.get('account') != g.account:
        return jsonify(error='Result does not belong to your account.'),403
    return jsonify(result)

def local_mode():
    return os.environ.get('DASHBOARD_MODE','local').lower()=='local'

class LocalFiles:
    def path(self,path):
        target=(BOT_ROOT/path.lstrip('/')).resolve()
        if not target.is_relative_to((BOT_ROOT/'.dashboard').resolve()):
            raise ValueError('Invalid dashboard path.')
        return target

    def open(self,path,mode):
        target=self.path(path)
        if 'w' in mode:
            target.parent.mkdir(parents=True,exist_ok=True)
        return target.open(mode)

    def rename(self,source,target):
        self.path(source).replace(self.path(target))

def local_deals(data,selected_date=None,ledger_data=None):
    if str(BOT_ROOT) not in sys.path:
        sys.path.insert(0,str(BOT_ROOT))
    from types import SimpleNamespace
    from cogs._helpers.deal_store import DealStore,dashboard_summary
    ledger=BOT_ROOT/'deal_data.json'
    if ledger_data is None:
        if not ledger.exists():
            return data
        ledger_data=json.loads(ledger.read_text(encoding='utf-8'))
    for key in ledger_data.get('accounts',{}):
        if not re.fullmatch(r'user:[0-9]{15,22}',key):
            continue
        bot=SimpleNamespace(user=SimpleNamespace(id=int(key.split(':')[1])))
        selected=data.setdefault('accounts',{}).setdefault(key,{'account':key,'name':'Local account','updated_at':0,'started_at':0,'prefix':','})
        selected['deal_reports']=dashboard_summary(bot,selected_date=selected_date,ledger_data=ledger_data)
        selected['pending_deals']=[row for row in ledger_data['accounts'][key].get('pending',[]) if row.get('status')=='pending']
        selected['deal_error']=None
        selected['dashboard_mode']='local' if local_mode() else 'hosting'
    return data
ACTIONS = {'deal_complete','deal_cancel','deal_add','deal_margin','deal_undo','upi_save','upi_delete','wallet_save','emoji_save','rates_save','prefix_save',
           'presence_save','presence_clear','rotator_start','rotator_stop','status_save','reminder_add','reminder_cancel','autoreply_save','autoreply_delete'}


@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    response.headers['X-Frame-Options'] = 'DENY'
    return response


def protected(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        password = os.environ.get('DASHBOARD_PASSWORD','')
        if len(password)<8:
            return jsonify(error='Set DASHBOARD_PASSWORD to a password of at least 8 characters in Vercel.'),503
        token = request.headers.get('Authorization','').removeprefix('Bearer ')
        g.account = None
        if not hmac.compare_digest(token.encode(),password.encode()):
            try:
                identity = sessions().loads(token,max_age=12*60*60)
                if not isinstance(identity,dict):
                    raise BadSignature('Invalid session')
                with connection() as sftp:
                    user = logins(sftp).get(identity.get('account'),{})
                if not user or not hmac.compare_digest(str(user.get('version','')),str(identity.get('version','!'))):
                    raise BadSignature('Revoked session')
                g.account = identity['account']
            except (BadSignature,SignatureExpired,KeyError,TypeError):
                return jsonify(error='Please sign in again.'),401
        if request.method == 'POST' and not request.is_json:
            return jsonify(error='JSON request required.'),415
        return function(*args, **kwargs)
    return wrapped


class PinnedHost(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        expected = os.environ.get('SFTP_HOST_KEY_MD5','').replace(':','').lower()
        if len(expected)!=32 or not hmac.compare_digest(expected,key.get_fingerprint().hex()):
            raise paramiko.SSHException('SFTP host key does not match the configured fingerprint.')


@contextmanager
def connection():
    if local_mode():
        yield LocalFiles()
        return
    required = ('SFTP_HOST','SFTP_USERNAME','SFTP_PASSWORD','SFTP_HOST_KEY_MD5')
    if not all(os.environ.get(name) for name in required):
        raise ValueError('Set the SFTP environment variables in your Vercel project.')
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedHost())
    try:
        client.connect(os.environ['SFTP_HOST'],port=int(os.environ.get('SFTP_PORT','2022')),
                       username=os.environ['SFTP_USERNAME'],password=os.environ['SFTP_PASSWORD'],
                       timeout=10,auth_timeout=10,banner_timeout=10,look_for_keys=False,allow_agent=False)
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(12)
            yield sftp
    finally:
        client.close()


def read_remote(sftp,path):
    with sftp.open(path,'rb') as file:
        data = file.read(1024*1024+1)
    if len(data)>1024*1024:
        raise ValueError('Dashboard state is too large.')
    return json.loads(data)


@app.errorhandler(Exception)
def error(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc,HTTPException):
        return jsonify(error=exc.description),exc.code
    # Only exception types go into logs, never credentials or file contents.
    LOG.warning('Dashboard request failed: %s',type(exc).__name__)
    if isinstance(exc,FileNotFoundError):
        message='Dashboard bridge is not ready. Start main.py on your PC, then refresh.' if local_mode() else 'Dashboard bridge is not ready. Restart the bot from your hosting panel, then refresh.'
    elif isinstance(exc,paramiko.AuthenticationException):
        message='SFTP login failed. Check the Vercel SFTP environment variables.'
    elif isinstance(exc,ValueError):
        message=str(exc)[:200]
    else:
        message='Could not reach the bot hosting through SFTP. Check connectivity and the configured host fingerprint.'
    return jsonify(error=message),503


@app.get('/health')
def health():
    return jsonify(app='guri-dashboard')


@app.get('/')
def index():
    return send_from_directory(app.static_folder,'index.html')


@app.post('/api/login')
def login():
    if not request.is_json:
        return jsonify(error='JSON request required.'),415
    data=request.get_json()
    if not isinstance(data,dict) or not isinstance(data.get('username'),str) or not isinstance(data.get('password'),str):
        return jsonify(error='Enter your Discord username and dashboard password.'),400
    username=normalized_username(data['username'])
    password=data['password']
    if not username or len(username)>64 or not 8<=len(password)<=256:
        return jsonify(error='Incorrect username or dashboard password.'),401
    # Bound the in-memory limiter. Use the socket address, never a client-supplied proxy header.
    now=time.monotonic()
    keys=('ip:'+str(request.remote_addr),'user:'+username)
    with LOGIN_LOCK:
        for key in keys:
            attempts=[at for at in LOGIN_ATTEMPTS.get(key,[]) if now-at<900]
            if len(attempts)>=10:
                return jsonify(error='Too many login attempts. Try again in 15 minutes.'),429
            LOGIN_ATTEMPTS[key]=attempts+[now]
            LOGIN_ATTEMPTS.move_to_end(key)
        while len(LOGIN_ATTEMPTS)>4096:
            LOGIN_ATTEMPTS.popitem(last=False)
    # Admin access remains available with the reserved admin username.
    admin=os.environ.get('DASHBOARD_PASSWORD','')
    if username=='admin' and len(admin)>=8 and hmac.compare_digest(password.encode(),admin.encode()):
        return jsonify(token=admin,role='admin')
    with connection() as sftp:
        users=logins(sftp)
    matches=[(account,user) for account,user in users.items() if user.get('username')==username]
    account,user=matches[0] if len(matches)==1 else ('',{})
    # Do the same password work for unknown usernames.
    valid=verify_password(password,user.get('password_hash','0'*32+':'+'0'*64))
    if not account or not valid:
        return jsonify(error='Incorrect username or dashboard password.'),401
    return jsonify(token=sessions().dumps({'account':account,'version':user['version']}),role='customer')


@app.get('/api/state')
@protected
def state():
    with connection() as sftp:
        data=read_remote(sftp,'/.dashboard/state.json')
        ledger=read_remote(sftp,'/deal_data.json') if not local_mode() else None
    data=local_deals(data,request.args.get('date'),ledger_data=ledger)
    if g.account is not None:
        selected=data.get('accounts',{}).get(g.account)
        if selected is None:
            return jsonify(error='Your bot account has not connected yet.'),409
        selected=copy.deepcopy(selected)
        # These legacy settings are shared across bot accounts. Customers cannot read or change them.
        selected.update(rates={},statuses='',status_emojis='',autoreplies=[])
        data={'accounts':{g.account:selected},'role':'customer','allowed_actions':sorted(CUSTOMER_ACTIONS)}
    else:
        data['role']='admin'
        data['allowed_actions']=sorted(ACTIONS)
    return jsonify(data)


@app.post('/api/action')
@protected
def action():
    data=request.get_json()
    if not isinstance(data,dict) or data.get('action') not in ACTIONS or not isinstance(data.get('payload'),dict):
        return jsonify(error='Invalid action.'),400
    identity=data.get('id','')
    if not isinstance(identity,str) or not re.fullmatch(r'[0-9a-f]{32}',identity):
        return jsonify(error='Invalid request ID.'),400
    account=data.get('account')
    if not isinstance(account,str) or not re.fullmatch(r'user:[0-9]{15,22}',account):
        return jsonify(error='Select an account first.'),400
    if g.account is not None and (account!=g.account or data['action'] not in CUSTOMER_ACTIONS):
        return jsonify(error='This action is not available for your account.'),403
    with connection() as sftp:
        try:
            result=read_remote(sftp,f'/.dashboard/results/{identity}.json')
            return scoped_result(result)
        except FileNotFoundError:
            pass
        state=read_remote(sftp,'/.dashboard/state.json')
        selected=state.get('accounts',{}).get(account)
        license_status = selected.get('license') if selected else None
        if isinstance(license_status,dict) and license_status.get('expires_at') is not None and time.time() >= license_status['expires_at']:
            return jsonify(error='License expired. Ask your seller to renew it.'),409
        if not selected or time.time()-selected.get('updated_at',0)>90:
            return jsonify(error='Bot is offline or its dashboard bridge has stopped. Restart it first.'),409
        queued={'account':account,'action':data['action'],'payload':data['payload'],'created_at':int(time.time())}
        target=f'/.dashboard/inbox/{identity}.json'
        temporary=target+'.upload'
        with sftp.open(temporary,'wb') as file:
            file.write(json.dumps(queued).encode())
        sftp.rename(temporary,target)
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            try:
                result=read_remote(sftp,f'/.dashboard/results/{identity}.json')
                return scoped_result(result)
            except FileNotFoundError:
                time.sleep(0.6)
    return jsonify(ok=None,id=identity,message='Queued. The bot will apply this change shortly.'),202


@app.get('/api/result/<identity>')
@protected
def result(identity):
    if not re.fullmatch(r'[0-9a-f]{32}',identity):
        return jsonify(error='Invalid request ID.'),400
    with connection() as sftp:
        try:
            return scoped_result(read_remote(sftp,f'/.dashboard/results/{identity}.json'))
        except FileNotFoundError:
            return jsonify(ok=None,message='Still queued.'),202


if __name__=='__main__':
    app.run(host='127.0.0.1',port=8080,debug=False)
