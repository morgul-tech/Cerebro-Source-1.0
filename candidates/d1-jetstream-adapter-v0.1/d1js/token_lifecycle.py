"""Bounded cross-oneshot credential reuse ONLY. Every Context operation remains fresh.

JWT metadata is conservative expiry input, never signature/grant validation.
The real Context server validates signed bearer and current assignment/lease per call.
"""
from __future__ import annotations
import base64,hashlib,json,math,time,uuid
from .private_token_state import TokenCustodyRefused,need,strict_json

PROFILE='BK11_ROLE_PRIVATE_M2M_PERSISTENT/v1';SCHEMA='bk11-role-private-token-lifecycle/v1'
WINDOW=31*86400;MAX_ATTEMPTS=120;MARGIN=300;MIN_INITIAL=12*3600;MAX_ISSUER_TTL=86400
CLIENTS={'publisher':'xiPoPVyXGfprRKhtSdkEUpyhouhs7aYr','receiver':'OcYm2M1fTfi2cufFBne7g5BXIQ1f8TaG'}
ISSUER='https://dev-lmzknalapbdhfdua.us.auth0.com'
RESOURCE='https://cerebro-context-deploy-production.up.railway.app'

def number(value):return type(value) in (int,float) and math.isfinite(value) and value>=0
def binding_for(entry,ref,client_id):
    required={'kind','issuer','resource','client_id_env','client_secret_env','role','credential_epoch','grant_epoch','clock_epoch','token_state_dir','lifecycle_profile'}
    need(type(entry)is dict and set(entry)==required and entry['kind']=='AUTH0_CLIENT_CREDENTIALS'
      and entry['lifecycle_profile']==PROFILE,'INSTALLED_TOKEN_LIFECYCLE_PROFILE_REQUIRED')
    role=entry['role'];need(role in CLIENTS and CLIENTS[role]==client_id,'EXACT_APPROVED_ROLE_CLIENT_REQUIRED')
    need(entry['issuer'].rstrip('/')==ISSUER and entry['resource']==RESOURCE,'EXACT_ISSUER_AUDIENCE_REQUIRED')
    need(type(ref)is str and 0<len(ref)<=160 and all(c.isalnum() or c in '_.:@/#=-' for c in ref),'TOKEN_REF_INVALID')
    for key in ['credential_epoch','grant_epoch','clock_epoch']:
        need(type(entry[key])is int and 0<entry[key]<=10**12,'ACTUAL_INSTALLATION_EPOCH_REQUIRED')
    return {'issuer':ISSUER,'client_id':client_id,'audience':RESOURCE,'credential_ref':ref,'role':role,
      'credential_epoch':entry['credential_epoch'],'grant_epoch':entry['grant_epoch'],'clock_epoch':entry['clock_epoch'],'profile':PROFILE}

def system_clock():
    with open('/proc/sys/kernel/random/boot_id','r',encoding='ascii') as stream:boot=stream.read(64).strip()
    need(len(boot)==36,'HOST_CLOCK_BOOT_BASIS_REQUIRED')
    return {'wall':time.time(),'mono':time.monotonic(),'boot':boot}

def checkpoint(value,previous,*,clock_epoch_changed=False):
    need(type(value)is dict and set(value)=={'wall','mono','boot'} and number(value['wall']) and number(value['mono'])
       and type(value['boot'])is str and 0<len(value['boot'])<=64,'CLOCK_BASIS_INVALID')
    if previous is not None:
        need(value['wall']>=previous['wall'],'CLOCK_ROLLBACK_REFUSED')
        if not clock_epoch_changed:
            need(value['boot']==previous['boot'],'HOST_BOOT_CLOCK_REQUALIFICATION_REQUIRED')
            delta=value['mono']-previous['mono']
            need(delta>=0 and abs((value['wall']-previous['wall'])-delta)<=60,'UNEXPECTED_CLOCK_JUMP_REFUSED')
    return dict(value)

def empty_state(binding,clock):
    return {'schema':SCHEMA,'role':binding['role'],'binding':binding,'clock':checkpoint(clock,None),
      'attempts':[],'known_issuances':0,'failures':0,'next_allowed':0,'token':None,'profile_hold':None}

def validate_state(state,role):
    need(type(state)is dict and set(state)=={'schema','role','binding','clock','attempts','known_issuances','failures','next_allowed','token','profile_hold'}
      and state['schema']==SCHEMA and state['role']==role,'FOREIGN_OR_CORRUPT_ROLE_STATE')
    bound=state['binding']
    need(type(bound)is dict and set(bound)=={'issuer','client_id','audience','credential_ref','role','credential_epoch','grant_epoch','clock_epoch','profile'}
       and bound['role']==role and bound['issuer']==ISSUER and bound['audience']==RESOURCE and bound['client_id']==CLIENTS[role]
       and bound['profile']==PROFILE and type(bound['credential_ref'])is str and 0<len(bound['credential_ref'])<=160
       and all(type(bound[k])is int and 0<bound[k]<=10**12 for k in ['credential_epoch','grant_epoch','clock_epoch']),'FOREIGN_OR_CORRUPT_BINDING_STATE')
    checkpoint(state['clock'],None)
    need(type(state['attempts'])is list and len(state['attempts'])<=MAX_ATTEMPTS,'DURABLE_ATTEMPT_LEDGER_INVALID')
    last=-1
    for item in state['attempts']:
        need(type(item)is dict and set(item)=={'at','id','outcome'} and number(item['at']) and item['at']>=last
         and type(item['id'])is str and len(item['id'])==32 and item['outcome'] in ('UNKNOWN','KNOWN_ISSUER_RESPONSE'), 'DURABLE_ATTEMPT_LEDGER_INVALID')
        last=item['at']
    need(type(state['known_issuances'])is int and state['known_issuances']>=0 and type(state['failures'])is int
      and 0<=state['failures']<=12 and number(state['next_allowed']),'DURABLE_BUDGET_FIELDS_INVALID')
    need(state['profile_hold'] in (None,'INITIAL_VALIDITY_UNQUALIFIED'),'CORRUPT_PROFILE_HOLD')
    return state

def token_window(response,started,ended,binding):
    need(number(started) and number(ended) and ended>=started,'REQUEST_CLOCK_BASIS_INVALID')
    need(type(response)is dict and response.get('token_type','').lower()=='bearer','ISSUER_BEARER_RESPONSE_REQUIRED')
    token=response.get('access_token');ttl=response.get('expires_in')
    need(type(token)is str and 0<len(token)<=32768 and not any(c.isspace() for c in token),'ISSUER_ACCESS_TOKEN_REQUIRED')
    need(type(ttl)is int and 0<ttl<=MAX_ISSUER_TTL,'ACTUAL_ISSUER_TTL_UNQUALIFIED')
    parts=token.split('.');need(len(parts)==3,'ACTUAL_JWT_EXPIRY_BASIS_REQUIRED')
    try:
        header=strict_json(base64.urlsafe_b64decode(parts[0]+'='*(-len(parts[0])%4)))
        claims=strict_json(base64.urlsafe_b64decode(parts[1]+'='*(-len(parts[1])%4)))
    except Exception:raise TokenCustodyRefused('ACTUAL_JWT_EXPIRY_BASIS_REQUIRED') from None
    need(type(header)is dict and header.get('alg')=='RS256' and type(claims)is dict,'ACTUAL_JWT_EXPIRY_BASIS_REQUIRED')
    # Reject inconsistent metadata; this is NOT verification/authentication.
    audience=claims.get('aud');audiences=[audience] if type(audience)is str else audience
    need(type(audiences)is list and audiences and all(type(v)is str for v in audiences)
      and RESOURCE in audiences and set(audiences)<= {RESOURCE,ISSUER+'/userinfo'}
      and claims.get('iss')==ISSUER+'/' and claims.get('sub')==binding['client_id']+'@clients'
      and claims.get('azp')==binding['client_id'] and claims.get('gty')=='client-credentials',
      'FOREIGN_ISSUER_CLIENT_AUDIENCE_METADATA')
    issued=claims.get('iat');expiry=claims.get('exp')
    need(number(issued) and number(expiry) and started-MARGIN<=issued<=ended and expiry>issued,'FUTURE_OR_INVALID_ISSUER_TIME')
    effective=min(started+ttl,expiry)-MARGIN
    need(effective-ended>=MIN_INITIAL,'INITIAL_EFFECTIVE_VALIDITY_BELOW_12H')
    return {'access_token':token,'expires_in':ttl,'request_started':started,'received_at':ended,
            'jwt_iat':issued,'jwt_exp':expiry,'effective_expiry':effective,'initial_fit':True,
            'binding_sha256':hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(',',':')).encode()).hexdigest()}

def cached_token(value,now,binding):
    if value is None:return None
    need(type(value)is dict and set(value)=={'access_token','expires_in','request_started','received_at','jwt_iat','jwt_exp','effective_expiry','initial_fit','binding_sha256'}
      and value['initial_fit'] is True,'CORRUPT_TOKEN_WINDOW')
    rebuilt=token_window({'token_type':'Bearer','access_token':value['access_token'],'expires_in':value['expires_in']},
                         value['request_started'],value['received_at'],binding)
    need(rebuilt==value and value['received_at']<=now and value['jwt_iat']<=now,'CORRUPT_OR_FUTURE_TOKEN_WINDOW')
    # INITIAL12h fit is not12h remaining: reuse until the original conservative expiry.
    return value['access_token'] if now<value['effective_expiry'] else None

class TokenLifecycle:
    def __init__(self,store,*,clock=system_clock,diagnostic_stage=None):
        self._store=store;self._clock=clock;self._diagnostic_stage=diagnostic_stage
    def _step(self,stage):
        if self._diagnostic_stage is not None:
            try:self._diagnostic_stage(stage)
            except Exception:pass # Diagnostic observation never changes lifecycle semantics.
    def initialize(self,binding):
        # Explicit installation action under actual role custody, never an implicit
        # cold-miss/reset path. Existing budget cannot be overwritten.
        with self._store.locked() as store:store.save(empty_state(binding,self._clock()),initialize=True)
    def token(self,binding,acquire):
        with self._store.locked() as store:
            self._step('STATE_LOAD')
            state=validate_state(store.load(),binding['role']);old=state['binding']
            epochs=['credential_epoch','grant_epoch','clock_epoch']
            need(all(binding[k]>=old[k] for k in epochs),'INSTALLATION_EPOCH_ROLLBACK_REFUSED')
            if old!=binding:
                need(any(binding[k]>old[k] for k in epochs),'FOREIGN_REF_OR_UNQUALIFIED_BINDING_CHANGE')
                if old['credential_ref']!=binding['credential_ref']:
                    need(binding['credential_epoch']>old['credential_epoch'],'CREDENTIAL_REF_ROTATION_EPOCH_REQUIRED')
            self._step('CLOCK_VALIDATE')
            try:now=checkpoint(self._clock(),state['clock'],clock_epoch_changed=old.get('clock_epoch')!=binding['clock_epoch'])
            except Exception:
                if old!=binding:
                    state.update(binding=dict(binding),token=None,profile_hold=None)
                    self._step('EPOCH_COMMIT')
                    store.save(state) # Refused clock must not undo learned epoch.
                self._step('CLOCK_VALIDATE')
                raise
            # Binding/epoch rotation discards credential only; rolling allowance,
            # unknown attempts and cooldown survive process/credential changes.
            if old!=binding:
                state.update(binding=dict(binding),token=None,profile_hold=None,clock=now)
                self._step('EPOCH_COMMIT')
                store.save(state) # Durable high-water/disposal BEFORE later refusals.
            state['clock']=now
            need(all(item['at']<=now['wall'] for item in state['attempts']),'FUTURE_ATTEMPT_LEDGER_REFUSED')
            state['attempts']=[item for item in state['attempts'] if item['at']>now['wall']-WINDOW]
            self._step('CACHE_VALIDATE')
            reuse=cached_token(state['token'],now['wall'],binding)
            if reuse is not None:
                self._step('CACHE_COMMIT')
                store.save(state);return reuse
            self._step('ACQUISITION_GUARDS')
            need(state['profile_hold'] is None,'QUALIFIED_INITIAL_VALIDITY_PROFILE_REQUIRED')
            need(len(state['attempts'])<MAX_ATTEMPTS,'ROLLING_31_DAY_ATTEMPT_ALLOWANCE_EXHAUSTED')
            need(now['wall']>=state['next_allowed'],'PERSISTENT_ACQUISITION_COOLDOWN')
            attempt={'at':now['wall'],'id':uuid.uuid4().hex,'outcome':'UNKNOWN'}
            state['attempts'].append(attempt);state['token']=None;state['failures']=min(state['failures']+1,12)
            state['next_allowed']=now['wall']+min(300*(2**(state['failures']-1)),3600)
            self._step('ACQUISITION_INTENT_COMMIT')
            store.save(state) # Durable attempt/cooldown BEFORE possible mint.
            self._step('ISSUER_CALLBACK_ENTER')
            try:response=acquire()
            except Exception:raise TokenCustodyRefused('UNKNOWN_ISSUER_OUTCOME_ATTEMPT_RETAINED') from None
            self._step('POST_ISSUER_VALIDATE')
            ended=checkpoint(self._clock(),now);state['clock']=ended
            known=type(response)is dict and type(response.get('access_token'))is str and bool(response['access_token'])
            if known:attempt['outcome']='KNOWN_ISSUER_RESPONSE';state['known_issuances']+=1
            try:window=token_window(response,now['wall'],ended['wall'],binding)
            except Exception:
                state['profile_hold']='INITIAL_VALIDITY_UNQUALIFIED'
                self._step('TOKEN_COMMIT')
                store.save(state)
                raise TokenCustodyRefused('QUALIFIED_INITIAL_VALIDITY_PROFILE_REQUIRED') from None
            state.update(token=window,failures=0,next_allowed=0,profile_hold=None)
            self._step('TOKEN_COMMIT')
            store.save(state)
            return window['access_token']
