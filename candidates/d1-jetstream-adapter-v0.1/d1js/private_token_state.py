"""Role-private Linux O_PATH ancestry and readable final-directory custody. Never a grant, generic POSIX, Windows ACL or peer-UID isolation claim."""
from __future__ import annotations
import contextlib,hashlib,json,os,stat,sys,time,uuid

class TokenCustodyRefused(RuntimeError):pass
MAX_BYTES=65536
STATE_NAME='token-lifecycle.json';LOCK_NAME='.token-lifecycle.lock'
WITNESS_NAME='.token-binding-witness.json'

def binding_witness(value):
    return {'schema':'bk11-token-binding-witness/v1','binding_sha256':hashlib.sha256(
      json.dumps(value['binding'],sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()}

def need(ok,code):
    if not ok:raise TokenCustodyRefused(code)

def private_file(info,uid):
    need(stat.S_ISREG(info.st_mode) and info.st_uid==uid and stat.S_IMODE(info.st_mode)==0o600
         and info.st_nlink==1,'PRIVATE_REGULAR_SINGLE_LINK_0600_REQUIRED')

def strict_json(raw):
    def pairs(items):
        out={}
        for key,value in items:
            need(key not in out,'PRIVATE_STATE_DUPLICATE_KEY');out[key]=value
        return out
    return json.loads(raw,object_pairs_hook=pairs,
      parse_constant=lambda value:(_ for _ in ()).throw(TokenCustodyRefused('PRIVATE_STATE_NONFINITE')))

class ProtectedRoleTokenStore:
    """Existing role state only. Installer creates directory; no auto budget reset."""
    def __init__(self,path,role):
        need(sys.platform.startswith('linux') and hasattr(os,'O_PATH') and hasattr(os,'geteuid') and os.geteuid()>0,'QUALIFIED_LINUX_OPATH_ROLE_REQUIRED')
        need(role in ('publisher','receiver') and path==f'/var/lib/cerebro/bk11/{role}/token-custody','INSTALLED_EXACT_ROLE_STATE_PATH_REQUIRED')
        self.path=path;self.role=role;self.uid=os.geteuid();self._dir=None;self._lock=None

    def _directory(self):
        need(sys.platform.startswith('linux') and hasattr(os,'O_PATH'),'LINUX_SEARCH_ONLY_ANCESTRY_REQUIRED')
        # Intermediate directories need search, not read/list permission. O_PATH
        # supports fstat/dirfd traversal, but never final-directory I/O or fsync.
        path_flags=os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC
        read_flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC
        fd=os.open('/',path_flags)
        try:
            parts=self.path.split('/')[1:]
            for index,part in enumerate(parts):
                need(part and part not in ('.','..'),'ROLE_PATH_COMPONENT_INVALID')
                info=os.fstat(fd)
                need(stat.S_ISDIR(info.st_mode) and info.st_uid in (0,self.uid)
                  and not stat.S_IMODE(info.st_mode)&0o022,'ROLE_ANCESTRY_CUSTODY_REQUIRED')
                flags=read_flags if index==len(parts)-1 else path_flags
                next_fd=os.open(part,flags,dir_fd=fd);os.close(fd);fd=next_fd
            info=os.fstat(fd)
            need(stat.S_ISDIR(info.st_mode) and info.st_uid==self.uid and stat.S_IMODE(info.st_mode)==0o700,'ROLE_DIRECTORY_0700_REQUIRED')
            return fd
        except BaseException:os.close(fd);raise

    def _stable(self):
        check=self._directory()
        try:
            before=os.fstat(self._dir);after=os.fstat(check)
            need((before.st_dev,before.st_ino)==(after.st_dev,after.st_ino),'ROLE_DIRECTORY_REPLACED')
            lock_info=os.fstat(self._lock);private_file(lock_info,self.uid)
            path_info=os.stat(LOCK_NAME,dir_fd=self._dir,follow_symlinks=False)
            need((lock_info.st_dev,lock_info.st_ino)==(path_info.st_dev,path_info.st_ino),'ROLE_LOCK_REPLACED')
        finally:os.close(check)

    @contextlib.contextmanager
    def locked(self):
        import fcntl
        need(self._dir is None,'ROLE_STORE_ALREADY_OPEN');fd=self._directory();lock=None
        try:
            lock=os.open(LOCK_NAME,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,0o600,dir_fd=fd)
            private_file(os.fstat(lock),self.uid);deadline=time.monotonic()+10
            while True:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    need(time.monotonic()<deadline,'ROLE_SINGLEFLIGHT_TIMEOUT');time.sleep(0.02)
            self._dir=fd;self._lock=lock;self._stable();yield self
        finally:
            self._dir=None;self._lock=None
            if lock is not None:os.close(lock) # Kernel lock releases on close/crash.
            os.close(fd)

    def _read_private(self,name):
        self._stable()
        try:fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,dir_fd=self._dir)
        except FileNotFoundError:raise TokenCustodyRefused('INSTALLER_INITIALIZED_DURABLE_ROLE_BUDGET_REQUIRED') from None
        try:
            info=os.fstat(fd);private_file(info,self.uid);need(0<info.st_size<=MAX_BYTES,'PRIVATE_STATE_SIZE')
            path_info=os.stat(name,dir_fd=self._dir,follow_symlinks=False)
            need((info.st_dev,info.st_ino)==(path_info.st_dev,path_info.st_ino),'PRIVATE_STATE_REPLACED')
            raw=b''
            while len(raw)<=MAX_BYTES:
                block=os.read(fd,4096)
                if not block:break
                raw+=block
            need(0<len(raw)<=MAX_BYTES,'PRIVATE_STATE_SIZE');return strict_json(raw)
        finally:os.close(fd)

    def load(self):
        value=self._read_private(STATE_NAME)
        witness=self._read_private(WITNESS_NAME)
        need(witness==binding_witness(value),'DURABLE_BINDING_COMMIT_REQUIRED_OWNER_HOLD')
        return value

    def _withdraw_witness(self):
        # Under the existing role lock. Once durable, any failed replacement
        # leaves the previous ledger unusable; owner repair preserves its budget.
        self._read_private(WITNESS_NAME);self._stable()
        os.unlink(WITNESS_NAME,dir_fd=self._dir);os.fsync(self._dir)

    def save(self,value,*,initialize=False):
        self._stable();raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        need(len(raw)<=MAX_BYTES,'PRIVATE_STATE_SIZE')
        if initialize:
            for name in (STATE_NAME,WITNESS_NAME):
                try:os.stat(name,dir_fd=self._dir,follow_symlinks=False)
                except FileNotFoundError:pass
                else:raise TokenCustodyRefused('EXISTING_BUDGET_MUST_NOT_BE_REINITIALIZED')
            changed=True
        else:
            previous=self.load()
            changed=binding_witness(previous)!=binding_witness(value)
            if changed:self._withdraw_witness()
        # Errors propagate without mint/credential return. Missing/mismatched
        # witness is persistent HOLD, never automatic reset or recovery.
        self._atomic_write(STATE_NAME,raw)
        if changed:
            witness=json.dumps(binding_witness(value),sort_keys=True,separators=(',',':')).encode()
            self._atomic_write(WITNESS_NAME,witness)

    def _atomic_write(self,name,raw):
        temporary='.token-pending-'+uuid.uuid4().hex;fd=None
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,0o600,dir_fd=self._dir)
            private_file(os.fstat(fd),self.uid)
            offset=0
            while offset<len(raw):
                written=os.write(fd,raw[offset:]);need(written>0,'PRIVATE_WRITE_STALLED');offset+=written
            os.fsync(fd);os.close(fd);fd=None;self._stable()
            os.replace(temporary,name,src_dir_fd=self._dir,dst_dir_fd=self._dir);os.fsync(self._dir)
        finally:
            if fd is not None:os.close(fd)
            try:os.unlink(temporary,dir_fd=self._dir)
            except FileNotFoundError:pass
