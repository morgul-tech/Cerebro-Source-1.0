"""Root-owned D3 factory entry. Capture/hash each core module once; execute those bytes.

Manifest path/digest is installation-owned admission, not Context authority.
External dependency/runtime custody is D2/D3's separate admitted responsibility.
"""
import hashlib,importlib.abc,importlib.util,json,os,re,stat,sys,types
from pathlib import Path

def _protected(path):
    path=Path(path)
    if not path.is_absolute() or os.name!='posix':raise RuntimeError('EDGE_PROTECTED_PATH_REQUIRED')
    for item in [path,*path.parents]:
        info=item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise RuntimeError('SOURCE_ROOT_CUSTODY_REQUIRED')
    return path

class _CapturedCore(importlib.abc.MetaPathFinder,importlib.abc.Loader):
    def __init__(self,prefix,snapshots):self.prefix=prefix;self.snapshots=snapshots
    def find_spec(self,fullname,path=None,target=None):
        if fullname==self.prefix:return importlib.util.spec_from_loader(fullname,self,is_package=True)
        if fullname.startswith(self.prefix+'.'):
            name=fullname[len(self.prefix)+1:]
            if name not in self.snapshots:raise ImportError('UNADMITTED_CORE_MODULE')
            return importlib.util.spec_from_loader(fullname,self,is_package=False)
        return None
    def create_module(self,spec):return None
    def exec_module(self,module):
        if module.__name__==self.prefix:
            module.__path__=[];return
        name=module.__name__[len(self.prefix)+1:];raw,path=self.snapshots[name]
        module.__file__=path
        exec(compile(raw,path,'exec'),module.__dict__)

def open_runtime(admission):
    try:
        bound=admission['core_manifest'];path=_protected(bound['path']);raw=path.read_bytes()
        if len(raw)>65536 or hashlib.sha256(raw).hexdigest()!=bound['sha256']:raise ValueError()
        value=json.loads(raw)
        if value['schema']!='bk11-edge-captured-core/v1' or not 1<=len(value['modules'])<=64:raise ValueError()
        root=_protected(value['root']);snapshots={}
        for item in value['modules']:
            name=item['module']
            if not re.fullmatch(r'[a-z][a-z0-9_]*',name) or name in snapshots:raise ValueError()
            source=_protected(root/(name+'.py'));data=source.read_bytes()
            if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']:raise ValueError()
            snapshots[name]=(data,str(source))
        prefix='_bk11_captured_'+bound['sha256'][:24]
        if prefix not in sys.modules:
            finder=_CapturedCore(prefix,snapshots);sys.meta_path.insert(0,finder)
            try:
                import importlib
                entry=importlib.import_module(prefix+'.edge_adapter')
            finally:sys.meta_path.remove(finder)
            # Later method calls may import captured modules; eagerly load all once.
            sys.meta_path.insert(0,finder)
            try:
                for name in snapshots:importlib.import_module(prefix+'.'+name)
            finally:sys.meta_path.remove(finder)
        else:entry=sys.modules[prefix+'.edge_adapter']
        return entry.open_runtime(admission)
    except Exception:raise RuntimeError('REVIEWED_CAPTURED_CORE_UNAVAILABLE') from None
